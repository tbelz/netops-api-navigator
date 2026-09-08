from __future__ import annotations

import hashlib

import pytest

from scripts.verify_pypi_release import verify_release

pytestmark = pytest.mark.unit


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def test_verify_release_matches_filenames_and_digests(tmp_path):
    wheel = tmp_path / "netops_api_navigator-0.3.0-py3-none-any.whl"
    source = tmp_path / "netops_api_navigator-0.3.0.tar.gz"
    wheel.write_bytes(b"wheel")
    source.write_bytes(b"source")

    def fetch_json(url):
        assert url == "https://test.pypi.org/pypi/netops-api-navigator/0.3.0/json"
        return {
            "urls": [
                {
                    "filename": wheel.name,
                    "url": "https://files.example.invalid/project.whl",
                    "digests": {"sha256": _digest(b"wheel")},
                },
                {
                    "filename": source.name,
                    "url": "https://files.example.invalid/project.tar.gz",
                    "digests": {"sha256": _digest(b"source")},
                },
            ]
        }

    assert (
        verify_release(
            index_url="https://test.pypi.org",
            project="netops-api-navigator",
            version="0.3.0",
            distributions=tmp_path,
            attempts=1,
            delay_seconds=0,
            fetch_json=fetch_json,
        )
        == "https://files.example.invalid/project.whl"
    )


def test_verify_release_rejects_digest_mismatch(tmp_path):
    wheel = tmp_path / "netops_api_navigator-0.3.0-py3-none-any.whl"
    source = tmp_path / "netops_api_navigator-0.3.0.tar.gz"
    wheel.write_bytes(b"wheel")
    source.write_bytes(b"source")

    def fetch_json(_url):
        return {
            "urls": [
                {
                    "filename": wheel.name,
                    "url": "https://files.example.invalid/project.whl",
                    "digests": {"sha256": "0" * 64},
                },
                {
                    "filename": source.name,
                    "url": "https://files.example.invalid/project.tar.gz",
                    "digests": {"sha256": _digest(b"source")},
                },
            ]
        }

    with pytest.raises(RuntimeError, match="did not verify"):
        verify_release(
            index_url="https://test.pypi.org",
            project="netops-api-navigator",
            version="0.3.0",
            distributions=tmp_path,
            attempts=1,
            delay_seconds=0,
            fetch_json=fetch_json,
        )
