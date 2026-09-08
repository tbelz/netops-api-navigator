#!/usr/bin/env python3
"""Populate config policy layer — discover categories, profiles, and scope assignments.

Dynamically discovers configuration categories from the Central API,
fetches library-level profiles with extended metadata, and creates scope
assignment relationships (*_ASSIGNS_CONFIG edges).

Effective (resolved) config per device is NOT computed in the graph.
Use the Central API with ``effective=true&detailed=true`` for authoritative
per-device effective config with provenance annotations.
"""

import argparse
import json
import sys

from central_helpers import api, graph, CentralAPIError

SEED_NAME = "populate_config_policy"


# ── ConfigProfile table DDL (idempotent) ────────────────────────────
CONFIG_PROFILE_DDL = """
CREATE NODE TABLE IF NOT EXISTS ConfigProfile (
    id                       STRING,
    name                     STRING,
    category                 STRING,
    scopeId                  STRING,
    deviceFunction           STRING,
    objectType               STRING,
    isDefault                BOOLEAN,
    isEditable               BOOLEAN,
    deviceScopeOnly          BOOLEAN,
    assignedScopeIds         STRING,
    assignedDeviceFunctions  STRING,
    lastSyncedAt             TIMESTAMP,
    PRIMARY KEY (id)
)
"""

_CONFIG_REL_DDLS = [
    "CREATE REL TABLE IF NOT EXISTS HAS_CONFIG (FROM Org TO ConfigProfile)",
    "CREATE REL TABLE IF NOT EXISTS ORG_ASSIGNS_CONFIG (FROM Org TO ConfigProfile)",
    "CREATE REL TABLE IF NOT EXISTS COLLECTION_ASSIGNS_CONFIG (FROM SiteCollection TO ConfigProfile)",
    "CREATE REL TABLE IF NOT EXISTS SITE_ASSIGNS_CONFIG (FROM Site TO ConfigProfile)",
    "CREATE REL TABLE IF NOT EXISTS GROUP_ASSIGNS_CONFIG (FROM DeviceGroup TO ConfigProfile)",
]


_CONFIG_PROFILE_ALTERS = [
    "ALTER TABLE ConfigProfile ADD lastSyncedAt TIMESTAMP",
]


def ensure_config_tables() -> None:
    graph.execute(CONFIG_PROFILE_DDL)
    for ddl in _CONFIG_REL_DDLS:
        graph.execute(ddl)
    # Idempotent migrations for graphs created before lastSyncedAt existed.
    for stmt in _CONFIG_PROFILE_ALTERS:
        try:
            graph.execute(stmt)
        except Exception as exc:
            msg = str(exc).lower()
            if "already exists" not in msg and "duplicate" not in msg and "already has" not in msg:
                raise


# ── Category discovery ───────────────────────────────────────────────

def discover_categories() -> list[str]:
    """Discover all config categories by listing the config API root."""
    try:
        resp = api.get("network-config/v1alpha1")
        if isinstance(resp, dict):
            # The root endpoint may return available categories
            for key in ("categories", "items", "result"):
                if key in resp and isinstance(resp[key], list):
                    cats = [c if isinstance(c, str) else c.get("name", c.get("category", ""))
                            for c in resp[key] if c]
                    return [cat for cat in cats if isinstance(cat, str) and cat.strip()]
        if isinstance(resp, list):
            cats = [c if isinstance(c, str) else c.get("name", c.get("category", ""))
                    for c in resp if c]
            return [cat for cat in cats if isinstance(cat, str) and cat.strip()]
    except CentralAPIError as exc:
        print(f"Warning: category discovery failed: [{exc.status_code}] {exc.message}",
              file=sys.stderr)

    # Fallback: well-known categories
    return [
        "wlan-ssids",
        "sw-port-profiles",
        "gw-port-profiles",
        "roles",
        "server-groups",
        "ntp",
        "dns",
        "snmp",
    ]


def _extract_items(resp, category: str) -> list[dict] | None:
    """Extract the list of config items from a config API response."""
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        for key in ("items", "result", category):
            if key in resp and isinstance(resp[key], list):
                return resp[key]
        # Singleton object with a name field
        if "name" in resp:
            return [resp]
    return None


# ── Profile fetching ────────────────────────────────────────────────

