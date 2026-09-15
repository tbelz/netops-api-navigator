"""Consumer-facing export integrity and publication failure boundaries."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import subprocess
import sys
from email.message import Message
from pathlib import Path

import pytest
import yaml
from openapi_spec_validator import validate

from netops_api_navigator.openapi_export import (
    ExportError,
    Source,
    build_exports,
    canonical,
    check_health,
    digest,
    merge,
    normalize,
    operation_keys,
    prefer_stable_operations,
    resolve_pointer,
    validate_references,
)

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parent.parent


def spec(path="/pets", *, version="3.1.0", model=None):
    return {
        "openapi": version,
        "info": {"title": "Example", "version": "1"},
        "servers": [{"url": "https://api.example.test"}],
        "components": {"schemas": {"Pet": model or {"type": "string"}}},
        "paths": {
            path: {
                "get": {
                    "operationId": path.strip("/"),
                    "responses": {
                        "200": {
                            "description": "OK",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Pet"},
                                }
                            },
                        },
                    },
                }
            }
        },
    }


def source(name, document):
    cleaned, _ = normalize(document, name)
    return Source(name, name.split("/")[0], cleaned, digest(document))


def response_schema(document, path):
    root = document["paths"][path]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]
    return resolve_pointer(document, root["$ref"])


def manifest(counts=None):
    counts = counts or {"MRT": 1, "Config": 1}
    return {
        "built_at": "2026-09-15T04:38:44Z",
        "sync_health": {
            "central": {
                "status": "ok",
                "spec_count": sum(counts.values()),
                "sources": [
                    {
                        "name": area,
                        "fetched_specs": n,
                        "cached_specs": 0,
                        "discovered": n,
                        "missing_specs": 0,
                        "discovery_error": None,
                        "failure_reasons": {},
                    }
                    for area, n in counts.items()
                ],
            }
        },
    }


def write_inputs(tmp_path):
    cache = tmp_path / "cache"
    for area, path in (("MRT", "/pets"), ("Config", "/configuration")):
        directory = cache / area
        directory.mkdir(parents=True)
        (directory / "get.json").write_text(canonical(spec(path)))
    return cache


def test_same_named_models_keep_distinct_meanings_and_safe_duplicates_merge():
    a, b, c = spec("/a"), spec("/b", model={"type": "integer"}), spec("/c")
    merged = merge(
        [source("MRT/a.json", a), source("MRT/b.json", b), source("Config/c.json", c)], "API"
    )
    assert response_schema(merged, "/a") == {"type": "string"}
    assert response_schema(merged, "/b") == {"type": "integer"}
    assert response_schema(merged, "/c") == {"type": "string"}
    assert len(merged["components"]["schemas"]) == 2
    validate_references(merged)
    validate(merged)


def test_equal_raw_refs_with_different_targets_are_not_merged():
    documents = []
    for label, kind in (("a", "string"), ("b", "integer")):
        doc = spec(
            f"/{label}",
            model={
                "type": "object",
                "properties": {"value": {"$ref": "#/components/schemas/Value"}},
            },
        )
        doc["components"]["schemas"]["Value"] = {"type": kind}
        documents.append(source(f"MRT/{label}.json", doc))
    merged = merge(documents, "API")
    for path, kind in (("/a", "string"), ("/b", "integer")):
        parent = response_schema(merged, path)
        assert resolve_pointer(merged, parent["properties"]["value"]["$ref"])["type"] == kind
    assert len(merged["components"]["schemas"]) == 4


def test_nested_identical_models_deduplicate_after_reference_rewriting():
    a = spec("/a", model={"type": "array", "items": {"$ref": "#/components/schemas/Leaf"}})
    a["components"]["schemas"]["Leaf"] = {"type": "integer"}
    b = copy.deepcopy(a)
    b["paths"] = {"/b": b["paths"].pop("/a")}
    b["paths"]["/b"]["get"]["operationId"] = "b"
    result = merge([source("MRT/a.json", a), source("Config/b.json", b)], "API")
    assert len(result["components"]["schemas"]) == 2


def test_cycles_and_escaped_component_pointers_remain_local():
    doc = spec(
        model={
            "type": "object",
            "properties": {
                "child": {"$ref": "#/components/schemas/Pet"},
            },
        }
    )
    doc["components"]["schemas"]["Pet"]["properties"]["odd"] = {
        "$ref": "#/components/schemas/Pet/properties/child"
    }
    result = merge([source("MRT/a.json", doc)], "API")
    assert validate_references(result) == 3
    assert response_schema(result, "/pets")["properties"]["child"]["$ref"].startswith("#/")
    assert resolve_pointer({"a/b": {"~key": True}}, "#/a~1b/~0key") is True


def test_defaults_only_change_in_schema_positions_and_inputs_are_unchanged():
    doc = spec(
        model={
            "type": "object",
            "properties": {
                "_id": {"type": "integer", "default": "1"},
                "x-switch": {"type": "boolean", "default": "false"},
                "text": {"type": "string", "default": 1},
            },
            "example": {"type": "integer", "default": "42", "_id": "keep"},
            "default": {"type": "boolean", "default": "false"},
        }
    )
    doc["paths"]["/pets"]["get"]["x-payload"] = {"type": "boolean", "default": "false"}
    before = copy.deepcopy(doc)
    result, changes = normalize(doc, "MRT/a.json")
    model = result["components"]["schemas"]["Pet"]
    assert model["properties"]["_id"]["default"] == 1
    assert model["properties"]["x-switch"]["default"] is False
    assert model["properties"]["text"]["default"] == "1"
    assert model["example"] == before["components"]["schemas"]["Pet"]["example"]
    assert model["default"] == before["components"]["schemas"]["Pet"]["default"]
    assert result["paths"] == doc["paths"]
    assert len(changes) == 3
    assert doc == before


def test_openapi30_nullable_and_exclusive_bounds_preserve_constraints():
    doc = spec(
        version="3.0.3",
        model={"type": "number", "nullable": True, "minimum": 1, "exclusiveMinimum": True},
    )
    result, _ = normalize(doc, "MRT/a.json")
    assert result["openapi"] == "3.1.0"
    assert result["components"]["schemas"]["Pet"] == {
        "type": ["number", "null"],
        "exclusiveMinimum": 1,
    }
    validate(result)


def test_auth_servers_and_parameter_overrides_are_preserved():
    a = spec("/a")
    a["components"]["securitySchemes"] = {"Token": {"type": "http", "scheme": "bearer"}}
    a["security"] = [{"Token": []}]
    a["paths"]["/a"]["parameters"] = [
        {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 1}},
    ]
    a["paths"]["/a"]["get"]["parameters"] = [
        {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 2}},
    ]
    b = spec("/token")
    b["servers"] = [{"url": "https://auth.example.test"}]
    result = merge([source("MRT/a.json", a), source("MRT/token.json", b)], "API")
    op = result["paths"]["/a"]["get"]
    assert len(op["parameters"]) == 1
    assert op["parameters"][0]["schema"]["default"] == 2
    scheme = next(iter(op["security"][0]))
    assert result["components"]["securitySchemes"][scheme]["scheme"] == "bearer"
    assert result["paths"]["/token"]["get"]["security"] == []
    assert result["paths"]["/token"]["get"]["servers"] == b["servers"]
    validate_references(result)


def test_shadowed_undefined_security_can_be_removed_but_effective_security_cannot():
    doc = spec()
    doc["security"] = [{"Undefined": []}]
    with pytest.raises(ExportError, match="Unknown security"):
        validate_references(normalize(doc, "MRT/a.json")[0])
    doc["paths"]["/pets"]["get"]["security"] = []
    normalized, changes = normalize(doc, "MRT/a.json")
    validate_references(normalized)
    assert "security" not in normalized
    assert changes[0]["rule"] == "remove_shadowed_security"


def test_security_and_requirements_do_not_collapse_different_credential_names():
    doc = spec()
    mechanism = {"type": "apiKey", "in": "header", "name": "Token"}
    doc["components"]["securitySchemes"] = {"A": mechanism, "B": mechanism}
    doc["security"] = [{"A": [], "B": []}]
    result = merge([source("MRT/a.json", doc)], "API")
    assert len(result["paths"]["/pets"]["get"]["security"][0]) == 2


def test_discriminator_mapping_and_examples_survive_component_renaming():
    doc = spec(
        model={
            "oneOf": [{"$ref": "#/components/schemas/Cat"}],
            "discriminator": {"propertyName": "kind"},
        }
    )
    doc["components"]["schemas"]["Cat"] = {
        "type": "object",
        "properties": {"kind": {"type": "string"}},
        "example": {"$ref": "this is payload data"},
    }
    result = merge([source("MRT/a.json", doc)], "API")
    model = response_schema(result, "/pets")
    cat = resolve_pointer(result, model["discriminator"]["mapping"]["Cat"])
    assert cat["example"] == {"$ref": "this is payload data"}
    validate_references(result)


@pytest.mark.parametrize("ref", ["#/components/schemas/Missing", "https://example.test/spec.json"])
def test_missing_and_external_refs_fail(ref):
    doc = spec(model={"$ref": ref})
    with pytest.raises(ExportError):
        validate_references(doc)


def test_operation_conflicts_fail():
    with pytest.raises(ExportError, match="Conflicting operation"):
        merge([source("MRT/a.json", spec()), source("Config/b.json", spec())], "API")


@pytest.mark.parametrize(
    "field,value",
    [
        ("cached_specs", 1),
        ("discovery_error", "timeout"),
        ("fetched_specs", 0),
        ("failure_reasons", {"http_500": 1}),
    ],
)
def test_incomplete_or_stale_scrapes_fail(field, value):
    report = manifest()
    report["sync_health"]["central"]["sources"][0][field] = value
    with pytest.raises(ExportError):
        check_health(report, {"MRT": 1, "Config": 1})


def test_pages_without_oas_are_accounted_for():
    report = manifest()
    report["sync_health"]["central"]["sources"][0].update(
        missing_specs=1, discovered=2, failure_reasons={"no_oas_block": 1}
    )
    check_health(report, {"MRT": 1, "Config": 1})


def version_pair():
    alpha_path = "/network-config/v1alpha1/sites"
    stable_path = "/network-config/v1/sites"
    alpha = spec(alpha_path)
    alpha["paths"][alpha_path]["get"].update(
        deprecated=True, description=f"Use {stable_path} instead."
    )
    stable = spec(stable_path, model={"type": "integer"})
    return alpha, stable, alpha_path, stable_path


def test_prefer_stable_preserves_successor_and_unreplaced_methods():
    alpha, stable, old, new = version_pair()
    alpha["paths"][old]["post"] = copy.deepcopy(alpha["paths"][old]["get"])
    alpha["paths"][old]["post"]["operationId"] = "createAlpha"
    document = merge([source("Config/old.json", alpha), source("Config/new.json", stable)], "API")
    successor = copy.deepcopy(document["paths"][new])
    replacements = prefer_stable_operations(document)
    assert operation_keys(document) == {("get", new), ("post", old)}
    assert document["paths"][new] == successor
    assert response_schema(document, new) == {"type": "integer"}
    assert replacements == [
        {
            "method": "get",
            "alpha_path": old,
            "stable_path": new,
            "alpha_operation_id": old.strip("/"),
            "stable_operation_id": new.strip("/"),
            "alpha_source": "Config/old.json",
            "stable_source": "Config/new.json",
        }
    ]
    validate_references(document)
    validate(document)


@pytest.mark.parametrize(
    "uncertain",
    [
        "not_deprecated",
        "deprecated_successor",
        "description",
        "different_server",
        "different_area",
        "different_major",
    ],
)
def test_alpha_is_retained_without_an_unambiguous_stable_successor(uncertain):
    alpha, stable, old, new = version_pair()
    area = "Config"
    if uncertain == "not_deprecated":
        alpha["paths"][old]["get"].pop("deprecated")
    elif uncertain == "deprecated_successor":
        stable["paths"][new]["get"]["deprecated"] = True
    elif uncertain == "description":
        alpha["paths"][old]["get"]["description"] = f"Use {new}-other instead."
    elif uncertain == "different_server":
        stable["servers"] = [{"url": "https://other.example.test"}]
    elif uncertain == "different_area":
        area = "MRT"
    elif uncertain == "different_major":
        stable["paths"] = {new.replace("/v1/", "/v2/"): stable["paths"].pop(new)}
    document = merge([source("Config/old.json", alpha), source(f"{area}/new.json", stable)], "API")
    before = operation_keys(document)
    assert prefer_stable_operations(document) == []
    assert operation_keys(document) == before


@pytest.mark.parametrize("reference_kind", ["operationId", "operationRef"])
def test_links_to_removed_operations_block_export(reference_kind):
    alpha, stable, old, new = version_pair()
    document = merge([source("Config/old.json", alpha), source("Config/new.json", stable)], "API")
    target = (
        old.strip("/")
        if reference_kind == "operationId"
        else "#/paths/" + old.replace("/", "~1") + "/get"
    )
    document["paths"][new]["get"]["responses"]["200"]["links"] = {
        "previous": {reference_kind: target}
    }
    with pytest.raises(ExportError):
        prefer_stable_operations(document)
        validate_references(document)


def test_export_reports_every_replacement_and_still_validates_excluded_sources(tmp_path):
    cache = write_inputs(tmp_path)
    alpha, stable, old, new = version_pair()
    (cache / "Config" / "get.json").write_text(canonical(alpha))
    (cache / "Config" / "stable.json").write_text(canonical(stable))
    health = manifest({"MRT": 1, "Config": 2})
    files, report = build_exports(cache, health)
    repeated, _ = build_exports(cache, health, workflow_url="another-day")
    combined = json.loads(files["central-openapi.json"])
    assert old not in combined["paths"]
    for name, details in report["outputs"].items():
        assert files[name] == repeated[name]
        assert details["source_operations"] == details["operations"] + len(details["replacements"])
        assert details["superseded_alpha_operations"] == (0 if "monitoring" in name else 1)
    assert report["source_count"] == 3
    assert (
        report["outputs"]["central-openapi.json"]["replacements"]
        == report["outputs"]["central-config-openapi.json"]["replacements"]
    )
    alpha["paths"][old]["get"]["responses"] = "invalid source"
    (cache / "Config" / "get.json").write_text(canonical(alpha))
    with pytest.raises(ExportError):
        build_exports(cache, health)


def test_export_sets_are_deterministic_complete_and_checksummed(tmp_path):
    cache = write_inputs(tmp_path)
    files, report = build_exports(cache, manifest())
    repeated, _ = build_exports(cache, manifest(), workflow_url="different-run")
    for filename, details in report["outputs"].items():
        assert files[filename] == repeated[filename]
        assert hashlib.sha256(files[filename]).hexdigest() == details["sha256"]
    total = operation_keys(json.loads(files["central-openapi.json"]))
    areas = operation_keys(json.loads(files["central-monitoring-openapi.json"]))
    areas |= operation_keys(json.loads(files["central-config-openapi.json"]))
    assert total == areas == {("get", "/pets"), ("get", "/configuration")}


def test_failed_export_does_not_replace_previous_publication(tmp_path):
    cache = write_inputs(tmp_path)
    (cache / "MRT" / "get.json").write_text('{"openapi": BROKEN}')
    report = tmp_path / "manifest.json"
    report.write_text(canonical(manifest()))
    previous = tmp_path / "published"
    previous.mkdir()
    (previous / "central-openapi.json").write_text("previous validated data")
    staged = tmp_path / "candidate"
    diagnostics = tmp_path / "diagnostics.json"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/build_openapi_export.py"),
            "--cache-dir",
            str(cache),
            "--manifest",
            str(report),
            "--output-dir",
            str(staged),
            "--diagnostics",
            str(diagnostics),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert not staged.exists()
    assert (previous / "central-openapi.json").read_text() == "previous validated data"
    assert json.loads(diagnostics.read_text())["status"] == "failed"


def test_workflow_keeps_validation_before_main_only_publication():
    workflow = yaml.safe_load((ROOT / ".github/workflows/update-knowledge-db.yml").read_text())
    assert workflow["concurrency"]["cancel-in-progress"] is False
    jobs = workflow["jobs"]
    assert jobs["export-openapi"]["needs"] == "build-knowledge-db"
    assert jobs["publish-openapi"]["needs"] == "export-openapi"
    assert jobs["publish-openapi"]["if"] == "github.ref == 'refs/heads/main'"
    assert jobs["publish-openapi"]["environment"]["name"] == "github-pages"
    for job in (jobs["build-knowledge-db"], jobs["publish-openapi"]):
        release = next(s for s in job["steps"] if s.get("uses", "").startswith("softprops/"))
        assert release["with"]["make_latest"] is False


@pytest.mark.parametrize("failure", [None, "checksum", "cors", "content_type", "report"])
def test_publication_verifier_checks_browser_access_and_complete_snapshot(monkeypatch, failure):
    module_spec = importlib.util.spec_from_file_location(
        "verify_publication", ROOT / "scripts/verify_openapi_publication.py"
    )
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    data = b'{"openapi":"3.1.0"}'
    report = {"outputs": {"central-openapi.json": {"sha256": hashlib.sha256(data).hexdigest()}}}
    seen = []

    def open_url(request, timeout):
        seen.append(request.full_url)
        headers = Message()
        headers["Content-Type"] = "text/html" if failure == "content_type" else "application/json"
        if failure != "cors":
            headers["Access-Control-Allow-Origin"] = "*"
        if request.full_url.endswith("export-report.json"):
            body = canonical({} if failure == "report" else report).encode()
        else:
            body = data + b" " if failure == "checksum" else data
        response = io.BytesIO(body)
        response.headers = headers
        return response

    monkeypatch.setattr(module.urllib.request, "urlopen", open_url)
    if failure:
        with pytest.raises(ValueError):
            module.verify("https://example.test/openapi/latest/", report)
    else:
        module.verify("https://example.test/openapi/latest/", report)
        assert len(seen) == 2


@pytest.mark.real_spec
@pytest.mark.slow
@pytest.mark.timeout(300)
def test_complete_central_corpus_has_no_lost_operations(real_central_specs):
    cache = real_central_specs[0].parent.parent
    counts = {area: len(list((cache / area).glob("*.json"))) for area in ("MRT", "Config")}
    files, report = build_exports(cache, manifest(counts))
    expected = set()
    for path in real_central_specs:
        expected.update(operation_keys(json.loads(path.read_text())))
    replacements = report["outputs"]["central-openapi.json"]["replacements"]
    removed = {(r["method"], r["alpha_path"]) for r in replacements}
    actual = operation_keys(json.loads(files["central-openapi.json"]))
    assert actual == expected - removed
    assert all((r["method"], r["stable_path"]) in actual for r in replacements)
    assert replacements == report["outputs"]["central-config-openapi.json"]["replacements"]
    assert report["outputs"]["central-monitoring-openapi.json"]["replacements"] == []
    assert report["source_count"] == len(real_central_specs)
