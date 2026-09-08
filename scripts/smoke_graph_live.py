#!/usr/bin/env python3
"""End-to-end test for the LadybugDB graph database integration.

Tests the full pipeline:
  1. Schema creation (in-memory)
  2. Population from live Central APIs
  3. Cypher queries against real data
  4. Read-only enforcement
  5. Refresh cycle

Requires Central credentials in .env (same as the MCP server).
"""

import atexit
import json
import os
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
# Phase 1: Schema Creation
# ======================================================================

print("\n" + "=" * 70)
print("Phase 1: Graph Schema — LadybugDB database")
print("=" * 70)

from netops_api_navigator.graph.manager import GraphManager
from netops_api_navigator.graph.schema import (
    NODE_TABLES,
    REL_TABLES,
)


def test_schema_constants():
    assert len(NODE_TABLES) >= 6, f"Expected >=6 node tables, got {len(NODE_TABLES)}"
    assert len(REL_TABLES) >= 6, f"Expected >=6 rel tables, got {len(REL_TABLES)}"
    print(f"    -> {len(NODE_TABLES)} node tables, {len(REL_TABLES)} rel tables")


_temp_dir = tempfile.TemporaryDirectory()
atexit.register(_temp_dir.cleanup)
_gm = GraphManager(Path(_temp_dir.name) / "test_graph_db")


def test_initialize():
    _gm.initialize()
    assert _gm._db is not None, "Database not created"
    assert _gm.is_available, "Should be available after initialize"
    print("    -> Database initialized, schema applied")


def test_schema_description():
    desc = _gm.get_schema_description()
    assert "Node Tables" in desc
    assert "Relationship Tables" in desc
    assert "Hierarchy" in desc
    print(f"    -> Schema description: {len(desc)} chars")


run_test("schema constants defined", test_schema_constants)
run_test("GraphManager.initialize() creates DB + schema", test_initialize)
run_test("get_schema_description() returns markdown", test_schema_description)


# ======================================================================
# Phase 2: Population from Live APIs
# ======================================================================

print("\n" + "=" * 70)
print("Phase 2: Population — live API data")
print("=" * 70)

base_url = os.environ.get("CENTRAL_BASE_URL", "")
client_id = os.environ.get("CENTRAL_CLIENT_ID", "")
client_secret = os.environ.get("CENTRAL_CLIENT_SECRET", "")

if not all([base_url, client_id, client_secret]):
    print(f"  [{WARN}] Skipping live API tests — Central credentials not configured")
    print("         Set CENTRAL_BASE_URL, CENTRAL_CLIENT_ID, CENTRAL_CLIENT_SECRET in .env")
    _has_creds = False
else:
    _has_creds = True

_summary = {}

if _has_creds:
    from netops_api_navigator.central_client import CentralClient

    _client = CentralClient(base_url, client_id, client_secret)

    def test_credentials():
        _client.validate()
        print("    -> OAuth2 token acquired")

    def test_populate():
        global _summary
        t0 = time.time()
        _summary = _gm.populate(_client)
        elapsed = time.time() - t0
        assert _gm.ready, "Graph should be ready after populate"
        assert _summary.get("sites", 0) > 0, f"No sites populated: {_summary}"
        assert _summary.get("devices", 0) > 0, f"No devices populated: {_summary}"
        print(f"    -> Populated in {elapsed:.1f}s: {json.dumps({k:v for k,v in _summary.items() if k != 'errors'})}")
        if _summary.get("errors"):
            print(f"    -> Errors: {_summary['errors']}")

    run_test("Central credentials valid", test_credentials)
    run_test("populate() fetches and inserts live data", test_populate)


# ======================================================================
# Phase 3: Cypher Queries
# ======================================================================

print("\n" + "=" * 70)
print("Phase 3: Cypher Queries — structural navigation")
print("=" * 70)

