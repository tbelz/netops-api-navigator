"""Build-time helper that decomposes a normalised OAS spec into the
schema subgraph defined by ADR 009.

Given a normalised spec and a list of ``(method, path)`` endpoints, this
populates the following node + relationship tables (created by
``graph/schema.py``):

  Parameter, RequestBody, Response, SchemaComponent, Property
  HAS_PARAMETER, HAS_REQUEST_BODY, HAS_RESPONSE,
  BODY_REFERENCES, RESPONSE_REFERENCES,
  HAS_PROPERTY, PROPERTY_OF_TYPE, COMPOSED_OF, REFERENCES

The helper is idempotent: rows are bulk-loaded via ``COPY FROM`` with
primary-key deduplication done in Python (``_Batch._seen_*``) before
the COPY runs, and ``populate_schema_graph`` preseeds those sets from
the DB so re-running on the same spec does not double-insert.

It does **not** create the underlying ``ApiEndpoint`` rows — those are
populated separately by ``scripts/build_knowledge_db.py``.  The helper
silently skips any endpoint whose ``ApiEndpoint`` row is missing so
build ordering does not become a hard constraint.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from typing import Any

import pyarrow as pa
import structlog

from .oas_normalize import (

    _extract_referenced_components,
    _follow_ref,
    _strip_skeleton_keys,
    schema_richness,
)

logger = structlog.get_logger("oas_schema_graph")


# ── Legacy JSON-blob decoder ────────────────────────────────────────
#
# Earlier builds tagged JSON-string columns with a ``b64:`` prefix to
# work around real_ladybug 0.15.x prepared-statement type-inference
# bugs. The current writer uses ``COPY FROM`` with PyArrow tables, so
# plain JSON is now stored directly. The decoder is kept tolerant so
# DBs built with the old encoder still read cleanly.
_JSON_BLOB_PREFIX = "b64:"


def decode_json_blob(stored: str) -> str:
    """Return the JSON text for ``stored``; tolerates legacy b64 blobs.

    A malformed legacy blob (corrupt base64 or non-UTF8 payload) falls
    back to the raw stored string so a single bad row never breaks a
    runtime describe call.
    """
    if not stored:
        return ""
    if stored.startswith(_JSON_BLOB_PREFIX):
        try:
            return base64.b64decode(
                stored[len(_JSON_BLOB_PREFIX):]
            ).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return stored
    return stored


# ── Cypher literal escaping (real_ladybug parameter-binding bug) ────
#
# Earlier builds rendered every value as a Cypher literal to bypass
# real_ladybug 0.15.x prepared-statement bugs (auto-inferred MAP from
# JSON strings, ANY[] vs STRING[] inference variance across rows, and
# an ``unordered_map::at`` crash on
# ``UNWIND $rows AS r MATCH ... MERGE rel``). The current writer uses
# ``COPY ... FROM $df`` with PyArrow tables (the documented bulk-load
# path), which avoids prepared statements AND MERGE entirely — Ladybug
# upstream issue #285 is "WONTFIX" for bulk MERGE.
#
# Nothing in this module emits MERGE statements anymore; primary-key
# deduplication happens in Python before COPY runs.


# ── Component IDs ────────────────────────────────────────────────────


def _named_component_id(spec_source: str, section: str, name: str) -> str:
    return f"{spec_source}:{section}:{name}"


def _anon_component_id(spec_source: str, body: Any) -> str:
    digest = hashlib.sha1(
        json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()[:12]
    return f"{spec_source}:anon:{digest}"


# ── Schema kind/type inference ───────────────────────────────────────


def _component_kind(body: dict) -> str:
    if not isinstance(body, dict):
        return "primitive"
    # Keep in lockstep with _compute_body_shape: enum-typed schemas are
    # leaves even when wrapped in a trivial allOf/oneOf/anyOf, otherwise
    # kind='union' would disagree with bodyShape='primitive' (INV-7).
    if isinstance(body.get("enum"), list) and body["enum"]:
        return "primitive"
    for kw in ("oneOf", "anyOf", "allOf"):
        if isinstance(body.get(kw), list) and body[kw]:
            return "union"
    t = body.get("type")
    if t == "object" or "properties" in body:
        return "object"
    if t == "array":
        return "array"
    return "primitive"


# ── Parameter hint inference ─────────────────────────────────────────


def _infer_param_hint(param: dict) -> str:
    """Return a short tag describing the semantic shape of a parameter.

    Heuristic only — falls back to '' when nothing matches.
    """
    schema = param.get("schema") or {}
    name = (param.get("name") or "").lower()
    fmt = schema.get("format") or ""
    if fmt in ("date-time", "date"):
        return f"rfc3339-{fmt}"
    if name in ("filter", "$filter") or "odata" in name:
        return "odata-filter"
    if name in ("orderby", "$orderby", "order_by"):
        return "odata-orderby"
    if "csv" in name or name.endswith("_list") or name.endswith("ids"):
        if schema.get("type") == "string":
            return "comma-list"
    if name in ("limit", "offset", "page", "page_size"):
        return "pagination"
    return ""


# ── Pick media schema (mirror oas_normalize._pick_media_schema) ─────


def _pick_media_schema(content: dict | None) -> tuple[str, dict | None]:
    if not isinstance(content, dict) or not content:
        return "", None
    if "application/json" in content:
        return "application/json", (content["application/json"] or {}).get("schema")
    media, body = next(iter(content.items()))
    return media, (body or {}).get("schema")


# ── REFERENCES walker ───────────────────────────────────────────────


_VIA_KEYS = ("properties", "items", "allOf", "oneOf", "anyOf", "additionalProperties")


def _walk_refs_with_site(node: Any, current_via: str = "") -> list[tuple[str, str]]:
    """Yield every ``$ref`` reachable from ``node`` plus the structural
    site (``properties`` | ``items`` | ``allOf`` | ``oneOf`` | ``anyOf``
    | ``additionalProperties``) it appeared under.

    The first ref encountered above any of the via keys takes
    ``current_via`` ("" for the root payload).
    """
    out: list[tuple[str, str]] = []
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            out.append((ref, current_via))
            # Do not descend through a $ref — the resolved component is
            # walked separately by the caller.
            return out
        for key, value in node.items():
            next_via = key if key in _VIA_KEYS else current_via
            out.extend(_walk_refs_with_site(value, next_via))
    elif isinstance(node, list):
        for item in node:
            out.extend(_walk_refs_with_site(item, current_via))
    return out


def _root_ref(schema: Any) -> str | None:
    """Return the top-level ``$ref`` of a schema fragment, if any."""
    if isinstance(schema, dict) and isinstance(schema.get("$ref"), str):
        return schema["$ref"]
    return None


# ── Find operation + components ─────────────────────────────────────


def _find_operation(spec: dict, method: str, path: str) -> dict | None:
    paths = spec.get("paths") or {}
    path_item = paths.get(path)
    if not isinstance(path_item, dict):
        return None
    op = path_item.get(method.lower())
    return op if isinstance(op, dict) else None


def _section_for_ref(ref: str) -> str:
    parts = ref.removeprefix("#/components/").split("/")
    return parts[0] if parts else "schemas"


def _name_for_ref(ref: str) -> str:
    parts = ref.removeprefix("#/components/").split("/")
    return parts[1] if len(parts) >= 2 else ""


# ── Public API ──────────────────────────────────────────────────────


# ── Vendor-extension columns promoted on Property nodes ────────────


_TYPED_EXT_DEVICE_TYPE = "x-supportedDeviceType"
_TYPED_EXT_YANG_PATH = "x-path"


def _collect_x_extensions(prop_body: dict) -> dict:
    """Return every ``x-*`` key from ``prop_body`` as a plain dict."""
    return {k: v for k, v in prop_body.items() if isinstance(k, str) and k.startswith("x-")}


def _resolve_property_schema(prop_body: Any, full_components: dict) -> dict:
    """Resolve a property's schema body, following one level of $ref."""
    if not isinstance(prop_body, dict):
        return {}
    ref = prop_body.get("$ref")
    if isinstance(ref, str):
        target = _follow_ref(ref, full_components)
        if isinstance(target, dict):
            return target
    return prop_body


# ── Per-spec write batch (COPY-flushed) ─────────────────────────────


