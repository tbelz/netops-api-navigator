"""Configuration from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """Server settings loaded from environment variables."""

    # Central API credentials
    central_base_url: str = ""
    central_client_id: str = ""
    central_client_secret: str = ""

    # GLP credentials (default to Central creds)
    glp_client_id: str = ""
    glp_client_secret: str = ""

    # GreenLake base URL
    glp_base_url: str = "https://global.api.greenlake.hpe.com"

    # Paths
    script_library_path: Path = field(default_factory=lambda: Path("/scripts/library"))
    docs_path: Path = field(default_factory=lambda: Path("/docs"))
    graph_db_path: Path = field(default_factory=lambda: Path("/data/graph_db"))
    graph_ipc_socket: Path = field(default_factory=lambda: Path("/tmp/ladybug_graph.sock"))

    # Inventory cache TTL in seconds
    inventory_cache_ttl: int = 300

    # GreenLake service slugs to include (comma-separated, or "*" for all)
    glp_included_slugs: str = ""

    # GitHub release repository for knowledge DB (owner/repo)
    knowledge_release_repo: str = ""
    knowledge_projection: str = "legacy"
    compiler_tools: bool = False
    runtime_hydration: bool = False
    compiler_db_path: Path = field(default_factory=lambda: Path("/data/knowledge_db_compiler"))
    compiler_ast_db_path: Path = field(default_factory=lambda: Path("/data/knowledge_db_ast"))

    # Read-only mode: refuse mutating Central / GreenLake API calls and hide
    # non-GET endpoints from the API catalog tools. Local graph writes and
    # script CRUD/execution remain available.
    read_only: bool = False

    @property
    def parsed_glp_included_slugs(self) -> set[str] | None:
        """Parse ``glp_included_slugs`` into a set, or ``None`` for all."""
        val = self.glp_included_slugs
        if not val or val == "*":
            return None
        return {s.strip() for s in val.split(",") if s.strip()}

    @property
    def has_credentials(self) -> bool:
        return bool(self.central_base_url and self.central_client_id and self.central_client_secret)

    @property
    def effective_glp_client_id(self) -> str:
        return self.glp_client_id or self.central_client_id

    @property
    def effective_glp_client_secret(self) -> str:
        return self.glp_client_secret or self.central_client_secret

    @property
    def has_glp_credentials(self) -> bool:
        return bool(self.effective_glp_client_id and self.effective_glp_client_secret)


_TRUTHY = {"1", "true", "yes", "on"}


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in _TRUTHY


def load_settings() -> Settings:
    """Load settings from environment variables."""
    knowledge_projection = (
        os.environ.get("MCP_KNOWLEDGE_PROJECTION")
        or os.environ.get("KNOWLEDGE_PROJECTION")
        or "legacy"
    ).strip().lower()
    if knowledge_projection in {"compiler", "v2"}:
        knowledge_projection = "v2"
    elif knowledge_projection != "legacy":
        knowledge_projection = "legacy"
    graph_db_path = Path(os.environ.get("GRAPH_DB_PATH", "/data/graph_db"))
    default_compiler_db_path = graph_db_path.parent / "knowledge_db_compiler"
    return Settings(
        central_base_url=os.environ.get("CENTRAL_BASE_URL", "").strip().rstrip("/"),
        central_client_id=os.environ.get("CENTRAL_CLIENT_ID", "").strip(),
        central_client_secret=os.environ.get("CENTRAL_CLIENT_SECRET", "").strip(),
        glp_client_id=os.environ.get("GREENLAKE_CLIENT_ID", os.environ.get("GLP_CLIENT_ID", "")).strip(),
        glp_client_secret=os.environ.get("GREENLAKE_CLIENT_SECRET", os.environ.get("GLP_CLIENT_SECRET", "")).strip(),
        glp_base_url=os.environ.get("GLP_BASE_URL", "https://global.api.greenlake.hpe.com").strip().rstrip("/"),
        script_library_path=Path(os.environ.get("SCRIPT_LIBRARY_PATH", "/scripts/library")),
        docs_path=Path(os.environ.get("DOCS_PATH", "/docs")),
        inventory_cache_ttl=int(os.environ.get("INVENTORY_CACHE_TTL", "300")),
        glp_included_slugs=os.environ.get("GLP_INCLUDED_SLUGS", "").strip(),
        graph_db_path=graph_db_path,
        graph_ipc_socket=Path(os.environ.get("GRAPH_IPC_SOCKET", "/tmp/ladybug_graph.sock")),
        knowledge_release_repo=os.environ.get("KNOWLEDGE_RELEASE_REPO", "").strip(),
        knowledge_projection=knowledge_projection,
        compiler_tools=_parse_bool(os.environ.get("MCP_COMPILER_TOOLS", "")),
        runtime_hydration=_parse_bool(os.environ.get("MCP_RUNTIME_HYDRATION", "")),
        compiler_db_path=Path(
            os.environ.get("MCP_COMPILER_DB_PATH", str(default_compiler_db_path))
        ),
        compiler_ast_db_path=Path(
            os.environ.get(
                "MCP_COMPILER_AST_DB_PATH",
                str(graph_db_path.parent / "knowledge_db_ast"),
            )
        ),
        read_only=_parse_bool(os.environ.get("READ_ONLY", "")),
    )
