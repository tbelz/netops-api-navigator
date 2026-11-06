"""Regression coverage for silent publication failures and alert delivery."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    import check_knowledge_health as health
    import monitor_workflow_health as monitor
finally:
    sys.path.pop(0)

pytestmark = pytest.mark.unit
NOW = datetime(2026, 11, 6, 10, tzinfo=timezone.utc)


@pytest.fixture
def manifest():
    return {
        "endpoint_count": 100,
        "sync_health": {"central": {"status": "ok"}, "greenlake": {"status": "ok"}, "vsg": {"status": "ok"}},
        "ast": {"enabled": True, "raw_spec_count": 20, "spec_count": 20, "degraded": {"compiled_count": 2, "failed_count": 0}},
        "compiler_projection": {"enabled": True, "node_kind_counts": {"ApiEndpoint": 100}},
        "compiler_cutover_gates": {"passed": True},
        "projection_parity": {"all_legacy_effectively_covered": True},
    }


def run(*, hours=1, conclusion="success", status="completed", event="schedule", branch="main"):
    return {"created_at": (NOW - timedelta(hours=hours)).isoformat(), "status": status,
            "conclusion": conclusion, "html_url": "https://github.com/example/run/1",
            "event": event, "head_branch": branch}


def test_complete_compilation_accepts_recovered_validation_errors(manifest):
    assert health.assess(manifest) == ([], [])


@pytest.mark.parametrize("field,value", [("spec_count", 19), ("enabled", False), ("raw_spec_count", 0), ("degraded", {"failed_count": 1})])
def test_incomplete_compilation_blocks_publication(manifest, field, value):
    manifest["ast"][field] = value
    assert health.assess(manifest)[0]


@pytest.mark.parametrize("status", ["error", "degraded", None])
def test_api_provider_failure_blocks_publication(manifest, status):
    manifest["sync_health"]["central"]["status"] = status
    assert health.assess(manifest)[0]


def test_endpoint_drop_and_missing_projection_block_publication(manifest):
    assert health.assess(manifest, {"endpoint_count": 200})[0]
    manifest["compiler_projection"]["node_kind_counts"]["ApiEndpoint"] = 99
    assert health.assess(manifest)[0]


def test_optional_degradation_is_reported_without_blocking_api_release(manifest):
    manifest["sync_health"]["vsg"] = {"status": "degraded", "reason": "access_denied"}
    manifest["projection_parity"]["all_legacy_effectively_covered"] = False
    errors, warnings = health.assess(manifest)
    assert not errors
    assert len(warnings) == 2


def test_schedule_gap_is_detected_despite_old_green_run():
    runs = {name: [run()] for name in monitor.WORKFLOWS}
    runs["update-knowledge-db.yml"] = [run(hours=37)]
    problems, _, pending = monitor.check_runs(runs, NOW)
    assert problems == ["Knowledge DB: no successful refresh within 36 hours"]
    assert not pending


@pytest.mark.parametrize("result", ["failure", "timed_out", "cancelled", "action_required"])
def test_all_unsuccessful_conclusions_alert(result):
    runs = {name: [run()] for name in monitor.WORKFLOWS}
    runs["build-and-push.yml"] = [run(conclusion=result)]
    assert monitor.check_runs(runs, NOW)[0] == [f"Container build: {result}"]


def test_running_refresh_does_not_clear_an_incident():
    runs = {name: [run()] for name in monitor.WORKFLOWS}
    runs["update-knowledge-db.yml"] = [run(hours=0, conclusion=None, status="in_progress"), run(hours=24)]
    problems, _, pending = monitor.check_runs(runs, NOW)
    assert not problems
    assert pending


class FakeGitHub:
    repo = "owner/repo"

    def __init__(self, issues=None):
        self.issues = issues or []
        self.calls = []

    def pages(self, path, key=None):
        return iter(self.issues if path.startswith("issues?") else [{"name": monitor.LABEL}])

    def api(self, path, method="GET", **payload):
        self.calls.append((path, method, payload))


def test_alarm_mentions_and_assigns_the_maintainer_then_deduplicates():
    github = FakeGitHub()
    monitor.reconcile(github, ["Build failed"], [], False, "owner", "https://example.test/run")
    path, method, payload = github.calls[0]
    assert (path, method) == ("issues", "POST")
    assert payload["assignees"] == ["owner"]
    assert "@owner" in payload["body"]
    github.issues = [{"number": 42, "body": payload["body"]}]
    github.calls.clear()
    monitor.reconcile(github, ["Build failed"], ["new run link"], False, "owner", "new URL")
    assert not github.calls
    monitor.reconcile(github, ["Different failure"], [], False, "owner", "URL")
    assert [call[1] for call in github.calls] == ["PATCH", "POST"]


def test_recovery_waits_for_completion_and_leaves_unrelated_issues_alone():
    github = FakeGitHub([{"number": 1, "body": monitor.MARKER}, {"number": 2, "body": "manual issue"}])
    monitor.reconcile(github, [], [], True, "owner", "URL")
    assert not github.calls
    monitor.reconcile(github, [], [], False, "owner", "URL")
    assert len(github.calls) == 2
    assert github.calls[-1] == ("issues/1", "PATCH", {"state": "closed", "state_reason": "completed"})


def test_collection_ignores_pr_failures_and_checks_published_content(monkeypatch, manifest):
    github = FakeGitHub()
    release = {"tag_name": "knowledge-db-test", "draft": False, "prerelease": False,
               "html_url": "https://example.test/release", "assets": [
                   {"name": name, "browser_download_url": f"https://example.test/{name}"} for name in monitor.ASSETS]}
    def api(path, method="GET", **payload):
        return {"state": "active"}
    github.api = api
    github.pages = lambda path, key=None: iter(
        [run(hours=0, conclusion="failure", event="pull_request"), run()]
        if key == "workflow_runs" else [release]
    )
    report = {"status": "valid", "scraped_at": NOW.isoformat(), "outputs": {"a": {}, "b": {}, "c": {}}}
    monkeypatch.setattr(monitor, "fetch_json", lambda url: manifest if url.endswith("manifest.json") else report)
    verified = []
    monkeypatch.setattr(monitor, "verify", lambda url, report: verified.append(url))
    assert monitor.collect(github, NOW)[0] == []
    assert verified == ["https://owner.github.io/repo/openapi/latest/"]
    report["scraped_at"] = (NOW - timedelta(hours=37)).isoformat()
    assert "Published OpenAPI schema has not been refreshed within 36 hours" in monitor.collect(github, NOW)[0]
    monkeypatch.setattr(monitor, "verify", lambda *_: (_ for _ in ()).throw(ValueError("checksum mismatch")))
    assert "Public OpenAPI verification failed: ValueError" in monitor.collect(github, NOW)[0]


def test_privileged_monitor_uses_only_main_and_health_gates_publication():
    workflow = yaml.safe_load((ROOT / ".github/workflows/workflow-health.yml").read_text())
    job = workflow["jobs"]["monitor"]
    assert job["steps"][0]["with"]["ref"] == "main"
    assert "pull_request" in job["if"]
    assert workflow["permissions"]["contents"] == "read"
    build = yaml.safe_load((ROOT / ".github/workflows/update-knowledge-db.yml").read_text())
    steps = build["jobs"]["build-knowledge-db"]["steps"]
    health_step = next(step for step in steps if step.get("id") == "health")
    assert "check_knowledge_health.py" in health_step["run"]
    assert "continue-on-error" not in health_step
    release = next(step for step in steps if step.get("uses", "").startswith("softprops/"))
    assert release["with"]["fail_on_unmatched_files"] is True
