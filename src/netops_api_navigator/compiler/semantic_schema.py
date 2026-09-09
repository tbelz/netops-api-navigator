"""LadybugDB schema for the Task 3 semantic overlay graph."""

from __future__ import annotations

SEMANTIC_NODE_TABLES: list[str] = [
    """
    CREATE NODE TABLE IF NOT EXISTS SemanticNode (
        semantic_id STRING,
        spec_id     STRING,
        kind        STRING,
        name        STRING,
        ast_node_id STRING,
        jsonPointer STRING,
        stableKey   STRING,
        summaryJson STRING,
        PRIMARY KEY (semantic_id)
    )
    """,
]

SEMANTIC_REL_TABLES: list[str] = [
    """
    CREATE REL TABLE IF NOT EXISTS SEMANTIC_DERIVED_FROM (
        FROM SemanticNode TO OasAstNode,
        role STRING
    )
    """,
    """
    CREATE REL TABLE IF NOT EXISTS SEMANTIC_EDGE (
        FROM SemanticNode TO SemanticNode,
        kind         STRING,
        ruleId       STRING,
        evidenceJson STRING
    )
    """,
]


def apply_semantic_schema(conn) -> None:
    """Create every L2 semantic overlay table in ``conn``."""
    for ddl in SEMANTIC_NODE_TABLES + SEMANTIC_REL_TABLES:
        conn.execute(ddl.strip())
