#!/usr/bin/env python3
"""Verify compiler projection traversal against persisted build artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from netops_api_navigator.compiler.traversal_report import (  # noqa: E402
    load_compiler_traversal_report,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run compiler endpoint/schema traversal readers against "
            "knowledge_db_compiler and knowledge_db_ast artifacts."
        )
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("build"),
        help="Build output directory containing knowledge_db_compiler and knowledge_db_ast.",
    )
    parser.add_argument(
        "--compiler-db",
        type=Path,
        default=None,
        help="Override path to knowledge_db_compiler.",
    )
    parser.add_argument(
        "--ast-db",
        type=Path,
        default=None,
        help="Override path to knowledge_db_ast.",
    )
    parser.add_argument(
        "--endpoint-limit",
        type=int,
        default=100,
        help="Number of endpoints to sample for traversal verification.",
    )
    parser.add_argument(
        "--schema-limit",
        type=int,
        default=100,
        help="Number of schemas to sample for traversal verification.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Return non-zero when traversal failures are found.",
    )
    args = parser.parse_args()

    output_dir = args.output_dir.resolve()
    compiler_db = (args.compiler_db or output_dir / "knowledge_db_compiler").resolve()
    ast_db = (args.ast_db or output_dir / "knowledge_db_ast").resolve()
    if not compiler_db.exists():
        print(f"ERROR: compiler DB not found: {compiler_db}", file=sys.stderr)
        return 1
    if not ast_db.exists():
        print(f"ERROR: AST DB not found: {ast_db}", file=sys.stderr)
        return 1

    report = load_compiler_traversal_report(
        compiler_db_path=compiler_db,
        ast_db_path=ast_db,
        endpoint_limit=args.endpoint_limit,
        schema_limit=args.schema_limit,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    if args.strict and report["failure_count"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