class _Batch:
    """Buffers schema-subgraph writes and bulk-loads them via ``COPY FROM``.

    Node and edge rows are accumulated in plain Python lists and flushed
    in one ``COPY {table} FROM $df`` per table at ``flush()`` time.
    LadybugDB issue #285 (UNWIND+MERGE bulk insert) is WONTFIX upstream,
    so this is the only viable bulk path.

    Every collector is deduped on insert via small ``set`` indexes
    (``_seen_*``) keyed by primary key (nodes) or edge tuple (rels):
    ``COPY`` does INSERT not MERGE, so PK duplicates would otherwise
    error.  See ``_preseed_batch_from_db`` for cross-batch idempotency.
    """

    __slots__ = (
        "params",
        "request_bodies",
        "responses",
        "components",
        "properties",
        "yang_paths",
        "yang_modules",
        "cli_commands",
        "has_param",
        "has_request_body",
        "body_refs",
        "has_response",
        "response_refs",
        "has_property",
        "property_of_type",
        "has_value_schema",
        "composed_of",
        "references",
        "property_at_yang",
        "configures_yang",
        "has_cli_command",
        "in_module",
        "_seen_params",
        "_seen_request_bodies",
        "_seen_responses",
        "_seen_components",
        "_seen_properties",
        "_seen_yang_paths",
        "_seen_yang_modules",
        "_seen_cli_commands",
        "_seen_has_param",
        "_seen_has_request_body",
        "_seen_body_refs",
        "_seen_has_response",
        "_seen_response_refs",
        "_seen_has_property",
        "_seen_property_of_type",
        "_seen_has_value_schema",
        "_seen_composed_of",
        "_seen_references",
        "_seen_property_at_yang",
        "_seen_configures_yang",
        "_seen_has_cli_command",
        "_seen_in_module",
        "_components_pos",
        "_components_richness",
        "_replaced_persisted_components",
    )

    def __init__(self) -> None:
        self.params: list[dict] = []
        self.request_bodies: list[dict] = []
        self.responses: list[dict] = []
        self.components: list[dict] = []
        self.properties: list[dict] = []
        self.yang_paths: list[dict] = []
        self.yang_modules: list[dict] = []
        self.cli_commands: list[dict] = []
        self.has_param: list[dict] = []
        self.has_request_body: list[dict] = []
        self.body_refs: list[dict] = []
        self.has_response: list[dict] = []
        self.response_refs: list[dict] = []
        self.has_property: list[dict] = []
        self.property_of_type: list[dict] = []
        self.has_value_schema: list[dict] = []
        self.composed_of: list[dict] = []
        self.references: list[dict] = []
        self.property_at_yang: list[dict] = []
        self.configures_yang: list[dict] = []
        self.has_cli_command: list[dict] = []
        self.in_module: list[dict] = []
        self._seen_params: set[str] = set()
        self._seen_request_bodies: set[str] = set()
        self._seen_responses: set[str] = set()
        self._seen_components: set[str] = set()
        self._seen_properties: set[str] = set()
        self._seen_yang_paths: set[str] = set()
        self._seen_yang_modules: set[str] = set()
        self._seen_cli_commands: set[str] = set()
        self._seen_has_param: set[tuple[str, str]] = set()
        self._seen_has_request_body: set[tuple[str, str]] = set()
        self._seen_body_refs: set[tuple[str, str]] = set()
        self._seen_has_response: set[tuple[str, str]] = set()
        self._seen_response_refs: set[tuple[str, str]] = set()
        self._seen_has_property: set[tuple[str, str]] = set()
        self._seen_property_of_type: set[tuple[str, str]] = set()
        self._seen_has_value_schema: set[tuple[str, str]] = set()
        self._seen_composed_of: set[tuple[str, str, str]] = set()
        self._seen_references: set[tuple[str, str, str]] = set()
        self._seen_property_at_yang: set[tuple[str, str]] = set()
        self._seen_configures_yang: set[tuple[str, str]] = set()
        self._seen_has_cli_command: set[tuple[str, str]] = set()
        self._seen_in_module: set[tuple[str, str]] = set()
        # Richness tracking enables "richest-wins" merge across multiple
        # decompositions of the same component_id (multi-spec collision,
        # stub-then-rich ordering). See add_component.
        self._components_pos: dict[str, int] = {}
        # Component ids that were preseeded from the DB and have since
        # been superseded by a richer version: at flush time these are
        # DETACH-DELETEd from the DB before the COPY of the new rows.
        self._replaced_persisted_components: set[str] = set()
        self._components_richness: dict[str, int] = {}

    # ── Node MERGE collectors ──────────────────────────────────

    def add_parameter(self, row: dict) -> bool:
        pid = row["parameter_id"]
        if pid in self._seen_params:
            return False
        self._seen_params.add(pid)
        self.params.append(row)
        return True

    def add_request_body(self, row: dict) -> bool:
        rid = row["request_body_id"]
        if rid in self._seen_request_bodies:
            return False
        self._seen_request_bodies.add(rid)
        self.request_bodies.append(row)
        return True

    def add_response(self, row: dict) -> bool:
        rid = row["response_id"]
        if rid in self._seen_responses:
            return False
        self._seen_responses.add(rid)
        self.responses.append(row)
        return True

    def add_component(self, row: dict, *, richness: int = 0) -> str:
        """Insert / replace a SchemaComponent row with richest-wins merge.

        ``richness`` is a comparable score (typically len of the stripped
        ``bodyJson``). Returns one of:

        - ``"new"``     — row was added for the first time.
        - ``"replaced"`` — a richer body replaced a previously buffered stub.
          Callers should re-emit the property subgraph for this id.
        - ``"skipped"``  — existing buffered row is at least as rich; no change.
        """
        cid = row["component_id"]
        if cid in self._seen_components:
            prev_score = self._components_richness.get(cid, 0)
            if richness > prev_score:
                idx = self._components_pos[cid]
                if idx < 0:
                    # Previous version was already persisted to DB in
                    # an earlier flush. Mark it for deletion and append
                    # the replacement as a fresh row.
                    self._replaced_persisted_components.add(cid)
                    self._components_pos[cid] = len(self.components)
                    self.components.append(row)
                    # Evict cached property / edge keys owned by the
                    # superseded component so the richer subgraph can
                    # be re-emitted from scratch. The corresponding DB
                    # rows are purged in flush() via DETACH DELETE.
                    self._evict_component_descendants(cid)
                else:
                    # Eviction may rebuild ``self.components`` (when
                    # inline children are dropped), so call it FIRST
                    # and re-read the position before the in-place
                    # mutate. Eviction is critical: stale dedup keys
                    # for the previous (thinner) body would otherwise
                    # suppress re-emission of the richer subgraph
                    # (properties / COMPOSED_OF children / value
                    # schemas), and stale rows already appended to the
                    # row buffers would flush a union of both shapes.
                    self._evict_component_descendants(cid)
                    idx = self._components_pos[cid]
                    self.components[idx].clear()
                    self.components[idx].update(row)
                self._components_richness[cid] = richness
                return "replaced"
            return "skipped"
        self._seen_components.add(cid)
        self._components_pos[cid] = len(self.components)
        self._components_richness[cid] = richness
        self.components.append(row)
        return "new"

    def _evict_component_descendants(self, cid: str) -> None:
        """Purge cached property / edge dedup keys AND buffered rows
        owned by ``cid``.

        Called when a buffered (or DB-persisted) SchemaComponent is being
        replaced by a richer body. Two things must happen:

        1. Drop entries from the ``_seen_*`` indexes so the richer
           subgraph re-emission isn't suppressed.
        2. Remove already-appended rows from the row buffers so the
           subsequent COPY doesn't flush a stale union of the thinner
           and richer shapes.

        DB-persisted rows are wiped separately in ``flush()`` via
        DETACH DELETE for ids in ``_replaced_persisted_components``.
        """
        inline_prefix = f"{cid}#"

        # `inline_prefix` ("{cid}#") subsumes the old "{cid}#prop:" prefix AND
        # ALSO matches properties declared on inline descendants
        # (e.g. ``{cid}#allOf:0#prop:foo``). Filtering by inline_prefix alone
        # catches both direct properties and inline-descendant properties —
        # without this, HAS_PROPERTY edges from inline children survived
        # eviction (a != cid), then dropped at COPY time when the inline
        # parent SchemaComponent row was wiped (foreign-key violation). That
        # regression orphaned ~42% of Property nodes on a real-spec rebuild.
        stale_props = {
            p for p in self._seen_properties if p.startswith(inline_prefix)
        }
        self._seen_properties -= stale_props
        self._seen_has_property = {
            (a, b) for (a, b) in self._seen_has_property
            if a != cid and not a.startswith(inline_prefix)
        }
        self._seen_property_of_type = {
            (a, b) for (a, b) in self._seen_property_of_type if a not in stale_props
        }
        self._seen_property_at_yang = {
            (a, b) for (a, b) in self._seen_property_at_yang if a not in stale_props
        }
        self._seen_has_value_schema = {
            (a, b) for (a, b) in self._seen_has_value_schema
            if a != cid and not a.startswith(inline_prefix)
        }
        self._seen_composed_of = {
            (a, b, k) for (a, b, k) in self._seen_composed_of
            if a != cid and not a.startswith(inline_prefix)
        }
        self._seen_references = {
            (a, b, via) for (a, b, via) in self._seen_references
            if a != cid and not a.startswith(inline_prefix)
        }

        # Inline child SchemaComponents (e.g. {cid}#oneOf:0,
        # {cid}#additionalProperties) — wipe their cached ids so the
        # re-emit rebuilds them, and drop their component rows from
        # the buffer.
        stale_inline = {
            c for c in self._seen_components
            if c != cid and c.startswith(inline_prefix)
        }
        self._seen_components -= stale_inline
        for c in stale_inline:
            self._components_pos.pop(c, None)
            self._components_richness.pop(c, None)

        # ── Row-buffer eviction ──
        # Without this, COPY would flush both the stale stub-era rows
        # and the richer re-emission for the same parent. Compact each
        # list in place so existing index positions in _components_pos
        # for OTHER components stay valid.
        if stale_props:
            self.properties = [
                r for r in self.properties if r["property_id"] not in stale_props
            ]
        self.has_property = [
            r for r in self.has_property
            if r["a"] != cid and not r["a"].startswith(inline_prefix)
        ]
        if stale_props:
            self.property_of_type = [
                r for r in self.property_of_type if r["a"] not in stale_props
            ]
            self.property_at_yang = [
                r for r in self.property_at_yang if r["a"] not in stale_props
            ]
        self.has_value_schema = [
            r for r in self.has_value_schema
            if r["a"] != cid and not r["a"].startswith(inline_prefix)
        ]
        self.composed_of = [
            r for r in self.composed_of
            if r["a"] != cid and not r["a"].startswith(inline_prefix)
        ]
        self.references = [
            r for r in self.references
            if r["a"] != cid and not r["a"].startswith(inline_prefix)
        ]

        if stale_inline:
            # Drop inline child SchemaComponent rows from the buffer.
            # Use a 2-pass rebuild so _components_pos remains accurate
            # for the rows we keep.
            new_components: list[dict] = []
            new_pos: dict[str, int] = {}
            for row in self.components:
                rid = row.get("component_id")
                if rid in stale_inline:
                    continue
                if rid in self._components_pos:
                    new_pos[rid] = len(new_components)
                new_components.append(row)
            self.components = new_components
            # Patch only the ids we kept; others (e.g. ``cid`` itself,
            # which is being replaced by the caller right after this
            # eviction) are repositioned by add_component.
            for rid, pos in new_pos.items():
                self._components_pos[rid] = pos

    def add_property(self, row: dict) -> bool:
        pid = row["property_id"]
        if pid in self._seen_properties:
            return False
        self._seen_properties.add(pid)
        self.properties.append(row)
        return True

    # ── Edge MERGE collectors ──────────────────────────────────

    def add_has_param(self, eid: str, pid: str) -> bool:
        key = (eid, pid)
        if key in self._seen_has_param:
            return False
        self._seen_has_param.add(key)
        self.has_param.append({"a": eid, "b": pid})
        return True

    def add_has_request_body(self, eid: str, rid: str) -> bool:
        key = (eid, rid)
        if key in self._seen_has_request_body:
            return False
        self._seen_has_request_body.add(key)
        self.has_request_body.append({"a": eid, "b": rid})
        return True

    def add_body_ref(self, rid: str, cid: str) -> bool:
        key = (rid, cid)
        if key in self._seen_body_refs:
            return False
        self._seen_body_refs.add(key)
        self.body_refs.append({"a": rid, "b": cid})
        return True

    def add_has_response(self, eid: str, rid: str) -> bool:
        key = (eid, rid)
        if key in self._seen_has_response:
            return False
        self._seen_has_response.add(key)
        self.has_response.append({"a": eid, "b": rid})
        return True

    def add_response_ref(self, rid: str, cid: str) -> bool:
        key = (rid, cid)
        if key in self._seen_response_refs:
            return False
        self._seen_response_refs.add(key)
        self.response_refs.append({"a": rid, "b": cid})
        return True

    def add_has_property(self, cid: str, pid: str) -> bool:
        key = (cid, pid)
        if key in self._seen_has_property:
            return False
        self._seen_has_property.add(key)
        self.has_property.append({"a": cid, "b": pid})
        return True

    def add_property_of_type(self, pid: str, cid: str) -> bool:
        key = (pid, cid)
        if key in self._seen_property_of_type:
            return False
        self._seen_property_of_type.add(key)
        self.property_of_type.append({"a": pid, "b": cid})
        return True

    def add_composed_of(self, parent_cid: str, child_cid: str, kind: str) -> bool:
        key = (parent_cid, child_cid, kind)
        if key in self._seen_composed_of:
            return False
        self._seen_composed_of.add(key)
        self.composed_of.append({"a": parent_cid, "b": child_cid, "kind": kind})
        return True

    def add_has_value_schema(self, parent_cid: str, value_cid: str) -> bool:
        key = (parent_cid, value_cid)
        if key in self._seen_has_value_schema:
            return False
        self._seen_has_value_schema.add(key)
        self.has_value_schema.append({"a": parent_cid, "b": value_cid})
        return True

    def add_yang_path(self, yang_path: str, module: str = "") -> bool:
        if not yang_path or yang_path in self._seen_yang_paths:
            return False
        self._seen_yang_paths.add(yang_path)
        self.yang_paths.append({"yangPath": yang_path, "module": module})
        return True

    def add_property_at_yang(self, pid: str, yang_path: str) -> bool:
        key = (pid, yang_path)
        if key in self._seen_property_at_yang:
            return False
        self._seen_property_at_yang.add(key)
        self.property_at_yang.append({"a": pid, "b": yang_path})
        return True

    def add_configures_yang(self, eid: str, yang_path: str) -> bool:
        key = (eid, yang_path)
        if key in self._seen_configures_yang:
            return False
        self._seen_configures_yang.add(key)
        self.configures_yang.append({"a": eid, "b": yang_path})
        return True

    def add_yang_module(self, module: str) -> bool:
        if not module or module in self._seen_yang_modules:
            return False
        self._seen_yang_modules.add(module)
        self.yang_modules.append({"module": module})
        return True

    def add_in_module(self, yang_path: str, module: str) -> bool:
        if not yang_path or not module:
            return False
        key = (yang_path, module)
        if key in self._seen_in_module:
            return False
        self._seen_in_module.add(key)
        self.in_module.append({"a": yang_path, "b": module})
        return True

    def add_cli_command(self, row: dict) -> bool:
        cid = row.get("command_id") or ""
        if not cid or cid in self._seen_cli_commands:
            return False
        self._seen_cli_commands.add(cid)
        self.cli_commands.append(row)
        return True

    def add_has_cli_command(self, eid: str, command_id: str) -> bool:
        key = (eid, command_id)
        if key in self._seen_has_cli_command:
            return False
        self._seen_has_cli_command.add(key)
        self.has_cli_command.append({"a": eid, "b": command_id})
        return True

    def add_reference(self, parent_cid: str, child_cid: str, via: str) -> bool:
        key = (parent_cid, child_cid, via)
        if key in self._seen_references:
            return False
        self._seen_references.add(key)
        self.references.append({"a": parent_cid, "b": child_cid, "via": via})
        return True

    # ── Flush ──────────────────────────────────────────────────

    def flush(self, conn) -> None:
        """Bulk-load every collector via ``COPY ... FROM $df`` with
        in-memory PyArrow tables.

        Avoids both the real_ladybug 0.15.x prepared-statement bugs and
        the documented MERGE-via-UNWIND anti-pattern (LadybugDB issue
        #285, WONTFIX). Primary-key deduplication has already been
        done in Python by the per-batch ``_seen_*`` sets, so every
        ``COPY`` runs without ``ignore_errors=true``: that flag is a
        silent-data-loss footgun in Kuzu 0.15.x (drops valid rel rows
        with no diagnostic). If a row cannot be COPYed we want the
        build to fail loudly.

        ``COPY`` requires nodes to exist before any rel rows that
        reference them, so node tables are loaded first in dependency
        order.
        """
        # ── DB-side replacements ──
        # If a previously persisted SchemaComponent has been superseded
        # by a richer body in this batch, delete the old node and its
        # owned Property nodes (HAS_PROPERTY edges go with the node via
        # DETACH DELETE) before COPYing the replacement. Inline child
        # SchemaComponents (e.g. promoted oneOf branches) carry the
        # parent's id as a prefix; wipe those too so the re-emit is a
        # clean rebuild.
        for cid in sorted(self._replaced_persisted_components):
            try:
                conn.execute(
                    "MATCH (c:SchemaComponent) "
                    "WHERE c.component_id = $cid "
                    "   OR c.component_id STARTS WITH $prefix "
                    "OPTIONAL MATCH (c)-[:HAS_PROPERTY]->(p:Property) "
                    "DETACH DELETE c, p",
                    parameters={"cid": cid, "prefix": cid + "#"},
                )
            except Exception as exc:
                # A leftover orphan from a failed DETACH DELETE is
                # caught by the invariant audit (INV-1/8) which fails
                # the build, so we log and continue rather than abort
                # the entire flush on a single replacement glitch.
                logger.warning(
                    "schemacomponent_detach_delete_failed",
                    component_id=cid,
                    error=str(exc),
                )

        # ── Node tables (in dependency order) ──
        _copy_node_table(
            conn,
            "Parameter",
            self.params,
            schema=_PARAM_SCHEMA,
        )
        _copy_node_table(
            conn,
            "RequestBody",
            self.request_bodies,
            schema=_REQUEST_BODY_SCHEMA,
        )
        _copy_node_table(
            conn,
            "Response",
            self.responses,
            schema=_RESPONSE_SCHEMA,
        )
        _copy_node_table(
            conn,
            "SchemaComponent",
            self.components,
            schema=_COMPONENT_SCHEMA,
        )
        _copy_node_table(
            conn,
            "Property",
            self.properties,
            schema=_PROPERTY_SCHEMA,
        )
        _copy_node_table(
            conn,
            "YangPath",
            self.yang_paths,
            schema=_YANG_PATH_SCHEMA,
        )
        _copy_node_table(
            conn,
            "YangModule",
            self.yang_modules,
            schema=_YANG_MODULE_SCHEMA,
        )
        _copy_node_table(
            conn,
            "CliCommand",
            self.cli_commands,
            schema=_CLI_COMMAND_SCHEMA,
        )

        # ── Rel tables (require both endpoints to be loaded) ──
        _copy_rel_table(conn, "HAS_PARAMETER", self.has_param, _REL_AB_SCHEMA)
        _copy_rel_table(conn, "HAS_REQUEST_BODY", self.has_request_body, _REL_AB_SCHEMA)
        _copy_rel_table(conn, "BODY_REFERENCES", self.body_refs, _REL_AB_SCHEMA)
        _copy_rel_table(conn, "HAS_RESPONSE", self.has_response, _REL_AB_SCHEMA)
        _copy_rel_table(conn, "RESPONSE_REFERENCES", self.response_refs, _REL_AB_SCHEMA)
        _copy_rel_table(conn, "HAS_PROPERTY", self.has_property, _REL_AB_SCHEMA)
        _copy_rel_table(conn, "PROPERTY_OF_TYPE", self.property_of_type, _REL_AB_SCHEMA)
        _copy_rel_table(conn, "HAS_VALUE_SCHEMA", self.has_value_schema, _REL_AB_SCHEMA)
        _copy_rel_table(
            conn, "COMPOSED_OF", self.composed_of, _REL_COMPOSED_OF_SCHEMA
        )
        _copy_rel_table(
            conn, "REFERENCES", self.references, _REL_REFERENCES_SCHEMA
        )
        _copy_rel_table(conn, "PROPERTY_AT_YANG", self.property_at_yang, _REL_AB_SCHEMA)
        _copy_rel_table(conn, "CONFIGURES_YANG", self.configures_yang, _REL_AB_SCHEMA)
        _copy_rel_table(conn, "HAS_CLI_COMMAND", self.has_cli_command, _REL_AB_SCHEMA)
        _copy_rel_table(conn, "IN_MODULE", self.in_module, _REL_AB_SCHEMA)


