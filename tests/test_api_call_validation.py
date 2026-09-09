"""Tests for the pre-flight call validator (ADR 010).

Replaces the previous schema-inspection gate tests. The validator is
stateless — every call gets a fresh check against the graph.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from netops_api_navigator.tools.api_call_validation import (
    ValidationResult,
    eid_for,
    format_validation_error,
    format_validation_warnings,
    normalise_path,
    validate_call,
)

# ── helpers ──────────────────────────────────────────────────────────


class _FakeGraph:
    """Stand-in for GraphManager. Returns canned rows per query."""

    def __init__(self, responses: dict[str, list[dict]] | None = None, available: bool = True):
        # Map "<query-key>" -> rows. Key derived from first 50 chars of query.
        self.responses = responses or {}
        self.is_available = available
        self.calls: list[tuple[str, dict | None]] = []

    def query(self, cypher: str, params=None, read_only: bool = True):
        self.calls.append((cypher, params))
        if "HAS_PARAMETER" in cypher and "p.required = true" in cypher:
            return self.responses.get("required_params", [])
        if "HAS_PARAMETER" in cypher:
            return self.responses.get("all_params", [])
        if "HAS_REQUEST_BODY" in cypher:
            return self.responses.get("body_props", [])
        return []


# ── normalise_path / eid_for ─────────────────────────────────────────


class TestPathHelpers:
    def test_normalise_path_adds_leading_slash(self):
        assert normalise_path("foo/bar") == "/foo/bar"

    def test_normalise_path_preserves_leading_slash(self):
        assert normalise_path("/foo/bar") == "/foo/bar"

    def test_normalise_path_strips_whitespace(self):
        assert normalise_path("  foo  ") == "/foo"

    def test_eid_for_uppercases_method(self):
        assert eid_for("get", "foo/bar") == "GET:/foo/bar"

    def test_eid_for_template_path(self):
        assert (
            eid_for("POST", "/things/{id}/action")
            == "POST:/things/{id}/action"
        )


# ── fail-open / graph unavailable ────────────────────────────────────


class TestFailOpen:
    def test_no_graph_manager_passes_with_warning(self):
        result = validate_call(None, "GET", "/x", None, None)
        assert result.ok
        assert any("graph database unavailable" in w for w in result.warnings)

    def test_graph_not_available_passes_with_warning(self):
        gm = _FakeGraph(available=False)
        result = validate_call(gm, "GET", "/x", None, None)
        assert result.ok
        assert any("graph database unavailable" in w for w in result.warnings)
        # No query should have been issued.
        assert gm.calls == []


# ── required query parameters ────────────────────────────────────────


class TestRequiredQueryParams:
    def test_missing_required_query_param_blocks(self):
        gm = _FakeGraph(
            {
                "required_params": [
                    {"name": "scopeId", "location": "query"},
                ]
            }
        )
        result = validate_call(gm, "GET", "/cfg/things", None, None)
        assert not result.ok
        assert any("scopeId" in e for e in result.errors)

    def test_supplied_required_query_param_passes(self):
        gm = _FakeGraph(
            {
                "required_params": [
                    {"name": "scopeId", "location": "query"},
                ]
            }
        )
        result = validate_call(
            gm, "GET", "/cfg/things", {"scopeId": "abc"}, None
        )
        assert result.ok
        assert result.required_query_params == {"scopeid"}

    def test_wrong_case_required_query_param_blocks(self):
        gm = _FakeGraph(
            {
                "required_params": [
                    {"name": "scopeId", "location": "query"},
                ]
            }
        )

        result = validate_call(
            gm, "GET", "/cfg/things", {"scopeid": "abc"}, None
        )

        assert not result.ok
        assert any("scopeId" in error for error in result.errors)

    def test_required_path_param_ignored(self):
        # Required path params are validated by the server returning 404,
        # not by the pre-flight check.
        gm = _FakeGraph(
            {
                "required_params": [
                    {"name": "id", "location": "path"},
                ]
            }
        )
        result = validate_call(gm, "GET", "/things/abc", None, None)
        assert result.ok

    def test_caller_can_own_required_pagination_params(self):
        gm = _FakeGraph(
            {
                "required_params": [
                    {"name": "limit", "location": "query"},
                    {"name": "next", "location": "query"},
                    {"name": "scopeId", "location": "query"},
                ]
            }
        )
        result = validate_call(
            gm,
            "GET",
            "/things",
            {"scopeId": "abc"},
            None,
            ignored_required_query_params={"limit", "next", "offset"},
        )
        assert result.ok
        assert result.required_query_params == {"limit", "next", "scopeid"}


# ── request body validation ──────────────────────────────────────────


class TestRequestBody:
    def test_missing_required_body_field_blocks_on_post(self):
        gm = _FakeGraph(
            {
                "body_props": [
                    {"name": "name", "required": True},
                    {"name": "description", "required": False},
                ]
            }
        )
        result = validate_call(gm, "POST", "/things", None, {"description": "hi"})
        assert not result.ok
        assert any("name" in e for e in result.errors)

    def test_required_body_field_present_on_post_passes(self):
        gm = _FakeGraph(
            {
                "body_props": [
                    {"name": "name", "required": True},
                ]
            }
        )
        result = validate_call(gm, "POST", "/things", None, {"name": "x"})
        assert result.ok

    def test_post_with_no_body_still_blocks_on_required_field(self):
        # body=None for POST must be treated as empty body so that
        # required-field validation still fires.
        gm = _FakeGraph(
            {
                "body_props": [
                    {"name": "name", "required": True},
                ]
            }
        )
        result = validate_call(gm, "POST", "/things", None, None)
        assert not result.ok
        assert any("name" in e for e in result.errors)

    def test_patch_skips_required_body_check(self):
        gm = _FakeGraph(
            {
                "body_props": [
                    {"name": "name", "required": True},
                ]
            }
        )
        # PATCH with no required field is a valid partial update.
        result = validate_call(gm, "PATCH", "/things", None, {"description": "x"})
        assert result.ok

    def test_unknown_body_key_is_warning_not_error_on_post(self):
        gm = _FakeGraph(
            {
                "body_props": [
                    {"name": "name", "required": True},
                ]
            }
        )
        result = validate_call(
            gm, "POST", "/things", None, {"name": "x", "bogus": 1}
        )
        assert result.ok  # required name present
        assert any("bogus" in w for w in result.warnings)

    def test_unknown_body_key_warning_on_patch(self):
        gm = _FakeGraph(
            {
                "body_props": [
                    {"name": "name", "required": False},
                ]
            }
        )
        result = validate_call(gm, "PATCH", "/things", None, {"bogus": 1})
        assert result.ok
        assert any("bogus" in w for w in result.warnings)


# ── schema summary on error ──────────────────────────────────────────


class TestSchemaSummaryOnError:
    def test_error_includes_schema_summary(self):
        gm = _FakeGraph(
            {
                "required_params": [
                    {"name": "scopeId", "location": "query"},
                ],
                "all_params": [
                    {
                        "name": "scopeId",
                        "location": "query",
                        "required": True,
                        "type": "string",
                    },
                ],
                "body_props": [],
            }
        )
        result = validate_call(gm, "GET", "/cfg/things", None, None)
        assert not result.ok
        assert result.schema_summary
        assert result.schema_summary["method"] == "GET"
        assert any(
            p["name"] == "scopeId" for p in result.schema_summary["parameters"]
        )

    def test_format_validation_error_includes_summary(self):
        result = ValidationResult(
            errors=["Missing required query parameter: 'scopeId'."],
            schema_summary={"method": "GET", "path": "/x", "parameters": [], "body_fields": []},
        )
        text = format_validation_error(result)
        assert "Missing required" in text
        assert "Schema summary" in text
        assert "query_graph" in text


# ── warnings rendering ───────────────────────────────────────────────


class TestQueryExceptionWarnings:
    class _BoomGraph:
        is_available = True

        def __init__(self, fail_on: str):
            self.fail_on = fail_on

        def query(self, cypher, params=None, read_only=True):
            if self.fail_on in cypher:
                raise RuntimeError("graph boom")
            return []

    def test_required_params_query_failure_warns(self):
        gm = self._BoomGraph("HAS_PARAMETER")
        result = validate_call(gm, "GET", "/x", None, None)
        assert result.ok
        assert any(
            "required-parameter lookup" in w for w in result.warnings
        )

    def test_body_props_query_failure_warns(self):
        gm = self._BoomGraph("HAS_REQUEST_BODY")
        result = validate_call(gm, "POST", "/x", None, {"a": 1})
        assert result.ok
        assert any(
            "request-body schema lookup" in w for w in result.warnings
        )


class TestWarningsHeader:
    def test_empty_warnings_returns_empty_string(self):
        result = ValidationResult()
        assert format_validation_warnings(result) == ""

    def test_warning_header_includes_messages(self):
        result = ValidationResult(warnings=["unknown key 'foo'"])
        text = format_validation_warnings(result)
        assert "Pre-flight" in text
        assert "foo" in text
