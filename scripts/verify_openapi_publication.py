#!/usr/bin/env python3
"""Verify a Pages publication against the validated local export report."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import urllib.request
from pathlib import Path


def verify(base_url: str, report: dict) -> None:
    expected_files = {**report["outputs"], "export-report.json": None}
    for name, expected in expected_files.items():
        request = urllib.request.Request(
            base_url.rstrip("/") + "/" + name,
            headers={"Origin": "https://editor.swagger.io", "Cache-Control": "no-cache"},
        )
        with urllib.request.urlopen(request, timeout=45) as response:
            if response.headers.get_content_type() != "application/json":
                raise ValueError(f"{name}: not served as JSON")
            if response.headers.get("Access-Control-Allow-Origin") not in {
                "*",
                "https://editor.swagger.io",
            }:
                raise ValueError(f"{name}: browser cross-origin access is not enabled")
            data = response.read()
        if expected is None:
            if json.loads(data) != report:
                raise ValueError("Public report does not match this scrape")
            continue
        if hashlib.sha256(data).hexdigest() != expected["sha256"]:
            raise ValueError(f"{name}: published checksum does not match this scrape")
        if json.loads(data).get("openapi") != "3.1.0":
            raise ValueError(f"{name}: expected OpenAPI 3.1.0")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--attempts", type=int, default=12)
    args = parser.parse_args()
    report = json.loads(args.report.read_text())
    for attempt in range(args.attempts):
        try:
            verify(args.base_url, report)
            print("All public OpenAPI exports match the validated snapshot; CORS is enabled.")
            return 0
        except Exception as exc:
            print(f"Publication check {attempt + 1}/{args.attempts}: {exc}", flush=True)
            if attempt + 1 < args.attempts:
                time.sleep(10)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