# ── PyArrow schemas (column order MUST match graph/schema.py DDL) ───

_PARAM_SCHEMA = pa.schema([
    ("parameter_id", pa.string()),
    ("endpoint_id", pa.string()),
    ("name", pa.string()),
    ("location", pa.string()),
    ("required", pa.bool_()),
    ("type", pa.string()),
    ("format", pa.string()),
    ("enumValues", pa.list_(pa.string())),
    ("pattern", pa.string()),
    ("inferredHint", pa.string()),
    ("description", pa.string()),
])

_REQUEST_BODY_SCHEMA = pa.schema([
    ("request_body_id", pa.string()),
    ("endpoint_id", pa.string()),
    ("content_type", pa.string()),
    ("required", pa.bool_()),
    ("root_component_ref", pa.string()),
])

_RESPONSE_SCHEMA = pa.schema([
    ("response_id", pa.string()),
    ("endpoint_id", pa.string()),
    ("status", pa.string()),
    ("content_type", pa.string()),
    ("root_component_ref", pa.string()),
])

_COMPONENT_SCHEMA = pa.schema([
    ("component_id", pa.string()),
    ("spec_source", pa.string()),
    ("section", pa.string()),
    ("name", pa.string()),
    ("type", pa.string()),
    ("kind", pa.string()),
    ("bodyShape", pa.string()),
    ("required", pa.list_(pa.string())),
    ("enumValues", pa.list_(pa.string())),
    ("supportedDeviceTypes", pa.list_(pa.string())),
    ("bodyJson", pa.string()),
    ("arrayKey", pa.list_(pa.string())),
    ("constraintsJson", pa.string()),
])

