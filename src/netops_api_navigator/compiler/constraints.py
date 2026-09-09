"""Shared extraction helpers for queryable OpenAPI/JSON Schema constraints."""

from __future__ import annotations

from typing import Any

CONSTRAINT_KEYS = (
    "const",
    "default",
    "deprecated",
    "enum",
    "exclusiveMaximum",
    "exclusiveMinimum",
    "maxContains",
    "maximum",
    "maxItems",
    "maxLength",
    "maxProperties",
    "minContains",
    "minimum",
    "minItems",
    "minLength",
    "minProperties",
    "multipleOf",
    "nullable",
    "pattern",
    "readOnly",
    "required",
    "uniqueItems",
    "writeOnly",
    "x-enumDescriptions",
    "x-key",
)


def collect_constraints(body: dict[str, Any]) -> dict[str, Any]:
    """Return recognized constraint/annotation values without flattening them."""
    return {key: body[key] for key in CONSTRAINT_KEYS if key in body}
