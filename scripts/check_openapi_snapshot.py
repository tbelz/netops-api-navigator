#!/usr/bin/env python3
"""Decide whether validated API files differ from the last OpenAPI release.

Report timestamps/provenance do not trigger a release. A comparison error must
stop the publishing job rather than silently create another snapshot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Callable

FILENAMES = (
    "central-openapi.json",
    "central-monitoring-openapi.json",
    "central-config-openapi.json",
)


def gh_json(*args: str):
    result = subprocess.run(["gh", *args], check=True, stdout=subprocess.PIPE, text=True)
    return json.loads(result.stdout)


def report_checksums(report: dict) -> dict[str, str]:
    checksums = {}
    for name in FILENAMES:
        value = report["outputs"][name]["sha256"]
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError(f"Invalid checksum for {name}")
        checksums[name] = value
    return checksums


def snapshot_changed(
    directory: Path, repository: str, *, fetch: Callable = gh_json
) -> bool:
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
        raise ValueError("Repository must be owner/name")
    current = report_checksums(json.loads((directory / "export-report.json").read_text()))
    for name, expected in current.items():
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Current export checksum mismatch: {name}")

    # Paginate: software and knowledge releases can push the last OpenAPI
    # snapshot beyond the first page, particularly after deduplication.
    page = 1
    while True:
        releases = fetch("api", f"repos/{repository}/releases?per_page=100&page={page}")
        if not isinstance(releases, list):
            raise ValueError("Expected a GitHub release list")
        previous = next(
            (
                release for release in releases
                if release["tag_name"].startswith("openapi-")
                and not release["draft"] and not release["prerelease"]
            ),
            None,
        )
        if previous is not None:
            names = {asset["name"] for asset in previous["assets"]}
            if not set((*FILENAMES, "export-report.json")).issubset(names):
                raise ValueError("Previous OpenAPI snapshot has an incomplete asset set")
            report = fetch(
                "release", "download", previous["tag_name"], "--repo", repository,
                "--pattern", "export-report.json", "--output", "-",
            )
            return current != report_checksums(report)
        if len(releases) < 100:
            return True
        page += 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args()
    changed = snapshot_changed(args.directory, args.repository)
    # Write the decision only after a complete, successful comparison.
    with args.github_output.open("a", encoding="utf-8") as handle:
        handle.write(f"changed={str(changed).lower()}\n")
    print("API files changed; creating snapshot" if changed else "API files unchanged; Pages only")


if __name__ == "__main__":
    main()
