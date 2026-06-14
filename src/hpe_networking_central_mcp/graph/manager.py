"""GraphManager — owns the LadybugDB file-backed database and exposes query/populate/refresh."""

from __future__ import annotations

import re
import shutil
import threading
from pathlib import Path
from typing import Any

import real_ladybug as lb
import structlog

from .schema import (
    ALTER_ADD_COMPILER_PROJECTION_COLUMNS,
    ALTER_ADD_LAST_SYNCED_AT,
    HYDRATION_NODE_TABLES,
    HYDRATION_REL_TABLES,
    KNOWLEDGE_NODE_TABLES,
    KNOWLEDGE_REL_TABLES,
    NODE_TABLES,
    POLICY_REL_TABLES,
    REL_TABLES,
    TOPOLOGY_REL_TABLES,
)

logger = structlog.get_logger("graph.manager")

# Cypher keywords that mutate the graph — blocked in read-only query tool.
# LOAD FROM is included because LadybugDB can read arbitrary filesystem paths.
_WRITE_KEYWORDS = re.compile(
    r"\b(CREATE|DELETE|DETACH|SET|REMOVE|MERGE|DROP|ALTER|COPY|INSERT|LOAD|INSTALL)\b",
    re.IGNORECASE,
)


class GraphManager:
    """Manages the LadybugDB file-backed graph database lifecycle.

    The database is stored on disk so that script subprocesses can open it
    for direct reads and writes via ``central_helpers.graph``.

    Thread safety: the Database object is thread-safe; Connection is not.
    We use a threading.Lock to serialise connection access.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db: lb.Database | None = None
        self._lock = threading.Lock()
        self._fts_available: bool = False

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def fts_available(self) -> bool:
        """Return True if the FTS extension was successfully loaded."""
        return self._fts_available

    # ── Lifecycle ─────────────────────────────────────────────────

    def initialize(self) -> None:
        """Create (or open) the file-backed database and apply schema DDL."""
        logger.info("graph_init_start", db_path=str(self._db_path))
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = lb.Database(str(self._db_path))
        conn = self._get_conn()

        # Bootstrap: knowledge layer + legacy domain tables
        bootstrap_ddl = (
            NODE_TABLES + KNOWLEDGE_NODE_TABLES
            + REL_TABLES + KNOWLEDGE_REL_TABLES
            + TOPOLOGY_REL_TABLES + POLICY_REL_TABLES
            + HYDRATION_NODE_TABLES + HYDRATION_REL_TABLES
        )
        for ddl in bootstrap_ddl:
            conn.execute(ddl.strip())

        # Idempotent column-add migrations for pre-existing databases.
        # ALTER fails when the column already exists; that error is benign.
        for stmt in ALTER_ADD_LAST_SYNCED_AT + ALTER_ADD_COMPILER_PROJECTION_COLUMNS:
            _execute_idempotent_alter(conn, stmt)

        # Load the algo extension
        try:
            conn.execute("INSTALL algo")
            conn.execute("LOAD EXTENSION algo")
        except Exception as exc:
            msg = str(exc)
            lower_msg = msg.lower()
            if "already installed" in lower_msg or "already loaded" in lower_msg:
                logger.debug("algo_extension_already_loaded", error=msg)
            else:
                logger.warning(
                    "algo_extension_load_failed",
                    reason="failed to install or load algo extension",
                    error=msg,
                    exc_info=True,
                )

        # Load the FTS extension (graceful degradation if unavailable)
        try:
            conn.execute("INSTALL fts")
            conn.execute("LOAD EXTENSION fts")
            self._fts_available = True
            logger.info("fts_extension_loaded")
        except Exception as exc:
            msg = str(exc)
            lower_msg = msg.lower()
            if "already installed" in lower_msg or "already loaded" in lower_msg:
                self._fts_available = True
                logger.debug("fts_extension_already_loaded", error=msg)
            else:
                self._fts_available = False
                logger.warning(
                    "fts_extension_load_failed",
                    reason="FTS unavailable — search will fall back to CONTAINS",
                    error=msg,
                )
        logger.info(
            "graph_schema_created",
            node_tables=len(NODE_TABLES),
            rel_tables=len(REL_TABLES) + len(TOPOLOGY_REL_TABLES) + len(POLICY_REL_TABLES),
        )

        self._check_ladybug_compat(conn)

    # ── Compatibility diagnostics ─────────────────────────────────

    @staticmethod
    def _check_ladybug_compat(conn: lb.Connection) -> None:
        """Log diagnostic warnings for known LadybugDB bugs.

        This runs once at startup and reports which workarounds are still
        needed so they can be removed when upstream fixes land.
        """
        issues: list[str] = []

        # 1. STRING parameter binding with JSON-like values (segfault risk)
        try:
            conn.execute(
                "RETURN $v AS v",
                parameters={"v": '[{"name":"test"}]'},
            )
        except Exception:
            issues.append("json_string_param_binding")

        # 2. STRING[] list parameter binding
        try:
            conn.execute(
                "RETURN $v AS v",
                parameters={"v": ["a", "b"]},
            )
        except Exception:
            issues.append("list_param_binding")

        # 3. MERGE on Script nodes (planner crash)
        try:
            conn.execute("MERGE (n:Script {filename: '__compat_probe__'}) SET n.description = 'probe'")
            conn.execute("MATCH (n:Script {filename: '__compat_probe__'}) DELETE n")
        except Exception:
            issues.append("merge_script_node")
            # Clean up probe node if MERGE partially succeeded
            try:
                conn.execute("MATCH (n:Script {filename: '__compat_probe__'}) DELETE n")
            except Exception:
                pass

        if issues:
            logger.warning(
                "ladybug_compat_workarounds_still_needed",
                issues=issues,
                hint="These workarounds in tools/scripts.py can be removed when the upstream bugs are fixed.",
            )
        else:
            logger.info(
                "ladybug_compat_all_clear",
                hint="All known LadybugDB workarounds may be removable — re-test and simplify.",
            )

    @property
    def is_available(self) -> bool:
        """Return True if the database is open and ready."""
        return self._db is not None

    def reset(self) -> None:
        """Delete the database and re-initialize with empty schema."""
        logger.info("graph_reset_start")
        if self._db is not None:
            self._db.close()
            self._db = None
        if self._db_path.exists():
            if self._db_path.is_dir():
                shutil.rmtree(self._db_path)
            else:
                self._db_path.unlink()
        self.initialize()

    def replace_db(self, new_db_path: Path) -> None:
        """Replace the database directory with a pre-built one.

        Closes the current DB, replaces the directory, and reopens.
        Used to swap in a knowledge DB downloaded from a GitHub release.
        """
        logger.info("graph_replace_start", new_path=str(new_db_path))
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None
            if self._db_path.exists():
                if self._db_path.is_dir():
                    shutil.rmtree(self._db_path)
                else:
                    self._db_path.unlink()
            if new_db_path.is_dir():
                shutil.copytree(new_db_path, self._db_path)
            else:
                shutil.copy2(new_db_path, self._db_path)
            self._db = lb.Database(str(self._db_path))
        logger.info("graph_replace_done")

    def create_fts_indexes(self) -> int:
        """Create FTS indexes on searchable node tables.

        Indexes are created for:
          - ApiEndpoint: summary, description, path, operationId
          - DocSection: title, content
          - Device: name, serial, model, deviceType
          - Site: name, address, city, country
          - Script: filename, description
          - Property: name, description, yangPath (ADR-013)

        Returns the number of indexes created, or 0 if FTS is unavailable.
        """
        if not self._fts_available:
            logger.info("fts_indexes_skipped", reason="FTS extension not available")
            return 0

        conn = self._get_conn()
        fts_defs: list[tuple[str, str, list[str]]] = [
            ("api_fts", "ApiEndpoint", ["summary", "description", "path", "operationId"]),
            ("doc_fts", "DocSection", ["title", "content"]),
            ("device_fts", "Device", ["name", "serial", "model", "deviceType"]),
            ("site_fts", "Site", ["name", "address", "city", "country"]),
            ("script_fts", "Script", ["filename", "description"]),
            # Property.enumValues is STRING[]; Kuzu FTS indexes scalar columns
            # only, so we omit it and rely on ``$val IN p.enumValues`` for enum
            # lookups. Mirror of the entry in build_knowledge_db._create_fts_indexes.
            ("property_fts", "Property", ["name", "description", "yangPath"]),
        ]

        created = 0
        for idx_name, table, fields in fts_defs:
            cypher_field_list = ", ".join(f"'{f}'" for f in fields)
            try:
                conn.execute(f"CALL DROP_FTS_INDEX('{table}', '{idx_name}')")
            except Exception:
                pass  # Index may not exist yet
            try:
                conn.execute(
                    f"CALL CREATE_FTS_INDEX('{table}', '{idx_name}', [{cypher_field_list}])"
                )
                created += 1
                logger.debug("fts_index_created", index=idx_name, table=table)
            except Exception as exc:
                logger.warning("fts_index_create_failed", index=idx_name, error=str(exc))

        logger.info("fts_indexes_created", count=created)
        return created

    # ── Query ─────────────────────────────────────────────────────

    def query(self, cypher: str, params: dict | None = None, *, read_only: bool = True) -> list[dict[str, Any]]:
        """Execute a Cypher query and return results as a list of dicts.

        Args:
            cypher: Cypher query string.
            params: Optional parameter dict for parameterized queries.
            read_only: If True, reject queries containing write keywords.

        Returns:
            List of result rows as dicts.

        Raises:
            ValueError: If read_only=True and query contains write keywords.
            RuntimeError: If graph is not initialized.
        """
        if read_only and _WRITE_KEYWORDS.search(cypher):
            raise ValueError(
                "Write operations are not allowed via query_graph. "
                "Only read queries (MATCH, RETURN, WITH, WHERE, ORDER BY, LIMIT, UNION, UNWIND, CALL) are permitted."
            )

        conn = self._get_conn()
        result = conn.execute(cypher, parameters=params or {})
        return list(result.rows_as_dict())

    def execute(self, cypher: str, params: dict | None = None) -> list[dict[str, Any]]:
        """Execute a Cypher statement (including writes) and return results.

        Used by seed scripts via central_helpers.graph and by internal refresh.

        Args:
            cypher: Cypher statement.
            params: Optional parameter dict.

        Returns:
            List of result rows as dicts (empty for write statements).
        """
        conn = self._get_conn()
        result = conn.execute(cypher, parameters=params or {})
        return list(result.rows_as_dict())

    def get_schema_description(self) -> str:
        """Return a dynamic schema description by introspecting the LadybugDB catalog."""
        if self._db is None:
            return "Graph not initialized."

        conn = self._get_conn()
        lines = ["# Graph Schema — Aruba Central Configuration & Topology\n"]

        # Node tables
        lines.append("## Node Tables\n")
        lines.append("| Table | Primary Key | Properties | Row Count |")
        lines.append("|-------|-------------|------------|-----------|")
        try:
            all_tables = list(conn.execute("CALL show_tables() RETURN *").rows_as_dict())
            node_tables = [row["name"] for row in all_tables if row.get("type") == "NODE"]
        except Exception:
            node_tables = []

        for table in sorted(node_tables):
            try:
                prop_rows = list(conn.execute(f"CALL table_info('{table}') RETURN *"))
                props = []
                pk = ""
                for prow in prop_rows:
                    prop_name = prow[1]  # name
                    if prow[3]:  # isPrimaryKey
                        pk = prop_name
                    else:
                        props.append(prop_name)
                # Count rows
                count_rows = list(conn.execute(f"MATCH (n:{table}) RETURN count(n) AS cnt"))
                cnt = count_rows[0][0] if count_rows else 0
                lines.append(f"| {table} | {pk} | {', '.join(props)} | {cnt} |")
            except Exception:
                lines.append(f"| {table} | — | — | — |")

        # Relationship tables
        lines.append("\n## Relationship Tables\n")
        lines.append("| Relationship | From → To | Properties | Edge Count |")
        lines.append("|-------------|-----------|------------|------------|")
        try:
            all_tables = list(conn.execute("CALL show_tables() RETURN *").rows_as_dict())
            rel_tables = [row["name"] for row in all_tables if row.get("type") == "REL"]
        except Exception:
            rel_tables = []

        for rel in sorted(rel_tables):
            try:
                # Get connection info
                conn_rows = list(conn.execute(f"CALL show_connection('{rel}') RETURN *"))
                from_to = f"{conn_rows[0][0]} → {conn_rows[0][1]}" if conn_rows else ""
                # Get properties
                prop_rows = list(conn.execute(f"CALL table_info('{rel}') RETURN *"))
                props = [prow[1] for prow in prop_rows]
                # Count edges
                count_rows = list(conn.execute(
                    f"MATCH ()-[r:{rel}]->() RETURN count(r) AS cnt"
                ))
                cnt = count_rows[0][0] if count_rows else 0
                prop_str = ", ".join(props) if props else "—"
                lines.append(f"| {rel} | {from_to} | {prop_str} | {cnt} |")
            except Exception:
                lines.append(f"| {rel} | — | — | — |")

        # Hierarchy diagram
        lines.append("""
