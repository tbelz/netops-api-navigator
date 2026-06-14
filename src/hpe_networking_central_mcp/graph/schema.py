"""LadybugDB graph schema DDL for the Aruba Central configuration hierarchy.

Defines bootstrap node and relationship tables for:
  - Domain: Org, SiteCollection, Site, Device, DeviceGroup, UnmanagedDevice
  - Knowledge: ApiEndpoint, ApiCategory, DocSection, Script
  - Topology: CONNECTED_TO, LINKED_TO
  - Runtime hydration: HydrationRun, RuntimeObservation, observed objects/fields
"""

from __future__ import annotations

import re

# ── Node table DDL ───────────────────────────────────────────────────

NODE_TABLES: list[str] = [
    """
    CREATE NODE TABLE IF NOT EXISTS Org (
        scopeId STRING,
        name    STRING,
        PRIMARY KEY (scopeId)
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS SiteCollection (
        scopeId      STRING,
        name         STRING,
        siteCount    INT64,
        deviceCount  INT64,
        lastSyncedAt TIMESTAMP,
        PRIMARY KEY (scopeId)
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS Site (
        scopeId        STRING,
        name           STRING,
        address        STRING,
        city           STRING,
        country        STRING,
        state          STRING,
        zipcode        STRING,
        lat            DOUBLE,
        lon            DOUBLE,
        deviceCount    INT64,
        collectionId   STRING,
        collectionName STRING,
        timezoneId     STRING,
        lastSyncedAt   TIMESTAMP,
        PRIMARY KEY (scopeId)
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS DeviceGroup (
        scopeId      STRING,
        name         STRING,
        deviceCount  INT64,
        lastSyncedAt TIMESTAMP,
        PRIMARY KEY (scopeId)
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS Device (
        serial          STRING,
        name            STRING,
        mac             STRING,
        model           STRING,
        deviceType      STRING,
        status          STRING,
        ipv4            STRING,
        firmware        STRING,
        persona         STRING,
        deviceFunction  STRING,
        siteId          STRING,
        siteName        STRING,
        partNumber      STRING,
        deployment      STRING,
        configStatus    STRING,
        deviceGroupId   STRING,
        deviceGroupName STRING,
        lastSyncedAt    TIMESTAMP,
        PRIMARY KEY (serial)
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS UnmanagedDevice (
        mac            STRING,
        name           STRING,
        model          STRING,
        deviceType     STRING,
        health         STRING,
        status         STRING,
        ipv4           STRING,
        siteId         STRING,
        PRIMARY KEY (mac)
    )
    """,
]

# ── Knowledge layer node tables (populated by GH runner or runtime fallback) ─

