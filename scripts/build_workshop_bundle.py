#!/usr/bin/env python3
"""Build the versioned, secret-free VS Code workshop starter archive."""

from __future__ import annotations

import argparse
import shutil
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXED_ZIP_TIME = (2026, 1, 1, 0, 0, 0)


def _version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def build_bundle(constraints: Path, output_dir: Path) -> Path:
    version = _version()
    source = ROOT / "workshop"
    mcp_config = source / ".vscode" / "mcp.json"
    config_text = mcp_config.read_text(encoding="utf-8")
    if f"hpe-networking-central-mcp=={version}" not in config_text:
        raise ValueError(
            f"Workshop MCP config does not pin project version {version}; update {mcp_config}"
        )
    if not constraints.is_file():
        raise FileNotFoundError(f"Constraints file not found: {constraints}")

    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / f"central-mcp-workshop-v{version}.zip"
    with tempfile.TemporaryDirectory() as temp:
        stage = Path(temp) / f"central-mcp-workshop-v{version}"
        shutil.copytree(source, stage)
        shutil.copy2(constraints, stage / "constraints.txt")
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(stage.rglob("*")):
                if not path.is_file():
                    continue
                relative = path.relative_to(stage.parent).as_posix()
                info = zipfile.ZipInfo(relative, FIXED_ZIP_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                archive.writestr(info, path.read_bytes())
    return destination


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--constraints", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    print(build_bundle(args.constraints, args.output_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
