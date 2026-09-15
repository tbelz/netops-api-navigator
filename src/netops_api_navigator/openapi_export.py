"""Self-contained Central OpenAPI exports, independent of graph ingestion.

All transformations use OpenAPI/JSON Schema positions, never arbitrary payload
objects. References stay local; cycles are preserved rather than dereferenced.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import unquote

METHODS = frozenset({"get", "post", "put", "patch", "delete", "options", "head", "trace"})
AREAS = {"MRT": "monitoring", "Config": "config"}
FILES = {
    "central-openapi.json": None,
    "central-monitoring-openapi.json": "MRT",
    "central-config-openapi.json": "Config",
}
SELECTION_POLICY = "prefer_documented_stable_successors_v1"
COMPONENT_KINDS = {
    "schemas": "schema",
    "responses": "response",
    "parameters": "parameter",
    "examples": "example",
    "requestBodies": "requestBody",
    "headers": "parameter",
    "securitySchemes": "securityScheme",
    "links": "link",
    "callbacks": "callback",
    "pathItems": "pathItem",
}
SCHEMA_MAPS = {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
SCHEMA_LISTS = {"allOf", "anyOf", "oneOf", "prefixItems"}
SCHEMA_SINGLE = {
    "items",
    "additionalProperties",
    "unevaluatedProperties",
    "unevaluatedItems",
    "not",
    "contains",
    "if",
    "then",
    "else",
    "propertyNames",
    "contentSchema",
}


class ExportError(ValueError):
    """An input cannot be exported without guessing or losing information."""


def canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def pointer_part(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def pointer_parts(ref: str) -> list[str]:
    if not ref.startswith("#/"):
        raise ExportError(f"Only local JSON Pointer references are supported: {ref}")
    return [p.replace("~1", "/").replace("~0", "~") for p in unquote(ref[2:]).split("/")]


def resolve_pointer(document: dict, ref: str) -> Any:
    target: Any = document
    try:
        for part in pointer_parts(ref):
            target = target[int(part)] if isinstance(target, list) else target[part]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ExportError(f"Unresolvable reference: {ref}") from exc
    return target


def walk(document: Any, kind: str = "root", path: str = "") -> Iterator[tuple[dict, str, str]]:
    """Visit typed structural objects, excluding example/default/extension data."""
    if not isinstance(document, dict):
        return
    yield document, kind, path
    children: list[tuple[Any, str, str]] = []

    def single(key: str, child_kind: str) -> None:
        if key in document:
            children.append((document[key], child_kind, f"{path}/{pointer_part(key)}"))

    def mapping(key: str, child_kind: str, *, extensions: bool = True) -> None:
        value = document.get(key, {})
        if isinstance(value, dict):
            children.extend(
                (v, child_kind, f"{path}/{key}/{pointer_part(k)}")
                for k, v in value.items()
                if not extensions or not k.startswith("x-")
            )

    def sequence(key: str, child_kind: str) -> None:
        value = document.get(key, [])
        if isinstance(value, list):
            children.extend((v, child_kind, f"{path}/{key}/{i}") for i, v in enumerate(value))

    if kind == "root":
        mapping("paths", "pathItem")
        mapping("webhooks", "pathItem")
        single("components", "components")
        sequence("security", "security")
    elif kind == "components":
        for section, child_kind in COMPONENT_KINDS.items():
            mapping(section, child_kind, extensions=False)
    elif kind == "pathItem":
        for method in METHODS:
            single(method, "operation")
        sequence("parameters", "parameter")
    elif kind == "operation":
        sequence("parameters", "parameter")
        sequence("security", "security")
        single("requestBody", "requestBody")
        mapping("responses", "response")
        mapping("callbacks", "callback")
    elif kind in {"parameter", "media"}:
        single("schema", "schema")
        mapping("examples", "example")
        mapping("content", "media")
        if kind == "media":
            mapping("encoding", "encoding")
    elif kind == "encoding":
        mapping("headers", "parameter")
    elif kind == "response":
        mapping("content", "media")
        mapping("headers", "parameter")
        mapping("links", "link")
    elif kind == "requestBody":
        mapping("content", "media")
    elif kind == "callback":
        children.extend(
            (v, "pathItem", f"{path}/{pointer_part(k)}")
            for k, v in document.items()
            if k != "$ref" and not k.startswith("x-")
        )
    elif kind == "schema":
        for key in SCHEMA_MAPS:
            # Property names beginning with x- are data-model names, not extensions.
            value = document.get(key, {})
            if isinstance(value, dict):
                children.extend(
                    (v, "schema", f"{path}/{key}/{pointer_part(k)}") for k, v in value.items()
                )
        for key in SCHEMA_LISTS:
            sequence(key, "schema")
        for key in SCHEMA_SINGLE:
            single(key, "schema")
        single("discriminator", "discriminator")
    for value, child_kind, child_path in children:
        yield from walk(value, child_kind, child_path)


def reference_slots(document: dict, kind: str = "root") -> Iterator[tuple[dict, str]]:
    for node, node_kind, _ in walk(document, kind):
        if "$ref" in node:
            yield node, "$ref"
        if node_kind == "link" and "operationRef" in node:
            yield node, "operationRef"
        if node_kind == "discriminator":
            mapping = node.get("mapping", {})
            for key in mapping:
                yield mapping, key


def validate_references(document: dict) -> int:
    count = 0
    for node, kind, path in walk(document):
        if kind == "schema" and any(k in node for k in ("$id", "$anchor", "$dynamicRef")):
            raise ExportError(f"Unsupported schema reference scope at {path}")
        if kind == "security":
            schemes = document.get("components", {}).get("securitySchemes", {})
            if any(key not in schemes for key in node):
                raise ExportError(f"Unknown security scheme at {path}")
    for node, key in reference_slots(document):
        ref = node[key]
        if not isinstance(ref, str):
            raise ExportError(f"Reference must be a string: {ref!r}")
        target = resolve_pointer(document, ref)
        if not isinstance(target, (dict, bool)):
            raise ExportError(f"Reference target is not an object/schema: {ref}")
        count += 1
    return count


def normalize(document: dict, source: str) -> tuple[dict, list[dict]]:
    """Conservative export-only corrections; never mutate the input document."""
    out = copy.deepcopy(document)
    version = out.get("openapi", "")
    if not isinstance(version, str) or not re.fullmatch(r"3\.[01]\.\d+", version):
        raise ExportError(f"{source}: unsupported OpenAPI version {version!r}")
    old_version = version.startswith("3.0.")
    changes: list[dict] = []

    def change(path: str, rule: str, before: Any, after: Any) -> None:
        changes.append(
            {"source": source, "pointer": path, "rule": rule, "before": before, "after": after}
        )

    # ReadMe's Config exports carry a dangling root AccessToken requirement,
    # but every operation explicitly overrides it with the declared OAuth2.
    # Removing a wholly shadowed root requirement preserves effective security.
    schemes = out.get("components", {}).get("securitySchemes", {})
    if any(key not in schemes for req in out.get("security", []) for key in req):
        operations = [node for node, kind, _ in walk(out) if kind == "operation"]
        if operations and all("security" in operation for operation in operations):
            change("/security", "remove_shadowed_security", out.pop("security"), None)

    for node, kind, path in walk(out):
        for key in ("_id", "_spec_source"):
            if key in node and kind not in {"security", "components"}:
                change(f"{path}/{key}", "remove_export_metadata", node.pop(key), None)
        if kind != "schema":
            continue
        scalar_type, default = node.get("type"), node.get("default")
        corrected = default
        if "default" in node:
            if scalar_type == "boolean" and isinstance(default, str):
                if default.strip().lower() in {"true", "false"}:
                    corrected = default.strip().lower() == "true"
            elif scalar_type == "integer" and isinstance(default, str):
                if re.fullmatch(r"[+-]?\d+", default.strip()):
                    corrected = int(default)
            elif scalar_type == "number" and isinstance(default, str):
                try:
                    value = float(default)
                    if math.isfinite(value):
                        corrected = value
                except ValueError:
                    pass
            elif scalar_type == "string" and isinstance(default, (int, float, bool)):
                corrected = canonical(default)
            if type(default) is not type(corrected) or default != corrected:
                node["default"] = corrected
                change(f"{path}/default", "default_type", default, corrected)
        if old_version:
            if "nullable" in node:
                nullable = node.pop("nullable")
                if nullable is True and isinstance(scalar_type, str):
                    node["type"] = [scalar_type, "null"]
                change(path, "oas30_nullable", nullable, node.get("type"))
            for bound in ("Minimum", "Maximum"):
                key = f"exclusive{bound}"
                value = node.get(key)
                if isinstance(value, bool):
                    node.pop(key)
                    if value:
                        inclusive = bound.lower()
                        if inclusive not in node:
                            raise ExportError(f"{source}: {path}/{key} has no bound")
                        node[key] = node.pop(inclusive)
                    change(f"{path}/{key}", "oas30_exclusive_bound", value, node.get(key))
        discriminator = node.get("discriminator")
        if isinstance(discriminator, dict):
            mapping = discriminator.setdefault("mapping", {})
            for option in node.get("oneOf", []) + node.get("anyOf", []):
                if isinstance(option, dict) and "$ref" in option:
                    ref = option["$ref"]
                    parts = pointer_parts(ref)
                    if len(parts) == 3 and parts[:2] == ["components", "schemas"]:
                        mapping.setdefault(parts[2], ref)
            if not mapping:
                raise ExportError(f"{source}: ambiguous implicit discriminator at {path}")
            for key, ref in mapping.items():
                if isinstance(ref, str) and ref in out.get("components", {}).get("schemas", {}):
                    mapping[key] = f"#/components/schemas/{pointer_part(ref)}"
    if version != "3.1.0":
        change("/openapi", "openapi_version", version, "3.1.0")
    out["openapi"] = "3.1.0"
    return out, changes


@dataclass
class Source:
    name: str
    area: str
    document: dict
    sha256: str


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ExportError(f"Duplicate JSON member: {key}")
        result[key] = value
    return result


def read_json(path: Path) -> dict:
    try:
        result = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (OSError, ValueError) as exc:
        raise ExportError(f"{path}: {exc}") from exc
    if not isinstance(result, dict):
        raise ExportError(f"{path}: expected a JSON object")
    return result


def check_health(manifest: dict, counts: dict[str, int]) -> dict:
    health = manifest.get("sync_health", {}).get("central", {})
    if health.get("status") != "ok" or not manifest.get("built_at"):
        raise ExportError("Missing successful Central sync report or scrape timestamp")
    reports = health.get("sources", [])
    if len(reports) != 2 or {r.get("name") for r in reports} != set(AREAS):
        raise ExportError("Both MRT and Config source reports are required")
    for report in reports:
        name = report["name"]
        failures = report.get("failure_reasons", {})
        if (
            report.get("discovery_error")
            or report.get("cached_specs") != 0
            or any(n for reason, n in failures.items() if reason != "no_oas_block")
        ):
            raise ExportError(f"{name}: fetch failed or used cached source data")
        if not counts.get(name) or report.get("fetched_specs") != counts[name]:
            raise ExportError(f"{name}: cache file count does not match the fresh scrape")
        missing = report.get("missing_specs")
        if (
            missing != failures.get("no_oas_block", 0)
            or report.get("discovered") != counts[name] + missing
        ):
            raise ExportError(f"{name}: unexplained missing source documents")
    if health.get("spec_count") != sum(counts.values()):
        raise ExportError("Central manifest count does not match the source files")
    return copy.deepcopy(health)


def load_sources(cache_dir: Path, manifest: dict) -> tuple[list[Source], list[dict], dict]:
    from openapi_spec_validator import validate

    paths = sorted(cache_dir.rglob("*.json"))
    counts: dict[str, int] = defaultdict(int)
    for path in paths:
        parts = path.relative_to(cache_dir).parts
        if path.is_symlink() or len(parts) != 2 or parts[0] not in AREAS:
            raise ExportError(f"Unexpected Central cache path: {path}")
        counts[parts[0]] += 1
    health = check_health(manifest, counts)
    sources, changes = [], []
    for path in paths:
        name = path.relative_to(cache_dir).as_posix()
        raw = read_json(path)
        document, corrections = normalize(raw, name)
        try:
            validate_references(document)
            validate(document)
        except Exception as exc:
            raise ExportError(f"{name}: {str(exc)[:1500]}") from exc
        sources.append(Source(name, name.split("/")[0], document, digest(raw)))
        changes.extend(corrections)
    return sources, changes, health


def rewrite(document: dict, aliases: dict[str, str], kind: str = "root") -> None:
    for node, key in reference_slots(document, kind):
        ref = node[key]
        parts = pointer_parts(ref)
        if parts[0] == "components" and len(parts) >= 3:
            root = "#/" + "/".join(pointer_part(p) for p in parts[:3])
            if root not in aliases:
                raise ExportError(f"Unmapped component reference: {ref}")
            suffix = "".join("/" + pointer_part(p) for p in parts[3:])
            node[key] = aliases[root] + suffix
        elif parts[0] != "paths":
            raise ExportError(f"Unsupported cross-document reference: {ref}")
    schemes = {
        pointer_parts(k)[2]: pointer_parts(v)[2]
        for k, v in aliases.items()
        if pointer_parts(k)[:2] == ["components", "securitySchemes"]
    }
    for node, node_kind, _ in walk(document, kind):
        if node_kind == "security":
            renamed = {schemes.get(key, key): value for key, value in node.items()}
            if len(renamed) != len(node):
                raise ExportError("Merging security schemes would alter an AND requirement")
            node.clear()
            node.update(renamed)


def operation_keys(document: dict) -> set[tuple[str, str]]:
    return {
        (method, path)
        for path, item in document.get("paths", {}).items()
        for method in item
        if method in METHODS
    }


def _parameters(document: dict, path_item: dict, operation: dict) -> list[dict]:
    entries: dict[tuple[str, str], dict] = {}
    for parameter in path_item.get("parameters", []) + operation.get("parameters", []):
        resolved = parameter
        seen = set()
        while "$ref" in resolved:
            ref = resolved["$ref"]
            if ref in seen:
                raise ExportError("Cyclic parameter reference")
            seen.add(ref)
            resolved = resolve_pointer(document, ref)
        entries[(resolved["name"], resolved["in"])] = parameter
    return list(entries.values())


def deduplicate(document: dict) -> None:
    """Bottom-up merging after namespacing; cyclic variants may remain separate.

    Equal raw bodies are NOT sufficient: scoped reference targets must match.
    Each pass rewrites targets before the next equality comparison.
    """
    components = document["components"]
    while True:
        aliases = {}
        removed = []
        for section, entries in components.items():
            seen = {}
            for name in sorted(entries):
                key = (
                    canonical(entries[name]),
                    name.rsplit("__", 1)[0] if section == "securitySchemes" else "",
                )
                representative = seen.setdefault(key, name)
                old = f"#/components/{section}/{pointer_part(name)}"
                aliases[old] = f"#/components/{section}/{pointer_part(representative)}"
                if representative != name:
                    removed.append((section, name))
        if not removed:
            return
        rewrite(document, aliases)
        for section, name in removed:
            del components[section][name]


def merge(sources: list[Source], title: str) -> dict:
    if not sources:
        raise ExportError("Cannot export an empty API")
    fingerprint = digest([(s.name, s.sha256) for s in sources])
    output: dict = {
        "openapi": "3.1.0",
        "info": {
            "title": title,
            "version": fingerprint[:16],
            "description": "Generated from HPE Aruba Networking Central API documentation. "
            "Source metadata is retained on operations; this is an independent export.",
        },
        "paths": {},
        "components": {},
        "tags": [],
    }
    ids = set()
    tags = {}
    expected = set()
    for source in sources:
        spec = copy.deepcopy(source.document)
        if spec.get("webhooks"):
            raise ExportError(f"{source.name}: root webhooks require an explicit merge policy")
        aliases = {}
        namespace = hashlib.sha256(source.name.encode()).hexdigest()[:12]
        for section, entries in spec.get("components", {}).items():
            if section not in COMPONENT_KINDS:
                raise ExportError(f"{source.name}: unsupported component section {section}")
            destination = output["components"].setdefault(section, {})
            for name, body in entries.items():
                scoped = f"{name}__{namespace}"
                if scoped in destination:
                    raise ExportError(f"Component namespace collision: {scoped}")
                aliases[f"#/components/{section}/{pointer_part(name)}"] = (
                    f"#/components/{section}/{pointer_part(scoped)}"
                )
                destination[scoped] = body
        # Values are shared with output components so their references are rewritten too.
        original = copy.deepcopy(spec)
        rewrite(spec, aliases)
        for tag in spec.get("tags", []):
            # A tag is a navigation label; per-source metadata retains every description.
            tags.setdefault(tag["name"], tag)
        for path, item in spec.get("paths", {}).items():
            if "$ref" in item:
                raise ExportError(f"{source.name}: referenced path items are not supported")
            destination = output["paths"].setdefault(path, {})
            for method in sorted(METHODS & item.keys()):
                operation = item[method]
                key = (method, path)
                if key in expected:
                    raise ExportError(f"Conflicting operation {method.upper()} {path}")
                expected.add(key)
                params = _parameters(
                    original, original["paths"][path], original["paths"][path][method]
                )
                if params:
                    wrapper = {"parameters": copy.deepcopy(params)}
                    rewrite(wrapper, aliases, "operation")
                    operation["parameters"] = wrapper["parameters"]
                operation.setdefault(
                    "servers", item.get("servers", spec.get("servers", [{"url": "/"}]))
                )
                operation.setdefault("security", spec.get("security", []))
                for text_key in ("summary", "description"):
                    if text_key in item:
                        operation.setdefault(text_key, item[text_key])
                operation.setdefault(
                    "operationId", source.name.removesuffix(".json").replace("/", "_")
                )
                if operation["operationId"] in ids:
                    raise ExportError(f"Duplicate operationId: {operation['operationId']}")
                ids.add(operation["operationId"])
                operation.setdefault(
                    "tags", ["Authentication" if source.area == "MRT" else "Configuration"]
                )
                operation["x-source-document"] = {
                    "file": source.name,
                    "sha256": source.sha256,
                    "metadata": {
                        k: v
                        for k, v in source.document.items()
                        if k not in {"paths", "components", "servers", "security"}
                    },
                    "pathMetadata": {
                        k: v
                        for k, v in original["paths"][path].items()
                        if k not in METHODS and k != "parameters"
                    },
                }
                destination[method] = operation
    output["tags"] = [tags[k] for k in sorted(tags)]
    deduplicate(output)
    if operation_keys(output) != expected:
        raise ExportError("Operation coverage changed during export")
    return output


def prefer_stable_operations(document: dict) -> list[dict]:
    """Remove only deprecated alpha operations with an explicit stable successor.

    Match the same method, resource, major version, source area and servers.
    The source description must name that successor. Keep the stable operation
    intact: its parameters and models can legitimately differ from the alpha API.
    """
    before = operation_keys(document)
    replacements = []
    for method, path in sorted(before):
        stable_path, count = re.subn(r"/(v\d+)alpha\d+(?=/)", r"/\1", path)
        if count != 1:
            continue
        alpha = document["paths"][path][method]
        stable = document["paths"].get(stable_path, {}).get(method)
        if not alpha.get("deprecated") or not stable or stable.get("deprecated"):
            continue
        source = alpha["x-source-document"]["file"]
        successor_source = stable["x-source-document"]["file"]
        if source.split("/")[0] != successor_source.split("/")[0]:
            continue
        if alpha["servers"] != stable["servers"]:
            continue
        if not re.search(
            r"\bUse\s+`?" + re.escape(stable_path) + r"`?\s+instead\b",
            alpha.get("description", ""),
            re.IGNORECASE,
        ):
            continue
        replacements.append(
            {
                "method": method,
                "alpha_path": path,
                "stable_path": stable_path,
                "alpha_operation_id": alpha["operationId"],
                "stable_operation_id": stable["operationId"],
                "alpha_source": source,
                "stable_source": successor_source,
            }
        )
    for replacement in replacements:
        path = replacement["alpha_path"]
        del document["paths"][path][replacement["method"]]
        if not METHODS.intersection(document["paths"][path]):
            del document["paths"][path]
    removed_ids = {r["alpha_operation_id"] for r in replacements}
    for node, kind, _ in walk(document):
        if kind == "link" and node.get("operationId") in removed_ids:
            raise ExportError("A retained OpenAPI link targets a superseded alpha operation")
    removed = {(r["method"], r["alpha_path"]) for r in replacements}
    if operation_keys(document) != before - removed:
        raise ExportError("Operation coverage changed beyond documented alpha replacements")
    document["info"]["version"] = digest(
        {"sources": document["info"]["version"], "selection_policy": SELECTION_POLICY}
    )[:16]
    document["info"]["description"] += (
        " Deprecated alpha operations are omitted when the source explicitly identifies "
        "a matching non-deprecated stable successor. Other alpha operations remain included."
    )
    return replacements


def build_exports(
    cache_dir: Path, manifest: dict, *, workflow_url: str = "", source_commit: str = ""
) -> tuple[dict[str, bytes], dict]:
    """Build and validate ALL outputs in memory before any publication writes."""
    from openapi_spec_validator import validate

    sources, changes, health = load_sources(cache_dir, manifest)
    report: dict = {
        "format_version": 1,
        "status": "valid",
        "scraped_at": manifest["built_at"],
        "workflow_url": workflow_url,
        "source_commit": source_commit,
        "source_health": health,
        "source_count": len(sources),
        "sources": [
            {"file": s.name, "sha256": s.sha256, "operations": len(operation_keys(s.document))}
            for s in sources
        ],
        "corrections": changes,
        "selection_policy": SELECTION_POLICY,
        "outputs": {},
    }
    files = {}
    keys_by_output = {}
    for filename, area in FILES.items():
        selected = [s for s in sources if area is None or s.area == area]
        title = "HPE Aruba Networking Central" + (f" — {AREAS[area].title()}" if area else "")
        print(f"Merging and validating {filename} ({len(selected)} sources)", flush=True)
        document = merge(selected, title)
        source_operations = len(operation_keys(document))
        replacements = prefer_stable_operations(document)
        refs = validate_references(document)
        validate(document)
        keys_by_output[filename] = operation_keys(document)
        data = (canonical(document) + "\n").encode()
        files[filename] = data
        report["outputs"][filename] = {
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "operations": len(keys_by_output[filename]),
            "references": refs,
            "validation": "passed",
            "source_count": len(selected),
            "source_operations": source_operations,
            "superseded_alpha_operations": len(replacements),
            "replacements": replacements,
        }
    if (
        keys_by_output["central-monitoring-openapi.json"]
        | keys_by_output["central-config-openapi.json"]
    ) != keys_by_output["central-openapi.json"]:
        raise ExportError("Area exports do not cover the combined export")
    files["export-report.json"] = (canonical(report) + "\n").encode()
    return files, report
