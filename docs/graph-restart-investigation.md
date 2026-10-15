# Persisted graph restart investigation

## Reproduction (2026-10-15)

Tested on macOS arm64, Python 3.12.12, real-ladybug 0.15.3 and Codex
0.162.0-alpha.17.2, starting from upstream `f768a4b`. Each experiment copied an
unopened, verified schema-10 knowledge snapshot into an isolated directory.
Download recovery was disabled to avoid masking failure. Connected tests used
`READ_ONLY=true`; no model turn or Central configuration change was made.

The actual Codex `app-server` client was initialized over STDIO and queried with
`mcpServerStatus/list` for `arubacentral`. Instrumentation in the MCP child recorded
seed start/completion, SIGTERM, and runtime close. The unpatched first discovery
exposed 15 tools, then received SIGTERM during seeding without executing cleanup.
The second discovery returned zero tools and an initialization connection-closed
error. An earlier copy from this same path independently failed Ladybug reopen
with an invalid WAL record type.

Crucially, this status request can dispose its temporary MCP server immediately.
Waiting after the response does **not** establish that seeding completed. The
initial reproduction's app-server-stderr observation was insufficient.

With this patch, three consecutive interrupted-seed discovery cycles exposed all
15 tools, with cancellation and completed runtime close recorded each time. A
separate three-cycle experiment held factory return until both built-in seeds
finished; every seed succeeded, every discovery exposed 15 tools, and every close
completed. The database was reused within each experiment, never replaced.

## What the evidence establishes

The server lacked SIGTERM cleanup and did not own/drain its seed and IPC work.
Those defects provide a reproducible interrupted-write shutdown path; handling
SIGTERM and draining that work prevents the observed restart failure in these
experiments. They do not establish the precise byte-level Ladybug corruption
mechanism or guarantee survival of SIGKILL, power failure, or arbitrary native
engine hangs. A client's short termination grace period can still interrupt an
in-flight native statement or checkpoint.

A stricter immediate app-server exit also exposed a second confirmed defect:
logging `graph_ipc_stopped` to the closed stderr pipe raised `BrokenPipeError`,
skipping the remaining runtime cleanup. The logging sink now tolerates a closed
client pipe; regression tests cover failures during both write and flush. With
that fix, three immediate app-server-exit cycles also exposed all 15 tools and
recorded completed graph cleanup.

The graph manager now serializes connection scopes and result consumption with
close. Connections/results are released before the database reference; it does
not forcibly close a database while outstanding references may still exist.
IPC shutdown wakes idle sockets and waits for executing requests. Seed shutdown
polls cancellation at 100 ms, kills the script process group (or Windows process
tree), reaps the script, and skips queued jobs. Output draining has a bounded
fallback for detached descendants that retain pipe descriptors.
Native graph work is drained rather than forcibly aborted.

Startup recognizes the observed short-read diagnostic as well as explicit WAL
corruption. It preserves unreadable runtime graphs and reports restore guidance.
It also skips release refresh for existing runtime graphs, avoiding silent loss
of local state or replacement of another process's open database. This changes
the previous automatic-refresh/reinstall behavior deliberately; see the README
for explicit restore/upgrade instructions.

## Re-running native discovery

Use an unopened verified snapshot directory containing `graph_db` and
`manifest.json`. The probe copies both into repo-local `tmp/`, uses a fresh script
library, disables downloads, and forces GET-only mode. Credentials are optional
for offline testing; provide an environment file for the connected 15-tool case.

```sh
uv run python scripts/probe_codex_graph_restart.py \
  --codex /path/to/codex --seed-dir /path/to/verified-seed \
  --env-file /path/to/credentials.env
# Repeat with --settled for completed-seed shutdowns.
```

Do not publish raw graph files, credentials, or tenant topology. The probe's
summary and lifecycle events contain tool names and seed states only. A successful
probe demonstrates the Codex MCP client path, not attachment to an already-open
chat. Production deployment, explicit recovery of existing corrupted data, and
reconnection of that chat remain separate actions.
