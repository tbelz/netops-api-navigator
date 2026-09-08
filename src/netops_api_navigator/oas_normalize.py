"""OpenAPI spec normalization + skeleton/glossary projections.

The raw OpenAPI specs published by Aruba Central's documentation portal
contain massive amounts of repetition.  A handful of error responses
(400/401/403/404/500) and a small number of nested object shapes
(``rule``, ``rule_action``, ``trunk_group_member``, …) are inlined into
every operation, blowing up endpoints like ``sw-port-profiles`` to 354 KB
of JSON.  Even after dedup the descriptive prose dominates the payload
budget — for ``sw-port-profiles`` that's 35+ KB of human-readable
descriptions that an LLM rarely needs to map a configuration value onto a
field name.

This module provides two responsibilities:

1. **``normalize(spec)``** — pure, idempotent transforms that promote
   repeated inline schemas to ``components/{schemas,responses}`` and
   rewrite their use sites to ``$ref``.  The resulting spec is still a
   valid OpenAPI 3.x document.

2. **Skeleton + Glossary + Components projections** —
   ``project_skeleton`` returns the *structure* of an endpoint
   (parameters, request body, success and error response shells, and a
   ``$components_index`` listing every transitively-referenced component
   by name with minimal hints — type, enum, required, child_refs). All
   human-readable strings (``description``, ``title``, ``example``,
   ``x-typeName``, ``x-typeDescription``) are stripped at every level.
   The full bodies of those components are emitted by the separate
   ``project_components`` projection so the build pipeline can store
   them in their own DB column; ``get_schema_component`` then serves a
   single component on demand without re-shipping the entire side-table
   on every detail call.

   ``project_glossary`` returns ONLY the descriptive prose for the same
   endpoint, organised per-component, so an agent can fetch human help
   for ambiguous fields on demand.

   The two projections are complements at the **nested schema /
   parameter prose level**, driven by a single constant
   :data:`_SKELETON_STRIP_KEYS`: the skeleton drops every key in that
   tuple from nested content, and the glossary surfaces only those keys
   wherever they appear.  Adding a key to the tuple moves its nested
   occurrences from skeleton to glossary atomically — no two-list
   drift.  The glossary additionally carries the minimum scaffolding
   needed to reach the prose (``method``, ``path``, ``components``,
   ``parameters``, the structural traversal keys ``properties`` /
   ``items`` / ``allOf`` / …, and the parameter ``in`` field), and
   operation-level ``summary`` is intentionally re-emitted in the
   skeleton rather than duplicated in the glossary.

   Together: 1 tool call for the common case (skeleton has enough),
   2 tool calls for the rare case where a field name is ambiguous.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any

# Description-bearing keys that the skeleton strips at every nesting level.
# This tuple is the SINGLE SOURCE OF TRUTH for the skeleton/glossary split:
# the skeleton drops these keys, and the glossary surfaces ONLY these keys.
# Adding a key here moves it from skeleton to glossary atomically.
_SKELETON_STRIP_KEYS = (
    "description",
    "title",
    "example",
    "examples",
    "x-typeName",
    "x-typeDescription",
    "x-patternSources",
    "summary",
)

# Structural keys that don't carry prose themselves but must be traversed
# to find prose underneath when projecting the glossary.  Anything not in
# _SKELETON_STRIP_KEYS and not in this set is ignored by the glossary
# (it lives in the skeleton).
_GLOSSARY_TRAVERSE_KEYS = (
    "properties",
    "patternProperties",
    "items",
    "additionalProperties",
    "allOf",
    "oneOf",
    "anyOf",
    "schema",
)

# Heuristics
_MIN_DEDUP_OCCURRENCES = 2          # inline shape must appear ≥ this many times
_MIN_DEDUP_PROPERTIES = 3            # only objects with ≥ this many properties
_MAX_DESCRIPTION_REPEAT = 200        # x-typeDescription stripped if ≤ this and == description
_MAX_PATTERN_SOURCES_LEN = 4         # x-patternSources lists longer than this are dropped
_DEFAULT_MAX_LENGTH_NOISE = {9999}   # maxLength values treated as noise defaults

_MUTEX_RE = re.compile(
    r"either\s+`?(?P<a>[A-Za-z0-9_\-]+)`?\s+or\s+`?(?P<b>[A-Za-z0-9_\-]+)`?",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Public: normalize
# ---------------------------------------------------------------------------


def normalize(spec: dict) -> dict:
    """Return an idempotent, dedup'd copy of ``spec``.

    Order matters: noise-stripping runs first so dedup hashes are stable
    across cosmetically-different copies.  Mutex hint emission is last
    because it inspects the cleaned descriptions.
    """
    if not isinstance(spec, dict):
        return spec

    out = copy.deepcopy(spec)
    out.setdefault("components", {})
    out["components"].setdefault("schemas", {})
    out["components"].setdefault("responses", {})

    _strip_noise(out)
    # Mutex hints are emitted before dedup so they annotate the inline
    # shapes that dedup will later promote into ``components/schemas``.
    # Running it after dedup would miss schemas that have already been
    # replaced by ``$ref`` at their use sites.
    _emit_mutex_hints(out)
    _dedup_error_responses(out)
    _dedup_nested_objects(out)

    return out


# ---------------------------------------------------------------------------
# Noise stripping
# ---------------------------------------------------------------------------


def _strip_noise(spec: dict) -> None:
    """Remove noisy metadata that bloats responses without aiding callers."""

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            # Drop x-typeDescription when it duplicates description verbatim.
            xtd = node.get("x-typeDescription")
            desc = node.get("description")
            if (
                isinstance(xtd, str)
                and isinstance(desc, str)
                and xtd.strip() == desc.strip()
                and len(xtd) <= _MAX_DESCRIPTION_REPEAT
            ):
                node.pop("x-typeDescription", None)

            # Trim oversized x-patternSources arrays (build-time YANG noise).
            xps = node.get("x-patternSources")
            if isinstance(xps, list) and len(xps) > _MAX_PATTERN_SOURCES_LEN:
                node.pop("x-patternSources", None)

            # Drop format == "const" — placeholder noise from upstream.
            if node.get("format") == "const":
                node.pop("format", None)

            # Drop placeholder maxLength defaults that signal "no real limit".
            ml = node.get("maxLength")
            if isinstance(ml, int) and ml in _DEFAULT_MAX_LENGTH_NOISE:
                node.pop("maxLength", None)

            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(spec)


# ---------------------------------------------------------------------------
# Error response deduplication
# ---------------------------------------------------------------------------

_ERROR_STATUSES = {"400", "401", "403", "404", "405", "409", "422", "429", "500", "502", "503", "504"}


def _dedup_error_responses(spec: dict) -> None:
    """Promote repeated inline error response objects to ``components/responses``."""
    components = spec["components"]["responses"]
    paths = spec.get("paths") or {}

    # First pass: collect all candidate inlined error responses.
    candidates: dict[str, list[tuple[dict, str]]] = {}
    # hash → list of (parent_responses_dict, status_key)
    for _path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete", "head", "options"}:
                continue
            if not isinstance(operation, dict):
                continue
            responses = operation.get("responses") or {}
            for status, resp in responses.items():
                if not isinstance(resp, dict):
                    continue
                if "$ref" in resp:
                    continue
                # Treat any 4xx/5xx (or wildcards) as candidates.
                norm_status = str(status)
                if not (
                    norm_status in _ERROR_STATUSES
                    or norm_status.upper().endswith("XX")
                    and norm_status[0] in {"4", "5"}
                ):
                    continue
                h = _hash_obj(resp)
                candidates.setdefault(h, []).append((responses, str(status)))

    # Second pass: promote each shape that appears ≥ threshold to components.
    for h, occurrences in candidates.items():
        if len(occurrences) < _MIN_DEDUP_OCCURRENCES:
            continue

        # Pick a stable name based on the most common status code.
        status_counts: dict[str, int] = {}
        for _, status in occurrences:
            status_counts[status] = status_counts.get(status, 0) + 1
        majority_status = max(status_counts.items(), key=lambda kv: kv[1])[0]

        component_name = _unique_name(
            f"Error{majority_status}_{h[:8]}",
            components,
        )

        # Snapshot the canonical shape from the first occurrence.
        first_responses_dict, first_status = occurrences[0]
        canonical = copy.deepcopy(first_responses_dict[first_status])
        components[component_name] = canonical

        ref = {"$ref": f"#/components/responses/{component_name}"}
        for responses_dict, status in occurrences:
            responses_dict[status] = dict(ref)


# ---------------------------------------------------------------------------
# Nested object schema deduplication
# ---------------------------------------------------------------------------


def _dedup_nested_objects(spec: dict) -> None:
    """Promote repeated inline object shapes to ``components/schemas``.

    Iterates to a fixed point so shapes that only become "exposed" after a
    parent object is promoted (and therefore appear ≥ threshold inside a
    handful of canonical components) still get deduplicated.
    """
    for _ in range(8):  # fixed-point loop with a hard cap as a safety net.
        if not _dedup_nested_objects_pass(spec):
            return


def _dedup_nested_objects_pass(spec: dict) -> bool:
    """Single dedup pass; returns True if anything was rewritten."""
    components = spec["components"]["schemas"]
    paths = spec.get("paths") or {}

    # Collect candidates: {hash: [(parent_dict, key), ...]}
    candidates: dict[str, list[tuple[Any, Any]]] = {}

    def _scan(node: Any, parent: Any = None, key: Any = None) -> None:
        if isinstance(node, dict):
            # Skip schemas that are already $refs.
            if "$ref" in node:
                return
            if _is_dedup_candidate(node):
                h = _hash_obj(node)
                if parent is not None:
                    candidates.setdefault(h, []).append((parent, key))
            # Recurse into children regardless — nested-in-nested may dedup too.
            for k, v in list(node.items()):
                _scan(v, node, k)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                _scan(item, node, i)

    # Scan operation request/response/parameters AND already-promoted components
    # so shapes uncovered by parent promotion in a previous pass are visible.
    for _path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete", "head", "options"}:
                continue
            if not isinstance(operation, dict):
                continue
            _scan(operation.get("requestBody"))
            _scan(operation.get("responses"))
            _scan(operation.get("parameters"))
    for _name, comp in list(components.items()):
        _scan(comp)

    # Promote each shape that appears ≥ threshold.
    rewrote = False
    for h, occurrences in candidates.items():
        if len(occurrences) < _MIN_DEDUP_OCCURRENCES:
            continue

        # Pick a name from the title/x-typeName if present, else from hash.
        first_parent, first_key = occurrences[0]
        first = first_parent[first_key]
        suggested = (
            first.get("title")
            or first.get("x-typeName")
            or "Shape"
        )
        suggested = re.sub(r"[^A-Za-z0-9_]", "", str(suggested)) or "Shape"
        component_name = _unique_name(f"{suggested}_{h[:8]}", components)

        components[component_name] = copy.deepcopy(first)
        ref = {"$ref": f"#/components/schemas/{component_name}"}
        for parent, key in occurrences:
            parent[key] = dict(ref)
        rewrote = True

    return rewrote


def _is_dedup_candidate(node: dict) -> bool:
    """True iff ``node`` looks like an inline object schema worth promoting."""
    if node.get("type") != "object":
        return False
    props = node.get("properties")
    if not isinstance(props, dict) or len(props) < _MIN_DEDUP_PROPERTIES:
        return False
    return True


# ---------------------------------------------------------------------------
# Mutual-exclusion hint emission
# ---------------------------------------------------------------------------


def _emit_mutex_hints(spec: dict) -> None:
    """Annotate schemas whose description says 'either X or Y' with a hint.

    Walks both ``paths`` (inline operation/schema bodies) and
    ``components`` (already-promoted shapes) so hints survive even when
    a later dedup pass moves the annotated schema into a component.
    """

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            desc = node.get("description")
            if isinstance(desc, str) and "either" in desc.lower():
                m = _MUTEX_RE.search(desc)
                if m:
                    a, b = m.group("a"), m.group("b")
                    props = node.get("properties")
                    if isinstance(props, dict) and a in props and b in props:
                        node.setdefault("x-mutually-exclusive", [a, b])
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    for _path, path_item in (spec.get("paths") or {}).items():
        _walk(path_item)
    components = spec.get("components") or {}
    for bucket_name in ("schemas", "responses"):
        bucket = components.get(bucket_name) or {}
        for _name, comp in bucket.items():
            _walk(comp)


# ---------------------------------------------------------------------------
# Hashing / naming helpers
# ---------------------------------------------------------------------------


def _hash_obj(obj: Any) -> str:
    return hashlib.sha1(
        json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _unique_name(base: str, registry: dict) -> str:
    if base not in registry:
        return base
    for n in range(2, 1000):
        candidate = f"{base}_{n}"
        if candidate not in registry:
            return candidate
    raise RuntimeError(f"could not find unique name for {base!r}")


# ---------------------------------------------------------------------------
# Projection helpers
# ---------------------------------------------------------------------------


def _find_operation(spec: dict, method: str, path: str) -> dict | None:
    paths = spec.get("paths") or {}
    item = paths.get(path)
    if not isinstance(item, dict):
        return None
    op = item.get(method.lower())
    return op if isinstance(op, dict) else None


def schema_richness(node: Any) -> int:
    """Comparable richness score for an OAS schema fragment.

    Used by both ref-resolution (cross-spec richest-wins, see
    ``_follow_ref``) and the SchemaComponent merge in
    :mod:`oas_schema_graph` so the two sites cannot drift apart.
    Returns 0 for ``None``. The score is the length of the
    JSON-serialised node — longer ≈ richer (more properties,
    descriptions, enums, ``allOf`` chains, vendor extensions).
    """
    if node is None:
        return 0
    try:
        return len(json.dumps(node, default=str, sort_keys=True))
    except (TypeError, ValueError):
        return len(repr(node))


def _follow_ref(
    ref: str,
    components: dict,
    fallback: dict | None = None,
) -> Any | None:
    """Resolve a local ``#/components/...`` ref, richest-wins across scopes.

    Tries both ``components`` and ``fallback`` (provider-wide union of
    every spec's components built during Pass A of the knowledge-DB
    build). When both define the target — common in multi-file Aruba
    Central bundles where one operation spec stubs a component that a
    sibling spec declares richly — the candidate with the higher
    :func:`schema_richness` wins. Returns ``None`` only if neither
    scope contains the path.
    """
    if not ref.startswith("#/components/"):
        return None
    parts = ref.removeprefix("#/components/").split("/")
    best: Any | None = None
    best_score = -1
    for scope in (components, fallback):
        if not isinstance(scope, dict):
            continue
        node: Any = scope
        for part in parts:
            if isinstance(node, dict):
                node = node.get(part)
            else:
                node = None
                break
        if node is None:
            continue
        score = schema_richness(node)
        if score > best_score:
            best = node
            best_score = score
    return best


def _collect_refs(node: Any, refs: set[str]) -> None:
    """Collect every ``$ref`` string reachable from ``node`` (transitively)."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            refs.add(ref)
        for v in node.values():
            _collect_refs(v, refs)
    elif isinstance(node, list):
        for item in node:
            _collect_refs(item, refs)


