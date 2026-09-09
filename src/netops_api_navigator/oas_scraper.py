"""Sync OpenAPI specs from ReadMe.io-hosted developer documentation.

Uses the ReadMe ``.md`` URL protocol: appending ``.md`` to any reference
page URL returns the full OpenAPI 3.x spec embedded in a markdown code block.

Discovery flow:
    1. Fetch the root reference page HTML (SSR sidebar lists all endpoints).
    2. Extract endpoint slugs from sidebar ``<a href>`` links.
    3. For each slug, fetch ``{slug}.md`` to get the per-endpoint OAS spec.
    4. Cache specs to disk with TTL-based freshness.

Resilience:
    * A realistic browser ``User-Agent`` is sent on every request.
    * Per-host pacing keeps request rates well below the upstream's 429
      threshold.
    * Transient HTTP errors (429, 5xx) are retried with exponential backoff
      and ``Retry-After`` honoured when present.
    * Failures are summarised with structured counts (HTTP status histogram)
      so unhealthy sync runs can be diagnosed quickly from the logs.
"""

from __future__ import annotations

import json
import random
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import structlog

logger = structlog.get_logger("oas_scraper")

DOCS_HOST = "https://developer.arubanetworks.com"

# Browser-like UA — many CDNs (Akamai/Cloudflare) increasingly block plain
# httpx/python defaults outright.  Keep this string realistic and current.
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_DEFAULT_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Tunables — keep conservative.  ReadMe.io starts returning 429 above ~5
# requests/sec for unauthenticated clients, so we cap well below that.
_HTTP_TIMEOUT = 30.0
_MAX_WORKERS = 3
_MIN_INTERVAL_SECONDS = 0.25  # ≈ 4 requests/sec/host
_MAX_RETRIES = 5
_BACKOFF_BASE = 1.0
_BACKOFF_CAP = 30.0


# ----- Spec source definitions ------------------------------------------------


@dataclass
class SpecSource:
    """A ReadMe.io API documentation site to fetch.

    Each source maps to one ``/reference`` site.  Endpoint slugs are
    auto-discovered from the server-side rendered sidebar HTML.
    """

    name: str
    """Human-readable label (e.g. "MRT", "Config")."""

    reference_path: str
    """Path under the docs host, e.g. ``/new-central/reference``."""

    # Populated after auto-discovery
    endpoint_slugs: list[str] = field(default_factory=list)


# Default sources — extend this list to add new API doc sites later.
DEFAULT_SOURCES: list[SpecSource] = [
    SpecSource(name="MRT", reference_path="/new-central/reference"),
    SpecSource(name="Config", reference_path="/new-central-config/reference"),
]


# ----- Per-host pacing --------------------------------------------------------


class _HostPacer:
    """Tiny per-host serialiser that enforces a minimum interval between
    requests against the same origin.

    Composes well with ``ThreadPoolExecutor`` and avoids needing async glue.
    """

    def __init__(self, min_interval: float) -> None:
        self._min_interval = min_interval
        self._locks: dict[str, threading.Lock] = {}
        self._next_allowed: dict[str, float] = {}
        self._registry_lock = threading.Lock()

    def _lock_for(self, host: str) -> threading.Lock:
        with self._registry_lock:
            lock = self._locks.get(host)
            if lock is None:
                lock = threading.Lock()
                self._locks[host] = lock
            return lock

    def wait(self, url: str) -> None:
        host = urlsplit(url).netloc
        if not host:
            return
        lock = self._lock_for(host)
        with lock:
            now = time.monotonic()
            next_ok = self._next_allowed.get(host, 0.0)
            if next_ok > now:
                time.sleep(next_ok - now)
                now = time.monotonic()
            self._next_allowed[host] = now + self._min_interval


_PACER = _HostPacer(_MIN_INTERVAL_SECONDS)


# ----- Slug discovery ---------------------------------------------------------

_SLUG_RE = re.compile(r'href="[^"]*?/reference/([a-zA-Z0-9_-]+)"')


def _extract_endpoint_slugs(html: str) -> list[str]:
    """Extract unique endpoint slugs from sidebar links in SSR HTML.

    ReadMe SuperHub renders the full sidebar server-side so all endpoint
    links are present in the initial HTML response.
    """
    seen: set[str] = set()
    slugs: list[str] = []
    for match in _SLUG_RE.finditer(html):
        slug = match.group(1)
        if slug not in seen:
            seen.add(slug)
            slugs.append(slug)
    return slugs


# ----- Markdown spec extraction -----------------------------------------------

_JSON_BLOCK_RE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)


def _extract_oas_from_markdown(text: str) -> dict | None:
    """Extract an OpenAPI spec from a ReadMe ``.md`` response.

    The ``.md`` endpoint returns markdown containing a JSON code block
    with the full OpenAPI 3.x specification for that endpoint.  When a
    second JSON block follows the spec we treat it as a best-effort
    example request body and attach it to the relevant operation under
    the ``x-example-request`` extension key.
    """
    spec: dict | None = None
    example: Any | None = None

    for m in _JSON_BLOCK_RE.finditer(text):
        try:
            parsed = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if spec is None and isinstance(parsed, dict) and "paths" in parsed:
            spec = parsed
            continue
        # First non-spec JSON block: treat as the example body.
        if spec is not None and example is None:
            example = parsed
            break

    if spec is None:
        return None

    if example is not None:
        _attach_example_to_first_operation(spec, example)
    return spec


