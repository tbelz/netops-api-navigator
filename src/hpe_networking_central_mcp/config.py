"""Cross-platform configuration for the MCP server."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from platformdirs import PlatformDirs

APP_NAME = "hpe-networking-central-mcp"
APP_AUTHOR = "tbelz"
DEFAULT_KNOWLEDGE_RELEASE_REPO = "tbelz/hpe-networking-central-mcp"
SUPPORTED_PROFILES = {"full", "workshop"}


def _platform_dirs() -> PlatformDirs:
    return PlatformDirs(APP_NAME, APP_AUTHOR)


def _default_data_path(name: str) -> Path:
    return Path(_platform_dirs().user_data_path) / name


def _default_cache_path(name: str) -> Path:
    return Path(_platform_dirs().user_cache_path) / name


@dataclass(frozen=True)
class Settings:
    """Server settings loaded from environment variables.

    ``workshop`` is a fail-closed profile. Constructing settings for that
    profile always enables read-only mode and disables every optional surface
    that can mutate local state or execute code.
    """

    profile: str = "full"

    # Central API credentials
    central_base_url: str = ""
    central_client_id: str = ""
    central_client_secret: str = ""

    # GLP credentials (default to Central creds)
    glp_client_id: str = ""
    glp_client_secret: str = ""
    glp_base_url: str = "https://global.api.greenlake.hpe.com"

    # Host-native paths. Environment variables can still override every path
    # so the existing Docker image keeps its explicit /data and /scripts
    # layout.
    script_library_path: Path = field(default_factory=lambda: _default_data_path("scripts"))
    docs_path: Path = field(default_factory=lambda: _default_data_path("docs"))
    graph_db_path: Path = field(default_factory=lambda: _default_data_path("graph_db"))
    spec_cache_path: Path = field(default_factory=lambda: _default_cache_path("specs"))

    # Retained only as a compatibility field for callers that construct
    # Settings directly. Runtime graph IPC now uses authenticated loopback TCP.
    graph_ipc_socket: Path | None = None

    inventory_cache_ttl: int = 300
    glp_included_slugs: str = ""

    # GitHub knowledge release selection. An empty tag follows the newest
    # knowledge-db-* release; a tag pins startup to one immutable snapshot.
    knowledge_release_repo: str = DEFAULT_KNOWLEDGE_RELEASE_REPO
    knowledge_release_tag: str = ""
    knowledge_asset_sha256: str = ""
    knowledge_projection: str = "legacy"
    compiler_tools: bool = False
    runtime_hydration: bool = False
    compiler_db_path: Path = field(
        default_factory=lambda: _default_data_path("knowledge_db_compiler")
    )
    compiler_ast_db_path: Path = field(
        default_factory=lambda: _default_data_path("knowledge_db_ast")
    )

    read_only: bool = False

    def __post_init__(self) -> None:
        profile = self.profile.strip().lower()
        if profile not in SUPPORTED_PROFILES:
            choices = ", ".join(sorted(SUPPORTED_PROFILES))
            raise ValueError(f"Unsupported MCP profile {self.profile!r}; choose one of: {choices}")
        object.__setattr__(self, "profile", profile)
        digest = self.knowledge_asset_sha256.strip().lower()
        if digest.startswith("sha256:"):
            digest = digest.removeprefix("sha256:")
        if digest and (
            len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError(
                "KNOWLEDGE_ASSET_SHA256 must be a 64-character SHA-256 hex digest"
            )
        object.__setattr__(self, "knowledge_asset_sha256", digest)
        if profile == "workshop":
            object.__setattr__(self, "read_only", True)
            object.__setattr__(self, "compiler_tools", False)
            object.__setattr__(self, "runtime_hydration", False)

    @property
    def workshop_mode(self) -> bool:
        return self.profile == "workshop"

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
    profile = os.environ.get("MCP_PROFILE", "full").strip().lower() or "full"
    knowledge_projection = (
        os.environ.get("MCP_KNOWLEDGE_PROJECTION")
        or os.environ.get("KNOWLEDGE_PROJECTION")
        or "legacy"
    ).strip().lower()
    if knowledge_projection in {"compiler", "v2"}:
        knowledge_projection = "v2"
    elif knowledge_projection != "legacy":
        knowledge_projection = "legacy"

    graph_db_path = Path(os.environ.get("GRAPH_DB_PATH") or _default_data_path("graph_db"))
    default_compiler_db_path = graph_db_path.parent / "knowledge_db_compiler"
    legacy_socket = os.environ.get("GRAPH_IPC_SOCKET", "").strip()

    return Settings(
        profile=profile,
        central_base_url=os.environ.get("CENTRAL_BASE_URL", "").strip().rstrip("/"),
        central_client_id=os.environ.get("CENTRAL_CLIENT_ID", "").strip(),
        central_client_secret=os.environ.get("CENTRAL_CLIENT_SECRET", "").strip(),
        glp_client_id=os.environ.get(
            "GREENLAKE_CLIENT_ID", os.environ.get("GLP_CLIENT_ID", "")
        ).strip(),
        glp_client_secret=os.environ.get(
            "GREENLAKE_CLIENT_SECRET", os.environ.get("GLP_CLIENT_SECRET", "")
        ).strip(),
        glp_base_url=os.environ.get(
            "GLP_BASE_URL", "https://global.api.greenlake.hpe.com"
        ).strip().rstrip("/"),
        script_library_path=Path(
            os.environ.get("SCRIPT_LIBRARY_PATH") or _default_data_path("scripts")
        ),
        docs_path=Path(os.environ.get("DOCS_PATH") or _default_data_path("docs")),
        graph_db_path=graph_db_path,
        spec_cache_path=Path(
            os.environ.get("SPEC_CACHE_DIR") or _default_cache_path("specs")
        ),
        graph_ipc_socket=Path(legacy_socket) if legacy_socket else None,
        inventory_cache_ttl=int(os.environ.get("INVENTORY_CACHE_TTL", "300")),
        glp_included_slugs=os.environ.get("GLP_INCLUDED_SLUGS", "").strip(),
        knowledge_release_repo=os.environ.get(
            "KNOWLEDGE_RELEASE_REPO", DEFAULT_KNOWLEDGE_RELEASE_REPO
        ).strip(),
        knowledge_release_tag=os.environ.get("KNOWLEDGE_RELEASE_TAG", "").strip(),
        knowledge_asset_sha256=os.environ.get("KNOWLEDGE_ASSET_SHA256", "").strip(),
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
