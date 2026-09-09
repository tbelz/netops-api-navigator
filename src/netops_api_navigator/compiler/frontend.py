"""Task 1 — Resolved ingestion layer (ADR 011).

Wraps ``prance.ResolvingParser`` to turn a raw OpenAPI document into a
fully ``$ref``-resolved Python dict.  Failures are reported per-spec as
structured ``ResolutionFailure`` records; the caller decides whether a
batch with failures should halt the build.

This module performs two mutations on the input before handing it to
prance:

1. Strip keys beginning with ``_`` (e.g. ``_id``): the ReadMe.io export
   pipeline injects internal indexing metadata that strict OAS 3.1
   validation rejects via ``unevaluatedProperties: False``.
2. Coerce stringly-typed primitive defaults (e.g. ``"false"`` → ``false``
   for ``type: boolean``): the Aruba ReadMe.io export pipeline serialises
   Python booleans as their string representations.  OAS 3.1 / JSON
   Schema 2020-12 says ``default`` values SHOULD (not MUST) be valid
   against their declared type; ``openapi-spec-validator`` enforces this
   as a hard error.  We coerce the obvious cases here to avoid false
   failures.

No other normalisation is performed; substantive content flows through
unchanged so the downstream AST generator (Task 2) sees the original
spec.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import multiprocessing
import os
import tempfile
import threading
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import prance
import prance.util.url

# Bump when strict resolution policy changes without a dependency-version change.
_RESOLUTION_CACHE_VERSION = 1
ResolutionCache = dict[str, dict[str, str]]


@dataclass
class ResolvedSpec:
    """A spec that successfully resolved all ``$ref``s."""

    spec: dict[str, Any]
    raw_spec: dict[str, Any]
    source: str
    title: str


@dataclass
class ResolutionFailure:
    """A spec that failed validation or ref resolution."""

    source: str
    title: str
    error: str
    error_type: Literal["validation", "resolution", "unexpected"]
    raw_spec: dict[str, Any] | None = None


@dataclass
class ResolutionResult:
    """Aggregated outcome of resolving a batch of specs."""

    resolved: list[ResolvedSpec] = field(default_factory=list)
    failed: list[ResolutionFailure] = field(default_factory=list)
    workers_used: int = 1
    cache_hits: int = 0
    cache_misses: int = 0

    @property
    def total(self) -> int:
        return len(self.resolved) + len(self.failed)


def _strip_underscore_keys(obj: Any) -> Any:
    """Recursively drop keys starting with ``_``.

    ReadMe.io's spec export injects ``_id`` (and occasionally other
    underscore-prefixed metadata) into every node.  Strict OAS 3.1
    validation rejects them because the meta-schema sets
    ``unevaluatedProperties: False``.
    """
    if isinstance(obj, dict):
        return {k: _strip_underscore_keys(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_strip_underscore_keys(v) for v in obj]
    return obj


def _coerce_defaults(obj: Any) -> Any:
    """Coerce stringly-typed primitive ``default`` values to native JSON types.

    The Aruba ReadMe.io export pipeline serialises Python booleans as
    ``"true"``/``"false"`` strings rather than native JSON ``true``/``false``.
    ``openapi-spec-validator`` enforces the OAS 3.1 / JSON Schema 2020-12
    SHOULD recommendation that default values be valid against their
    declared type, treating it as a hard error.  We normalise the three
    affected primitive types (boolean, integer, number) to prevent false
    failures.

    Only acts when *both* ``type`` (as a scalar string) and ``default``
    are present in the same object.  All other content is returned
    unchanged.
    """
    if isinstance(obj, dict):
        result = {k: _coerce_defaults(v) for k, v in obj.items()}
        t = result.get("type")
        d = result.get("default")
        if isinstance(t, str) and isinstance(d, str):
            if t == "boolean":
                lowered = d.strip().lower()
                if lowered in ("true", "1"):
                    result["default"] = True
                elif lowered in ("false", "0", ""):
                    result["default"] = False
            elif t == "integer":
                try:
                    result["default"] = int(d)
                except ValueError:
                    pass
            elif t == "number":
                try:
                    result["default"] = float(d)
                except ValueError:
                    pass
        return result
    if isinstance(obj, list):
        return [_coerce_defaults(v) for v in obj]
    return obj


def _spec_title(spec: dict[str, Any], source: str) -> str:
    info = spec.get("info") or {}
    return info.get("title") or source


def clean_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Return the cleaned raw spec used as Task 1's compiler input.

    This removes ReadMe.io underscore metadata and coerces obvious
    stringly-typed primitive defaults, but does not resolve ``$ref``s or
    otherwise change OpenAPI structure. Task 2 consumes this cleaned raw
    form so reference topology remains visible in the L1 AST.
    """
    return _coerce_defaults(_strip_underscore_keys(spec))


