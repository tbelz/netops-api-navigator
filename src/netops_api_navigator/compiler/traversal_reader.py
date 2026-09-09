"""Constrained read primitives for compiler graph artifacts.

These helpers are a staging surface for future agent-facing MCP tools.  They
use the typed compiler projection for deterministic traversal, and the
provenance detail reader for full OpenAPI source access when compact rows are
not enough.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import real_ladybug as lb

from .detail_reader import (
    ProjectionDetailError,
    ProjectionRowNotFoundError,
    fetch_projection_detail,
)

_DEFAULT_BUFFER_POOL_SIZE = 256 * 1024 * 1024


class AmbiguousEndpointError(ProjectionDetailError):
    """Raised when method/path lookup finds more than one projected endpoint."""


def load_endpoint_context(
    *,
    compiler_db_path: Path,
    ast_db_path: Path,
    method: str,
    path: str,
    include_raw: bool = True,
    buffer_pool_size: int = _DEFAULT_BUFFER_POOL_SIZE,
) -> dict[str, Any]:
    """Open compiler artifacts and return endpoint traversal context."""
    compiler_db = None
    ast_db = None
    try:
        compiler_db = lb.Database(str(compiler_db_path), buffer_pool_size=buffer_pool_size)
        ast_db = lb.Database(str(ast_db_path), buffer_pool_size=buffer_pool_size)
        return fetch_endpoint_context(
            compiler_conn=lb.Connection(compiler_db),
            ast_conn=lb.Connection(ast_db),
            method=method,
            path=path,
            include_raw=include_raw,
        )
    finally:
        if ast_db is not None:
            ast_db.close()
        if compiler_db is not None:
            compiler_db.close()


def load_schema_context(
    *,
    compiler_db_path: Path,
    ast_db_path: Path,
    component_id: str,
    include_raw: bool = True,
    buffer_pool_size: int = _DEFAULT_BUFFER_POOL_SIZE,
) -> dict[str, Any]:
    """Open compiler artifacts and return schema traversal context."""
    compiler_db = None
    ast_db = None
    try:
        compiler_db = lb.Database(str(compiler_db_path), buffer_pool_size=buffer_pool_size)
        ast_db = lb.Database(str(ast_db_path), buffer_pool_size=buffer_pool_size)
        return fetch_schema_context(
            compiler_conn=lb.Connection(compiler_db),
            ast_conn=lb.Connection(ast_db),
            component_id=component_id,
            include_raw=include_raw,
        )
    finally:
        if ast_db is not None:
            ast_db.close()
        if compiler_db is not None:
            compiler_db.close()


def fetch_endpoint_context(
    *,
    compiler_conn,
    ast_conn,
    method: str,
    path: str,
    include_raw: bool = True,
) -> dict[str, Any]:
    """Return a deterministic endpoint-centered compiler graph view."""
    endpoints = _rows(
        compiler_conn,
        """
        MATCH (endpoint:ApiEndpoint {method: $method, path: $path})
        RETURN endpoint.endpoint_id AS endpoint_id
        ORDER BY endpoint.endpoint_id
        """,
        {"method": method.upper(), "path": path},
    )
    if not endpoints:
        raise ProjectionRowNotFoundError(
            f"No compiler endpoint found for {method.upper()}:{path}"
        )
    if len(endpoints) > 1:
        endpoint_ids = ", ".join(str(row["endpoint_id"]) for row in endpoints)
        raise AmbiguousEndpointError(
            f"Multiple compiler endpoints found for {method.upper()}:{path}: {endpoint_ids}"
        )
    endpoint_id = endpoints[0]["endpoint_id"]
    endpoint_detail = _detail(
        compiler_conn,
        ast_conn,
        "ApiEndpoint",
        endpoint_id,
        include_raw=include_raw,
    )

    parameters = []
    for row in _rows(
        compiler_conn,
        """
        MATCH (:ApiEndpoint {endpoint_id: $endpoint_id})-[:HAS_PARAMETER]->(param:Parameter)
        OPTIONAL MATCH (param)-[:PARAMETER_REFERENCES]->(target:SchemaComponent)
        RETURN param.parameter_id AS parameter_id,
               target.component_id AS target_component_id
        ORDER BY param.location, param.name
        """,
        {"endpoint_id": endpoint_id},
    ):
        parameter = _detail(
            compiler_conn,
            ast_conn,
            "Parameter",
            row["parameter_id"],
            include_raw=include_raw,
        )
        parameter["schema"] = _optional_detail(
            compiler_conn,
            ast_conn,
            "SchemaComponent",
            row.get("target_component_id") or "",
            include_raw=include_raw,
        )
        parameters.append(parameter)

    request_bodies = []
    for row in _rows(
        compiler_conn,
        """
        MATCH (:ApiEndpoint {endpoint_id: $endpoint_id})-[:HAS_REQUEST_BODY]->(body:RequestBody)
        RETURN body.request_body_id AS request_body_id,
               body.root_component_ref AS root_component_ref
        ORDER BY body.content_type
        """,
        {"endpoint_id": endpoint_id},
    ):
        body = _detail(
            compiler_conn,
            ast_conn,
            "RequestBody",
            row["request_body_id"],
            include_raw=include_raw,
        )
        body["schema"] = _optional_detail(
            compiler_conn,
            ast_conn,
            "SchemaComponent",
            row.get("root_component_ref") or "",
            include_raw=include_raw,
        )
        request_bodies.append(body)

    responses = []
    for row in _rows(
        compiler_conn,
        """
        MATCH (:ApiEndpoint {endpoint_id: $endpoint_id})-[:HAS_RESPONSE]->(response:Response)
        RETURN response.response_id AS response_id,
               response.root_component_ref AS root_component_ref
        ORDER BY response.status, response.content_type
        """,
        {"endpoint_id": endpoint_id},
    ):
        response = _detail(
            compiler_conn,
            ast_conn,
            "Response",
            row["response_id"],
            include_raw=include_raw,
        )
        response["schema"] = _optional_detail(
            compiler_conn,
            ast_conn,
            "SchemaComponent",
            row.get("root_component_ref") or "",
            include_raw=include_raw,
        )
        responses.append(response)

    yang_paths = [
        _detail(
            compiler_conn,
            ast_conn,
            "YangPath",
            row["yangPath"],
            include_raw=include_raw,
        )
        for row in _rows(
            compiler_conn,
            """
            MATCH (:ApiEndpoint {endpoint_id: $endpoint_id})-[:CONFIGURES_YANG]->(yang:YangPath)
            RETURN yang.yangPath AS yangPath
            ORDER BY yang.yangPath
            """,
            {"endpoint_id": endpoint_id},
        )
    ]

    cli_commands = [
        _detail(
            compiler_conn,
            ast_conn,
            "CliCommand",
            row["command_id"],
            include_raw=include_raw,
        )
        for row in _rows(
            compiler_conn,
            """
            MATCH (:ApiEndpoint {endpoint_id: $endpoint_id})-[:HAS_CLI_COMMAND]->(command:CliCommand)
            RETURN command.command_id AS command_id
            ORDER BY command.commandName
            """,
            {"endpoint_id": endpoint_id},
        )
    ]

    return {
        "endpoint": endpoint_detail,
        "parameters": parameters,
        "request_bodies": request_bodies,
        "responses": responses,
        "yang_paths": yang_paths,
        "cli_commands": cli_commands,
    }


def fetch_schema_context(
    *,
    compiler_conn,
    ast_conn,
    component_id: str,
    include_raw: bool = True,
) -> dict[str, Any]:
    """Return a deterministic schema-centered compiler graph view."""
    schema = _detail(
        compiler_conn,
        ast_conn,
        "SchemaComponent",
        component_id,
        include_raw=include_raw,
    )

    properties = []
    for row in _rows(
        compiler_conn,
        """
        MATCH (:SchemaComponent {component_id: $component_id})-[:HAS_PROPERTY]->(prop:Property)
        OPTIONAL MATCH (prop)-[:PROPERTY_OF_TYPE]->(target:SchemaComponent)
        OPTIONAL MATCH (prop)-[:HAS_ITEM_SCHEMA]->(item:SchemaComponent)
        RETURN prop.property_id AS property_id,
               target.component_id AS target_component_id,
               item.component_id AS item_component_id
        ORDER BY prop.name
        """,
        {"component_id": component_id},
    ):
        prop = _detail(
            compiler_conn,
            ast_conn,
            "Property",
            row["property_id"],
            include_raw=include_raw,
        )
        prop["schema"] = _optional_detail(
            compiler_conn,
            ast_conn,
            "SchemaComponent",
            row.get("target_component_id") or "",
            include_raw=include_raw,
        )
        prop["item_schema"] = _optional_detail(
            compiler_conn,
            ast_conn,
            "SchemaComponent",
            row.get("item_component_id") or "",
            include_raw=include_raw,
        )
        properties.append(prop)

    composition = [
        {
            "kind": row["kind"],
            "schema": _detail(
                compiler_conn,
                ast_conn,
                "SchemaComponent",
                row["component_id"],
                include_raw=include_raw,
            ),
        }
        for row in _rows(
            compiler_conn,
            """
            MATCH (:SchemaComponent {component_id: $component_id})-[edge:COMPOSED_OF]->(target:SchemaComponent)
            RETURN edge.kind AS kind,
                   target.component_id AS component_id
            ORDER BY edge.kind, target.component_id
            """,
            {"component_id": component_id},
        )
    ]

    value_schemas = [
        _detail(
            compiler_conn,
            ast_conn,
            "SchemaComponent",
            row["component_id"],
            include_raw=include_raw,
        )
        for row in _rows(
            compiler_conn,
            """
            MATCH (:SchemaComponent {component_id: $component_id})-[:HAS_VALUE_SCHEMA]->(target:SchemaComponent)
            RETURN target.component_id AS component_id
            ORDER BY target.component_id
            """,
            {"component_id": component_id},
        )
    ]

    references = [
        {
            "via": row["via"],
            "schema": _detail(
                compiler_conn,
                ast_conn,
                "SchemaComponent",
                row["component_id"],
                include_raw=include_raw,
            ),
        }
        for row in _rows(
            compiler_conn,
            """
            MATCH (:SchemaComponent {component_id: $component_id})-[edge:REFERENCES]->(target:SchemaComponent)
            RETURN edge.via AS via,
                   target.component_id AS component_id
            ORDER BY edge.via, target.component_id
            """,
            {"component_id": component_id},
        )
    ]

    return {
        "schema": schema,
        "properties": properties,
        "composition": composition,
        "value_schemas": value_schemas,
        "references": references,
    }


def _detail(
    compiler_conn,
    ast_conn,
    table_name: str,
    row_id: str,
    *,
    include_raw: bool,
) -> dict[str, Any]:
    detail = fetch_projection_detail(
        compiler_conn=compiler_conn,
        ast_conn=ast_conn,
        table_name=table_name,
        row_id=row_id,
    )
    if not include_raw:
        detail = dict(detail)
        detail.pop("raw_openapi", None)
        ast_node = detail.get("ast_node")
        if isinstance(ast_node, dict):
            ast_node = dict(ast_node)
            ast_node.pop("rawJson", None)
            ast_node.pop("scalarJson", None)
            detail["ast_node"] = ast_node
    return detail


def _optional_detail(
    compiler_conn,
    ast_conn,
    table_name: str,
    row_id: str,
    *,
    include_raw: bool,
) -> dict[str, Any] | None:
    if not row_id:
        return None
    try:
        return _detail(
            compiler_conn,
            ast_conn,
            table_name,
            row_id,
            include_raw=include_raw,
        )
    except ProjectionRowNotFoundError:
        return None


def _rows(conn, cypher: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    return list(conn.execute(cypher, parameters=params).rows_as_dict())