KNOWLEDGE_NODE_TABLES: list[str] = [
    """
    CREATE NODE TABLE IF NOT EXISTS ApiEndpoint (
        endpoint_id    STRING,
        method         STRING,
        path           STRING,
        summary        STRING,
        description    STRING,
        operationId    STRING,
        category       STRING,
        deprecated     BOOLEAN,
        tags           STRING[],
        parameters     STRING,
        requestBody    STRING,
        responses      STRING,
        PRIMARY KEY (endpoint_id)
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS ApiCategory (
        name           STRING,
        endpointCount  INT64,
        sourceProvider STRING,
        PRIMARY KEY (name)
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS DocSection (
        section_id STRING,
        title      STRING,
        content    STRING,
        source     STRING,
        url        STRING,
        PRIMARY KEY (section_id)
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS Script (
        filename    STRING,
        description STRING,
        tags        STRING[],
        content     STRING,
        parameters  STRING,
        created_at  STRING,
        last_run    STRING,
        last_exit_code INT64,
        PRIMARY KEY (filename)
    )
    """,
    # ── Schema subgraph (ADR 009) ────────────────────────────────────
    # Decomposes per-endpoint OAS blobs into queryable Cypher entities so
    # ``query_graph`` can serve as the primary API-discovery surface.
    """
    CREATE NODE TABLE IF NOT EXISTS Parameter (
        parameter_id STRING,
        endpoint_id  STRING,
        name         STRING,
        location     STRING,
        required     BOOLEAN,
        type         STRING,
        format       STRING,
        enumValues   STRING[],
        pattern      STRING,
        inferredHint STRING,
        description  STRING,
        PRIMARY KEY (parameter_id)
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS RequestBody (
        request_body_id     STRING,
        endpoint_id         STRING,
        content_type        STRING,
        required            BOOLEAN,
        root_component_ref  STRING,
        PRIMARY KEY (request_body_id)
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS Response (
        response_id         STRING,
        endpoint_id         STRING,
        status              STRING,
        content_type        STRING,
        root_component_ref  STRING,
        PRIMARY KEY (response_id)
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS SchemaComponent (
        component_id         STRING,
        spec_source          STRING,
        section              STRING,
        name                 STRING,
        type                 STRING,
        kind                 STRING,
        bodyShape            STRING,
        required             STRING[],
        enumValues           STRING[],
        supportedDeviceTypes STRING[],
        bodyJson             STRING,
        arrayKey             STRING[],
        constraintsJson      STRING,
        PRIMARY KEY (component_id)
    )
    """,
    # ── Property-level subgraph (ADR 009 Phase 2C) ────────────────────
    # First-class node per leaf field so vendor extensions like
    # x-supportedDeviceType / x-path become Cypher-queryable. Properties
    # live only on the SchemaComponent that declares them; consumers
    # gather inherited fields by walking COMPOSED_OF*0..N before HAS_PROPERTY.
    """
    CREATE NODE TABLE IF NOT EXISTS Property (
        property_id          STRING,
        parent_component_id  STRING,
        name                 STRING,
        type                 STRING,
        format               STRING,
        required             BOOLEAN,
        enumValues           STRING[],
        description          STRING,
        supportedDeviceTypes STRING[],
        yangPath             STRING,
        extensionsJson       STRING,
        readOnly             BOOLEAN,
        pattern              STRING,
        defaultValue         STRING,
        minimum              DOUBLE,
        maximum              DOUBLE,
        minLength            INT64,
        maxLength            INT64,
        enumDescriptionsJson STRING,
        constraintsJson      STRING,
        PRIMARY KEY (property_id)
    )
    """,
    # ── YANG reverse-index (Phase 3) ────────────────────────────────
    # Lets an agent map a known YANG path (e.g. from a legacy CLI/YANG
    # config) back to the API endpoints that configure it, or to the
    # property/schema-component where it lives, in one hop.
    """
    CREATE NODE TABLE IF NOT EXISTS YangPath (
        yangPath  STRING,
        module    STRING,
        PRIMARY KEY (yangPath)
    )
    """,
    # ── CLI bridge (ADR-015) ────────────────────────────────────────
    # CliCommand: harvested from OAS ``x-cliParam`` vendor extension.
    # Lets agents resolve a CLI keyword to the ApiEndpoint that
    # configures it in one hop.
    """
    CREATE NODE TABLE IF NOT EXISTS CliCommand (
        command_id     STRING,
        commandName    STRING,
        commandUse     STRING,
        parentCommand  STRING,
        pathToPrint    STRING,
        paramKeys      STRING[],
        PRIMARY KEY (command_id)
    )
    """,
    # YangModule: derived from the ``x-path`` prefix on YangPath nodes.
    # Lets agents scope queries to a single feature area without
    # re-parsing YANG paths on every query.
    """
    CREATE NODE TABLE IF NOT EXISTS YangModule (
        module        STRING,
        PRIMARY KEY (module)
    )
    """,
]