def _extract_referenced_components(
    payload: Any,
    full_components: dict,
    fallback: dict | None = None,
) -> dict:
    """Return a side-table containing only the components referenced by ``payload``.

    Walks transitively: a referenced schema may itself contain ``$ref`` to
    another schema; both are included.  Returns an empty dict if nothing is
    referenced.  Output mirrors OAS layout: ``{"schemas": {...}, "responses": {...}}``.

    ``fallback`` is a provider-wide component pool used when ``payload`` /
    a transitive reference targets a component that the local spec does not
    define (cross-spec ref).
    """
    seen: set[str] = set()
    pending: set[str] = set()
    _collect_refs(payload, pending)

    out: dict[str, dict] = {}
    while pending:
        ref = pending.pop()
        if ref in seen:
            continue
        seen.add(ref)
        if not ref.startswith("#/components/"):
            continue
        parts = ref.removeprefix("#/components/").split("/")
        if len(parts) < 2:
            continue
        section, name = parts[0], parts[1]
        target = _follow_ref(ref, full_components, fallback=fallback)
        if target is None:
            continue
        out.setdefault(section, {})[name] = target
        # Walk into the resolved target — it may transitively reference more.
        new_refs: set[str] = set()
        _collect_refs(target, new_refs)
        pending |= (new_refs - seen)

    return out