_PROPERTY_SCHEMA = pa.schema([
    ("property_id", pa.string()),
    ("parent_component_id", pa.string()),
    ("name", pa.string()),
    ("type", pa.string()),
    ("format", pa.string()),
    ("required", pa.bool_()),
    ("enumValues", pa.list_(pa.string())),
    ("description", pa.string()),
    ("supportedDeviceTypes", pa.list_(pa.string())),
    ("yangPath", pa.string()),
    ("extensionsJson", pa.string()),
    ("readOnly", pa.bool_()),
    ("pattern", pa.string()),
    ("defaultValue", pa.string()),
    ("minimum", pa.float64()),
    ("maximum", pa.float64()),
    ("minLength", pa.int64()),
    ("maxLength", pa.int64()),
    ("enumDescriptionsJson", pa.string()),
    ("constraintsJson", pa.string()),
])

_YANG_PATH_SCHEMA = pa.schema([
    ("yangPath", pa.string()),
    ("module", pa.string()),
])

_YANG_MODULE_SCHEMA = pa.schema([
    ("module", pa.string()),
])

_CLI_COMMAND_SCHEMA = pa.schema([
    ("command_id", pa.string()),
    ("commandName", pa.string()),
    ("commandUse", pa.string()),
    ("parentCommand", pa.string()),
    ("pathToPrint", pa.string()),
    ("paramKeys", pa.list_(pa.string())),
])

# Rel schemas: first two columns are FROM/TO PKs; extra cols follow.
_REL_AB_SCHEMA = pa.schema([
    ("a", pa.string()),
    ("b", pa.string()),
])

_REL_COMPOSED_OF_SCHEMA = pa.schema([
    ("a", pa.string()),
    ("b", pa.string()),
    ("kind", pa.string()),
])

_REL_REFERENCES_SCHEMA = pa.schema([
    ("a", pa.string()),
    ("b", pa.string()),
    ("via", pa.string()),
])


def _rows_to_pa(rows: list[dict], schema: pa.Schema) -> pa.Table:
    """Materialise a list[dict] as a PyArrow table conforming to ``schema``.

    Missing keys are coerced to type-appropriate empty values
    (``""`` / ``[]`` / ``False``) for most columns. ``supportedDeviceTypes``
    is the exception: ``None`` is preserved (yielding SQL NULL) so
    consumers can distinguish "applies to all device types" (NULL) from
    "explicitly restricted to no device types" (empty list).
    Column order follows ``schema``.
    """
    cols: dict[str, list] = {f.name: [] for f in schema}
    for r in rows:
        for f in schema:
            v = r.get(f.name)
            if v is None and f.name != "supportedDeviceTypes":
                if pa.types.is_string(f.type):
                    v = ""
                elif pa.types.is_list(f.type):
                    v = []
                elif pa.types.is_boolean(f.type):
                    v = False
                elif pa.types.is_floating(f.type) or pa.types.is_integer(f.type):
                    v = None
            cols[f.name].append(v)
    return pa.table(cols, schema=schema)


def _copy_node_table(
    conn,
    table: str,
    rows: list[dict],
    *,
    schema: pa.Schema,
) -> None:
    """Bulk-load a node table from buffered rows."""
    if not rows:
        return
    pa_table = _rows_to_pa(rows, schema)
    conn.execute(
        f"COPY {table} FROM $df",
        parameters={"df": pa_table},
    )