def _attach_example_to_first_operation(spec: dict, example: Any) -> None:
    """Attach an example body to the spec's first defined operation.

    ReadMe ``.md`` payloads cover one endpoint at a time, so the spec
    almost always has exactly one operation — but defensively pick the
    first ``(method, path)`` we find.
    """
    paths = spec.get("paths") or {}
    for _path, item in paths.items():
        if not isinstance(item, dict):
            continue
        for method in ("post", "put", "patch", "delete", "get"):
            op = item.get(method)
            if isinstance(op, dict):
                op["x-example-request"] = example
                return


# ----- Caching ----------------------------------------------------------------


def _cache_path(cache_dir: Path, source_name: str, slug: str) -> Path:
    return cache_dir / source_name / f"{slug}.json"


def _is_fresh(path: Path, ttl: int) -> bool:
    if not path.exists():
        return False
    age = time.time() - path.stat().st_mtime
    return age < ttl


def _read_cache(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_cache(path: Path, spec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(spec), encoding="utf-8")


# ----- Fetching ---------------------------------------------------------------


class FetchError(Exception):
    """Wraps the last error of a failed fetch with classification info."""

    def __init__(self, message: str, *, status: int | None, kind: str) -> None:
        super().__init__(message)
        self.status = status
        self.kind = kind  # one of: "http", "network", "timeout"


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    """Parse ``Retry-After`` header; returns seconds or None."""
    val = resp.headers.get("Retry-After")
    if not val:
        return None
    try:
        return max(0.0, float(val))
    except ValueError:
        return None


def _backoff_delay(attempt: int, retry_after: float | None) -> float:
    """Exponential backoff with jitter, honouring ``Retry-After``."""
    if retry_after is not None:
        return min(retry_after, _BACKOFF_CAP)
    base = min(_BACKOFF_BASE * (2 ** attempt), _BACKOFF_CAP)
    return base * (0.5 + random.random() / 2)


def _fetch_page(url: str) -> str:
    """Fetch a page, return text. Raises :class:`FetchError` on failure."""
    last_status: int | None = None
    last_kind = "network"
    last_msg = ""
    for attempt in range(_MAX_RETRIES):
        _PACER.wait(url)
        try:
            resp = httpx.get(
                url,
                timeout=_HTTP_TIMEOUT,
                follow_redirects=True,
                headers=_DEFAULT_HEADERS,
            )
        except httpx.TimeoutException as e:
            last_kind = "timeout"
            last_msg = str(e) or "timeout"
            time.sleep(_backoff_delay(attempt, None))
            continue
        except httpx.HTTPError as e:
            last_kind = "network"
            last_msg = str(e) or e.__class__.__name__
            time.sleep(_backoff_delay(attempt, None))
            continue

        # Retryable HTTP statuses: 429 and 5xx.
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            last_status = resp.status_code
            last_kind = "http"
            last_msg = f"HTTP {resp.status_code}"
            delay = _backoff_delay(attempt, _retry_after_seconds(resp))
            time.sleep(delay)
            continue

        if resp.is_success:
            return resp.text

        # Non-retryable HTTP status (e.g. 4xx other than 429).
        raise FetchError(
            f"HTTP {resp.status_code} for {url}",
            status=resp.status_code,
            kind="http",
        )

    raise FetchError(
        f"giving up after {_MAX_RETRIES} attempts ({last_msg}) for {url}",
        status=last_status,
        kind=last_kind,
    )


def _fetch_spec_for_slug(
    source_name: str,
    reference_path: str,
    slug: str,
    cache_dir: Path,
    ttl: int,
) -> tuple[str, dict | None, bool, str | None]:
    """Fetch one endpoint's OAS spec via the ``.md`` protocol.

    Returns ``(slug, spec_or_None, was_cached, failure_reason_or_None)``.
    """
    cp = _cache_path(cache_dir, source_name, slug)

    if _is_fresh(cp, ttl):
        spec = _read_cache(cp)
        if spec:
            return slug, spec, True, None

    url = f"{DOCS_HOST}{reference_path}/{slug}.md"
    try:
        text = _fetch_page(url)
    except FetchError as e:
        logger.warning(
            "oas_fetch_failed",
            source=source_name,
            slug=slug,
            status=e.status,
            kind=e.kind,
            error=str(e),
        )
        spec = _read_cache(cp)
        if spec:
            logger.info("oas_using_stale_cache", source=source_name, slug=slug)
            return slug, spec, True, None
        reason = f"http_{e.status}" if e.status else e.kind
        return slug, None, False, reason

    spec = _extract_oas_from_markdown(text)
    if spec:
        _write_cache(cp, spec)
        return slug, spec, False, None

    logger.warning("oas_no_definition", source=source_name, slug=slug)
    return slug, None, False, "no_oas_block"


# ----- Public API -------------------------------------------------------------


@dataclass
class SourceReport:
    """Per-source counters returned by :func:`discover_and_sync`."""

    name: str
    discovered: int = 0
    fetched_specs: int = 0
    cached_specs: int = 0
    missing_specs: int = 0
    discovery_error: str | None = None
    failure_reasons: Counter = field(default_factory=Counter)

    @property
    def total_specs(self) -> int:
        return self.fetched_specs + self.cached_specs

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "discovered": self.discovered,
            "fetched_specs": self.fetched_specs,
            "cached_specs": self.cached_specs,
            "missing_specs": self.missing_specs,
            "discovery_error": self.discovery_error,
            "failure_reasons": dict(self.failure_reasons),
        }


