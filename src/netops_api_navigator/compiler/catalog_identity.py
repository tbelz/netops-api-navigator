"""Catalog-level identity helpers for compiler projection rows.

The lossless AST and semantic overlay are per-spec.  The typed compiler
projection is catalog-wide, so named component ids need one extra rule:
identical bodies may share the legacy-style id, but distinct bodies with the
same provider/section/name must remain separate variants.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from .ast_builder import AstGraph


@dataclass(slots=True)
class _Entry:
    bodies: dict[str, str] = field(default_factory=dict)
    occurrence_count: int = 0


@dataclass
class CatalogIdentityRegistry:
    """Deterministic component ids across a multi-spec catalog."""

    _entries: dict[tuple[str, str, str], _Entry] = field(default_factory=dict)
    _finalized: bool = False
    _variant_ids: dict[tuple[str, str, str, str], str] = field(default_factory=dict)

    @property
    def is_finalized(self) -> bool:
        return self._finalized

    def add_ast_graph(self, ast: AstGraph) -> None:
        if self._finalized:
            raise RuntimeError("Cannot add AST graphs after catalog identities are finalized")
        self.add_spec(ast.spec_row.get("source", ""), ast.spec)

    def add_spec(self, source: str, spec: dict[str, Any]) -> None:
        if self._finalized:
            raise RuntimeError("Cannot add specs after catalog identities are finalized")
        components = spec.get("components")
        if not isinstance(components, dict):
            return
        provider = provider_from_source(source)
        for section, entries in components.items():
            if not isinstance(section, str) or not isinstance(entries, dict):
                continue
            for name, body in entries.items():
                if not isinstance(name, str) or not isinstance(body, dict):
                    continue
                body_hash = canonical_body_hash(body)
                key = (provider, section, name)
                entry = self._entries.setdefault(key, _Entry())
                entry.occurrence_count += 1
                entry.bodies.setdefault(body_hash, canonical_json(body))

    def finalize(self) -> None:
        if self._finalized:
            return
        for (provider, section, name), entry in sorted(self._entries.items()):
            body_hashes = sorted(entry.bodies)
            base_id = component_base_id(provider, section, name)
            if len(body_hashes) <= 1:
                for body_hash in body_hashes:
                    self._variant_ids[(provider, section, name, body_hash)] = base_id
                continue
            primary_hash = max(
                body_hashes,
                key=lambda body_hash: (len(entry.bodies[body_hash]), body_hash),
            )
            for body_hash in body_hashes:
                component_id = (
                    base_id
                    if body_hash == primary_hash
                    else f"{base_id}@{body_hash[:12]}"
                )
                self._variant_ids[(provider, section, name, body_hash)] = component_id
        self._finalized = True

    def component_id(
        self,
        *,
        provider: str,
        section: str,
        name: str,
        body: dict[str, Any],
    ) -> str:
        """Return the catalog-safe id for one named component body."""
        self.finalize()
        body_hash = canonical_body_hash(body)
        return self._variant_ids.get(
            (provider, section, name, body_hash),
            component_base_id(provider, section, name),
        )

    def stats(self) -> dict[str, Any]:
        self.finalize()
        base_identity_count = len(self._entries)
        variant_identity_count = len(self._variant_ids)
        conflict_count = sum(1 for entry in self._entries.values() if len(entry.bodies) > 1)
        identical_merge_count = sum(
            max(0, entry.occurrence_count - len(entry.bodies))
            for entry in self._entries.values()
        )
        return {
            "base_identity_count": base_identity_count,
            "variant_identity_count": variant_identity_count,
            "conflicting_named_identity_count": conflict_count,
            "identical_named_identity_merge_count": identical_merge_count,
        }


def build_catalog_identity_registry(
    ast_graphs: list[AstGraph],
) -> CatalogIdentityRegistry:
    registry = CatalogIdentityRegistry()
    for ast in ast_graphs:
        registry.add_ast_graph(ast)
    registry.finalize()
    return registry


def component_base_id(provider: str, section: str, name: str) -> str:
    return f"{provider}:{section}:{name}"


def provider_from_source(source: str) -> str:
    return source.split("/", 1)[0] if "/" in source else source or "unknown"


def canonical_body_hash(body: dict[str, Any]) -> str:
    return hashlib.sha1(canonical_json(body).encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
