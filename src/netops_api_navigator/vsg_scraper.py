"""Sync documentation pages from the HPE Aruba Networking VSG site.

Fetches the Central section of the Validated Solution Guide and splits
each page into H2-level sections stored as ``DocSection`` graph nodes.

The site is a Jekyll 4.x static site with clean HTML — content lives
inside ``div#main-content``.  No JavaScript rendering required.

Resilience:
    * A realistic browser ``User-Agent`` is sent on every request.
    * Transient HTTP errors (429, 5xx) are retried with exponential backoff.
    * If the upstream WAF blocks the runner (HTTP 403), the provider
      degrades gracefully: a single ``vsg_access_denied`` warning is logged
      instead of one per page, and stale cache (if any) is reused.
"""

from __future__ import annotations

import random
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

import httpx
import structlog

logger = structlog.get_logger("vsg_scraper")

VSG_HOST = "https://arubanetworking.hpe.com"

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_DEFAULT_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_HTTP_TIMEOUT = 30.0
_MAX_WORKERS = 3
_MAX_RETRIES = 3
_BACKOFF_BASE = 1.0
_BACKOFF_CAP = 15.0

# ── Pages to scrape ──────────────────────────────────────────────────

# (slug used for section_id generation, full URL path)
VSG_CENTRAL_PAGES: list[tuple[str, str]] = [
    ("overview", "/techdocs/VSG/docs/002-central/central-000/"),
    ("readiness", "/techdocs/VSG/docs/002-central/central-010-readiness/"),
    ("config-model", "/techdocs/VSG/docs/002-central/central-020-config-model/"),
    ("config-example", "/techdocs/VSG/docs/002-central/central-030-central-config-example/"),
    ("config-api", "/techdocs/VSG/docs/002-central/central-020-configuration-api/"),
    ("policy-config", "/techdocs/VSG/docs/002-central/central-040-policy-configuration/"),
    ("on-premises", "/techdocs/VSG/docs/002-central/central-090-central-on-premises/"),
]


# ── Data model ───────────────────────────────────────────────────────


@dataclass
class DocEntry:
    """A single documentation section ready for graph insertion."""

    section_id: str
    title: str
    content: str
    source: str
    url: str


# ── HTML parsing ─────────────────────────────────────────────────────


class _MainContentExtractor(HTMLParser):
    """Extract the inner HTML of ``div#main-content``."""

    def __init__(self) -> None:
        super().__init__()
        self._inside = False
        self._depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict = dict(attrs)
        if tag == "div" and attr_dict.get("id") == "main-content":
            self._inside = True
            self._depth = 1
            return
        if self._inside:
            if tag in _VOID_ELEMENTS:
                pass
            else:
                self._depth += 1
            self._parts.append(self._rebuild_tag(tag, attrs))

    def handle_endtag(self, tag: str) -> None:
        if self._inside:
            if tag not in _VOID_ELEMENTS:
                self._depth -= 1
            if self._depth <= 0:
                self._inside = False
                return
            self._parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._inside:
            self._parts.append(data)

    @staticmethod
    def _rebuild_tag(tag: str, attrs: list[tuple[str, str | None]]) -> str:
        parts = [tag]
        for k, v in attrs:
            if v is None:
                parts.append(k)
            else:
                parts.append(f'{k}="{v}"')
        return "<" + " ".join(parts) + ">"

    def get_html(self) -> str:
        return "".join(self._parts)


_VOID_ELEMENTS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img",
    "input", "link", "meta", "param", "source", "track", "wbr",
})