if _has_creds and _gm.ready:

    def test_query_all_sites():
        rows = _gm.query("MATCH (s:Site) RETURN s.name AS name ORDER BY s.name")
        assert len(rows) > 0, "No sites returned"
        names = [r["name"] for r in rows]
        print(f"    -> {len(rows)} sites: {names}")

    def test_query_all_devices():
        rows = _gm.query(
            "MATCH (d:Device) RETURN d.serial AS serial, d.name AS name, "
            "d.deviceType AS type, d.status AS status ORDER BY d.serial"
        )
        assert len(rows) > 0, "No devices returned"
        print(f"    -> {len(rows)} devices")
        for r in rows[:5]:
            print(f"       {r['serial']} | {r['name']} | {r['type']} | {r['status']}")

    def test_query_hierarchy():
        rows = _gm.query(
            "MATCH (o:Org)-[:HAS_COLLECTION]->(sc:SiteCollection)-[:CONTAINS_SITE]->(s:Site) "
            "RETURN o.name AS org, sc.name AS collection, s.name AS site"
        )
        # May be empty if no collections, but should not error
        print(f"    -> {len(rows)} collection→site paths")
        for r in rows:
            print(f"       {r['org']} → {r['collection']} → {r['site']}")

    def test_query_standalone_sites():
        rows = _gm.query(
            "MATCH (o:Org)-[:HAS_SITE]->(s:Site) RETURN s.name AS site"
        )
        print(f"    -> {len(rows)} standalone sites")

    def test_query_site_devices():
        rows = _gm.query(
            "MATCH (s:Site)-[:HAS_DEVICE]->(d:Device) "
            "RETURN s.name AS site, d.serial AS serial, d.name AS device, d.status AS status "
            "ORDER BY s.name, d.serial"
        )
        assert len(rows) > 0, "No site→device relationships"
        print(f"    -> {len(rows)} site→device relationships")

    def test_query_blast_radius():
        # Find the first collection and query blast radius
        colls = _gm.query("MATCH (sc:SiteCollection) RETURN sc.name AS name LIMIT 1")
        if not colls:
            print("    -> No collections to test blast radius on")
            return
        cname = colls[0]["name"]
        # Use string interpolation (safe — cname comes from our own graph, not user input)
        rows = _gm.query(
            f"MATCH (sc:SiteCollection)-[:CONTAINS_SITE]->(s:Site)-[:HAS_DEVICE]->(d:Device) "
            f"WHERE sc.name = '{cname}' "
            f"RETURN sc.name AS collection, s.name AS site, d.serial, d.name AS device"
        )
        print(f"    -> Blast radius for '{cname}': {len(rows)} devices")

    def test_query_device_groups():
        rows = _gm.query(
            "MATCH (dg:DeviceGroup) RETURN dg.name AS name, dg.deviceCount AS count"
        )
        print(f"    -> {len(rows)} device groups")
        for r in rows:
            print(f"       {r['name']} ({r['count']} devices)")

    def test_query_device_group_membership():
        rows = _gm.query(
            "MATCH (dg:DeviceGroup)-[:HAS_MEMBER]->(d:Device) "
            "RETURN dg.name AS grp, d.name AS device, d.serial AS serial "
            "ORDER BY dg.name, d.name"
        )
        print(f"    -> {len(rows)} device-group memberships")
        for r in rows:
            print(f"       {r['grp']} -> {r['device']} ({r['serial']})")
        assert len(rows) > 0, "No HAS_MEMBER relationships populated — deviceGroupId mapping failed"

    def test_query_device_count_per_site():
        rows = _gm.query(
            "MATCH (s:Site)-[:HAS_DEVICE]->(d:Device) "
            "RETURN s.name AS site, count(d) AS devices "
            "ORDER BY devices DESC"
        )
        print(f"    -> Device count per site:")
        for r in rows:
            print(f"       {r['site']}: {r['devices']}")

    def test_query_firmware_versions():
        rows = _gm.query(
            "MATCH (d:Device) WHERE d.firmware <> '' "
            "RETURN d.firmware AS version, d.deviceType AS type, count(d) AS count "
            "ORDER BY type, count DESC"
        )
        print(f"    -> {len(rows)} firmware combos")
        for r in rows[:10]:
            print(f"       {r['type']}: {r['version']} ({r['count']})")

    run_test("MATCH all sites", test_query_all_sites)
    run_test("MATCH all devices", test_query_all_devices)
    run_test("MATCH Org→SiteCollection→Site hierarchy", test_query_hierarchy)
    run_test("MATCH standalone Org→Site", test_query_standalone_sites)
    run_test("MATCH Site→Device relationships", test_query_site_devices)
    run_test("MATCH blast radius for collection", test_query_blast_radius)
    run_test("MATCH device groups", test_query_device_groups)
    run_test("MATCH device group membership", test_query_device_group_membership)
    run_test("MATCH config profiles", test_query_config_profiles)
    run_test("COUNT devices per site", test_query_device_count_per_site)
    run_test("MATCH firmware versions", test_query_firmware_versions)