## Hierarchy & Configuration Model

Central uses five **configuration scopes** in a directory-style structure.
Four are hierarchy-based; one (DeviceGroup) is cross-cutting.

```
Org  (= Global scope — config here applies to ALL devices)
├── SiteCollection  (optional — regions, business units)
│   └── Site  (physical location)
│       ├── Device ──CONNECTED_TO──► Device (LLDP topology)
│       │           └──LINKED_TO──► UnmanagedDevice
│       └── UnmanagedDevice
├── Site  (standalone, not in a collection)
│   └── Device / UnmanagedDevice
├── DeviceGroup  (cross-cutting — devices from ANY site)
│   └── Device
```

### Configuration Propagation Paths
1. `Global (Org) → SiteCollection → Site → Device`
2. `DeviceGroup → Device`

### Precedence (highest wins on conflict)
`Device > DeviceGroup > Site > SiteCollection > Global`

### Effective Config (API-only)
There are no config profile nodes in the graph.  Use the Central API directly:
- Discover categories from the API endpoint catalog (`api://endpoint-catalog`)
- Read effective config: `GET network-config/v1alpha1/{category}?effective=true&detailed=true`
- The `detailed=true` response includes provenance annotations showing which scope each setting comes from.

### Useful Queries
- Devices at a site: `MATCH (s:Site {name: $n})-[:HAS_DEVICE]->(d) RETURN d.serial, d.name`
- Blast radius: `MATCH (sc:SiteCollection)-[:CONTAINS_SITE]->(s:Site)-[:HAS_DEVICE]->(d) RETURN s.name, d.serial`