def _operation_meta(operation: dict, method: str, path: str) -> dict:
    return {
        "method": method.upper(),
        "path": path,
        "summary": operation.get("summary", "") or "",
        "description": operation.get("description", "") or "",
        "operation_id": operation.get("operationId", "") or "",
        "tags": operation.get("tags", []) or [],
        "deprecated": bool(operation.get("deprecated", False)),
    }


def _strip_skeleton_keys(node: Any) -> Any:
    """Recursively drop description-bearing keys from a schema fragment.

    Non-destructive — operates on a deep copy.  Preserves all structural
    keys (``type``, ``properties``, ``items``, ``required``, ``enum``,
    ``default``, ``format``, ``$ref``, ``allOf``/``oneOf``/``anyOf``,
    ``x-mutually-exclusive``) so an agent retains everything it needs to
    construct a valid request body.
    """

    def _walk(n: Any) -> Any:
        if isinstance(n, dict):
            out: dict[str, Any] = {}
            for k, v in n.items():
                if k in _SKELETON_STRIP_KEYS:
                    continue
                # `properties` / `patternProperties` keys are user-defined
                # field names, not OpenAPI schema keywords.  Never filter
                # them by name (a property literally called `description`
                # is legal and must survive); only recurse into the
                # *values* so nested schemas still get their prose stripped.
                if k in ("properties", "patternProperties") and isinstance(v, dict):
                    out[k] = {pname: _walk(pval) for pname, pval in v.items()}
                else:
                    out[k] = _walk(v)
            return out
        if isinstance(n, list):
            return [_walk(item) for item in n]
        return n

    return _walk(node)