def _copy_rel_table(
    conn,
    table: str,
    rows: list[dict],
    schema: pa.Schema,
) -> None:
    """Bulk-load a rel table from buffered rows.

    Schema must list FROM column first, TO column second, then any
    edge properties.
    """
    if not rows:
        return
    pa_table = _rows_to_pa(rows, schema)
    conn.execute(
        f"COPY {table} FROM $df",
        parameters={"df": pa_table},
    )


def _empty_stats() -> dict:
    return {
        "endpoints": 0,
        "parameters": 0,
        "request_bodies": 0,
        "responses": 0,
        "components": 0,
        "properties": 0,
        "references": 0,
    }


def _query_existing_eids(conn, requested_eids: list[str]) -> set[str]:
    """One-shot lookup of which ApiEndpoint IDs already exist."""
    if not requested_eids:
        return set()
    rows = conn.execute(
        "UNWIND $eids AS eid MATCH (e:ApiEndpoint {endpoint_id: eid}) "
        "RETURN e.endpoint_id AS eid",
        parameters={"eids": requested_eids},
    ).rows_as_dict()
    return {r["eid"] for r in rows}


def _collect_spec_into_batch(
    batch: "_Batch",
    *,
    spec_source: str,
    spec: dict,
    endpoints: list[tuple[str, str]],
    existing_eids: set[str],
    stats: dict,
    emit_property_subgraph: bool = True,
    resolution_scope: dict | None = None,
) -> None:
    """Pure-Python collection: walk one spec, append rows to ``batch``.

    No DB I/O. The caller is responsible for flushing the batch via
    ``batch.flush(conn)`` (or ``flush_batch`` below) after all
    interesting specs have been collected.

    ``resolution_scope`` is an optional provider-wide ``components``
    dict used as a fallback when the local spec doesn't define a
    referenced component (cross-spec $ref). When omitted, only the
    spec's own components are consulted.
    """
    components = spec.get("components") or {}

    for method, path in endpoints:
        method_u = method.upper()
        op = _find_operation(spec, method_u, path)
        if op is None:
            continue
        eid = f"{method_u}:{path}"
        if eid not in existing_eids:
            continue
        stats["endpoints"] += 1

        # ── CLI bridge (vendor extension ``x-cliParam``) ──
        # Aruba Central OAS specs may annotate an operation with the
        # CLI command that maps to the same configuration. Surface
        # this as a graph node so agents can resolve CLI ↔ API in
        # a single MATCH instead of grepping spec files.
        cli = op.get("x-cliParam")
        if isinstance(cli, dict):
            command_name = str(cli.get("commandName") or "").strip()
            if command_name:
                raw_param_keys = cli.get("paramKeys") or []
                param_keys: list[str] = []
                if isinstance(raw_param_keys, list):
                    for entry in raw_param_keys:
                        if isinstance(entry, str):
                            param_keys.append(entry)
                        elif isinstance(entry, dict):
                            k = entry.get("key")
                            if isinstance(k, str):
                                param_keys.append(k)
                command_id = f"{eid}::{command_name}"
                if batch.add_cli_command({
                    "command_id": command_id,
                    "commandName": command_name,
                    "commandUse": str(cli.get("commandUse") or ""),
                    "parentCommand": str(cli.get("parentCommand") or ""),
                    "pathToPrint": str(cli.get("pathToPrint") or ""),
                    "paramKeys": param_keys,
                }):
                    stats["cli_commands"] = stats.get("cli_commands", 0) + 1
                batch.add_has_cli_command(eid, command_id)

        # ── Parameters ──
        for idx, raw_p in enumerate(op.get("parameters") or []):
            if not isinstance(raw_p, dict):
                continue
            resolved = raw_p
            if "$ref" in raw_p:
                target = _follow_ref(raw_p["$ref"], components, fallback=resolution_scope)
                if isinstance(target, dict):
                    resolved = target
            schema = resolved.get("schema") or {}
            pname = resolved.get("name") or ""
            ploc = resolved.get("in") or ""
            # Use ``@{idx}`` for unnamed params so a numeric name like
            # "5" can never collide with the synthesised positional id.
            param_id = f"{eid}#param:{ploc}:{pname or f'@{idx}'}"
            enum_vals = [
                v for v in (schema.get("enum") or []) if isinstance(v, str)
            ]
            batch.add_parameter({
                "parameter_id": param_id,
                "endpoint_id": eid,
                "name": pname,
                "location": ploc,
                "required": bool(resolved.get("required", False)),
                "type": str(schema.get("type") or ""),
                "format": str(schema.get("format") or ""),
                "enumValues": enum_vals,
                "pattern": str(schema.get("pattern") or ""),
                "inferredHint": _infer_param_hint(resolved),
                "description": str(resolved.get("description") or ""),
            })
            batch.add_has_param(eid, param_id)
            stats["parameters"] += 1

        # ── Request body ──
        rb = op.get("requestBody")
        if isinstance(rb, dict):
            rb_resolved = rb
            if "$ref" in rb:
                target = _follow_ref(rb["$ref"], components, fallback=resolution_scope)
                if isinstance(target, dict):
                    rb_resolved = target
            content = rb_resolved.get("content") or {}
            media, schema = _pick_media_schema(content)
            rb_id = f"{eid}#requestBody"
            root_ref = _root_ref(schema) or ""
            if not root_ref:
                logger.warning(
                    "requestbody_without_schema_root",
                    endpoint_id=eid,
                    method=method,
                    path=path,
                    content_types=list(content.keys()),
                )
            batch.add_request_body({
                "request_body_id": rb_id,
                "endpoint_id": eid,
                "content_type": media,
                "required": bool(rb_resolved.get("required", False)),
                "root_component_ref": root_ref,
            })
            batch.add_has_request_body(eid, rb_id)
            stats["request_bodies"] += 1
            if root_ref:
                comp_id = _ensure_component_node(
                    batch, spec_source, root_ref, components, stats,
                    emit_property_subgraph=emit_property_subgraph,
                    resolution_scope=resolution_scope,
                )
                if comp_id:
                    batch.add_body_ref(rb_id, comp_id)

        # ── Responses ──
        for status, resp in (op.get("responses") or {}).items():
            if not isinstance(resp, dict):
                continue
            resp_resolved = resp
            if "$ref" in resp:
                target = _follow_ref(resp["$ref"], components, fallback=resolution_scope)
                if isinstance(target, dict):
                    resp_resolved = target
            content = resp_resolved.get("content") or {}
            media, schema = _pick_media_schema(content)
            resp_id = f"{eid}#response:{status}"
            root_ref = _root_ref(schema) or ""
            batch.add_response({
                "response_id": resp_id,
                "endpoint_id": eid,
                "status": str(status),
                "content_type": media,
                "root_component_ref": root_ref,
            })
            batch.add_has_response(eid, resp_id)
            stats["responses"] += 1
            if root_ref:
                comp_id = _ensure_component_node(
                    batch, spec_source, root_ref, components, stats,
                    emit_property_subgraph=emit_property_subgraph,
                    resolution_scope=resolution_scope,
                )
                if comp_id:
                    batch.add_response_ref(resp_id, comp_id)

        # ── REFERENCES walker ──
        refs_root: dict[str, Any] = {
            "parameters": op.get("parameters") or [],
            "requestBody": op.get("requestBody"),
            "responses": op.get("responses") or {},
        }
        side = _extract_referenced_components(
            refs_root, components, fallback=resolution_scope
        )
        for section, entries in side.items():
            if not isinstance(entries, dict):
                continue
            for name, body in entries.items():
                ref = f"#/components/{section}/{name}"
                comp_id = _ensure_component_node(
                    batch, spec_source, ref, components, stats,
                    emit_property_subgraph=emit_property_subgraph,
                    resolution_scope=resolution_scope,
                )
                if not comp_id:
                    continue
                for child_ref, via in _walk_refs_with_site(body):
                    if not child_ref.startswith("#/components/"):
                        continue
                    child_id = _ensure_component_node(
                        batch, spec_source, child_ref, components, stats,
                        emit_property_subgraph=emit_property_subgraph,
                        resolution_scope=resolution_scope,
                    )
                    if not child_id or child_id == comp_id:
                        continue
                    if batch.add_reference(comp_id, child_id, via):
                        stats["references"] += 1


# ── Public API ──────────────────────────────────────────────────────


# Public alias so callers (build script, tests) can import a typed Batch.
SchemaGraphBatch = _Batch


def new_batch() -> "_Batch":
    """Return a fresh schema-graph batch for use with ``collect_into_batch``."""
    return _Batch()