class _TextExtractor(HTMLParser):
    """Strip HTML tags and return clean text content."""

    _SKIP_TAGS = frozenset({"script", "style", "details", "noscript"})

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif self._skip_depth == 0:
            # Add line break for block-level elements
            if tag in ("p", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "div"):
                self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif self._skip_depth == 0 and tag in ("p", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"):
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def get_text(self) -> str:
        text = "".join(self._parts)
        # Collapse multiple blank lines
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def _html_to_text(html: str) -> str:
    """Convert an HTML fragment to clean text."""
    parser = _TextExtractor()
    parser.feed(html)
    return parser.get_text()


def _extract_main_content(full_html: str) -> str:
    """Extract the inner HTML of div#main-content."""
    parser = _MainContentExtractor()
    parser.feed(full_html)
    return parser.get_html()


# ── Section splitting ────────────────────────────────────────────────

# Matches <h1 ...>...</h1> or <h2 ...>...</h2> headings
_HEADING_RE = re.compile(
    r"<(h[12])\b([^>]*)>(.*?)</\1>",
    re.IGNORECASE | re.DOTALL,
)
_ID_RE = re.compile(r'id=["\']([^"\']*)["\']', re.IGNORECASE)


def _extract_heading_text(inner_html: str) -> str:
    """Get plain text from heading inner HTML (may contain <a> tags)."""
    return re.sub(r"<[^>]+>", "", inner_html).strip()


def extract_sections(html: str, page_url: str, page_slug: str) -> list[DocEntry]:
    """Split a page's main content into sections by H1/H2 headings.

    Returns a list of :class:`DocEntry` objects — one per section.
    """
    main_html = _extract_main_content(html)
    if not main_html.strip():
        return []

    # Find all H1/H2 heading positions
    headings = list(_HEADING_RE.finditer(main_html))

    if not headings:
        # No headings — treat the entire content as one section
        text = _html_to_text(main_html)
        if not text.strip():
            return []
        return [DocEntry(
            section_id=f"vsg-central-{page_slug}",
            title=page_slug.replace("-", " ").title(),
            content=text,
            source="vsg-central",
            url=page_url,
        )]

    entries: list[DocEntry] = []

    # Content before the first heading (page intro)
    first_pos = headings[0].start()
    if first_pos > 0:
        intro_text = _html_to_text(main_html[:first_pos])
        if intro_text.strip():
            entries.append(DocEntry(
                section_id=f"vsg-central-{page_slug}-intro",
                title=_extract_heading_text(headings[0].group(3)) + " — Introduction",
                content=intro_text,
                source="vsg-central",
                url=page_url,
            ))

    # Each heading starts a section that runs to the next heading
    for i, match in enumerate(headings):
        heading_tag = match.group(1).lower()
        attrs_str = match.group(2) or ""
        id_match = _ID_RE.search(attrs_str)
        heading_id = id_match.group(1) if id_match else ""
        heading_text = _extract_heading_text(match.group(3))

        if not heading_text:
            continue

        # Section content runs from after this heading to the next heading
        section_start = match.end()
        section_end = headings[i + 1].start() if i + 1 < len(headings) else len(main_html)
        section_html = main_html[section_start:section_end]
        section_text = _html_to_text(section_html)

        if not section_text.strip():
            continue

        # Build section_id from heading id or slug
        if heading_id:
            sid = f"vsg-central-{page_slug}-{heading_id}"
        else:
            slug_heading = re.sub(r"[^a-z0-9]+", "-", heading_text.lower()).strip("-")
            sid = f"vsg-central-{page_slug}-{slug_heading}"

        fragment = f"#{heading_id}" if heading_id else ""
        entries.append(DocEntry(
            section_id=sid,
            title=heading_text,
            content=section_text,
            source="vsg-central",
            url=f"{page_url}{fragment}",
        ))

    return entries


# ── Caching ──────────────────────────────────────────────────────────


def _cache_path(cache_dir: Path, slug: str) -> Path:
    return cache_dir / f"{slug}.html"


def _is_fresh(path: Path, ttl: int) -> bool:
    if not path.exists():
        return False
    return (time.time() - path.stat().st_mtime) < ttl


def _read_cached_html(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return None


def _write_cached_html(path: Path, html: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


# ── Fetching ─────────────────────────────────────────────────────────


class VsgFetchError(Exception):
    def __init__(self, message: str, *, status: int | None, kind: str) -> None:
        super().__init__(message)
        self.status = status
        self.kind = kind


def _backoff_delay(attempt: int) -> float:
    base = min(_BACKOFF_BASE * (2 ** attempt), _BACKOFF_CAP)
    return base * (0.5 + random.random() / 2)


def _fetch_page(url: str) -> str:
    """Fetch a page and return its HTML, retrying transient failures."""
    last_status: int | None = None
    last_kind = "network"
    last_msg = ""
    for attempt in range(_MAX_RETRIES):
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
            time.sleep(_backoff_delay(attempt))
            continue
        except httpx.HTTPError as e:
            last_kind = "network"
            last_msg = str(e) or e.__class__.__name__
            time.sleep(_backoff_delay(attempt))
            continue

        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            last_status = resp.status_code
            last_kind = "http"
            last_msg = f"HTTP {resp.status_code}"
            time.sleep(_backoff_delay(attempt))
            continue

        if resp.is_success:
            return resp.text

        raise VsgFetchError(
            f"HTTP {resp.status_code} for {url}",
            status=resp.status_code,
            kind="http",
        )

    raise VsgFetchError(
        f"giving up after {_MAX_RETRIES} attempts ({last_msg}) for {url}",
        status=last_status,
        kind=last_kind,
    )


def _fetch_page_cached(
    slug: str, url_path: str, cache_dir: Path, ttl: int
) -> tuple[str, str | None, bool, str | None]:
    """Fetch one VSG page with cache.

    Returns ``(slug, html_or_None, was_cached, failure_reason_or_None)``.
    ``failure_reason`` is a stable token like ``http_403`` so callers can
    aggregate identical upstream blocks instead of spamming per-page logs.
    """
    cp = _cache_path(cache_dir, slug)

    if _is_fresh(cp, ttl):
        html = _read_cached_html(cp)
        if html:
            return slug, html, True, None

    full_url = f"{VSG_HOST}{url_path}"
    try:
        html = _fetch_page(full_url)
        _write_cached_html(cp, html)
        return slug, html, False, None
    except Exception as e:
        status = getattr(e, "status", None)
        kind = getattr(e, "kind", "network")
        # Suppress the per-page warning for the most common upstream-blocked
        # case (403); we summarise it once at sync_complete instead.
        if status != 403:
            logger.warning(
                "vsg_fetch_failed",
                slug=slug,
                status=status,
                kind=kind,
                error=str(e),
            )
        html = _read_cached_html(cp)
        if html:
            logger.info("vsg_using_stale_cache", slug=slug)
            return slug, html, True, None
        reason = f"http_{status}" if status else kind
        return slug, None, False, reason


# ── Public API ───────────────────────────────────────────────────────


@dataclass
class VsgReport:
    """Counters returned by :func:`sync_vsg_docs`."""

    pages_total: int = 0
    pages_ok: int = 0
    pages_cached: int = 0
    pages_fetched: int = 0
    pages_failed: int = 0
    sections: int = 0
    failure_reasons: Counter = field(default_factory=Counter)

    @property
    def access_denied(self) -> bool:
        """True iff every failure was an HTTP 403 (upstream WAF block)."""
        if self.pages_failed == 0:
            return False
        return all(
            reason == "http_403" and count == self.pages_failed
            for reason, count in self.failure_reasons.items()
        )

    def as_dict(self) -> dict:
        return {
            "pages_total": self.pages_total,
            "pages_ok": self.pages_ok,
            "pages_cached": self.pages_cached,
            "pages_fetched": self.pages_fetched,
            "pages_failed": self.pages_failed,
            "sections": self.sections,
            "failure_reasons": dict(self.failure_reasons),
        }


def sync_vsg_docs(
    pages: list[tuple[str, str]] | None = None,
    cache_dir: Path = Path("/data/vsg_cache"),
    ttl: int = 86400,
) -> tuple[list[DocEntry], VsgReport]:
    """Sync VSG Central documentation pages and split into sections.

    Args:
        pages: List of ``(slug, url_path)`` tuples. Defaults to
            :data:`VSG_CENTRAL_PAGES`.
        cache_dir: Cache directory for raw HTML pages.
        ttl: Cache freshness in seconds.

    Returns:
        ``(entries, report)`` — entries ready for graph insertion plus a
        report with per-page counters and aggregated failure reasons.
    """
    if pages is None:
        pages = list(VSG_CENTRAL_PAGES)

    all_entries: list[DocEntry] = []
    report = VsgReport(pages_total=len(pages))

    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {
            pool.submit(_fetch_page_cached, slug, url_path, cache_dir, ttl): (slug, url_path)
            for slug, url_path in pages
        }

        for future in as_completed(futures):
            slug, url_path = futures[future]
            slug_result, html, was_cached, reason = future.result()

            if html:
                report.pages_ok += 1
                if was_cached:
                    report.pages_cached += 1
                else:
                    report.pages_fetched += 1
                page_url = f"{VSG_HOST}{url_path}"
                sections = extract_sections(html, page_url, slug)
                all_entries.extend(sections)
                logger.info("vsg_page_done", slug=slug, sections=len(sections))
            else:
                report.pages_failed += 1
                if reason:
                    report.failure_reasons[reason] += 1

    report.sections = len(all_entries)

    if report.access_denied:
        # Upstream is blocking us at the edge — surface this once with a
        # clear, actionable message instead of N per-page warnings.
        logger.warning(
            "vsg_access_denied",
            host=VSG_HOST,
            pages_blocked=report.pages_failed,
            hint=(
                "VSG host returned HTTP 403 for every page. Likely the "
                "upstream WAF (Akamai) is blocking the runner's egress IP. "
                "VSG sections will be empty in this build."
            ),
        )

    logger.info(
        "vsg_sync_complete",
        total_sections=report.sections,
        pages_ok=report.pages_ok,
        pages_cached=report.pages_cached,
        pages_fetched=report.pages_fetched,
        pages_failed=report.pages_failed,
        failures=dict(report.failure_reasons),
    )
    return all_entries, report


# Backwards-compatible alias kept so callers/tests using the old name keep
# working.  Returns entries only.
def scrape_vsg_docs(
    pages: list[tuple[str, str]] | None = None,
    cache_dir: Path = Path("/data/vsg_cache"),
    ttl: int = 86400,
) -> list[DocEntry]:
    entries, _report = sync_vsg_docs(pages=pages, cache_dir=cache_dir, ttl=ttl)
    return entries


class VsgDocProvider:
    """Provider that pulls VSG Central documentation pages.

    After :meth:`fetch_docs` runs, ``last_report`` exposes the per-run
    counters from the most recent invocation.
    """

    def __init__(self) -> None:
        self.last_report: VsgReport | None = None

    @property
    def name(self) -> str:
        return "VSG Central"

    def fetch_docs(
        self, cache_dir: Path, ttl: int = 86400
    ) -> list[DocEntry]:
        entries, report = sync_vsg_docs(cache_dir=cache_dir, ttl=ttl)
        self.last_report = report
        return entries