def _extract_prose(node: Any) -> Any:
    """Return ``node`` filtered to the prose keys in :data:`_SKELETON_STRIP_KEYS`.

    The walker keeps every occurrence of a strip-key (verbatim, at every
    nesting level) and traverses the structural keys in
    :data:`_GLOSSARY_TRAVERSE_KEYS` to reach prose nested deeper.  Every
    other key (``type``, ``enum``, ``format``, ``pattern``, ``default``,
    ``required``, ``$ref``, ``x-mutually-exclusive``, ``minLength`` …) is
    ignored — those keys are preserved by the skeleton, so duplicating
    them in the glossary would only bloat payloads.

    Returns ``None`` when no prose is reachable, so callers can drop empty
    entries (parameters, components, properties …) without an extra
    emptiness check.

    Property/parameter names inside ``properties`` / ``patternProperties``
    are user-controlled keys, not OpenAPI keywords, so they are preserved
    as map keys (a property literally named ``description`` survives).
    """
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k in _SKELETON_STRIP_KEYS:
                # Drop content-free prose so a node with only an empty
                # ``description: ""`` still collapses to ``None`` and the
                # caller can prune the whole entry.
                if isinstance(v, str) and not v.strip():
                    continue
                if isinstance(v, (list, tuple, dict)) and not v:
                    continue
                out[k] = v
                continue
            if k in ("properties", "patternProperties") and isinstance(v, dict):
                nested: dict[str, Any] = {}
                for pname, pval in v.items():
                    pe = _extract_prose(pval)
                    if pe:
                        nested[pname] = pe
                if nested:
                    out[k] = nested
                continue
            if k in _GLOSSARY_TRAVERSE_KEYS:
                sub = _extract_prose(v)
                if sub:
                    out[k] = sub
        return out or None
    if isinstance(node, list):
        # Preserve list length and item ordering for ``allOf`` / ``oneOf``
        # / ``anyOf`` so glossary indices line up with the skeleton's
        # — clients can correlate ``glossary["allOf"][i]`` with
        # ``skeleton["allOf"][i]`` without remapping.  Items with no
        # prose become ``None`` placeholders; if every item is empty the
        # whole list collapses to ``None``.
        results = [_extract_prose(item) for item in node]
        if any(r is not None for r in results):
            return results
        return None
    return None