def collect_into_batch(
    batch: "_Batch",
    *,
    spec_source: str,
    spec: dict,
    endpoints: list[tuple[str, str]],
    existing_eids: set[str],
    emit_property_subgraph: bool = True,
    resolution_scope: dict | None = None,
) -> dict:
    """Collect schema-subgraph rows for ONE spec into ``batch`` (no DB I/O).

    ``existing_eids`` must be a pre-computed set of ApiEndpoint IDs
    that already exist in the DB. The caller filters with
    ``query_existing_eids(conn, [...])`` before the loop so the build
    script doesn't issue one MATCH query per spec.

    ``resolution_scope`` is an optional provider-wide components dict
    used as a fallback when the local spec doesn't define a referenced
    component (cross-spec $ref). The build script builds this in
    Pass A of the two-pass populate.

    Returns per-spec stats; the build script logs these to provide ETA.
    """
    stats = _empty_stats()
    _collect_spec_into_batch(
        batch,
        spec_source=spec_source,
        spec=spec,
        endpoints=endpoints,
        existing_eids=existing_eids,
        stats=stats,
        emit_property_subgraph=emit_property_subgraph,
        resolution_scope=resolution_scope,
    )
    return stats


def query_existing_eids(conn, eids: list[str]) -> set[str]:
    """Public helper: which of ``eids`` exist in the ApiEndpoint table."""
    return _query_existing_eids(conn, eids)


def _preseed_batch_from_db(conn, batch: "_Batch") -> None:
    """Populate ``batch._seen_*`` sets from existing DB rows so a
    re-run of ``populate_schema_graph`` becomes idempotent.

    Only used by the per-spec wrapper. The build path runs against an
    empty DB and skips this entirely.
    """
    # Nodes
    for r in conn.execute("MATCH (p:Parameter) RETURN p.parameter_id AS k").rows_as_dict():
        batch._seen_params.add(r["k"])
    for r in conn.execute("MATCH (rb:RequestBody) RETURN rb.request_body_id AS k").rows_as_dict():
        batch._seen_request_bodies.add(r["k"])
    for r in conn.execute("MATCH (resp:Response) RETURN resp.response_id AS k").rows_as_dict():
        batch._seen_responses.add(r["k"])
    for r in conn.execute(
        "MATCH (c:SchemaComponent) RETURN c.component_id AS k, c.bodyJson AS body"
    ).rows_as_dict():
        cid = r["k"]
        batch._seen_components.add(cid)
        # Track richness so a subsequent in-batch richer body can still
        # win; position is -1 because the row is in the DB, not in
        # ``self.components``. If a richer body is later re-emitted the
        # replace path (``_replaced_persisted_components``) DETACH-DELETEs
        # the stale node before COPYing the new one — see ``flush()``.
        batch._components_pos[cid] = -1
        batch._components_richness[cid] = len(r.get("body") or "")
    for r in conn.execute("MATCH (p:Property) RETURN p.property_id AS k").rows_as_dict():
        batch._seen_properties.add(r["k"])
    for r in conn.execute("MATCH (y:YangPath) RETURN y.yangPath AS k").rows_as_dict():
        batch._seen_yang_paths.add(r["k"])
    # Rels
    for r in conn.execute(
        "MATCH (e:ApiEndpoint)-[:HAS_PARAMETER]->(p:Parameter) "
        "RETURN e.endpoint_id AS a, p.parameter_id AS b"
    ).rows_as_dict():
        batch._seen_has_param.add((r["a"], r["b"]))
    for r in conn.execute(
        "MATCH (e:ApiEndpoint)-[:HAS_REQUEST_BODY]->(rb:RequestBody) "
        "RETURN e.endpoint_id AS a, rb.request_body_id AS b"
    ).rows_as_dict():
        batch._seen_has_request_body.add((r["a"], r["b"]))
    for r in conn.execute(
        "MATCH (rb:RequestBody)-[:BODY_REFERENCES]->(c:SchemaComponent) "
        "RETURN rb.request_body_id AS a, c.component_id AS b"
    ).rows_as_dict():
        batch._seen_body_refs.add((r["a"], r["b"]))
    for r in conn.execute(
        "MATCH (e:ApiEndpoint)-[:HAS_RESPONSE]->(resp:Response) "
        "RETURN e.endpoint_id AS a, resp.response_id AS b"
    ).rows_as_dict():
        batch._seen_has_response.add((r["a"], r["b"]))
    for r in conn.execute(
        "MATCH (resp:Response)-[:RESPONSE_REFERENCES]->(c:SchemaComponent) "
        "RETURN resp.response_id AS a, c.component_id AS b"
    ).rows_as_dict():
        batch._seen_response_refs.add((r["a"], r["b"]))
    for r in conn.execute(
        "MATCH (c:SchemaComponent)-[:HAS_PROPERTY]->(p:Property) "
        "RETURN c.component_id AS a, p.property_id AS b"
    ).rows_as_dict():
        batch._seen_has_property.add((r["a"], r["b"]))
    for r in conn.execute(
        "MATCH (p:Property)-[:PROPERTY_OF_TYPE]->(c:SchemaComponent) "
        "RETURN p.property_id AS a, c.component_id AS b"
    ).rows_as_dict():
        batch._seen_property_of_type.add((r["a"], r["b"]))
    for r in conn.execute(
        "MATCH (a:SchemaComponent)-[r:COMPOSED_OF]->(b:SchemaComponent) "
        "RETURN a.component_id AS a, b.component_id AS b, r.kind AS kind"
    ).rows_as_dict():
        batch._seen_composed_of.add((r["a"], r["b"], r["kind"]))
    for r in conn.execute(
        "MATCH (a:SchemaComponent)-[r:REFERENCES]->(b:SchemaComponent) "
        "RETURN a.component_id AS a, b.component_id AS b, r.via AS via"
    ).rows_as_dict():
        batch._seen_references.add((r["a"], r["b"], r["via"]))
    for r in conn.execute(
        "MATCH (a:SchemaComponent)-[:HAS_VALUE_SCHEMA]->(b:SchemaComponent) "
        "RETURN a.component_id AS a, b.component_id AS b"
    ).rows_as_dict():
        batch._seen_has_value_schema.add((r["a"], r["b"]))
    for r in conn.execute(
        "MATCH (p:Property)-[:PROPERTY_AT_YANG]->(y:YangPath) "
        "RETURN p.property_id AS a, y.yangPath AS b"
    ).rows_as_dict():
        batch._seen_property_at_yang.add((r["a"], r["b"]))
    for r in conn.execute(
        "MATCH (e:ApiEndpoint)-[:CONFIGURES_YANG]->(y:YangPath) "
        "RETURN e.endpoint_id AS a, y.yangPath AS b"
    ).rows_as_dict():
        batch._seen_configures_yang.add((r["a"], r["b"]))
    for r in conn.execute("MATCH (m:YangModule) RETURN m.module AS k").rows_as_dict():
        batch._seen_yang_modules.add(r["k"])
    for r in conn.execute(
        "MATCH (y:YangPath)-[:IN_MODULE]->(m:YangModule) "
        "RETURN y.yangPath AS a, m.module AS b"
    ).rows_as_dict():
        batch._seen_in_module.add((r["a"], r["b"]))
    for r in conn.execute("MATCH (c:CliCommand) RETURN c.command_id AS k").rows_as_dict():
        batch._seen_cli_commands.add(r["k"])
    for r in conn.execute(
        "MATCH (e:ApiEndpoint)-[:HAS_CLI_COMMAND]->(c:CliCommand) "
        "RETURN e.endpoint_id AS a, c.command_id AS b"
    ).rows_as_dict():
        batch._seen_has_cli_command.add((r["a"], r["b"]))


def flush_batch(conn, batch: "_Batch") -> None:
    """Flush an accumulated batch to the DB via COPY FROM PyArrow."""
    batch.flush(conn)


def populate_schema_graph(
    conn,
    *,
    spec_source: str,
    spec: dict,
    endpoints: list[tuple[str, str]],
    emit_property_subgraph: bool = True,
    resolution_scope: dict | None = None,
) -> dict:
    """Decompose ``spec`` into schema-subgraph rows for ``endpoints``
    and bulk-load them via ``COPY ... FROM $df``.

    Convenience wrapper that creates a batch, looks up existing
    endpoint IDs, collects rows, and flushes — useful for tests and
    one-off populations. The build script uses ``collect_into_batch`` +
    ``flush_batch`` so all 1000+ specs share a single global batch and
    one COPY per table.

    Returns a stats dict with per-table counts. Endpoints whose
    ``ApiEndpoint`` row is missing are silently skipped.
    """
    stats = _empty_stats()
    requested_eids = [f"{m.upper()}:{p}" for m, p in endpoints]
    if not requested_eids:
        return stats
    existing_eids = _query_existing_eids(conn, requested_eids)
    batch = _Batch()
    _preseed_batch_from_db(conn, batch)
    _collect_spec_into_batch(
        batch,
        spec_source=spec_source,
        spec=spec,
        endpoints=endpoints,
        existing_eids=existing_eids,
        stats=stats,
        emit_property_subgraph=emit_property_subgraph,
        resolution_scope=resolution_scope,
    )
    batch.flush(conn)
    return stats



