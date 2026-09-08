"""Pre-authenticated API helper for scripts executed by the MCP server.

This module is copied into the script library at server startup so that
scripts can simply ``from central_helpers import api`` and make API calls
without any OAuth2 boilerplate.

Usage inside a script::

    from central_helpers import api

    devices = api.get("network-monitoring/v1alpha1/devices", params={"limit": "100"})
    api.post("network-config/v1alpha1/dhcp-pool", json_body={"name": "pool1", ...})

    # Graph access (for enrichment scripts)
    from central_helpers import graph

    graph.execute("MERGE (n:Device {serial: $s}) SET n.name = $name", {"s": "SN1", "name": "SW1"})
    rows = graph.query("MATCH (d:Device) RETURN d.serial, d.name")
"""

from __future__ import annotations

import json
import os

try:  # Installed package import (tests and direct library use)
    from ._http_core import (  # type: ignore[import-not-found]
        AuthenticationError,
        BaseHTTPClient,
        CentralAPIError,
        NotFoundError,
        PaginationError,
        RateLimitError,
        detect_item_key,
    )
except ImportError:  # Copied next to _http_core.py in the script library
    from _http_core import (  # type: ignore[no-redef]  # noqa: F401
        AuthenticationError,
        BaseHTTPClient,
        CentralAPIError,
        NotFoundError,
        PaginationError,
        RateLimitError,
        detect_item_key,
    )


class CentralAPI(BaseHTTPClient):
    """Pre-authenticated HTTP client for Central API (Level 2 Smart Client).

    Reads credentials from environment variables injected by the MCP server.
    Inherits token acquisition, 401 retry, 429 rate-limit retry, and
    structured error parsing from BaseHTTPClient.
    """

    def __init__(self) -> None:
        super().__init__(
            base_url=os.environ["CENTRAL_BASE_URL"],
            client_id=os.environ["CENTRAL_CLIENT_ID"],
            client_secret=os.environ["CENTRAL_CLIENT_SECRET"],
        )

    def paginate(
        self,
        path: str,
        params: dict | None = None,
        *,
        max_pages: int = 50,
        page_size: int = 100,
        item_key: str | None = None,
    ) -> list[dict]:
        """Fetch all pages of a paginated list endpoint.

        Auto-detects cursor-based (MRT APIs with ``next`` token) vs
        offset-based (Config APIs with ``offset`` integer) pagination
        from the first response.

        Args:
            path: API path (e.g. "network-monitoring/v1alpha1/devices").
            params: Extra query parameters (merged with pagination params).
            max_pages: Safety limit on number of pages fetched (default 50).
            page_size: Items per page (default 100).
            item_key: Response key containing the item array. Auto-detected
                      if not provided (tries "items", then first list-valued key).

        Returns:
            Flat list of all items across all pages.

        Raises:
            PaginationError: On unexpected response structure or max_pages exceeded.
        """
        all_items: list[dict] = []
        merged = dict(params or {})
        merged["limit"] = str(page_size)

        # First request — also determines pagination style
        try:
            resp = self._request("GET", path, params=merged)
        except CentralAPIError as exc:
            raise PaginationError(
                exc.status_code, exc.error_code,
                f"Pagination failed on first page: {exc.message}", exc.debug_id,
            ) from exc

        if not isinstance(resp, dict):
            raise PaginationError(0, "", f"Expected dict response, got {type(resp).__name__}")

        # Detect item key
        key = item_key or detect_item_key(resp)
        if key is None:
            raise PaginationError(0, "", f"Cannot detect item array in response keys: {list(resp.keys())}")

        items = resp.get(key, [])
        all_items.extend(items)
        total = resp.get("total", 0)

        # Detect pagination style from the first response
        style = "cursor" if resp.get("next") is not None else "offset"

        for page_num in range(2, max_pages + 1):
            if total and len(all_items) >= total:
                break
            if not items:
                break

            page_params = dict(merged)
            if style == "cursor":
                cursor = resp.get("next")
                if not cursor:
                    break
                page_params["next"] = str(cursor)
            else:
                page_params["offset"] = str(len(all_items))

            try:
                resp = self._request("GET", path, params=page_params)
            except CentralAPIError as exc:
                raise PaginationError(
                    exc.status_code, exc.error_code,
                    f"Pagination failed on page {page_num}: {exc.message}", exc.debug_id,
                ) from exc

            items = resp.get(key, [])
            all_items.extend(items)
        else:
            # Loop exhausted ``max_pages`` without the natural exits firing
            # (no ``next`` cursor / total reached / empty page). Raise hard
            # — silently truncating server-side data is a data-correctness
            # hazard for downstream RCA workflows. Callers who genuinely
            # want a partial result must opt in by raising ``max_pages``
            # or pre-filtering the query (e.g. add a server-side filter).
            if total and len(all_items) < total:
                raise PaginationError(
                    0,
                    "PAGINATION_TRUNCATED",
                    (
                        f"paginate() hit max_pages={max_pages} after "
                        f"collecting {len(all_items)}/{total} items. "
                        "Increase max_pages= or pre-filter the query "
                        "server-side (e.g. with a `filter=` parameter) "
                        "to keep the result bounded."
                    ),
                )

        return all_items


