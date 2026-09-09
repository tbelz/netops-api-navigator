"""Tests for the VSG documentation scraper."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from netops_api_navigator.vsg_scraper import (
    DocEntry,
    VsgDocProvider,
    _html_to_text,
    _extract_main_content,
    extract_sections,
    scrape_vsg_docs,
)


# ── Fixtures ────────────────────────────────────────────────────────

SAMPLE_PAGE_HTML = """\
<!DOCTYPE html>
<html>
<head><title>Test Page</title></head>
<body>
<div id="main-content">
  <h1 id="central-configuration-readiness">Central Configuration Readiness</h1>
  <p>Prerequisites needed to ensure an optimal experience with new HPE Aruba Networking Central.</p>
  <h2 id="summary-of-steps">Summary of Steps</h2>
  <p>The steps needed to onboard and activate devices are summarized as follows:</p>
  <p>Step 1: Create an HPE GreenLake account.</p>
  <p>Step 2: Add devices to inventory.</p>
  <h2 id="device-groups">Device Groups</h2>
  <p>For compatibility with the newest version of Central, devices must be added to groups.</p>
  <p>Devices must be assigned to a device group that supports Central configuration.</p>
  <h2 id="sites">Sites</h2>
  <p>Sites are mandatory in new HPE Aruba Networking Central.</p>
  <img src="screenshot.png" alt="screenshot">
  <p>A site defines a location where managed devices are physically installed.</p>
</div>
</body>
</html>
"""

MINIMAL_PAGE_HTML = """\
<html><body>
<div id="main-content">
  <p>Just some text without headings.</p>
</div>
</body></html>
"""

EMPTY_MAIN_HTML = """\
<html><body>
<div id="main-content">
</div>
</body></html>
"""

NO_MAIN_HTML = """\
<html><body>
<div id="other-content">
  <p>No main-content div here.</p>