else:
    print(f"  [{WARN}] Skipping query tests — graph not populated")


# ======================================================================
# Phase 4: Safety & Refresh
# ======================================================================

print("\n" + "=" * 70)
print("Phase 4: Safety — read-only enforcement & refresh")
print("=" * 70)


def test_write_blocked():
    """Verify that write queries are rejected."""
    # Use a fresh manager to avoid needing live data
    gm2 = GraphManager()
    gm2.initialize()
    # Force ready state for testing
    gm2._ready = True

    write_queries = [
        "CREATE (n:Org {scopeId: 'evil', name: 'hacked'})",
        "MATCH (n:Org) DELETE n",
        "MATCH (n:Org) SET n.name = 'hacked'",
        "MATCH (n:Org) DETACH DELETE n",
        "DROP TABLE Org",
        "MERGE (n:Org {scopeId: 'evil'})",
    ]
    for q in write_queries:
        try:
            gm2.query(q, read_only=True)
            raise AssertionError(f"Should have blocked: {q}")
        except ValueError as e:
            assert "Write operations are not allowed" in str(e)
    print(f"    -> All {len(write_queries)} write patterns blocked")


def test_not_ready_error():
    """Verify query returns error when graph not ready."""
    gm3 = GraphManager()
    gm3.initialize()
    # Don't populate — ready should be False
    try:
        gm3.query("MATCH (s:Site) RETURN s.name")
        raise AssertionError("Should have raised RuntimeError")
    except RuntimeError as e:
        assert "still loading" in str(e)
    print("    -> Not-ready error returned correctly")


run_test("write queries blocked (CREATE, DELETE, SET, ...)", test_write_blocked)
run_test("query before populate returns 'still loading'", test_not_ready_error)


if _has_creds and _gm.ready:
    def test_refresh():
        t0 = time.time()
        summary = _gm.refresh(_client)
        elapsed = time.time() - t0
        assert _gm.ready
        assert summary.get("sites", 0) > 0
        assert summary.get("devices", 0) > 0
        print(f"    -> Refreshed in {elapsed:.1f}s: sites={summary['sites']}, devices={summary['devices']}")

    run_test("refresh() re-populates graph", test_refresh)


# ======================================================================
# Phase 5: Topology — L2 link data
# ======================================================================

print("\n" + "=" * 70)
print("Phase 5: Topology — L2 link population and queries")
print("=" * 70)

from netops_api_navigator.graph.schema import TOPOLOGY_REL_TABLES

