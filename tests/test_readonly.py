"""Tests for READ_ONLY mode.

Covers:
- Settings parses the READ_ONLY env var (truthy/falsy variants).
- BaseHTTPClient._request refuses non-GET when READ_ONLY is set in env.
- call_central_api / call_greenlake_api refuse non-GET when settings.read_only.
- _build_env propagates READ_ONLY to the script subprocess.
- The MCP instructions string contains the READ_ONLY banner when active.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from netops_api_navigator._http_core import BaseHTTPClient, CentralAPIError
from netops_api_navigator.config import Settings, load_settings
from netops_api_navigator.tools.execution import _build_env

# ── Settings parsing ────────────────────────────────────────────────


class TestSettingsReadOnly:

    @pytest.mark.parametrize("value", ["1", "true", "True", "TRUE", "yes", "YES", "on"])
    def test_truthy_values(self, monkeypatch, value):
        monkeypatch.setenv("READ_ONLY", value)
        # required creds for load_settings to be sensible
        monkeypatch.setenv("CENTRAL_BASE_URL", "https://x")
        monkeypatch.setenv("CENTRAL_CLIENT_ID", "id")
        monkeypatch.setenv("CENTRAL_CLIENT_SECRET", "sec")
        s = load_settings()
        assert s.read_only is True

    @pytest.mark.parametrize("value", ["0", "false", "False", "no", "off", ""])
    def test_falsy_values(self, monkeypatch, value):
        monkeypatch.setenv("READ_ONLY", value)
        monkeypatch.setenv("CENTRAL_BASE_URL", "https://x")
        monkeypatch.setenv("CENTRAL_CLIENT_ID", "id")
        monkeypatch.setenv("CENTRAL_CLIENT_SECRET", "sec")
        s = load_settings()
        assert s.read_only is False

    def test_unset_defaults_to_false(self, monkeypatch):
        monkeypatch.delenv("READ_ONLY", raising=False)
        monkeypatch.setenv("CENTRAL_BASE_URL", "https://x")
        monkeypatch.setenv("CENTRAL_CLIENT_ID", "id")
        monkeypatch.setenv("CENTRAL_CLIENT_SECRET", "sec")
        s = load_settings()
        assert s.read_only is False


# ── _http_core enforcement ──────────────────────────────────────────


class TestHttpCoreReadOnly:

    @pytest.fixture
    def client(self):
        c = BaseHTTPClient("https://api.example.com", "cid", "csec")
        # Pre-populate token so _request doesn't try to fetch one.
        c._access_token = "tok"
        c._token_expires_at = 9999999999
        yield c
        c.close()

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    def test_blocks_non_get_when_readonly_env_set(self, client, monkeypatch, method):
        monkeypatch.setenv("READ_ONLY", "true")
        with pytest.raises(CentralAPIError) as exc_info:
            client._request(method, "some/path")
        assert exc_info.value.status_code == 403
        assert "READ_ONLY" in str(exc_info.value).upper()

    def test_allows_get_when_readonly(self, client, monkeypatch):
        monkeypatch.setenv("READ_ONLY", "true")
        with patch.object(client._http, "request") as mock_req:
            import httpx
            mock_req.return_value = httpx.Response(
                200, json={"ok": True},
                request=httpx.Request("GET", "https://api.example.com/p"),
            )
            result = client._request("GET", "p")
        assert result == {"ok": True}

    def test_allows_non_get_when_readonly_off(self, client, monkeypatch):
        monkeypatch.delenv("READ_ONLY", raising=False)
        with patch.object(client._http, "request") as mock_req:
            import httpx
            mock_req.return_value = httpx.Response(
                200, json={"ok": True},
                request=httpx.Request("POST", "https://api.example.com/p"),
            )
            result = client._request("POST", "p", json_body={"x": 1})
        assert result == {"ok": True}


# ── api_call tools ──────────────────────────────────────────────────


class TestApiCallToolsReadOnly:

    @pytest.fixture
    def setup(self, monkeypatch):
        from mcp.server.fastmcp import FastMCP

        from netops_api_navigator.central_client import CentralClient
        from netops_api_navigator.tools.api_call import (
            register_api_call_tools,
            register_greenlake_api_call_tools,
        )
        # Avoid real HTTP from BaseHTTPClient init
        settings = Settings(
            central_base_url="https://x",
            central_client_id="cid",
            central_client_secret="csec",
            read_only=True,
        )
        client = CentralClient(settings.central_base_url,
                               settings.central_client_id,
                               settings.central_client_secret)
        mcp = FastMCP("test-ro")
        register_api_call_tools(mcp, settings, client, None)
        register_greenlake_api_call_tools(mcp, settings, client, None)  # use same client for test
        tools = {t.name: t.fn for t in mcp._tool_manager._tools.values()}
        return tools

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    def test_central_call_rejects_writes(self, setup, method):
        from mcp.server.fastmcp.exceptions import ToolError
        with pytest.raises(ToolError) as exc_info:
            setup["call_central_api"](path="some/path", method=method, body={"a": 1})
        assert "READ_ONLY" in str(exc_info.value).upper()

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    def test_greenlake_call_rejects_writes(self, setup, method):
        from mcp.server.fastmcp.exceptions import ToolError
        with pytest.raises(ToolError) as exc_info:
            setup["call_greenlake_api"](path="some/path", method=method, body={"a": 1})
        assert "READ_ONLY" in str(exc_info.value).upper()


class TestWorkshopApiTool:

    def test_schema_has_no_method_or_body_and_batch_rejects_writes(self):
        from mcp.server.fastmcp import FastMCP
        from mcp.server.fastmcp.exceptions import ToolError

        from netops_api_navigator.central_client import CentralClient
        from netops_api_navigator.tools.api_call import register_workshop_api_call_tool

        settings = Settings(
            profile="workshop",
            central_base_url="https://x",
            central_client_id="cid",
            central_client_secret="csec",
        )
        client = CentralClient("https://x", "cid", "csec")
        mcp = FastMCP("test-workshop")
        register_workshop_api_call_tool(mcp, settings, client, None)
        tools = mcp._tool_manager._tools
        call_tool = tools["call_central_api"]
        paginate_tool = tools["paginate_central_api"]

        assert set(call_tool.parameters["properties"]) == {"path", "query_params", "calls"}
        assert set(paginate_tool.parameters["properties"]) == {
            "path",
            "query_params",
            "page_size",
            "max_pages",
            "item_key",
        }
        with pytest.raises(ToolError, match="GET"):
            call_tool.fn(calls=[{"path": "x", "method": "POST", "body": {"x": 1}}])

        client.paginate = MagicMock(return_value=[{"serial": "SERIAL-1"}])
        result = json.loads(paginate_tool.fn(path="devices", page_size=25, max_pages=3))
        assert result["item_count"] == 1
        assert result["items"] == [{"serial": "SERIAL-1"}]
        client.paginate.assert_called_once_with(
            "devices",
            params=None,
            page_size=25,
            max_pages=3,
            item_key=None,
        )

        with pytest.raises(ToolError, match="managed by this tool"):
            paginate_tool.fn(path="devices", query_params={"next": "2"})

    def test_paginator_seeds_required_initial_offset(self):
        from mcp.server.fastmcp import FastMCP

        from netops_api_navigator.central_client import CentralClient
        from netops_api_navigator.tools.api_call import register_workshop_api_call_tool

        class _RequiredOffsetGraph:
            is_available = True

            def query(self, cypher, params=None, read_only=True):
                if "WHERE p.required = true" in cypher:
                    return [{"name": "offset", "location": "query"}]
                return []

        settings = Settings(
            profile="workshop",
            central_base_url="https://x",
            central_client_id="cid",
            central_client_secret="csec",
        )
        client = CentralClient("https://x", "cid", "csec")
        client.paginate = MagicMock(return_value=[])
        mcp = FastMCP("test-workshop")
        register_workshop_api_call_tool(mcp, settings, client, _RequiredOffsetGraph())

        result = json.loads(
            mcp._tool_manager._tools["paginate_central_api"].fn(path="devices")
        )

        assert result["request"]["query_params"] == {"offset": "0"}
        client.paginate.assert_called_once_with(
            "devices",
            params={"offset": "0"},
            page_size=100,
            max_pages=50,
            item_key=None,
        )


# ── _build_env propagation ──────────────────────────────────────────


class TestBuildEnvPropagation:

    def test_read_only_forwarded_when_true(self, monkeypatch):
        monkeypatch.delenv("READ_ONLY", raising=False)
        s = Settings(
            central_base_url="https://x",
            central_client_id="cid",
            central_client_secret="csec",
            read_only=True,
        )
        env = _build_env(s)
        assert env.get("READ_ONLY") == "true"

    def test_pythonpath_includes_script_runtime_when_readonly(self, monkeypatch):
        monkeypatch.delenv("READ_ONLY", raising=False)
        monkeypatch.delenv("PYTHONPATH", raising=False)
        s = Settings(
            central_base_url="https://x",
            central_client_id="cid",
            central_client_secret="csec",
            read_only=True,
        )
        env = _build_env(s)
        pp = env.get("PYTHONPATH", "")
        assert "script_runtime" in pp
        # The sitecustomize.py shipped in script_runtime must exist so that
        # Python actually picks it up at interpreter startup.
        first_entry = pp.split(os.pathsep)[0]
        assert (Path(first_entry) / "sitecustomize.py").exists()

    def test_read_only_not_set_when_false(self, monkeypatch):
        monkeypatch.delenv("READ_ONLY", raising=False)
        s = Settings(
            central_base_url="https://x",
            central_client_id="cid",
            central_client_secret="csec",
            read_only=False,
        )
        env = _build_env(s)
        # Either absent or explicitly empty/false — must not be truthy
        val = env.get("READ_ONLY", "")
        assert val.lower() not in ("1", "true", "yes", "on")


# ── sitecustomize httpx guard ───────────────────────────────────────


class TestScriptRuntimeHttpxGuard:
    """Verify that the sitecustomize shipped to script subprocesses
    monkey-patches httpx so raw-httpx scripts cannot bypass READ_ONLY."""

    def test_httpx_post_blocked_in_subprocess(self, tmp_path):
        runtime_dir = (
            Path(__file__).parent.parent
            / "src"
            / "netops_api_navigator"
            / "script_runtime"
        )
        assert (runtime_dir / "sitecustomize.py").exists()

        script = tmp_path / "evil.py"
        script.write_text(
            "import sys, httpx\n"
            "try:\n"
            "    httpx.Client().post('https://example.invalid/x', json={'a': 1})\n"
            "except httpx.HTTPError as e:\n"
            "    msg = str(e)\n"
            "    if 'READ_ONLY' in msg:\n"
            "        print('BLOCKED'); sys.exit(0)\n"
            "    print('UNEXPECTED:' + msg); sys.exit(2)\n"
            "print('NOT_BLOCKED'); sys.exit(1)\n",
            encoding="utf-8",
        )

        import subprocess
        env = os.environ.copy()
        env["READ_ONLY"] = "true"
        env["PYTHONPATH"] = str(runtime_dir) + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        assert result.returncode == 0, (
            f"expected sitecustomize to block POST; "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert "BLOCKED" in result.stdout

    def test_httpx_get_allowed_when_readonly(self, tmp_path):
        """GET must not be blocked by the sitecustomize guard."""
        runtime_dir = (
            Path(__file__).parent.parent
            / "src"
            / "netops_api_navigator"
            / "script_runtime"
        )

        script = tmp_path / "ok.py"
        # We don't actually want to make a real network call — just verify
        # that the guard does not raise *before* httpx tries to connect.
        # A connection error is fine; a "READ_ONLY" HTTPError is not.
        script.write_text(
            "import sys, httpx\n"
            "try:\n"
            "    httpx.Client(timeout=0.1).get('http://127.0.0.1:1/x')\n"
            "except httpx.HTTPError as e:\n"
            "    if 'READ_ONLY' in str(e):\n"
            "        print('WRONGLY_BLOCKED'); sys.exit(1)\n"
            "    print('OK_CONNECT_ERROR'); sys.exit(0)\n"
            "print('OK_RESPONSE'); sys.exit(0)\n",
            encoding="utf-8",
        )

        import subprocess
        env = os.environ.copy()
        env["READ_ONLY"] = "true"
        env["PYTHONPATH"] = str(runtime_dir) + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        assert result.returncode == 0
        assert "WRONGLY_BLOCKED" not in result.stdout

    def test_sso_token_post_not_blocked_when_readonly(self, tmp_path):
        """POST to the HPE SSO token endpoint must NOT be blocked in READ_ONLY
        mode — central_helpers needs it to acquire a bearer token before
        issuing any GET request to Central/GreenLake.
        """
        runtime_dir = (
            Path(__file__).parent.parent
            / "src"
            / "netops_api_navigator"
            / "script_runtime"
        )

        script = tmp_path / "token_post.py"
        script.write_text(
            "import sys, httpx\n"
            "TOKEN_URL = 'https://sso.common.cloud.hpe.com/as/token.oauth2'\n"
            "try:\n"
            "    httpx.Client(timeout=0.1).post(TOKEN_URL, data={'grant_type': 'client_credentials'})\n"
            "except httpx.HTTPError as e:\n"
            "    if 'READ_ONLY' in str(e):\n"
            "        print('WRONGLY_BLOCKED'); sys.exit(1)\n"
            "    # Any other HTTP/network error (ConnectError, timeout) is fine.\n"
            "    print('OK_NETWORK_ERROR'); sys.exit(0)\n"
            "print('OK_RESPONSE'); sys.exit(0)\n",
            encoding="utf-8",
        )

        import subprocess
        env = os.environ.copy()
        env["READ_ONLY"] = "true"
        env["PYTHONPATH"] = str(runtime_dir) + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        assert result.returncode == 0, (
            f"SSO token POST should not be blocked in READ_ONLY mode; "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert "WRONGLY_BLOCKED" not in result.stdout


# ── Server instructions banner ──────────────────────────────────────


class TestInstructionsBanner:

    def test_banner_present_when_readonly(self):
        from netops_api_navigator.instructions import build_instructions
        text = build_instructions(read_only=True)
        assert "READ_ONLY" in text.upper()
        # Mention that network changes are not allowed
        upper = text.upper()
        assert "NOT" in upper and ("PERMITTED" in upper or "ALLOWED" in upper)

    def test_banner_absent_when_not_readonly(self):
        from netops_api_navigator.instructions import build_instructions
        text = build_instructions(read_only=False)
        assert "READ_ONLY MODE ACTIVE" not in text.upper()

    def test_workshop_instructions_do_not_advertise_mutating_surface(self):
        from netops_api_navigator.instructions import build_instructions

        text = build_instructions(read_only=True, workshop_mode=True)
        assert "WORKSHOP PROFILE" in text
        assert "GET-ONLY" in text
        assert "write_graph" not in text
        assert "execute_script" not in text
        assert "query_topology" in text
        assert "paginate_central_api" in text
        assert "bundled GET-only" in text
