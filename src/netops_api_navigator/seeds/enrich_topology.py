#!/usr/bin/env python3
"""Enrich the graph with L2 physical topology data from LLDP.

Fetches per-site LLDP topology from Central and creates:
- CONNECTED_TO edges between managed Device nodes
- LINKED_TO edges from Device to UnmanagedDevice (third-party LLDP neighbours)
- UnmanagedDevice nodes for discovered third-party devices
- HAS_UNMANAGED edges from Site to UnmanagedDevice

Requires: populate_base_graph must have been run first (needs Site + Device nodes).
"""

import argparse
import json
import sys

from central_helpers import api, graph, CentralAPIError

TOPOLOGY_PATH = "network-monitoring/v1/topology"
SEED_NAME = "enrich_topology"


def fetch_site_topology(site_id: str) -> dict:
    """Fetch L2 topology for a single site."""
    try:
        return api.get(f"{TOPOLOGY_PATH}/{site_id}")
    except CentralAPIError as exc:
        print(f"Warning: topology fetch failed for site {site_id}: "
              f"[{exc.status_code}] {exc.message}", file=sys.stderr)
        return {"devices": [], "links": []}


def main():
    parser = argparse.ArgumentParser(description="Enrich graph with L2 topology.")
    parser.add_argument(
        "--site-id",
        default=None,
        help="Only enrich topology for this site (otherwise iterates all sites).",
    )
    args = parser.parse_args()

    # Get all site IDs from the graph
    if args.site_id:
        site_ids = [args.site_id]
    else:
        site_rows = graph.query("MATCH (s:Site) RETURN s.scopeId AS sid")
        site_ids = [r["sid"] for r in site_rows if r.get("sid")]

    if not site_ids:
        print(json.dumps({"error": "No sites in graph. Run populate_base_graph first."}))
        sys.exit(1)

    print(f"Enriching topology for {len(site_ids)} sites...", file=sys.stderr)

    summary = {
        "site_id_filter": args.site_id,
        "sites_processed": 0,
        "connected_to": 0,
        "linked_to": 0,
        "unmanaged_devices": 0,
        "errors": [],
    }

    for site_id in site_ids:
        topo = fetch_site_topology(site_id)
        links = topo.get("links", [])
        if not links:
            summary["sites_processed"] += 1
            continue

        # Collect unmanaged device metadata
        topo_device_info: dict[str, dict] = {}
        for td in topo.get("devices", []):
            serial = str(td.get("serial", ""))
            if serial:
                topo_device_info[serial] = td

        # Aggregate links by (from, to) device pair
        edge_map: dict[tuple[str, str], dict] = {}
        for link in links:
            from_serial = str(link.get("from", ""))
            to_serial = str(link.get("to", ""))
            if not from_serial or not to_serial:
                continue

            key = (from_serial, to_serial)
            if key not in edge_map:
                edge_map[key] = {
                    "fromPorts": [],
                    "toPorts": [],
                    "speed": link.get("speed", 0.0),
                    "edgeType": link.get("edgeType", ""),
                    "health": link.get("health", ""),
                    "lag": "",
                    "stpState": link.get("stpState", ""),
                    "isSibling": bool(link.get("isSibling", False)),
                }

            for p in link.get("fromPortList", []):
                name = p.get("name", "")
                if name:
                    edge_map[key]["fromPorts"].append(name)
                lag = p.get("lag", "")
                if lag:
                    edge_map[key]["lag"] = lag
            for p in link.get("toPortList", []):
                name = p.get("name", "")
                if name:
                    edge_map[key]["toPorts"].append(name)

        # Insert edges
        unmanaged_created: set[str] = set()
        for (from_serial, to_serial), props in edge_map.items():
            from_is_unmanaged = from_serial.startswith("tpd_")
            to_is_unmanaged = to_serial.startswith("tpd_")

            params = {
                "fromPorts": ",".join(props["fromPorts"]),
                "toPorts": ",".join(props["toPorts"]),
                "speed": float(props["speed"] or 0) / 1_000_000_000,  # bps -> Gbps
                "edgeType": props["edgeType"],
                "health": props["health"],
                "lag": props["lag"],
                "stpState": props["stpState"],
                "isSibling": props["isSibling"],
            }

            if from_is_unmanaged and to_is_unmanaged:
                continue
            elif not from_is_unmanaged and not to_is_unmanaged:
                # Device -> Device
                graph.execute(
                    "MATCH (a:Device {serial: $a}), (b:Device {serial: $b}) "
                    "MERGE (a)-[c:CONNECTED_TO]->(b) "
                    "SET c.fromPorts = $fromPorts, c.toPorts = $toPorts, "
                    "c.speed = $speed, c.edgeType = $edgeType, c.health = $health, "
                    "c.lag = $lag, c.stpState = $stpState, c.isSibling = $isSibling",
                    {"a": from_serial, "b": to_serial, **params},
                )
                summary["connected_to"] += 1
            else:
                managed = from_serial if not from_is_unmanaged else to_serial
                unmanaged = to_serial if not from_is_unmanaged else from_serial

                if unmanaged not in unmanaged_created:
                    mac = unmanaged[4:] if unmanaged.startswith("tpd_") else unmanaged
                    info = topo_device_info.get(unmanaged, {})
                    graph.execute(
                        "MERGE (u:UnmanagedDevice {mac: $mac}) "
                        "SET u.name = $name, u.model = $model, u.deviceType = $dt, "
                        "u.health = $health, u.status = $status, u.ipv4 = $ip, "
                        "u.siteId = $sid",
                        {
                            "mac": mac,
                            "name": info.get("name", "") or "",
                            "model": info.get("model", "") or "",
                            "dt": info.get("type", info.get("deviceFunction", "")) or "",
                            "health": str(info.get("health", "")) or "",
                            "status": info.get("status", "") or "",
                            "ip": info.get("ipv4", "") or "",
                            "sid": site_id,
                        },
                    )
                    graph.execute(
                        "MATCH (s:Site {scopeId: $sid}), (u:UnmanagedDevice {mac: $mac}) "
                        "MERGE (s)-[:HAS_UNMANAGED]->(u)",
                        {"sid": site_id, "mac": mac},
                    )
                    unmanaged_created.add(unmanaged)
                    summary["unmanaged_devices"] += 1

                mac = unmanaged[4:] if unmanaged.startswith("tpd_") else unmanaged
                graph.execute(
                    "MATCH (d:Device {serial: $d}), (u:UnmanagedDevice {mac: $mac}) "
                    "MERGE (d)-[l:LINKED_TO]->(u) "
                    "SET l.fromPorts = $fromPorts, l.toPorts = $toPorts, "
                    "l.speed = $speed, l.edgeType = $edgeType, l.health = $health, "
                    "l.lag = $lag, l.stpState = $stpState, l.isSibling = $isSibling",
                    {"d": managed, "mac": mac, **params},
                )
                summary["linked_to"] += 1

        summary["sites_processed"] += 1

    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