# ── Component shape / device-type / yang helpers ────────────────────


def _compute_body_shape(body: dict) -> str:
    """Single-word structural signature of a SchemaComponent body.

    Stable across spec versions; lets agents filter `WHERE c.bodyShape =
    'union-oneOf'` without grepping bodyJson. Falls back to "primitive".
    """
    if not isinstance(body, dict):
        return "primitive"
    # Enum-typed schemas are primitive scalars even if they carry a
    # trivial allOf/oneOf wrapper for inheritance metadata. The
    # allowed values are fully described by `enum`, so classifying
    # them as composites would force property-edge emission for what
    # is really a leaf type.
    if isinstance(body.get("enum"), list) and body["enum"]:
        return "primitive"
    if isinstance(body.get("oneOf"), list) and body["oneOf"]:
        return "union-oneOf"
    if isinstance(body.get("anyOf"), list) and body["anyOf"]:
        return "union-anyOf"
    if isinstance(body.get("allOf"), list) and body["allOf"]:
        return "allOf-composite"
    if isinstance(body.get("properties"), dict) and body["properties"]:
        return "object"
    # ``additionalProperties`` may legitimately be ``{}`` (free-form map of
    # any), which is falsy in Python; treat anything other than absent/False
    # as a map shape so HAS_VALUE_SCHEMA emission and bodyShape stay aligned.
    ap = body.get("additionalProperties")
    if ap is True or isinstance(ap, dict):
        return "map"
    t = body.get("type")
    if t == "object":
        return "object"
    if t == "array":
        return "array"
    return "primitive"


def _lift_supported_device_types(body: dict) -> list[str] | None:
    """Lift ``x-supportedDeviceType`` from a component body, if present.

    Many Aruba Central CX configuration schemas carry the device-type
    annotation at the schema-component root in addition to (or instead
    of) on each leaf property. Surfacing it on the SchemaComponent lets
    callers slice ``MATCH (c:SchemaComponent) WHERE 'Switch CX' IN
    c.supportedDeviceTypes`` without descending into properties.

    Returns ``None`` when the extension is absent so that consumers can
    distinguish "applies to all device types" (NULL) from "explicitly
    no device types" (empty list). An empty list in the source spec is
    preserved as ``[]``.
    """
    if not isinstance(body, dict):
        return None
    if "x-supportedDeviceType" not in body:
        return None
    raw = body.get("x-supportedDeviceType")
    if isinstance(raw, list):
        return [v for v in raw if isinstance(v, str)]
    if isinstance(raw, str):
        return [raw]
    return None


def _yang_module_for(yang_path: str) -> str:
    """Extract the YANG module name from a path like ``/ac-ntp:ntp/...``."""
    if not isinstance(yang_path, str) or not yang_path:
        return ""
    for part in yang_path.split("/"):
        if ":" in part:
            return part.split(":", 1)[0]
    return ""


def _richness(body_json: str) -> int:
    """Comparable richness score for richest-wins merge.

    Bodies are scored on the JSON-serialised payload length so the
    metric matches :func:`netops_api_navigator.oas_normalize.schema_richness`
    used during ref resolution.
    """
    return len(body_json or "")


# ── Component MERGE (buffered) ──────────────────────────────────────


def _ensure_component_node(
    batch: _Batch,
    spec_source: str,
    ref: str,
    full_components: dict,
    stats: dict,
    emit_property_subgraph: bool = True,
    *,
    resolution_scope: dict | None = None,
) -> str:
    """Buffer the SchemaComponent row + its property subgraph.

    Returns the component_id (deterministic per provider+section+name)
    so callers can wire up edges.

    Resolution order: local ``full_components`` first, then the
    provider-wide ``resolution_scope`` fallback (multi-spec ref
    handling). When neither resolves the ref we still emit a
    placeholder ``UnresolvedRef``-style node so consumers can detect
    and report missing definitions instead of silently dropping the
    edge.

    Richest-wins: if the component was already buffered (e.g. from an
    earlier spec where it was a stub), and the current body is richer
    (longer serialised), the row is replaced in-place and the property
    subgraph is re-emitted.
    """
    if not ref.startswith("#/components/"):
        return ""
    section = _section_for_ref(ref)
    name = _name_for_ref(ref)
    if not section or not name:
        return ""
    comp_id = _named_component_id(spec_source, section, name)

    body = _follow_ref(ref, full_components, fallback=resolution_scope)
    if not isinstance(body, dict):
        # Emit a placeholder so the graph still has the node and any
        # incoming BODY_REFERENCES / COMPOSED_OF edges land somewhere.
        # A future spec that *does* define this component will replace
        # the placeholder via richest-wins.
        if comp_id not in batch._seen_components:
            batch.add_component(
                {
                    "component_id": comp_id,
                    "spec_source": spec_source,
                    "section": section,
                    "name": name,
                    "type": "",
                    "kind": "unresolved",
                    "bodyShape": "unresolved",
                    "required": [],
                    "enumValues": [],
                    "supportedDeviceTypes": None,
                    "bodyJson": "",
                },
                richness=0,
            )
            stats["components"] += 1
        return comp_id

    stripped = _strip_skeleton_keys(body)
    body_json = json.dumps(stripped)
    enum_vals = [v for v in (body.get("enum") or []) if isinstance(v, str)]
    _req = body.get("required")
    required = [v for v in (_req if isinstance(_req, list) else []) if isinstance(v, str)]
    sdt = _lift_supported_device_types(body)
    shape = _compute_body_shape(body)
    richness = _richness(body_json)

    outcome = batch.add_component(
        {
            "component_id": comp_id,
            "spec_source": spec_source,
            "section": section,
            "name": name,
            "type": str(body.get("type") or ""),
            "kind": _component_kind(body),
            "bodyShape": shape,
            "required": required,
            "enumValues": enum_vals,
            "supportedDeviceTypes": sdt,
            "bodyJson": body_json,
        },
        richness=richness,
    )
    if outcome == "new":
        stats["components"] += 1

    # Emit property subgraph on first-add OR when richer body replaced
    # a stub (placeholder or otherwise less-rich). Skipped only when
    # the buffered row is already at least as rich.
    if emit_property_subgraph and outcome in ("new", "replaced"):
        _emit_property_subgraph(
            batch,
            spec_source=spec_source,
            parent_component_id=comp_id,
            parent_component_name=name,
            body=body,
            full_components=full_components,
            stats=stats,
            resolution_scope=resolution_scope,
        )

    return comp_id


# ── Property-level extraction (buffered) ────────────────────────────


def _emit_property_subgraph(
    batch: _Batch,
    *,
    spec_source: str,
    parent_component_id: str,
    parent_component_name: str,
    body: dict,
    full_components: dict,
    stats: dict,
    resolution_scope: dict | None = None,
) -> None:
    """Emit HAS_PROPERTY / COMPOSED_OF / PROPERTY_OF_TYPE / HAS_VALUE_SCHEMA rows.

    Properties live exclusively on the component that declares them.
    `allOf` / `oneOf` / `anyOf` branches are wired up via `COMPOSED_OF`;
    consumers walk `(parent)-[:COMPOSED_OF*0..N]->(c)-[:HAS_PROPERTY]->(p)`
    to gather inherited fields. Inline branches without a `$ref` are
    promoted to synthetic SchemaComponents so their fields are reachable
    via the same traversal instead of stranded in `bodyJson`.
    """
    _req = body.get("required")
    own_required = set(
        v for v in (_req if isinstance(_req, list) else []) if isinstance(v, str)
    )
    own_props = body.get("properties") or {}
    if isinstance(own_props, dict):
        for prop_name, prop_body in own_props.items():
            _emit_one_property(
                batch,
                spec_source=spec_source,
                parent_component_id=parent_component_id,
                prop_name=prop_name,
                prop_body=prop_body if isinstance(prop_body, dict) else {},
                required=prop_name in own_required,
                full_components=full_components,
                stats=stats,
                resolution_scope=resolution_scope,
            )

    # additionalProperties → HAS_VALUE_SCHEMA edge to a synthetic
    # SchemaComponent that holds the value shape. ``True`` and ``{}`` both
    # mean "free-form map of any"; normalise both to an empty dict so the
    # synthetic value component exists and bodyShape='map' (set in
    # _compute_body_shape) is never edge-less.
    addl = body.get("additionalProperties")
    if addl is True:
        addl = {}
    if isinstance(addl, dict):
        ref = addl.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/"):
            value_id = _ensure_component_node(
                batch, spec_source, ref, full_components, stats,
                resolution_scope=resolution_scope,
            )
            if value_id:
                batch.add_has_value_schema(parent_component_id, value_id)
        else:
            synth_id = f"{parent_component_id}#additionalProperties"
            _ensure_inline_component(
                batch,
                spec_source=spec_source,
                synth_id=synth_id,
                synth_name=f"{parent_component_name}[additionalProperties]",
                body=addl,
                full_components=full_components,
                stats=stats,
                resolution_scope=resolution_scope,
            )
            batch.add_has_value_schema(parent_component_id, synth_id)

    for kind in ("allOf", "oneOf", "anyOf"):
        branches = body.get(kind)
        if not isinstance(branches, list):
            continue
        for idx, branch in enumerate(branches):
            if not isinstance(branch, dict):
                continue
            ref = branch.get("$ref")
            if isinstance(ref, str):
                target_id = _ensure_component_node(
                    batch, spec_source, ref, full_components, stats,
                    resolution_scope=resolution_scope,
                )
                if target_id:
                    batch.add_composed_of(parent_component_id, target_id, kind)
            elif (
                isinstance(branch.get("properties"), dict)
                or branch.get("allOf") or branch.get("oneOf") or branch.get("anyOf")
                or isinstance(branch.get("additionalProperties"), dict)
            ):
                # Inline branch with structural content: promote to a
                # synthetic SchemaComponent so its fields are reachable
                # via COMPOSED_OF traversal.
                synth_id = f"{parent_component_id}#{kind}:{idx}"
                synth_name = f"{parent_component_name}[{kind}:{idx}]"
                _ensure_inline_component(
                    batch,
                    spec_source=spec_source,
                    synth_id=synth_id,
                    synth_name=synth_name,
                    body=branch,
                    full_components=full_components,
                    stats=stats,
                    resolution_scope=resolution_scope,
                )
                batch.add_composed_of(parent_component_id, synth_id, kind)


