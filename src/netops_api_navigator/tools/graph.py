"""MCP tools for querying and writing to the LadybugDB graph database."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

import structlog
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ..config import Settings
from ..graph.manager import GraphManager
from ..graph.schema import VOLATILE_FIELDS, compact_schema_hint, get_node_properties

logger = structlog.get_logger("tools.graph")

# Column-name shape Kuzu uses for property projections, e.g. "d.status".
_PROJ_RE = re.compile(r"^(?P<alias>[A-Za-z_][\w]*)\.(?P<prop>[A-Za-z_][\w]*)$")

# Matches Cypher node patterns like "(d:Device)" or "(s :Site {scopeId: ...})".
_ALIAS_LABEL_RE = re.compile(r"\(\s*([A-Za-z_]\w*)\s*:\s*([A-Za-z_]\w*)")

# Matches the binder hint "for <alias>" in a 'Cannot find property X for d.' message.
_FOR_ALIAS_RE = re.compile(r"\bfor\s+([A-Za-z_]\w*)\b", re.IGNORECASE)


def _parse_alias_labels(cypher: str) -> dict[str, str]:
    """Best-effort alias->label map from MATCH/CREATE/MERGE node patterns."""
    if not cypher:
        return {}
    return {m.group(1): m.group(2) for m in _ALIAS_LABEL_RE.finditer(cypher)}


def _default_stale_threshold_seconds() -> int:
    raw = os.environ.get("MCP_GRAPH_STALE_THRESHOLD_SECONDS", "900")
    try:
        return max(0, int(raw))
    except ValueError:
        return 900


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        v = int(raw)
        return v if v > 0 else default
    except ValueError:
        return default


def _per_cell_byte_cap() -> int:
    return _env_int("MCP_GRAPH_PER_CELL_BYTES", 4096)


def _per_response_byte_cap() -> int:
    return _env_int("MCP_GRAPH_PER_RESPONSE_BYTES", 50_000)


def _truncate_cell(value: str, cap: int) -> dict:
    """Replace an oversize string cell with a typed truncation envelope.

    Keeps the agent aware the value exists (and how to fetch it) instead of
    silently inlining a 100 KB JSON blob like ``bodyJson``.
    """
    # Use UTF-8 byte length — multi-byte characters would otherwise cause the
    # reported size and cap enforcement to diverge from the actual wire size.
    byte_len = len(value.encode("utf-8"))
    return {
        "_truncated": True,
        "preview": value[:200],
        "size_bytes": byte_len,
        "hint": (
            "Cell exceeded per-cell cap. Use get_raw_schema(component_id) "
            "for raw JSON, or walk COMPOSED_OF/HAS_PROPERTY to enumerate "
            "fields structurally."
        ),
    }


def _apply_per_cell_cap(rows: list[dict], cap: int) -> list[dict]:
    """Walk rows in place; replace string cells longer than ``cap`` with envelopes."""
    if not rows or cap <= 0:
        return rows
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key, val in list(row.items()):
            if isinstance(val, str) and len(val.encode("utf-8")) > cap:
                row[key] = _truncate_cell(val, cap)
    return rows


def _apply_response_byte_cap(
    rows: list[dict], cap: int, base_envelope: dict | None = None
) -> tuple[list[dict], dict | None]:
    """If the JSON-serialised rows exceed ``cap``, drop rows until they fit.

    Returns ``(rows_to_emit, envelope_or_None)``. When trimming occurs, the
    envelope is the wrapper to serialise instead of the raw row array.
    """
    def _measure(r: list[dict]) -> int:
        # Measure UTF-8 bytes of the indented form — matching what the tool
        # actually serialises — so the cap is a true byte budget, not a
        # character count of compact JSON.
        return len(json.dumps(r, default=str, indent=2).encode("utf-8"))

    if cap <= 0 or not rows:
        return rows, None
    total = _measure(rows)
    if total <= cap:
        return rows, None
    # Binary-search the largest prefix that fits under the cap.
    lo, hi = 0, len(rows)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _measure(rows[:mid]) <= cap:
            lo = mid
        else:
            hi = mid - 1
    kept = rows[:lo]
    env = {
        "truncated": True,
        "reason": "response_byte_cap",
        "cap_bytes": cap,
        "total_bytes": total,
        "rows_returned": len(kept),
        "rows_dropped": len(rows) - len(kept),
        "warning": (
            "Response exceeded byte cap. Add LIMIT/WHERE, project fewer "
            "columns, or avoid RETURN c.bodyJson; use get_raw_schema for "
            "single-component raw fetches."
        ),
        "rows": kept,
    }
    if base_envelope:
        for k, v in base_envelope.items():
            env.setdefault(k, v)
    return kept, env


def _coerce_datetime(value: object) -> datetime | None:
    """Best-effort conversion of a graph value to a timezone-aware datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def _label_from_node(node: object) -> str | None:
    """Pull the node label out of whatever shape Kuzu hands us."""
    if not isinstance(node, dict):
        return None
    for key in ("_label", "label", "_labels", "labels"):
        v = node.get(key)
        if isinstance(v, str):
            return v
        if isinstance(v, list) and v:
            return str(v[0])
    return None


