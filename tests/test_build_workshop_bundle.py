from __future__ import annotations

import zipfile

import pytest

from scripts.build_workshop_bundle import build_bundle

pytestmark = pytest.mark.unit


def test_bundle_contains_rendered_config_project_wheel_and_wheelhouse(tmp_path):
    constraints = tmp_path / "constraints.txt"
    constraints.write_text("httpx==0.28.1\n", encoding="utf-8")
    wheel = tmp_path / "netops_api_navigator-0.3.0-py3-none-any.whl"
    wheel.write_bytes(b"project-wheel")
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    (wheelhouse / "httpx-0.28.1-py3-none-any.whl").write_bytes(b"dependency-wheel")

    archive_path = build_bundle(constraints, wheel, wheelhouse, tmp_path / "output")

    root = "netops-api-navigator-workshop-v0.3.0"
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        assert names == {
            f"{root}/.vscode/mcp.json",
            f"{root}/README.md",
            f"{root}/constraints.txt",
            f"{root}/{wheel.name}",
            f"{root}/wheelhouse/httpx-0.28.1-py3-none-any.whl",
        }
        config = archive.read(f"{root}/.vscode/mcp.json").decode()

    assert "__PROJECT_WHEEL__" not in config
    assert f"${{workspaceFolder}}/{wheel.name}" in config
    assert '"--no-index"' in config
    assert '"netops-api-navigator"' in config


def test_bundle_rejects_a_wheel_for_another_version(tmp_path):
    constraints = tmp_path / "constraints.txt"
    constraints.write_text("httpx==0.28.1\n", encoding="utf-8")
    wheel = tmp_path / "netops_api_navigator-9.9.9-py3-none-any.whl"
    wheel.write_bytes(b"project-wheel")
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    (wheelhouse / "httpx-0.28.1-py3-none-any.whl").write_bytes(b"dependency-wheel")

    with pytest.raises(ValueError, match="must match version 0.3.0"):
        build_bundle(constraints, wheel, wheelhouse, tmp_path / "output")