KNOWLEDGE_REL_TABLES: list[str] = [
    "CREATE REL TABLE IF NOT EXISTS BELONGS_TO_CATEGORY (FROM ApiEndpoint TO ApiCategory)",
    # ── Schema subgraph relationships (ADR 009) ──────────────────────
    "CREATE REL TABLE IF NOT EXISTS HAS_PARAMETER (FROM ApiEndpoint TO Parameter)",
    "CREATE REL TABLE IF NOT EXISTS HAS_REQUEST_BODY (FROM ApiEndpoint TO RequestBody)",
    "CREATE REL TABLE IF NOT EXISTS HAS_RESPONSE (FROM ApiEndpoint TO Response)",
    "CREATE REL TABLE IF NOT EXISTS BODY_REFERENCES (FROM RequestBody TO SchemaComponent)",
    "CREATE REL TABLE IF NOT EXISTS RESPONSE_REFERENCES (FROM Response TO SchemaComponent)",
    "CREATE REL TABLE IF NOT EXISTS PARAMETER_REFERENCES (FROM Parameter TO SchemaComponent)",
    "CREATE REL TABLE IF NOT EXISTS REFERENCES (FROM SchemaComponent TO SchemaComponent, via STRING)",
    # ── Property-level edges (ADR 009 Phase 2C) ──────────────────────
    "CREATE REL TABLE IF NOT EXISTS HAS_PROPERTY (FROM SchemaComponent TO Property)",
    "CREATE REL TABLE IF NOT EXISTS PROPERTY_OF_TYPE (FROM Property TO SchemaComponent)",
    "CREATE REL TABLE IF NOT EXISTS HAS_ITEM_SCHEMA (FROM Property TO SchemaComponent)",
    "CREATE REL TABLE IF NOT EXISTS COMPOSED_OF (FROM SchemaComponent TO SchemaComponent, kind STRING)",
    "CREATE REL TABLE IF NOT EXISTS HAS_VALUE_SCHEMA (FROM SchemaComponent TO SchemaComponent)",
    # ── YANG reverse-index edges (Phase 3) ───────────────────────────
    "CREATE REL TABLE IF NOT EXISTS PROPERTY_AT_YANG (FROM Property TO YangPath)",
    "CREATE REL TABLE IF NOT EXISTS CONFIGURES_YANG (FROM ApiEndpoint TO YangPath)",
    # ── CLI bridge edges (ADR-015) ───────────────────────────────────
    "CREATE REL TABLE IF NOT EXISTS HAS_CLI_COMMAND (FROM ApiEndpoint TO CliCommand)",
    "CREATE REL TABLE IF NOT EXISTS IN_MODULE (FROM YangPath TO YangModule)",
]

# ── Relationship table DDL ───────────────────────────────────────────

REL_TABLES: list[str] = [
    "CREATE REL TABLE IF NOT EXISTS HAS_COLLECTION  (FROM Org TO SiteCollection)",
    "CREATE REL TABLE IF NOT EXISTS HAS_SITE        (FROM Org TO Site)",
    "CREATE REL TABLE IF NOT EXISTS CONTAINS_SITE   (FROM SiteCollection TO Site)",
    "CREATE REL TABLE IF NOT EXISTS HAS_DEVICE      (FROM Site TO Device)",
    "CREATE REL TABLE IF NOT EXISTS HAS_MEMBER      (FROM DeviceGroup TO Device)",
    "CREATE REL TABLE IF NOT EXISTS HAS_UNMANAGED   (FROM Site TO UnmanagedDevice)"
]

# Topology relationship tables — created alongside the main schema but
# populated lazily on first topology query or explicit refresh.
TOPOLOGY_REL_TABLES: list[str] = [
    """
    CREATE REL TABLE IF NOT EXISTS CONNECTED_TO (
        FROM Device TO Device,
        fromPorts STRING,
        toPorts   STRING,
        speed     DOUBLE,
        edgeType  STRING,
        health    STRING,
        lag       STRING,
        stpState  STRING,
        isSibling BOOLEAN
    )
    """,
    """
    CREATE REL TABLE IF NOT EXISTS LINKED_TO (
        FROM Device TO UnmanagedDevice,
        fromPorts STRING,
        toPorts   STRING,
        speed     DOUBLE,
        edgeType  STRING,
        health    STRING,
        lag       STRING,
        stpState  STRING,
        isSibling BOOLEAN
    )
    """,
]

# ── Config policy relationship tables ────────────────────────────────

POLICY_REL_TABLES: list[str] = []

# ── Runtime hydration foundation ────────────────────────────────────
# Generic observation/provenance layer for opt-in endpoint hydration.
# These tables intentionally do not model specific Central domains. They store
# the fact that an endpoint was called, the raw response envelope, decomposed
# observation nodes/fields, and provenance edges back to the API graph. Typed
# runtime "highways" can be materialized from these generic facts later.

