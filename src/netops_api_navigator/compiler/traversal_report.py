"""Corpus-level verification for compiler traversal artifacts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import real_ladybug as lb

from .detail_reader import ProjectionDetailError
from .traversal_reader import fetch_endpoint_context, fetch_schema_context

_DEFAULT_BUFFER_POOL_SIZE = 256 * 1024 * 1024


def load_compiler_traversal_report(
    *,
    compiler_db_path: Path,
    ast_db_path: Path,
    endpoint_limit: int = 100,
    schema_limit: int = 100,
    buffer_pool_size: int = _DEFAULT_BUFFER_POOL_SIZE,
) -> dict[str, Any]:
    """Open compiler artifacts and compute a traversal verification report."""
    compiler_db = None
    ast_db = None
    try:
        compiler_db = lb.Database(str(compiler_db_path), buffer_pool_size=buffer_pool_size)
        ast_db = lb.Database(str(ast_db_path), buffer_pool_size=buffer_pool_size)
        return compute_compiler_traversal_report(
            compiler_conn=lb.Connection(compiler_db),
            ast_conn=lb.Connection(ast_db),
            endpoint_limit=endpoint_limit,
            schema_limit=schema_limit,
        )
    finally:
        if ast_db is not None:
            ast_db.close()
        if compiler_db is not None:
            compiler_db.close()


def compute_compiler_traversal_report(
    *,
    compiler_conn,
    ast_conn,
    endpoint_limit: int = 100,
    schema_limit: int = 100,
) -> dict[str, Any]:
    """Sample compiler projection rows and verify traversal readers over them."""
    endpoint_limit = max(0, endpoint_limit)
    schema_limit = max(0, schema_limit)
    totals = {
        "endpoints": _count(compiler_conn, "MATCH (n:ApiEndpoint) RETURN COUNT(n) AS n"),
        "schemas": _count(compiler_conn, "MATCH (n:SchemaComponent) RETURN COUNT(n) AS n"),
        "parameters": _count(compiler_conn, "MATCH (n:Parameter) RETURN COUNT(n) AS n"),
        "request_bodies": _count(compiler_conn, "MATCH (n:RequestBody) RETURN COUNT(n) AS n"),
        "responses": _count(compiler_conn, "MATCH (n:Response) RETURN COUNT(n) AS n"),
        "properties_with_constraints": _count(
            compiler_conn,
            """
            MATCH (n:Property)
            WHERE n.constraintsJson <> '{}'
            RETURN COUNT(n) AS n
            """,
        ),
        "item_schema_edges": _count(
            compiler_conn,
            "MATCH (:Property)-[edge:HAS_ITEM_SCHEMA]->(:SchemaComponent) RETURN COUNT(edge) AS n",
        ),
        "yang_paths": _count(compiler_conn, "MATCH (n:YangPath) RETURN COUNT(n) AS n"),
        "cli_commands": _count(compiler_conn, "MATCH (n:CliCommand) RETURN COUNT(n) AS n"),
    }

    endpoint_sample = _endpoint_sample(compiler_conn, endpoint_limit)
    schema_sample = _schema_sample(compiler_conn, schema_limit)
    endpoint_metrics = _empty_endpoint_metrics()
    schema_metrics = _empty_schema_metrics()
    failures: list[dict[str, str]] = []

    for row in endpoint_sample:
        try:
            context = fetch_endpoint_context(
                compiler_conn=compiler_conn,
                ast_conn=ast_conn,
                method=row["method"],
                path=row["path"],
                include_raw=False,
            )
        except Exception as exc:  # noqa: BLE001 - report verification failures
            failures.append(
                _failure(
                    kind="endpoint",
                    identifier=row.get("endpoint_id", ""),
                    error=exc,
                )
            )
            continue
        _merge_endpoint_context(endpoint_metrics, context)

    for row in schema_sample:
        try:
            context = fetch_schema_context(
                compiler_conn=compiler_conn,
                ast_conn=ast_conn,
                component_id=row["component_id"],
                include_raw=False,
            )
        except Exception as exc:  # noqa: BLE001 - report verification failures
            failures.append(
                _failure(
                    kind="schema",
                    identifier=row.get("component_id", ""),
                    error=exc,
                )
            )
            continue
        _merge_schema_context(schema_metrics, context)

    return {
        "totals": totals,
        "sample": {
            "endpoint_limit": endpoint_limit,
            "schema_limit": schema_limit,
            "endpoint_count": len(endpoint_sample),
            "schema_count": len(schema_sample),
        },
        "endpoint_context": _finish_metrics(endpoint_metrics, len(endpoint_sample)),
        "schema_context": _finish_metrics(schema_metrics, len(schema_sample)),
        "failure_count": len(failures),
        "failure_samples": failures[:25],
        "status": "ok" if not failures else "failed",
    }


def _endpoint_sample(conn, limit: int) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    return list(
        conn.execute(
            """
            MATCH (endpoint:ApiEndpoint)
            RETURN endpoint.endpoint_id AS endpoint_id,
                   endpoint.method AS method,
                   endpoint.path AS path
            ORDER BY endpoint.endpoint_id
            LIMIT $limit
            """,
            parameters={"limit": limit},
        ).rows_as_dict()
    )


def _schema_sample(conn, limit: int) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    return list(
        conn.execute(
            """
            MATCH (schema:SchemaComponent)
            RETURN schema.component_id AS component_id
            ORDER BY schema.component_id
            LIMIT $limit
            """,
            parameters={"limit": limit},
        ).rows_as_dict()
    )


def _empty_endpoint_metrics() -> dict[str, int]:
    return {
        "ok": 0,
        "with_parameters": 0,
        "with_parameter_schema": 0,
        "with_request_bodies": 0,
        "with_request_schema": 0,
        "with_responses": 0,
        "with_response_schema": 0,
        "with_yang_paths": 0,
        "with_cli_commands": 0,
    }


def _empty_schema_metrics() -> dict[str, int]:
    return {
        "ok": 0,
        "with_properties": 0,
        "with_property_schema": 0,
        "with_item_schema": 0,
        "with_composition": 0,
        "with_value_schemas": 0,
        "with_references": 0,
    }


def _merge_endpoint_context(metrics: dict[str, int], context: dict[str, Any]) -> None:
    metrics["ok"] += 1
    parameters = context.get("parameters") or []
    request_bodies = context.get("request_bodies") or []
    responses = context.get("responses") or []
    if parameters:
        metrics["with_parameters"] += 1
    if any(param.get("schema") for param in parameters if isinstance(param, dict)):
        metrics["with_parameter_schema"] += 1
    if request_bodies:
        metrics["with_request_bodies"] += 1
    if any(body.get("schema") for body in request_bodies if isinstance(body, dict)):
        metrics["with_request_schema"] += 1
    if responses:
        metrics["with_responses"] += 1
    if any(response.get("schema") for response in responses if isinstance(response, dict)):
        metrics["with_response_schema"] += 1
    if context.get("yang_paths"):
        metrics["with_yang_paths"] += 1
    if context.get("cli_commands"):
        metrics["with_cli_commands"] += 1


def _merge_schema_context(metrics: dict[str, int], context: dict[str, Any]) -> None:
    metrics["ok"] += 1
    properties = context.get("properties") or []
    if properties:
        metrics["with_properties"] += 1
    if any(prop.get("schema") for prop in properties if isinstance(prop, dict)):
        metrics["with_property_schema"] += 1
    if any(prop.get("item_schema") for prop in properties if isinstance(prop, dict)):
        metrics["with_item_schema"] += 1
    if context.get("composition"):
        metrics["with_composition"] += 1
    if context.get("value_schemas"):
        metrics["with_value_schemas"] += 1
    if context.get("references"):
        metrics["with_references"] += 1


def _finish_metrics(metrics: dict[str, int], sample_count: int) -> dict[str, Any]:
    finished: dict[str, Any] = dict(metrics)
    denominator = max(1, sample_count)
    finished["ratios"] = {
        key: round(value / denominator, 4)
        for key, value in sorted(metrics.items())
        if key != "ok"
    }
    finished["ok_ratio"] = round(metrics.get("ok", 0) / denominator, 4)
    return finished


def _count(conn, cypher: str) -> int:
    rows = list(conn.execute(cypher).rows_as_dict())
    return int(rows[0]["n"]) if rows else 0


def _failure(*, kind: str, identifier: str, error: Exception) -> dict[str, Any]:
    return {
        "kind": kind,
        "identifier": identifier,
        "error_type": type(error).__name__,
        "error": str(error)[:500],
        "is_projection_detail_error": isinstance(error, ProjectionDetailError),
    }