def resolve_spec(spec: dict[str, Any], *, source: str) -> ResolvedSpec | ResolutionFailure:
    """Resolve all ``$ref``s in a single spec under strict validation.

    Args:
        spec: Raw OpenAPI document as a Python dict (e.g. ``json.load``
            of a cached spec file).
        source: Caller-supplied origin label, used in error messages and
            on the returned :class:`ResolvedSpec`.

    Returns:
        :class:`ResolvedSpec` on success, otherwise :class:`ResolutionFailure`
        with ``error_type`` set to ``"validation"``, ``"resolution"`` or
        ``"unexpected"``.  This function never raises for spec-level
        errors so callers can aggregate failures across a batch.
    """
    cleaned = clean_spec(spec)
    return _resolve_cleaned_spec(cleaned, source=source)


def resolve_specs(
    specs: list[dict[str, Any]],
    *,
    max_workers: int | None = None,
    retain_resolved_spec: bool = True,
    cache: ResolutionCache | None = None,
) -> ResolutionResult:
    """Resolve a batch of specs.

    The ``source`` label for each spec combines the ``_spec_source``
    provider tag (``"central"``/``"glp"``) stamped by the spec providers
    with the spec title so that failure messages are individually
    identifiable.  Without the title, all Central failures would appear
    as the same ambiguous ``"central"`` label.  Specs without a provider
    tag use the title alone; specs without either fall back to
    ``"unknown"``.

    Batches use a bounded process pool because Prance validation/resolution is
    CPU-bound and each spec is independent. Small batches stay sequential to
    avoid process startup overhead. ``executor.map`` preserves input order, so
    the relative order inside the resolved and failed result buckets remains
    deterministic. When expanded output is not retained, callers may provide a
    content-addressed cache of prior strict outcomes; cache misses still use the
    same Prance path.
    """
    entries = [
        (clean_spec(raw), _source_label(raw), retain_resolved_spec)
        for raw in specs
    ]
    outcomes: list[ResolvedSpec | ResolutionFailure | None] = [None] * len(entries)
    misses: list[tuple[int, tuple[dict[str, Any], str, bool], str]] = []
    result = ResolutionResult()
    cache_enabled = cache is not None and not retain_resolved_spec
    for index, entry in enumerate(entries):
        cleaned, source, _ = entry
        cache_key = _cleaned_spec_hash(cleaned) if cache_enabled else ""
        cached = cache.get(cache_key) if cache_enabled else None
        if cached is not None:
            outcomes[index] = _cached_outcome(cleaned, source, cached)
            result.cache_hits += 1
        else:
            misses.append((index, entry, cache_key))
            if cache_enabled:
                result.cache_misses += 1

    workers = _resolve_worker_count(len(misses), max_workers)
    result.workers_used = workers
    miss_entries = [entry for _, entry, _ in misses]
    if workers == 1:
        miss_outcomes = [_resolve_entry(entry) for entry in miss_entries]
    else:
        executor_kwargs = {}
        if os.environ.get("PYTEST_CURRENT_TEST") or threading.active_count() > 1:
            executor_kwargs["mp_context"] = _safe_process_context()
        with ProcessPoolExecutor(max_workers=workers, **executor_kwargs) as executor:
            miss_outcomes = list(executor.map(_resolve_entry, miss_entries))

    for (index, _, cache_key), outcome in zip(misses, miss_outcomes):
        outcomes[index] = outcome
        if cache_enabled:
            cache[cache_key] = _cache_entry(outcome)

    for outcome in outcomes:
        assert outcome is not None
        if isinstance(outcome, ResolvedSpec):
            result.resolved.append(outcome)
        else:
            result.failed.append(outcome)
    return result


