"""Parity checks between the legacy and compiler-produced L3 projections."""

from __future__ import annotations

import json
import re
from typing import Any


_PARITY_QUERIES: dict[str, str] = {
    "endpoints": """
        MATCH (e:ApiEndpoint)
        RETURN e.method AS method, e.path AS path
    """,
    "parameters": """
        MATCH (e:ApiEndpoint)-[:HAS_PARAMETER]->(p:Parameter)
        RETURN e.method AS method, e.path AS path,
               p.location AS location, p.name AS name
    """,
    "request_bodies": """
        MATCH (e:ApiEndpoint)-[:HAS_REQUEST_BODY]->(body:RequestBody)
        RETURN e.method AS method, e.path AS path,
               body.content_type AS content_type
    """,
    "body_references": """
        MATCH (e:ApiEndpoint)-[:HAS_REQUEST_BODY]->(body:RequestBody)
              -[:BODY_REFERENCES]->(schema:SchemaComponent)
        RETURN e.method AS method, e.path AS path,
               body.content_type AS content_type,
               schema.component_id AS component_id
    """,
    "responses": """
        MATCH (e:ApiEndpoint)-[:HAS_RESPONSE]->(response:Response)
        RETURN e.method AS method, e.path AS path,
               response.status AS status,
               response.content_type AS content_type
    """,
    "response_references": """
        MATCH (e:ApiEndpoint)-[:HAS_RESPONSE]->(response:Response)
              -[:RESPONSE_REFERENCES]->(schema:SchemaComponent)
        RETURN e.method AS method, e.path AS path,
               response.status AS status,
               response.content_type AS content_type,
               schema.component_id AS component_id
    """,
    "schema_components": """
        MATCH (schema:SchemaComponent)
        RETURN schema.component_id AS component_id,
               schema.bodyJson AS __body_json
    """,
    "properties": """
        MATCH (schema:SchemaComponent)-[:HAS_PROPERTY]->(prop:Property)
        RETURN schema.component_id AS component_id,
               prop.name AS property,
               schema.bodyJson AS __component_body_json
    """,
    "property_types": """
        MATCH (schema:SchemaComponent)-[:HAS_PROPERTY]->(prop:Property)
              -[:PROPERTY_OF_TYPE]->(target:SchemaComponent)
        RETURN schema.component_id AS component_id,
               prop.name AS property,
               target.component_id AS target_component_id
    """,
    "composition": """
        MATCH (source:SchemaComponent)-[edge:COMPOSED_OF]->(target:SchemaComponent)
        RETURN source.component_id AS component_id,
               edge.kind AS kind,
               target.component_id AS target_component_id
    """,
    "value_schemas": """
        MATCH (source:SchemaComponent)-[:HAS_VALUE_SCHEMA]->(target:SchemaComponent)
        RETURN source.component_id AS component_id,
               target.component_id AS target_component_id
    """,
    "yang_paths": """
        MATCH (yang:YangPath)
        RETURN yang.yangPath AS yang_path
    """,
    "property_yang": """
        MATCH (schema:SchemaComponent)-[:HAS_PROPERTY]->(prop:Property)
              -[:PROPERTY_AT_YANG]->(yang:YangPath)
        RETURN schema.component_id AS component_id,
               prop.name AS property,
               yang.yangPath AS yang_path
    """,
    "configures_yang": """
        MATCH (e:ApiEndpoint)-[:CONFIGURES_YANG]->(yang:YangPath)
        RETURN e.method AS method, e.path AS path,
               yang.yangPath AS yang_path
    """,
    "cli_commands": """
        MATCH (e:ApiEndpoint)-[:HAS_CLI_COMMAND]->(command:CliCommand)
        RETURN e.method AS method, e.path AS path,
               command.commandName AS command_name
    """,
}

_VARIANT_SUFFIX_RE = re.compile(r"@[0-9a-f]{12}(?=#|$)")
_SYNTHETIC_SHAPE_RE = re.compile(r":Shape_[0-9a-f]+$")

_COMPONENT_ID_FIELDS = {"component_id", "target_component_id"}

_ALTERNATE_TARGET_CONTEXT_FIELDS: dict[str, tuple[str, ...]] = {
    "body_references": ("method", "path", "content_type"),
    "response_references": ("method", "path", "status", "content_type"),
    "property_types": ("component_id", "property"),
    "composition": ("component_id", "kind"),
    "value_schemas": ("component_id",),
}