# ---------------------------------------------------------------------------
# Projections
# ---------------------------------------------------------------------------


def project_skeleton(spec: dict, method: str, path: str) -> dict | None:
    """Return the structural shape of an endpoint, descriptions stripped.

    Includes parameters, request body, success response, first error
    response (as ``$ref``), a ``$components_index`` summarising every
    transitively-referenced component by name (type, enum, required,
    child_refs), and a flat ``required_paths`` list computed from the
    fully-resolved request body.  Every ``description`` / ``title`` /
    ``example`` / ``x-typeName`` / ``x-typeDescription`` /
    ``x-patternSources`` / ``summary`` field inside ``parameters``,
    ``request_body``, and ``responses`` is removed.  Operation-level
    ``summary`` is preserved at the top level so the agent keeps a
    one-line label for the endpoint.

    Component bodies are NOT inlined.  Use :func:`project_components`
    (build pipeline) to obtain the full bodies, and
    ``get_schema_component(method, path, name)`` (runtime tool) to fetch
    a single one on demand.  This keeps detail calls small even on
    huge endpoints (port profiles, ethernet interfaces, …) where
    transitively expanded components otherwise dominate >98 % of
    payload bytes.
    """
    op = _find_operation(spec, method, path)
    if op is None:
        return None
    components = spec.get("components") or {}

    # Parameters — preserve structure, strip description fields.
    params_out: list[dict] = []
    for p in op.get("parameters", []) or []:
        if not isinstance(p, dict):
            continue
        # Resolve one level so a $ref-only parameter still carries name/in/schema.
        resolved = p
        if "$ref" in p:
            target = _follow_ref(p["$ref"], components)
            if isinstance(target, dict):
                resolved = target
        schema = resolved.get("schema") or {}
        params_out.append(_strip_skeleton_keys({
            "name": resolved.get("name", ""),
            "in": resolved.get("in", ""),
            "required": resolved.get("required", False),
            "schema": schema,
        }))

    # Request body — keep $refs as-is so the side-table reuse is preserved.
    rb_out = None
    required_paths: list[str] = []
    rb = op.get("requestBody")
    if isinstance(rb, dict):
        # Resolve the requestBody object itself one level (it may be a $ref).
        rb_resolved = rb
        if "$ref" in rb:
            target = _follow_ref(rb["$ref"], components)
            if isinstance(target, dict):
                rb_resolved = target
        content = rb_resolved.get("content") or {}
        media, schema = _pick_media_schema(content)
        if schema is not None:
            rb_out = _strip_skeleton_keys({
                "content_type": media,
                "schema": schema,
                "required": rb_resolved.get("required", False),
            })
            # Required leaves come from the FULLY resolved body so deeply
            # nested required fields surface; the schema itself stays
            # ref-shaped so the side-table can dedupe.
            fully_resolved = _resolve_full(schema, components)
            required_paths = _collect_required_paths(fully_resolved)

    # Responses — keep success body shape (refs preserved) + first error ref.
    responses_out: dict[str, Any] = {}
    success_status, success_resp = _pick_success_response(op.get("responses") or {})
    if success_resp is not None:
        success_resolved = success_resp
        if isinstance(success_resp, dict) and "$ref" in success_resp:
            target = _follow_ref(success_resp["$ref"], components)
            if isinstance(target, dict):
                success_resolved = target
        content = (success_resolved or {}).get("content") or {}
        _, schema = _pick_media_schema(content)
        responses_out[success_status] = _strip_skeleton_keys({"schema": schema})

    error_ref = _pick_first_error_ref(op.get("responses") or {})
    if error_ref is not None:
        responses_out["error"] = error_ref

    payload: dict[str, Any] = {
        "method": method.upper(),
        "path": path,
        "summary": op.get("summary", "") or "",
        "operation_id": op.get("operationId", "") or "",
        "tags": op.get("tags", []) or [],
        "deprecated": bool(op.get("deprecated", False)),
    }
    payload["parameters"] = params_out
    # Always emit request_body / required_paths so the response shape is
    # stable across GET (no body) and write endpoints.  GET endpoints get
    # ``request_body: null`` and ``required_paths: []``.
    payload["request_body"] = rb_out
    payload["required_paths"] = required_paths
    payload["responses"] = responses_out

    # Side-table is replaced by an *index*: name + minimal hints
    # (type/enum/required/child_refs) per referenced component.  Full
    # bodies live in a separate :func:`project_components` projection so
    # ``get_api_endpoint_detail`` stays small even on huge endpoints
    # (port profiles, ethernet interfaces, …) where transitively
    # expanded components otherwise dominate >98 % of the payload bytes.
    # Use ``get_schema_component(method, path, name)`` to fetch a single
    # component's full body on demand.
    side = _extract_referenced_components(payload, components)
    payload["$components_index"] = _build_components_index(side)
    return payload