def load_resolution_cache(path: Path) -> ResolutionCache:
    """Load reusable strict-validation outcomes for unchanged cleaned specs."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if payload.get("fingerprint") != resolution_cache_fingerprint():
        return {}
    entries = payload.get("entries")
    if not isinstance(entries, dict):
        return {}
    return {
        key: value
        for key, value in entries.items()
        if isinstance(key, str) and _is_valid_cache_entry(value)
    }


def write_resolution_cache(path: Path, cache: ResolutionCache) -> None:
    """Persist content-addressed strict-validation outcomes atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "fingerprint": resolution_cache_fingerprint(),
            "entries": cache,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_file.write(payload)
            temp_path = Path(temp_file.name)
        temp_path.replace(path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def resolution_cache_fingerprint() -> str:
    """Return the validation-toolchain fingerprint that invalidates old outcomes."""
    versions = []
    for package in ("prance", "openapi-spec-validator"):
        try:
            versions.append(f"{package}={importlib.metadata.version(package)}")
        except importlib.metadata.PackageNotFoundError:
            versions.append(f"{package}=unknown")
    return f"v{_RESOLUTION_CACHE_VERSION}|" + "|".join(versions)


def _source_label(raw: dict[str, Any]) -> str:
    """Return the stable provider/title label used for one raw spec."""
    provider = raw.get("_spec_source")
    title = _spec_title(raw, "unknown")
    return f"{provider}/{title}" if provider else title


def _resolve_entry(
    entry: tuple[dict[str, Any], str, bool],
) -> ResolvedSpec | ResolutionFailure:
    """Process-pool compatible wrapper around :func:`resolve_spec`."""
    cleaned, source, retain_resolved_spec = entry
    outcome = _resolve_cleaned_spec(cleaned, source=source)
    if isinstance(outcome, ResolvedSpec) and not retain_resolved_spec:
        outcome.spec = {}
    return outcome


def _resolve_cleaned_spec(
    cleaned: dict[str, Any],
    *,
    source: str,
) -> ResolvedSpec | ResolutionFailure:
    """Resolve one already-cleaned spec without repeating the cleaning walk."""
    title = _spec_title(cleaned, source)
    try:
        parser = prance.ResolvingParser(
            spec_string=json.dumps(cleaned),
            backend="openapi-spec-validator",
            lazy=True,
            strict=True,
        )
        parser.parse()
    except prance.ValidationError as e:
        return ResolutionFailure(
            source=source,
            title=title,
            error=str(e),
            error_type="validation",
            raw_spec=cleaned,
        )
    except prance.util.url.ResolutionError as e:
        return ResolutionFailure(
            source=source,
            title=title,
            error=str(e),
            error_type="resolution",
            raw_spec=cleaned,
        )
    except Exception as e:  # noqa: BLE001 — prance can raise unwrapped library errors
        return ResolutionFailure(
            source=source,
            title=title,
            error=f"{type(e).__name__}: {e}",
            error_type="unexpected",
            raw_spec=cleaned,
        )
    return ResolvedSpec(
        spec=parser.specification,
        raw_spec=cleaned,
        source=source,
        title=title,
    )


def _cleaned_spec_hash(cleaned: dict[str, Any]) -> str:
    canonical = json.dumps(cleaned, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _cached_outcome(
    cleaned: dict[str, Any],
    source: str,
    cached: dict[str, str],
) -> ResolvedSpec | ResolutionFailure:
    title = _spec_title(cleaned, source)
    if cached.get("status") == "resolved":
        return ResolvedSpec(spec={}, raw_spec=cleaned, source=source, title=title)
    error_type = cached.get("error_type", "unexpected")
    if error_type not in {"validation", "resolution", "unexpected"}:
        error_type = "unexpected"
    return ResolutionFailure(
        source=source,
        title=title,
        error=cached.get("error", "Cached strict validation failure"),
        error_type=error_type,  # type: ignore[arg-type]
        raw_spec=cleaned,
    )


def _cache_entry(outcome: ResolvedSpec | ResolutionFailure) -> dict[str, str]:
    if isinstance(outcome, ResolvedSpec):
        return {"status": "resolved"}
    return {
        "status": "failed",
        "error_type": outcome.error_type,
        "error": outcome.error,
    }


def _is_valid_cache_entry(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("status") == "resolved":
        return True
    return (
        value.get("status") == "failed"
        and value.get("error_type") in {"validation", "resolution", "unexpected"}
        and isinstance(value.get("error"), str)
    )


def _resolve_worker_count(total: int, requested: int | None) -> int:
    """Return a bounded worker count, avoiding pool overhead for small batches."""
    if requested is not None:
        if requested < 1:
            raise ValueError("max_workers must be at least 1")
        return min(total, requested) if total else 1
    if total < 8:
        return 1
    return min(total, 16, os.cpu_count() or 1)


def _safe_process_context() -> multiprocessing.context.BaseContext:
    """Avoid forking multithreaded callers while preferring a lightweight server."""
    methods = multiprocessing.get_all_start_methods()
    return multiprocessing.get_context("forkserver" if "forkserver" in methods else "spawn")