## API Discovery Subgraph (ADR 009)

The graph also models the entire OpenAPI surface as first-class nodes so
`query_graph()` is the **primary tool for API discovery** — find endpoints,
inspect request/response shapes, traverse component references, and locate
required fields without ever materialising a full skeleton.

### Node tables
- `ApiEndpoint` — one row per `(method, path)`. Properties include
  `endpoint_id` (PK), `method`, `path`, `category`, `summary`,
  `description`, `operationId`, `deprecated`.
- `Parameter` — request parameters. Properties: `parameter_id` (PK),
  `endpoint_id`, `name`, `location` (path/query/header/cookie),
  `required`, `type`, `format`, `pattern`, `enumValues`,
  `inferredHint`, `description`.
- `RequestBody` — root request-body wrapper. Links to a `SchemaComponent`
  via `BODY_REFERENCES`.
- `Response` — one row per status code (e.g. `200`, `400`). Links to a
  `SchemaComponent` via `RESPONSE_REFERENCES`.
- `SchemaComponent` — every reusable OpenAPI component (schemas,
  parameters, requestBodies, responses) AND every promoted inline
  schema (oneOf/anyOf/allOf branches, additionalProperties value
  shapes, array items, nested objects). Properties: `component_id`
  (PK — canonical shape `<provider>:<section>:<Name>`, e.g.
  `central:schemas:VlanInterface`; inline-promoted branches add
  a `#` suffix such as `central:schemas:NtpprofileSchema#allOf:2`
  and live in `section='inline'`), `spec_source`, `section`
  (`schemas` / `parameters` / ... / `inline` for synthesised nodes), `name`, `type`, `kind`,
  `bodyShape` (single-word structural signature: `object`,
  `array`, `primitive`, `union-oneOf`, `union-anyOf`,
  `allOf-composite`, `map`, `unresolved`), `required`,
  `enumValues`, `supportedDeviceTypes` (component-level lift of
  `x-supportedDeviceType` — lets you slice by device type without
  descending to properties), `arrayKey` (from `x-key` on array/map-like
  schemas), `constraintsJson` (generic JSON Schema/OpenAPI constraints),
  `bodyJson` (full serialised component
  for deep inspection; empty when `kind = 'unresolved'`, i.e. an
  unresolvable `$ref` placeholder).
