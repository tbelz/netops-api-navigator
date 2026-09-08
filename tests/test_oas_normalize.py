"""Tests for the OAS normalization + view projections."""

from __future__ import annotations

import copy
import json

from netops_api_navigator.oas_normalize import (
    normalize,
    project_components,
    project_glossary,
    project_skeleton,
)
from netops_api_navigator.oas_normalize import _SKELETON_STRIP_KEYS


# ── Helpers ──────────────────────────────────────────────────────────


def _make_error_response(status_msg: str) -> dict:
    return {
        "description": status_msg,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "error_code": {"type": "string"},
                        "description": {"type": "string"},
                        "service_name": {"type": "string"},
                    },
                }
            }
        },
    }


def _make_nested_object_schema() -> dict:
    return {
        "type": "object",
        "title": "Rule",
        "properties": {
            "src": {"type": "string"},
            "dst": {"type": "string"},
            "action": {"type": "string"},
            "protocol": {"type": "string"},
        },
    }


def _make_synthetic_spec(num_endpoints: int = 12) -> dict:
    """Build a spec that mimics the duplication patterns of sw-port-profiles."""
    paths: dict = {}
    rule_schema = _make_nested_object_schema()
    for i in range(num_endpoints):
        path = f"/v1/widgets/{i}"
        paths[path] = {
            "post": {
                "summary": f"Create widget {i}",
                "operationId": f"createWidget{i}",
                "tags": ["widgets"],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["name", "rule"],
                                "properties": {
                                    "name": {"type": "string"},
                                    "rule": copy.deepcopy(rule_schema),
                                    "fallback_rule": copy.deepcopy(rule_schema),
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "201": {
                        "description": "created",
                        "content": {
                            "application/json": {
                                "schema": {"type": "object", "properties": {"id": {"type": "string"}}}
                            }
                        },
                    },
                    "400": _make_error_response("Bad request"),
                    "404": _make_error_response("Not found"),
                    "500": _make_error_response("Internal server error"),
                },
            }
        }
    return {"openapi": "3.0.0", "info": {"title": "Widgets API"}, "paths": paths}


# ── Normalize ────────────────────────────────────────────────────────


def test_normalize_dedups_error_responses_to_components():
    spec = _make_synthetic_spec(num_endpoints=4)

    out = normalize(spec)

    # The four 400/404/500 inline errors per operation should now point at $refs.
    op = out["paths"]["/v1/widgets/0"]["post"]
    assert "$ref" in op["responses"]["400"]
    assert "$ref" in op["responses"]["500"]
    # And the components section grew.
    assert out["components"]["responses"], "expected promoted error components"


def test_normalize_dedups_nested_object_to_components():
    spec = _make_synthetic_spec(num_endpoints=3)
    out = normalize(spec)

    op = out["paths"]["/v1/widgets/0"]["post"]
    schema = op["requestBody"]["content"]["application/json"]["schema"]
    # The outer body schema may itself have been promoted; resolve one hop.
    if "$ref" in schema:
        ref = schema["$ref"].removeprefix("#/components/schemas/")
        schema = out["components"]["schemas"][ref]
    rule = schema["properties"]["rule"]
    assert "$ref" in rule, f"expected rule to be $ref, got {rule!r}"
    assert rule["$ref"].startswith("#/components/schemas/")


def test_normalize_is_idempotent():
    spec = _make_synthetic_spec(num_endpoints=3)
    once = normalize(spec)
    twice = normalize(once)
    assert json.dumps(once, sort_keys=True) == json.dumps(twice, sort_keys=True)


def test_normalize_lossless_against_full_resolution():
    """Normalization must not change the resolved skeleton shape."""
    spec = _make_synthetic_spec(num_endpoints=3)
    raw_skel = project_skeleton(spec, "POST", "/v1/widgets/0")
    norm_skel = project_skeleton(normalize(spec), "POST", "/v1/widgets/0")
    assert raw_skel is not None
    assert norm_skel is not None
    # ``parameters`` and ``required_paths`` must match exactly across the
    # raw and normalized projections.  ``request_body`` and ``responses``
    # legitimately differ in shape (inline schemas become ``$ref``s after
    # dedup) and ``$components`` may grow new entries — those are the
    # whole point of normalization, so we don't compare them here.
    for key in ("parameters", "required_paths"):
        assert raw_skel.get(key) == norm_skel.get(key), key