def project_components(spec: dict, method: str, path: str) -> dict | None:
    """Return the prose-stripped full bodies of components for an endpoint.

    Returns the same shape that :func:`project_skeleton` previously
    embedded under ``$components`` — ``{"schemas": {...}, "responses":
    {...}}`` — with descriptions / titles / examples stripped at every
    nesting level.  Used by the build pipeline to populate a separate
    DB column so ``get_schema_component`` can serve a single component
    on demand without re-walking the spec.

    Returns ``None`` when the endpoint is unknown.
    """
    op = _find_operation(spec, method, path)
    if op is None:
        return None
    components = spec.get("components") or {}
    refs_root: dict[str, Any] = {
        "parameters": op.get("parameters") or [],
        "requestBody": op.get("requestBody"),
        "responses": op.get("responses") or {},
    }
    side = _extract_referenced_components(refs_root, components)
    if not side:
        return {}
    out: dict[str, dict] = {}
    for section, entries in side.items():
        if not isinstance(entries, dict):
            continue
        out[section] = {
            name: _strip_skeleton_keys(sch) for name, sch in entries.items()
        }
    return out


# ---------------------------------------------------------------------------
# Components index — minimal per-component summary
# ---------------------------------------------------------------------------
#
# For a typical "big" endpoint, >98 % of the legacy skeleton's bytes lived
# in the transitively-expanded ``$components`` side-table.  The index
# replaces that blob with one entry per referenced component carrying
# only the structural hints an agent needs to decide whether to drill in
# (via ``get_schema_component``).

_INDEX_KEEP_KEYS = ("type", "format")