- `Property` — one row per leaf property of a `SchemaComponent`,
  defined **only on the component that declares it**. Inherited
  fields are gathered at query time by walking
  `(parent)-[:COMPOSED_OF*0..N]->(c)-[:HAS_PROPERTY]->(p)`.
  Properties:
  `property_id` (PK), `parent_component_id`, `name`, `type`, `format`,
  `required`, `enumValues`, `description`, `supportedDeviceTypes`
  (typed list extracted from the `x-supportedDeviceType` vendor
  extension — first-class for filtering), `yangPath` (typed extraction
  of the `x-path` vendor extension), `extensionsJson` (the **full**
  set of `x-*` vendor extensions for that property as a JSON string,
  including the typed-extracted ones), `readOnly` (boolean), plus
  compiler-projection constraint columns: `pattern`, `defaultValue`,
  `minimum`, `maximum`, `minLength`, `maxLength`,
  `enumDescriptionsJson`, and `constraintsJson`.
- `YangPath` — reverse index of every YANG path appearing on a
  `Property`. Properties: `yangPath` (PK, the full `/ac-foo:bar/...`
  string), `module` (the prefix, e.g. `ac-ntp`).

### Relationship tables
- `(ApiEndpoint)-[:HAS_PARAMETER]->(Parameter)`
- `(ApiEndpoint)-[:HAS_REQUEST_BODY]->(RequestBody)`
- `(ApiEndpoint)-[:HAS_RESPONSE]->(Response)` — one edge per status
  code (the status is stored on the `Response.status` property).
