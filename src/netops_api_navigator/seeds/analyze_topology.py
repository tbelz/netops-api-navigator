#!/usr/bin/env python3
"""Analyze L2 network topology for a site using NetworkX.

Fetches the LLDP topology from Central and builds a NetworkX graph to identify:
- Single points of failure (bridges / articulation points)
- Network diameter and connectivity
- LAG vs single-link connections
- Unhealthy links and STP anomalies
"""

import argparse
import json
import sys

import networkx as nx

from central_helpers import api


def fetch_topology(site_id: str) -> dict:
    """Fetch L2 topology for a site."""
    return api.get(f"network-monitoring/v1/topology/{site_id}")


def build_graph(topo: dict) -> nx.Graph:
    """Build a NetworkX graph from topology API response."""
    G = nx.Graph()

    for device in topo.get("devices", []):
        serial = device.get("serial", "")
        G.add_node(
            serial,
            name=device.get("name", serial),
            device_type=device.get("type", device.get("deviceFunction", "")),
            health=device.get("health", ""),
            ip=device.get("ipv4", ""),
        )

    for link in topo.get("links", []):
        from_s = link.get("from", "")
        to_s = link.get("to", "")
        if not from_s or not to_s:
            continue
        ports_from = [p.get("name", "") for p in link.get("fromPortList", [])]
        ports_to = [p.get("name", "") for p in link.get("toPortList", [])]
        lag = ""
        for p in link.get("fromPortList", []):
            if p.get("lag"):
                lag = p["lag"]

        G.add_edge(
            from_s,
            to_s,
            speed=link.get("speed", 0),
            edge_type=link.get("edgeType", ""),
            health=link.get("health", ""),
            stp_state=link.get("stpState", ""),
            from_ports=",".join(ports_from),
            to_ports=",".join(ports_to),
            lag=lag,
        )

    return G


def analyze(G: nx.Graph) -> dict:
    """Run topology analysis and return a structured report."""
    report = {
        "nodes": G.number_of_nodes(),
        "edges": G.number_of_edges(),
        "connected": nx.is_connected(G),
    }

    if G.number_of_nodes() == 0:
        report["message"] = "No devices in topology"
        return report

    if nx.is_connected(G):
        report["diameter"] = nx.diameter(G)
        report["avg_shortest_path"] = round(nx.average_shortest_path_length(G), 2)
    else:
        components = list(nx.connected_components(G))
        report["connected_components"] = len(components)
        report["component_sizes"] = [len(c) for c in components]

    # Single points of failure
    bridges = list(nx.bridges(G))
    report["bridges"] = [
        {
            "from": u,
            "to": v,
            "from_name": G.nodes[u].get("name", u),
            "to_name": G.nodes[v].get("name", v),
        }
        for u, v in bridges
    ]

    articulation_points = list(nx.articulation_points(G))
    report["articulation_points"] = [
        {"serial": n, "name": G.nodes[n].get("name", n)} for n in articulation_points
    ]

    # Links without LAG (potential SPOF for that connection)
    no_lag = []
    for u, v, data in G.edges(data=True):
        if not data.get("lag"):
            no_lag.append(
                {
                    "from": G.nodes[u].get("name", u),
                    "to": G.nodes[v].get("name", v),
                    "speed": data.get("speed", 0),
                }
            )
    report["links_without_lag"] = no_lag

    # Unhealthy links
    unhealthy = []
    for u, v, data in G.edges(data=True):
        health = data.get("health", "")
        if health and health != "Good":
            unhealthy.append(
                {
                    "from": G.nodes[u].get("name", u),
                    "to": G.nodes[v].get("name", v),
                    "health": health,
                    "stp_state": data.get("stp_state", ""),
                }
            )
    report["unhealthy_links"] = unhealthy

    # STP anomalies (non-FORWARDING)
    stp_issues = []
    for u, v, data in G.edges(data=True):
        stp = data.get("stp_state", "")
        if stp and stp != "FORWARDING":
            stp_issues.append(
                {
                    "from": G.nodes[u].get("name", u),
                    "to": G.nodes[v].get("name", v),
                    "state": stp,
                }
            )
    report["stp_anomalies"] = stp_issues

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-id", required=True, help="Site scope ID")
    args = parser.parse_args()

    topo = fetch_topology(args.site_id)
    G = build_graph(topo)
    report = analyze(G)

    print(json.dumps(report, indent=2))

    # Summary
    print(f"\n--- Topology Summary ---")
    print(f"Devices: {report['nodes']}, Links: {report['edges']}")
    print(f"Connected: {report['connected']}")
    if report.get("diameter"):
        print(f"Diameter: {report['diameter']} hops")
    if report.get("bridges"):
        print(f"Single points of failure (bridges): {len(report['bridges'])}")
    if report.get("articulation_points"):
        print(f"Articulation points: {len(report['articulation_points'])}")
    if report.get("unhealthy_links"):
        print(f"Unhealthy links: {len(report['unhealthy_links'])}")


if __name__ == "__main__":
    main()