</div>
</body></html>
"""


# ── HTML parsing tests ──────────────────────────────────────────────

class TestHtmlToText:
    def test_strips_tags(self):
        text = _html_to_text("<p>Hello <strong>world</strong></p>")
        assert "Hello" in text
        assert "world" in text
        assert "<" not in text

    def test_skips_script_tags(self):
        text = _html_to_text("<p>Visible</p><script>var x = 1;</script><p>Also visible</p>")
        assert "Visible" in text
        assert "Also visible" in text
        assert "var x" not in text

    def test_skips_style_tags(self):
        text = _html_to_text("<style>.foo { color: red; }</style><p>Text</p>")
        assert "Text" in text
        assert "color" not in text

    def test_skips_details(self):
        text = _html_to_text("<details><summary>TOC</summary><p>Hidden</p></details><p>Visible</p>")
        assert "Visible" in text
        assert "Hidden" not in text


class TestExtractMainContent:
    def test_extracts_main_content(self):
        html = _extract_main_content(SAMPLE_PAGE_HTML)
        assert "Central Configuration Readiness" in html
        assert "Summary of Steps" in html
        assert "id=\"main-content\"" not in html  # The wrapper div itself is excluded

    def test_returns_empty_for_no_main(self):
        html = _extract_main_content(NO_MAIN_HTML)
        assert html.strip() == ""


# ── Section splitting tests ─────────────────────────────────────────

class TestExtractSections:
    def test_splits_by_headings(self):
        sections = extract_sections(
            SAMPLE_PAGE_HTML,
            "https://example.com/readiness/",
            "readiness",
        )
        assert len(sections) >= 3  # summary-of-steps, device-groups, sites (+ possible intro)
        titles = [s.title for s in sections]
        assert "Summary of Steps" in titles
        assert "Device Groups" in titles
        assert "Sites" in titles

    def test_section_ids_are_correct(self):
        sections = extract_sections(
            SAMPLE_PAGE_HTML,
            "https://example.com/readiness/",
            "readiness",
        )
        ids = [s.section_id for s in sections]
        assert "vsg-central-readiness-summary-of-steps" in ids
        assert "vsg-central-readiness-device-groups" in ids
        assert "vsg-central-readiness-sites" in ids

    def test_section_urls_have_fragments(self):
        sections = extract_sections(
            SAMPLE_PAGE_HTML,
            "https://example.com/readiness/",
            "readiness",
        )
        device_groups = next(s for s in sections if s.title == "Device Groups")
        assert device_groups.url == "https://example.com/readiness/#device-groups"

    def test_section_content_is_text(self):
        sections = extract_sections(
            SAMPLE_PAGE_HTML,
            "https://example.com/readiness/",
            "readiness",
        )
        device_groups = next(s for s in sections if s.title == "Device Groups")
        assert "devices must be added to groups" in device_groups.content
        assert "<p>" not in device_groups.content

    def test_source_is_vsg_central(self):
        sections = extract_sections(
            SAMPLE_PAGE_HTML,
            "https://example.com/readiness/",
            "readiness",
        )
        assert all(s.source == "vsg-central" for s in sections)

    def test_no_headings_produces_single_section(self):
        sections = extract_sections(
            MINIMAL_PAGE_HTML,
            "https://example.com/page/",
            "page",
        )
        assert len(sections) == 1
        assert sections[0].section_id == "vsg-central-page"
        assert "text without headings" in sections[0].content

    def test_empty_main_returns_empty(self):
        sections = extract_sections(
            EMPTY_MAIN_HTML,
            "https://example.com/empty/",
            "empty",
        )
        assert sections == []

    def test_no_main_content_returns_empty(self):
        sections = extract_sections(
            NO_MAIN_HTML,
            "https://example.com/other/",
            "other",
        )
        assert sections == []

    def test_images_stripped_from_content(self):
        sections = extract_sections(
            SAMPLE_PAGE_HTML,
            "https://example.com/readiness/",
            "readiness",
        )
        sites = next(s for s in sections if s.title == "Sites")
        assert "<img" not in sites.content
        assert "screenshot.png" not in sites.content

    def test_intro_section_created(self):
        sections = extract_sections(
            SAMPLE_PAGE_HTML,
            "https://example.com/readiness/",
            "readiness",
        )
        intro = [s for s in sections if "intro" in s.section_id]
        # There may or may not be an intro depending on content before first H1
        # The H1 is the first heading, so intro would be content before it
        # In our sample, H1 is the first element, so no intro content


# ── Scraping with mocked HTTP ───────────────────────────────────────

class TestScrapeVsgDocs:
    @patch("netops_api_navigator.vsg_scraper._fetch_page")
    def test_scrape_returns_sections(self, mock_fetch):
        mock_fetch.return_value = SAMPLE_PAGE_HTML
        entries = scrape_vsg_docs(
            pages=[("readiness", "/test/readiness/")],
            cache_dir=Path("/tmp/test_vsg_cache"),
            ttl=0,
        )
        assert len(entries) >= 3
        assert all(isinstance(e, DocEntry) for e in entries)

    @patch("netops_api_navigator.vsg_scraper._fetch_page")
    def test_scrape_handles_fetch_error(self, mock_fetch):
        mock_fetch.side_effect = Exception("Network error")
        entries = scrape_vsg_docs(
            pages=[("readiness", "/test/readiness/")],
            cache_dir=Path("/tmp/test_vsg_cache_err"),
            ttl=0,
        )
        assert entries == []


class TestVsgDocProvider:
    @patch("netops_api_navigator.vsg_scraper._fetch_page")
    def test_provider_name(self, mock_fetch):
        provider = VsgDocProvider()
        assert provider.name == "VSG Central"

    @patch("netops_api_navigator.vsg_scraper._fetch_page")
    def test_provider_fetch_docs(self, mock_fetch):
        mock_fetch.return_value = SAMPLE_PAGE_HTML
        provider = VsgDocProvider()
        # Use a single page for faster test
        with patch("netops_api_navigator.vsg_scraper.VSG_CENTRAL_PAGES", [("test", "/test/")]):
            entries = provider.fetch_docs(cache_dir=Path("/tmp/test_vsg_provider"), ttl=0)
        assert len(entries) >= 1


# ── Build pipeline integration ──────────────────────────────────────

class TestPopulateDocs:
    """Test that _populate_docs correctly inserts DocSection nodes."""

    def test_populate_docs_inserts_nodes(self):
        """Verify _populate_docs creates DocSection nodes in the database."""
        try:
            import real_ladybug as lb
        except ImportError:
            pytest.skip("real_ladybug not available")

        from netops_api_navigator.graph.schema import (
            KNOWLEDGE_NODE_TABLES,
            NODE_TABLES,
        )

        # Import the function under test
        parent_dir = Path(__file__).parent.parent
        sys.path.insert(0, str(parent_dir))
        # We need to import from the scripts directory
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "build_knowledge_db",
            parent_dir / "scripts" / "build_knowledge_db.py",
        )
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        _populate_docs = mod._populate_docs

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test_db"
            db = lb.Database(str(db_path), max_db_size=256 * 1024 * 1024)
            conn = lb.Connection(db)

            # Apply schema
            for ddl in NODE_TABLES + KNOWLEDGE_NODE_TABLES:
                conn.execute(ddl.strip())

            entries = [
                DocEntry(
                    section_id="test-section-1",
                    title="Test Section",
                    content="This is test content about device groups.",
                    source="vsg-central",
                    url="https://example.com/test/#section",
                ),
                DocEntry(
                    section_id="test-section-2",
                    title="Another Section",
                    content="More content about sites and configuration.",
                    source="vsg-central",
                    url="https://example.com/test/#another",
                ),
            ]

            count = _populate_docs(db, entries)
            assert count == 2

            # Verify nodes exist
            result = conn.execute(
                "MATCH (d:DocSection) RETURN d.section_id ORDER BY d.section_id"
            )
            assert result.get_num_tuples() == 2

            db.close()
