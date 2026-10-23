"""Release content, repair behavior and snapshot publication boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts.check_openapi_snapshot import FILENAMES, snapshot_changed
from scripts.render_release_notes import render_notes

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def render(directory, version="0.3.2", tag="v0.3.2"):
    return render_notes(
        version=version, tag=tag, repository="tbelz/netops-api-navigator", notes_dir=directory
    )


def test_notes_preserve_storage_warning_and_version_specific_links():
    body = render(ROOT / "docs/releases")
    assert "storage format 40 to 41" in body
    assert "rollback requires restoring the entire pre-upgrade backup" in body
    assert "including startup or `doctor`" in body
    assert "https://pypi.org/project/netops-api-navigator/0.3.2/" in body
    assert "netops-api-navigator==0.3.2" in body
    assert "compare/v0.3.1...v0.3.2" in body
    assert "netops-api-navigator-workshop-v0.3.2.zip" in body
    assert body == render(ROOT / "docs/releases")


def test_notes_reject_missing_file_mismatched_version_and_empty_sections(tmp_path):
    with pytest.raises(FileNotFoundError):
        render(tmp_path)
    with pytest.raises(ValueError, match="must match"):
        render(tmp_path, tag="v0.3.1")
    notes = tmp_path / "0.3.2.md"
    notes.write_text("## Highlights\n\n## Upgrade notes\nNo migration.\n")
    with pytest.raises(ValueError, match="Highlights"):
        render(tmp_path)
    notes.write_text("## Highlights\nSomething new.\n")
    with pytest.raises(ValueError, match="Upgrade notes"):
        render(tmp_path)


def exports(directory):
    report = {"outputs": {}, "scraped_at": "today", "workflow_url": "new-run"}
    for name in FILENAMES:
        body = json.dumps({"name": name}).encode()
        (directory / name).write_bytes(body)
        report["outputs"][name] = {"sha256": hashlib.sha256(body).hexdigest()}
    (directory / "export-report.json").write_text(json.dumps(report))
    return report


def release(tag="openapi-previous"):
    return {
        "tag_name": tag, "draft": False, "prerelease": False,
        "assets": [{"name": name} for name in (*FILENAMES, "export-report.json")],
    }


def test_snapshot_ignores_report_freshness_and_finds_snapshot_beyond_first_page(tmp_path):
    previous = exports(tmp_path)
    previous.update(scraped_at="yesterday", workflow_url="old-run")
    calls = []

    def fetch(*args):
        calls.append(args)
        if args[0] == "release":
            return previous
        if args[1].endswith("page=1"):
            return [release("knowledge-db-daily")] * 100
        return [release()]

    assert not snapshot_changed(tmp_path, "tbelz/netops-api-navigator", fetch=fetch)
    assert "page=2" in calls[1][1]
    assert calls[2][2] == "openapi-previous"


def test_changed_api_or_first_publication_creates_snapshot(tmp_path):
    previous = exports(tmp_path)
    previous["outputs"][FILENAMES[0]]["sha256"] = "0" * 64
    assert snapshot_changed(
        tmp_path, "tbelz/netops-api-navigator",
        fetch=lambda *args: [release()] if args[0] == "api" else previous,
    )
    assert snapshot_changed(tmp_path, "tbelz/netops-api-navigator", fetch=lambda *args: [])


@pytest.mark.parametrize("failure", ["api", "download", "missing_asset", "checksum"])
def test_failed_comparison_cannot_be_treated_as_change(tmp_path, failure):
    previous = exports(tmp_path)
    prior_release = release()
    if failure == "missing_asset":
        prior_release["assets"].pop()
    if failure == "checksum":
        (tmp_path / FILENAMES[0]).write_bytes(b"corrupted")

    def fetch(*args):
        if (args[0] == "api" and failure == "api") or (
            args[0] == "release" and failure == "download"
        ):
            raise subprocess.CalledProcessError(1, "gh")
        return [prior_release] if args[0] == "api" else previous

    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        snapshot_changed(tmp_path, "tbelz/netops-api-navigator", fetch=fetch)


@pytest.mark.parametrize("existing", [False, True])
def test_release_create_and_repair_apply_curated_notes_and_exclude_metadata(tmp_path, existing):
    workflow = yaml.safe_load((ROOT / ".github/workflows/python-package.yml").read_text())
    jobs = workflow["jobs"]
    steps = jobs["release"]["steps"]
    script = next(s["run"] for s in steps if s.get("name") == "Create or repair GitHub release")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "release-notes.md").write_text("curated release notes")
    (dist / "project.whl").write_bytes(b"wheel")
    (dist / "SHA256SUMS").write_text("checksum")
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        f"#!{sys.executable}\n"
        "import json,os,sys\n"
        "with open(os.environ['GH_CALL_LOG'], 'a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n"
        "if sys.argv[1:3] == ['release','view']: sys.exit(int(os.environ['GH_VIEW_STATUS']))\n"
    )
    fake_gh.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    subprocess.run(
        ["bash", "-e", "-c", script], cwd=tmp_path, check=True,
        env={
            **os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "GITHUB_REF_NAME": "v0.3.2", "GITHUB_REPOSITORY": "tbelz/netops-api-navigator",
            "GH_CALL_LOG": str(log), "GH_VIEW_STATUS": "0" if existing else "1",
        },
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    final = calls[-1]
    assert final[:2] == ["release", "edit" if existing else "create"]
    assert final[final.index("--notes-file") + 1] == "release-notes.md"
    assert "--latest" in final
    assert "--generate-notes" not in final
    assert "dist/release-notes.md" not in [arg for call in calls for arg in call]
    assert (tmp_path / "release-notes.md").read_text() == "curated release notes"
    assert jobs["release"]["needs"] == "pypi-install-smoke"
    assert any("render_release_notes.py" in s.get("run", "") for s in jobs["build"]["steps"])


def test_only_openapi_snapshot_steps_are_conditional():
    workflow = yaml.safe_load((ROOT / ".github/workflows/update-knowledge-db.yml").read_text())
    steps = workflow["jobs"]["publish-openapi"]["steps"]
    by_name = {s.get("name"): s for s in steps}
    snapshot = by_name["Publish dated OpenAPI snapshot"]
    assert snapshot["if"] == "steps.content.outputs.changed == 'true'"
    assert "if" not in by_name["Publish the validated current snapshot"]
    assert "if" not in by_name["Verify public JSON, checksums and browser access"]