def _emit_one_property(
    batch: _Batch,
    *,
    spec_source: str,
    parent_component_id: str,
    prop_name: str,
    prop_body: dict,
    required: bool,
    full_components: dict,
    stats: dict,
    resolution_scope: dict | None = None,
) -> None:
    resolved = _resolve_property_schema(prop_body, full_components)
    if resolved is prop_body and isinstance(prop_body.get("$ref"), str):
        # Local lookup failed — try provider-wide pool.
        fb = _follow_ref(prop_body["$ref"], full_components, fallback=resolution_scope)
        if isinstance(fb, dict):
            resolved = fb
    extensions = _collect_x_extensions(prop_body)
    for k, v in _collect_x_extensions(resolved).items():
        extensions.setdefault(k, v)

    if _TYPED_EXT_DEVICE_TYPE in extensions:
        sdt_raw = extensions.get(_TYPED_EXT_DEVICE_TYPE)
        if isinstance(sdt_raw, list):
            sdt: list[str] | None = [v for v in sdt_raw if isinstance(v, str)]
        elif isinstance(sdt_raw, str):
            sdt = [sdt_raw]
        else:
            sdt = None
    else:
        # Absent extension → property applies to every device type.
        # Stored as NULL so consumers filter with `IS NULL OR ...`.
        sdt = None

    yang_path_raw = extensions.get(_TYPED_EXT_YANG_PATH) or ""
    yang_path = yang_path_raw if isinstance(yang_path_raw, str) else ""

    enum_raw = resolved.get("enum") or []
    enum_vals = [str(v) for v in enum_raw if isinstance(v, (str, int, float, bool))]

    prop_type = str(resolved.get("type") or "")
    prop_format = str(resolved.get("format") or "")
    description = str(resolved.get("description") or prop_body.get("description") or "")

    read_only_raw = prop_body.get("readOnly")
    if read_only_raw is None:
        read_only_raw = resolved.get("readOnly")
    read_only = bool(read_only_raw) if read_only_raw is not None else False

    extensions_json = json.dumps(extensions, sort_keys=True) if extensions else ""

    property_id = f"{parent_component_id}#prop:{prop_name}"

    if batch.add_property({
        "property_id": property_id,
        "parent_component_id": parent_component_id,
        "name": prop_name,
        "type": prop_type,
        "format": prop_format,
        "required": bool(required),
        "enumValues": enum_vals,
        "description": description,
        "supportedDeviceTypes": sdt,
        "yangPath": yang_path,
        "extensionsJson": extensions_json,
        "readOnly": read_only,
    }):
        stats["properties"] = stats.get("properties", 0) + 1
    batch.add_has_property(parent_component_id, property_id)

    if yang_path:
        module = _yang_module_for(yang_path)
        batch.add_yang_path(yang_path, module)
        batch.add_property_at_yang(property_id, yang_path)
        if module:
            if batch.add_yang_module(module):
                stats["yang_modules"] = stats.get("yang_modules", 0) + 1
            batch.add_in_module(yang_path, module)

    target_ref: str | None = None
    if isinstance(prop_body.get("$ref"), str):
        target_ref = prop_body["$ref"]
    else:
        items = prop_body.get("items")
        if isinstance(items, dict) and isinstance(items.get("$ref"), str):
            target_ref = items["$ref"]
    if target_ref and target_ref.startswith("#/components/"):
        target_id = _ensure_component_node(
            batch, spec_source, target_ref, full_components, stats,
            resolution_scope=resolution_scope,
        )
        if target_id:
            batch.add_property_of_type(property_id, target_id)
            return
        # Fall through to inline materialisation below.

    # Inline schema (no $ref): materialise as a synthetic SchemaComponent so
    # that nested fields are reachable from the property graph instead of
    # stranded inside opaque ``bodyJson``.
    items = prop_body.get("items") if isinstance(prop_body.get("items"), dict) else None
    inline_body: dict | None = None
    inline_kind: str = ""
    if items is not None and (
        isinstance(items.get("properties"), dict)
        or items.get("allOf") or items.get("oneOf") or items.get("anyOf")
    ):
        inline_body = items
        inline_kind = "items"
    elif isinstance(prop_body.get("properties"), dict):
        inline_body = prop_body
        inline_kind = "object"
    elif prop_body.get("oneOf") or prop_body.get("anyOf") or prop_body.get("allOf"):
        # Inline union / composition at the property level (no $ref):
        # promote so the agent can walk into the branches.
        inline_body = prop_body
        inline_kind = "union"

    if inline_body is not None:
        synth_id = f"{property_id}#{inline_kind}"
        synth_name = f"{prop_name}[{inline_kind}]"
        _ensure_inline_component(
            batch,
            spec_source=spec_source,
            synth_id=synth_id,
            synth_name=synth_name,
            body=inline_body,
            full_components=full_components,
            stats=stats,
            resolution_scope=resolution_scope,
        )
        batch.add_property_of_type(property_id, synth_id)


def _ensure_inline_component(
    batch: _Batch,
    *,
    spec_source: str,
    synth_id: str,
    synth_name: str,
    body: dict,
    full_components: dict,
    stats: dict,
    resolution_scope: dict | None = None,
) -> None:
    """Materialise an inline schema (no `$ref`) as a SchemaComponent and
    recurse into its property subgraph.

    Synthetic ids carry an ``#items`` / ``#object`` / ``#union`` /
    ``#additionalProperties`` / ``#oneOf:N`` suffix derived from the
    enclosing ``property_id`` or parent ``component_id``. Guarantees
    uniqueness across specs and prevents collision with named-component
    ids (which use ``:`` separators between provider/section/name).
    Dedupe via batch._seen_components makes recursion cycle-safe.
    """
    if synth_id in batch._seen_components:
        return

    stripped = _strip_skeleton_keys(body)
    body_json = json.dumps(stripped)
    enum_vals = [v for v in (body.get("enum") or []) if isinstance(v, str)]
    _req = body.get("required")
    required = [v for v in (_req if isinstance(_req, list) else []) if isinstance(v, str)]
    sdt = _lift_supported_device_types(body)
    shape = _compute_body_shape(body)

    batch.add_component(
        {
            "component_id": synth_id,
            "spec_source": spec_source,
            "section": "inline",
            "name": synth_name,
            "type": str(body.get("type") or ""),
            "kind": _component_kind(body),
            "bodyShape": shape,
            "required": required,
            "enumValues": enum_vals,
            "supportedDeviceTypes": sdt,
            "bodyJson": body_json,
        },
        richness=_richness(body_json),
    )
    stats["components"] = stats.get("components", 0) + 1

    _emit_property_subgraph(
        batch,
        spec_source=spec_source,
        parent_component_id=synth_id,
        parent_component_name=synth_name,
        body=body,
        full_components=full_components,
        stats=stats,
        resolution_scope=resolution_scope,
    )