def discover_and_sync(
    sources: list[SpecSource] | None = None,
    cache_dir: Path = Path("/data/oas_cache"),
    ttl: int = 86400,
) -> tuple[list[dict], list[SourceReport]]:
    """Sync OpenAPI specs from all configured sources.

    For each source:
        1. Fetch the root reference page to discover endpoint slugs.
        2. For each slug, fetch ``{slug}.md`` to get the per-endpoint OAS spec.
        3. Cache specs to disk; reuse fresh caches (``ttl`` seconds).

    Returns ``(all_specs, reports)`` where ``reports`` contains per-source
    counters useful for downstream health checks.
    """
    if sources is None:
        sources = [SpecSource(s.name, s.reference_path) for s in DEFAULT_SOURCES]

    all_specs: list[dict] = []
    reports: list[SourceReport] = []

    for source in sources:
        report = SourceReport(name=source.name)
        reports.append(report)
        logger.info("oas_source_start", source=source.name, path=source.reference_path)

        # Step 1: discover endpoint slugs from the sidebar HTML
        if not source.endpoint_slugs:
            try:
                root_url = f"{DOCS_HOST}{source.reference_path}"
                root_html = _fetch_page(root_url)
                source.endpoint_slugs = _extract_endpoint_slugs(root_html)
                logger.info(
                    "oas_slugs_discovered",
                    source=source.name,
                    count=len(source.endpoint_slugs),
                )
            except FetchError as e:
                report.discovery_error = str(e)
                logger.error(
                    "oas_discovery_failed",
                    source=source.name,
                    status=e.status,
                    kind=e.kind,
                    error=str(e),
                )
                continue

        report.discovered = len(source.endpoint_slugs)

        # Step 2: fetch specs for each endpoint in parallel
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
            futures = {
                pool.submit(
                    _fetch_spec_for_slug,
                    source.name,
                    source.reference_path,
                    slug,
                    cache_dir,
                    ttl,
                ): slug
                for slug in source.endpoint_slugs
            }

            for future in as_completed(futures):
                slug, spec, was_cached, reason = future.result()
                if spec:
                    all_specs.append(spec)
                    if was_cached:
                        report.cached_specs += 1
                    else:
                        report.fetched_specs += 1
                else:
                    report.missing_specs += 1
                    if reason:
                        report.failure_reasons[reason] += 1

        logger.info(
            "oas_source_done",
            source=source.name,
            specs=report.total_specs,
            cached=report.cached_specs,
            fetched=report.fetched_specs,
            missing=report.missing_specs,
            failures=dict(report.failure_reasons),
        )

    logger.info(
        "oas_sync_complete",
        total_specs=len(all_specs),
        sources=[r.as_dict() for r in reports],
    )
    return all_specs, reports


# Backwards-compatible alias kept so callers/tests using the old name keep
# working.  Returns specs only; use :func:`discover_and_sync` if you also need
# the per-source reports.
def discover_and_scrape(
    sources: list[SpecSource] | None = None,
    cache_dir: Path = Path("/data/oas_cache"),
    ttl: int = 86400,
) -> list[dict]:
    specs, _reports = discover_and_sync(sources=sources, cache_dir=cache_dir, ttl=ttl)
    return specs


# ----- SpecProvider wrapper ---------------------------------------------------


class ReadMeSpecProvider:
    """SpecProvider that pulls OpenAPI specs from ReadMe.io-hosted docs.

    Wraps :func:`discover_and_sync` to conform to the
    :class:`~netops_api_navigator.spec_provider.SpecProvider` protocol.

    After :meth:`fetch_specs` runs, ``last_reports`` holds the per-source
    counters from the most recent invocation.
    """

    def __init__(self, sources: list[SpecSource] | None = None) -> None:
        self._sources = sources
        self.last_reports: list[SourceReport] = []

    @property
    def name(self) -> str:
        return "Central"

    def fetch_specs(self, cache_dir: Path, ttl: int) -> list[dict]:
        specs, reports = discover_and_sync(
            sources=self._sources, cache_dir=cache_dir, ttl=ttl
        )
        self.last_reports = reports
        return specs