def _scan_freshness(
    rows: list[dict], threshold_seconds: int, cypher: str = ""
) -> list[dict]:
    """Return freshness_warnings for any volatile fields present in rows.

    A warning is emitted when:
      - a projected column matches a known volatile field (e.g. "d.status"
        where the Device node carries lastSyncedAt), OR
      - a row column is a whole node carrying lastSyncedAt + a volatile
        property,
    AND the lastSyncedAt is older than threshold_seconds (or missing).

    Each warning aggregates by (label, age bucket) and reports the maximum
    age observed plus the volatile fields that triggered it.
    """
    if not rows:
        return []

    now = datetime.now(timezone.utc)
    # Map label -> {"volatile_in_result": set[str], "max_age": int|None, "stamped": bool, "row_ids": set[int]}
    findings: dict[str, dict] = {}

    volatile_props_by_label: dict[str, set[str]] = {
        lbl: set(fields) for lbl, fields in VOLATILE_FIELDS.items()
    }
    alias_to_label = _parse_alias_labels(cypher)

    def _record(label: str, prop: str, ts: datetime | None, row_idx: int) -> None:
        age = int((now - ts).total_seconds()) if ts else None
        if ts is not None and age is not None and age < threshold_seconds:
            return
        bucket = findings.setdefault(
            label,
            {"volatile_in_result": set(), "max_age_seconds": None,
             "stamped": True, "row_ids": set()},
        )
        bucket["volatile_in_result"].add(prop)
        bucket["row_ids"].add(row_idx)
        if ts is None:
            bucket["stamped"] = False
        elif age is not None and (
            bucket["max_age_seconds"] is None
            or age > bucket["max_age_seconds"]
        ):
            bucket["max_age_seconds"] = age

    for row_idx, row in enumerate(rows):
        if not isinstance(row, dict):
            continue

        # 1. Whole-node columns (dict carrying a label + volatile props).
        for value in row.values():
            label = _label_from_node(value)
            if not label or label not in volatile_props_by_label:
                continue
            triggered = {p for p in volatile_props_by_label[label] if p in value}
            if not triggered:
                continue
            ts = _coerce_datetime(value.get("lastSyncedAt"))
            for prop in triggered:
                _record(label, prop, ts, row_idx)

        # 2. Projected scalar columns (alias.property). Disambiguate the
        # node label via the cypher MATCH pattern when possible; otherwise
        # fall back to the unique label that owns the property.
        for col in row:
            m = _PROJ_RE.match(col)
            if not m:
                continue
            alias = m.group("alias")
            prop = m.group("prop")
            label = alias_to_label.get(alias)
            if label is not None:
                if prop not in volatile_props_by_label.get(label, set()):
                    continue
                candidate_labels = [label]
            else:
                candidate_labels = [
                    lbl for lbl, vfields in volatile_props_by_label.items()
                    if prop in vfields
                ]
                # Without an alias->label binding, only record when the
                # property name belongs to exactly one label; otherwise we
                # would have to guess.
                if len(candidate_labels) != 1:
                    continue
            lsa_col = f"{alias}.lastSyncedAt"
            ts = _coerce_datetime(row.get(lsa_col))
            for lbl in candidate_labels:
                _record(lbl, prop, ts, row_idx)

    if not findings:
        return []

    warnings: list[dict] = []
    for label, data in findings.items():
        warnings.append({
            "node_label": label,
            "volatile_fields_in_result": sorted(data["volatile_in_result"]),
            "max_age_seconds": data["max_age_seconds"],
            "lastSyncedAt_present": data["stamped"],
            "rows_affected": len(data["row_ids"]),
            "threshold_seconds": threshold_seconds,
            "recommendation": (
                f"Values for {label} fields {sorted(data['volatile_in_result'])} "
                "may be stale. For live state call the Central API directly "
                "(e.g. call_central_api on the matching monitoring endpoint); "
                f"to refresh the graph run execute_script('populate_base_graph.py'"
                + (", parameters={'site-id': '<siteScopeId>'}" if label in ('Device', 'Site') else "")
                + ")."
                + (
                    " Device rows are refreshed by re-fetching their containing"
                    " site, so pass the site's scopeId (not a device serial)."
                    if label == 'Device' else ""
                )
            ),
        })
    return warnings


def _build_error_hint(error_msg: str, cypher: str = "") -> str:
    """Build a context-aware hint from a Cypher error message.

    For "Cannot find property X for <alias>" errors we resolve <alias> back
    to the node label declared in the user's cypher (e.g. ``(d:Device)``)
    and return *that* label's properties, instead of substring-matching
    table names against the message (which would pick up the OAS-schema
    ``Property`` node table for any error containing the word "property").
    """
    msg_lower = error_msg.lower()
    is_property_err = (
        "cannot find property" in msg_lower
        or ("property" in msg_lower and "does not exist" in msg_lower)
    )

    if is_property_err:
        node_props = get_node_properties()
        alias_to_label = _parse_alias_labels(cypher)
        alias_match = _FOR_ALIAS_RE.search(error_msg)
        if alias_match:
            alias = alias_match.group(1)
            label = alias_to_label.get(alias)
            if label and label in node_props:
                return (
                    f"\n\nValid {label} properties: {', '.join(node_props[label])}\n"
                    "Read graph://schema for the full schema."
                )
        return (
            f"\n\nRead graph://schema for the full schema with node types, "
            f"properties, and relationships.\n\n{compact_schema_hint()}"
        )

    if "does not exist" in msg_lower or "cannot find" in msg_lower:
        return f"\n\nRead graph://schema for the full schema with node types, properties, and relationships.\n\n{compact_schema_hint()}"

    return ""


