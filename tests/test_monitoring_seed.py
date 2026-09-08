"""TDD tests for the populate_monitoring seed script.

Tests the monitoring data transformation and graph population logic
without requiring live API credentials.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── Fixtures: mock API responses ────────────────────────────────────

MOCK_DEVICES = [
    {"serial": "SW001", "name": "Core-SW-1", "deviceType": "SWITCH", "siteName": "HQ"},
    {"serial": "AP001", "name": "AP-Office-1", "deviceType": "AP", "siteName": "HQ"},
    {"serial": "GW001", "name": "GW-HQ", "deviceType": "GATEWAY", "siteName": "HQ"},
]

MOCK_SWITCH_PORTS = {
    "items": [
        {
            "port_number": 1,
            "name": "1/1/1",
            "admin_status": "Up",
            "oper_status": "Up",
            "speed": "1000",
            "duplex": "Full",
            "type": "Ethernet",
            "vlan_id": 100,
            "poe_status": "Delivering",
        },
        {
            "port_number": 2,
            "name": "1/1/2",
            "admin_status": "Up",
            "oper_status": "Down",
            "speed": "1000",
            "duplex": "Full",
            "type": "Ethernet",
            "vlan_id": 200,
            "poe_status": "Disabled",
        },
    ]
}

MOCK_AP_RADIOS = {
    "radios": [
        {
            "index": 0,
            "band": "2.4GHz",
            "channel": 6,
            "tx_power": 18,
            "client_count": 12,
            "noise_floor": -95,
            "utilization": 35,
            "radio_mode": "AP",
        },
        {
            "index": 1,
            "band": "5GHz",
            "channel": 36,
            "tx_power": 20,
            "client_count": 25,
            "noise_floor": -100,
            "utilization": 55,
            "radio_mode": "AP",
        },
    ]
}

MOCK_CLIENTS = [
    {
        "macaddr": "AA:BB:CC:DD:EE:01",
        "name": "laptop-1",
        "ip_address": "10.0.1.10",
        "os_type": "Windows",
        "connection_type": "wireless",
        "associated_device": "AP001",
        "signal_db": -55,
        "snr": 40,
        "speed_mbps": 866,
    },
    {
        "macaddr": "AA:BB:CC:DD:EE:02",
        "name": "phone-1",
        "ip_address": "10.0.1.11",
        "os_type": "iOS",
        "connection_type": "wireless",
        "associated_device": "AP001",
        "signal_db": -70,
        "snr": 25,
        "speed_mbps": 400,
    },
]


# ── Helpers: build mock central_helpers module ──────────────────────


def _make_mock_api(responses: dict[str, dict | list]) -> MagicMock:
    """Create a mock API that returns predefined responses for paths."""
    api = MagicMock()

    def mock_get(path, params=None):
        for pattern, resp in responses.items():
            if pattern in path:
                return resp
        return {}

    def mock_paginate(path, **kwargs):
        for pattern, resp in responses.items():
            if pattern in path:
                return resp if isinstance(resp, list) else resp.get("items", [])
        return []

    api.get = MagicMock(side_effect=mock_get)
    api.paginate = MagicMock(side_effect=mock_paginate)
    return api


def _make_mock_graph() -> MagicMock:
    """Create a mock graph helper that records Cypher calls."""
    g = MagicMock()
    g._executed = []

    def mock_execute(cypher, params=None):
        g._executed.append({"cypher": cypher, "params": params or {}})
        return []

    def mock_query(cypher, params=None):
        if "Device" in cypher and "RETURN" in cypher:
            return [
                {"serial": d["serial"], "name": d["name"], "deviceType": d["deviceType"]}
                for d in MOCK_DEVICES
            ]
        return []

    g.execute = MagicMock(side_effect=mock_execute)
    g.query = MagicMock(side_effect=mock_query)
    return g


# ── Import the seed module with mocked dependencies ─────────────────
# The seed imports `from central_helpers import api, graph, CentralAPIError`
# We mock this at the module level before importing.


@pytest.fixture
def seed_module():
    """Import populate_monitoring with mocked central_helpers."""
    # Create mock central_helpers module
    mock_ch = MagicMock()
    mock_ch.CentralAPIError = type("CentralAPIError", (Exception,), {})
    # Add status_code and message attributes via custom __init__
    def _cae_init(self, msg="", status_code=0, message=""):
        super(type(self), self).__init__(msg)
        self.status_code = status_code
        self.message = message
    mock_ch.CentralAPIError.__init__ = _cae_init

    mock_api = _make_mock_api({
        "SW001/interfaces": MOCK_SWITCH_PORTS,
        "AP001/radios": MOCK_AP_RADIOS,
        "clients": MOCK_CLIENTS,
    })
    mock_graph = _make_mock_graph()
    mock_ch.api = mock_api
    mock_ch.graph = mock_graph

    # Inject into sys.modules
    old = sys.modules.get("central_helpers")
    sys.modules["central_helpers"] = mock_ch

    # Add seeds dir to path
    seeds_dir = str(Path(__file__).parent.parent / "src" / "netops_api_navigator" / "seeds")
    sys.path.insert(0, seeds_dir)

    try:
        # Remove cached import if any
        if "populate_monitoring" in sys.modules:
            del sys.modules["populate_monitoring"]
        import populate_monitoring
        yield populate_monitoring, mock_api, mock_graph
    finally:
        sys.path.remove(seeds_dir)
        if old is not None:
            sys.modules["central_helpers"] = old
        else:
            sys.modules.pop("central_helpers", None)
        sys.modules.pop("populate_monitoring", None)


# =====================================================================
# Test: Module Structure
# =====================================================================


class TestModuleStructure:
    """Verify the seed module has the expected entry points."""

    def test_has_main(self, seed_module):
        mod, _, _ = seed_module
        assert hasattr(mod, "main"), "seed must define main()"

    def test_has_fetch_switch_ports(self, seed_module):
        mod, _, _ = seed_module
        assert hasattr(mod, "fetch_switch_ports")

    def test_has_fetch_ap_radios(self, seed_module):
        mod, _, _ = seed_module
        assert hasattr(mod, "fetch_ap_radios")

    def test_has_fetch_clients(self, seed_module):
        mod, _, _ = seed_module
        assert hasattr(mod, "fetch_clients")


# =====================================================================
# Test: Data Fetching
# =====================================================================


class TestDataFetching:
    """Test that the seed calls the correct API endpoints."""

    def test_fetch_switch_ports_calls_api(self, seed_module):
        mod, mock_api, _ = seed_module
        ports = mod.fetch_switch_ports("SW001")
        assert len(ports) == 2
        assert ports[0]["port_number"] == 1
        mock_api.get.assert_called()

    def test_fetch_ap_radios_calls_api(self, seed_module):
        mod, mock_api, _ = seed_module
        radios = mod.fetch_ap_radios("AP001")
        assert len(radios) == 2
        assert radios[0]["band"] == "2.4GHz"
        mock_api.get.assert_called()

    def test_fetch_clients_paginates(self, seed_module):
        mod, mock_api, _ = seed_module
        clients = mod.fetch_clients()
        assert len(clients) == 2
        mock_api.paginate.assert_called()


# =====================================================================
# Test: Graph Population
# =====================================================================


class TestGraphPopulation:
    """Test that monitoring data is correctly written to the graph."""

    def test_upsert_ports_creates_merge_statements(self, seed_module):
        mod, _, mock_graph = seed_module
        mod.upsert_ports("SW001", MOCK_SWITCH_PORTS["items"])
        # Should MERGE Port nodes and HAS_PORT relationships
        calls = mock_graph.execute.call_args_list
        assert len(calls) >= 2, "Should create at least 2 port nodes"
        # Check that MERGE is used (idempotent)
        for call in calls:
            cypher = call[0][0]
            assert "MERGE" in cypher

    def test_upsert_ports_links_to_device(self, seed_module):
        mod, _, mock_graph = seed_module
        mod.upsert_ports("SW001", MOCK_SWITCH_PORTS["items"])
        calls = mock_graph.execute.call_args_list
        # At least one call should reference HAS_PORT or Device
        port_rels = [c for c in calls if "HAS_PORT" in c[0][0]]
        assert len(port_rels) >= 2, "Each port should be linked to its device"

    def test_upsert_radios_creates_merge_statements(self, seed_module):
        mod, _, mock_graph = seed_module
        mod.upsert_radios("AP001", MOCK_AP_RADIOS["radios"])
        calls = mock_graph.execute.call_args_list
        assert len(calls) >= 2
        for call in calls:
            assert "MERGE" in call[0][0]

    def test_upsert_radios_links_to_device(self, seed_module):
        mod, _, mock_graph = seed_module
        mod.upsert_radios("AP001", MOCK_AP_RADIOS["radios"])
        calls = mock_graph.execute.call_args_list
        radio_rels = [c for c in calls if "HAS_RADIO" in c[0][0]]
        assert len(radio_rels) >= 2

    def test_upsert_clients_creates_merge_statements(self, seed_module):
        mod, _, mock_graph = seed_module
        mod.upsert_clients(MOCK_CLIENTS)
        calls = mock_graph.execute.call_args_list
        assert len(calls) >= 2
        for call in calls:
            assert "MERGE" in call[0][0]

    def test_upsert_clients_links_to_device(self, seed_module):
        mod, _, mock_graph = seed_module
        mod.upsert_clients(MOCK_CLIENTS)
        calls = mock_graph.execute.call_args_list
        client_rels = [c for c in calls if "HAS_CLIENT" in c[0][0]]
        assert len(client_rels) >= 2


# =====================================================================
# Test: Table DDL (ensures dynamic tables exist)
# =====================================================================


class TestTableCreation:
    """Test that monitoring tables are created before population."""

    def test_ensure_tables_creates_port_table(self, seed_module):
        mod, _, mock_graph = seed_module
        mod.ensure_monitoring_tables()
        calls = mock_graph.execute.call_args_list
        port_ddl = [c for c in calls if "CREATE NODE TABLE" in c[0][0] and "Port" in c[0][0]]
        assert len(port_ddl) >= 1

    def test_ensure_tables_creates_radio_table(self, seed_module):
        mod, _, mock_graph = seed_module
        mod.ensure_monitoring_tables()
        calls = mock_graph.execute.call_args_list
        radio_ddl = [c for c in calls if "CREATE NODE TABLE" in c[0][0] and "Radio" in c[0][0]]
        assert len(radio_ddl) >= 1

    def test_ensure_tables_creates_client_table(self, seed_module):
        mod, _, mock_graph = seed_module
        mod.ensure_monitoring_tables()
        calls = mock_graph.execute.call_args_list
        client_ddl = [c for c in calls if "CREATE NODE TABLE" in c[0][0] and "Client" in c[0][0]]
        assert len(client_ddl) >= 1

    def test_ensure_tables_creates_rel_tables(self, seed_module):
        mod, _, mock_graph = seed_module
        mod.ensure_monitoring_tables()
        calls = mock_graph.execute.call_args_list
        rel_ddl = [c for c in calls if "CREATE REL TABLE" in c[0][0]]
        assert len(rel_ddl) >= 3, "HAS_PORT, HAS_RADIO, HAS_CLIENT"

    def test_ensure_tables_uses_if_not_exists(self, seed_module):
        mod, _, mock_graph = seed_module
        mod.ensure_monitoring_tables()
        calls = mock_graph.execute.call_args_list
        ddl_calls = [c for c in calls if "CREATE" in c[0][0]]
        for call in ddl_calls:
            assert "IF NOT EXISTS" in call[0][0], "DDL must be idempotent"


# =====================================================================
# Test: Main Orchestration
# =====================================================================


class TestMainOrchestration:
    """Test the main() function orchestrates everything correctly."""

    def test_main_returns_summary_json(self, seed_module, capsys):
        mod, _, _ = seed_module
        mod.main()
        captured = capsys.readouterr()
        summary = json.loads(captured.out)
        assert "ports" in summary
        assert "radios" in summary
        assert "clients" in summary
        assert "errors" in summary

    def test_main_processes_switches(self, seed_module, capsys):
        mod, _, _ = seed_module
        mod.main()
        captured = capsys.readouterr()
        summary = json.loads(captured.out)
        assert summary["ports"] >= 2, "Should have populated 2 switch ports"

    def test_main_processes_aps(self, seed_module, capsys):
        mod, _, _ = seed_module
        mod.main()
        captured = capsys.readouterr()
        summary = json.loads(captured.out)
        assert summary["radios"] >= 2, "Should have populated 2 AP radios"

    def test_main_processes_clients(self, seed_module, capsys):
        mod, _, _ = seed_module
        mod.main()
        captured = capsys.readouterr()
        summary = json.loads(captured.out)
        assert summary["clients"] >= 2

    def test_main_handles_api_errors_gracefully(self, seed_module, capsys):
        """API errors for one device shouldn't crash the whole seed."""
        mod, mock_api, _ = seed_module

        # Make the port fetch fail for one device
        original_get = mock_api.get.side_effect
        call_count = [0]

        def failing_get(path, params=None):
            if "interfaces" in path:
                call_count[0] += 1
                raise mod.CentralAPIError("timeout", status_code=504, message="timeout")
            return original_get(path, params)

        mock_api.get.side_effect = failing_get
        mod.main()
        captured = capsys.readouterr()
        summary = json.loads(captured.out)
        assert len(summary["errors"]) >= 1, "Should record the error"


# =====================================================================
# Test: Meta JSON
# =====================================================================


class TestMetaJSON:
    """Verify the meta.json file is correct."""

    def test_meta_json_exists(self):
        meta_path = (
            Path(__file__).parent.parent
            / "src"
            / "netops_api_navigator"
            / "seeds"
            / "populate_monitoring.meta.json"
        )
        assert meta_path.exists(), "meta.json must exist"

    def test_meta_json_auto_run_false(self):
        meta_path = (
            Path(__file__).parent.parent
            / "src"
            / "netops_api_navigator"
            / "seeds"
            / "populate_monitoring.meta.json"
        )
        meta = json.loads(meta_path.read_text())
        assert meta["auto_run"] is False, "Monitoring seed must be on-demand only"

    def test_meta_json_has_monitoring_tag(self):
        meta_path = (
            Path(__file__).parent.parent
            / "src"
            / "netops_api_navigator"
            / "seeds"
            / "populate_monitoring.meta.json"
        )
        meta = json.loads(meta_path.read_text())
        assert "monitoring" in meta["tags"]
