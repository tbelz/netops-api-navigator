# Agent Notes

Operational rules for AI coding agents working in this repo. Read before acting.

## Running long commands (CRITICAL)

**Never re-issue a long-running command (like `pytest`) while a previous instance
of the same command is still running.** Doing so spawns a competing process that
can deadlock the foreground shell, eat the previous process's output, or cause
both runs to fail.

Required workflow for anything that may take >10s (test suites, builds,
`uv sync`, `pip install`, `build_knowledge_db.py`, etc.):

1. Start the command with `mode="async"` (background terminal). Capture the
   returned terminal ID.
2. Poll with `get_terminal_output` using that ID. **Do not** issue another
   command into the same terminal, and **do not** start the same command in a
   new terminal, until the first one prints a shell prompt again or you have
   explicitly killed it with `kill_terminal`.
3. If polling shows no progress for a long time, prefer `kill_terminal`
   followed by a fresh `mode="async"` start over piling on more invocations.
4. Use a background terminal with output redirected to a file. No pipes in WSL!
5. Reading from the terminal with cat/tail via a second sync shell results in failure.
6. **Always write log/scratch files into the repo-local `tmp/` directory** (which
   is gitignored). Do NOT redirect to `/tmp/...` or any other path outside the
   project root — every such command requires manual user confirmation. Use
   `tmp/<name>.log` for pytest output, build logs, etc. Create the directory if
   missing (`mkdir -p tmp`).


## Python

* Always use `uv run` (e.g. `uv run pytest`, `uv run python scripts/...`).
* The repo's pytest config lives in `pyproject.toml`; do not pass `--rootdir`.

## Test feedback loops

The full pytest suite is too slow for a per-edit feedback cycle. Use one of:

* `bash scripts/dev_test.sh` — fast unit subset (target <30s). Excludes
  slow, integration, live_api, and real_spec markers. Use this before
  every push.
* `bash scripts/test_changed.sh` — maps changed source files to relevant
  test files and runs only those. Falls back to `dev_test.sh` for
  foundational changes (pyproject.toml, conftest.py).
* `bash scripts/hydrate_test_fixtures.sh` — one-shot download of the
  real Central spec cache from the latest GitHub release into
  `tmp/test_fixtures/central_spec_cache/`. Required before running
  tests marked `@pytest.mark.real_spec`.

To run the opt-in pre-push hook (recommended for agent loops):

```bash
git config core.hooksPath .githooks
```

CI (`.github/workflows/tests.yml`) still runs the full suite on every
PR. The local loop is for tightening agent iteration, not for replacing
the gate.

## Documentation

- Keep README.md focused on purpose, quickstart, and navigation. Add sections only when explicitly requested.
- Update existing README text when entry-level behavior changes; do not append feature notes, bugfix histories, or maintainer details.
- Put details in the relevant docs/ page; update existing coverage and link instead of duplicating it.
- Verify documentation against code and release availability; update affected docs with behavior changes.
- Keep comments focused on intent, constraints, and non-obvious behavior; remove stale explanations and avoid change logs.


  Don't add anything to AGENTS.md except when I explicitly tell you!
