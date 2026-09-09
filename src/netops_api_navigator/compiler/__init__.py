"""Compiler pipeline for the OAS-to-graph translation (ADR 011).

This package implements the four-task compiler that supersedes the
hand-curated graph populator. Tasks are introduced incrementally:

* Task 1: :mod:`.frontend` - resolved ingestion via ``prance``.
* Task 2: :mod:`.ast_builder` / :mod:`.ast_writer` - lossless AST graph.
* Task 3: :mod:`.semantic_builder` / :mod:`.semantic_writer` - semantic overlay.
* Task 4: agent projection + MCP tool surface (not yet implemented).
"""
