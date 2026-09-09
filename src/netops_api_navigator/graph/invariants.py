"""Post-flush invariants for the schema-graph ingestion pipeline.

Runs against a freshly populated knowledge DB to catch shape-level
regressions that hand-written test fixtures cannot reproduce —
specifically the failure mode where a named ``SchemaComponent`` is
persisted with a non-trivial object body but has zero ``HAS_PROPERTY``
/ ``COMPOSED_OF`` decomposition edges (the "stub-wins" / "in-batch
shadow" bug class addressed by ADR-011).

Surfaces violations as :class:`InvariantViolation` (raised in strict
mode) or as a structured list returned for inspection.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class InvariantViolation:
    invariant: str
    detail: str
    sample: list[dict] = field(default_factory=list)

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        if not self.sample:
            return f"[{self.invariant}] {self.detail}"
        head = ", ".join(
            f"{row.get('component_id') or row.get('id') or row}"
            for row in self.sample[:5]
        )
        return f"[{self.invariant}] {self.detail} :: {head}"


class InvariantViolationError(AssertionError):
    """Raised by :func:`assert_graph_invariants` in strict mode."""

    def __init__(self, violations: list[InvariantViolation]):
        self.violations = violations
        body = "\n  ".join(str(v) for v in violations)
        super().__init__(f"{len(violations)} graph invariant(s) violated:\n  {body}")


def _rows(conn, cypher: str, params: dict | None = None) -> list[dict]:
    return list(conn.execute(cypher, parameters=params or {}).rows_as_dict())


# Anonymous inline components (e.g. ``schema#prop:foo[items]``) are
# allowed to have empty bodyJson: they are leaves of inline promotion
# whose decomposition lives on the parent. Named ``SchemaComponent``
# rows (those whose component_id has the canonical ``provider:section:Name``
# shape with no ``#`` separator) are the ones that must decompose.
_NAMED_COMPONENT_FILTER = "NOT c.component_id CONTAINS '#'"


def check_named_components_decompose(conn) -> InvariantViolation | None:
    """INV-1: every reachable, non-primitive named SchemaComponent that
    has a non-empty object/array bodyJson MUST have at least one outgoing
    ``HAS_PROPERTY``, ``COMPOSED_OF`` or ``HAS_VALUE_SCHEMA`` edge.

    Reachability is approximated by ``BODY_REFERENCES`` (from endpoints'
    request/response bodies) plus ``COMPOSED_OF`` (parent components).
    Components only reached via parameter-level bodies are also covered
    because the parameter walk populates the same edges as bodies.
    """
    rows = _rows(
        conn,
        f"""
        MATCH (c:SchemaComponent)
        WHERE c.bodyJson <> ''
          AND c.kind <> 'primitive'
          AND c.kind <> 'unresolved'
          AND c.bodyShape IN ['object', 'union-oneOf', 'union-anyOf', 'allOf-composite', 'map']
          AND NOT (c.bodyShape = 'object' AND NOT c.bodyJson CONTAINS '"properties"')
          AND {_NAMED_COMPONENT_FILTER}
          AND NOT EXISTS {{ MATCH (c)-[:HAS_PROPERTY]->() }}
          AND NOT EXISTS {{ MATCH (c)-[:COMPOSED_OF]->() }}
          AND NOT EXISTS {{ MATCH (c)-[:HAS_VALUE_SCHEMA]->() }}
        RETURN c.component_id AS component_id,
               c.name AS name,
               c.section AS section,
               c.bodyShape AS bodyShape,
               size(c.bodyJson) AS bodyLen
        ORDER BY bodyLen DESC
        LIMIT 25
        """,
    )
    if not rows:
        return None
    return InvariantViolation(
        invariant="INV-1",
        detail=(
            f"{len(rows)}+ named SchemaComponents have non-empty object/union/map "
            "bodyJson but no HAS_PROPERTY / COMPOSED_OF / HAS_VALUE_SCHEMA edges "
            "(stub-wins or eviction-skip regression)"
        ),
        sample=rows,
    )


def check_no_primitive_with_object_body(conn) -> InvariantViolation | None:
    """INV-2: ``kind='primitive'`` rows must not carry an object/union body."""
    rows = _rows(
        conn,
        """
        MATCH (c:SchemaComponent)
        WHERE c.kind = 'primitive'
          AND c.bodyJson <> ''
          AND c.bodyShape IN ['object', 'union-oneOf', 'union-anyOf', 'allOf-composite', 'map']
        RETURN c.component_id AS component_id, c.bodyShape AS bodyShape
        LIMIT 25
        """,
    )
    if not rows:
        return None
    return InvariantViolation(
        invariant="INV-2",
        detail=f"{len(rows)} primitive SchemaComponents carry object/union bodies",
        sample=rows,
    )


def check_no_duplicate_component_ids(conn) -> InvariantViolation | None:
    """INV-3: ``component_id`` is the primary key — duplicates indicate
    flush ordering / replacement bugs.
    """
    rows = _rows(
        conn,
        """
        MATCH (c:SchemaComponent)
        WITH c.component_id AS cid, COUNT(*) AS n
        WHERE n > 1
        RETURN cid AS component_id, n
        LIMIT 25
        """,
    )
    if not rows:
        return None
    return InvariantViolation(
        invariant="INV-3",
        detail=f"{len(rows)} component_ids are duplicated",
        sample=rows,
    )


def check_no_duplicate_property_names_per_parent(conn) -> InvariantViolation | None:
    """INV-5: ``HAS_PROPERTY`` must be unique on ``(parent, name)``.

    Duplicates indicate the emitter wrote a property twice for the same
    declaring component (the regression class that motivated dropping
    the allOf flattening — inherited copies stamped onto every consumer).
    """
    rows = _rows(
        conn,
        """
        MATCH (c:SchemaComponent)-[:HAS_PROPERTY]->(p:Property)
        WITH c.component_id AS parent_id, p.name AS pname, COUNT(p) AS n
        WHERE n > 1
        RETURN parent_id, pname, n
        ORDER BY n DESC
        LIMIT 25
        """,
    )
    if not rows:
        return None
    return InvariantViolation(
        invariant="INV-5",
        detail=f"{len(rows)} (component, property) pairs have duplicate HAS_PROPERTY edges",
        sample=rows,
    )


def check_named_object_components_have_properties(conn) -> InvariantViolation | None:
    """INV-6: every named ``SchemaComponent`` with ``bodyShape='object'``
    and a non-empty ``properties`` map must have at least one outgoing
    ``HAS_PROPERTY`` edge (object schemas must expose their fields).
    Genuinely empty object schemas (``{"type":"object"}`` with no
    properties) are allowed — they're valid OAS for empty payloads.
    Map-shaped or pure-union shapes are covered by INV-1's
    ``HAS_VALUE_SCHEMA`` / ``COMPOSED_OF`` clauses.
    """
    rows = _rows(
        conn,
        f"""
        MATCH (c:SchemaComponent)
        WHERE c.bodyShape = 'object'
          AND c.bodyJson <> ''
          AND c.bodyJson CONTAINS '"properties"'
          AND c.kind <> 'unresolved'
          AND {_NAMED_COMPONENT_FILTER}
          AND NOT EXISTS {{ MATCH (c)-[:HAS_PROPERTY]->() }}
        RETURN c.component_id AS component_id,
               c.name AS name,
               c.section AS section,
               size(c.bodyJson) AS bodyLen
        ORDER BY bodyLen DESC
        LIMIT 25
        """,
    )
    if not rows:
        return None
    return InvariantViolation(
        invariant="INV-6",
        detail=(
            f"{len(rows)} named object SchemaComponents have no HAS_PROPERTY edges "
            "(properties dropped during emission)"
        ),
        sample=rows,
    )


def check_body_shape_kind_agreement(conn) -> InvariantViolation | None:
    """INV-7: ``bodyShape`` must agree with ``kind``.

    Catches drift between the two classifiers — e.g. a component tagged
    ``bodyShape='union-oneOf'`` but ``kind='primitive'`` is internally
    inconsistent and confuses any agent filtering by either column.
    """
    rows = _rows(
        conn,
        """
        MATCH (c:SchemaComponent)
        WHERE c.bodyJson <> ''
          AND (
                (c.bodyShape = 'object'          AND c.kind = 'primitive')
             OR (c.bodyShape = 'union-oneOf'     AND c.kind = 'primitive')
             OR (c.bodyShape = 'union-anyOf'     AND c.kind = 'primitive')
             OR (c.bodyShape = 'allOf-composite' AND c.kind = 'primitive')
             OR (c.bodyShape = 'map'             AND c.kind = 'primitive')
             OR (c.bodyShape = 'primitive'       AND c.kind = 'object')
          )
        RETURN c.component_id AS component_id,
               c.bodyShape AS bodyShape,
               c.kind AS kind
        LIMIT 25
        """,
    )
    if not rows:
        return None
    return InvariantViolation(
        invariant="INV-7",
        detail=f"{len(rows)} SchemaComponents have bodyShape/kind disagreement",
        sample=rows,
    )


def check_no_orphaned_properties(conn) -> InvariantViolation | None:
    """INV-8: every ``Property`` must have at least one incoming
    ``HAS_PROPERTY`` edge — otherwise it is an orphan unreachable from
    any SchemaComponent.

    Orphans appear when the populator buffers a ``Property`` row plus a
    ``HAS_PROPERTY`` edge from an inline child SchemaComponent, then
    drops the inline child's component row during a richer-wins
    eviction without also dropping the dependent edge. ``COPY`` silently
    rejects the foreign-key-violating edge row under ``ignore_errors=true``
    and the Property is left dangling. A clean rebuild should always
    report zero rows.
    """
    rows = _rows(
        conn,
        """
        MATCH (p:Property)
        WHERE NOT EXISTS { MATCH (:SchemaComponent)-[:HAS_PROPERTY]->(p) }
        RETURN p.property_id AS property_id, p.name AS name
        LIMIT 25
        """,
    )
    if not rows:
        return None
    total = _rows(
        conn,
        """
        MATCH (p:Property)
        WHERE NOT EXISTS { MATCH (:SchemaComponent)-[:HAS_PROPERTY]->(p) }
        RETURN COUNT(p) AS n
        """,
    )
    n = total[0]["n"] if total else len(rows)
    return InvariantViolation(
        invariant="INV-8",
        detail=(
            f"{n} Property nodes have no incoming HAS_PROPERTY edge "
            "(orphaned by populator eviction or stale preseed)"
        ),
        sample=rows,
    )


def check_property_parent_edge_consistency(conn) -> InvariantViolation | None:
    """INV-9: every ``Property`` has at least one inbound ``HAS_PROPERTY``
    edge whose source SchemaComponent's ``component_id`` matches the
    Property's ``parent_component_id``.

    A Property carrying a ``parent_component_id`` but no edge from that
    exact parent indicates the COPY layer silently dropped a rel row
    (historically caused by Kuzu 0.15.x ``COPY ... (ignore_errors=true)``
    which discards valid rows with no diagnostic). A clean rebuild must
    report zero rows.
    """
    rows = _rows(
        conn,
        """
        MATCH (p:Property)
        WHERE p.parent_component_id <> ''
          AND NOT EXISTS {
              MATCH (c:SchemaComponent {component_id: p.parent_component_id})
                    -[:HAS_PROPERTY]->(p)
          }
        RETURN p.property_id AS property_id,
               p.parent_component_id AS parent_component_id,
               p.name AS name
        LIMIT 25
        """,
    )
    if not rows:
        return None
    total = _rows(
        conn,
        """
        MATCH (p:Property)
        WHERE p.parent_component_id <> ''
          AND NOT EXISTS {
              MATCH (c:SchemaComponent {component_id: p.parent_component_id})
                    -[:HAS_PROPERTY]->(p)
          }
        RETURN COUNT(p) AS n
        """,
    )
    n = total[0]["n"] if total else len(rows)
    return InvariantViolation(
        invariant="INV-9",
        detail=(
            f"{n} Property nodes are missing the HAS_PROPERTY edge from "
            "their declared parent_component_id (silent COPY-time drop)"
        ),
        sample=rows,
    )


def check_cli_command_has_single_endpoint(conn) -> InvariantViolation | None:
    """INV-13: every ``CliCommand`` has exactly one inbound
    ``HAS_CLI_COMMAND`` edge from an ``ApiEndpoint``.

    Command IDs are deterministic (``<endpoint_id>::<commandName>``), so
    any duplicate or orphan points at populator bug or silent COPY
    failure.
    """
    rows = _rows(
        conn,
        """
        MATCH (c:CliCommand)
        OPTIONAL MATCH (e:ApiEndpoint)-[:HAS_CLI_COMMAND]->(c)
        WITH c, COUNT(e) AS n
        WHERE n <> 1
        RETURN c.command_id AS command_id, n
        LIMIT 25
        """,
    )
    if not rows:
        return None
    total = _rows(
        conn,
        """
        MATCH (c:CliCommand)
        OPTIONAL MATCH (e:ApiEndpoint)-[:HAS_CLI_COMMAND]->(c)
        WITH c, COUNT(e) AS n
        WHERE n <> 1
        RETURN COUNT(c) AS n
        """,
    )
    n = total[0]["n"] if total else len(rows)
    return InvariantViolation(
        invariant="INV-13",
        detail=(
            f"{n} CliCommand nodes do not have exactly one inbound "
            "HAS_CLI_COMMAND edge from an ApiEndpoint"
        ),
        sample=rows,
    )


def check_yang_path_has_module_edge(conn) -> InvariantViolation | None:
    """INV-14: every ``YangPath`` with a non-empty ``module`` has exactly
    one outbound ``IN_MODULE`` edge to the matching ``YangModule``.

    Paths whose ``_yang_module_for`` derivation yielded an empty string
    are exempted — there is no module to point at. This invariant
    guards against the populator skipping the module-edge emission when
    the column is set.
    """
    rows = _rows(
        conn,
        """
        MATCH (y:YangPath)
        WHERE y.module <> ''
        OPTIONAL MATCH (y)-[:IN_MODULE]->(m:YangModule)
        WHERE m.module = y.module
        WITH y, COUNT(m) AS n
        WHERE n <> 1
        RETURN y.yangPath AS yangPath, y.module AS module, n
        LIMIT 25
        """,
    )
    if not rows:
        return None
    total = _rows(
        conn,
        """
        MATCH (y:YangPath)
        WHERE y.module <> ''
        OPTIONAL MATCH (y)-[:IN_MODULE]->(m:YangModule)
        WHERE m.module = y.module
        WITH y, COUNT(m) AS n
        WHERE n <> 1
        RETURN COUNT(y) AS n
        """,
    )
    n = total[0]["n"] if total else len(rows)
    return InvariantViolation(
        invariant="INV-14",
        detail=(
            f"{n} YangPath nodes with a non-empty module lack a single "
            "IN_MODULE edge to the matching YangModule"
        ),
        sample=rows,
    )


_CHECKS = (
    check_named_components_decompose,
    check_no_primitive_with_object_body,
    check_no_duplicate_component_ids,
    check_no_duplicate_property_names_per_parent,
    check_named_object_components_have_properties,
    check_body_shape_kind_agreement,
    check_no_orphaned_properties,
    check_property_parent_edge_consistency,
    check_cli_command_has_single_endpoint,
    check_yang_path_has_module_edge,
)


def assert_graph_invariants(conn, *, strict: bool = True) -> list[InvariantViolation]:
    """Run every invariant against ``conn``.

    Returns the list of violations. When ``strict`` is true (the default
    used by ``build_knowledge_db.py --strict`` and the smoke tests),
    raises :class:`InvariantViolationError` if any check fails.
    """
    violations: list[InvariantViolation] = []
    for check in _CHECKS:
        v = check(conn)
        if v is not None:
            violations.append(v)
    if violations and strict:
        raise InvariantViolationError(violations)
    return violations


def format_report(violations: list[InvariantViolation]) -> str:
    """Pretty-print for the build script log."""
    if not violations:
        return f"✓ graph invariants OK ({len(_CHECKS)}/{len(_CHECKS)} checks passed)"
    lines = [f"⚠ {len(violations)} graph invariant(s) violated:"]
    for v in violations:
        lines.append(f"  • {v.invariant}: {v.detail}")
        for row in v.sample[:3]:
            lines.append(f"      {json.dumps(row, default=str)}")
        if len(v.sample) > 3:
            lines.append(f"      ... (+{len(v.sample) - 3} more)")
    return "\n".join(lines)
