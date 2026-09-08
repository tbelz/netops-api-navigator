"""Persistence helpers for the Task 3 semantic overlay graph."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import real_ladybug as lb

from .semantic_builder import SemanticGraph
from .semantic_schema import apply_semantic_schema

_DEFAULT_GRAPH_BATCH_SIZE = 32

_SEMANTIC_NODE_SCHEMA = pa.schema([
    ("semantic_id", pa.string()),
    ("spec_id", pa.string()),
    ("kind", pa.string()),
    ("name", pa.string()),
    ("ast_node_id", pa.string()),
    ("jsonPointer", pa.string()),
    ("stableKey", pa.string()),
    ("summaryJson", pa.string()),
])

_SEMANTIC_DERIVED_SCHEMA = pa.schema([
    ("a", pa.string()),
    ("b", pa.string()),
    ("role", pa.string()),
])

_SEMANTIC_EDGE_SCHEMA = pa.schema([
    ("a", pa.string()),
    ("b", pa.string()),
    ("kind", pa.string()),
    ("ruleId", pa.string()),
    ("evidenceJson", pa.string()),
])

_COPY_TABLES = {"SemanticNode", "SEMANTIC_DERIVED_FROM", "SEMANTIC_EDGE"}


def write_semantic_graph(conn, graph: SemanticGraph) -> None:
    """Bulk-load one L2 semantic overlay graph into an open LadybugDB connection."""
    write_semantic_graphs(conn, [graph])


def write_semantic_graphs(conn, graphs: list[SemanticGraph]) -> None:
    """Bulk-load a bounded batch of L2 semantic graphs into one connection."""
    _copy(
        conn,
        "SemanticNode",
        [
            {
                "semantic_id": n.semantic_id,
                "spec_id": n.spec_id,
                "kind": n.kind,
                "name": n.name,
                "ast_node_id": n.ast_node_id,
                "jsonPointer": n.json_pointer,
                "stableKey": n.stable_key,
                "summaryJson": n.summary_json,
            }
            for graph in graphs
            for n in graph.nodes
        ],
        _SEMANTIC_NODE_SCHEMA,
    )
    _copy(
        conn,
        "SEMANTIC_DERIVED_FROM",
        [
            {"a": e.semantic_id, "b": e.ast_node_id, "role": e.role}
            for graph in graphs
            for e in graph.derived_edges
        ],
        _SEMANTIC_DERIVED_SCHEMA,
    )
    _copy(
        conn,
        "SEMANTIC_EDGE",
        [
            {
                "a": e.source_id,
                "b": e.target_id,
                "kind": e.kind,
                "ruleId": e.rule_id,
                "evidenceJson": e.evidence_json,
            }
            for graph in graphs
            for e in graph.edges
        ],
        _SEMANTIC_EDGE_SCHEMA,
    )


def write_semantic_database(
    db_path: Path,
    graphs: list[SemanticGraph],
    *,
    buffer_pool_size: int | None = None,
    graph_batch_size: int = _DEFAULT_GRAPH_BATCH_SIZE,
) -> None:
    """Open an existing AST DB, apply L2 schema, and write ``graphs``."""
    db_kwargs = {}
    if buffer_pool_size is not None:
        db_kwargs["buffer_pool_size"] = buffer_pool_size
    db = lb.Database(str(db_path), **db_kwargs)
    try:
        conn = lb.Connection(db)
        apply_semantic_schema(conn)
        for start in range(0, len(graphs), graph_batch_size):
            write_semantic_graphs(conn, graphs[start:start + graph_batch_size])
    finally:
        db.close()


def _copy(conn, table: str, rows: list[dict], schema: pa.Schema) -> None:
    if not rows:
        return
    if table not in _COPY_TABLES:
        raise ValueError(f"Unexpected semantic COPY table: {table}")
    columns: dict[str, list] = {field.name: [] for field in schema}
    for row in rows:
        for field in schema:
            columns[field.name].append(row.get(field.name))
    conn.execute(f"COPY {table} FROM $df", parameters={"df": pa.table(columns, schema=schema)})