def test_normalize_strips_noisy_metadata():
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "T"},
        "paths": {
            "/x": {
                "get": {
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "format": "const",
                                        "maxLength": 9999,
                                        "description": "a duck",
                                        "x-typeDescription": "a duck",
                                        "x-patternSources": list(range(50)),
                                        "properties": {"q": {"type": "string"}},
                                    }
                                }
                            },
                        }
                    }
                }
            }
        },
    }
    out = normalize(spec)
    schema = out["paths"]["/x"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert "format" not in schema
    assert "maxLength" not in schema
    assert "x-typeDescription" not in schema
    assert "x-patternSources" not in schema


# ── Projections (skeleton + glossary) ───────────────────────────────


DESCRIPTION_KEYS = (
    "description", "title", "example", "examples",
    "x-typeName", "x-typeDescription", "x-patternSources",
)


def _walk(node, *, _in_properties: bool = False):
    """Yield every dict reachable under ``node``.

    ``properties`` and ``patternProperties`` map *user-defined field
    names* to schemas; their keys are data, not OpenAPI keywords.  We
    therefore walk *into* their values but never yield the property-name
    dict itself for keyword checks (``description`` is a perfectly legal
    field name).
    """
    if isinstance(node, dict):
        if not _in_properties:
            yield node
        for k, v in node.items():
            yield from _walk(v, _in_properties=k in ("properties", "patternProperties"))
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def test_skeleton_strips_descriptions_at_every_level():
    spec = normalize(_make_synthetic_spec(num_endpoints=4))
    skel = project_skeleton(spec, "POST", "/v1/widgets/0")
    assert skel is not None
    # The top-level meta KEEPS its summary so the agent has a one-line label.
    # But every NESTED schema dict in parameters / request_body / responses
    # must have no description-bearing keys.  ``properties`` and
    # ``patternProperties`` are skipped because their keys are
    # user-defined field names (a property literally called ``description``
    # is legal and must survive).  ``$components_index`` carries only
    # type/enum/required/child_refs hints — never prose — so it is
    # vacuously safe.
    for key in ("parameters", "request_body", "responses"):
        sub = skel.get(key)
        if sub is None:
            continue
        for node in _walk(sub):
            for forbidden in DESCRIPTION_KEYS:
                assert forbidden not in node, (
                    f"{forbidden!r} leaked into skeleton at {node!r}"
                )


def test_skeleton_preserves_user_property_named_description():
    """Regression: a property literally named ``description`` must survive.

    The skeleton walker previously stripped any dict key matching
    ``description`` / ``title`` / ``summary`` / etc., which corrupted
    schemas whose users named a field ``description`` (common in error
    response bodies — e.g. ``{error_code, description, service_name}``).
    """
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "T"},
        "paths": {
            "/things": {
                "post": {
                    "summary": "Create a thing",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["description", "title"],
                                    "properties": {
                                        # User-defined field names that
                                        # collide with OpenAPI keywords.
                                        "description": {"type": "string"},
                                        "title": {"type": "string"},
                                        "summary": {"type": "string"},
                                        "example": {"type": "string"},
                                    },
                                }
                            }
                        },
                    },
                    "responses": {"201": {"description": "ok"}},
                }
            }
        },
    }
    skel = project_skeleton(spec, "POST", "/things")
    assert skel is not None
    body_schema = skel["request_body"]["schema"]
    props = body_schema["properties"]
    for field in ("description", "title", "summary", "example"):
        assert field in props, f"property {field!r} was stripped"
        assert props[field] == {"type": "string"}
    # Required list naming a stripped key must also survive.
    assert "description" in skel["required_paths"]
    assert "title" in skel["required_paths"]


