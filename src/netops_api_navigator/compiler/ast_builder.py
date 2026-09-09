"""Task 2 lossless OpenAPI AST graph builder.

The L1 AST is intentionally structural.  It preserves the cleaned raw
OpenAPI document, including ``$ref`` sites, and fails loudly when a fixed
OpenAPI/JSON-Schema object contains an unknown non-extension keyword.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from .frontend import ResolvedSpec, ResolutionFailure


_HTTP_METHODS = {
    "get",
    "put",
    "post",
    "delete",
    "options",
    "head",
    "patch",
    "trace",
}

_DOCUMENT_KEYS = {
    "openapi",
    "jsonSchemaDialect",
    "info",
    "servers",
    "paths",
    "webhooks",
    "components",
    "security",
    "tags",
    "externalDocs",
}

_INFO_KEYS = {
    "title",
    "summary",
    "description",
    "termsOfService",
    "contact",
    "license",
    "version",
}

_PATH_ITEM_KEYS = {"$ref", "summary", "description", "servers", "parameters"} | _HTTP_METHODS

_OPERATION_KEYS = {
    "tags",
    "summary",
    "description",
    "externalDocs",
    "operationId",
    "parameters",
    "requestBody",
    "responses",
    "callbacks",
    "deprecated",
    "security",
    "servers",
}

_PARAMETER_KEYS = {
    "name",
    "in",
    "description",
    "required",
    "deprecated",
    "allowEmptyValue",
    "style",
    "explode",
    "allowReserved",
    "schema",
    "example",
    "examples",
    "content",
}

_REQUEST_BODY_KEYS = {"description", "content", "required"}
_RESPONSE_KEYS = {"description", "headers", "content", "links"}
_MEDIA_TYPE_KEYS = {"schema", "example", "examples", "encoding"}
_HEADER_KEYS = _PARAMETER_KEYS - {"name", "in"}
_EXAMPLE_KEYS = {"summary", "description", "value", "externalValue"}
_TAG_KEYS = {"name", "description", "externalDocs"}
_EXTERNAL_DOCS_KEYS = {"description", "url"}
_DISCRIMINATOR_KEYS = {"propertyName", "mapping"}
_ENCODING_KEYS = {"contentType", "headers", "style", "explode", "allowReserved"}

_SECURITY_SCHEME_KEYS = {
    "type",
    "description",
    "name",
    "in",
    "scheme",
    "bearerFormat",
    "flows",
    "openIdConnectUrl",
}
_OAUTH_FLOWS_KEYS = {"implicit", "password", "clientCredentials", "authorizationCode"}
_OAUTH_FLOW_KEYS = {"authorizationUrl", "tokenUrl", "refreshUrl", "scopes"}
_LINK_KEYS = {"operationRef", "operationId", "parameters", "requestBody", "description", "server"}
_SERVER_KEYS = {"url", "description", "variables"}
_SERVER_VARIABLE_KEYS = {"enum", "default", "description"}

_COMPONENT_SECTIONS = {
    "schemas",
    "responses",
    "parameters",
    "examples",
    "requestBodies",
    "headers",
    "securitySchemes",
    "links",
    "callbacks",
    "pathItems",
}

_SCHEMA_KEYS = {
    "$schema",
    "$id",
    "$anchor",
    "$ref",
    "$defs",
    "definitions",
    "title",
    "multipleOf",
    "maximum",
    "exclusiveMaximum",
    "minimum",
    "exclusiveMinimum",
    "maxLength",
    "minLength",
    "pattern",
    "maxItems",
    "minItems",
    "uniqueItems",
    "maxContains",
    "minContains",
    "maxProperties",
    "minProperties",
    "required",
    "dependentRequired",
    "enum",
    "const",
    "type",
    "allOf",
    "anyOf",
    "oneOf",
    "not",
    "items",
    "additionalItems",
    "prefixItems",
    "contains",
    "properties",
    "patternProperties",
    "additionalProperties",
    "dependentSchemas",
    "propertyNames",
    "unevaluatedItems",
    "unevaluatedProperties",
    "description",
    "format",
    "default",
    "nullable",
    "discriminator",
    "readOnly",
    "writeOnly",
    "xml",
    "externalDocs",
    "example",
    "examples",
    "deprecated",
    "if",
    "then",
    "else",
    "contentEncoding",
    "contentMediaType",
    "contentSchema",
}

_SCHEMA_CONSTRAINT_KEYS = _SCHEMA_KEYS - {
    "$defs",
    "definitions",
    "allOf",
    "anyOf",
    "oneOf",
    "not",
    "items",
    "additionalItems",
    "prefixItems",
    "contains",
    "properties",
    "patternProperties",
    "additionalProperties",
    "dependentSchemas",
    "propertyNames",
    "unevaluatedItems",
    "unevaluatedProperties",
    "discriminator",
    "xml",
    "externalDocs",
    "examples",
    "if",
    "then",
    "else",
    "contentSchema",
}

_FIXED_ALLOWED_KEYS: dict[str, set[str]] = {
    "Document": _DOCUMENT_KEYS,
    "Info": _INFO_KEYS,
    "PathItem": _PATH_ITEM_KEYS,
    "Operation": _OPERATION_KEYS,
    "Parameter": _PARAMETER_KEYS,
    "RequestBody": _REQUEST_BODY_KEYS,
    "Response": _RESPONSE_KEYS,
    "MediaType": _MEDIA_TYPE_KEYS,
    "Header": _HEADER_KEYS,
    "Example": _EXAMPLE_KEYS,
    "Tag": _TAG_KEYS,
    "ExternalDocs": _EXTERNAL_DOCS_KEYS,
    "Discriminator": _DISCRIMINATOR_KEYS,
    "Encoding": _ENCODING_KEYS,
    "SecurityScheme": _SECURITY_SCHEME_KEYS,
    "OAuthFlows": _OAUTH_FLOWS_KEYS,
    "OAuthFlow": _OAUTH_FLOW_KEYS,
    "Link": _LINK_KEYS,
    "Server": _SERVER_KEYS,
    "ServerVariable": _SERVER_VARIABLE_KEYS,
    "Schema": _SCHEMA_KEYS,
    "Property": _SCHEMA_KEYS,
    "Items": _SCHEMA_KEYS,
}

# Structural map/array nodes are reconstructable from AST_CHILD plus scalarJson.
# Persisting their full recursive JSON duplicates large portions of each spec
# (Document -> components -> schemas -> properties). Keep raw detail only on
# meaningful OpenAPI constructs and vendor-extension roots.
_RAW_DETAIL_KINDS = (frozenset(_FIXED_ALLOWED_KEYS) - {"Document"}) | {
    "Callback",
    "Extension",
}


@dataclass(frozen=True, slots=True)
class AstNode:
    node_id: str
    spec_id: str
    kind: str
    json_pointer: str
    name: str | None
    key: str | None
    index: int | None
    value_type: str
    raw_json: str
    scalar_json: str
    is_extension: bool


@dataclass(frozen=True, slots=True)
class AstChildEdge:
    parent_id: str
    child_id: str
    role: str
    key: str | None
    index: int | None


@dataclass(frozen=True, slots=True)
class AstRefTargetEdge:
    ref_node_id: str
    target_node_id: str
    ref: str


@dataclass(slots=True)
class AstGraph:
    spec: dict[str, Any]
    spec_row: dict[str, str]
    root_node_id: str
    nodes: list[AstNode] = field(default_factory=list)
    child_edges: list[AstChildEdge] = field(default_factory=list)
    ref_edges: list[AstRefTargetEdge] = field(default_factory=list)

    @property
    def spec_id(self) -> str:
        return self.spec_row["spec_id"]


class UnknownKeywordError(ValueError):
    """Raised when a fixed OAS/JSON-Schema object contains an unknown key."""

    def __init__(self, *, source: str, pointer: str, parent_kind: str, key: str):
        self.source = source
        self.pointer = pointer
        self.parent_kind = parent_kind
        self.key = key
        super().__init__(
            f"Unknown OpenAPI keyword {key!r} at {pointer or '/'} "
            f"inside {parent_kind} for {source}"
        )


def build_ast_from_resolved(resolved: ResolvedSpec) -> AstGraph:
    """Build L1 from the cleaned raw spec carried by a Task 1 result."""
    return build_ast_graph(
        resolved.raw_spec,
        source=resolved.source,
        title=resolved.title,
        ingestion_status="strict_valid",
    )


def build_ast_from_failure(failure: ResolutionFailure) -> AstGraph:
    """Build a marked degraded L1 graph from a strict Task 1 failure."""
    if failure.raw_spec is None:
        raise ValueError("Task 1 failure did not retain a cleaned raw spec")
    return build_ast_graph(
        failure.raw_spec,
        source=failure.source,
        title=failure.title,
        ingestion_status="degraded",
        ingestion_error_type=failure.error_type,
        ingestion_error=failure.error,
    )


def build_ast_graph(
    spec: dict[str, Any],
    *,
    source: str,
    title: str | None = None,
    ingestion_status: str = "strict_valid",
    ingestion_error_type: str = "",
    ingestion_error: str = "",
) -> AstGraph:
    """Return a lossless AST graph for one cleaned raw OpenAPI document."""
    canonical = _json(spec, sort_keys=True)
    content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    spec_id = "oas:" + hashlib.sha1(
        f"{source}\0{content_hash}".encode("utf-8")
    ).hexdigest()[:20]
    info = spec.get("info") if isinstance(spec, dict) else {}
    resolved_title = title or (info.get("title") if isinstance(info, dict) else None) or source
    openapi_version = str(spec.get("openapi") or "") if isinstance(spec, dict) else ""
    graph = AstGraph(
        spec=spec,
        spec_row={
            "spec_id": spec_id,
            "source": source,
            "title": resolved_title,
            "openapi_version": openapi_version,
            "content_hash": content_hash,
            "ingestion_status": ingestion_status,
            "ingestion_error_type": ingestion_error_type,
            "ingestion_error": ingestion_error[:2000],
        },
        root_node_id=f"{spec_id}#",
    )
    index: dict[str, str] = {}
    _walk(
        graph,
        index,
        obj=spec,
        pointer="",
        kind="Document",
        key="",
        array_index=None,
        source=source,
    )
    _link_internal_refs(graph, index)
    return graph


def reconstruct_spec(graph: AstGraph) -> Any:
    """Reconstruct the cleaned raw document from the AST graph."""
    nodes = {n.node_id: n for n in graph.nodes}
    children: dict[str, list[AstChildEdge]] = {}
    for edge in graph.child_edges:
        children.setdefault(edge.parent_id, []).append(edge)

    def _rebuild(node_id: str) -> Any:
        node = nodes[node_id]
        if node.value_type == "object":
            result: dict[str, Any] = {}
            for edge in children.get(node_id, []):
                result[edge.key] = _rebuild(edge.child_id)
            return result
        if node.value_type == "array":
            ordered = sorted(children.get(node_id, []), key=lambda e: e.index or 0)
            return [_rebuild(edge.child_id) for edge in ordered]
        return json.loads(node.scalar_json)

    return _rebuild(graph.root_node_id)


def _walk(
    graph: AstGraph,
    index: dict[str, str],
    *,
    obj: Any,
    pointer: str,
    kind: str,
    key: str | None,
    array_index: int | None,
    source: str,
) -> str:
    value_type = _value_type(obj)
    is_extension = isinstance(key, str) and key.startswith("x-") and kind != "Property"
    if is_extension:
        kind = "Extension"
    node_id = graph.root_node_id if pointer == "" else f"{graph.spec_id}#{pointer}"
    node = AstNode(
        node_id=node_id,
        spec_id=graph.spec_id,
        kind=kind,
        json_pointer=pointer,
        name=_node_name(obj=obj, key=key, array_index=array_index),
        key=key,
        index=array_index,
        value_type=value_type,
        raw_json=_raw_json_for_node(
            obj,
            kind=kind,
            pointer=pointer,
            value_type=value_type,
            is_extension=is_extension,
        ),
        scalar_json=_json(obj) if value_type == "scalar" else "",
        is_extension=is_extension,
    )
    graph.nodes.append(node)
    index[pointer] = node_id

    if isinstance(obj, dict):
        _validate_keys(obj, source=source, pointer=pointer, kind=kind)
        for child_index, (child_key, child_value) in enumerate(obj.items()):
            child_pointer = _join_pointer(pointer, str(child_key))
            child_kind = _child_kind(
                parent_kind=kind,
                parent_pointer=pointer,
                key=str(child_key),
                value=child_value,
            )
            child_id = _walk(
                graph,
                index,
                obj=child_value,
                pointer=child_pointer,
                kind=child_kind,
                key=str(child_key),
                array_index=None,
                source=source,
            )
            graph.child_edges.append(
                AstChildEdge(
                    parent_id=node_id,
                    child_id=child_id,
                    role=str(child_key),
                    key=str(child_key),
                    index=None,
                )
            )
    elif isinstance(obj, list):
        for child_index, child_value in enumerate(obj):
            child_pointer = _join_pointer(pointer, str(child_index))
            child_kind = _array_item_kind(
                parent_kind=kind,
                parent_pointer=pointer,
                parent_key=key,
                value=child_value,
            )
            child_id = _walk(
                graph,
                index,
                obj=child_value,
                pointer=child_pointer,
                kind=child_kind,
                key=None,
                array_index=child_index,
                source=source,
            )
            graph.child_edges.append(
                AstChildEdge(
                    parent_id=node_id,
                    child_id=child_id,
                    role="item",
                    key=None,
                    index=child_index,
                )
            )
    return node_id


def _validate_keys(obj: dict[str, Any], *, source: str, pointer: str, kind: str) -> None:
    if "$ref" in obj and kind not in {"Schema", "Property", "Items", "PathItem"}:
        allowed_ref_keys = {"$ref", "summary", "description"}
        for child_key in obj:
            if isinstance(child_key, str) and child_key.startswith("x-"):
                continue
            if child_key not in allowed_ref_keys:
                raise UnknownKeywordError(
                    source=source,
                    pointer=pointer,
                    parent_kind="Reference",
                    key=str(child_key),
                )
        return
    if pointer == "/components":
        for child_key in obj:
            if isinstance(child_key, str) and child_key.startswith("x-"):
                continue
            if child_key not in _COMPONENT_SECTIONS:
                raise UnknownKeywordError(
                    source=source,
                    pointer=pointer,
                    parent_kind="Components",
                    key=str(child_key),
                )
        return
    if kind not in _FIXED_ALLOWED_KEYS:
        return
    allowed = _FIXED_ALLOWED_KEYS[kind]
    for child_key in obj:
        if isinstance(child_key, str) and child_key.startswith("x-"):
            continue
        if child_key not in allowed:
            raise UnknownKeywordError(
                source=source,
                pointer=pointer,
                parent_kind=kind,
                key=str(child_key),
            )


def _child_kind(*, parent_kind: str, parent_pointer: str, key: str, value: Any) -> str:
    if parent_kind == "Extension":
        return parent_kind
    parts = _pointer_parts(parent_pointer)
    if parent_kind == "Scalar" and _is_schema_property_map(parts):
        return "Property"
    if parent_kind == "Scalar" and _is_schema_def_map(parts):
        return "Schema"
    if parent_kind == "Constraint":
        return parent_kind
    if parent_kind in {"Schema", "Property", "Items"}:
        return _schema_child_kind(parent_pointer=parent_pointer, key=key, value=value)
    if key.startswith("x-"):
        return "Extension"
    if parent_kind == "Document":
        return {
            "info": "Info",
            "externalDocs": "ExternalDocs",
            "servers": "Scalar",
        }.get(key, "Scalar")
    if parent_kind == "PathItem" and key in _HTTP_METHODS:
        return "Operation"
    if parent_kind == "Operation":
        return {
            "requestBody": "RequestBody",
            "responses": "Scalar",
            "externalDocs": "ExternalDocs",
            "servers": "Scalar",
            "callbacks": "Scalar",
        }.get(key, "Scalar")
    if parent_kind == "Parameter" and key == "schema":
        return "Schema"
    if parent_kind == "RequestBody" and key == "content":
        return "Scalar"
    if parent_kind == "Response":
        return {"content": "Scalar", "headers": "Scalar", "links": "Scalar"}.get(key, "Scalar")
    if parent_kind == "MediaType":
        return {"schema": "Schema", "encoding": "Scalar", "examples": "Scalar"}.get(key, "Scalar")
    if parent_kind == "Header" and key == "schema":
        return "Schema"
    if parent_kind == "SecurityScheme" and key == "flows":
        return "OAuthFlows"
    if parent_kind == "OAuthFlows":
        return "OAuthFlow"
    if parent_kind == "Server" and key == "variables":
        return "Scalar"
    if parent_kind == "Tag" and key == "externalDocs":
        return "ExternalDocs"

    if parts == ["paths"] or parts == ["webhooks"]:
        return "PathItem"
    if len(parts) >= 1 and parts[0] == "paths" and key in _HTTP_METHODS:
        return "Operation"
    if _is_component_entry(parts):
        return _component_kind(parts[1])
    if _is_server_variable_entry(parts):
        return "ServerVariable"
    if _is_link_entry(parts):
        return "Link"
    if _is_callback_expression_entry(parts):
        return "PathItem"
    if _is_callback_name_entry(parts):
        return "Callback"
    if _is_content_entry(parts):
        return "MediaType"
    if _is_named_map_entry(parts, "responses"):
        return "Response"
    if _is_named_map_entry(parts, "headers"):
        return "Header"
    if _is_named_map_entry(parts, "examples"):
        return "Example"
    if _is_named_map_entry(parts, "encoding"):
        return "Encoding"
    if _is_named_map_entry(parts, "parameters") and parent_kind == "Scalar":
        return "Scalar"
    if key == "discriminator":
        return "Discriminator"
    if key == "externalDocs":
        return "ExternalDocs"
    return "Scalar"


def _schema_child_kind(*, parent_pointer: str, key: str, value: Any) -> str:
    parts = _pointer_parts(parent_pointer)
    if key.startswith("x-"):
        return "Extension"
    if key in {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}:
        return "Scalar"
    if key in {
        "not",
        "additionalProperties",
        "additionalItems",
        "contains",
        "propertyNames",
        "unevaluatedItems",
        "unevaluatedProperties",
        "if",
        "then",
        "else",
        "contentSchema",
    } and isinstance(value, dict):
        return "Schema"
    if key == "items" and isinstance(value, dict):
        return "Items"
    if key == "discriminator" and isinstance(value, dict):
        return "Discriminator"
    if key == "externalDocs" and isinstance(value, dict):
        return "ExternalDocs"
    if key in {"example", "examples", "default", "enum", "const", "xml"}:
        return "Constraint"
    if _is_schema_def_map(parts):
        return "Schema"
    if key in _SCHEMA_CONSTRAINT_KEYS:
        return "Constraint"
    return "Schema" if isinstance(value, dict) else "Constraint"


def _array_item_kind(
    *, parent_kind: str, parent_pointer: str, parent_key: str | None, value: Any
) -> str:
    if parent_kind == "Extension":
        return parent_kind
    parts = _pointer_parts(parent_pointer)
    if _is_example_value_descendant(parts):
        return "Scalar"
    if parent_key in {"allOf", "anyOf", "oneOf", "prefixItems"}:
        return "Schema"
    if parent_kind == "Constraint":
        return parent_kind
    if parent_key == "servers":
        return "Server"
    if parent_key == "parameters":
        return "Parameter"
    if parent_key == "tags" and len(parts) == 1:
        return "Tag"
    if parent_key == "security":
        return "Scalar"
    if parent_key in {"enum", "required", "dependentRequired"}:
        return "Scalar"
    return "Schema" if isinstance(value, dict) and parent_key in _SCHEMA_KEYS else "Scalar"


def _is_example_value_descendant(parts: list[str]) -> bool:
    dynamic_map_parents = {
        "$defs",
        "callbacks",
        "components",
        "definitions",
        "examples",
        "headers",
        "links",
        "parameters",
        "pathItems",
        "properties",
        "requestBodies",
        "responses",
        "schemas",
    }
    for index, part in enumerate(parts):
        if part == "value" and index >= 2 and parts[index - 2] == "examples":
            return True
        if part not in {"example", "default"}:
            continue
        if index > 0 and parts[index - 1] in dynamic_map_parents:
            continue
        return True
    return False


def _component_kind(section: str) -> str:
    return {
        "schemas": "Schema",
        "responses": "Response",
        "parameters": "Parameter",
        "examples": "Example",
        "requestBodies": "RequestBody",
        "headers": "Header",
        "securitySchemes": "SecurityScheme",
        "pathItems": "PathItem",
        "links": "Link",
        "callbacks": "Callback",
    }.get(section, "Scalar")


def _link_internal_refs(graph: AstGraph, index: dict[str, str]) -> None:
    for node in graph.nodes:
        if node.key != "$ref" or node.value_type != "scalar":
            continue
        try:
            ref = json.loads(node.scalar_json)
        except json.JSONDecodeError:
            continue
        if not isinstance(ref, str) or not ref.startswith("#"):
            continue
        target_pointer = ref[1:] or ""
        target_id = index.get(target_pointer)
        if target_id:
            graph.ref_edges.append(
                AstRefTargetEdge(ref_node_id=node.node_id, target_node_id=target_id, ref=ref)
            )


def _value_type(obj: Any) -> str:
    if isinstance(obj, dict):
        return "object"
    if isinstance(obj, list):
        return "array"
    return "scalar"


def _node_name(*, obj: Any, key: str | None, array_index: int | None) -> str | None:
    if isinstance(obj, dict):
        for name_key in ("name", "operationId", "title"):
            value = obj.get(name_key)
            if isinstance(value, str) and value:
                return value
    if key:
        return key
    return str(array_index) if array_index is not None else None


def _json(obj: Any, *, sort_keys: bool = False) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=sort_keys)


def _raw_json_for_node(
    obj: Any,
    *,
    kind: str,
    pointer: str,
    value_type: str,
    is_extension: bool,
) -> str:
    """Return direct-source detail only where it is not redundant AST storage."""
    if value_type == "scalar":
        return ""
    if is_extension or kind in _RAW_DETAIL_KINDS or _is_schema_detail_node(pointer, value_type):
        return _json(obj)
    return ""


def _is_schema_detail_node(pointer: str, value_type: str) -> bool:
    """Recognize inline schema objects even when their structural kind is generic."""
    if value_type != "object":
        return False
    parts = _pointer_parts(pointer)
    return len(parts) >= 2 and parts[-2] in {
        "allOf",
        "anyOf",
        "oneOf",
        "prefixItems",
        "properties",
        "patternProperties",
    }


def _join_pointer(parent: str, token: str) -> str:
    escaped = token.replace("~", "~0").replace("/", "~1")
    return f"{parent}/{escaped}" if parent else f"/{escaped}"


def _pointer_parts(pointer: str) -> list[str]:
    if not pointer:
        return []
    return [p.replace("~1", "/").replace("~0", "~") for p in pointer.lstrip("/").split("/")]


def _is_component_entry(parts: list[str]) -> bool:
    return len(parts) == 2 and parts[0] == "components" and parts[1] in _COMPONENT_SECTIONS


def _is_content_entry(parts: list[str]) -> bool:
    return bool(parts) and parts[-1] == "content"


def _is_named_map_entry(parts: list[str], map_name: str) -> bool:
    return bool(parts) and parts[-1] == map_name


def _is_schema_property_map(parts: list[str]) -> bool:
    return bool(parts) and parts[-1] in {"properties", "patternProperties"}


def _is_schema_def_map(parts: list[str]) -> bool:
    return bool(parts) and parts[-1] in {"$defs", "definitions", "dependentSchemas"}


def _is_server_variable_entry(parts: list[str]) -> bool:
    return len(parts) >= 1 and parts[-1] == "variables"


def _is_link_entry(parts: list[str]) -> bool:
    return bool(parts) and parts[-1] == "links"


def _is_callback_name_entry(parts: list[str]) -> bool:
    return bool(parts) and parts[-1] == "callbacks"


def _is_callback_expression_entry(parts: list[str]) -> bool:
    return len(parts) >= 2 and parts[-2] == "callbacks"