# Module-level singleton — ready to use on import
api = CentralAPI()


class GreenLakeAPI(BaseHTTPClient):
    """Pre-authenticated HTTP client for HPE GreenLake Platform API.

    Reads credentials from environment variables injected by the MCP server.
    Same smart-client features as CentralAPI: token management, 401 retry,
    429 rate-limit retry, structured error parsing, and pagination.

    Usage inside a script::

        from central_helpers import glp

        devices = glp.get("devices/v1/devices", params={"limit": "100"})
    """

    def __init__(self) -> None:
        self._glp_client_id = os.environ.get(
            "GREENLAKE_CLIENT_ID", os.environ.get("GLP_CLIENT_ID", "")
        )
        self._glp_client_secret = os.environ.get(
            "GREENLAKE_CLIENT_SECRET", os.environ.get("GLP_CLIENT_SECRET", "")
        )
        super().__init__(
            base_url=os.environ.get(
                "GLP_BASE_URL", "https://global.api.greenlake.hpe.com"
            ),
            client_id=self._glp_client_id,
            client_secret=self._glp_client_secret,
        )

    @property
    def available(self) -> bool:
        """True if GreenLake credentials are configured."""
        return bool(self._glp_client_id and self._glp_client_secret)

    def _ensure_token(self) -> None:
        if not self.available:
            raise AuthenticationError(
                0, "", "GreenLake credentials not configured. "
                "Set GREENLAKE_CLIENT_ID and GREENLAKE_CLIENT_SECRET."
            )
        super()._ensure_token()

    def paginate(
        self,
        path: str,
        params: dict | None = None,
        *,
        max_pages: int = 50,
        page_size: int = 100,
        item_key: str | None = None,
    ) -> list[dict]:
        """Fetch all pages of a paginated GreenLake list endpoint.

        Supports offset-based pagination (standard GreenLake pattern).
        """
        all_items: list[dict] = []
        merged = dict(params or {})
        merged["limit"] = str(page_size)

        try:
            resp = self._request("GET", path, params=merged)
        except CentralAPIError as exc:
            raise PaginationError(
                exc.status_code, exc.error_code,
                f"Pagination failed on first page: {exc.message}", exc.debug_id,
            ) from exc

        if not isinstance(resp, dict):
            raise PaginationError(0, "", f"Expected dict response, got {type(resp).__name__}")

        key = item_key or detect_item_key(resp)
        if key is None:
            raise PaginationError(0, "", f"Cannot detect item array in response keys: {list(resp.keys())}")

        items = resp.get(key, [])
        all_items.extend(items)
        total = resp.get("total", resp.get("count", 0))

        for page_num in range(2, max_pages + 1):
            if total and len(all_items) >= total:
                break
            if not items:
                break

            page_params = dict(merged)
            page_params["offset"] = str(len(all_items))

            try:
                resp = self._request("GET", path, params=page_params)
            except CentralAPIError as exc:
                raise PaginationError(
                    exc.status_code, exc.error_code,
                    f"Pagination failed on page {page_num}: {exc.message}", exc.debug_id,
                ) from exc

            items = resp.get(key, [])
            all_items.extend(items)
        else:
            if total and len(all_items) < total:
                raise PaginationError(
                    0,
                    "PAGINATION_TRUNCATED",
                    (
                        f"paginate() hit max_pages={max_pages} after "
                        f"collecting {len(all_items)}/{total} items. "
                        "Increase max_pages= or pre-filter the query "
                        "server-side to keep the result bounded."
                    ),
                )

        return all_items


