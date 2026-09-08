#!/usr/bin/env python3
"""Enrich the graph with monitoring data: switch ports, AP radios, connected clients.

Fetches operational monitoring data from Aruba Central APIs and populates
Port, Radio, and Client nodes linked to existing Device nodes via
HAS_PORT, HAS_RADIO, and HAS_CLIENT relationships.

This is an on-demand enrichment seed — run after populate_base_graph.
"""

import json
import sys

from central_helpers import api, graph, CentralAPIError

# ── Monitoring table DDL (idempotent — safe to re-run) ──────────────

PORT_TABLE_DDL = """
CREATE NODE TABLE IF NOT EXISTS Port (
    port_id      STRING,
    port_number  INT64,
    name         STRING,
    admin_status STRING,
    oper_status  STRING,
    speed        STRING,
    duplex       STRING,
    type         STRING,
    vlan_id      INT64,
    poe_status   STRING,
    PRIMARY KEY (port_id)
)
"""

RADIO_TABLE_DDL = """
CREATE NODE TABLE IF NOT EXISTS Radio (
    radio_id     STRING,
    radio_index  INT64,
    band         STRING,
    channel      INT64,
    tx_power     INT64,
    client_count INT64,
    noise_floor  INT64,
    utilization  INT64,
    radio_mode   STRING,
    PRIMARY KEY (radio_id)
)
"""

CLIENT_TABLE_DDL = """
CREATE NODE TABLE IF NOT EXISTS Client (
    macaddr         STRING,
    name            STRING,
    ip_address      STRING,
    os_type         STRING,
    connection_type STRING,
    signal_db       INT64,
    snr             INT64,
    speed_mbps      INT64,
    PRIMARY KEY (macaddr)
)
"""

HAS_PORT_DDL = """
CREATE REL TABLE IF NOT EXISTS HAS_PORT (
    FROM Device TO Port
)
"""

HAS_RADIO_DDL = """
CREATE REL TABLE IF NOT EXISTS HAS_RADIO (
    FROM Device TO Radio
)
"""

HAS_CLIENT_DDL = """
CREATE REL TABLE IF NOT EXISTS HAS_CLIENT (
    FROM Device TO Client
)
"""


# ── Table setup ─────────────────────────────────────────────────────

def ensure_monitoring_tables() -> None:
    """Create monitoring node and relationship tables if they don't exist."""
    for ddl in (
        PORT_TABLE_DDL,
        RADIO_TABLE_DDL,
        CLIENT_TABLE_DDL,
        HAS_PORT_DDL,
        HAS_RADIO_DDL,
        HAS_CLIENT_DDL,
    ):
        graph.execute(ddl)


# ── Data fetching ───────────────────────────────────────────────────

def fetch_switch_ports(serial: str) -> list[dict]:
    """Fetch port/interface data for a switch."""
    resp = api.get(f"network-monitoring/v1/switches/{serial}/interfaces")
    return resp.get("items", [])


def fetch_ap_radios(serial: str) -> list[dict]:
    """Fetch radio data for an access point."""
    resp = api.get(f"network-monitoring/v1/aps/{serial}/radios")
    return resp.get("radios", [])


def fetch_clients() -> list[dict]:
    """Fetch all connected clients."""
    return api.paginate("network-monitoring/v1/clients", page_size=100)


# ── Graph upsert ────────────────────────────────────────────────────

def upsert_ports(serial: str, ports: list[dict]) -> int:
    """MERGE Port nodes and HAS_PORT relationships for a switch."""
    count = 0
    for port in ports:
        port_id = f"{serial}:{port.get('port_number', 0)}"
        graph.execute(
            "MERGE (p:Port {port_id: $pid}) "
            "SET p.port_number = $pnum, p.name = $name, "
            "p.admin_status = $admin, p.oper_status = $oper, "
            "p.speed = $speed, p.duplex = $duplex, "
            "p.type = $ptype, p.vlan_id = $vlan, "
            "p.poe_status = $poe",
            {
                "pid": port_id,
                "pnum": port.get("port_number", 0),
                "name": port.get("name", ""),
                "admin": port.get("admin_status", ""),
                "oper": port.get("oper_status", ""),
                "speed": str(port.get("speed", "")),
                "duplex": port.get("duplex", ""),
                "ptype": port.get("type", ""),
                "vlan": port.get("vlan_id", 0),
                "poe": port.get("poe_status", ""),
            },
        )
        graph.execute(
            "MATCH (d:Device {serial: $ser}), (p:Port {port_id: $pid}) "
            "MERGE (d)-[:HAS_PORT]->(p)",
            {"ser": serial, "pid": port_id},
        )
        count += 1
    return count