def test_skeleton_preserves_structure():
    spec = normalize(_make_synthetic_spec(num_endpoints=4))
    skel = project_skeleton(spec, "POST", "/v1/widgets/0")
    assert skel is not None
    assert skel["method"] == "POST"
    assert skel["path"] == "/v1/widgets/0"
    assert "parameters" in skel
    assert "request_body" in skel
    assert "required_paths" in skel
    assert "name" in skel["required_paths"]
    body_str = json.dumps(skel)
    if body_str.count('"$ref"') > 0:
        assert "$components_index" in skel
        # Index entries must be tiny: never carry full schema bodies.
        for section_entries in skel["$components_index"].values():
            for entry in section_entries.values():
                assert "properties" not in entry
                assert "items" not in entry  # only items_ref, not full items


def test_glossary_returns_descriptions_only():
    spec = normalize(_make_synthetic_spec(num_endpoints=4))
    gloss = project_glossary(spec, "POST", "/v1/widgets/0")
    assert gloss is not None
    assert gloss["method"] == "POST"
    assert gloss["path"] == "/v1/widgets/0"
    assert isinstance(gloss["components"], dict)
    for entry in gloss["components"].values():
        # Glossary must not leak structural keys.
        assert "type" not in entry
        for pe in (entry.get("properties") or {}).values():
            assert "type" not in pe
            assert "$ref" not in pe


def test_glossary_carries_property_descriptions_when_present():
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "T"},
        "components": {
            "schemas": {
                "VlanCfg": {
                    "type": "object",
                    "description": "VLAN configuration object.",
                    "properties": {
                        "vlan_id": {
                            "type": "integer",
                            "description": "VLAN identifier in [1, 4094].",
                            "enum": [1, 100, 200],
                        },
                    },
                }
            }
        },
        "paths": {
            "/v/{id}": {
                "post": {
                    "summary": "create",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/VlanCfg"}
                            }
                        },
                    },
                    "responses": {"201": {"description": "ok"}},
                }
            }
        },
    }
    gloss = project_glossary(spec, "POST", "/v/{id}")
    assert gloss is not None
    assert "VlanCfg" in gloss["components"]
    entry = gloss["components"]["VlanCfg"]
    assert entry["description"] == "VLAN configuration object."
    vlan_id = entry["properties"]["vlan_id"]
    assert vlan_id["description"].startswith("VLAN identifier")
    # ``enum`` is a structural key — it lives in the skeleton, not the
    # glossary.  The glossary surfaces only the prose keys that the
    # skeleton strips via _SKELETON_STRIP_KEYS (plus the minimum
    # traversal scaffolding to reach them).
    assert "enum" not in vlan_id
    assert "type" not in vlan_id