HYDRATION_NODE_TABLES: list[str] = [
    """
    CREATE NODE TABLE IF NOT EXISTS HydrationRun (
        run_id          STRING,
        provider        STRING,
        endpoint_id     STRING,
        method          STRING,
        path            STRING,
        parametersJson  STRING,
        scopeJson       STRING,
        status          STRING,
        startedAt       TIMESTAMP,
        finishedAt      TIMESTAMP,
        durationMs      INT64,
        requestHash     STRING,
        responseHash    STRING,
        responseBytes   INT64,
        itemCount       INT64,
        paginationStyle STRING,
        error           STRING,
        PRIMARY KEY (run_id)
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS RuntimeObservation (
        observation_id      STRING,
        run_id              STRING,
        endpoint_id         STRING,
        provider            STRING,
        observedAt          TIMESTAMP,
        status              STRING,
        contentType         STRING,
        rootKind            STRING,
        rawJson             STRING,
        responseHash        STRING,
        responseBytes       INT64,
        itemCount           INT64,
        schema_component_id STRING,
        staleAfterSeconds   INT64,
        PRIMARY KEY (observation_id)
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS RuntimeObservedObject (
        object_id           STRING,
        observation_id      STRING,
        endpoint_id         STRING,
        jsonPointer         STRING,
        schema_component_id STRING,
        itemIndex           INT64,
        identityJson        STRING,
        valueType           STRING,
        rawJson             STRING,
        PRIMARY KEY (object_id)
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS RuntimeObservedField (
        field_id       STRING,
        object_id      STRING,
        observation_id STRING,
        endpoint_id    STRING,
        property_id    STRING,
        name           STRING,
        jsonPointer    STRING,
        valueJson      STRING,
        scalarType     STRING,
        PRIMARY KEY (field_id)
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS RuntimeFact (
        fact_id        STRING,
        observation_id STRING,
        object_id      STRING,
        endpoint_id    STRING,
        entityType     STRING,
        identityKey    STRING,
        attributesJson STRING,
        confidence     STRING,
        materializedAt TIMESTAMP,
        PRIMARY KEY (fact_id)
    )
    """,
]

HYDRATION_REL_TABLES: list[str] = [
    "CREATE REL TABLE IF NOT EXISTS CALLED_API (FROM HydrationRun TO ApiEndpoint)",
    (
        "CREATE REL TABLE IF NOT EXISTS PRODUCED_OBSERVATION "
        "(FROM HydrationRun TO RuntimeObservation)"
    ),
    (
        "CREATE REL TABLE IF NOT EXISTS OBSERVATION_OF_SCHEMA "
        "(FROM RuntimeObservation TO SchemaComponent)"
    ),
    (
        "CREATE REL TABLE IF NOT EXISTS OBSERVATION_HAS_OBJECT "
        "(FROM RuntimeObservation TO RuntimeObservedObject)"
    ),
    (
        "CREATE REL TABLE IF NOT EXISTS OBSERVED_OBJECT_SCHEMA "
        "(FROM RuntimeObservedObject TO SchemaComponent)"
    ),
    (
        "CREATE REL TABLE IF NOT EXISTS OBSERVED_OBJECT_HAS_FIELD "
        "(FROM RuntimeObservedObject TO RuntimeObservedField)"
    ),
    (
        "CREATE REL TABLE IF NOT EXISTS OBSERVED_FIELD_PROPERTY "
        "(FROM RuntimeObservedField TO Property)"
    ),
    (
        "CREATE REL TABLE IF NOT EXISTS OBSERVATION_MATERIALIZED_FACT "
        "(FROM RuntimeObservation TO RuntimeFact)"
    ),
    (
        "CREATE REL TABLE IF NOT EXISTS FACT_FROM_OBJECT "
        "(FROM RuntimeFact TO RuntimeObservedObject)"
    ),
    (
        "CREATE REL TABLE IF NOT EXISTS FACT_FROM_RUN "
        "(FROM RuntimeFact TO HydrationRun)"
    ),
    (
        "CREATE REL TABLE IF NOT EXISTS FACT_FROM_API "
        "(FROM RuntimeFact TO ApiEndpoint)"
    ),
]

# ── Helpers for dynamic property lookup (used by error hints) ────────

_PROP_RE = re.compile(r"^\s+(\w+)\s+", re.MULTILINE)
_TABLE_NAME_RE = re.compile(r"CREATE NODE TABLE IF NOT EXISTS (\w+)")


def get_node_properties() -> dict[str, list[str]]:
    """Extract {TableName: [property, ...]} from the DDL, always in sync."""
    result: dict[str, list[str]] = {}
    for ddl in NODE_TABLES + KNOWLEDGE_NODE_TABLES + HYDRATION_NODE_TABLES:
        m = _TABLE_NAME_RE.search(ddl)
        if not m:
            continue
        table = m.group(1)
        props = [p.group(1) for p in _PROP_RE.finditer(ddl)
                 if p.group(1).upper() not in ("PRIMARY", "CREATE", "FROM", "TO")]
        result[table] = props
    return result