def compute_projection_parity(
    legacy_conn,
    compiler_conn,
    *,
    sample_limit: int = 25,
) -> dict[str, Any]:
    """Compare legacy and compiler L3 projections using agent-facing keys."""
    checks: dict[str, Any] = {}
    total_legacy = 0
    total_missing = 0
    total_effective_missing = 0
    total_compiler = 0
    for name, query in _PARITY_QUERIES.items():
        legacy = _row_set(legacy_conn, query)
        compiler = _row_set(compiler_conn, query)
        missing_keys = sorted(set(legacy) - set(compiler))
        extra_keys = sorted(set(compiler) - set(legacy))
        alias_equivalent_keys = _alias_equivalent_missing_keys(
            name,
            legacy,
            compiler,
            missing_keys,
        )
        alternate_target_keys = _alternate_target_missing_keys(
            name,
            legacy,
            compiler,
            missing_keys,
            alias_equivalent_keys,
        )
        structural_equivalent_keys = _structural_equivalent_missing_keys(
            name,
            legacy,
            compiler,
            missing_keys,
            alias_equivalent_keys,
            alternate_target_keys,
        )
        effective_missing_keys = [
            key
            for key in missing_keys
            if key not in alias_equivalent_keys
            and key not in alternate_target_keys
            and key not in structural_equivalent_keys
        ]
        total_legacy += len(legacy)
        total_compiler += len(compiler)
        total_missing += len(missing_keys)
        total_effective_missing += len(effective_missing_keys)
        checks[name] = {
            "legacy_count": len(legacy),
            "compiler_count": len(compiler),
            "legacy_missing_count": len(missing_keys),
            "legacy_alias_equivalent_count": len(alias_equivalent_keys),
            "legacy_alternate_target_count": len(alternate_target_keys),
            "legacy_structural_equivalent_count": len(structural_equivalent_keys),
            "legacy_effective_missing_count": len(effective_missing_keys),
            "compiler_extra_count": len(extra_keys),
            "legacy_coverage_ratio": _ratio(len(legacy) - len(missing_keys), len(legacy)),
            "legacy_effective_coverage_ratio": _ratio(
                len(legacy) - len(effective_missing_keys),
                len(legacy),
            ),
            "legacy_missing_samples": [
                _public_row(legacy[key]) for key in missing_keys[:sample_limit]
            ],
            "legacy_effective_missing_samples": [
                _public_row(legacy[key]) for key in effective_missing_keys[:sample_limit]
            ],
            "legacy_alias_equivalent_samples": [
                _public_row(legacy[key]) for key in alias_equivalent_keys[:sample_limit]
            ],
            "legacy_alternate_target_samples": [
                _public_row(legacy[key]) for key in alternate_target_keys[:sample_limit]
            ],
            "legacy_structural_equivalent_samples": [
                _public_row(legacy[key]) for key in structural_equivalent_keys[:sample_limit]
            ],
            "compiler_extra_samples": [
                _public_row(compiler[key]) for key in extra_keys[:sample_limit]
            ],
        }
    return {
        "enabled": True,
        "all_legacy_covered": total_missing == 0,
        "total_legacy_signatures": total_legacy,
        "total_compiler_signatures": total_compiler,
        "total_legacy_missing": total_missing,
        "total_legacy_effective_missing": total_effective_missing,
        "overall_legacy_coverage_ratio": _ratio(total_legacy - total_missing, total_legacy),
        "overall_legacy_effective_coverage_ratio": _ratio(
            total_legacy - total_effective_missing,
            total_legacy,
        ),
        "all_legacy_effectively_covered": total_effective_missing == 0,
        "checks": checks,
    }


def format_projection_parity_report(report: dict[str, Any]) -> str:
    """Return a compact build-log summary for compiler projection parity."""
    if report.get("all_legacy_covered"):
        return (
            "✓ compiler projection covers all legacy API graph signatures "
            f"({report.get('total_legacy_signatures', 0)} checked)"
        )
    lines = [
        "⚠ compiler projection parity gaps: "
        f"{report.get('total_legacy_missing', 0)} missing legacy signatures "
        f"across {report.get('total_legacy_signatures', 0)} checked "
        f"({report.get('total_legacy_effective_missing', 0)} effective)"
    ]
    for name, check in report.get("checks", {}).items():
        missing = check.get("legacy_missing_count", 0)
        if missing <= 0:
            continue
        effective = check.get("legacy_effective_missing_count", missing)
        lines.append(
            f"  • {name}: {missing}/{check.get('legacy_count', 0)} legacy "
            f"signatures missing ({effective} effective)"
        )
        for row in check.get("legacy_effective_missing_samples", [])[:3]:
            lines.append(f"      {json.dumps(row, default=str)}")
    return "\n".join(lines)