def fetch_library_profiles(category: str) -> list[dict]:
    """Fetch library-level config profiles for a category."""
    try:
        resp = api.get(
            f"network-config/v1alpha1/{category}",
            params={"view-type": "LIBRARY"},
        )
        items = _extract_items(resp, category)
        return items or []
    except CentralAPIError as exc:
        print(f"  Warning: fetch {category} failed: [{exc.status_code}] {exc.message}",
              file=sys.stderr)
        return []


def fetch_scope_profiles(category: str, scope_id: str, scope_type: str) -> list[dict]:
    """Fetch config profiles assigned at a specific scope."""
    try:
        resp = api.get(
            f"network-config/v1alpha1/{category}",
            params={"scopeId": scope_id, "scopeType": scope_type},
        )
        items = _extract_items(resp, category)
        return items or []
    except CentralAPIError:
        return []


# ── Graph insertion ──────────────────────────────────────────────────

def upsert_profile(category: str, profile: dict) -> str:
    """Insert or update a ConfigProfile node with extended metadata.

    Returns the profile ID used as the primary key.
    """
    pid = profile.get("id", profile.get("name", ""))
    if not pid:
        return ""

    full_id = f"{category}:{pid}"

    graph.execute(
        "MERGE (cp:ConfigProfile {id: $pid}) "
        "SET cp.name = $name, cp.category = $cat, "
        "cp.scopeId = $sid, cp.deviceFunction = $df, cp.objectType = $ot, "
        "cp.isDefault = $isDef, cp.isEditable = $isEdit, "
        "cp.deviceScopeOnly = $dso, "
        "cp.assignedScopeIds = $asi, cp.assignedDeviceFunctions = $adf, "
        "cp.lastSyncedAt = current_timestamp()",
        {
            "pid": full_id,
            "name": profile.get("name", pid),
            "cat": category,
            "sid": profile.get("scopeId", profile.get("scope_id", "")),
            "df": profile.get("deviceFunction", profile.get("device_function", "")),
            "ot": profile.get("objectType", profile.get("object_type", "")),
            "isDef": bool(profile.get("isDefault", profile.get("is_default", False))),
            "isEdit": bool(profile.get("isEditable", profile.get("is_editable", True))),
            "dso": bool(profile.get("deviceScopeOnly", profile.get("device_scope_only", False))),
            "asi": json.dumps(profile.get("assignedScopeIds", profile.get("assigned_scope_ids", []))),
            "adf": json.dumps(profile.get("assignedDeviceFunctions",
                                          profile.get("assigned_device_functions", []))),
        },
    )
    return full_id


def link_profile_to_scope(scope_label: str, scope_key: str, scope_id: str,
                          profile_id: str, rel_type: str) -> None:
    """Create a relationship from a scope node to a ConfigProfile."""
    graph.execute(
        f"MATCH (s:{scope_label} {{{scope_key}: $sid}}), "
        f"(cp:ConfigProfile {{id: $pid}}) "
        f"MERGE (s)-[:{rel_type}]->(cp)",
        {"sid": scope_id, "pid": profile_id},
    )


# ── Scope assignment resolution ─────────────────────────────────────

