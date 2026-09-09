"""Pre-flight validator for API-call tools (ADR 010).

Replaces the schema-inspection gate from ADR 009. Before
``call_central_api`` / ``call_greenlake_api`` dispatches an HTTP request,
the validator queries the graph for the endpoint's required ``Parameter``
and ``Property`` nodes and reports any missing required values. Unknown
body keys are returned as non-blocking warnings.

If the graph is unavailable the validator fails open (allows the call,
attaches a warning) — failing closed would block every API call on
graph hiccups.

Process scope, stateless: there is no per-session inspection tracking.
Every call gets the same validation pass.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..graph.manager import GraphManager


def normalise_path(path: str) -> str:
    """Return ``path`` trimmed of surrounding space and prefixed with a slash.

    Internal slash structure is preserved as-is — callers are responsible
    for collapsing accidental ``//`` if that matters to them.
    """
    p = (path or "").strip()
    return p if p.startswith("/") else f"/{p}"


def eid_for(method: str, path: str) -> str:
    """Canonical ``ApiEndpoint.endpoint_id`` for ``method`` / ``path``."""
    return f"{method.upper()}:{normalise_path(path)}"


@dataclass
class ValidationResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    schema_summary: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


_REQUIRED_PARAMS_QUERY = (
    "MATCH (e:ApiEndpoint {endpoint_id: $eid})-[:HAS_PARAMETER]->(p:Parameter) "
    "WHERE p.required = true "
    "RETURN p.name AS name, p.location AS location"
)

_REQUEST_BODY_PROPS_QUERY = (
    "MATCH (e:ApiEndpoint {endpoint_id: $eid})"
    "-[:HAS_REQUEST_BODY]->(:RequestBody)"
    "-[:BODY_REFERENCES]->(root:SchemaComponent) "
    "MATCH (root)-[:COMPOSED_OF*0..5]->(c:SchemaComponent)"
    "-[:HAS_PROPERTY]->(p:Property) "
    "RETURN DISTINCT p.name AS name, p.required AS required"
)

_PARAMS_FULL_QUERY = (
    "MATCH (e:ApiEndpoint {endpoint_id: $eid})-[:HAS_PARAMETER]->(p:Parameter) "
    "RETURN p.name AS name, p.location AS location, p.required AS required, "
    "p.type AS type"
)


def validate_call(
    graph_manager: "GraphManager | None",
    method: str,
    path: str,
    query_params: dict[str, str] | None,
    body: dict | None,
) -> ValidationResult:
    """Pre-flight validation against the graph.

    Errors block the call:
      * missing required query parameter (location='query');
      * POST only — missing required top-level body field.

    Warnings are advisory and surface on success:
      * POST/PATCH/PUT — body keys not declared in the schema.

    PATCH/PUT skip required-body checks because partial updates are
    expected. Path/header/cookie parameters are not validated (Central
    returns a clear 4xx if missing/wrong).
    """
    result = ValidationResult()
    if graph_manager is None or not getattr(graph_manager, "is_available", False):
        result.warnings.append(
            "Pre-flight validation skipped — graph database unavailable. "
            "Body and parameters were not checked against the schema."
        )
        return result

    eid = eid_for(method, path)

    try:
        param_rows = graph_manager.query(
            _REQUIRED_PARAMS_QUERY, {"eid": eid}, read_only=True
        )
    except Exception:
        param_rows = []
        result.warnings.append(
            "Pre-flight validation partially skipped — required-parameter "
            "lookup against the graph failed. Query parameters were not "
            "checked against the schema."
        )

    supplied_query = set((query_params or {}).keys())
    for row in param_rows or []:
        loc = (row.get("location") or "").lower()
        name = row.get("name") or ""
        if not name or loc != "query":
            continue
        if name not in supplied_query:
            result.errors.append(f"Missing required query parameter: {name!r}.")

    method_u = method.upper()
    if method_u in {"POST", "PUT", "PATCH"}:
        try:
            prop_rows = graph_manager.query(
                _REQUEST_BODY_PROPS_QUERY, {"eid": eid}, read_only=True
            )
        except Exception:
            prop_rows = []
            result.warnings.append(
                "Pre-flight validation partially skipped — request-body "
                "schema lookup against the graph failed. Body fields were "
                "not checked against the schema."
            )
        if prop_rows:
            known_names = {(r.get("name") or "") for r in prop_rows}
            known_names.discard("")
            required_names = {
                (r.get("name") or "") for r in prop_rows if r.get("required")
            }
            required_names.discard("")
            supplied_body = set((body or {}).keys())

            if body is not None:
                for key in sorted(supplied_body - known_names):
                    result.warnings.append(
                        f"Body field {key!r} is not declared in the schema for "
                        f"{method_u} {normalise_path(path)}."
                    )

            if method_u == "POST":
                for key in sorted(required_names - supplied_body):
                    result.errors.append(
                        f"Missing required body field: {key!r}."
                    )

    if result.errors:
        result.schema_summary = _compact_schema_summary(
            graph_manager, eid, method_u, normalise_path(path)
        )

    return result


def _compact_schema_summary(
    graph_manager: "GraphManager", eid: str, method: str, path: str
) -> dict[str, Any]:
    try:
        params = graph_manager.query(
            _PARAMS_FULL_QUERY, {"eid": eid}, read_only=True
        )
    except Exception:
        params = []
    try:
        props = graph_manager.query(
            _REQUEST_BODY_PROPS_QUERY, {"eid": eid}, read_only=True
        )
    except Exception:
        props = []
    return {
        "method": method,
        "path": path,
        "parameters": [
            {
                "name": r.get("name") or "",
                "location": r.get("location") or "",
                "required": bool(r.get("required") or False),
                "type": r.get("type") or "",
            }
            for r in params or []
        ],
        "body_fields": [
            {
                "name": r.get("name") or "",
                "required": bool(r.get("required") or False),
            }
            for r in props or []
        ],
        "hint": (
            "Use `query_graph` with the canned patterns in `graph://schema` "
            "to inspect Property / Parameter / SchemaComponent nodes for "
            "more detail (allOf flattening, supportedDeviceTypes, yangPath, etc.)."
        ),
    }


def format_validation_error(result: ValidationResult) -> str:
    """Render a failing ``ValidationResult`` as the ``ToolError`` message."""
    lines = ["API call rejected by pre-flight validator:", ""]
    for err in result.errors:
        lines.append(f"  - {err}")
    if result.warnings:
        lines.append("")
        for warn in result.warnings:
            lines.append(f"  ! {warn}")
    if result.schema_summary:
        method = result.schema_summary.get("method") or ""
        path = result.schema_summary.get("path") or ""
        lines.extend(
            [
                "",
                "Schema summary for this endpoint:",
                json.dumps(result.schema_summary, indent=2),
                "",
                "Inspect every body field (including inherited via allOf and",
                "promoted inline branches) with this canonical query \u2014",
                "pass deviceType='' to skip the device filter, or e.g.",
                "'Switch CX' / 'AP' to slice:",
                "",
                "```cypher",
                f"MATCH (e:ApiEndpoint {{method: '{method}', path: '{path}'}})",
                "      -[:HAS_REQUEST_BODY]->(:RequestBody)",
                "      -[:BODY_REFERENCES]->(root:SchemaComponent)",
                "MATCH (root)-[:COMPOSED_OF*0..5]->(c:SchemaComponent)",
                "      -[:HAS_PROPERTY]->(p:Property)",
                "WHERE $deviceType = ''",
                "   OR p.supportedDeviceTypes IS NULL",
                "   OR size(p.supportedDeviceTypes) = 0",
                "   OR $deviceType IN p.supportedDeviceTypes",
                "RETURN c.name AS host, c.bodyShape AS shape,",
                "       p.name, p.type, p.required, p.enumValues,",
                "       p.supportedDeviceTypes, p.yangPath",
                "ORDER BY c.name, p.required DESC, p.name",
                "```",
                "",
                "Run it via `query_graph(cypher, parameters={'deviceType': ...})`.",
                "See `graph://schema` for more canned Cypher patterns.",
            ]
        )
    return "\n".join(lines)


def format_validation_warnings(result: ValidationResult) -> str:
    """Render a passing-but-warning ``ValidationResult`` as a header block."""
    if not result.warnings:
        return ""
    lines = ["! Pre-flight validation warnings:"]
    for warn in result.warnings:
        lines.append(f"  - {warn}")
    return "\n".join(lines) + "\n\n"