- `(RequestBody)-[:BODY_REFERENCES]->(SchemaComponent)`
- `(Response)-[:RESPONSE_REFERENCES]->(SchemaComponent)`
- `(Parameter)-[:PARAMETER_REFERENCES]->(SchemaComponent)` — when a
  parameter schema is a reusable or promoted schema component.
- `(SchemaComponent)-[:HAS_PROPERTY]->(Property)` — every leaf
  property **declared directly** on the component. Walk
  `COMPOSED_OF*0..N` first to reach inherited fields from `allOf`
  parents or promoted inline branches.
- `(Property)-[:PROPERTY_OF_TYPE]->(SchemaComponent)` — when a
  property's value is itself a named component (`$ref` or
  `items.$ref`).
- `(Property)-[:HAS_ITEM_SCHEMA]->(SchemaComponent)` — array property
  item schema. Use this for arrays; `PROPERTY_OF_TYPE` may point to the
  same component for compatibility, but `HAS_ITEM_SCHEMA` names the
  item role explicitly.
- `(SchemaComponent)-[:COMPOSED_OF {kind}]->(SchemaComponent)` —
  records `allOf` / `oneOf` / `anyOf` composition; `kind` is the
  composition keyword. Synthetic inline components (sections =
  `inline`) are linked the same way so a single `COMPOSED_OF*0..N`
  walk covers both named and promoted-inline branches.
- `(SchemaComponent)-[:HAS_VALUE_SCHEMA]->(SchemaComponent)` —
  links a map-shaped component (`additionalProperties`) to the
  component describing its values.
- `(SchemaComponent)-[:REFERENCES {via}]->(SchemaComponent)`
  — captures `$ref` edges between components; `via` is e.g.
  `"property:<name>"`, `"items:<name>"`, `"allOf"`.
