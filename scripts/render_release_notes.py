#!/usr/bin/env python3
"""Validate curated release notes and render a repeatable GitHub release body."""

from __future__ import annotations

import argparse
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def render_notes(*, version: str, tag: str, repository: str, notes_dir: Path) -> str:
    if tag != f"v{version}":
        raise ValueError(f"Tag {tag!r} must match package version v{version}")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Software releases require a major.minor.patch version")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
        raise ValueError("Repository must be owner/name")
    notes = (notes_dir / f"{version}.md").read_text(encoding="utf-8").strip()
    for heading in ("Highlights", "Upgrade notes"):
        match = re.search(rf"^## {heading}\n(.+?)(?=^## |\Z)", notes, re.M | re.S)
        if not match or not match.group(1).strip():
            raise ValueError(f"Release notes need a nonempty '## {heading}' section")

    def key(value: str) -> tuple[int, ...]:
        return tuple(map(int, value.split(".")))

    versions = [
        path.stem for path in notes_dir.glob("*.md")
        if re.fullmatch(r"\d+\.\d+\.\d+", path.stem) and key(path.stem) < key(version)
    ]
    base = f"https://github.com/{repository}"
    changelog = (
        f"{base}/compare/v{max(versions, key=key)}...{tag}"
        if versions else f"{base}/commits/{tag}"
    )
    assets = f"{base}/releases/download/{tag}"
    workshop = f"{assets}/netops-api-navigator-workshop-{tag}.zip"
    wheel = f"{assets}/netops_api_navigator-{version}-py3-none-any.whl"
    return f"""{notes}

## Install

[PyPI {version}](https://pypi.org/project/netops-api-navigator/{version}/)

```text
uv tool install --python 3.12 --no-build netops-api-navigator=={version}
```

Read the upgrade notes above before opening an existing graph, including with `doctor`.

## Downloads

| Download | Use |
| --- | --- |
| [Workshop ZIP]({workshop}) | VS Code starter with supported-platform dependency wheels |
| [Wheel]({wheel}) | Manual Python installation; normal installation uses PyPI |
| [Source archive]({assets}/netops_api_navigator-{version}.tar.gz) | Python source distribution |
| [constraints.txt]({assets}/constraints.txt) | Locked runtime dependencies |
| [SHA256SUMS]({assets}/SHA256SUMS) | Checksums for all release downloads |

[Full changelog]({changelog}) · [Documentation]({base}/blob/{tag}/README.md)

Automated `knowledge-db-*` and `openapi-*` releases are independent data snapshots.
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag")
    parser.add_argument("--version", help="Historical version; CI uses the package version")
    parser.add_argument("--repository", default="tbelz/netops-api-navigator")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    version = args.version or project["version"]
    body = render_notes(
        version=version,
        tag=args.tag or f"v{version}",
        repository=args.repository,
        notes_dir=ROOT / "docs/releases",
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(body, encoding="utf-8")


if __name__ == "__main__":
    main()