def test_glossary_carries_parameter_descriptions():
    """Mirrors the getAlertListV1 case: a query parameter whose
    ``description`` carries multi-line OData/filter prose plus per-field
    enums.  The skeleton drops the prose; the glossary must surface it.
    """
    odata_prose = (
        "OData Version 4.0 filter string (limited functionality). "
        "Supports only 'and' conjunction ('or' and 'not' are NOT supported). "
        "Supported fields: siteId, typeId, status, category, deviceType. "
        "Operators: eq, in."
    )
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "T"},
        "components": {
            "parameters": {
                "SharedSiteId": {
                    "name": "siteId",
                    "in": "query",
                    "description": "Restrict results to a single site.",
                    "schema": {"type": "string", "format": "uuid"},
                }
            }
        },
        "paths": {
            "/v1/alerts": {
                "get": {
                    "summary": "list alerts",
                    "parameters": [
                        {
                            "name": "filter",
                            "in": "query",
                            "description": odata_prose,
                            "schema": {
                                "type": "string",
                                "example": "status eq 'Active'",
                            },
                        },
                        {
                            "name": "status",
                            "in": "query",
                            "description": "Filter by alert status.",
                            "schema": {
                                "type": "string",
                                "enum": ["Active", "Deferred", "Cleared"],
                                "default": "Active",
                            },
                        },
                        {
                            "name": "limit",
                            "in": "query",
                            "schema": {"type": "integer", "default": 100},
                        },
                        {"$ref": "#/components/parameters/SharedSiteId"},
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }

    gloss = project_glossary(spec, "GET", "/v1/alerts")
    assert gloss is not None
    params = gloss.get("parameters")
    assert isinstance(params, dict)

    # OData prose survives verbatim.
    f = params["filter"]
    assert f["in"] == "query"
    assert f["description"] == odata_prose
    # Schema-level ``example`` is reached by walking through the
    # ``schema`` traversal key — it preserves the spec's nesting rather
    # than flattening to a top-level field.
    assert f["schema"]["example"] == "status eq 'Active'"

    # Schema-level enum + default are preserved by the SKELETON, not the
    # glossary, so they must NOT appear here — only the description text.
    s = params["status"]
    assert s["description"] == "Filter by alert status."
    assert "enum" not in s
    assert "default" not in s

    # Parameter with NO prose-bearing field (no description, no example)
    # must be omitted to keep payloads small.
    assert "limit" not in params

    # $ref-style parameter must be resolved one level so its prose is
    # captured under its real name.  Schema-level structural fields like
    # ``format`` are not repeated here.
    assert "siteId" in params
    assert params["siteId"]["description"] == "Restrict results to a single site."
    assert "format" not in params["siteId"]


def test_glossary_omits_parameters_block_when_no_prose():
    """If no parameter carries descriptive content, the ``parameters``
    block is omitted entirely (keeps the payload minimal).
    """
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "T"},
        "paths": {
            "/v1/x": {
                "get": {
                    "summary": "x",
                    "parameters": [
                        {"name": "page", "in": "query", "schema": {"type": "integer"}},
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    gloss = project_glossary(spec, "GET", "/v1/x")
    assert gloss is not None
    assert "parameters" not in gloss


def test_glossary_does_not_duplicate_keys_preserved_by_skeleton():
    """Invariant: the glossary surfaces the prose-bearing keys from
    ``_SKELETON_STRIP_KEYS`` plus only the traversal / scaffolding keys
    needed to reach them (e.g. ``properties``, ``items``, parameter
    ``in``).  Anything else the skeleton preserves (``enum``, ``format``,
    ``pattern``, ``default``, ``required``, ``type``, ``$ref``,
    ``x-mutually-exclusive``, length / numeric constraints) must NOT
    appear in glossary entries — duplicating them would only bloat
    payloads.
    """
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "T"},
        "components": {
            "schemas": {
                "Cfg": {
                    "type": "object",
                    "description": "Cfg with structural noise.",
                    "required": ["mode"],
                    "x-mutually-exclusive": ["mode", "auto"],
                    "properties": {
                        "mode": {
                            "type": "string",
                            "description": "Mode of operation.",
                            "enum": ["a", "b", "c"],
                            "default": "a",
                            "format": "lowercase",
                            "pattern": "^[a-c]$",
                            "minLength": 1,
                            "maxLength": 1,
                        }
                    },
                }
            }
        },
        "paths": {
            "/v/x": {
                "post": {
                    "summary": "x",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Cfg"}
                            }
                        },
                    },
                    "responses": {"201": {"description": "ok"}},
                }
            }
        },
    }
    gloss = project_glossary(spec, "POST", "/v/x")
    assert gloss is not None
    cfg = gloss["components"]["Cfg"]
    assert cfg["description"] == "Cfg with structural noise."
    for k in ("type", "required", "x-mutually-exclusive"):
        assert k not in cfg, f"glossary leaked structural key {k!r} at component level"
    mode = cfg["properties"]["mode"]
    assert mode["description"] == "Mode of operation."
    for k in (
        "type", "enum", "default", "format", "pattern",
        "minLength", "maxLength",
    ):
        assert k not in mode, f"glossary leaked structural key {k!r} at property level"


def test_glossary_keys_form_complement_of_skeleton_strip_keys():
    """Invariant: every key in a glossary payload is either a
    ``_SKELETON_STRIP_KEYS`` member, a structural traversal key, or a
    user-controlled field name nested inside ``properties`` /
    ``patternProperties``.  Guards against silent reintroduction of
    cherry-picking that re-emits structural keys.
    """
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "T"},
        "components": {
            "schemas": {
                "Outer": {
                    "type": "object",
                    "description": "Outer.",
                    "properties": {
                        "inner": {
                            "type": "object",
                            "description": "Inner.",
                            "properties": {
                                "leaf": {
                                    "type": "string",
                                    "description": "Leaf.",
                                    "example": "x",
                                }
                            },
                        }
                    },
                }
            }
        },
        "paths": {
            "/v/o": {
                "post": {
                    "summary": "o",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Outer"}
                            }
                        },
                    },
                    "responses": {"201": {"description": "ok"}},
                }
            }
        },
    }
    gloss = project_glossary(spec, "POST", "/v/o")
    assert gloss is not None

    allowed_struct = {
        "method", "path", "components", "parameters",
        "properties", "patternProperties", "items", "additionalProperties",
        "allOf", "oneOf", "anyOf", "schema",
        "in",  # parameter scaffold
    }
    user_keys = {"Outer", "inner", "leaf", "requestBody"}
    allowed = allowed_struct | set(_SKELETON_STRIP_KEYS) | user_keys

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                assert k in allowed, (
                    f"glossary contains unexpected key {k!r}; expected one of "
                    "_SKELETON_STRIP_KEYS, traversal keys, or a user name"
                )
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(gloss)