def upsert_radios(serial: str, radios: list[dict]) -> int:
    """MERGE Radio nodes and HAS_RADIO relationships for an AP."""
    count = 0
    for radio in radios:
        radio_id = f"{serial}:{radio.get('index', 0)}"
        graph.execute(
            "MERGE (r:Radio {radio_id: $rid}) "
            "SET r.radio_index = $idx, r.band = $band, "
            "r.channel = $chan, r.tx_power = $txp, "
            "r.client_count = $cc, r.noise_floor = $nf, "
            "r.utilization = $util, r.radio_mode = $mode",
            {
                "rid": radio_id,
                "idx": radio.get("index", 0),
                "band": radio.get("band", ""),
                "chan": radio.get("channel", 0),
                "txp": radio.get("tx_power", 0),
                "cc": radio.get("client_count", 0),
                "nf": radio.get("noise_floor", 0),
                "util": radio.get("utilization", 0),
                "mode": radio.get("radio_mode", ""),
            },
        )
        graph.execute(
            "MATCH (d:Device {serial: $ser}), (r:Radio {radio_id: $rid}) "
            "MERGE (d)-[:HAS_RADIO]->(r)",
            {"ser": serial, "rid": radio_id},
        )
        count += 1
    return count


def upsert_clients(clients: list[dict]) -> int:
    """MERGE Client nodes and HAS_CLIENT relationships."""
    count = 0
    for client in clients:
        mac = client.get("macaddr", "")
        if not mac:
            continue
        graph.execute(
            "MERGE (c:Client {macaddr: $mac}) "
            "SET c.name = $name, c.ip_address = $ip, "
            "c.os_type = $os, c.connection_type = $conn, "
            "c.signal_db = $sig, c.snr = $snr, "
            "c.speed_mbps = $speed",
            {
                "mac": mac,
                "name": client.get("name", ""),
                "ip": client.get("ip_address", ""),
                "os": client.get("os_type", ""),
                "conn": client.get("connection_type", ""),
                "sig": client.get("signal_db", 0),
                "snr": client.get("snr", 0),
                "speed": client.get("speed_mbps", 0),
            },
        )
        assoc_device = client.get("associated_device", "")
        if assoc_device:
            graph.execute(
                "MATCH (d:Device {serial: $ser}), (c:Client {macaddr: $mac}) "
                "MERGE (d)-[:HAS_CLIENT]->(c)",
                {"ser": assoc_device, "mac": mac},
            )
        count += 1
    return count


# ── Main ────────────────────────────────────────────────────────────

def main() -> None:
    summary = {
        "ports": 0,
        "radios": 0,
        "clients": 0,
        "errors": [],
    }

    # Ensure monitoring tables exist
    print("Ensuring monitoring tables exist...", file=sys.stderr)
    ensure_monitoring_tables()

    # Discover devices in the graph
    print("Querying devices from graph...", file=sys.stderr)
    devices = graph.query(
        "MATCH (d:Device) RETURN d.serial AS serial, d.name AS name, "
        "d.deviceType AS deviceType"
    )

    switches = [d for d in devices if d.get("deviceType") == "SWITCH"]
    aps = [d for d in devices if d.get("deviceType") == "AP"]
    print(f"  Switches: {len(switches)}, APs: {len(aps)}", file=sys.stderr)

    # Fetch and populate switch ports
    for sw in switches:
        serial = sw["serial"]
        try:
            ports = fetch_switch_ports(serial)
            summary["ports"] += upsert_ports(serial, ports)
        except CentralAPIError as exc:
            summary["errors"].append(
                f"ports/{serial}: [{exc.status_code}] {exc.message}"
            )

    # Fetch and populate AP radios
    for ap in aps:
        serial = ap["serial"]
        try:
            radios = fetch_ap_radios(serial)
            summary["radios"] += upsert_radios(serial, radios)
        except CentralAPIError as exc:
            summary["errors"].append(
                f"radios/{serial}: [{exc.status_code}] {exc.message}"
            )

    # Fetch and populate clients
    try:
        clients = fetch_clients()
        summary["clients"] += upsert_clients(clients)
    except CentralAPIError as exc:
        summary["errors"].append(
            f"clients: [{exc.status_code}] {exc.message}"
        )

    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