def get_node_tables() -> list[str]:
    """Return all node table names."""
    ddl_tables = NODE_TABLES + KNOWLEDGE_NODE_TABLES + HYDRATION_NODE_TABLES
    return [m.group(1) for ddl in ddl_tables if (m := _TABLE_NAME_RE.search(ddl))]


def get_rel_tables() -> list[str]:
    """Return all relationship table names (including topology, policy, provenance)."""
    _rel_re = re.compile(r"CREATE REL TABLE (?:GROUP )?IF NOT EXISTS (\w+)")
    all_ddl = (
        REL_TABLES + KNOWLEDGE_REL_TABLES + TOPOLOGY_REL_TABLES
        + POLICY_REL_TABLES + HYDRATION_REL_TABLES
    )
    return [m.group(1) for ddl in all_ddl if (m := _rel_re.search(ddl))]


def get_rel_tables_with_endpoints() -> list[tuple[str, str, str]]:
    """Return [(rel_name, from_table, to_table), ...] parsed from DDL."""
    _rel_detail_re = re.compile(
        r"CREATE REL TABLE (?:GROUP )?IF NOT EXISTS (\w+)\s*\(\s*FROM\s+(\w+)\s+TO\s+(\w+)",
        re.IGNORECASE | re.DOTALL,
    )
    all_ddl = (
        REL_TABLES + KNOWLEDGE_REL_TABLES + TOPOLOGY_REL_TABLES
        + POLICY_REL_TABLES + HYDRATION_REL_TABLES
    )
    return [
        (m.group(1), m.group(2), m.group(3))
        for ddl in all_ddl
        if (m := _rel_detail_re.search(ddl))
    ]


# ── Freshness signalling ─────────────────────────────────────────────
# Fields whose value tracks live operational state. When a query_graph
# result projects one of these AND the node's lastSyncedAt is older than
# the freshness threshold, query_graph attaches a freshness_warnings
# block so the agent does not silently treat stale values as authoritative.
VOLATILE_FIELDS: dict[str, set[str]] = {
    "Device": {"configStatus", "status", "firmware", "ipv4"},
    "Site": {"deviceCount"},
    "DeviceGroup": {"deviceCount"},
    "SiteCollection": {"deviceCount", "siteCount"},
    "ConfigProfile": {"isDefault", "assignedScopeIds", "assignedDeviceFunctions"},
}

# Idempotent ALTER TABLE statements adding lastSyncedAt to existing graphs
# created before this column was part of the base DDL. Skipped silently when
# the column already exists.
ALTER_ADD_LAST_SYNCED_AT: list[str] = [
    "ALTER TABLE Site ADD lastSyncedAt TIMESTAMP",
    "ALTER TABLE SiteCollection ADD lastSyncedAt TIMESTAMP",
    "ALTER TABLE DeviceGroup ADD lastSyncedAt TIMESTAMP",
    "ALTER TABLE Device ADD lastSyncedAt TIMESTAMP",
]

# Compatibility ALTERs for databases built before the ADR-011 compiler
# projection columns were promoted into the bootstrap DDL. Build outputs after
# this change create the columns directly; startup applies these idempotently
# for older local/downloaded graphs.
ALTER_ADD_COMPILER_PROJECTION_COLUMNS: list[str] = [
    "ALTER TABLE SchemaComponent ADD arrayKey STRING[]",
    "ALTER TABLE SchemaComponent ADD constraintsJson STRING",
    "ALTER TABLE Property ADD pattern STRING",
    "ALTER TABLE Property ADD defaultValue STRING",
    "ALTER TABLE Property ADD minimum DOUBLE",
    "ALTER TABLE Property ADD maximum DOUBLE",
    "ALTER TABLE Property ADD minLength INT64",
    "ALTER TABLE Property ADD maxLength INT64",
    "ALTER TABLE Property ADD enumDescriptionsJson STRING",
    "ALTER TABLE Property ADD constraintsJson STRING",
]


def compact_schema_hint() -> str:
    """One-line-per-table property summary for error messages."""
    lines = []
    for table, props in get_node_properties().items():
        lines.append(f"  {table}: {', '.join(props)}")
    rels = get_rel_tables_with_endpoints()
    lines.append("  Relationships:")
    for name, src, dst in rels:
        lines.append(f"    {src} -[{name}]-> {dst}")
    return "\n".join(lines)
