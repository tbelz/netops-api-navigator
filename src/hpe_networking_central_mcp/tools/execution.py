"""Script execution tool."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import structlog
from mcp.types import ToolAnnotations

from ..config import Settings

logger = structlog.get_logger("tools.execution")

EXECUTION_TIMEOUT = 300  # 5 minutes


def _run_script(
    settings: Settings,
    filename: str,
    parameters: dict[str, str] | None = None,
    *,
    ipc_env: dict[str, str] | None = None,
) -> str:
    """Execute a script from the library and return JSON result.

    Shared implementation used by both execute_script() and save_script(execute=True).
    Scripts access the graph database through authenticated loopback IPC.
    """
    lib = settings.script_library_path
    script_path = lib / filename

    # Security: ensure script is within library directory
    try:
        script_path = script_path.resolve()
        lib_resolved = lib.resolve()
        if not str(script_path).startswith(str(lib_resolved)):
            return json.dumps({"error": "Path traversal detected. Script must be in the library directory."})
    except (OSError, ValueError):
        return json.dumps({"error": "Invalid script path."})

    if not script_path.exists():
        return json.dumps({
            "error": f"Script '{filename}' not found in library.",
            "hint": "Use list_scripts() to see available scripts, or save_script() to create a new one.",
        })

    if not script_path.suffix == ".py":
        return json.dumps({"error": "Only .py scripts can be executed."})

    # Build command
    cmd = [sys.executable, str(script_path)]
    if parameters:
        for key, value in parameters.items():
            cmd.extend([f"--{key}", str(value)])

    env = _build_env(settings, ipc_env=ipc_env)
    start_time = time.time()

    logger.info("script_execution_start", filename=filename, parameters=parameters)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=EXECUTION_TIMEOUT,
            cwd=str(lib),
            env=env,
        )
        duration = round(time.time() - start_time, 2)

        # Update metadata with run info
        meta_path = script_path.with_suffix(".meta.json")
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            meta["last_exit_code"] = result.returncode
            meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

        logger.info("script_execution_done", filename=filename, exit_code=result.returncode, duration=duration)

        return json.dumps({
            "exit_code": result.returncode,
            "stdout": result.stdout[-10000:] if len(result.stdout) > 10000 else result.stdout,
            "stderr": result.stderr[-5000:] if len(result.stderr) > 5000 else result.stderr,
            "duration_seconds": duration,
            "truncated": len(result.stdout) > 10000 or len(result.stderr) > 5000,
        }, indent=2)

    except subprocess.TimeoutExpired:
        duration = round(time.time() - start_time, 2)
        logger.warning("script_execution_timeout", filename=filename, timeout=EXECUTION_TIMEOUT)
        return json.dumps({
            "error": f"Script execution timed out after {EXECUTION_TIMEOUT} seconds.",
            "exit_code": -1,
            "duration_seconds": duration,
        })
    except Exception as e:
        logger.error("script_execution_error", filename=filename, error=str(e))
        return json.dumps({"error": f"Execution failed: {str(e)}"})


def _build_env(
    settings: Settings, *, ipc_env: dict[str, str] | None = None
) -> dict[str, str]:
    """Build environment variables dict for script execution."""
    env = os.environ.copy()
    env["CENTRAL_BASE_URL"] = settings.central_base_url
    env["CENTRAL_CLIENT_ID"] = settings.central_client_id
    env["CENTRAL_CLIENT_SECRET"] = settings.central_client_secret
    env["GLP_CLIENT_ID"] = settings.effective_glp_client_id
    env["GLP_CLIENT_SECRET"] = settings.effective_glp_client_secret
    env["GREENLAKE_CLIENT_ID"] = settings.effective_glp_client_id
    env["GREENLAKE_CLIENT_SECRET"] = settings.effective_glp_client_secret
    env["GLP_BASE_URL"] = settings.glp_base_url
    env["GRAPH_DB_PATH"] = str(settings.graph_db_path)
    for key in (
        "GRAPH_IPC_HOST",
        "GRAPH_IPC_PORT",
        "GRAPH_IPC_TOKEN",
        "GRAPH_IPC_SOCKET",
    ):
        env.pop(key, None)
    if ipc_env:
        env.update(ipc_env)
    # Propagate read-only mode so script subprocesses' HTTP clients refuse
    # mutating Central / GreenLake API calls (enforced in _http_core for the
    # central_helpers path, and at the httpx layer via the sitecustomize
    # module shipped in script_runtime/ for raw-httpx bypasses).
    if settings.read_only:
        env["READ_ONLY"] = "true"
        # Prepend script_runtime/ so its sitecustomize.py is auto-imported
        # by Python at interpreter startup and installs the httpx guard.
        runtime_dir = str(Path(__file__).resolve().parent.parent / "script_runtime")
        existing_pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            runtime_dir + os.pathsep + existing_pp if existing_pp else runtime_dir
        )
    else:
        env.pop("READ_ONLY", None)
    # Ensure no stale values leak through from host environment
    for key in ("BASE_URL", "CLIENT_ID", "CLIENT_SECRET"):
        env.pop(key, None)
    return env


def register_execution_tools(
    mcp, settings: Settings, *, ipc_env: dict[str, str] | None = None
):
    """Register script execution tools with the MCP server."""

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=False, openWorldHint=True),
    )
    def execute_script(filename: str, parameters: dict[str, str] | None = None) -> str:
        """Execute a Python script from the automation library.

        Runs the script with Central credentials available as environment variables.
        Parameters are passed as CLI arguments (--name value). Scripts should use
        argparse to parse them.

        Args:
            filename: Script filename in the library (e.g., "onboard_device.py").
            parameters: Optional dict of parameter name→value pairs passed as CLI args.

        Returns:
            JSON with stdout, stderr, exit_code, and execution duration.
        """
        return _run_script(settings, filename, parameters, ipc_env=ipc_env)