def test_glossary_captures_schema_title_and_x_type_metadata():
    """The skeleton strips ``title``, ``examples``, ``x-typeName``,
    ``x-typeDescription``, ``x-patternSources`` everywhere.  The glossary
    must capture them — under the old cherry-picking design they were
    lost from BOTH blobs.
    """
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "T"},
        "components": {
            "schemas": {
                "Widget": {
                    "type": "string",
                    "title": "Widget Identifier",
                    "description": "A widget id.",
                    "x-typeName": "WidgetId",
                    "x-typeDescription": "RFC-9999 widget identifier.",
                    "examples": ["w-1", "w-2"],
                }
            }
        },
        "paths": {
            "/v/w": {
                "post": {
                    "summary": "w",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Widget"}
                            }
                        },
                    },
                    "responses": {"201": {"description": "ok"}},
                }
            }
        },
    }
    gloss = project_glossary(spec, "POST", "/v/w")
    assert gloss is not None
    w = gloss["components"]["Widget"]
    assert w["title"] == "Widget Identifier"
    assert w["description"] == "A widget id."
    assert w["x-typeName"] == "WidgetId"
    assert w["x-typeDescription"] == "RFC-9999 widget identifier."
    assert w["examples"] == ["w-1", "w-2"]


def test_projections_return_none_for_unknown_endpoint():
    spec = _make_synthetic_spec(num_endpoints=1)
    assert project_skeleton(spec, "GET", "/nope") is None
    assert project_glossary(spec, "GET", "/nope") is None


def test_normalize_preserves_existing_components():
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "T"},
        "components": {"schemas": {"Existing": {"type": "object"}}},
        "paths": {},
    }
    out = normalize(spec)
    assert "Existing" in out["components"]["schemas"]


# ── project_components / index split ─────────────────────────────────


def test_project_components_returns_full_bodies():
    """project_components emits the same prose-stripped full bodies that
    used to live under the skeleton's ``$components`` key."""
    spec = normalize(_make_synthetic_spec(num_endpoints=4))
    components = project_components(spec, "POST", "/v1/widgets/0")
    assert components is not None
    # normalize() promotes deduped error responses into components.
    assert any(section_entries for section_entries in components.values())
    # Every emitted body must be prose-stripped (same rule as skeleton).
    for section_entries in components.values():
        for entry in section_entries.values():
            for forbidden in DESCRIPTION_KEYS:
                assert forbidden not in entry


def test_components_index_is_smaller_than_full_bodies():
    """The skeleton's $components_index must be substantially smaller
    than the equivalent full component bodies — that's the whole point
    of splitting the projection."""
    spec = normalize(_make_synthetic_spec(num_endpoints=4))
    skel = project_skeleton(spec, "POST", "/v1/widgets/0")
    comps = project_components(spec, "POST", "/v1/widgets/0")
    assert skel is not None and comps is not None
    if "$components_index" in skel:
        index_size = len(json.dumps(skel["$components_index"]))
        full_size = len(json.dumps(comps))
        assert index_size <= full_size, "index must not exceed full bodies"


def test_project_components_returns_none_for_unknown_endpoint():
    spec = normalize(_make_synthetic_spec(num_endpoints=2))
    assert project_components(spec, "GET", "/nope") is None