- `(Property)-[:PROPERTY_AT_YANG]->(YangPath)` — fast reverse
  lookup from a YANG path string to every Property that maps to it.
- `(ApiEndpoint)-[:CONFIGURES_YANG]->(YangPath)` — derived
  edge: every endpoint whose request body (any depth, via
  `COMPOSED_OF*0..6`) reaches a property that maps to this YANG
  path. Lets you go from a CLI/YANG identifier straight to the API
  endpoints that touch it without a multi-hop traversal.

### Canned API discovery patterns

```cypher
// CANONICAL body-discovery query: every field of an endpoint's request
// body, walking through allOf/oneOf/anyOf composition AND promoted
// inline components, optionally filtered by device type. Pass
// $deviceType='' to skip the filter. Use this first when you need to
// construct or validate an API call body.
MATCH (e:ApiEndpoint {method: $m, path: $p})
      -[:HAS_REQUEST_BODY]->(:RequestBody)
      -[:BODY_REFERENCES]->(root:SchemaComponent)
MATCH (root)-[:COMPOSED_OF*0..5]->(c:SchemaComponent)
      -[:HAS_PROPERTY]->(p:Property)
WHERE $deviceType = ''
   OR p.supportedDeviceTypes IS NULL
   OR size(p.supportedDeviceTypes) = 0
   OR $deviceType IN p.supportedDeviceTypes
RETURN c.name AS host, c.bodyShape AS shape,
       p.name, p.type, p.required, p.enumValues,
       p.yangPath
ORDER BY c.name, p.required DESC, p.name
```

```cypher
// Find all GET endpoints in a category
MATCH (e:ApiEndpoint {category: 'Network Services', method: 'GET'})
RETURN e.path, e.summary
ORDER BY e.path
```

```cypher
// Required parameters for a specific endpoint
MATCH (e:ApiEndpoint {method: 'POST', path: $p})-[:HAS_PARAMETER]->(p:Parameter {required: true})
RETURN p.name, p.location, p.type, p.inferredHint
```

```cypher
// Required top-level fields of an endpoint's request body
MATCH (e:ApiEndpoint {method: 'POST', path: $p})
      -[:HAS_REQUEST_BODY]->(:RequestBody)-[:BODY_REFERENCES]->(c:SchemaComponent)
RETURN c.name, c.required
```

```cypher
// Endpoints that accept a given component as input
MATCH (c:SchemaComponent {section: 'schemas', name: $componentName})
      <-[:BODY_REFERENCES]-(:RequestBody)<-[:HAS_REQUEST_BODY]-(e:ApiEndpoint)
RETURN e.method, e.path
```

```cypher
// Walk one level of $ref from a component
MATCH (c:SchemaComponent {name: $name})-[r:REFERENCES]->(target:SchemaComponent)
RETURN r.via, target.section, target.name, target.type
```

```cypher
// Endpoints whose 200 response uses a particular component
MATCH (e:ApiEndpoint)-[:HAS_RESPONSE]->(resp:Response {status: '200'})
      -[:RESPONSE_REFERENCES]->(c:SchemaComponent {name: $name})
RETURN e.method, e.path
```

```cypher
// Headline use case: which fields of an NTP profile apply to Switch CX?
MATCH (c:SchemaComponent {name: $componentName})-[:COMPOSED_OF*0..5]->(host:SchemaComponent)-[:HAS_PROPERTY]->(p:Property)
WHERE p.supportedDeviceTypes IS NULL
   OR size(p.supportedDeviceTypes) = 0
   OR 'Switch CX' IN p.supportedDeviceTypes
RETURN host.name AS declaredOn, p.name, p.type, p.required, p.yangPath
ORDER BY p.name
```

```cypher
// Find an API field by its YANG path (helps map legacy CLI/YANG configs
// to the right OpenAPI body field)
MATCH (p:Property {yangPath: $yp})<-[:HAS_PROPERTY]-(c:SchemaComponent)
      <-[:BODY_REFERENCES]-(:RequestBody)<-[:HAS_REQUEST_BODY]-(e:ApiEndpoint)
RETURN e.method, e.path, c.name AS component, p.name AS field
```

