"""Read compiler projection rows with L2/L1 provenance detail.

The compiler projection is intentionally a compact traversal index.  This
module provides the escape hatch back to the richer semantic overlay and the
lossless OpenAPI AST so callers do not have to treat projected columns as the
closed-world source of truth.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import real_ladybug as lb

_DEFAULT_BUFFER_POOL_SIZE = 256 * 1024 * 1024

_PROJECTION_TABLE_COLUMNS: dict[str, tuple[str, tuple[str, ...]]] = {
    "ApiEndpoint": (
        "endpoint_id",
        (
            "endpoint_id",
            "method",
            "path",
            "summary",
            "description",
            "operationId",
            "category",
            "deprecated",
            "tags",
            "parameters",
            "requestBody",
            "responses",
        ),
    ),
    "Parameter": (
        "parameter_id",
        (
            "parameter_id",
            "endpoint_id",
            "name",
            "location",
            "required",
            "type",
            "format",
            "enumValues",
            "pattern",
            "inferredHint",
            "description",
        ),
    ),
    "RequestBody": (
        "request_body_id",
        (
            "request_body_id",
            "endpoint_id",
            "content_type",
            "required",
            "root_component_ref",
        ),
    ),
    "Response": (
        "response_id",
        (
            "response_id",
            "endpoint_id",
            "status",
            "content_type",
            "root_component_ref",
        ),
    ),
    "SchemaComponent": (
        "component_id",
        (
            "component_id",
            "spec_source",
            "section",
            "name",
            "type",
            "kind",
            "bodyShape",
            "required",
            "enumValues",
            "supportedDeviceTypes",
            "arrayKey",
            "constraintsJson",
            "bodyJson",
        ),
    ),
    "Property": (
        "property_id",
        (
            "property_id",
            "parent_component_id",
            "name",
            "type",
            "format",
            "required",
            "enumValues",
            "description",
            "supportedDeviceTypes",
            "yangPath",
            "extensionsJson",
            "readOnly",
            "pattern",
            "defaultValue",
            "minimum",
            "maximum",
            "minLength",
            "maxLength",
            "enumDescriptionsJson",
            "constraintsJson",
        ),
    ),
    "YangPath": ("yangPath", ("yangPath", "module")),
    "YangModule": ("module", ("module",)),
    "CliCommand": (
        "command_id",
        (
            "command_id",
            "commandName",
            "commandUse",
            "parentCommand",
            "pathToPrint",
            "paramKeys",
        ),
    ),
}


class ProjectionDetailError(ValueError):
    """Base error for compiler projection detail lookups."""


class UnknownProjectionTableError(ProjectionDetailError):
    """Raised when a detail lookup names a table outside the compiler projection."""


class ProjectionRowNotFoundError(ProjectionDetailError):
    """Raised when either the projection row or provenance row is absent."""


def load_projection_detail(
    *,
    compiler_db_path: Path,
    ast_db_path: Path,
    table_name: str,
    row_id: str,
    buffer_pool_size: int = _DEFAULT_BUFFER_POOL_SIZE,
) -> dict[str, Any]:
    """Open compiler/AST databases and return full provenance for one row."""
    _projection_columns(table_name)
    compiler_db = None
    ast_db = None
    try:
        compiler_db = lb.Database(str(compiler_db_path), buffer_pool_size=buffer_pool_size)
        ast_db = lb.Database(str(ast_db_path), buffer_pool_size=buffer_pool_size)
        return fetch_projection_detail(
            compiler_conn=lb.Connection(compiler_db),
            ast_conn=lb.Connection(ast_db),
            table_name=table_name,
            row_id=row_id,
        )
    finally:
        if ast_db is not None:
            ast_db.close()
        if compiler_db is not None:
            compiler_db.close()


def fetch_projection_detail(
    *,
    compiler_conn,
    ast_conn,
    table_name: str,
    row_id: str,
) -> dict[str, Any]:
    """Return projected row data plus its semantic and AST source detail."""
    projection_row = _fetch_projection_row(compiler_conn, table_name, row_id)
    provenance_rows = _fetch_provenance_rows(compiler_conn, table_name, row_id)
    provenance = provenance_rows[0]
    semantic_node = _fetch_semantic_node(ast_conn, provenance.get("semantic_id") or "")
    ast_node = _fetch_ast_node(
        ast_conn,
        ast_node_id=provenance.get("ast_node_id") or "",
        spec_id=provenance.get("spec_id") or "",
        json_pointer=provenance.get("json_pointer") or "",
    )
    raw_json = _load_json((ast_node or {}).get("rawJson"))
    scalar_json = _load_json((ast_node or {}).get("scalarJson"))

    return {
        "table_name": table_name,
        "row_id": row_id,
        "projection_row": projection_row,
        "provenance": provenance,
        "provenance_rows": provenance_rows,
        "semantic_node": semantic_node,
        "semantic_summary": _load_json((semantic_node or {}).get("summaryJson")),
        "ast_node": ast_node,
        "raw_openapi": raw_json if raw_json is not None else scalar_json,
    }


def _fetch_projection_row(conn, table_name: str, row_id: str) -> dict[str, Any]:
    id_column, columns = _projection_columns(table_name)
    select_list = ", ".join(f"row.{column} AS {column}" for column in columns)
    rows = list(
        conn.execute(
            f"""
            MATCH (row:{table_name} {{{id_column}: $row_id}})
            RETURN {select_list}
            LIMIT 1
            """,
            parameters={"row_id": row_id},
        ).rows_as_dict()
    )
    if not rows:
        raise ProjectionRowNotFoundError(
            f"No compiler projection row found for {table_name}:{row_id}"
        )
    return rows[0]


def _fetch_provenance_rows(conn, table_name: str, row_id: str) -> list[dict[str, Any]]:
    rows = list(
        conn.execute(
            """
            MATCH (row:CompilerProjectionMap {table_name: $table_name, row_id: $row_id})
            RETURN row.projection_id AS projection_id,
                   row.table_name AS table_name,
                   row.row_id AS row_id,
                   row.semantic_id AS semantic_id,
                   row.ast_node_id AS ast_node_id,
                   row.json_pointer AS json_pointer,
                   row.spec_id AS spec_id,
                   row.source AS source,
                   row.ingestion_status AS ingestion_status,
                   row.ingestion_error_type AS ingestion_error_type
            """,
            parameters={"table_name": table_name, "row_id": row_id},
        ).rows_as_dict()
    )
    if not rows:
        raise ProjectionRowNotFoundError(
            f"No compiler projection provenance found for {table_name}:{row_id}"
        )
    rows.sort(
        key=lambda row: (
            0 if row.get("ingestion_status") == "strict_valid" else 1,
            row.get("source") or "",
            row.get("semantic_id") or "",
            row.get("ast_node_id") or "",
        )
    )
    return rows


def _fetch_semantic_node(conn, semantic_id: str) -> dict[str, Any] | None:
    if not semantic_id:
        return None
    rows = list(
        conn.execute(
            """
            MATCH (row:SemanticNode {semantic_id: $semantic_id})
            RETURN row.semantic_id AS semantic_id,
                   row.spec_id AS spec_id,
                   row.kind AS kind,
                   row.name AS name,
                   row.ast_node_id AS ast_node_id,
                   row.jsonPointer AS jsonPointer,
                   row.stableKey AS stableKey,
                   row.summaryJson AS summaryJson
            LIMIT 1
            """,
            parameters={"semantic_id": semantic_id},
        ).rows_as_dict()
    )
    return rows[0] if rows else None


def _fetch_ast_node(
    conn,
    *,
    ast_node_id: str,
    spec_id: str,
    json_pointer: str,
) -> dict[str, Any] | None:
    rows = []
    if ast_node_id:
        rows = list(
            conn.execute(
                """
                MATCH (row:OasAstNode {node_id: $ast_node_id})
                RETURN row.node_id AS node_id,
                       row.spec_id AS spec_id,
                       row.kind AS kind,
                       row.jsonPointer AS jsonPointer,
                       row.name AS name,
                       row.key AS key,
                       row.index AS index,
                       row.valueType AS valueType,
                       row.rawJson AS rawJson,
                       row.scalarJson AS scalarJson,
                       row.isExtension AS isExtension
                LIMIT 1
                """,
                parameters={"ast_node_id": ast_node_id},
            ).rows_as_dict()
        )
    if not rows and spec_id and json_pointer:
        rows = list(
            conn.execute(
                """
                MATCH (row:OasAstNode {spec_id: $spec_id, jsonPointer: $json_pointer})
                RETURN row.node_id AS node_id,
                       row.spec_id AS spec_id,
                       row.kind AS kind,
                       row.jsonPointer AS jsonPointer,
                       row.name AS name,
                       row.key AS key,
                       row.index AS index,
                       row.valueType AS valueType,
                       row.rawJson AS rawJson,
                       row.scalarJson AS scalarJson,
                       row.isExtension AS isExtension
                LIMIT 1
                """,
                parameters={"spec_id": spec_id, "json_pointer": json_pointer},
            ).rows_as_dict()
        )
    return rows[0] if rows else None


def _projection_columns(table_name: str) -> tuple[str, tuple[str, ...]]:
    columns = _PROJECTION_TABLE_COLUMNS.get(table_name)
    if columns is None:
        raise UnknownProjectionTableError(f"Unknown compiler projection table: {table_name}")
    return columns


def _load_json(value: Any) -> Any:
    if not isinstance(value, str) or not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None
