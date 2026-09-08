"""Direct API call tools — authenticated access to Central and GreenLake APIs."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, Literal

import structlog
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ..central_client import BaseAPIClient, CentralAPIError, CentralClient, GreenLakeClient
from ..config import Settings
from .api_call_validation import (
    format_validation_error,
    format_validation_warnings,
    validate_call,
)

if TYPE_CHECKING:
    from ..graph.manager import GraphManager

logger = structlog.get_logger("tools.api_call")

_WRITE_METHODS = {"POST", "PATCH", "PUT", "DELETE"}
_BATCH_CAP = 25


# ── 404 path suggestions from the API schema graph ──────────────────

_PATH_SUGGEST_CYPHER = """
MATCH (e:ApiEndpoint)
WHERE e.path CONTAINS $seg
RETURN e.method AS method, e.path AS path
ORDER BY size(e.path) ASC
LIMIT 5
"""

# True version segments like ``v1``, ``v2beta1`` — but NOT real path
# segments that merely start with 'v' (vlan, vpn, vrf, virtual-networks).
_VERSION_SEG_RE = re.compile(r"^v\d+([a-z]+\d*)?$", re.IGNORECASE)


def _suggest_paths_for_404(
    graph_manager: "GraphManager | None", path: str
) -> list[dict]:
    """Look up similar endpoint paths via the ApiEndpoint subgraph.

    Returns up to 5 candidates whose path contains the longest informative
    segment of the failed path (skipping version segments like ``v1``).
    Fails silent — when the graph is unavailable we return an empty list so
    the caller still produces a useful error message.
    """
    if graph_manager is None or not getattr(graph_manager, "is_available", False):
        return []

    parts = [
        p for p in path.strip("/").split("/")
        if p and not _VERSION_SEG_RE.match(p)
    ]
    parts.sort(key=len, reverse=True)
    seen: set[tuple[str, str]] = set()
    suggestions: list[dict] = []
    for seg in parts[:3]:
        if len(seg) < 3:
            continue
        try:
            rows = graph_manager.query(
                _PATH_SUGGEST_CYPHER, params={"seg": seg}, read_only=True
            )
        except Exception:
            continue
        for row in rows:
            key = (row.get("method", ""), row.get("path", ""))
            if not key[1] or key in seen:
                continue
            seen.add(key)
            suggestions.append({"method": key[0], "path": key[1]})
            if len(suggestions) >= 5:
                return suggestions
    return suggestions


def _api_error_hint(
    exc: CentralAPIError,
    path: str,
    method: str,
    graph_manager: "GraphManager | None" = None,
) -> str:
    """Build a contextual hint for common API errors."""
    msg = (exc.message or "").lower()

    if "restricted" in msg or "scope" in msg:
        return (
            "\n\nHint: Central config APIs require a valid scopeId and scopeType. "
            "Use `query_graph` to find scopeId values from Site, SiteCollection, "
            "or Device nodes. For resolved/effective config including provenance, "
            "add `?effective=true&detailed=true` to the URL. See `graph://schema` "
            "for canned Cypher patterns covering required parameters."
        )

    if exc.status_code == 404:
        suggestions = _suggest_paths_for_404(graph_manager, path)
        if suggestions:
            lines = "\n".join(
                f"  - {s['method']} /{s['path'].lstrip('/')}" for s in suggestions
            )
            return (
                "\n\nHint: Endpoint not found. Did you mean one of these "
                "(matched from the ApiEndpoint subgraph)?\n"
                f"{lines}\n"
                "Read `api://endpoint-catalog` for the full list."
            )
        return (
            "\n\nHint: Endpoint not found. Read the `api://endpoint-catalog` "
            "resource for the full list of valid METHOD /path combinations. "
            "Guessing paths without consulting the catalog has a near-zero "
            "chance of success."
        )

    if exc.status_code == 400:
        return (
            "\n\nHint: Inspect required parameters and body fields with "
            "`query_graph` — see `graph://schema` for canned patterns "
            "(`Required parameters for a specific endpoint`, `Required "
            "top-level fields of an endpoint's request body`)."
        )

    return ""


# ── Per-call dispatch (envelope-returning, never raises) ────────────


def _validate_path(path: str) -> str | None:
    clean = path.strip().lstrip("/")
    if not clean:
        return None
    if ".." in clean:
        return None
    return clean


def _build_request_envelope(
    path: str, method: str, query_params: dict | None, body: dict | None, batch: bool
) -> dict:
    env: dict[str, Any] = {"method": method.upper(), "path": path}
    if query_params:
        env["query_params"] = query_params
    if body is not None and method.upper() != "GET":
        env["body"] = body
    if batch:
        env["batch_item"] = True
    return env


def _dispatch_one(
    client: BaseAPIClient,
    platform: str,
    graph_manager: "GraphManager | None",
    path: str,
    method: str,
    query_params: dict | None,
    body: dict | None,
) -> dict:
    """Validate + dispatch a single call. Always returns a dict envelope."""
    request_env = _build_request_envelope(path, method, query_params, body, batch=True)
    method_u = method.upper()

    clean_path = _validate_path(path)
    if clean_path is None:
        return {
            "ok": False,
            "request": request_env,
            "status": None,
            "errors": ["Path cannot be empty or contain '..'."],
        }
    if body and method_u == "GET":
        return {
            "ok": False,
            "request": request_env,
            "status": None,
            "errors": ["Cannot send a request body with GET. Use POST, PATCH, or PUT."],
        }

    result = validate_call(graph_manager, method_u, path, query_params, body)
    if not result.ok:
        return {
            "ok": False,
            "request": request_env,
            "status": None,
            "errors": result.errors,
            "warnings": result.warnings,
            "schema_summary": result.schema_summary,
        }

    try:
        logger.info("api_call_start", platform=platform, method=method_u, path=clean_path, params=query_params)
        response = client._request(method_u, clean_path, params=query_params, json_body=body)
        logger.info("api_call_done", platform=platform, method=method_u, path=clean_path)
        return {
            "ok": True,
            "request": request_env,
            "status": 200,
            "response": response,
            "warnings": result.warnings,
        }
    except CentralAPIError as e:
        logger.error(
            "api_call_failed",
            platform=platform,
            method=method_u,
            path=clean_path,
            status=e.status_code,
            error_code=e.error_code,
        )
        hint = _api_error_hint(e, clean_path, method_u, graph_manager)
        err_msg = f"[{e.status_code}] {e.message}{hint}"
        if e.debug_id:
            err_msg += f" (debugId: {e.debug_id})"
        return {
            "ok": False,
            "request": request_env,
            "status": e.status_code,
            "errors": [err_msg],
            "warnings": result.warnings,
        }
    except Exception as e:  # pragma: no cover - defensive
        logger.error("api_call_failed", platform=platform, method=method_u, path=clean_path, error=str(e))
        return {
            "ok": False,
            "request": request_env,
            "status": None,
            "errors": [f"API call failed: {e}"],
            "warnings": result.warnings,
        }


def _make_api_call(
    client: BaseAPIClient,
    platform: str,
    path: str,
    method: str,
    query_params: dict[str, str] | None,
    body: dict | None,
    warning_header: str = "",
    graph_manager: "GraphManager | None" = None,
) -> str:
    """Shared implementation for single authenticated API calls.

    Returns a JSON-encoded envelope ``{request, status, response}`` on
    success, prefixed (as plain text) with ``warning_header`` when the
    pre-flight validator produced warnings. On API failure raises
    ``ToolError`` with the same hint pipeline used by batch mode.
    """
    clean_path = _validate_path(path)
    method_u = method.upper()
    if clean_path is None:
        raise ToolError("Path cannot be empty or contain '..'.")
    if body and method_u == "GET":
        raise ToolError("Cannot send a request body with GET. Use POST, PATCH, or PUT.")

    try:
        logger.info("api_call_start", platform=platform, method=method_u, path=clean_path, params=query_params)
        response = client._request(method_u, clean_path, params=query_params, json_body=body)
        logger.info("api_call_done", platform=platform, method=method_u, path=clean_path)
        envelope = {
            "request": _build_request_envelope(path, method_u, query_params, body, batch=False),
            "status": 200,
            "response": response,
        }
        return warning_header + json.dumps(envelope, indent=2, default=str)
    except CentralAPIError as e:
        logger.error(
            "api_call_failed",
            platform=platform,
            method=method_u,
            path=clean_path,
            status=e.status_code,
            error_code=e.error_code,
        )
        hint = _api_error_hint(e, clean_path, method_u, graph_manager)
        raise ToolError(
            f"API call failed [{e.status_code}]: {e.message}{hint}"
            + (f" (debugId: {e.debug_id})" if e.debug_id else "")
        )
    except Exception as e:
        logger.error("api_call_failed", platform=platform, method=method_u, path=clean_path, error=str(e))
        raise ToolError(f"API call failed: {e}")


# ── Batch helpers ───────────────────────────────────────────────────


def _normalize_batch_calls(calls: list[dict]) -> tuple[list[dict] | None, str | None]:
    """Validate the shape of the batch ``calls`` list. Returns (normalized, error)."""
    if not isinstance(calls, list):
        return None, "`calls` must be a list of call specs."
    if not calls:
        return None, "`calls` must contain at least one item."
    if len(calls) > _BATCH_CAP:
        return None, (
            f"`calls` contains {len(calls)} items; batch cap is {_BATCH_CAP}. "
            "Split into multiple invocations or aggregate via a script."
        )
    normalized: list[dict] = []
    for i, item in enumerate(calls):
        if not isinstance(item, dict):
            return None, f"calls[{i}] must be an object."
        if "path" not in item or not isinstance(item["path"], str) or not item["path"].strip():
            return None, f"calls[{i}] is missing a non-empty `path`."
        method = (item.get("method") or "GET").upper()
        if method not in {"GET", "POST", "PATCH", "PUT", "DELETE"}:
            return None, f"calls[{i}].method must be one of GET/POST/PATCH/PUT/DELETE."
        qp = item.get("query_params")
        if qp is not None and not isinstance(qp, dict):
            return None, f"calls[{i}].query_params must be an object or null."
        body = item.get("body")
        if body is not None and not isinstance(body, dict):
            return None, f"calls[{i}].body must be an object or null."
        normalized.append({"path": item["path"], "method": method, "query_params": qp, "body": body})
    return normalized, None


def _run_batch(
    client: BaseAPIClient,
    platform: str,
    graph_manager: "GraphManager | None",
    calls: list[dict],
    read_only: bool,
) -> str:
    results: list[dict] = []
    ok_count = 0
    fail_count = 0
    for item in calls:
        method_u = item["method"]
        if read_only and method_u in _WRITE_METHODS:
            results.append({
                "ok": False,
                "request": _build_request_envelope(
                    item["path"], method_u, item["query_params"], item["body"], batch=True
                ),
                "status": None,
                "errors": [
                    f"Server is in READ_ONLY mode; {method_u} requests are not permitted."
                ],
            })
            fail_count += 1
            continue
        res = _dispatch_one(
            client,
            platform,
            graph_manager,
            item["path"],
            method_u,
            item["query_params"],
            item["body"],
        )
        if res.get("ok"):
            ok_count += 1
        else:
            fail_count += 1
        results.append(res)

    envelope = {
        "batch": True,
        "total": len(results),
        "ok": ok_count,
        "failed": fail_count,
        "results": results,
    }
    return json.dumps(envelope, indent=2, default=str)


def register_api_call_tools(
    mcp,
    settings: Settings,
    client: CentralClient,
    graph_manager: "GraphManager | None" = None,
):
    """Register the Central direct API call tool with the MCP server."""

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    def call_central_api(
        path: str = "",
        method: Literal["GET", "POST", "PATCH", "PUT", "DELETE"] = "GET",
        query_params: dict[str, str] | None = None,
        body: dict | None = None,
        calls: list[dict] | None = None,
    ) -> str:
        """Make one or more authenticated requests to Central API endpoints.

        For single-item lookups and one-off mutations ONLY. To fetch all items
        from a collection (list devices, list sites, etc.), write a script
        using ``api.paginate()`` — never pass ``limit`` to this tool.

        **Before calling**, consult the ``api://endpoint-catalog`` resource
        (or the catalog in the system instructions) for the correct
        ``METHOD /path``, then use ``query_graph`` against the schema
        subgraph (``Parameter``, ``RequestBody``, ``SchemaComponent``,
        ``Property``) for parameter/body shape. See ``graph://schema``.

        For resolved/effective config with provenance, add
        ``effective=true&detailed=true`` to query_params on
        ``network-config/`` endpoints.

        A pre-flight validator runs against the graph. Missing required
        query params or required body fields (POST only) reject the call
        with a structured error containing a schema summary. Unknown body
        keys show as warnings on a successful response. Fails open if the
        graph is unavailable.

        **Batch mode**: pass ``calls=[{"path", "method", "query_params",
        "body"}, ...]`` (cap 25) to run several independent requests
        sequentially, continue-on-error. Each item gets its own envelope in
        ``results``. When ``calls`` is set, top-level args are ignored.

        Args:
            path: API path for single-call form (e.g.
                  ``network-monitoring/v1/device-inventory``). No base URL.
            method: HTTP method for single-call form. Default GET.
            query_params: Optional query params for single-call form.
            body: Optional JSON request body for POST/PATCH/PUT.
            calls: Optional list of per-call dicts for batch mode (cap 25).

        Returns:
            JSON envelope. Single: ``{request, status, response}`` (maybe
            with a validator-warning prefix). Batch: ``{batch, total, ok,
            failed, results}``.
        """
        if not settings.has_credentials:
            raise ToolError(
                "Central credentials not configured. "
                "Set CENTRAL_BASE_URL, CENTRAL_CLIENT_ID, CENTRAL_CLIENT_SECRET."
            )

        if calls is not None:
            normalized, err = _normalize_batch_calls(calls)
            if err:
                raise ToolError(err)
            return _run_batch(client, "central", graph_manager, normalized, settings.read_only)

        if not path or not path.strip():
            raise ToolError("Either `path` or `calls` must be provided.")

        if settings.read_only and method.upper() in _WRITE_METHODS:
            raise ToolError(
                f"Server is in READ_ONLY mode; {method.upper()} requests are not permitted. "
                "Network-side configuration changes are disabled."
            )

        result = validate_call(graph_manager, method, path, query_params, body)
        if not result.ok:
            raise ToolError(format_validation_error(result))

        return _make_api_call(
            client,
            "central",
            path,
            method,
            query_params,
            body,
            warning_header=format_validation_warnings(result),
            graph_manager=graph_manager,
        )


def register_workshop_api_call_tool(
    mcp,
    settings: Settings,
    client: CentralClient,
    graph_manager: "GraphManager | None" = None,
):
    """Register a GET-only Central tool with no mutating method in its schema."""

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    def call_central_api(
        path: str = "",
        query_params: dict[str, str] | None = None,
        calls: list[dict] | None = None,
    ) -> str:
        """Make one or more authenticated GET requests to Central API.

        This workshop-safe variant cannot express POST, PUT, PATCH, or DELETE.
        Discover exact paths and parameters with ``query_fts`` and
        ``query_api_schema`` before calling it. Batch entries may contain only
        ``path`` and ``query_params`` and are processed sequentially (cap 25).

        Args:
            path: Central API path for a single GET request; no base URL.
            query_params: Optional string-valued query parameters.
            calls: Optional list of GET calls with path and query_params.

        Returns:
            The same structured response envelope as the full Central tool.
        """
        if not settings.has_credentials:
            raise ToolError(
                "Central credentials not configured. Set CENTRAL_BASE_URL, "
                "CENTRAL_CLIENT_ID, and CENTRAL_CLIENT_SECRET."
            )

        if calls is not None:
            for item in calls:
                method = str(item.get("method", "GET")).upper() if isinstance(item, dict) else ""
                if method != "GET" or (isinstance(item, dict) and item.get("body") is not None):
                    raise ToolError(
                        "Workshop profile permits GET requests only; batch calls "
                        "must not contain a mutating method or request body."
                    )
            normalized, err = _normalize_batch_calls(calls)
            if err:
                raise ToolError(err)
            return _run_batch(client, "central", graph_manager, normalized, True)

        if not path or not path.strip():
            raise ToolError("Either `path` or `calls` must be provided.")
        result = validate_call(graph_manager, "GET", path, query_params, None)
        if not result.ok:
            raise ToolError(format_validation_error(result))
        return _make_api_call(
            client,
            "central",
            path,
            "GET",
            query_params,
            None,
            warning_header=format_validation_warnings(result),
            graph_manager=graph_manager,
        )


def register_greenlake_api_call_tools(
    mcp,
    settings: Settings,
    glp_client: GreenLakeClient | None,
    graph_manager: "GraphManager | None" = None,
):
    """Register the GreenLake direct API call tool with the MCP server."""

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    def call_greenlake_api(
        path: str = "",
        method: Literal["GET", "POST", "PATCH", "PUT", "DELETE"] = "GET",
        query_params: dict[str, str] | None = None,
        body: dict | None = None,
        calls: list[dict] | None = None,
    ) -> str:
        """Make one or more authenticated requests to HPE GreenLake Platform endpoints.

        Use for GreenLake-specific operations: device inventory management,
        subscription/license management, service catalog, locations, etc.
        The base URL is https://global.api.greenlake.hpe.com.

        Same pre-flight validator and 404 path-suggester as ``call_central_api``.

        **Batch mode**: pass ``calls=[{...}, ...]`` (cap 25) for sequential
        continue-on-error dispatch of independent requests.

        Args:
            path: API path for the single-call form
                  (e.g., "devices/v1/devices"). Ignored when ``calls`` is set.
            method: HTTP method for the single-call form. Defaults to GET.
            query_params: Optional query parameters for the single-call form.
            body: Optional JSON request body for POST/PATCH/PUT.
            calls: Optional list of per-call dicts for batch mode (cap 25).

        Returns:
            JSON envelope. Single mode: ``{request, status, response}``
            (optionally prefixed by a validator warning block). Batch mode:
            ``{batch: true, total, ok, failed, results: [...]}``.
        """
        if glp_client is None:
            raise ToolError(
                "GreenLake credentials not configured. "
                "Set GREENLAKE_CLIENT_ID and GREENLAKE_CLIENT_SECRET in your .env file "
                "(or GLP_CLIENT_ID / GLP_CLIENT_SECRET)."
            )

        if calls is not None:
            normalized, err = _normalize_batch_calls(calls)
            if err:
                raise ToolError(err)
            return _run_batch(glp_client, "greenlake", graph_manager, normalized, settings.read_only)

        if not path or not path.strip():
            raise ToolError("Either `path` or `calls` must be provided.")

        if settings.read_only and method.upper() in _WRITE_METHODS:
            raise ToolError(
                f"Server is in READ_ONLY mode; {method.upper()} requests are not permitted. "
                "Network-side configuration changes are disabled."
            )

        result = validate_call(graph_manager, method, path, query_params, body)
        if not result.ok:
            raise ToolError(format_validation_error(result))

        return _make_api_call(
            glp_client,
            "greenlake",
            path,
            method,
            query_params,
            body,
            warning_header=format_validation_warnings(result),
            graph_manager=graph_manager,
        )
