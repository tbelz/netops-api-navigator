#!/usr/bin/env python3
"""End-to-end test for the OpenAPI-based API catalog.

Tests the full pipeline: scraper -> index -> catalog tools -> MCP server stdio.
Runs against the live developer.arubanetworks.com site.
Uses Central credentials from .env for the full MCP server test.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Ensure the src directory is importable
sys.path.insert(0, str(Path(__file__).parent / "src"))

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
WARN = "\033[93mWARN\033[0m"

results: list[tuple[str, bool, str]] = []


def run_test(name: str, func):
    """Run a test function and record result."""
    try:
        func()
        results.append((name, True, ""))
        print(f"  [{PASS}] {name}")
    except Exception as e:
        results.append((name, False, str(e)))
        print(f"  [{FAIL}] {name}: {e}")


# Load .env manually (no python-dotenv dependency)
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip().replace("\r", "")
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


# ======================================================================
# Phase 1: OAS Scraper
# ======================================================================

print("\n" + "=" * 70)
print("Phase 1: OAS Scraper - live scrape from developer.arubanetworks.com")
print("=" * 70)

from netops_api_navigator.oas_scraper import (
    _extract_page_data,
    _discover_categories,
    _extract_oas,
    _fetch_page,
    discover_and_scrape,
    DOCS_HOST,
)

# Fetch one page to use for multiple tests
print("  Fetching MRT reference root page...")
_mrt_html = _fetch_page(f"{DOCS_HOST}/new-central/reference")
_mrt_data = _extract_page_data(_mrt_html)


def test_extract_page_data():
    assert isinstance(_mrt_data, dict), f"Expected dict, got {type(_mrt_data)}"
    assert "sidebars" in _mrt_data, f"Missing 'sidebars'. Keys: {list(_mrt_data.keys())[:10]}"


def test_discover_categories():
    cats = _discover_categories(_mrt_data)
    assert len(cats) >= 5, f"Expected >=5 categories, got {len(cats)}"
    for c in cats:
        assert c["title"], "Category missing title"
        assert c["first_child_slug"], f"Category '{c['title']}' missing first_child_slug"
    print(f"    -> {len(cats)} categories: {[c['title'] for c in cats]}")


def test_extract_oas():
    spec = _extract_oas(_mrt_data)
    if spec is None:
        print(f"    -> Root page has no oasDefinition (redirect page), acceptable")
        return
    assert "paths" in spec and "info" in spec
    print(f"    -> {spec['info'].get('title', '?')}: {len(spec['paths'])} paths")


run_test("extract_page_data returns dict with sidebars", test_extract_page_data)
run_test("discover_categories finds MRT categories", test_discover_categories)
run_test("extract_oas returns spec from root page", test_extract_oas)

# Full scrape with disk cache
print("\n  Running full discover_and_scrape (MRT + Config)...")
_cache_dir = Path(tempfile.mkdtemp(prefix="oas_test_"))
os.environ["SPEC_CACHE_DIR"] = str(_cache_dir)
t0 = time.time()
_all_specs = discover_and_scrape(cache_dir=_cache_dir, ttl=86400)
_scrape_time = time.time() - t0


def test_scrape_count():
    assert len(_all_specs) >= 10, f"Expected >=10 specs, got {len(_all_specs)}"
    print(f"    -> {len(_all_specs)} specs in {_scrape_time:.1f}s")


def test_spec_structure():
    for spec in _all_specs:
        title = spec.get("info", {}).get("title", "")
        paths = spec.get("paths", {})
        assert title, "Spec missing info.title"
        assert len(paths) > 0, f"Spec '{title}' has 0 paths"


def test_cache_written():
    cache_files = list(_cache_dir.rglob("*.json"))
    assert len(cache_files) >= 10, f"Expected >=10, got {len(cache_files)}"
    print(f"    -> {len(cache_files)} cache files")


def test_cache_reuse():
    t0 = time.time()
    specs2 = discover_and_scrape(cache_dir=_cache_dir, ttl=86400)
    elapsed = time.time() - t0
    assert len(specs2) == len(_all_specs), f"Count mismatch: {len(specs2)} vs {len(_all_specs)}"
    assert elapsed < 10, f"Took {elapsed:.1f}s, expected <10s"
    print(f"    -> {len(specs2)} specs in {elapsed:.1f}s (cached)")


run_test(f"discover_and_scrape returns >=10 specs ({len(_all_specs)})", test_scrape_count)
run_test("each spec has info.title and paths", test_spec_structure)
run_test("cache files written to disk", test_cache_written)
run_test("re-scrape uses cache (fast)", test_cache_reuse)


# ======================================================================
# Phase 2: OAS Index
# ======================================================================

print("\n" + "=" * 70)
print("Phase 2: OAS Index - build, search, detail")
print("=" * 70)

from netops_api_navigator.oas_index import OASIndex

_index = OASIndex()
_index.build(_all_specs)


def test_index_endpoint_count():
    assert _index.total_endpoints >= 200, f"Got {_index.total_endpoints}"


def test_index_category_count():
    cats = _index.categories
    assert len(cats) >= 10, f"Got {len(cats)}"
    print(f"    -> {list(cats.keys())}")


def test_search_vlan():
    r = _index.search("vlan")
    assert len(r) > 0, "No results"
    for x in r[:3]:
        print(f"    -> {x.method} {x.path} - {x.summary[:60]}")


def test_search_dhcp():
    r = _index.search("dhcp")
    assert len(r) > 0
    print(f"    -> {len(r)} results")


def test_search_devices():
    r = _index.search("devices")
    assert len(r) > 0
    print(f"    -> {len(r)} results")


def test_deprecated_filter():
    without = _index.search("monitoring", include_deprecated=False)
    with_dep = _index.search("monitoring", include_deprecated=True)
    print(f"    -> Without deprecated: {len(without)}, with: {len(with_dep)}")
    assert len(with_dep) >= len(without)


def test_get_detail_resolved():
    r = _index.search("devices")
    assert r, "Need search results"
    first = r[0]
    detail = _index.get_detail(first.method, first.path)
    assert detail is not None
    print(f"    -> {detail.method} {detail.path}")
    print(f"      Params: {len(detail.parameters)}, Responses: {len(detail.responses)}")
    if detail.parameters:
        p = detail.parameters[0]
        print(f"      First param: {p.name} ({p.location}, required={p.required})")
        if p.schema:
            assert "$ref" not in p.schema, f"Unresolved $ref: {p.schema}"


def test_post_request_body():
    for entry in _index._entries:
        if entry.method == "POST" and entry.request_body:
            detail = _index.get_detail(entry.method, entry.path)
            assert detail and detail.request_body
            assert "$ref" not in str(detail.request_body)[:500]
            print(f"    -> {detail.method} {detail.path}")
            print(f"      Body keys: {list(detail.request_body.keys())[:5]}")
            return
    print(f"    {WARN} No POST with request_body found")


def test_operation_id_lookup():
    for entry in _index._entries:
        if entry.operation_id:
            result = _index.get_detail_by_operation_id(entry.operation_id)
            assert result is not None
            assert result.operation_id == entry.operation_id
            print(f"    -> {entry.operation_id} -> {result.method} {result.path}")
            return
    raise AssertionError("No entries with operationId")


def test_search_nonsense():
    r = _index.search("xyzzy_nonexistent_12345")
    assert len(r) == 0, f"Expected 0, got {len(r)}"


run_test(f"index has >=200 endpoints ({_index.total_endpoints})", test_index_endpoint_count)
run_test(f"index has >=10 categories ({len(_index.categories)})", test_index_category_count)
run_test("search('vlan') returns results", test_search_vlan)
run_test("search('dhcp') returns results", test_search_dhcp)
run_test("search('devices') returns results", test_search_devices)
run_test("deprecated filter works", test_deprecated_filter)
run_test("get_detail returns resolved schemas", test_get_detail_resolved)
run_test("POST endpoint has request_body schema", test_post_request_body)
run_test("get_detail_by_operation_id works", test_operation_id_lookup)
run_test("search nonsense returns empty", test_search_nonsense)


# ======================================================================
# Phase 3: Catalog Tool Functions (direct invocation via OASIndex)
# ======================================================================

print("\n" + "=" * 70)
print("Phase 3: Catalog tool functions - direct invocation")
print("=" * 70)


def test_list_categories():
    cats = _index.list_categories()
    assert len(cats) >= 10
    total = sum(cats.values())
    print(f"    -> {len(cats)} categories, {total} total endpoints")
    for name, count in sorted(cats.items()):
        print(f"      {name}: {count}")


def test_search_detail_roundtrip():
    """Simulate two-step agent workflow: search compact -> get detail."""
    compact = _index.search("switch")
    assert compact, "No results for 'switch'"
    print(f"    -> search('switch'): {len(compact)} results")
    pick = compact[0]
    detail = _index.get_detail(pick.method, pick.path)
    assert detail is not None
    print(f"    -> {pick.method} {pick.path}")
    print(f"      Summary: {detail.summary[:80]}")
    print(f"      Params: {len(detail.parameters)}")
    if detail.request_body:
        print(f"      Has request body schema")
    print(f"      Responses: {[r.status for r in detail.responses]}")


run_test("search -> detail round-trip works", test_search_detail_roundtrip)


# ======================================================================
# Phase 4: Full MCP Server via stdio subprocess
# ======================================================================

print("\n" + "=" * 70)
print("Phase 4: Full MCP Server (subprocess stdio)")
print("=" * 70)

from netops_api_navigator.config import load_settings

settings = load_settings()
if not settings.has_credentials:
    print(f"  [{WARN}] No credentials in .env - skipping MCP server tests")
else:
    mcp_env = {
        **os.environ,
        "CENTRAL_BASE_URL": settings.central_base_url,
        "CENTRAL_CLIENT_ID": settings.central_client_id,
        "CENTRAL_CLIENT_SECRET": settings.central_client_secret,
        "SPEC_CACHE_DIR": str(_cache_dir),
        "SPEC_CACHE_TTL": "86400",
    }

    # MCP stdio uses newline-delimited JSON (one JSON object per line).
    # Use binary mode to avoid Windows encoding issues.
    def _mcp_call(
        messages: list[dict],
        target_id: int = 2,
        timeout: int = 90,
    ) -> dict | None:
        """Start MCP server, send messages via stdin, return response matching target_id."""
        stdin_data = "\n".join(json.dumps(m) for m in messages) + "\n"

        proc = subprocess.Popen(
            [sys.executable, "-m", "netops_api_navigator"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=mcp_env,
            cwd=str(Path(__file__).parent),
        )
        proc.stdin.write(stdin_data.encode("utf-8"))
        proc.stdin.close()

        try:
            stdout_bytes, stderr_bytes = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout_bytes, _ = proc.communicate()

        result = None
        for line in stdout_bytes.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                if msg.get("id") == target_id:
                    result = msg
                    break
            except json.JSONDecodeError:
                continue
        return result

    _INIT_MSGS = [
        {
            "jsonrpc": "2.0", "method": "initialize", "id": 1,
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "e2e-test", "version": "1.0.0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    ]

    def _tool_call(name: str, args: dict, **kwargs) -> dict | None:
        msgs = _INIT_MSGS + [{
            "jsonrpc": "2.0", "method": "tools/call", "id": 2,
            "params": {"name": name, "arguments": args},
        }]
        return _mcp_call(msgs, **kwargs)

    def _resource_read(uri: str, **kwargs) -> dict | None:
        msgs = _INIT_MSGS + [{
            "jsonrpc": "2.0", "method": "resources/read", "id": 2,
            "params": {"uri": uri},
        }]
        return _mcp_call(msgs, **kwargs)

    def _get_text(resp: dict) -> str:
        return resp["result"]["content"][0]["text"]

    # The MCP server initializes the catalog in a background thread.
    # With pre-populated cache it's fast, but the first tool call may still
    # see an empty catalog.

    def test_mcp_initialize():
        """Verify MCP handshake works."""
        msgs = [_INIT_MSGS[0]]
        resp = _mcp_call(msgs, target_id=1, timeout=30)
        assert resp is not None, "No response to initialize"
        assert "result" in resp, f"Handshake error: {json.dumps(resp)[:200]}"
        info = resp["result"].get("serverInfo", {})
        print(f"    -> Server: {info.get('name', '?')} v{info.get('version', '?')}")
        caps = resp["result"].get("capabilities", {})
        print(f"      Capabilities: {list(caps.keys())}")

    def test_mcp_api_call():
        resp = _tool_call("call_central_api", {
            "path": "network-monitoring/v1/switches",
            "query_params": {"limit": "1"},
        })
        assert resp, "No response"
        assert "result" in resp, f"Error response: {json.dumps(resp)[:300]}"
        text = _get_text(resp)
        is_error = resp.get("result", {}).get("isError", False)
        try:
            data = json.loads(text)
            print(f"    -> API response keys: {list(data.keys())[:5]}")
        except json.JSONDecodeError:
            if is_error:
                print(f"    -> Tool error: {text[:200]}")
            else:
                print(f"    -> Response text: {text[:200]}")

    def test_mcp_inventory():
        resp = _tool_call("refresh_inventory", {
            "detail_level": "summary",
            "force_refresh": True,
        })
        assert resp, "No response"
        text = _get_text(resp)
        assert len(text) > 10, "Empty response"
        print(f"    -> {len(text)} chars")

    run_test("MCP: initialize handshake", test_mcp_initialize)
    run_test("MCP: call_central_api (real API)", test_mcp_api_call)
    run_test("MCP: refresh_inventory", test_mcp_inventory)


# ======================================================================
# Summary
# ======================================================================

print("\n" + "=" * 70)
print("SUMMARY")
print("=" * 70)

passed = sum(1 for _, ok, _ in results if ok)
failed = sum(1 for _, ok, _ in results if not ok)
total = len(results)

print(f"\n  Passed: {passed}/{total}")
if failed:
    print(f"  Failed: {failed}/{total}")
    for name, ok, err in results:
        if not ok:
            print(f"    FAIL: {name}: {err}")

# Spec summary
total_paths = sum(len(s.get("paths", {})) for s in _all_specs)
total_schemas = sum(len(s.get("components", {}).get("schemas", {})) for s in _all_specs)
print(f"\n  Specs: {len(_all_specs)}")
print(f"  Total paths: {total_paths}")
print(f"  Total schemas: {total_schemas}")
print(f"  Index endpoints: {_index.total_endpoints}")
print(f"  Index categories: {len(_index.categories)}")
print(f"  Cache dir: {_cache_dir}")

sys.exit(1 if failed else 0)
