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


def build_bundle(
    constraints: Path,
    wheel: Path,
    wheelhouse: Path,
    output_dir: Path,
) -> Path:
    version = _version()
    source = ROOT / "workshop"
    mcp_template = source / ".vscode" / "mcp.json.template"
    if not constraints.is_file():
        raise FileNotFoundError(f"Constraints file not found: {constraints}")
    if not wheel.is_file():
        raise FileNotFoundError(f"Project wheel not found: {wheel}")
    expected_wheel_prefix = f"netops_api_navigator-{version}-"
    if not wheel.name.startswith(expected_wheel_prefix) or wheel.suffix != ".whl":
        raise ValueError(
            f"Project wheel must match version {version} and start with "
            f"{expected_wheel_prefix!r}: {wheel.name}"
        )
    if not wheelhouse.is_dir() or not any(wheelhouse.glob("*.whl")):
        raise FileNotFoundError(f"Dependency wheelhouse is empty or missing: {wheelhouse}")

    config_text = mcp_template.read_text(encoding="utf-8")
    placeholder = "__PROJECT_WHEEL__"
    if config_text.count(placeholder) != 1:
        raise ValueError(f"Workshop MCP template must contain exactly one {placeholder}")
    rendered_config = config_text.replace(placeholder, wheel.name)

    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / f"netops-api-navigator-workshop-v{version}.zip"
    with tempfile.TemporaryDirectory() as temp:
        stage = Path(temp) / f"netops-api-navigator-workshop-v{version}"
        (stage / ".vscode").mkdir(parents=True)
        shutil.copy2(source / "README.md", stage / "README.md")
        (stage / ".vscode" / "mcp.json").write_text(rendered_config, encoding="utf-8")
        shutil.copy2(constraints, stage / "constraints.txt")
        shutil.copy2(wheel, stage / wheel.name)
        shutil.copytree(wheelhouse, stage / "wheelhouse")
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
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    print(build_bundle(args.constraints, args.wheel, args.wheelhouse, args.output_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