```cypher
// Same lookup, but using the derived CONFIGURES_YANG edge (fast path):
// every endpoint that touches a given YANG path anywhere in its body.
MATCH (e:ApiEndpoint)-[:CONFIGURES_YANG]->(y:YangPath {yangPath: $yp})
RETURN DISTINCT e.method, e.path
ORDER BY e.path
```

```cypher
// Branches of a oneOf / anyOf union component (use bodyShape to
// detect unions without grep'ing bodyJson). The branches are either
// named components or synthesised `inline` ones; both are walked.
MATCH (c:SchemaComponent {name: $name})
WHERE c.bodyShape IN ['union-oneOf', 'union-anyOf']
MATCH (c)-[r:COMPOSED_OF]->(branch:SchemaComponent)
RETURN r.kind AS kind, branch.section, branch.name, branch.bodyShape
```

```cypher
// Find map-shaped components (additionalProperties) and what their
// values are constrained to.
MATCH (c:SchemaComponent)-[:HAS_VALUE_SCHEMA]->(v:SchemaComponent)
RETURN c.name AS map, v.name AS valueShape, v.bodyShape
```

```cypher
// Debug: components whose body could not be resolved (missing $ref
// target). Empty result = healthy ingestion.
MATCH (c:SchemaComponent {kind: 'unresolved'})
RETURN c.spec_source, c.section, c.name
ORDER BY c.spec_source, c.name
```

```cypher
// Show the allOf composition tree of a component
MATCH (c:SchemaComponent {name: $name})-[r:COMPOSED_OF {kind: 'allOf'}]->(b:SchemaComponent)
RETURN b.name, b.section
```

```cypher
// Inherited vs directly-defined properties on a composite component:
// walk up to 5 levels of COMPOSED_OF to find every declaring host
// component (allOf parents and promoted-inline branches).
MATCH (c:SchemaComponent {name: $name})-[:COMPOSED_OF*0..5]->(host:SchemaComponent)-[:HAS_PROPERTY]->(p:Property)
RETURN p.name, p.type, p.required,
       CASE WHEN host.component_id = c.component_id THEN 'direct' ELSE host.name END AS source
ORDER BY source, p.name
```

When you need a field-by-field guide for assembling a call body
(request or response), use the following canned patterns. Inherited
fields live on parent components; walk `COMPOSED_OF*0..N` before
`HAS_PROPERTY` to gather them.

```cypher
// Required parameters for a specific endpoint
MATCH (e:ApiEndpoint {method: $m, path: $p})-[:HAS_PARAMETER]->(param:Parameter)
RETURN param.name, param.location, param.required, param.type
ORDER BY param.required DESC, param.name
```

```cypher
// Request-body properties for an endpoint, walking composition and
// promoted-inline branches, optionally filtered by device type.
// (Pass deviceType='' to skip the filter.) This is the same canonical
// pattern shown above — repeated here for the call-construction
// workflow.
MATCH (e:ApiEndpoint {method: $m, path: $p})
      -[:HAS_REQUEST_BODY]->(:RequestBody)
      -[:BODY_REFERENCES]->(root:SchemaComponent)
MATCH (root)-[:COMPOSED_OF*0..5]->(c:SchemaComponent)
      -[:HAS_PROPERTY]->(p:Property)
WHERE $deviceType = ''
   OR p.supportedDeviceTypes IS NULL
   OR size(p.supportedDeviceTypes) = 0
   OR $deviceType IN p.supportedDeviceTypes
RETURN c.name AS host, c.bodyShape AS shape,
       p.name, p.type, p.required, p.enumValues,
       p.supportedDeviceTypes
ORDER BY c.name, p.required DESC, p.name
```

```cypher
// Required top-level fields of an endpoint's request body
MATCH (e:ApiEndpoint {method: $m, path: $p})
      -[:HAS_REQUEST_BODY]->(:RequestBody)
      -[:BODY_REFERENCES]->(c:SchemaComponent)
      -[:HAS_PROPERTY]->(p:Property {required: true})
RETURN p.name, p.type, p.enumValues
ORDER BY p.name
```

