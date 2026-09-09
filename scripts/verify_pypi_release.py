#!/usr/bin/env python3
"""Verify one published release against locally built distributions."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

JsonFetcher = Callable[[str], dict[str, Any]]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fetch_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "netops-api-navigator-release-check"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def verify_release(
    *,
    index_url: str,
    project: str,
    version: str,
    distributions: Path,
    attempts: int = 12,
    delay_seconds: float = 10,
    fetch_json: JsonFetcher = _fetch_json,
) -> str:
    """Return the verified wheel URL after the index matches local artifacts."""
    local_files = {
        path.name: path
        for path in distributions.iterdir()
        if path.is_file() and (path.suffix == ".whl" or path.name.endswith(".tar.gz"))
    }
    if len(local_files) != 2 or not any(name.endswith(".whl") for name in local_files):
        raise ValueError(
            f"Expected exactly one wheel and one source distribution in {distributions}"
        )

    api_url = f"{index_url.rstrip('/')}/pypi/{project}/{version}/json"
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            payload = fetch_json(api_url)
            published_files = {
                item["filename"]: item
                for item in payload.get("urls", [])
                if isinstance(item, dict) and item.get("filename")
            }
            if set(published_files) != set(local_files):
                raise ValueError(
                    "Published filenames do not match local distributions: "
                    f"published={sorted(published_files)}, local={sorted(local_files)}"
                )

            for filename, path in local_files.items():
                published_digest = published_files[filename].get("digests", {}).get("sha256")
                local_digest = _sha256(path)
                if published_digest != local_digest:
                    raise ValueError(
                        f"SHA-256 mismatch for {filename}: "
                        f"published={published_digest}, local={local_digest}"
                    )

            wheels = [
                item["url"]
                for filename, item in published_files.items()
                if filename.endswith(".whl") and item.get("url")
            ]
            if len(wheels) != 1:
                raise ValueError(f"Expected one published wheel URL, found {len(wheels)}")
            return wheels[0]
        except (OSError, ValueError, KeyError, TypeError, urllib.error.HTTPError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(delay_seconds)

    raise RuntimeError(
        f"Release {project}=={version} did not verify after {attempts} attempts"
    ) from last_error


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index-url", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--distributions", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--attempts", type=int, default=12)
    parser.add_argument("--delay-seconds", type=float, default=10)
    args = parser.parse_args()

    wheel_url = verify_release(
        index_url=args.index_url,
        project=args.project,
        version=args.version,
        distributions=args.distributions,
        attempts=args.attempts,
        delay_seconds=args.delay_seconds,
    )
    print(f"Verified {args.project}=={args.version}: {wheel_url}")
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write(f"wheel_url={wheel_url}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
