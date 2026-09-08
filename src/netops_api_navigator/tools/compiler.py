"""Opt-in MCP tools for ADR-011 compiler graph artifacts."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import structlog
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ..compiler.detail_reader import (
    ProjectionDetailError,
    load_projection_detail,
)
from ..compiler.traversal_report import load_compiler_traversal_report
from ..config import Settings

logger = structlog.get_logger("tools.compiler")

_DB_BUFFER_POOL_SIZE = 256 * 1024 * 1024
_DEFAULT_RESPONSE_BYTES = 200_000


def register_compiler_tools(mcp, settings: Settings) -> None:
    """Register opt-in compiler provenance and release-health tools.

    Normal endpoint discovery and schema traversal intentionally stay on
    the shared graph-query aliases (``query_fts``, ``query_api_schema``,
    ``query_yang``). The compiler tools here are diagnostics/escape hatches
    over sidecar artifacts, not a parallel API-discovery model.
    """

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False),
    )
    def get_openapi_source_detail(table_name: str, row_id: str) -> str:
        """Resolve a compiler projection row back to source OpenAPI detail.

        Returns the projection row, provenance, semantic summary, AST node
        metadata, and raw OpenAPI object or scalar for that row.
        """
        _require_artifacts(settings, include_ast=True)
        try:
            detail = load_projection_detail(
                compiler_db_path=settings.compiler_db_path,
                ast_db_path=settings.compiler_ast_db_path,
                table_name=table_name,
                row_id=row_id,
                buffer_pool_size=_DB_BUFFER_POOL_SIZE,
            )
            return _json_response(detail)
        except ProjectionDetailError as exc:
            raise ToolError(str(exc)) from exc
        except Exception as exc:
            raise ToolError(f"Compiler source detail failed: {exc}") from exc

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False),
    )
    def get_compiler_graph_health(
        endpoint_limit: int = 100,
        schema_limit: int = 100,
    ) -> str:
        """Report compiler graph traversal health from bounded samples."""
        _require_artifacts(settings, include_ast=True)
        try:
            report = load_compiler_traversal_report(
                compiler_db_path=settings.compiler_db_path,
                ast_db_path=settings.compiler_ast_db_path,
                endpoint_limit=endpoint_limit,
                schema_limit=schema_limit,
                buffer_pool_size=_DB_BUFFER_POOL_SIZE,
            )
            return _json_response(report)
        except Exception as exc:
            raise ToolError(f"Compiler graph health failed: {exc}") from exc


def _require_artifacts(settings: Settings, *, include_ast: bool) -> None:
    missing = []
    if not _artifact_exists(settings.compiler_db_path):
        missing.append(f"compiler DB at {settings.compiler_db_path}")
    if include_ast and not _artifact_exists(settings.compiler_ast_db_path):
        missing.append(f"AST DB at {settings.compiler_ast_db_path}")
    if missing:
        raise ToolError(
            "Compiler artifacts are unavailable: "
            + ", ".join(missing)
            + ". Set MCP_COMPILER_TOOLS=true with hydrated compiler artifacts."
        )


def _artifact_exists(path: Path) -> bool:
    return path.exists()


def _json_response(payload: Any) -> str:
    cap = _env_int("MCP_COMPILER_RESPONSE_BYTES", _DEFAULT_RESPONSE_BYTES)
    body = json.dumps(payload, indent=2, default=str)
    if len(body.encode("utf-8")) <= cap:
        return body

    redacted = _redact_raw_payloads(payload)
    body = json.dumps(
        {
            "_truncated": True,
            "reason": "compiler_response_byte_cap",
            "cap_bytes": cap,
            "payload": redacted,
        },
        indent=2,
        default=str,
    )
    if len(body.encode("utf-8")) <= cap:
        return body
    return json.dumps(
        {
            "_truncated": True,
            "reason": "compiler_response_byte_cap",
            "cap_bytes": cap,
            "hint": (
                "Narrow the request, lower sample limits, or fetch raw source "
                "detail for a smaller projection row."
            ),
        },
        indent=2,
    )


def _redact_raw_payloads(value: Any) -> Any:
    if isinstance(value, list):
        return [_redact_raw_payloads(item) for item in value]
    if not isinstance(value, dict):
        return value
    redacted: dict[str, Any] = {}
    for key, item in value.items():
        if key in {"raw_openapi", "rawJson", "scalarJson", "bodyJson"}:
            redacted[key] = _redaction_envelope(item)
        else:
            redacted[key] = _redact_raw_payloads(item)
    return redacted


def _redaction_envelope(value: Any) -> dict[str, Any]:
    size = len(json.dumps(value, default=str).encode("utf-8"))
    return {
        "_truncated": True,
        "size_bytes": size,
        "hint": "Raw payload omitted by MCP_COMPILER_RESPONSE_BYTES cap.",
    }


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default
