"""Task 3A semantic overlay builder for the OpenAPI AST graph.

The L2 overlay is intentionally compact.  It creates deterministic
"highway" nodes and edges for agent traversal while keeping the lossless
L1 AST as the source of truth and provenance for every semantic row.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from .ast_builder import AstGraph, AstNode
from .constraints import collect_constraints

STRUCTURAL_RULE_PACK_ID = "semantic.structural.v1"
IDENTITY_RULE_PACK_ID = "semantic.identity.v1"

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

_SCHEMA_CHILD_KEYS = {
    "additionalItems",
    "additionalProperties",
    "contains",
    "contentSchema",
    "else",
    "if",
    "items",
    "not",
    "propertyNames",
    "then",
    "unevaluatedItems",
    "unevaluatedProperties",
}

_SCHEMA_MAP_KEYS = {
    "$defs",
    "definitions",
    "dependentSchemas",
}


@dataclass(frozen=True, slots=True)
class SemanticNode:
    semantic_id: str
    spec_id: str
    kind: str
    name: str
    ast_node_id: str
    json_pointer: str
    stable_key: str
    summary_json: str


@dataclass(frozen=True, slots=True)
class SemanticEdge:
    source_id: str
    target_id: str
    kind: str
    rule_id: str
    evidence_json: str


@dataclass(frozen=True, slots=True)
class SemanticDerivedFromEdge:
    semantic_id: str
    ast_node_id: str
    role: str


@dataclass(slots=True)
class SemanticGraph:
    spec_id: str
    rule_packs: tuple[str, ...] = (STRUCTURAL_RULE_PACK_ID, IDENTITY_RULE_PACK_ID)
    nodes: list[SemanticNode] = field(default_factory=list)
    edges: list[SemanticEdge] = field(default_factory=list)
    derived_edges: list[SemanticDerivedFromEdge] = field(default_factory=list)


class _SemanticState:
    def __init__(self, ast_graph: AstGraph) -> None:
        self.ast_graph = ast_graph
        self.semantic_graph = SemanticGraph(spec_id=ast_graph.spec_id)
        self.ast_by_pointer = {n.json_pointer: n for n in ast_graph.nodes}
        self.semantic_by_key: dict[tuple[str, str], SemanticNode] = {}
        self.schema_by_pointer: dict[str, SemanticNode] = {}
        self.property_by_pointer: dict[str, SemanticNode] = {}
        self.yang_by_path: dict[str, SemanticNode] = {}
        self.model_by_key: dict[str, SemanticNode] = {}
        self._seen_edges: set[tuple[str, str, str, str, str]] = set()

    def add_node(
        self,
        *,
        kind: str,
        stable_key: str,
        name: str,
        ast_pointer: str,
        summary: dict[str, Any],
    ) -> SemanticNode:
        key = (kind, stable_key)
        existing = self.semantic_by_key.get(key)
        if existing is not None:
            return existing
        ast_node = self.ast_by_pointer.get(ast_pointer)
        ast_node_id = ast_node.node_id if ast_node else ""
        node = SemanticNode(
            semantic_id=_semantic_id(self.ast_graph.spec_id, kind, stable_key),
            spec_id=self.ast_graph.spec_id,
            kind=kind,
            name=name,
            ast_node_id=ast_node_id,
            json_pointer=ast_pointer,
            stable_key=stable_key,
            summary_json=_json(summary),
        )
        self.semantic_by_key[key] = node
        self.semantic_graph.nodes.append(node)
        if ast_node_id:
            self.semantic_graph.derived_edges.append(
                SemanticDerivedFromEdge(
                    semantic_id=node.semantic_id,
                    ast_node_id=ast_node_id,
                    role="primary",
                )
            )
        return node

    def add_edge(
        self,
        source: SemanticNode | None,
        target: SemanticNode | None,
        *,
        kind: str,
        rule_id: str,
        evidence: dict[str, Any],
    ) -> None:
        if source is None or target is None:
            return
        evidence_json = _json(evidence)
        key = (source.semantic_id, target.semantic_id, kind, rule_id, evidence_json)
        if key in self._seen_edges:
            return
        self._seen_edges.add(key)
        self.semantic_graph.edges.append(
            SemanticEdge(
                source_id=source.semantic_id,
                target_id=target.semantic_id,
                kind=kind,
                rule_id=rule_id,
                evidence_json=evidence_json,
            )
        )


def build_semantic_overlay(ast_graph: AstGraph) -> SemanticGraph:
    """Build a compact semantic overlay from one lossless L1 AST graph."""
    state = _SemanticState(ast_graph)
    _build_schema_nodes(state)
    _build_property_nodes(state)
    _build_schema_edges(state)
    _build_endpoint_nodes_and_edges(state)
    _build_model_identity_overlay(state)
    return state.semantic_graph


def _build_schema_nodes(state: _SemanticState) -> None:
    for ast_node in state.ast_graph.nodes:
        if not _is_schema_ast_node(ast_node):
            continue
        body = _load_object(ast_node.raw_json)
        if body is None:
            continue
        pointer = ast_node.json_pointer
        node = state.add_node(
            kind="SchemaComponent",
            stable_key=f"schema:{pointer or '/'}",
            name=_schema_name(ast_node),
            ast_pointer=pointer,
            summary={
                "bodyShape": _schema_shape(body),
                "constraints": collect_constraints(body),
                "description": _as_str(body.get("description")),
                "enumValues": _enum_values(body),
                "format": _as_str(body.get("format")),
                "isNamed": _is_named_component_schema(pointer),
                "kind": _component_kind(body),
                "required": _string_list(body.get("required")),
                "type": _schema_type(body),
                "x-key": _string_list(body.get("x-key")),
                "x-path": body.get("x-path") if isinstance(body.get("x-path"), str) else "",
                "xExtensions": _x_extensions(body),
                "x-supportedDeviceType": _supported_device_types(body),
            },
        )
        state.schema_by_pointer[pointer] = node


def _build_property_nodes(state: _SemanticState) -> None:
    parent_by_child = _parent_by_child(state.ast_graph)
    for ast_node in state.ast_graph.nodes:
        if not _is_property_ast_node(ast_node):
            continue
        body = _load_object(ast_node.raw_json)
        if body is None:
            continue
        property_name = ast_node.key or ast_node.name or _last_pointer_part(ast_node.json_pointer)
        parent_schema = _parent_schema_node(state, ast_node, parent_by_child)
        node = state.add_node(
            kind="Property",
            stable_key=f"property:{ast_node.json_pointer}",
            name=property_name,
            ast_pointer=ast_node.json_pointer,
            summary={
                "constraints": collect_constraints(body),
                "description": _as_str(body.get("description")),
                "enumValues": _enum_values(body),
                "format": _as_str(body.get("format")),
                "readOnly": bool(body.get("readOnly")) if body.get("readOnly") is not None else False,
                "required": _is_required_property(parent_schema, property_name),
                "type": _schema_type(body),
                "x-key": _string_list(body.get("x-key")),
                "xExtensions": _x_extensions(body),
                "x-supportedDeviceType": _supported_device_types(body),
                "x-path": body.get("x-path") if isinstance(body.get("x-path"), str) else "",
            },
        )
        state.property_by_pointer[ast_node.json_pointer] = node

        parent_semantic = (
            state.schema_by_pointer.get(parent_schema.json_pointer)
            if parent_schema is not None
            else None
        )
        state.add_edge(
            parent_semantic,
            node,
            kind="HAS_PROPERTY",
            rule_id=f"{STRUCTURAL_RULE_PACK_ID}.schema.properties",
            evidence={"propertyPointer": ast_node.json_pointer},
        )

        type_pointer = _schema_type_pointer(body, ast_node.json_pointer)
        type_node = state.schema_by_pointer.get(type_pointer)
        state.add_edge(
            node,
            type_node,
            kind="PROPERTY_OF_TYPE",
            rule_id=f"{STRUCTURAL_RULE_PACK_ID}.property.type",
            evidence={"schemaPointer": type_pointer},
        )

        items = body.get("items")
        if isinstance(items, dict):
            item_pointer = _schema_type_pointer(
                items,
                _join_pointer(ast_node.json_pointer, "items"),
            )
            state.add_edge(
                node,
                state.schema_by_pointer.get(item_pointer),
                kind="HAS_ITEM_SCHEMA",
                rule_id=f"{STRUCTURAL_RULE_PACK_ID}.property.items",
                evidence={
                    "itemPointer": item_pointer,
                    "propertyPointer": ast_node.json_pointer,
                },
            )

        yang_path = body.get("x-path")
        if isinstance(yang_path, str) and yang_path:
            yang_node = _ensure_yang_node(state, yang_path, ast_node.json_pointer)
            state.add_edge(
                node,
                yang_node,
                kind="PROPERTY_AT_YANG",
                rule_id=f"{STRUCTURAL_RULE_PACK_ID}.property.x-path",
                evidence={"propertyPointer": ast_node.json_pointer, "x-path": yang_path},
            )


def _build_schema_edges(state: _SemanticState) -> None:
    for pointer, node in state.schema_by_pointer.items():
        body = _get_pointer(state.ast_graph.spec, pointer)
        if not isinstance(body, dict):
            continue

        ref_pointer = _internal_ref_pointer(body.get("$ref"))
        if ref_pointer:
            state.add_edge(
                node,
                state.schema_by_pointer.get(ref_pointer),
                kind="REFERENCES",
                rule_id=f"{STRUCTURAL_RULE_PACK_ID}.schema.ref",
                evidence={"ref": body.get("$ref"), "schemaPointer": pointer},
            )

        for composition in ("allOf", "anyOf", "oneOf"):
            entries = body.get(composition)
            if not isinstance(entries, list):
                continue
            for index, child in enumerate(entries):
                if not isinstance(child, dict):
                    continue
                child_pointer = _join_pointer(pointer, composition, str(index))
                target_pointer = _schema_type_pointer(child, child_pointer)
                state.add_edge(
                    node,
                    state.schema_by_pointer.get(target_pointer),
                    kind="COMPOSED_OF",
                    rule_id=f"{STRUCTURAL_RULE_PACK_ID}.schema.composition",
                    evidence={
                        "composition": composition,
                        "index": index,
                        "schemaPointer": pointer,
                        "targetPointer": target_pointer,
                    },
                )

        for map_key in ("items", "additionalProperties"):
            child = body.get(map_key)
            if isinstance(child, dict):
                child_pointer = _join_pointer(pointer, map_key)
                target_pointer = _schema_type_pointer(child, child_pointer)
                state.add_edge(
                    node,
                    state.schema_by_pointer.get(target_pointer),
                    kind="HAS_VALUE_SCHEMA",
                    rule_id=f"{STRUCTURAL_RULE_PACK_ID}.schema.value",
                    evidence={
                        "role": map_key,
                        "schemaPointer": pointer,
                        "targetPointer": target_pointer,
                    },
                )


def _build_endpoint_nodes_and_edges(state: _SemanticState) -> None:
    paths = state.ast_graph.spec.get("paths")
    if not isinstance(paths, dict):
        return
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            op_pointer = _join_pointer("", "paths", path, method)
            endpoint = state.add_node(
                kind="ApiEndpoint",
                stable_key=f"endpoint:{method.upper()}:{path}",
                name=f"{method.upper()} {path}",
                ast_pointer=op_pointer,
                summary={
                    "description": _as_str(operation.get("description")),
                    "method": method.upper(),
                    "operationId": _as_str(operation.get("operationId")),
                    "path": path,
                    "summary": _as_str(operation.get("summary")),
                    "tags": [v for v in operation.get("tags", []) if isinstance(v, str)],
                },
            )
            _add_cli_command(state, endpoint, operation, op_pointer)
            _add_parameter_edges(state, endpoint, path_item, operation, op_pointer)
            _add_request_body_edges(state, endpoint, operation, op_pointer)
            _add_response_edges(state, endpoint, operation, op_pointer)


def _add_cli_command(
    state: _SemanticState,
    endpoint: SemanticNode,
    operation: dict[str, Any],
    op_pointer: str,
) -> None:
    cli = operation.get("x-cliParam")
    if not isinstance(cli, dict):
        return
    command_name = _as_str(cli.get("commandName")).strip()
    if not command_name:
        return
    param_keys: list[str] = []
    raw_param_keys = cli.get("paramKeys")
    if isinstance(raw_param_keys, list):
        for entry in raw_param_keys:
            if isinstance(entry, str):
                param_keys.append(entry)
            elif isinstance(entry, dict) and isinstance(entry.get("key"), str):
                param_keys.append(entry["key"])
    node = state.add_node(
        kind="CliCommand",
        stable_key=f"cli:{endpoint.stable_key}:{command_name}",
        name=command_name,
        ast_pointer=_join_pointer(op_pointer, "x-cliParam"),
        summary={
            "commandName": command_name,
            "commandUse": _as_str(cli.get("commandUse")),
            "parentCommand": _as_str(cli.get("parentCommand")),
            "paramKeys": param_keys,
            "pathToPrint": _as_str(cli.get("pathToPrint")),
        },
    )
    state.add_edge(
        endpoint,
        node,
        kind="HAS_CLI_COMMAND",
        rule_id=f"{STRUCTURAL_RULE_PACK_ID}.operation.x-cliParam",
        evidence={"operationPointer": op_pointer},
    )


def _add_parameter_edges(
    state: _SemanticState,
    endpoint: SemanticNode,
    path_item: dict[str, Any],
    operation: dict[str, Any],
    op_pointer: str,
) -> None:
    merged: dict[tuple[str, str], tuple[dict[str, Any], str, str]] = {}
    for owner, params in (
        (_parent_pointer(op_pointer), path_item.get("parameters")),
        (op_pointer, operation.get("parameters")),
    ):
        if not isinstance(params, list):
            continue
        for index, parameter in enumerate(params):
            if not isinstance(parameter, dict):
                continue
            param_pointer = _join_pointer(owner, "parameters", str(index))
            body, body_pointer = _resolve_reference_object(
                state.ast_graph.spec,
                parameter,
                param_pointer,
            )
            if not isinstance(body, dict):
                continue
            name = _as_str(body.get("name"))
            location = _as_str(body.get("in"))
            if not name or not location:
                continue
            merged[(location, name)] = (body, body_pointer, param_pointer)

    for body, body_pointer, param_pointer in merged.values():
        name = _as_str(body.get("name"))
        location = _as_str(body.get("in"))
        schema = body.get("schema")
        schema_pointer = (
            _join_pointer(body_pointer, "schema")
            if isinstance(schema, dict)
            else ""
        )
        target_pointer = (
            _schema_type_pointer(schema, schema_pointer)
            if isinstance(schema, dict)
            else ""
        )
        node = state.add_node(
            kind="Parameter",
            stable_key=f"parameter:{endpoint.stable_key}:{location}:{name}",
            name=name,
            ast_pointer=body_pointer,
            summary={
                "description": _as_str(body.get("description")),
                "enumValues": _enum_values(schema) if isinstance(schema, dict) else [],
                "format": _as_str(schema.get("format")) if isinstance(schema, dict) else "",
                "in": location,
                "inferredHint": _infer_param_hint(body),
                "name": name,
                "pattern": _as_str(schema.get("pattern")) if isinstance(schema, dict) else "",
                "required": bool(body.get("required")),
                "schemaPointer": schema_pointer,
                "targetPointer": target_pointer,
                "type": _schema_type(schema) if isinstance(schema, dict) else "",
            },
        )
        state.add_edge(
            endpoint,
            node,
            kind="HAS_PARAMETER",
            rule_id=f"{STRUCTURAL_RULE_PACK_ID}.operation.parameter",
            evidence={
                "operationPointer": op_pointer,
                "parameterPointer": body_pointer,
                "sourcePointer": param_pointer,
            },
        )
        if target_pointer:
            state.add_edge(
                node,
                state.schema_by_pointer.get(target_pointer),
                kind="PARAMETER_REFERENCES",
                rule_id=f"{STRUCTURAL_RULE_PACK_ID}.parameter.schema",
                evidence={
                    "parameterPointer": body_pointer,
                    "schemaPointer": schema_pointer,
                    "targetPointer": target_pointer,
                },
            )


def _add_request_body_edges(
    state: _SemanticState,
    endpoint: SemanticNode,
    operation: dict[str, Any],
    op_pointer: str,
) -> None:
    request_body = operation.get("requestBody")
    if not isinstance(request_body, dict):
        return
    request_pointer = _join_pointer(op_pointer, "requestBody")
    body, body_pointer = _resolve_reference_object(
        state.ast_graph.spec, request_body, request_pointer
    )
    if not isinstance(body, dict):
        return
    media_schemas = _iter_media_schemas(body.get("content"), body_pointer)
    if not media_schemas:
        body_node = state.add_node(
            kind="RequestBody",
            stable_key=f"requestBody:{endpoint.stable_key}:",
            name=f"{endpoint.name} requestBody",
            ast_pointer=body_pointer,
            summary={
                "contentType": "",
                "description": _as_str(body.get("description")),
                "required": bool(body.get("required")),
                "requestBodyPointer": body_pointer,
                "sourcePointer": request_pointer,
            },
        )
        state.add_edge(
            endpoint,
            body_node,
            kind="HAS_REQUEST_BODY",
            rule_id=f"{STRUCTURAL_RULE_PACK_ID}.operation.requestBody.node",
            evidence={"operationPointer": op_pointer, "requestBodyPointer": body_pointer},
        )
        return
    for media, schema, schema_pointer in media_schemas:
        _add_request_body_media_edges(
            state,
            endpoint,
            body,
            body_pointer,
            request_pointer,
            op_pointer,
            media,
            schema,
            schema_pointer,
        )


def _add_request_body_media_edges(
    state: _SemanticState,
    endpoint: SemanticNode,
    body: dict[str, Any],
    body_pointer: str,
    request_pointer: str,
    op_pointer: str,
    media: str,
    schema: dict[str, Any],
    schema_pointer: str,
) -> None:
    body_node = state.add_node(
        kind="RequestBody",
        stable_key=f"requestBody:{endpoint.stable_key}:{media}",
        name=f"{endpoint.name} requestBody",
        ast_pointer=body_pointer,
        summary={
            "contentType": media,
            "description": _as_str(body.get("description")),
            "required": bool(body.get("required")),
            "requestBodyPointer": body_pointer,
            "sourcePointer": request_pointer,
        },
    )
    state.add_edge(
        endpoint,
        body_node,
        kind="HAS_REQUEST_BODY",
        rule_id=f"{STRUCTURAL_RULE_PACK_ID}.operation.requestBody.node",
        evidence={"operationPointer": op_pointer, "requestBodyPointer": body_pointer},
    )
    target_pointer = _schema_type_pointer(schema, schema_pointer)
    target = state.schema_by_pointer.get(target_pointer)
    state.add_edge(
        body_node,
        target,
        kind="BODY_REFERENCES",
        rule_id=f"{STRUCTURAL_RULE_PACK_ID}.requestBody.schema",
        evidence={
            "contentType": media,
            "requestBodyPointer": body_pointer,
            "schemaPointer": schema_pointer,
            "targetPointer": target_pointer,
        },
    )
    state.add_edge(
        endpoint,
        target,
        kind="ACCEPTS_SCHEMA",
        rule_id=f"{STRUCTURAL_RULE_PACK_ID}.operation.requestBody",
        evidence={
            "contentType": media,
            "requestBodyPointer": body_pointer,
            "schemaPointer": schema_pointer,
            "targetPointer": target_pointer,
        },
    )
    for yang_path in _collect_yang_paths(state.ast_graph.spec, schema, schema_pointer):
        state.add_edge(
            endpoint,
            _ensure_yang_node(state, yang_path, schema_pointer),
            kind="CONFIGURES_YANG",
            rule_id=f"{STRUCTURAL_RULE_PACK_ID}.operation.requestBody.x-path",
            evidence={"schemaPointer": schema_pointer, "x-path": yang_path},
        )


def _add_response_edges(
    state: _SemanticState,
    endpoint: SemanticNode,
    operation: dict[str, Any],
    op_pointer: str,
) -> None:
    responses = operation.get("responses")
    if not isinstance(responses, dict):
        return
    for status, response in responses.items():
        if not isinstance(response, dict):
            continue
        response_pointer = _join_pointer(op_pointer, "responses", str(status))
        body, body_pointer = _resolve_reference_object(
            state.ast_graph.spec, response, response_pointer
        )
        if not isinstance(body, dict):
            continue
        content = body.get("content")
        if isinstance(content, dict) and content:
            for media, media_type in content.items():
                media_name = str(media)
                schema = media_type.get("schema") if isinstance(media_type, dict) else None
                if isinstance(schema, dict):
                    _add_response_media_edges(
                        state,
                        endpoint,
                        body,
                        body_pointer,
                        response_pointer,
                        op_pointer,
                        str(status),
                        media_name,
                        schema,
                        _join_pointer(body_pointer, "content", media_name, "schema"),
                    )
                    continue
                _add_schema_less_response_edge(
                    state,
                    endpoint,
                    body,
                    body_pointer,
                    response_pointer,
                    op_pointer,
                    str(status),
                    media_name,
                )
            continue
        _add_schema_less_response_edge(
            state,
            endpoint,
            body,
            body_pointer,
            response_pointer,
            op_pointer,
            str(status),
            "",
        )


def _add_schema_less_response_edge(
    state: _SemanticState,
    endpoint: SemanticNode,
    body: dict[str, Any],
    body_pointer: str,
    response_pointer: str,
    op_pointer: str,
    status: str,
    media: str,
) -> None:
    response_node = _add_response_node(
        state,
        endpoint,
        body,
        body_pointer,
        response_pointer,
        status,
        media,
    )
    state.add_edge(
        endpoint,
        response_node,
        kind="HAS_RESPONSE",
        rule_id=f"{STRUCTURAL_RULE_PACK_ID}.operation.response.node",
        evidence={"operationPointer": op_pointer, "responsePointer": body_pointer},
    )


def _add_response_media_edges(
    state: _SemanticState,
    endpoint: SemanticNode,
    body: dict[str, Any],
    body_pointer: str,
    response_pointer: str,
    op_pointer: str,
    status: str,
    media: str,
    schema: dict[str, Any],
    schema_pointer: str,
) -> None:
    response_node = _add_response_node(
        state,
        endpoint,
        body,
        body_pointer,
        response_pointer,
        status,
        media,
    )
    state.add_edge(
        endpoint,
        response_node,
        kind="HAS_RESPONSE",
        rule_id=f"{STRUCTURAL_RULE_PACK_ID}.operation.response.node",
        evidence={"operationPointer": op_pointer, "responsePointer": body_pointer},
    )
    target_pointer = _schema_type_pointer(schema, schema_pointer)
    target = state.schema_by_pointer.get(target_pointer)
    state.add_edge(
        response_node,
        target,
        kind="RESPONSE_REFERENCES",
        rule_id=f"{STRUCTURAL_RULE_PACK_ID}.response.schema",
        evidence={
            "contentType": media,
            "responsePointer": body_pointer,
            "schemaPointer": schema_pointer,
            "status": str(status),
            "targetPointer": target_pointer,
        },
    )
    state.add_edge(
        endpoint,
        target,
        kind="RETURNS_SCHEMA",
        rule_id=f"{STRUCTURAL_RULE_PACK_ID}.operation.response",
        evidence={
            "contentType": media,
            "responsePointer": body_pointer,
            "schemaPointer": schema_pointer,
            "status": str(status),
            "targetPointer": target_pointer,
        },
    )


def _add_response_node(
    state: _SemanticState,
    endpoint: SemanticNode,
    body: dict[str, Any],
    body_pointer: str,
    response_pointer: str,
    status: str,
    media: str,
) -> SemanticNode:
    return state.add_node(
        kind="Response",
        stable_key=f"response:{endpoint.stable_key}:{status}:{media}",
        name=f"{endpoint.name} {status}",
        ast_pointer=body_pointer,
        summary={
            "contentType": media,
            "description": _as_str(body.get("description")),
            "responsePointer": body_pointer,
            "sourcePointer": response_pointer,
            "status": str(status),
        },
    )


def _build_model_identity_overlay(state: _SemanticState) -> None:
    node_by_id = {node.semantic_id: node for node in state.semantic_graph.nodes}
    schema_model_by_id: dict[str, SemanticNode] = {}
    property_model_by_id: dict[str, SemanticNode] = {}
    property_parent_schema_by_id: dict[str, SemanticNode] = {}

    for node in list(state.semantic_graph.nodes):
        if node.kind != "SchemaComponent":
            continue
        summary = _load_summary(node)
        identity_key = _schema_identity_key(node, summary)
        model = _ensure_model_entity(
            state,
            identity_key=identity_key,
            name=node.name,
            ast_pointer=node.json_pointer,
            summary={
                "bodyShape": summary.get("bodyShape", ""),
                "identityKey": identity_key,
                "identityType": "schema",
                "sourceKind": node.kind,
                "sourcePointer": node.json_pointer,
                "supportedDeviceTypes": summary.get("x-supportedDeviceType"),
                "type": summary.get("type", ""),
                "yangPath": summary.get("x-path", ""),
            },
        )
        schema_model_by_id[node.semantic_id] = model
        state.add_edge(
            node,
            model,
            kind="REPRESENTS_MODEL",
            rule_id=f"{IDENTITY_RULE_PACK_ID}.schema.identity",
            evidence={"identityKey": identity_key, "schemaPointer": node.json_pointer},
        )
        yang_path = summary.get("x-path")
        if isinstance(yang_path, str) and yang_path:
            state.add_edge(
                model,
                _ensure_yang_node(state, yang_path, node.json_pointer),
                kind="MODEL_AT_YANG",
                rule_id=f"{IDENTITY_RULE_PACK_ID}.schema.x-path",
                evidence={"identityKey": identity_key, "x-path": yang_path},
            )

    for edge in list(state.semantic_graph.edges):
        if edge.kind != "HAS_PROPERTY":
            continue
        parent = node_by_id.get(edge.source_id)
        prop = node_by_id.get(edge.target_id)
        if parent is None or prop is None:
            continue
        property_parent_schema_by_id[prop.semantic_id] = parent

    for node in list(state.semantic_graph.nodes):
        if node.kind != "Property":
            continue
        summary = _load_summary(node)
        parent_schema = property_parent_schema_by_id.get(node.semantic_id)
        parent_model = (
            schema_model_by_id.get(parent_schema.semantic_id)
            if parent_schema is not None
            else None
        )
        identity_key = _property_identity_key(node, summary, parent_model)
        model = _ensure_model_entity(
            state,
            identity_key=identity_key,
            name=node.name,
            ast_pointer=node.json_pointer,
            summary={
                "identityKey": identity_key,
                "identityType": "property",
                "keyFields": summary.get("x-key", []),
                "parentIdentityKey": _summary_value(parent_model, "identityKey"),
                "required": summary.get("required", False),
                "sourceKind": node.kind,
                "sourcePointer": node.json_pointer,
                "supportedDeviceTypes": summary.get("x-supportedDeviceType"),
                "type": summary.get("type", ""),
                "yangPath": summary.get("x-path", ""),
            },
        )
        property_model_by_id[node.semantic_id] = model
        state.add_edge(
            node,
            model,
            kind="REPRESENTS_MODEL",
            rule_id=f"{IDENTITY_RULE_PACK_ID}.property.identity",
            evidence={"identityKey": identity_key, "propertyPointer": node.json_pointer},
        )
        state.add_edge(
            parent_model,
            model,
            kind="MODEL_HAS_PROPERTY",
            rule_id=f"{IDENTITY_RULE_PACK_ID}.schema.property",
            evidence={
                "identityKey": identity_key,
                "parentIdentityKey": _summary_value(parent_model, "identityKey"),
                "propertyPointer": node.json_pointer,
            },
        )
        yang_path = summary.get("x-path")
        if isinstance(yang_path, str) and yang_path:
            state.add_edge(
                model,
                _ensure_yang_node(state, yang_path, node.json_pointer),
                kind="MODEL_AT_YANG",
                rule_id=f"{IDENTITY_RULE_PACK_ID}.property.x-path",
                evidence={"identityKey": identity_key, "x-path": yang_path},
            )

    _add_endpoint_model_shortcuts(
        state,
        {node.semantic_id: node for node in state.semantic_graph.nodes},
        schema_model_by_id,
        property_model_by_id,
    )


def _add_endpoint_model_shortcuts(
    state: _SemanticState,
    node_by_id: dict[str, SemanticNode],
    schema_model_by_id: dict[str, SemanticNode],
    property_model_by_id: dict[str, SemanticNode],
) -> None:
    yang_to_models: dict[str, list[SemanticNode]] = {}
    for edge in state.semantic_graph.edges:
        if edge.kind != "MODEL_AT_YANG":
            continue
        model = node_by_id.get(edge.source_id)
        yang = node_by_id.get(edge.target_id)
        if model is not None and yang is not None:
            yang_to_models.setdefault(yang.semantic_id, []).append(model)

    for edge in list(state.semantic_graph.edges):
        source = node_by_id.get(edge.source_id)
        target = node_by_id.get(edge.target_id)
        if source is None or target is None:
            continue
        if source.kind == "ApiEndpoint" and edge.kind == "ACCEPTS_SCHEMA":
            state.add_edge(
                source,
                schema_model_by_id.get(target.semantic_id),
                kind="ACCEPTS_MODEL",
                rule_id=f"{IDENTITY_RULE_PACK_ID}.operation.requestModel",
                evidence={
                    "schemaSemanticId": target.semantic_id,
                    "structuralEvidence": _load_edge_evidence(edge),
                },
            )
        elif source.kind == "ApiEndpoint" and edge.kind == "RETURNS_SCHEMA":
            state.add_edge(
                source,
                schema_model_by_id.get(target.semantic_id),
                kind="RETURNS_MODEL",
                rule_id=f"{IDENTITY_RULE_PACK_ID}.operation.responseModel",
                evidence={
                    "schemaSemanticId": target.semantic_id,
                    "structuralEvidence": _load_edge_evidence(edge),
                },
            )
        elif source.kind == "RequestBody" and edge.kind == "BODY_REFERENCES":
            state.add_edge(
                source,
                schema_model_by_id.get(target.semantic_id),
                kind="BODY_MODEL",
                rule_id=f"{IDENTITY_RULE_PACK_ID}.requestBody.model",
                evidence={
                    "schemaSemanticId": target.semantic_id,
                    "structuralEvidence": _load_edge_evidence(edge),
                },
            )
        elif source.kind == "Response" and edge.kind == "RESPONSE_REFERENCES":
            state.add_edge(
                source,
                schema_model_by_id.get(target.semantic_id),
                kind="RESPONSE_MODEL",
                rule_id=f"{IDENTITY_RULE_PACK_ID}.response.model",
                evidence={
                    "schemaSemanticId": target.semantic_id,
                    "structuralEvidence": _load_edge_evidence(edge),
                },
            )
        elif source.kind == "Property" and edge.kind == "PROPERTY_OF_TYPE":
            property_model = property_model_by_id.get(source.semantic_id)
            schema_model = schema_model_by_id.get(target.semantic_id)
            if property_model is not None and property_model != schema_model:
                state.add_edge(
                    property_model,
                    schema_model,
                    kind="MODEL_OF_TYPE",
                    rule_id=f"{IDENTITY_RULE_PACK_ID}.property.typeModel",
                    evidence={
                        "propertySemanticId": source.semantic_id,
                        "schemaSemanticId": target.semantic_id,
                        "structuralEvidence": _load_edge_evidence(edge),
                    },
                )
        elif source.kind == "Property" and edge.kind == "HAS_ITEM_SCHEMA":
            state.add_edge(
                property_model_by_id.get(source.semantic_id),
                schema_model_by_id.get(target.semantic_id),
                kind="MODEL_HAS_ITEM_SCHEMA",
                rule_id=f"{IDENTITY_RULE_PACK_ID}.property.itemModel",
                evidence={
                    "propertySemanticId": source.semantic_id,
                    "schemaSemanticId": target.semantic_id,
                    "structuralEvidence": _load_edge_evidence(edge),
                },
            )
        elif source.kind == "SchemaComponent" and edge.kind == "COMPOSED_OF":
            state.add_edge(
                schema_model_by_id.get(source.semantic_id),
                schema_model_by_id.get(target.semantic_id),
                kind="MODEL_COMPOSED_OF",
                rule_id=f"{IDENTITY_RULE_PACK_ID}.schema.compositionModel",
                evidence={
                    "sourceSchemaSemanticId": source.semantic_id,
                    "targetSchemaSemanticId": target.semantic_id,
                    "structuralEvidence": _load_edge_evidence(edge),
                },
            )
        elif source.kind == "SchemaComponent" and edge.kind == "REFERENCES":
            state.add_edge(
                schema_model_by_id.get(source.semantic_id),
                schema_model_by_id.get(target.semantic_id),
                kind="MODEL_REFERENCES",
                rule_id=f"{IDENTITY_RULE_PACK_ID}.schema.referenceModel",
                evidence={
                    "sourceSchemaSemanticId": source.semantic_id,
                    "targetSchemaSemanticId": target.semantic_id,
                    "structuralEvidence": _load_edge_evidence(edge),
                },
            )
        elif source.kind == "SchemaComponent" and edge.kind == "HAS_VALUE_SCHEMA":
            state.add_edge(
                schema_model_by_id.get(source.semantic_id),
                schema_model_by_id.get(target.semantic_id),
                kind="MODEL_HAS_VALUE_SCHEMA",
                rule_id=f"{IDENTITY_RULE_PACK_ID}.schema.valueModel",
                evidence={
                    "sourceSchemaSemanticId": source.semantic_id,
                    "targetSchemaSemanticId": target.semantic_id,
                    "structuralEvidence": _load_edge_evidence(edge),
                },
            )
        elif source.kind == "ApiEndpoint" and edge.kind == "CONFIGURES_YANG":
            for model in yang_to_models.get(target.semantic_id, []):
                state.add_edge(
                    source,
                    model,
                    kind="CONFIGURES_MODEL",
                    rule_id=f"{IDENTITY_RULE_PACK_ID}.operation.yangModel",
                    evidence={
                        "structuralEvidence": _load_edge_evidence(edge),
                        "yangSemanticId": target.semantic_id,
                    },
                )


def _ensure_model_entity(
    state: _SemanticState,
    *,
    identity_key: str,
    name: str,
    ast_pointer: str,
    summary: dict[str, Any],
) -> SemanticNode:
    existing = state.model_by_key.get(identity_key)
    if existing is not None:
        merged_summary = _merge_model_summary(_load_summary(existing), summary)
        if merged_summary == _load_summary(existing):
            return existing
        replacement = SemanticNode(
            semantic_id=existing.semantic_id,
            spec_id=existing.spec_id,
            kind=existing.kind,
            name=existing.name,
            ast_node_id=existing.ast_node_id,
            json_pointer=existing.json_pointer,
            stable_key=existing.stable_key,
            summary_json=_json(merged_summary),
        )
        state.model_by_key[identity_key] = replacement
        state.semantic_by_key[(existing.kind, existing.stable_key)] = replacement
        for index, node in enumerate(state.semantic_graph.nodes):
            if node.semantic_id == existing.semantic_id:
                state.semantic_graph.nodes[index] = replacement
                break
        return replacement
    node = state.add_node(
        kind="ModelEntity",
        stable_key=f"model:{identity_key}",
        name=name,
        ast_pointer=ast_pointer,
        summary=summary,
    )
    state.model_by_key[identity_key] = node
    return node


def _merge_model_summary(
    existing: dict[str, Any],
    incoming: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in incoming.items():
        if _is_empty_summary_value(value):
            continue
        current = merged.get(key)
        if _is_empty_summary_value(current):
            merged[key] = value
            continue
        if current == value:
            continue
        if isinstance(current, list) or isinstance(value, list):
            merged[key] = _merge_summary_lists(current, value)
        elif key in {"identityType", "sourceKind"}:
            merged[f"{key}s"] = _merge_summary_lists(
                merged.get(f"{key}s", current),
                value,
            )
        elif key == "sourcePointer":
            merged["sourcePointers"] = _merge_summary_lists(
                merged.get("sourcePointers", current),
                value,
            )
        elif key == "required":
            merged[key] = bool(current) or bool(value)
    return merged


def _merge_summary_lists(left: Any, right: Any) -> list[Any]:
    values: list[Any] = []
    for raw in (left, right):
        items = raw if isinstance(raw, list) else [raw]
        for item in items:
            if _is_empty_summary_value(item) or item in values:
                continue
            values.append(item)
    return values


def _is_empty_summary_value(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _ensure_yang_node(
    state: _SemanticState,
    yang_path: str,
    evidence_pointer: str,
) -> SemanticNode:
    existing = state.yang_by_path.get(yang_path)
    if existing is not None:
        return existing
    node = state.add_node(
        kind="YangPath",
        stable_key=f"yang:{yang_path}",
        name=yang_path,
        ast_pointer=evidence_pointer,
        summary={"module": _yang_module_for(yang_path), "yangPath": yang_path},
    )
    state.yang_by_path[yang_path] = node
    return node


def _parent_by_child(ast_graph: AstGraph) -> dict[str, AstNode]:
    nodes = {n.node_id: n for n in ast_graph.nodes}
    result: dict[str, AstNode] = {}
    for edge in ast_graph.child_edges:
        parent = nodes.get(edge.parent_id)
        if parent is not None:
            result[edge.child_id] = parent
    return result


def _parent_schema_node(
    state: _SemanticState,
    property_node: AstNode,
    parent_by_child: dict[str, AstNode],
) -> AstNode | None:
    map_node = parent_by_child.get(property_node.node_id)
    if map_node is None:
        return None
    parent = parent_by_child.get(map_node.node_id)
    while parent is not None and parent.json_pointer not in state.schema_by_pointer:
        parent = parent_by_child.get(parent.node_id)
    return parent


def _is_required_property(parent_schema: AstNode | None, property_name: str) -> bool:
    if parent_schema is None:
        return False
    body = _load_object(parent_schema.raw_json)
    if not body:
        return False
    required = body.get("required")
    return isinstance(required, list) and property_name in required


def _is_schema_ast_node(ast_node: AstNode) -> bool:
    if ast_node.value_type != "object":
        return False
    body = _load_object(ast_node.raw_json)
    if body is None:
        return False
    if _is_named_component_schema(ast_node.json_pointer):
        return True
    if _is_ref_only_schema(body):
        return False
    if _is_property_ast_node(ast_node):
        return _property_schema_is_structural(body)
    if ast_node.kind in {"Schema", "Items"}:
        return True
    parts = _pointer_parts(ast_node.json_pointer)
    if parts and parts[-1] in _SCHEMA_CHILD_KEYS:
        return True
    return len(parts) >= 2 and parts[-2] in {
        "allOf",
        "anyOf",
        "oneOf",
        "prefixItems",
        *_SCHEMA_MAP_KEYS,
    }


def _is_property_ast_node(ast_node: AstNode) -> bool:
    if ast_node.value_type != "object":
        return False
    if ast_node.kind == "Property":
        return True
    parts = _pointer_parts(ast_node.json_pointer)
    return len(parts) >= 2 and parts[-2] in {"properties", "patternProperties"}


def _is_ref_only_schema(body: dict[str, Any]) -> bool:
    """Return whether a schema is only a transparent reference wrapper."""
    if not isinstance(body.get("$ref"), str):
        return False
    return set(body).issubset({"$ref", "description", "summary", "title"})


def _property_schema_is_structural(body: dict[str, Any]) -> bool:
    """Return whether a property needs its own traversable schema node."""
    if isinstance(body.get("properties"), dict):
        return True
    if body.get("additionalProperties") is True or isinstance(
        body.get("additionalProperties"),
        dict,
    ):
        return True
    return any(isinstance(body.get(key), list) and body[key] for key in (
        "allOf",
        "anyOf",
        "oneOf",
    ))


def _schema_name(ast_node: AstNode) -> str:
    if _is_named_component_schema(ast_node.json_pointer):
        return _last_pointer_part(ast_node.json_pointer)
    if ast_node.kind == "Items":
        parent_name = _last_pointer_part(_parent_pointer(ast_node.json_pointer))
        return f"{parent_name}.items" if parent_name else "items"
    return ast_node.name or ast_node.key or _last_pointer_part(ast_node.json_pointer) or "schema"


def _is_named_component_schema(pointer: str) -> bool:
    parts = _pointer_parts(pointer)
    return len(parts) == 3 and parts[0] == "components" and parts[1] == "schemas"


def _schema_type_pointer(schema: dict[str, Any], pointer: str) -> str:
    ref_pointer = _internal_ref_pointer(schema.get("$ref"))
    if ref_pointer:
        return ref_pointer
    items = schema.get("items")
    if isinstance(items, dict):
        items_ref_pointer = _internal_ref_pointer(items.get("$ref"))
        if items_ref_pointer:
            return items_ref_pointer
        return _join_pointer(pointer, "items")
    return pointer


def _pick_media_schema(
    content: Any,
    content_owner_pointer: str,
) -> tuple[str, dict[str, Any] | None, str | None]:
    if not isinstance(content, dict) or not content:
        return "", None, None
    preferred = None
    if "application/json" in content:
        preferred = "application/json"
    else:
        for media in content:
            if isinstance(media, str) and media.startswith("application/") and media.endswith("+json"):
                preferred = media
                break
    if preferred is None:
        preferred = next(iter(content))
    media_type = content.get(preferred)
    if not isinstance(media_type, dict):
        return str(preferred), None, None
    schema = media_type.get("schema")
    if not isinstance(schema, dict):
        return str(preferred), None, None
    return str(preferred), schema, _join_pointer(content_owner_pointer, "content", str(preferred), "schema")


def _iter_media_schemas(
    content: Any,
    content_owner_pointer: str,
) -> list[tuple[str, dict[str, Any], str]]:
    if not isinstance(content, dict) or not content:
        return []
    result: list[tuple[str, dict[str, Any], str]] = []
    for media, media_type in content.items():
        if not isinstance(media_type, dict):
            continue
        schema = media_type.get("schema")
        if not isinstance(schema, dict):
            continue
        media_name = str(media)
        result.append(
            (
                media_name,
                schema,
                _join_pointer(content_owner_pointer, "content", media_name, "schema"),
            )
        )
    return result


def _iter_media_types(content: Any) -> list[str]:
    if not isinstance(content, dict) or not content:
        return []
    return [str(media) for media in content if isinstance(media, str)]


def _resolve_reference_object(
    spec: dict[str, Any],
    obj: dict[str, Any],
    pointer: str,
) -> tuple[dict[str, Any] | None, str]:
    ref_pointer = _internal_ref_pointer(obj.get("$ref"))
    if not ref_pointer:
        return obj, pointer
    target = _get_pointer(spec, ref_pointer)
    return (target, ref_pointer) if isinstance(target, dict) else (None, ref_pointer)


def _collect_yang_paths(
    spec: dict[str, Any],
    schema: dict[str, Any],
    pointer: str,
    seen_refs: set[str] | None = None,
) -> set[str]:
    seen_refs = seen_refs or set()
    result: set[str] = set()
    yang_path = schema.get("x-path")
    if isinstance(yang_path, str) and yang_path:
        result.add(yang_path)

    ref_pointer = _internal_ref_pointer(schema.get("$ref"))
    if ref_pointer and ref_pointer not in seen_refs:
        seen_refs.add(ref_pointer)
        target = _get_pointer(spec, ref_pointer)
        if isinstance(target, dict):
            result.update(_collect_yang_paths(spec, target, ref_pointer, seen_refs))

    for key, value in schema.items():
        if key == "$ref":
            continue
        child_pointer = _join_pointer(pointer, str(key))
        if isinstance(value, dict):
            result.update(_collect_yang_paths(spec, value, child_pointer, seen_refs))
        elif isinstance(value, list):
            for index, entry in enumerate(value):
                if isinstance(entry, dict):
                    result.update(
                        _collect_yang_paths(
                            spec,
                            entry,
                            _join_pointer(child_pointer, str(index)),
                            seen_refs,
                        )
                    )
    return result


def _internal_ref_pointer(ref: Any) -> str:
    if ref in {"#", "#/"}:
        return ""
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return ""
    return ref[1:]


def _get_pointer(obj: Any, pointer: str) -> Any:
    if pointer in ("", "/"):
        return obj
    current = obj
    for part in _pointer_parts(pointer):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return current


def _pointer_parts(pointer: str) -> list[str]:
    if not pointer:
        return []
    return [_unescape_pointer(part) for part in pointer.strip("/").split("/")]


def _join_pointer(base: str, *parts: str) -> str:
    pointer = base.rstrip("/")
    for part in parts:
        pointer += "/" + _escape_pointer(part)
    return pointer


def _parent_pointer(pointer: str) -> str:
    parts = _pointer_parts(pointer)
    if not parts:
        return ""
    result = ""
    for part in parts[:-1]:
        result = _join_pointer(result, part)
    return result


def _last_pointer_part(pointer: str) -> str:
    parts = _pointer_parts(pointer)
    return parts[-1] if parts else ""


def _escape_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _unescape_pointer(value: str) -> str:
    return value.replace("~1", "/").replace("~0", "~")


def _schema_shape(body: dict[str, Any]) -> str:
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
    additional_properties = body.get("additionalProperties")
    if additional_properties is True or isinstance(additional_properties, dict):
        return "map"
    if body.get("type") == "object":
        return "object"
    if body.get("type") == "array":
        return "array"
    return "primitive"


def _component_kind(body: dict[str, Any]) -> str:
    if isinstance(body.get("enum"), list) and body["enum"]:
        return "primitive"
    for key in ("oneOf", "anyOf", "allOf"):
        if isinstance(body.get(key), list) and body[key]:
            return "union"
    additional_properties = body.get("additionalProperties")
    if additional_properties is True or isinstance(additional_properties, dict):
        return "map"
    if body.get("type") == "object" or "properties" in body:
        return "object"
    if body.get("type") == "array":
        return "array"
    return "primitive"


def _schema_type(body: dict[str, Any]) -> str:
    value = body.get("type")
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "|".join(str(v) for v in value)
    if "$ref" in body:
        return "$ref"
    return ""


def _enum_values(body: dict[str, Any] | None) -> list[str]:
    if not isinstance(body, dict):
        return []
    raw = body.get("enum")
    if not isinstance(raw, list):
        return []
    return [str(v) for v in raw if isinstance(v, (str, int, float, bool))]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str)]


def _x_extensions(body: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in body.items() if isinstance(k, str) and k.startswith("x-")}


def _supported_device_types(body: dict[str, Any]) -> list[str] | None:
    raw = body.get("x-supportedDeviceType")
    if raw is None:
        return None
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [v for v in raw if isinstance(v, str)]
    return None


def _infer_param_hint(param: dict[str, Any]) -> str:
    schema = param.get("schema") if isinstance(param.get("schema"), dict) else {}
    name = _as_str(param.get("name")).lower()
    fmt = _as_str(schema.get("format"))
    if fmt in {"date-time", "date"}:
        return f"rfc3339-{fmt}"
    if name in {"filter", "$filter"} or "odata" in name:
        return "odata-filter"
    if name in {"orderby", "$orderby", "order_by"}:
        return "odata-orderby"
    if "csv" in name or name.endswith("_list") or name.endswith("ids"):
        if schema.get("type") == "string":
            return "comma-list"
    if name in {"limit", "offset", "page", "page_size"}:
        return "pagination"
    return ""


def _yang_module_for(yang_path: str) -> str:
    for part in yang_path.split("/"):
        if ":" in part:
            return part.split(":", 1)[0]
    return ""


def _load_object(raw_json: str) -> dict[str, Any] | None:
    try:
        value = json.loads(raw_json)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _load_summary(node: SemanticNode) -> dict[str, Any]:
    try:
        value = json.loads(node.summary_json)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _load_edge_evidence(edge: SemanticEdge) -> dict[str, Any]:
    try:
        value = json.loads(edge.evidence_json)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _summary_value(node: SemanticNode | None, key: str) -> str:
    if node is None:
        return ""
    value = _load_summary(node).get(key)
    return value if isinstance(value, str) else ""


def _schema_identity_key(node: SemanticNode, summary: dict[str, Any]) -> str:
    yang_path = summary.get("x-path")
    if isinstance(yang_path, str) and yang_path:
        return f"yang:{yang_path}"
    parts = _pointer_parts(node.json_pointer)
    if len(parts) == 3 and parts[0] == "components" and parts[1] == "schemas":
        return f"schema:{_normalize_identity_token(parts[2])}"
    return f"schema-pointer:{node.json_pointer}"


def _property_identity_key(
    node: SemanticNode,
    summary: dict[str, Any],
    parent_model: SemanticNode | None,
) -> str:
    yang_path = summary.get("x-path")
    if isinstance(yang_path, str) and yang_path:
        return f"yang:{yang_path}"
    parent_key = _summary_value(parent_model, "identityKey")
    if parent_key:
        return f"{parent_key}.property:{_normalize_identity_token(node.name)}"
    return f"property-pointer:{node.json_pointer}"


def _normalize_identity_token(value: str) -> str:
    normalized = re.sub(r"[^0-9a-zA-Z]+", "-", value.strip()).strip("-").lower()
    return normalized or "unnamed"


def _semantic_id(spec_id: str, kind: str, stable_key: str) -> str:
    digest = hashlib.sha256(f"{spec_id}\0{kind}\0{stable_key}".encode("utf-8")).hexdigest()
    return f"sem:{digest[:24]}"