### Hierarchy & scope navigation

```cypher
// All devices at a site (by scopeId or by friendly name)
MATCH (s:Site)-[:HAS_DEVICE]->(d:Device)
WHERE s.scopeId = $siteScopeId OR s.name = $siteName
RETURN d.serial, d.name, d.deviceType, d.status, d.lastSyncedAt
ORDER BY d.name
```

```cypher
// Resolve a site/collection/group friendly name to its scopeId
MATCH (n)
WHERE (n:Site OR n:SiteCollection OR n:DeviceGroup) AND n.name = $name
RETURN labels(n)[0] AS kind, n.scopeId, n.name
```

```cypher
// Full scope chain for a device (Device <- Group/Site <- Collection <- Org)
MATCH (d:Device {serial: $serial})
OPTIONAL MATCH (g:DeviceGroup)-[:HAS_MEMBER]->(d)
OPTIONAL MATCH (s:Site)-[:HAS_DEVICE]->(d)
OPTIONAL MATCH (sc:SiteCollection)-[:CONTAINS_SITE]->(s)
OPTIONAL MATCH (org:Org)
RETURN d.serial AS serial, d.name AS device,
       g.scopeId AS deviceGroupScopeId,
       s.scopeId AS siteScopeId,
       sc.scopeId AS collectionScopeId,
       org.scopeId AS orgScopeId
```

```cypher
// All sites in a site collection
MATCH (sc:SiteCollection {scopeId: $collectionScopeId})-[:CONTAINS_SITE]->(s:Site)
RETURN s.scopeId, s.name, s.deviceCount, s.lastSyncedAt
ORDER BY s.name
```

### ApiEndpoint discovery from a fragment

```cypher
// Search the API catalog by a path fragment when you don't know the full path
MATCH (e:ApiEndpoint)
WHERE e.path CONTAINS $fragment
RETURN e.method, e.path, e.category
ORDER BY size(e.path), e.path
LIMIT 20
```

### Freshness sanity check

```cypher
// Detect stale operational data before reading it
MATCH (d:Device)
WHERE d.lastSyncedAt IS NULL
   OR d.lastSyncedAt < timestamp() - duration({minutes: 15})
RETURN count(d) AS staleDevices
```

### Reserved-keyword caveat

Cypher reserves keywords such as `in`, `from`, `to`, `where`, `order`,
`with`. Property names that collide with these (most often
`Parameter.in` for the OpenAPI parameter location) must be referenced
by string syntax, not dot syntax. Use `MATCH (p:Parameter) WHERE
p.location = 'query'` if available, otherwise quote the property with
backticks:

```cypher
MATCH (p:Parameter) RETURN p.`in` AS location
```

For everything else (cross-spec lookups, category rollups, custom
traversals) use `query_graph()` directly.

## Tips
- Use `list_scripts()` to find enrichment scripts (e.g., populate_base_graph, enrich_topology).
- Execute enrichment scripts to populate/enrich graph data on demand.
- Read `graph://schema` for up-to-date schema introspection after enrichment.
- Write operations are blocked in `query_graph()` — enrichment happens via scripts only.
- `query_graph()` accepts a `parameters` JSON arg for parameterised queries
  and applies a soft cap of 200 rows / hard cap of 2000.
""")

        return "\n".join(lines)

    # ── Internal ──────────────────────────────────────────────────

    def _get_conn(self) -> lb.Connection:
        """Get a connection, serialised via lock.

        Raises:
            RuntimeError: If the database is not initialized.
        """
        with self._lock:
            if self._db is None:
                raise RuntimeError("Graph database is not initialized.")
            return lb.Connection(self._db)


def _execute_idempotent_alter(conn: lb.Connection, stmt: str) -> None:
    try:
        conn.execute(stmt)
    except Exception as exc:
        msg = str(exc).lower()
        if "already exists" in msg or "duplicate" in msg or "already has" in msg:
            logger.debug("alter_add_column_skipped", stmt=stmt, error=str(exc))
            return
        logger.error("alter_add_column_failed", stmt=stmt, error=str(exc))
        raise
