"""Fetch OpenAPI specs from the HPE GreenLake Developer Portal.

Auto-discovers available API services and their OpenAPI spec bundles
using the portal's Gatsby page-data protocol, then downloads native
OpenAPI 3.x JSON specs via the ``_bundle`` download endpoint.

Discovery flow:
    1. Fetch the catalog sidebar JSON to enumerate all services.
    2. For each service, fetch its sidebar JSON to find OpenAPI spec names.
    3. Download each spec via ``_bundle/…/index.json?download``.

Implements the :class:`~netops_api_navigator.spec_provider.SpecProvider`
protocol.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx
import structlog

logger = structlog.get_logger("glp_spec_provider")

_PORTAL = "https://developer.greenlake.hpe.com"
_CATALOG_SIDEBAR_URL = (
    f"{_PORTAL}/page-data/shared/"
    "sidebar-services-integrations.sidebars.yaml.json"
)

_HTTP_TIMEOUT = 30.0
_MAX_WORKERS = 6

# ── Service slug allow-list ──────────────────────────────────────────────────────
# Only these GreenLake service slugs are fetched.  Edit this set to
# control which API specs are included in the search index.
# A value of None means "fetch everything discovered on the portal".

GLP_INCLUDED_SLUGS: set[str] | None = None


# ── Gatsby page-data discovery ───────────────────────────────────────────────────


def _fetch_json(url: str) -> dict:
    resp = httpx.get(url, timeout=_HTTP_TIMEOUT, follow_redirects=True)
    resp.raise_for_status()
    return resp.json()


def _discover_services(
    included_slugs: set[str] | None = None,
) -> list[tuple[str, str]]:
    """Return ``(slug, portal_link)`` pairs from the catalog sidebar.

    Fetches the Gatsby page-data sidebar JSON for the services catalog
    and extracts each service as ``(slug, link)`` where *link* is the
    portal path like ``/docs/greenlake/services/device-management/public``.

    Pass *included_slugs* to filter; ``None`` means fetch everything.
    """
    effective = included_slugs if included_slugs is not None else GLP_INCLUDED_SLUGS
    sidebar = _fetch_json(_CATALOG_SIDEBAR_URL)
    services: list[tuple[str, str]] = []
    for group in sidebar.get("items", []):
        if group.get("label") != "Services":
            continue
        for item in group.get("items", []):
            link = (item.get("link") or "").rstrip("/")
            if not link or "/services/" not in link:
                continue
            if link == "/docs/greenlake/services":
                continue
            slug = link.split("/services/")[1].split("/")[0]
            if effective is not None and slug not in effective:
                continue
            services.append((slug, link))
    return services


def _discover_specs_for_service(slug: str) -> list[tuple[str, str]]:
    """Return ``(spec_name, bundle_path)`` pairs for a service.

    Fetches the service's own sidebar JSON and looks for ``/openapi/{name}/``
    patterns in the navigation links to find the OpenAPI spec bundle names.
    The *bundle_path* is the path segment between ``/services/`` and
    ``/openapi/`` needed to construct the ``_bundle`` download URL.
    """
    encoded = urllib.parse.quote(
        f"sidebar-docs/greenlake/services/{slug}/sidebars.yaml", safe=""
    )
    url = f"{_PORTAL}/page-data/shared/{encoded}.json"
    try:
        sidebar = _fetch_json(url)
    except Exception as exc:
        logger.debug("glp_sidebar_fetch_failed", slug=slug, error=str(exc))
        return []

    text = json.dumps(sidebar)
    hits = re.findall(
        r"/docs/greenlake/services/" + re.escape(slug) + r'(/[^\"]*?)/openapi/([a-z0-9_-]+)/',
        text,
    )
    seen: set[str] = set()
    results: list[tuple[str, str]] = []
    for path_suffix, spec_name in hits:
        if spec_name == "changelog" or spec_name in seen:
            continue
        seen.add(spec_name)
        bundle_path = f"{slug}{path_suffix}"
        results.append((spec_name, bundle_path))
    return results


# ── Caching ──────────────────────────────────────────────────────────────


def _cache_path(cache_dir: Path, slug: str) -> Path:
    return cache_dir / "greenlake" / f"{slug}.json"


def _is_fresh(path: Path, ttl: int) -> bool:
    if not path.exists():
        return False
    return (time.time() - path.stat().st_mtime) < ttl


def _read_cache(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_cache(path: Path, spec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(spec), encoding="utf-8")


# ── Per-spec fetch ─────────────────────────────────────────────────────────


def _try_bundle_download(bundle_path: str, spec_name: str) -> dict | None:
    """Download OAS via ``_bundle/…/index.json?download``."""
    url = (
        f"{_PORTAL}/_bundle/docs/greenlake/services/"
        f"{bundle_path}/openapi/{spec_name}/index.json?download"
    )
    try:
        resp = httpx.get(url, timeout=_HTTP_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def _try_pagedata_download(bundle_path: str, spec_name: str) -> dict | None:
    """Download OAS via Gatsby ``page-data/shared/oas-docs/…`` wrapper."""
    url = (
        f"{_PORTAL}/page-data/shared/oas-docs/greenlake/services/"
        f"{bundle_path}/openapi/{spec_name}/index.yaml.json"
    )
    try:
        resp = httpx.get(url, timeout=_HTTP_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
        wrapper = resp.json()
        return wrapper.get("definition") if isinstance(wrapper, dict) else None
    except Exception:
        return None


def _fetch_spec(
    slug: str,
    spec_name: str,
    bundle_path: str,
    cache_dir: Path,
    ttl: int,
) -> tuple[str, str, dict | None, bool]:
    """Download one OAS spec. Returns ``(slug, spec_name, spec_or_None, was_cached)``.

    Tries the ``_bundle`` download first.  If that fails, falls back to
    the Gatsby ``page-data/shared/oas-docs`` endpoint which wraps the
    spec inside a ``{"definition": …}`` envelope.
    """
    cp = _cache_path(cache_dir, f"{slug}__{spec_name}")

    if _is_fresh(cp, ttl):
        spec = _read_cache(cp)
        if spec:
            return slug, spec_name, spec, True

    spec = _try_bundle_download(bundle_path, spec_name)
    if spec is None:
        spec = _try_pagedata_download(bundle_path, spec_name)

    if spec and isinstance(spec, dict) and ("paths" in spec or "openapi" in spec):
        _write_cache(cp, spec)
        return slug, spec_name, spec, False

    if spec is not None:
        logger.warning("glp_no_paths", slug=slug, spec=spec_name)

    cached = _read_cache(cp)
    if cached:
        logger.info("glp_using_stale_cache", slug=slug, spec=spec_name)
        return slug, spec_name, cached, True
    return slug, spec_name, None, False


# ── Public API ─────────────────────────────────────────────────────────────


def discover_and_fetch(
    cache_dir: Path = Path("/data/oas_cache"),
    ttl: int = 86400,
    included_slugs: set[str] | None = None,
) -> list[dict]:
    """Auto-discover GreenLake APIs and fetch their OpenAPI specs.

    Uses the portal's Gatsby page-data protocol:

    1. Fetches the catalog sidebar JSON to enumerate services.
    2. For each service, fetches its sidebar JSON to find OpenAPI spec names.
    3. Downloads each spec via the ``_bundle/…/index.json?download`` endpoint,
       falling back to the ``page-data/shared/oas-docs`` wrapper.
    4. Returns all parsed specs as a list of dicts.

    Pass *included_slugs* to filter services; ``None`` uses the module default.
    """
    effective = included_slugs if included_slugs is not None else GLP_INCLUDED_SLUGS
    logger.info("glp_discovery_start", included_slugs=effective)

    try:
        services = _discover_services(included_slugs=effective)
        logger.info("glp_services_discovered", count=len(services),
                     slugs=[s for s, _ in services])
    except Exception as exc:
        logger.error("glp_catalog_fetch_failed", error=str(exc))
        return []

    # Discover spec names per service (parallel)
    spec_jobs: list[tuple[str, str, str]] = []
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {
            pool.submit(_discover_specs_for_service, slug): (slug, link)
            for slug, link in services
        }
        for future in as_completed(futures):
            slug, link = futures[future]
            try:
                for spec_name, bundle_path in future.result():
                    spec_jobs.append((slug, spec_name, bundle_path))
            except Exception as exc:
                logger.warning("glp_spec_discovery_failed", slug=slug, error=str(exc))

    logger.info(
        "glp_specs_discovered",
        total=len(spec_jobs),
        details=[(s, n) for s, n, _ in spec_jobs],
    )

    # Fetch specs in parallel
    all_specs: list[dict] = []
    seen_titles: set[str] = set()
    cached_count = 0
    fetched_count = 0

    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {
            pool.submit(_fetch_spec, slug, spec_name, bundle_path, cache_dir, ttl): slug
            for slug, spec_name, bundle_path in spec_jobs
        }
        for future in as_completed(futures):
            slug, spec_name, spec, was_cached = future.result()
            if was_cached:
                cached_count += 1
            else:
                fetched_count += 1
            if spec:
                title = spec.get("info", {}).get("title", f"{slug}/{spec_name}")
                if title not in seen_titles:
                    seen_titles.add(title)
                    all_specs.append(spec)

    logger.info(
        "glp_fetch_complete",
        total_specs=len(all_specs),
        cached=cached_count,
        fetched=fetched_count,
    )
    return all_specs


# ── SpecProvider class ───────────────────────────────────────────────────────


class GreenLakeSpecProvider:
    """SpecProvider that fetches native OpenAPI specs from the GreenLake portal.

    Conforms to the
    :class:`~netops_api_navigator.spec_provider.SpecProvider` protocol.
    """

    def __init__(self, included_slugs: set[str] | None = None) -> None:
        self._included_slugs = included_slugs

    @property
    def name(self) -> str:
        return "GreenLake"

    def fetch_specs(self, cache_dir: Path, ttl: int) -> list[dict]:
        return discover_and_fetch(
            cache_dir=cache_dir, ttl=ttl, included_slugs=self._included_slugs,
        )