def _component_index_entry(body: Any) -> dict[str, Any]:
    """Summarise one component into the index shape.

    Keeps:
      * ``type`` and ``format`` (top-level only),
      * ``enum`` for primitives,
      * ``required`` for objects,
      * ``oneOf`` / ``anyOf`` / ``allOf`` presence flags so the agent
        knows the component is a union and must fetch the full body
        before guessing a shape,
      * ``items_ref`` for arrays whose items are a single ``$ref``,
      * ``child_refs`` — every ``$ref`` reachable from this component
        (deduplicated, sorted), so the agent can plan its drill-downs
        without first fetching the body.
    """
    if not isinstance(body, dict):
        return {}
    entry: dict[str, Any] = {}
    for k in _INDEX_KEEP_KEYS:
        if k in body:
            entry[k] = body[k]
    enum = body.get("enum")
    if isinstance(enum, list) and enum:
        entry["enum"] = enum
    required = body.get("required")
    if isinstance(required, list) and required:
        entry["required"] = [r for r in required if isinstance(r, str)]
    for kw in ("oneOf", "anyOf", "allOf"):
        if isinstance(body.get(kw), list) and body[kw]:
            entry[kw] = True
    if body.get("type") == "array":
        items = body.get("items")
        if isinstance(items, dict) and "$ref" in items:
            entry["items_ref"] = items["$ref"]
    refs: set[str] = set()
    _collect_refs(body, refs)
    if refs:
        entry["child_refs"] = sorted(refs)
    return entry


def _build_components_index(side: Any) -> dict[str, dict]:
    """Project a full components side-table to its per-section index."""
    if not isinstance(side, dict):
        return {}
    out: dict[str, dict] = {}
    for section, entries in side.items():
        if not isinstance(entries, dict):
            continue
        out[section] = {
            name: _component_index_entry(body) for name, body in entries.items()
        }
    return out


def project_glossary(spec: dict, method: str, path: str) -> dict | None:
    """Return the descriptive prose for an endpoint, organised per-component.

    The glossary carries the prose-bearing keys that the skeleton strips
    from nested parameter and schema content: ``description``, ``title``,
    ``example``, ``examples``, ``x-typeName``, ``x-typeDescription``, and
    ``x-patternSources`` wherever they appear.  Operation-level
    ``summary`` is the one intentional exception: although it is listed
    in :data:`_SKELETON_STRIP_KEYS` (so the recursive stripper drops it
    from nested schemas), :func:`project_skeleton` re-emits it at the
    top level as a one-line endpoint label, and the glossary therefore
    does not duplicate it.  Structural keys (``type``, ``enum``,
    ``format``, ``pattern``, ``default``, ``required``, ``$ref``,
    ``x-mutually-exclusive``, length / numeric constraints, …) are NOT
    repeated because the skeleton preserves them; duplicating them here
    would only bloat payloads.  Adding a key to
    :data:`_SKELETON_STRIP_KEYS` automatically moves it from nested
    skeleton output to glossary on the next build.

    Output shape::

        {
          "method": "POST",
          "path": "/foo",
          "description": "... operation-level prose ...",
          "parameters": {
            "filter": {
              "in": "query",
              "description": "... full prose, e.g. OData filter syntax ...",
              "example": "...",
              "schema": {"description": "..."},
            },
            ...
          },
          "components": {
            "ArubaInterfaceCommon_SwitchportConfig": {
              "description": "...",
              "title": "...",
              "properties": {
                "vlan-mode": {"description": "..."},
                ...
              }
            },
            ...
          }
        }

    Parameters and components with no descriptive content are omitted.
    Each parameter entry carries an ``in`` scaffold so an agent can tell
    where the parameter is bound (``query`` / ``path`` / ``header`` /
    ``cookie``); that single key is the only non-prose field present.
    """
    op = _find_operation(spec, method, path)
    if op is None:
        return None
    components = spec.get("components") or {}

    # Build a transient skeleton-shaped payload to discover which components
    # are reachable from THIS endpoint, so the glossary stays endpoint-scoped.
    refs_root: dict[str, Any] = {
        "parameters": op.get("parameters") or [],
        "requestBody": op.get("requestBody"),
        "responses": op.get("responses") or {},
    }
    side = _extract_referenced_components(refs_root, components)

    payload: dict[str, Any] = {
        "method": method.upper(),
        "path": path,
    }
    op_desc = op.get("description")
    if isinstance(op_desc, str) and op_desc.strip():
        payload["description"] = op_desc

    # Parameters — _extract_prose walks each parameter (resolving one
    # level of $ref so name/in scaffolding survives) and surfaces every
    # prose key it finds.  The single non-prose scaffold we add back is
    # ``in`` so the agent can tell query vs path vs header vs cookie.
    params_out: dict[str, dict[str, Any]] = {}
    for p in op.get("parameters", []) or []:
        if not isinstance(p, dict):
            continue
        resolved = p
        if "$ref" in p:
            target = _follow_ref(p["$ref"], components)
            if isinstance(target, dict):
                resolved = target
        pname = resolved.get("name")
        if not isinstance(pname, str) or not pname:
            continue
        prose = _extract_prose(resolved)
        if prose:
            prose["in"] = resolved.get("in", "") or ""
            params_out[pname] = prose
    if params_out:
        payload["parameters"] = params_out

    # Components reachable from this endpoint.
    components_out: dict[str, Any] = {}
    schemas = side.get("schemas", {}) if isinstance(side, dict) else {}
    for name, sch in schemas.items():
        entry = _extract_prose(sch)
        if entry:
            components_out[name] = entry

    # Inline request-body schema (when the body isn't a $ref) — same
    # treatment, surfaced under the synthetic ``requestBody`` key.
    rb = op.get("requestBody")
    if isinstance(rb, dict):
        content = (rb.get("content") or {})
        _, body_schema = _pick_media_schema(content)
        if isinstance(body_schema, dict) and "$ref" not in body_schema:
            entry = _extract_prose(body_schema)
            if entry:
                components_out["requestBody"] = entry

    payload["components"] = components_out
    return payload


