"""Abstract interface for OpenAPI specification providers.

Different documentation sources (ReadMe.io scraping, GreenLake native OAS)
implement this protocol so the catalog system can aggregate them uniformly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class SpecProvider(Protocol):
    """Provider that can fetch OpenAPI specification dicts."""

    @property
    def name(self) -> str:
        """Human-readable label for logging (e.g. 'Central', 'GreenLake')."""
        ...

    def fetch_specs(self, cache_dir: Path, ttl: int) -> list[dict]:
        """Fetch parsed OpenAPI spec dicts from this source.

        Args:
            cache_dir: Root directory for caching specs to disk.
            ttl: Cache freshness in seconds.

        Returns:
            List of parsed OpenAPI 3.x specification dictionaries.
        """
        ...
