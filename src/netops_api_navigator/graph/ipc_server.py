"""Authenticated loopback IPC for graph access from script subprocesses.

The server deliberately uses TCP on ``127.0.0.1`` rather than Unix-domain
sockets so the full MCP profile works unchanged on Windows and macOS. Each
server start creates an unguessable capability token which every request must
carry. The token and selected ephemeral port are passed only to child scripts.
"""

from __future__ import annotations

import json
import secrets
import socket
import socketserver
import threading
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from .manager import GraphManager

logger = structlog.get_logger("graph.ipc_server")


class _GraphRequestHandler(socketserver.StreamRequestHandler):
    """Handle newline-delimited JSON requests from one child process."""

    def handle(self) -> None:
        try:
            self._handle_requests()
        except OSError:
            # Peer disconnected or shutdown interrupted an idle socket.
            pass

    def _handle_requests(self) -> None:
        manager: GraphManager = self.server.graph_manager  # type: ignore[attr-defined]
        expected_token: str = self.server.capability_token  # type: ignore[attr-defined]
        for raw_line in self.rfile:
            line = raw_line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError as exc:
                self._send({"error": f"Invalid JSON: {exc}"})
                continue

            req_id = req.get("id")
            if not secrets.compare_digest(str(req.get("token", "")), expected_token):
                self._send({"id": req_id, "error": "Unauthorized graph IPC request"})
                continue

            method = req.get("method", "")
            cypher = req.get("cypher", "")
            params = req.get("params") or {}
            if method not in ("query", "execute"):
                self._send({"id": req_id, "error": f"Unknown method: {method}"})
                continue

            try:
                if method == "query":
                    rows = manager.query(cypher, params=params, read_only=False)
                else:
                    rows = manager.execute(cypher, params)
                self._send({"id": req_id, "result": rows})
            except Exception as exc:
                self._send({"id": req_id, "error": str(exc)})

    def _send(self, obj: dict) -> None:
        self.wfile.write((json.dumps(obj, default=str) + "\n").encode("utf-8"))
        self.wfile.flush()


class _ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = False
    allow_reuse_address = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._requests: set[socket.socket] = set()
        self._requests_lock = threading.Lock()

    def process_request(self, request, client_address):
        with self._requests_lock:
            self._requests.add(request)
        try:
            super().process_request(request, client_address)
        except BaseException:
            with self._requests_lock:
                self._requests.discard(request)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._requests_lock:
                self._requests.discard(request)

    def drain(self):
        # Called after shutdown() stops admission. Wake idle readers without
        # interrupting graph work, then server_close() joins every handler.
        with self._requests_lock:
            for request in self._requests:
                try:
                    request.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        self.server_close()


class GraphIPCServer:
    """Own an authenticated, loopback-only graph IPC endpoint."""

    def __init__(self, graph_manager: GraphManager) -> None:
        self._graph_manager = graph_manager
        self._token = secrets.token_urlsafe(32)
        self._server: _ThreadedTCPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def host(self) -> str:
        return "127.0.0.1"

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("Graph IPC server has not been started")
        return int(self._server.server_address[1])

    @property
    def capability_token(self) -> str:
        return self._token

    @property
    def environment(self) -> dict[str, str]:
        """Environment values to inject into an authorized child process."""
        return {
            "GRAPH_IPC_HOST": self.host,
            "GRAPH_IPC_PORT": str(self.port),
            "GRAPH_IPC_TOKEN": self.capability_token,
        }

    def start(self) -> None:
        if self._server is not None:
            return
        self._server = _ThreadedTCPServer((self.host, 0), _GraphRequestHandler)
        self._server.graph_manager = self._graph_manager  # type: ignore[attr-defined]
        self._server.capability_token = self._token  # type: ignore[attr-defined]
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.05},
            name="netops-api-navigator-graph-ipc",
            daemon=True,
        )
        self._thread.start()
        logger.info("graph_ipc_started", host=self.host, port=self.port)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.drain()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        logger.info("graph_ipc_stopped")
