#!/usr/bin/env python3
"""Create validated Central OpenAPI files from one completed scrape."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from netops_api_navigator.openapi_export import build_exports, read_json


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--diagnostics", type=Path, required=True)
    parser.add_argument("--workflow-url", default="")
    parser.add_argument("--source-commit", default="")
    args = parser.parse_args()
    try:
        if args.output_dir.exists():
            raise ValueError("Output directory must be new; never overwrite a published export")
        files, report = build_exports(
            args.cache_dir,
            read_json(args.manifest),
            workflow_url=args.workflow_url,
            source_commit=args.source_commit,
        )
        args.output_dir.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".openapi-", dir=args.output_dir.parent))
        try:
            for name, data in files.items():
                (staging / name).write_bytes(data)
            os.rename(staging, args.output_dir)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        args.diagnostics.parent.mkdir(parents=True, exist_ok=True)
        args.diagnostics.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report["outputs"], indent=2))
        return 0
    except Exception as exc:
        report = {
            "status": "failed",
            "error_type": type(exc).__name__,
            "error": str(exc)[:5000],
            "workflow_url": args.workflow_url,
        }
        args.diagnostics.parent.mkdir(parents=True, exist_ok=True)
        args.diagnostics.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"OpenAPI export failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
