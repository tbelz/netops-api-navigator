#!/usr/bin/env python3
"""Monitor README workflows and publication health; notify the maintainer on changes.

Uses only the standard library and the authenticated GitHub CLI. It deliberately
never downloads or executes code from a triggering workflow or pull request.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import urllib.request
from datetime import datetime, timedelta, timezone

from check_knowledge_health import assess
from verify_openapi_publication import verify

WORKFLOWS = {
    "build-and-push.yml": "Container build",
    "python-package.yml": "Python package",
    "tests.yml": "Tests",
    "update-knowledge-db.yml": "Knowledge DB / OpenAPI publication",
}
LABEL = "workflow-health-alert"
MARKER = "<!-- workflow-health-monitor -->"
MAX_AGE = timedelta(hours=36)
ASSETS = {"manifest.json", "knowledge_db.tar.gz", "knowledge_db_ast.tar.gz", "knowledge_db_compiler.tar.gz", "central-spec-cache.tar.gz"}


class GitHub:
    def __init__(self, repo: str):
        self.repo = repo

    def api(self, path: str, method: str = "GET", **payload):
        command = ["gh", "api", f"repos/{self.repo}/{path}", "--method", method]
        if payload:
            command.extend(["--input", "-"])
        result = subprocess.run(command, input=json.dumps(payload) if payload else None,
                                text=True, capture_output=True, check=True, timeout=60)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def pages(self, path: str, key: str | None = None):
        # Follow all pages: daily data releases must not hide software releases
        # or an existing alert after the first page.
        page = 1
        while True:
            separator = "&" if "?" in path else "?"
            value = self.api(f"{path}{separator}per_page=100&page={page}")
            items = value[key] if key else value
            yield from items
            if len(items) < 100:
                return
            page += 1


def timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def check_runs(workflows: dict, now: datetime) -> tuple[list[str], list[str], bool]:
    problems, links = [], []
    pending = False
    for filename, name in WORKFLOWS.items():
        runs = workflows.get(filename, [])
        if not runs:
            problems.append(f"{name}: no production run found")
            continue
        latest = max(runs, key=lambda run: (run["created_at"], run.get("run_attempt", 1)))
        links.append(f"[{name}]({latest['html_url']})")
        if latest["status"] != "completed":
            pending = True
            if now - timestamp(latest["created_at"]) > timedelta(hours=2):
                problems.append(f"{name}: run has not completed within two hours")
        elif latest.get("conclusion") != "success":
            problems.append(f"{name}: {latest.get('conclusion', 'unknown result')}")
        if filename == "update-knowledge-db.yml":
            successful = [run for run in runs if run.get("conclusion") == "success"]
            if not successful or now - max(timestamp(run["created_at"]) for run in successful) > MAX_AGE:
                problems.append("Knowledge DB: no successful refresh within 36 hours")
    return problems, links, pending


def fetch_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "netops-api-navigator-health"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def collect(github: GitHub, now: datetime) -> tuple[list[str], list[str], bool]:
    workflows = {}
    for filename in WORKFLOWS:
        # PR/feature-branch CI must not overwrite production health or gain
        # access to the privileged issue-writing workflow.
        production_runs = []
        for run in github.pages(f"actions/workflows/{filename}/runs", "workflow_runs"):
            if run.get("event") not in {"push", "schedule", "workflow_dispatch"}:
                continue
            branch = run.get("head_branch") or ""
            if branch != "main" and not (filename in {"python-package.yml", "build-and-push.yml"} and branch.startswith("v")):
                continue
            production_runs.append(run)
            if len(production_runs) == 10:
                break
        workflows[filename] = production_runs
    problems, links, pending = check_runs(workflows, now)
    for filename in WORKFLOWS:
        metadata = github.api(f"actions/workflows/{filename}")
        if metadata.get("state") != "active":
            problems.append(f"{WORKFLOWS[filename]}: workflow is disabled")

    release = next((item for item in github.pages("releases")
                    if item["tag_name"].startswith("knowledge-db-") and not item["draft"] and not item["prerelease"]), None)
    if release is None:
        problems.append("Knowledge DB: no published release found")
    else:
        links.append(f"[Knowledge release]({release['html_url']})")
        assets = {asset["name"]: asset for asset in release["assets"]}
        missing = sorted(ASSETS - assets.keys())
        if missing:
            problems.append(f"Knowledge DB: missing release assets: {', '.join(missing)}")
        if "manifest.json" in assets:
            manifest = fetch_json(assets["manifest.json"]["browser_download_url"])
            errors, warnings = assess(manifest)
            problems.extend(errors + warnings)

    owner, repository = github.repo.split("/")
    base_url = f"https://{owner}.github.io/{repository}/openapi/latest/"
    try:
        report = fetch_json(base_url + "export-report.json")
        if report.get("status") != "valid":
            problems.append("Published OpenAPI export is not valid")
        if now - timestamp(report["scraped_at"]) > MAX_AGE:
            problems.append("Published OpenAPI schema has not been refreshed within 36 hours")
        if len(report.get("outputs", {})) != 3:
            problems.append("Published OpenAPI export is incomplete")
        verify(base_url, report)
        links.append(f"[Public OpenAPI report]({base_url}export-report.json)")
    except Exception as error:
        problems.append(f"Public OpenAPI verification failed: {type(error).__name__}")
        print(f"OpenAPI verification details: {error}")
    return problems, links, pending


def reconcile(github: GitHub, problems: list[str], links: list[str], pending: bool,
              assignee: str, monitor_url: str) -> None:
    issues = [issue for issue in github.pages(f"issues?state=open&labels={LABEL}")
              if MARKER in (issue.get("body") or "") and "pull_request" not in issue]
    if not problems:
        if not pending:
            for issue in issues:
                github.api(f"issues/{issue['number']}/comments", "POST",
                           body=f"@{assignee} All monitored workflows and data checks recovered. [Health check]({monitor_url})")
                github.api(f"issues/{issue['number']}", "PATCH", state="closed", state_reason="completed")
        return

    fingerprint = hashlib.sha256(json.dumps(sorted(set(problems))).encode()).hexdigest()
    state_marker = f"<!-- health-state:{fingerprint} -->"
    body = "\n".join([
        MARKER, state_marker, f"@{assignee} Repository health needs attention.", "",
        *[f"- {problem}" for problem in sorted(set(problems))], "", *links,
        f"[Monitor run]({monitor_url})", "",
        "This issue closes only after all monitored checks recover. Repeated unchanged findings do not send new comments.",
    ])
    if issues:
        issue = issues[0]
        if state_marker not in issue["body"]:
            github.api(f"issues/{issue['number']}", "PATCH", body=body)
            github.api(f"issues/{issue['number']}/comments", "POST", body=body)
    else:
        # Idempotent label setup; API failures are never swallowed.
        labels = {label["name"] for label in github.pages("labels")}
        if LABEL not in labels:
            github.api("labels", "POST", name=LABEL, color="B60205", description="Automated workflow and data health alert")
        github.api("issues", "POST", title="Repository health: workflow or API data needs attention",
                   body=body, labels=[LABEL], assignees=[assignee])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"), required=not os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--assignee", default=os.environ.get("ALERT_ASSIGNEE", "tbelz"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    github = GitHub(args.repo)
    monitor_url = f"https://github.com/{args.repo}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
    try:
        problems, links, pending = collect(github, datetime.now(timezone.utc))
    except Exception as error:
        # A broken health check must not close an existing incident.
        problems, links, pending = [f"Health inspection failed: {type(error).__name__}"], [], True
        print(f"Inspection details: {error}")
    print(json.dumps({"problems": problems, "links": links, "pending": pending}, indent=2))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as stream:
            stream.write("## Repository health\n\n" + "\n".join(f"- {item}" for item in (problems or ["All checks passed"])) + "\n\n" + "\n".join(links) + "\n")
    if not args.dry_run:
        reconcile(github, problems, links, pending, args.assignee, monitor_url)
    raise SystemExit(bool(problems))


if __name__ == "__main__":
    main()