# ---------------------------------------------------------------------------
# Resolution helpers shared by projections
# ---------------------------------------------------------------------------


_MAX_FULL_DEPTH = 15


def _resolve_full(node: Any, components: dict, *, depth: int = 0) -> Any:
    """Recursively expand every ``$ref``.  Mirrors ``oas_index._resolve_refs``."""
    if depth > _MAX_FULL_DEPTH:
        return {"$circular_ref": True}
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            target = _follow_ref(ref, components)
            if target is not None:
                return _resolve_full(target, components, depth=depth + 1)
            return node
        return {k: _resolve_full(v, components, depth=depth) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve_full(i, components, depth=depth) for i in node]
    return node


def _pick_media_schema(content: dict) -> tuple[str, Any]:
    """Return (media_type, schema) preferring application/json."""
    if not isinstance(content, dict) or not content:
        return ("", None)
    for preferred in ("application/json", "application/merge-patch+json"):
        if preferred in content:
            entry = content[preferred] or {}
            return (preferred, entry.get("schema"))
    media, entry = next(iter(content.items()))
    return (media, (entry or {}).get("schema"))


def _pick_success_response(responses: dict) -> tuple[str, Any]:
    """Return (status, response_obj) preferring 200/201/204."""
    for status in ("200", "201", "204", "202"):
        if status in responses:
            return (status, responses[status])
    # Fallback: first 2xx.
    for status, resp in responses.items():
        s = str(status)
        if s.startswith("2") or s.upper() == "2XX":
            return (s, resp)
    return ("", None)


def _pick_first_error_ref(responses: dict) -> dict | None:
    """Return the first error-class response that points at a $ref."""
    for status, resp in responses.items():
        s = str(status)
        if not (s.startswith(("4", "5")) or s.upper().endswith("XX") and s[0] in {"4", "5"}):
            continue
        if isinstance(resp, dict) and "$ref" in resp:
            return {"status": s, "$ref": resp["$ref"]}
    return None


def _collect_required_paths(schema: Any, prefix: str = "") -> list[str]:
    """Walk a fully-resolved schema and return dot-paths to required leaves."""
    out: list[str] = []
    if not isinstance(schema, dict):
        return out

    required = schema.get("required") or []
    props = schema.get("properties") or {}
    if isinstance(required, list) and isinstance(props, dict):
        for name in required:
            if not isinstance(name, str):
                continue
            child_path = f"{prefix}.{name}" if prefix else name
            child = props.get(name)
            if isinstance(child, dict) and child.get("type") == "object":
                nested = _collect_required_paths(child, child_path)
                out.extend(nested or [child_path])
            elif isinstance(child, dict) and child.get("type") == "array":
                items = child.get("items")
                if isinstance(items, dict):
                    nested = _collect_required_paths(items, f"{child_path}[]")
                    out.extend(nested or [child_path])
                else:
                    out.append(child_path)
            else:
                out.append(child_path)

    # Also walk into nested properties even if not required, to surface deeply
    # nested required leaves.
    if isinstance(props, dict):
        for name, child in props.items():
            if not isinstance(child, dict):
                continue
            child_path = f"{prefix}.{name}" if prefix else name
            if name in (required or []):
                continue  # already handled above
            if child.get("type") == "object":
                out.extend(_collect_required_paths(child, child_path))
            elif child.get("type") == "array":
                items = child.get("items")
                if isinstance(items, dict):
                    out.extend(_collect_required_paths(items, f"{child_path}[]"))

    return out