def _alias_equivalent_missing_keys(
    check_name: str,
    legacy: dict[str, dict[str, Any]],
    compiler: dict[str, dict[str, Any]],
    missing_keys: list[str],
) -> list[str]:
    compiler_normalized = {
        _semantic_key(check_name, row)
        for row in compiler.values()
    }
    return [
        key
        for key in missing_keys
        if _semantic_key(check_name, legacy[key]) in compiler_normalized
    ]


def _alternate_target_missing_keys(
    check_name: str,
    legacy: dict[str, dict[str, Any]],
    compiler: dict[str, dict[str, Any]],
    missing_keys: list[str],
    alias_equivalent_keys: list[str],
) -> list[str]:
    context_fields = _ALTERNATE_TARGET_CONTEXT_FIELDS.get(check_name)
    if context_fields is None:
        return []
    alias_key_set = set(alias_equivalent_keys)
    legacy_context_counts = _context_counts(check_name, legacy.values(), context_fields)
    compiler_context_counts = _context_counts(check_name, compiler.values(), context_fields)
    return [
        key
        for key in missing_keys
        if key not in alias_key_set
        and legacy_context_counts.get(
            _context_key(check_name, legacy[key], context_fields),
            0,
        ) == 1
        and compiler_context_counts.get(
            _context_key(check_name, legacy[key], context_fields),
            0,
        ) == 1
    ]


def _structural_equivalent_missing_keys(
    check_name: str,
    legacy: dict[str, dict[str, Any]],
    compiler: dict[str, dict[str, Any]],
    missing_keys: list[str],
    alias_equivalent_keys: list[str],
    alternate_target_keys: list[str],
) -> list[str]:
    if check_name == "schema_components":
        compiler_shapes = {
            _schema_shape_key(row.get("__body_json"))
            for row in compiler.values()
        }
        compiler_shapes.discard("")
        return [
            key
            for key in missing_keys
            if key not in alias_equivalent_keys
            and key not in alternate_target_keys
            and _is_synthetic_shape_component(legacy[key].get("component_id"))
            and _schema_shape_key(legacy[key].get("__body_json")) in compiler_shapes
        ]
    if check_name == "properties":
        compiler_property_shapes = {
            (
                shape_key,
                row.get("property", ""),
            )
            for row in compiler.values()
            if (shape_key := _schema_shape_key(row.get("__component_body_json")))
        }
        return [
            key
            for key in missing_keys
            if key not in alias_equivalent_keys
            and key not in alternate_target_keys
            and _is_synthetic_shape_component(legacy[key].get("component_id"))
            and (
                _schema_shape_key(legacy[key].get("__component_body_json")),
                legacy[key].get("property", ""),
            ) in compiler_property_shapes
        ]
    return []


def _is_synthetic_shape_component(component_id: Any) -> bool:
    return isinstance(component_id, str) and bool(_SYNTHETIC_SHAPE_RE.search(component_id))


def _row_set(conn, query: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in conn.execute(query).rows_as_dict():
        normalized = {key: _normalize(value) for key, value in row.items()}
        result[_key(normalized)] = normalized
    return result


def _key(row: dict[str, Any]) -> str:
    return json.dumps(_public_row(row), sort_keys=True, separators=(",", ":"))


def _public_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if not key.startswith("__")}


def _semantic_key(check_name: str, row: dict[str, Any]) -> str:
    return _key(_semantic_row(check_name, row))


def _semantic_row(_check_name: str, row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: _normalize_component_id(value)
        if key in _COMPONENT_ID_FIELDS and isinstance(value, str)
        else value
        for key, value in row.items()
    }


def _context_key(
    check_name: str,
    row: dict[str, Any],
    context_fields: tuple[str, ...],
) -> str:
    semantic = _semantic_row(check_name, row)
    return _key({field: semantic.get(field, "") for field in context_fields})


def _context_counts(
    check_name: str,
    rows: Any,
    context_fields: tuple[str, ...],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        key = _context_key(check_name, row, context_fields)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _normalize_component_id(value: str) -> str:
    return _VARIANT_SUFFIX_RE.sub("", value)


def _normalize(value: Any) -> Any:
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if value is None:
        return ""
    return value


def _schema_shape_key(raw_body: Any) -> str:
    if not isinstance(raw_body, str) or not raw_body:
        return ""
    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError:
        return ""
    if not isinstance(body, dict):
        return ""
    return json.dumps(
        _canonical_shape(body),
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_shape(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _canonical_shape(item)
            for key, item in sorted(value.items())
            if key not in {"description", "example", "examples", "title"}
        }
    if isinstance(value, list):
        return [_canonical_shape(item) for item in value]
    return value


def _ratio(count: int, total: int) -> float:
    if total <= 0:
        return 1.0
    return round(count / total, 4)