def register_graph_tools(mcp, settings: Settings, graph: GraphManager):
    """Register graph query and write tools with the MCP server."""

    def _run_query(cypher: str, parameters: str, tool_label: str) -> str:
        """Shared body for ``query_graph`` and its focused aliases.

        All read tools (``query_graph`` and focused aliases) delegate here.
        They differ only
        in docstring focus so each gets its own ~1500-char guidance budget
        without overflowing one giant tool description; the caps, error
        hints, and freshness scanning are identical.
        """
        if not cypher or not cypher.strip():
            raise ToolError(
                "Cypher query cannot be empty. Read graph://schema for the schema."
            )

        try:
            params = json.loads(parameters) if parameters else {}
        except json.JSONDecodeError as exc:
            raise ToolError(
                f"Invalid JSON in parameters: {exc}. "
                "Example: {\"serial\": \"SN001\"}"
            )
        if not isinstance(params, dict):
            raise ToolError("parameters must decode to a JSON object (dict).")

        try:
            rows = graph.query(cypher, params=params, read_only=True)
        except ValueError as exc:
            raise ToolError(str(exc))
        except Exception as exc:
            msg = str(exc)
            hint = _build_error_hint(msg, cypher)
            raise ToolError(f"Cypher query failed: {msg}{hint}")

        soft_cap = 200
        hard_cap = 2000
        n = len(rows)
        if n > hard_cap:
            raise ToolError(
                f"Query returned {n} rows which exceeds the hard cap of {hard_cap}. "
                "Add LIMIT or WHERE filters, or aggregate with COUNT/COLLECT."
            )

        logger.info("query_done", tool=tool_label, rows=n)
        threshold = _default_stale_threshold_seconds()
        freshness_warnings = (
            _scan_freshness(rows, threshold, cypher) if threshold > 0 else []
        )

        # Per-cell byte cap: replace oversize string cells (e.g. bodyJson)
        # with a typed truncation envelope BEFORE row-cap envelope so the
        # serialised payload stays small.
        rows = _apply_per_cell_cap(rows, _per_cell_byte_cap())

        if n > soft_cap:
            envelope = {
                "truncated": True,
                "cap": soft_cap,
                "total_returned": n,
                "warning": (
                    f"Result truncated: query returned {n} rows; only the first "
                    f"{soft_cap} are shown. Add LIMIT/WHERE or aggregate to see "
                    "the rest."
                ),
                "rows": rows[:soft_cap],
            }
            if freshness_warnings:
                envelope["freshness_warnings"] = freshness_warnings
            kept, byte_env = _apply_response_byte_cap(
                envelope["rows"], _per_response_byte_cap(),
            )
            if byte_env is not None:
                if freshness_warnings:
                    byte_env["freshness_warnings"] = freshness_warnings
                byte_env["row_cap"] = soft_cap
                byte_env["total_returned"] = n
                return json.dumps(byte_env, indent=2, default=str)
            return json.dumps(envelope, indent=2, default=str)

        kept, byte_env = _apply_response_byte_cap(rows, _per_response_byte_cap())
        if byte_env is not None:
            if freshness_warnings:
                byte_env["freshness_warnings"] = freshness_warnings
            return json.dumps(byte_env, indent=2, default=str)

        if freshness_warnings:
            return json.dumps(
                {"rows": rows, "freshness_warnings": freshness_warnings},
                indent=2,
                default=str,
            )
        return json.dumps(rows, indent=2, default=str)

    def _run_batch(queries: list[dict], tool_label: str) -> str:
        """Sequentially dispatch a list of {cypher, parameters?, label?}
        dicts via ``_run_query``, returning a single envelope.

        Continues on per-item errors. Item caps (rows, per-cell bytes,
        per-response bytes) are applied INSIDE each ``_run_query`` call
        so individual items stay bounded; a top-level byte cap then
        trims trailing items if the aggregate would exceed
        ``MCP_GRAPH_BATCH_RESPONSE_BYTES``.

        Per-item envelope::
            {"ok": bool, "label": str|None, "result": <parsed _run_query JSON>}
        or on failure::
            {"ok": false, "label": str|None, "error": str}

        Top-level envelope::
            {"batch": true, "total": N, "ok": K, "failed": N-K, "results": [...],
             "truncated"?: true, "kept_items"?: int}
        """
        max_items = _env_int("MCP_GRAPH_BATCH_MAX_ITEMS", 25)
        if not queries:
            raise ToolError("queries list is empty.")
        if not isinstance(queries, list):
            raise ToolError("queries must be a JSON array of {cypher, ...} objects.")
        if len(queries) > max_items:
            raise ToolError(
                f"queries list has {len(queries)} items but the per-batch cap "
                f"is {max_items}. Split the batch or raise MCP_GRAPH_BATCH_MAX_ITEMS."
            )

        results: list[dict] = []
        ok_count = 0
        fail_count = 0
        for i, item in enumerate(queries):
            label = None
            if not isinstance(item, dict):
                fail_count += 1
                results.append({
                    "ok": False, "label": None,
                    "error": f"item {i}: expected a JSON object, got {type(item).__name__}",
                })
                continue
            label = item.get("label")
            cypher = item.get("cypher", "")
            raw_params = item.get("parameters", "{}")
            params_str = (
                json.dumps(raw_params) if isinstance(raw_params, dict)
                else (raw_params if isinstance(raw_params, str) else "{}")
            )
            try:
                result_json = _run_query(cypher, params_str, tool_label)
                # _run_query returns a JSON-serialised payload; parse so it
                # nests cleanly in the batch envelope instead of being a
                # string-of-JSON.
                results.append({
                    "ok": True, "label": label,
                    "result": json.loads(result_json),
                })
                ok_count += 1
            except ToolError as exc:
                fail_count += 1
                results.append({"ok": False, "label": label, "error": str(exc)})
            except Exception as exc:  # defensive: never crash the batch
                fail_count += 1
                results.append({
                    "ok": False, "label": label,
                    "error": f"unexpected: {exc}",
                })

        envelope: dict = {
            "batch": True,
            "total": len(queries),
            "ok": ok_count,
            "failed": fail_count,
            "results": results,
        }
        cap_bytes = _env_int(
            "MCP_GRAPH_BATCH_RESPONSE_BYTES", 200_000,
        )
        payload = json.dumps(envelope, indent=2, default=str)
        if len(payload.encode("utf-8")) > cap_bytes:
            kept: list[dict] = []
            for r in results:
                kept.append(r)
                trial = {
                    **envelope,
                    "results": kept,
                    "truncated": True,
                    "kept_items": len(kept),
                }
                if len(json.dumps(trial, indent=2, default=str).encode("utf-8")) > cap_bytes:
                    kept.pop()
                    break
            envelope["results"] = kept
            envelope["truncated"] = True
            envelope["kept_items"] = len(kept)
            payload = json.dumps(envelope, indent=2, default=str)
        logger.info(
            "batch_done", tool=tool_label, total=len(queries),
            ok=ok_count, failed=fail_count,
            truncated=envelope.get("truncated", False),
        )
        return payload

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False),
    )
    def query_graph(cypher: str = "", parameters: str = "{}", queries: list[dict] | None = None) -> str:
        """Read-only Cypher escape hatch over the full Central graph.

        Prefer a focused alias first — each carries its own canonical
        templates and is smaller for the LLM to load:

        - ``query_api_schema`` — OpenAPI endpoints, schemas, properties, YANG.
        - ``query_fts`` — keyword search via ``CALL QUERY_FTS_INDEX(...)``.
        - ``query_topology`` — Org / SiteCollection / Site / Device /
          DeviceGroup / UnmanagedDevice and HAS_* / LINKED_TO / CONNECTED_TO.
        - ``query_runtime`` — hydrated observations, facts, entities,
          freshness, and provenance from runtime hydration.
        - ``query_yang`` — YangPath, CONFIGURES_YANG, PROPERTY_AT_YANG.

        Use ``query_graph`` when your query spans topics (e.g. topology +
        runtime facts) or uses node tables the aliases don't describe
        (``DocSection``, ``Script``, ``ApiCategory``, custom labels from
        ``write_graph``).
        For writes use ``write_graph``; for one raw schema JSON use
        ``get_raw_schema(component_id)``. Full schema: ``graph://schema``.

        Caps: rows soft 200 / hard 2000; per-cell ~4 KB; per-response ~50 KB.
        Oversize string cells return
        ``{"_truncated": true, "preview": ..., "size_bytes": N,
        "hint": "use get_raw_schema(...)"}``. Override via
        ``MCP_GRAPH_PER_CELL_BYTES`` / ``MCP_GRAPH_PER_RESPONSE_BYTES``.

        **Batch mode**: pass ``queries=[{"cypher": ..., "parameters":
        {...}, "label": "opt"}, ...]`` (cap 25 via
        ``MCP_GRAPH_BATCH_MAX_ITEMS``). Items run sequentially, continue on
        error, each wrapped in its own envelope. Top-level ``cypher`` /
        ``parameters`` are ignored when ``queries`` is set. Per-item caps
        still apply; an aggregate cap
        (``MCP_GRAPH_BATCH_RESPONSE_BYTES``, default 200 KB) drops trailing
        items on overflow.

        Args:
            cypher: Read-only Cypher (or ``CALL QUERY_FTS_INDEX``).
                Ignored when ``queries`` is set.
            parameters: JSON-encoded parameter dict (default ``"{}"``).
            queries: Optional ``[{cypher, parameters?, label?}, ...]``
                batch (cap 25).

        Returns:
            Single: JSON rows or truncation envelope. Batch:
            ``{batch: true, total, ok, failed, results: [...]}``.
        """
        if queries is not None:
            return _run_batch(queries, "query_graph")
        return _run_query(cypher, parameters, "query_graph")

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False),
    )
    def query_api_schema(cypher: str = "", parameters: str = "{}", queries: list[dict] | None = None) -> str:
        """Cypher over the OpenAPI subgraph: endpoints, schemas, properties, YANG.

        Nodes: ``ApiEndpoint``, ``Parameter``, ``RequestBody``,
        ``Response``, ``SchemaComponent``, ``Property``, ``YangPath``.
        Edges: ``HAS_*``, ``BODY_REFERENCES``,
        ``RESPONSE_REFERENCES``, ``PARAMETER_REFERENCES``, ``COMPOSED_OF``,
        ``PROPERTY_OF_TYPE``, ``HAS_ITEM_SCHEMA``, ``HAS_VALUE_SCHEMA``,
        ``PROPERTY_AT_YANG``, ``CONFIGURES_YANG``.
        Full schema and more examples: ``graph://schema``.

        First contact for a path: batch endpoint/params and top body fields:

        ```cypher
        MATCH (e:ApiEndpoint {method: $m, path: $p})
        OPTIONAL MATCH (e)-[:HAS_PARAMETER]->(param:Parameter)
        OPTIONAL MATCH (e)-[:HAS_REQUEST_BODY]->(:RequestBody)
                 -[:BODY_REFERENCES]->(root:SchemaComponent)
        OPTIONAL MATCH (root)-[:COMPOSED_OF*0..5]->(c:SchemaComponent)
                 -[:HAS_PROPERTY]->(prop:Property)
        RETURN e.operationId, param.name, param.required,
               c.name AS declaredOn, prop.name, prop.type, prop.required
        LIMIT 80
        ```

        Request-body walk. Properties live on the declaring component; use
        ``COMPOSED_OF*0..5`` for allOf/promoted inline fields:

        ```cypher
        MATCH (e:ApiEndpoint {method: $m, path: $p})
              -[:HAS_REQUEST_BODY]->(:RequestBody)
              -[:BODY_REFERENCES]->(root:SchemaComponent)
        MATCH (root)-[:COMPOSED_OF*0..5]->(c:SchemaComponent)
              -[:HAS_PROPERTY]->(p:Property)
        WHERE $deviceType IS NULL
           OR size(coalesce(p.supportedDeviceTypes, [])) = 0
           OR $deviceType IN p.supportedDeviceTypes
        OPTIONAL MATCH (p)-[:PROPERTY_OF_TYPE]->(child:SchemaComponent)
        OPTIONAL MATCH (p)-[:HAS_ITEM_SCHEMA]->(item:SchemaComponent)
        RETURN DISTINCT c.name AS declaredOn, p.name, p.type, p.required,
               p.enumValues, p.yangPath, p.constraintsJson,
               child.component_id AS childSchema,
               item.component_id AS itemSchema
        ```

        Nested object: follow ``PROPERTY_OF_TYPE``. Array item shape:
        follow ``HAS_ITEM_SCHEMA``. Recurse into the child component's tree.

        Args:
            cypher: Read-only Cypher.
            parameters: JSON-encoded parameter dict (default ``"{}"``).
            queries: Optional batch (cap 25); see ``query_graph``.
        """
        if queries is not None:
            return _run_batch(queries, "query_api_schema")
        return _run_query(cypher, parameters, "query_api_schema")

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False),
    )
    def query_fts(cypher: str = "", parameters: str = "{}", queries: list[dict] | None = None) -> str:
        """Full-text search via ``CALL QUERY_FTS_INDEX(table, index, query)``.

        Required for keyword discovery — path-grep misses cases like "vrf"
        (which lives under ``/stacks/`` — only the description mentions it).
        Each hit yields ``node`` + ``score``; chain into a structural MATCH
        in the same Cypher block. Kuzu FTS rejects ``WHERE`` directly after
        ``YIELD``; use ``WITH node, score WHERE ...``.

        Indexes (table → index → fields):

        - ``ApiEndpoint`` → ``api_fts`` → summary, description, path, operationId
        - ``DocSection`` → ``doc_fts`` → title, body
        - ``Script`` → ``script_fts`` → name, description
        - ``Property`` → ``property_fts`` → name, description, yangPath
        - ``Device`` → ``device_fts`` (runtime, populated by live seed)
        - ``Site`` → ``site_fts`` (runtime)
        - ``SchemaComponent`` → ``config_fts`` (runtime)

        ## Canonical: keyword → endpoint

        ```cypher
        CALL QUERY_FTS_INDEX('ApiEndpoint','api_fts','vrf')
        YIELD node, score
        RETURN node.method, node.path, node.summary, score
        ORDER BY score DESC LIMIT 25
        ```

        ## Canonical: keyword → property → owning component → endpoints

        Use when you know what a field does but not which schema/endpoint
        owns it.

        ```cypher
        CALL QUERY_FTS_INDEX('Property','property_fts','ntp server')
        YIELD node AS p, score
        MATCH (c:SchemaComponent)-[:HAS_PROPERTY]->(p)
        OPTIONAL MATCH (e:ApiEndpoint)
                 -[:HAS_REQUEST_BODY|:HAS_RESPONSE]->()
                 -[:BODY_REFERENCES|:RESPONSE_REFERENCES]->(root:SchemaComponent)
                 -[:COMPOSED_OF*0..5]->(c)
        RETURN p.name, p.type, c.name AS declaredOn,
               e.method, e.path, score
        ORDER BY score DESC LIMIT 25
        ```

        Lucene-style terms work (``"foo bar"``, ``foo*``, ``foo OR bar``).
        If 0 rows, fall back to ``CONTAINS``. Caps as in ``query_graph``.

        Args:
            cypher: Read-only Cypher starting with ``CALL QUERY_FTS_INDEX``.
            parameters: JSON-encoded parameter dict (default ``"{}"``).
            queries: Optional batch (cap 25); see ``query_graph``.
        """
        if queries is not None:
            return _run_batch(queries, "query_fts")
        return _run_query(cypher, parameters, "query_fts")

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False),
    )
    def query_topology(cypher: str = "", parameters: str = "{}", queries: list[dict] | None = None) -> str:
        """Cypher over the live Aruba Central network topology subgraph.

        Nodes (all carry ``lastSyncedAt TIMESTAMP``; volatile fields trigger
        ``freshness_warnings`` when stale):

        - ``Org(scopeId PK, name)``
        - ``SiteCollection(scopeId PK, name, parent_scope_id)``
        - ``Site(scopeId PK, name, address, city, country, latitude, longitude)``
        - ``Device(serial PK, name, model, deviceType, status, ipv4, mac,
          firmware, persona, deviceFunction, siteId, siteName,
          deviceGroupId, deviceGroupName, configStatus)``
        - ``DeviceGroup(scopeId PK, name, deviceCount)``
        - ``UnmanagedDevice(mac PK, name, model, deviceType, status, siteId)``

        Edges (parent → child unless noted):

        - ``(Org)-[:HAS_COLLECTION]->(SiteCollection)``
        - ``(Org)-[:HAS_SITE]->(Site)`` (standalone site)
        - ``(SiteCollection)-[:CONTAINS_SITE]->(Site)``
        - ``(Site)-[:HAS_DEVICE]->(Device)``
        - ``(Site)-[:HAS_UNMANAGED]->(UnmanagedDevice)``
        - ``(DeviceGroup)-[:HAS_MEMBER]->(Device)`` (cross-site)
        - ``(Device)-[:CONNECTED_TO]->(Device)`` — LLDP/CDP
        - ``(Device)-[:LINKED_TO]->(UnmanagedDevice)``

        ## Canonical: all devices at a site

        ```cypher
        MATCH (s:Site {name: $siteName})-[:HAS_DEVICE]->(d:Device)
        RETURN d.serial, d.name, d.model, d.deviceType, d.status
        ORDER BY d.deviceType, d.name
        ```

        ## Canonical: blast radius (device-group members across sites)

        ```cypher
        MATCH (g:DeviceGroup {name: $groupName})-[:HAS_MEMBER]->(d:Device)
        MATCH (s:Site)-[:HAS_DEVICE]->(d)
        RETURN s.name AS site, d.serial, d.name, d.model
        ORDER BY site, d.name
        ```

        ## Canonical: L2 neighbours of a switch

        ```cypher
        MATCH (d:Device {serial: $serial})-[:CONNECTED_TO]->(n:Device)
        RETURN n.serial, n.name, n.deviceType, n.model
        ```

        Freshness: selecting a volatile field (``status``, ``ipv4``,
        ``firmware``, ...) on a stale node (``lastSyncedAt`` > ~15 min)
        wraps as ``{"rows": [...], "freshness_warnings": [...]}``
        (env ``MCP_GRAPH_STALE_THRESHOLD_SECONDS``). Caps as in
        ``query_graph``.

        Args:
            cypher: Read-only Cypher.
            parameters: JSON-encoded parameter dict (default ``"{}"``).
            queries: Optional batch (cap 25); see ``query_graph``.
        """
        if queries is not None:
            return _run_batch(queries, "query_topology")
        return _run_query(cypher, parameters, "query_topology")

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False),
    )
    def query_runtime(cypher: str = "", parameters: str = "{}", queries: list[dict] | None = None) -> str:
        """Cypher over hydrated runtime state written by runtime hydration.

        Use after ``hydrate_runtime_graph`` for live observations, facts,
        entity highways, freshness, and provenance. Nodes: ``HydrationRun``,
        ``RuntimeObservation``,
        ``RuntimeObservedObject``, ``RuntimeObservedField``, ``RuntimeFact``,
        ``RuntimeEntity``. Edges: ``CALLED_API``, ``PRODUCED_OBSERVATION``,
        ``OBSERVATION_HAS_OBJECT``, ``OBSERVED_OBJECT_HAS_FIELD``,
        ``OBSERVED_FIELD_PROPERTY``, ``OBSERVATION_MATERIALIZED_FACT``,
        ``FACT_FROM_OBJECT``, ``FACT_FROM_RUN``, ``FACT_FROM_API``,
        ``ENTITY_FROM_FACT``, ``ENTITY_FROM_API``.

        Latest observed state for an endpoint:
        ```cypher
        MATCH (obs:RuntimeObservation {endpoint_id: $endpointId})
        RETURN obs.observation_id, obs.provider, obs.observedAt,
               obs.itemCount, obs.staleAfterSeconds
        ORDER BY obs.observedAt DESC LIMIT 5
        ```

        Entities with API provenance:
        ```cypher
        MATCH (ent:RuntimeEntity {entityType: $type})
              -[:ENTITY_FROM_FACT]->(fact:RuntimeFact)
              -[:FACT_FROM_RUN]->(run:HydrationRun)
        MATCH (fact)-[:FACT_FROM_API]->(api:ApiEndpoint)
        RETURN ent.identityKey, ent.latestFactId, run.finishedAt,
               api.method, api.path LIMIT 25
        ```

        Fields linked to schema properties:
        ```cypher
        MATCH (obs:RuntimeObservation {observation_id: $obs})
              -[:OBSERVATION_HAS_OBJECT]->(:RuntimeObservedObject)
              -[:OBSERVED_OBJECT_HAS_FIELD]->(f:RuntimeObservedField)
        OPTIONAL MATCH (f)-[:OBSERVED_FIELD_PROPERTY]->(p:Property)
        RETURN f.name, f.scalarType, f.valueJson, p.property_id LIMIT 100
        ```

        Args:
            cypher: Read-only Cypher.
            parameters: JSON-encoded parameter dict (default ``"{}"``).
            queries: Optional batch (cap 25); see ``query_graph``.
        """
        if queries is not None:
            return _run_batch(queries, "query_runtime")
        return _run_query(cypher, parameters, "query_runtime")

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False),
    )
    def query_yang(cypher: str = "", parameters: str = "{}", queries: list[dict] | None = None) -> str:
        """Cypher over the YANG, CLI, and config-profile reverse indexes.

        Map YANG paths or legacy CLI/config keywords to Central API
        endpoints and schema properties.

        Nodes: ``YangPath(yangPath PK, module)``,
        ``YangModule(module PK)``, ``CliCommand(command_id PK,
        commandName, commandUse, parentCommand, pathToPrint, paramKeys)``.
        ``CliCommand`` comes from OpenAPI ``x-cliParam``.

        Edges: ``PROPERTY_AT_YANG``, ``CONFIGURES_YANG``, ``IN_MODULE``,
        ``HAS_CLI_COMMAND``.

        ## Endpoint → YANG paths

        ```cypher
        MATCH (e:ApiEndpoint {method: $m, path: $p})
              -[:CONFIGURES_YANG]->(y:YangPath)
        RETURN y.yangPath, y.module ORDER BY y.yangPath
        ```

        ## YANG path → endpoints and properties

        ```cypher
        MATCH (e:ApiEndpoint)-[:CONFIGURES_YANG]->(:YangPath {yangPath: $yp})
        MATCH (p:Property)-[:PROPERTY_AT_YANG]->(:YangPath {yangPath: $yp})
        RETURN DISTINCT e.method, e.path, p.parent_component_id, p.name, p.type
        ```

        ## CLI keyword → endpoint/config profile

        ```cypher
        MATCH (e:ApiEndpoint)-[:HAS_CLI_COMMAND]->(cli:CliCommand)
        WHERE toLower(coalesce(cli.commandName,'')) CONTAINS toLower($keyword)
           OR toLower(coalesce(cli.commandUse,'')) CONTAINS toLower($keyword)
           OR toLower(coalesce(cli.parentCommand,'')) CONTAINS toLower($keyword)
        RETURN e.method, e.path, e.summary, cli.commandName, cli.commandUse,
               cli.parentCommand, cli.pathToPrint, cli.paramKeys
        LIMIT 25
        ```

        ## Property keyword → YANG → CLI + API

        ```cypher
        CALL QUERY_FTS_INDEX('Property','property_fts',$keyword)
        YIELD node AS p, score
        MATCH (p)-[:PROPERTY_AT_YANG]->(yp:YangPath)-[:IN_MODULE]->(m:YangModule)
        OPTIONAL MATCH (e:ApiEndpoint)-[:CONFIGURES_YANG]->(yp)
        OPTIONAL MATCH (e)-[:HAS_CLI_COMMAND]->(cli:CliCommand)
        RETURN p.name, p.parent_component_id, yp.yangPath, m.module,
               e.method, e.path, cli.commandName, cli.pathToPrint, score
        ORDER BY score DESC LIMIT 20
        ```

        Caps as in ``query_graph``.

        Args:
            cypher: Read-only Cypher.
            parameters: JSON-encoded parameter dict (default ``"{}"``).
            queries: Optional batch (cap 25); see ``query_graph``.
        """
        if queries is not None:
            return _run_batch(queries, "query_yang")
        return _run_query(cypher, parameters, "query_yang")

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False),
    )
    def get_raw_schema(
        component_id: str = "",
        component_ids: list[str] | None = None,
    ) -> str:
        """Fetch the raw OpenAPI ``bodyJson`` for one ``SchemaComponent`` by id.

        Escape hatch for the per-cell truncation envelope returned by
        ``query_graph`` when ``RETURN c.bodyJson`` would be too large. Prefer
        walking ``(:SchemaComponent)-[:COMPOSED_OF*0..5]->()-[:HAS_PROPERTY]->(:Property)``
        for structural exploration; reach for this tool only when you need
        the literal JSON (e.g. to read a vendor extension not surfaced as a
        graph property).

        ## Component ID format

        ``component_id`` is the canonical primary key
        ``<provider>:<section>:<Name>``, e.g.::

            central:schemas:VlanInterface
            central:parameters:TenantId
            glp:schemas:Device

        Inline-promoted branches (oneOf/anyOf/allOf items, ``additionalProperties``
        value shapes, array items) carry a ``#`` suffix and live in
        ``section='inline'``::

            central:schemas:NtpprofileSchema#allOf:2
            central:schemas:VlanInterface#prop:vlan_ids#items

        Look up the exact id first if you only know the bare name::

            MATCH (c:SchemaComponent) WHERE c.name = $name
            RETURN c.component_id, c.section LIMIT 5

        ## Batch mode

        Pass ``component_ids=[...]`` (cap 25) to fetch multiple components in
        one call; the response uses the same envelope as ``query_graph``
        batch mode: ``{"batch": true, "total", "ok", "failed", "results":
        [{"ok": bool, "component_id": str, "result"|"error": ...}, ...]}``.

        Args:
            component_id: A single ``SchemaComponent.component_id`` PK
                (ignored when ``component_ids`` is provided).
            component_ids: Optional batch of PKs (cap 25). When provided,
                ``component_id`` is ignored.

        Returns:
            JSON object ``{"component_id": ..., "name": ..., "section": ...,
            "bodyShape": ..., "bodyJson": ...}``. Over-cap bodies return
            ``{"error": ..., "size_bytes": N, "hint": "walk
            COMPOSED_OF/HAS_PROPERTY instead"}``.
        """
        if component_ids is not None:
            return _fetch_raw_schemas_batch(component_ids)
        return _fetch_raw_schema_one(component_id)

    def _fetch_raw_schema_one(component_id: str) -> str:
        if not component_id or not component_id.strip():
            raise ToolError("component_id cannot be empty.")

        try:
            rows = graph.query(
                "MATCH (c:SchemaComponent {component_id: $cid}) "
                "RETURN c.component_id AS component_id, c.name AS name, "
                "       c.section AS section, c.bodyShape AS bodyShape, "
                "       c.bodyJson AS bodyJson",
                params={"cid": component_id},
                read_only=True,
            )
        except Exception as exc:
            raise ToolError(f"Lookup failed: {exc}") from exc

        if not rows:
            raise ToolError(
                f"No SchemaComponent with component_id={component_id!r}. "
                "IDs are EXACT — the canonical shape is "
                "'<provider>:<section>:<Name>' (e.g. 'central:schemas:VlanInterface'). "
                "If you only know the bare name, look it up first: "
                "MATCH (c:SchemaComponent) WHERE c.name = $name "
                "RETURN c.component_id, c.section LIMIT 5"
            )

        row = rows[0]
        body = row.get("bodyJson") or ""
        max_blob = _env_int("MCP_GRAPH_RAW_SCHEMA_MAX_BYTES", 200_000)
        if len(body) > max_blob:
            return json.dumps(
                {
                    "error": "bodyJson exceeds raw-schema cap",
                    "component_id": row.get("component_id"),
                    "name": row.get("name"),
                    "size_bytes": len(body),
                    "cap_bytes": max_blob,
                    "hint": (
                        "Walk the property graph instead: "
                        "MATCH (root:SchemaComponent {component_id: $cid})"
                        "-[:COMPOSED_OF*0..5]->(c)-[:HAS_PROPERTY]->(p:Property) "
                        "RETURN c.name, p.name, p.type, p.required"
                    ),
                },
                indent=2,
            )

        return json.dumps(row, indent=2, default=str)

    def _fetch_raw_schemas_batch(component_ids: list[str]) -> str:
        max_items = _env_int("MCP_GRAPH_BATCH_MAX_ITEMS", 25)
        if not isinstance(component_ids, list):
            raise ToolError("component_ids must be a JSON array of strings.")
        if not component_ids:
            raise ToolError("component_ids list is empty.")
        if len(component_ids) > max_items:
            raise ToolError(
                f"component_ids has {len(component_ids)} items but the "
                f"per-batch cap is {max_items}."
            )

        results: list[dict] = []
        ok_count = 0
        fail_count = 0
        for i, cid in enumerate(component_ids):
            if not isinstance(cid, str) or not cid.strip():
                fail_count += 1
                results.append({
                    "ok": False,
                    "component_id": cid if isinstance(cid, str) else None,
                    "error": f"item {i}: component_id must be a non-empty string",
                })
                continue
            try:
                payload = _fetch_raw_schema_one(cid)
                results.append({
                    "ok": True,
                    "component_id": cid,
                    "result": json.loads(payload),
                })
                ok_count += 1
            except ToolError as exc:
                fail_count += 1
                results.append({"ok": False, "component_id": cid, "error": str(exc)})
            except Exception as exc:  # defensive
                fail_count += 1
                results.append({
                    "ok": False, "component_id": cid,
                    "error": f"unexpected: {exc}",
                })

        envelope: dict = {
            "batch": True,
            "total": len(component_ids),
            "ok": ok_count,
            "failed": fail_count,
            "results": results,
        }
        cap_bytes = _env_int("MCP_GRAPH_BATCH_RESPONSE_BYTES", 200_000)
        payload = json.dumps(envelope, indent=2, default=str)
        if len(payload.encode("utf-8")) > cap_bytes:
            kept: list[dict] = []
            for r in results:
                kept.append(r)
                trial = {
                    **envelope,
                    "results": kept,
                    "truncated": True,
                    "kept_items": len(kept),
                }
                if len(json.dumps(trial, indent=2, default=str).encode("utf-8")) > cap_bytes:
                    kept.pop()
                    break
            envelope["results"] = kept
            envelope["truncated"] = True
            envelope["kept_items"] = len(kept)
            payload = json.dumps(envelope, indent=2, default=str)
        logger.info(
            "batch_done", tool="get_raw_schema", total=len(component_ids),
            ok=ok_count, failed=fail_count,
            truncated=envelope.get("truncated", False),
        )
        return payload

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=False, openWorldHint=False),
    )
    def write_graph(cypher: str, parameters: str = "{}") -> str:
        """Execute a write Cypher statement to enrich the graph with new nodes, relationships, or properties.

        Use this to add discovered information back to the graph — for example,
        creating relationships between devices and sites after investigating
        API responses, or annotating nodes with computed properties.

        Allowed operations: CREATE, MERGE, SET, DELETE, REMOVE.
        Schema-altering statements (DROP, ALTER, CREATE NODE TABLE) are blocked.

        Args:
            cypher: A write Cypher statement.
                    Example: MATCH (d:Device {serial: 'SN001'}) SET d.customLabel = 'core-switch'
            parameters: JSON-encoded parameter dict for parameterized queries (default: "{}").
                    Example: {"serial": "SN001", "label": "core-switch"}

        Returns:
            JSON object with status and the number of rows affected.
        """
        if not cypher or not cypher.strip():
            raise ToolError("Cypher statement cannot be empty. Read graph://schema for the schema.")

        # Block schema-altering DDL
        _DDL_PATTERN = re.compile(
            r"\b(DROP|ALTER|CREATE\s+(NODE|REL)\s+TABLE|CREATE\s+INDEX|CREATE\s+CONSTRAINT)\b",
            re.IGNORECASE,
        )
        if _DDL_PATTERN.search(cypher):
            raise ToolError(
                "Schema-altering statements (DROP, ALTER, CREATE NODE/REL TABLE) are not allowed. "
                "Only data manipulation (CREATE, MERGE, SET, DELETE, REMOVE) is permitted."
            )

        # Block other side-effecting or privileged operations
        _DENY_PATTERN = re.compile(
            r"\b(LOAD|COPY|INSTALL|CALL|CREATE\s+DATABASE|DROP\s+DATABASE)\b",
            re.IGNORECASE,
        )
        if _DENY_PATTERN.search(cypher):
            raise ToolError(
                "Only data manipulation statements using CREATE, MERGE, SET, DELETE, or REMOVE are "
                "allowed. Statements using LOAD, COPY, INSTALL, CALL, or database-level DDL are "
                "not permitted in this tool."
            )

        # Require at least one allowed write keyword
        cypher_lower = cypher.lower()
        _ALLOWED_WRITE_KEYWORDS = ("create", "merge", "set", "delete", "remove")
        if not any(re.search(rf"\b{kw}\b", cypher_lower) for kw in _ALLOWED_WRITE_KEYWORDS):
            raise ToolError(
                "write_graph only permits data-manipulation statements that use CREATE, MERGE, SET, "
                "DELETE, or REMOVE (optionally after MATCH/WITH/WHERE). Other operations are not "
                "allowed."
            )

        try:
            params = json.loads(parameters)
        except json.JSONDecodeError as exc:
            raise ToolError(f"Invalid JSON in parameters: {exc}. Example: {{\"serial\": \"SN001\", \"label\": \"core-switch\"}}")

        try:
            rows = graph.execute(cypher, params=params)
        except ValueError as exc:
            raise ToolError(str(exc))
        except Exception as exc:
            msg = str(exc)
            hint = _build_error_hint(msg)
            raise ToolError(f"Cypher write failed: {msg}{hint}")

        logger.info("write_graph_done", rows_returned=len(rows))
        result: dict = {"status": "ok"}
        if rows:
            result["rows"] = rows
        return json.dumps(result, indent=2)
