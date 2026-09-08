"""LadybugDB schema for the Task 2 lossless OpenAPI AST graph."""

from __future__ import annotations

AST_NODE_TABLES: list[str] = [
    """
    CREATE NODE TABLE IF NOT EXISTS OasSpec (
        spec_id         STRING,
        source          STRING,
        title           STRING,
        openapi_version STRING,
        content_hash    STRING,
        ingestion_status     STRING,
        ingestion_error_type STRING,
        ingestion_error      STRING,
        PRIMARY KEY (spec_id)
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS OasAstNode (
        node_id     STRING,
        spec_id     STRING,
        kind        STRING,
        jsonPointer STRING,
        name        STRING,
        key         STRING,
        index       INT64,
        valueType   STRING,
        rawJson     STRING,
        scalarJson  STRING,
        isExtension BOOLEAN,
        PRIMARY KEY (node_id)
    )
    """,
]

AST_REL_TABLES: list[str] = [
    "CREATE REL TABLE IF NOT EXISTS HAS_AST_ROOT (FROM OasSpec TO OasAstNode)",
    """
    CREATE REL TABLE IF NOT EXISTS AST_CHILD (
        FROM OasAstNode TO OasAstNode,
        role  STRING,
        key   STRING,
        index INT64
    )
    """,
    """
    CREATE REL TABLE IF NOT EXISTS AST_REF_TARGET (
        FROM OasAstNode TO OasAstNode,
        ref STRING
    )
    """,
]


def apply_ast_schema(conn) -> None:
    """Create every L1 AST table in ``conn``."""
    for ddl in AST_NODE_TABLES + AST_REL_TABLES:
        conn.execute(ddl.strip())