def resolve_scope_assignments(category: str, scope_filter: tuple[str, str] | None = None) -> dict:
    """Resolve config assignments across all known scopes.

    scope_filter, if set, restricts work to (scope_type, scope_id) where
    scope_type ∈ {"org", "collection", "site", "group"}.
    """
    stats = {"org": 0, "collection": 0, "site": 0, "group": 0, "device": 0}
    filter_type = scope_filter[0] if scope_filter else None
    filter_id = scope_filter[1] if scope_filter else None

    # Org-level
    if not filter_type or filter_type == "org":
        org_profiles = fetch_scope_profiles(category, filter_id or "org-root", "org")
        for p in org_profiles:
            pid = upsert_profile(category, p)
            if pid:
                link_profile_to_scope("Org", "scopeId", filter_id or "org-root", pid, "ORG_ASSIGNS_CONFIG")
                stats["org"] += 1

    # Site-collections
    if not filter_type or filter_type == "collection":
        if filter_id:
            collections_iter = [{"sc.scopeId": filter_id}]
        else:
            collections_iter = graph.query("MATCH (sc:SiteCollection) RETURN sc.scopeId")
        for row in collections_iter:
            sc_id = row.get("sc.scopeId", "")
            if not sc_id:
                continue
            profiles = fetch_scope_profiles(category, sc_id, "collection")
            for p in profiles:
                pid = upsert_profile(category, p)
                if pid:
                    link_profile_to_scope("SiteCollection", "scopeId", sc_id, pid,
                                          "COLLECTION_ASSIGNS_CONFIG")
                    stats["collection"] += 1

    # Sites
    if not filter_type or filter_type == "site":
        if filter_id:
            sites_iter = [{"s.scopeId": filter_id}]
        else:
            sites_iter = graph.query("MATCH (s:Site) RETURN s.scopeId")
        for row in sites_iter:
            s_id = row.get("s.scopeId", "")
            if not s_id:
                continue
            profiles = fetch_scope_profiles(category, s_id, "site")
            for p in profiles:
                pid = upsert_profile(category, p)
                if pid:
                    link_profile_to_scope("Site", "scopeId", s_id, pid, "SITE_ASSIGNS_CONFIG")
                    stats["site"] += 1

    # Device-groups
    if not filter_type or filter_type == "group":
        if filter_id:
            groups_iter = [{"dg.scopeId": filter_id}]
        else:
            groups_iter = graph.query("MATCH (dg:DeviceGroup) RETURN dg.scopeId")
        for row in groups_iter:
            dg_id = row.get("dg.scopeId", "")
            if not dg_id:
                continue
            profiles = fetch_scope_profiles(category, dg_id, "device-group")
            for p in profiles:
                pid = upsert_profile(category, p)
                if pid:
                    link_profile_to_scope("DeviceGroup", "scopeId", dg_id, pid,
                                          "GROUP_ASSIGNS_CONFIG")
                    stats["group"] += 1

    return stats


# ── Main ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Populate config policy layer.")
    parser.add_argument("--category", default=None,
                        help="Only refresh this config category (e.g. wlan-ssids).")
    parser.add_argument("--scope-id", default=None,
                        help="Restrict assignment resolution to this scope id.")
    parser.add_argument("--scope-type", default=None,
                        choices=["org", "collection", "site", "group"],
                        help="Scope type for --scope-id; required when --scope-id is set.")
    args = parser.parse_args()

    if (args.scope_id and not args.scope_type) or (args.scope_type and not args.scope_id):
        parser.error("--scope-id and --scope-type must be supplied together")
    scope_filter = (args.scope_type, args.scope_id) if args.scope_id else None

    ensure_config_tables()

    summary = {
        "categories_discovered": 0,
        "categories_with_profiles": 0,
        "total_profiles": 0,
        "scope_assignments": {},
        "filters": {
            "category": args.category,
            "scope_id": args.scope_id,
            "scope_type": args.scope_type,
        },
        "errors": [],
    }

    # Step 1: Discover categories (skipped when --category is set)
    if args.category:
        categories = [args.category]
        print(f"Using single category: {args.category}", file=sys.stderr)
    else:
        print("Discovering config categories...", file=sys.stderr)
        categories = discover_categories()
        print(f"  Found {len(categories)} categories", file=sys.stderr)
    summary["categories_discovered"] = len(categories)

    # Step 2: For each category, fetch library profiles
    for cat in categories:
        print(f"  Processing {cat}...", file=sys.stderr)

        profiles = fetch_library_profiles(cat)

        if not profiles:
            continue

        summary["categories_with_profiles"] += 1

        # Insert library-level profiles and link to Org
        for p in profiles:
            pid = upsert_profile(cat, p)
            if pid:
                link_profile_to_scope("Org", "scopeId", "org-root", pid, "ORG_ASSIGNS_CONFIG")
                # Also keep the legacy HAS_CONFIG relationship
                graph.execute(
                    "MATCH (o:Org {scopeId: 'org-root'}), (cp:ConfigProfile {id: $pid}) "
                    "MERGE (o)-[:HAS_CONFIG]->(cp)",
                    {"pid": pid},
                )
                summary["total_profiles"] += 1

        # Step 3: Resolve per-scope assignments
        stats = resolve_scope_assignments(cat, scope_filter)
        summary["scope_assignments"][cat] = stats

    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