if _has_creds and _gm.ready:

    def test_topology_schema_constants():
        assert len(TOPOLOGY_REL_TABLES) >= 2, f"Expected >=2 topology rel tables, got {len(TOPOLOGY_REL_TABLES)}"
        assert "CONNECTED_TO" in str(TOPOLOGY_REL_TABLES)
        assert "LINKED_TO" in str(TOPOLOGY_REL_TABLES)
        print(f"    -> {len(TOPOLOGY_REL_TABLES)} topology relationship tables defined")

    def test_load_topology():
        assert not _gm.topology_ready, "Topology should not be loaded yet"
        t0 = time.time()
        summary = _gm.load_topology(_client)
        elapsed = time.time() - t0
        assert _gm.topology_ready, "Topology should be ready after load"
        print(f"    -> Topology loaded in {elapsed:.1f}s: {json.dumps({k:v for k,v in summary.items() if k != 'errors'})}")
        if summary.get("errors"):
            print(f"    -> Errors: {summary['errors']}")

    def test_load_topology_idempotent():
        """Second call should return cached data."""
        t0 = time.time()
        summary = _gm.load_topology(_client)
        elapsed = time.time() - t0
        assert elapsed < 1.0, f"Second load_topology call took {elapsed:.1f}s — should be cached"
        assert _gm.topology_ready
        print(f"    -> Cached topology returned in {elapsed:.3f}s")

    def test_query_connected_to():
        rows = _gm.query(
            "MATCH (a:Device)-[c:CONNECTED_TO]->(b:Device) "
            "RETURN a.name AS from, b.name AS to, c.speed AS speed, "
            "c.edgeType AS type, c.health AS health, c.stpState AS stp, "
            "c.fromPorts AS fromPorts, c.toPorts AS toPorts "
            "ORDER BY a.name LIMIT 20"
        )
        print(f"    -> {len(rows)} CONNECTED_TO edges (showing up to 20)")
        for r in rows[:5]:
            print(f"       {r['from']} → {r['to']} | {r.get('speed','')} Gbps | {r.get('type','')} | {r.get('health','')}")

    def test_query_unmanaged_devices():
        rows = _gm.query(
            "MATCH (u:UnmanagedDevice) "
            "RETURN u.mac AS mac, u.name AS name, u.model AS model, "
            "u.deviceType AS type, u.siteId AS siteId"
        )
        print(f"    -> {len(rows)} unmanaged devices")
        for r in rows[:5]:
            print(f"       {r['mac']} | {r.get('name','')} | {r.get('model','')}")

    def test_query_linked_to():
        rows = _gm.query(
            "MATCH (d:Device)-[l:LINKED_TO]->(u:UnmanagedDevice) "
            "RETURN d.name AS device, u.mac AS unmanagedMac, u.name AS unmanagedName, "
            "l.fromPorts AS ports, l.speed AS speed"
        )
        print(f"    -> {len(rows)} LINKED_TO edges")
        for r in rows[:5]:
            print(f"       {r['device']} → {r.get('unmanagedName', r['unmanagedMac'])}")

    def test_query_topology_per_site():
        rows = _gm.query(
            "MATCH (s:Site)-[:HAS_DEVICE]->(d:Device)-[c:CONNECTED_TO]->(d2:Device) "
            "RETURN s.name AS site, count(c) AS links "
            "ORDER BY links DESC"
        )
        print(f"    -> Topology links per site:")
        for r in rows:
            print(f"       {r['site']}: {r['links']} links")

    def test_query_site_unmanaged():
        rows = _gm.query(
            "MATCH (s:Site)-[:HAS_UNMANAGED]->(u:UnmanagedDevice) "
            "RETURN s.name AS site, count(u) AS unmanaged "
            "ORDER BY unmanaged DESC"
        )
        print(f"    -> Unmanaged devices per site:")
        for r in rows:
            print(f"       {r['site']}: {r['unmanaged']}")

    def test_topology_in_schema_description():
        desc = _gm.get_schema_description()
        assert "CONNECTED_TO" in desc
        assert "LINKED_TO" in desc
        assert "UnmanagedDevice" in desc
        print("    -> Schema description includes topology tables")

    run_test("topology schema constants", test_topology_schema_constants)
    run_test("load_topology() populates L2 data", test_load_topology)
    run_test("load_topology() idempotent (cached)", test_load_topology_idempotent)
    run_test("MATCH CONNECTED_TO edges", test_query_connected_to)
    run_test("MATCH UnmanagedDevice nodes", test_query_unmanaged_devices)
    run_test("MATCH LINKED_TO edges", test_query_linked_to)
    run_test("COUNT topology links per site", test_query_topology_per_site)
    run_test("COUNT unmanaged devices per site", test_query_site_unmanaged)
    run_test("schema description includes topology", test_topology_in_schema_description)

else:
    print(f"  [{WARN}] Skipping topology tests — graph not populated")


# ======================================================================
# Summary
# ======================================================================

print("\n" + "=" * 70)
print("SUMMARY")
print("=" * 70)

passed = sum(1 for _, ok, _ in results if ok)
failed = sum(1 for _, ok, _ in results if not ok)

if failed:
    print(f"\n  {passed} passed, {failed} FAILED\n")
    for name, ok, err in results:
        if not ok:
            print(f"  [{FAIL}] {name}: {err}")
    sys.exit(1)
else:
    print(f"\n  All {passed} tests passed\n")
    sys.exit(0)
