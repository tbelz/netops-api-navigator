"""Persistence helpers for the Task 2 OpenAPI AST graph."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import real_ladybug as lb

from .ast_builder import AstGraph
from .ast_schema import apply_ast_schema

_DEFAULT_GRAPH_BATCH_SIZE = 32

_SPEC_SCHEMA = pa.schema([
    ("spec_id", pa.string()),
    ("source", pa.string()),
    ("title", pa.string()),
    ("openapi_version", pa.string()),
    ("content_hash", pa.string()),
    ("ingestion_status", pa.string()),
    ("ingestion_error_type", pa.string()),
    ("ingestion_error", pa.string()),
])

_NODE_SCHEMA = pa.schema([
    ("node_id", pa.string()),
    ("spec_id", pa.string()),
    ("kind", pa.string()),
    ("jsonPointer", pa.string()),
    ("name", pa.string()),
    ("key", pa.string()),
    ("index", pa.int64()),
    ("valueType", pa.string()),
    ("rawJson", pa.string()),
    ("scalarJson", pa.string()),
    ("isExtension", pa.bool_()),
])

_HAS_ROOT_SCHEMA = pa.schema([
    ("a", pa.string()),
    ("b", pa.string()),
])

_CHILD_SCHEMA = pa.schema([
    ("a", pa.string()),
    ("b", pa.string()),
    ("role", pa.string()),
    ("key", pa.string()),
    ("index", pa.int64()),
])

_REF_SCHEMA = pa.schema([
    ("a", pa.string()),
    ("b", pa.string()),
    ("ref", pa.string()),
])


def write_ast_graph(conn, graph: AstGraph) -> None:
    """Bulk-load one L1 AST graph into an open LadybugDB connection."""
    write_ast_graphs(conn, [graph])


def write_ast_graphs(conn, graphs: list[AstGraph]) -> None:
    """Bulk-load a bounded batch of L1 AST graphs into one connection."""
    _copy(conn, "OasSpec", [graph.spec_row for graph in graphs], _SPEC_SCHEMA)
    _copy(
        conn,
        "OasAstNode",
        [
            {
                "node_id": n.node_id,
                "spec_id": n.spec_id,
                "kind": n.kind,
                "jsonPointer": n.json_pointer,
                "name": n.name,
                "key": n.key,
                "index": n.index,
                "valueType": n.value_type,
                "rawJson": n.raw_json,
                "scalarJson": n.scalar_json,
                "isExtension": n.is_extension,
            }
            for graph in graphs
            for n in graph.nodes
        ],
        _NODE_SCHEMA,
    )
    _copy(
        conn,
        "HAS_AST_ROOT",
        [{"a": graph.spec_id, "b": graph.root_node_id} for graph in graphs],
        _HAS_ROOT_SCHEMA,
    )
    _copy(
        conn,
        "AST_CHILD",
        [
            {
                "a": e.parent_id,
                "b": e.child_id,
                "role": e.role,
                "key": e.key,
                "index": e.index,
            }
            for graph in graphs
            for e in graph.child_edges
        ],
        _CHILD_SCHEMA,
    )
    _copy(
        conn,
        "AST_REF_TARGET",
        [
            {"a": e.ref_node_id, "b": e.target_node_id, "ref": e.ref}
            for graph in graphs
            for e in graph.ref_edges
        ],
        _REF_SCHEMA,
    )


def build_ast_database(
    db_path: Path,
    graphs: list[AstGraph],
    *,
    buffer_pool_size: int | None = None,
    graph_batch_size: int = _DEFAULT_GRAPH_BATCH_SIZE,
) -> None:
    """Create an AST-only LadybugDB at ``db_path`` and write ``graphs``."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_kwargs = {}
    if buffer_pool_size is not None:
        db_kwargs["buffer_pool_size"] = buffer_pool_size
    db = lb.Database(str(db_path), **db_kwargs)
    try:
        conn = lb.Connection(db)
        apply_ast_schema(conn)
        for start in range(0, len(graphs), graph_batch_size):
            write_ast_graphs(conn, graphs[start:start + graph_batch_size])
    finally:
        db.close()


def _copy(conn, table: str, rows: list[dict], schema: pa.Schema) -> None:
    if not rows:
        return
    columns: dict[str, list] = {field.name: [] for field in schema}
    for row in rows:
        for field in schema:
            columns[field.name].append(row.get(field.name))
    conn.execute(f"COPY {table} FROM $df", parameters={"df": pa.table(columns, schema=schema)})
