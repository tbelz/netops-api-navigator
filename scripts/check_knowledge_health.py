#!/usr/bin/env python3
"""Validate release completeness, retaining optional-source warnings for alerts."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def assess(manifest: dict, previous: dict | None = None) -> tuple[list[str], list[str]]:
    errors, warnings = [], []
    endpoints = manifest.get("endpoint_count", 0)
    if endpoints <= 0:
        errors.append("Knowledge graph has no API endpoints")
    if previous and endpoints < previous.get("endpoint_count", 0) * 0.75:
        errors.append("API endpoint coverage dropped by more than 25%")
    health = manifest.get("sync_health", {})
    for provider in ("central", "greenlake"):
        if health.get(provider, {}).get("status") != "ok":
            errors.append(f"{provider}: API source is missing, degraded or errored")
    for provider, report in health.items():
        if provider not in ("central", "greenlake") and report.get("status") != "ok":
            warnings.append(f"{provider}: {report.get('status', 'unknown')} ({report.get('reason', 'see manifest')})")
    ast = manifest.get("ast", {})
    if not ast.get("enabled") or not ast.get("raw_spec_count"):
        errors.append("Compiler AST is missing or empty")
    elif ast.get("spec_count") != ast["raw_spec_count"] or ast.get("degraded", {}).get("failed_count", 0):
        errors.append("Not every downloaded API specification was compiled into the AST")
    projection = manifest.get("compiler_projection", {})
    if not projection.get("enabled") or projection.get("node_kind_counts", {}).get("ApiEndpoint") != endpoints:
        errors.append("Compiler graph does not contain every API endpoint")
    if manifest.get("compiler_cutover_gates", {}).get("passed") is not True:
        errors.append("Compiler invariant/traversal release gates did not pass")
    parity = manifest.get("projection_parity", {})
    if parity.get("all_legacy_effectively_covered") is not True:
        warnings.append("Compiler projection differs from the default legacy graph; default cutover is not ready")
    return errors, warnings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--previous", type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    previous = json.loads(args.previous.read_text()) if args.previous and args.previous.exists() else None
    errors, warnings = assess(manifest, previous)
    for message in warnings:
        print(f"::warning::{message}")
    for message in errors:
        print(f"::error::{message}")
    print(json.dumps({"endpoint_count": manifest.get("endpoint_count"), "errors": errors, "warnings": warnings}))
    raise SystemExit(bool(errors))


if __name__ == "__main__":
    main()
