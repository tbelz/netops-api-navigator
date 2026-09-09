"""Tests for the hard-fail behaviour of ``CentralAPI.paginate``.

Previously ``paginate()`` would silently emit a stderr warning and return
a partial list when ``max_pages`` was hit before the server reported all
items collected. That was a data-correctness hazard: a downstream RCA
script could conclude e.g. "no client at site X" simply because the loop
truncated. The new contract is to raise ``PaginationError`` so callers
must consciously choose between raising the cap or pre-filtering.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# central_helpers.py lives next to _http_core.py and imports it as a
# top-level module (script-runtime style). Make both importable.
_PKG_DIR = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "netops_api_navigator"
)


def _load_central_helpers():
    package_path = str(_PKG_DIR)
    added_path = package_path not in sys.path
    if added_path:
        sys.path.insert(0, package_path)
    try:
        spec = importlib.util.spec_from_file_location(
            "central_helpers_under_test", _PKG_DIR / "central_helpers.py"
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        if added_path:
            sys.path.remove(package_path)


@pytest.fixture(scope="module")
def helpers():
    return _load_central_helpers()


class TestPaginateHardFail:

    def test_raises_when_max_pages_hit_with_outstanding_items(self, helpers):
        api = helpers.CentralAPI()
        api._ensure_token = MagicMock()  # bypass real auth

        # Server reports total=10, but each page only returns 2 items —
        # so finishing requires 5 pages. We cap at max_pages=2 to force
        # the truncation branch.
        page = {"aps": [{"i": 1}, {"i": 2}], "total": 10}
        api._request = MagicMock(return_value=page)

        with pytest.raises(helpers.PaginationError) as exc:
            api.paginate("/dummy", max_pages=2)
        assert "PAGINATION_TRUNCATED" in exc.value.error_code
        assert "max_pages=2" in str(exc.value)

    def test_returns_full_list_when_total_reached_within_cap(self, helpers):
        api = helpers.CentralAPI()
        api._ensure_token = MagicMock()

        # First page reports total=2 and includes both items — natural exit.
        api._request = MagicMock(return_value={"aps": [{"i": 1}, {"i": 2}], "total": 2})

        out = api.paginate("/dummy", max_pages=5)
        assert len(out) == 2

    def test_returns_partial_when_server_runs_dry_before_total(self, helpers):
        """Empty page exits the loop naturally; this is *not* truncation."""
        api = helpers.CentralAPI()
        api._ensure_token = MagicMock()

        responses = [
            {"aps": [{"i": 1}], "total": 100},  # server lied about total
            {"aps": [], "total": 100},          # but next page is empty
        ]
        api._request = MagicMock(side_effect=responses)

        out = api.paginate("/dummy", max_pages=5)
        assert out == [{"i": 1}]

    def test_raises_when_max_pages_hit_without_server_total(self, helpers):
        api = helpers.CentralAPI()
        api._ensure_token = MagicMock()
        api._request = MagicMock(return_value={"aps": [{"i": 1}]})

        with pytest.raises(helpers.PaginationError) as exc:
            api.paginate("/dummy", max_pages=2)
        assert exc.value.error_code == "PAGINATION_TRUNCATED"
        assert "did not report a total" in str(exc.value)

    def test_cursor_pagination_stops_on_null_next(self, helpers):
        api = helpers.CentralAPI()
        api._ensure_token = MagicMock()
        api._request = MagicMock(
            side_effect=[
                {"items": [{"i": 1}], "next": "cursor-2"},
                {"items": [{"i": 2}], "next": None},
            ]
        )

        out = api.paginate("/dummy", max_pages=5)
        assert out == [{"i": 1}, {"i": 2}]
        assert api._request.call_args_list[1].kwargs["params"]["next"] == "cursor-2"

    def test_offset_pagination_continues_when_next_is_null(self, helpers):
        api = helpers.CentralAPI()
        api._ensure_token = MagicMock()
        api._request = MagicMock(
            side_effect=[
                {"items": [{"i": 1}, {"i": 2}], "total": 3, "next": None},
                {"items": [{"i": 3}], "total": 3, "next": None},
            ]
        )

        out = api.paginate("/dummy", page_size=2, max_pages=3)
        assert out == [{"i": 1}, {"i": 2}, {"i": 3}]
        second_page = api._request.call_args_list[1].kwargs["params"]
        assert second_page["offset"] == "2"
        assert "next" not in second_page

    def test_page_local_count_does_not_truncate_cursor_pagination(self, helpers):
        api = helpers.CentralAPI()
        api._ensure_token = MagicMock()
        api._request = MagicMock(
            side_effect=[
                {"items": [{"i": 1}, {"i": 2}], "count": 2, "next": "cursor-2"},
                {"items": [{"i": 3}], "count": 1, "next": None},
            ]
        )

        out = api.paginate("/dummy", page_size=2, max_pages=3)
        assert out == [{"i": 1}, {"i": 2}, {"i": 3}]
        assert api._request.call_args_list[1].kwargs["params"]["next"] == "cursor-2"

    def test_repeated_cursor_raises(self, helpers):
        api = helpers.CentralAPI()
        api._ensure_token = MagicMock()
        api._request = MagicMock(
            return_value={"items": [{"i": 1}], "next": "same"}
        )

        with pytest.raises(helpers.PaginationError) as exc:
            api.paginate("/dummy", max_pages=5)
        assert exc.value.error_code == "PAGINATION_LOOP"

    def test_script_helper_allows_explicit_page_cap_above_workshop_limit(self, helpers):
        api = helpers.CentralAPI()
        api._ensure_token = MagicMock()
        api._request = MagicMock(return_value={"items": [{"i": 1}], "total": 1})

        assert api.paginate("/dummy", max_pages=51) == [{"i": 1}]

    def test_auto_detected_item_key_is_reused_on_later_pages(self, helpers):
        api = helpers.CentralAPI()
        api._ensure_token = MagicMock()
        api._request = MagicMock(
            side_effect=[
                {"aps": [{"i": 1}], "total": 2},
                {"warnings": [], "aps": [{"i": 2}], "total": 2},
            ]
        )

        assert api.paginate("/dummy") == [{"i": 1}, {"i": 2}]