# Module-level GreenLake singleton — ready to use on import
glp = GreenLakeAPI()


# ── Graph helper for enrichment scripts ──────────────────────────────


class GraphHelper:
    """Read/write access to the shared LadybugDB graph database via IPC.

    The MCP server runs an authenticated loopback TCP endpoint that holds the
    LadybugDB database open. Scripts receive its host, ephemeral port, and
    capability token through their environment and send JSON requests instead
    of opening the database directly.

    Usage::

        from central_helpers import graph

        graph.execute("MERGE (n:Device {serial: $s}) SET n.name = $name",
                      {"s": "SN1", "name": "Switch-1"})
        rows = graph.query("MATCH (d:Device) RETURN d.serial, d.name")
    """

    def __init__(self) -> None:
        self._sock = None
        self._rfile = None
        self._wfile = None
        self._token = ""
        self._req_id = 0

    def _ensure_conn(self):
        if self._sock is not None:
            return
        import socket as _socket

        host = os.environ.get("GRAPH_IPC_HOST", "")
        port = os.environ.get("GRAPH_IPC_PORT", "")
        token = os.environ.get("GRAPH_IPC_TOKEN", "")
        if not host or not port or not token:
            raise RuntimeError(
                "GRAPH_IPC_HOST/PORT/TOKEN not set — graph access is only "
                "available in scripts executed via the MCP server."
            )
        if host != "127.0.0.1":
            raise RuntimeError("Graph IPC host must be the local loopback address")
        try:
            port_number = int(port)
        except ValueError as exc:
            raise RuntimeError("GRAPH_IPC_PORT must be an integer") from exc
        self._token = token
        self._sock = _socket.create_connection((host, port_number), timeout=10)
        self._rfile = self._sock.makefile("rb")
        self._wfile = self._sock.makefile("wb")

    def _call(self, method: str, cypher: str, params: dict | None = None) -> list[dict]:
        self._ensure_conn()
        self._req_id += 1
        req = {
            "id": self._req_id,
            "token": self._token,
            "method": method,
            "cypher": cypher,
            "params": params or {},
        }
        data = (json.dumps(req, default=str) + "\n").encode("utf-8")
        self._wfile.write(data)
        self._wfile.flush()
        line = self._rfile.readline()
        if not line:
            raise RuntimeError("IPC connection closed by server")
        resp = json.loads(line)
        if "error" in resp:
            raise RuntimeError(f"Graph IPC error: {resp['error']}")
        return resp.get("result", [])

    def query(self, cypher: str, params: dict | None = None) -> list[dict]:
        """Execute a read-only Cypher query and return rows as dicts."""
        return self._call("query", cypher, params)

    def execute(self, cypher: str, params: dict | None = None) -> list[dict]:
        """Execute a Cypher statement (including writes) and return rows."""
        return self._call("execute", cypher, params)


# Module-level graph singleton
graph = GraphHelper()
