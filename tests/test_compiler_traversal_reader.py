"""Tests for constrained compiler graph traversal readers."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
import shutil
import tempfile

import pytest
import real_ladybug as lb

from netops_api_navigator.compiler.ast_builder import build_ast_graph
from netops_api_navigator.compiler.ast_writer import build_ast_database
from netops_api_navigator.compiler.projection_writer import (
    build_compiler_projection_database,
)
from netops_api_navigator.compiler.semantic_builder import build_semantic_overlay
from netops_api_navigator.compiler.semantic_writer import write_semantic_database
from netops_api_navigator.compiler.traversal_reader import (
    fetch_schema_context,
    load_endpoint_context,
)

pytestmark = [pytest.mark.compiler, pytest.mark.unit]

_TEST_DB_BUFFER_POOL_SIZE = 256 * 1024 * 1024


@pytest.fixture
def repo_tmp_path() -> Iterator[Path]:
    repo_tmp = Path(__file__).resolve().parent.parent / "tmp"
    repo_tmp.mkdir(exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix="test_compiler_traversal_", dir=repo_tmp))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _build_reader_artifacts(repo_tmp_path: Path) -> tuple[Path, Path]:
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "Traversal", "version": "1.0"},
        "paths": {
            "/devices/{serial}": {
                "parameters": [
                    {
                        "name": "serial",
                        "in": "path",
                        "required": True,
                        "schema": {
                            "type": "string",
                            "pattern": "^[A-Z0-9]+$",
                            "x-parameter-source": "path-template",
                        },
                    }
                ],
                "post": {
                    "summary": "Create device",
                    "operationId": "createDevice",
                    "x-cliParam": {
                        "commandName": "device create",
                        "commandUse": "create a device",
                        "parentCommand": "device",
                        "paramKeys": [{"key": "serial"}],
                    },
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/DeviceCreate"}
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Device"}
                                }
                            },
                        }
                    },
                },
            }
        },
        "components": {
            "schemas": {
                "DeviceCreate": {
                    "type": "object",
                    "x-path": "/aruba/device/create",
                    "required": ["serial"],
                    "properties": {
                        "serial": {
                            "type": "string",
                            "default": "UNKNOWN",
                            "description": "device serial",
                            "pattern": "^[A-Z0-9]+$",
                            "minLength": 12,
                            "maxLength": 32,
                            "x-path": "/aruba/device/create/serial",
                        },
                        "tags": {
                            "type": "array",
                            "minItems": 1,
                            "x-key": ["name"],
                            "items": {"$ref": "#/components/schemas/Tag"},
                        },
                    },
                },
                "Device": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                },
                "DeviceEnvelope": {
                    "allOf": [
                        {"$ref": "#/components/schemas/DeviceCreate"},
                        {
                            "type": "object",
                            "properties": {"createdAt": {"type": "string"}},
                        },
                    ]
                },
                "Tag": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                },
                "TagAlias": {"$ref": "#/components/schemas/Tag"},
                "TagMap": {
                    "type": "object",
                    "additionalProperties": {"$ref": "#/components/schemas/Tag"},
                },
            }
        },
    }
    ast = build_ast_graph(spec, source="central/traversal")
    semantic = build_semantic_overlay(ast)
    ast_db_path = repo_tmp_path / "knowledge_db_ast"
    compiler_db_path = repo_tmp_path / "knowledge_db_compiler"
    build_ast_database(ast_db_path, [ast], buffer_pool_size=_TEST_DB_BUFFER_POOL_SIZE)
    write_semantic_database(ast_db_path, [semantic], buffer_pool_size=_TEST_DB_BUFFER_POOL_SIZE)
    build_compiler_projection_database(
        compiler_db_path,
        [ast],
        [semantic],
        buffer_pool_size=_TEST_DB_BUFFER_POOL_SIZE,
    )
    return compiler_db_path, ast_db_path


def test_endpoint_context_walks_compiler_projection_and_raw_detail(
    repo_tmp_path: Path,
) -> None:
    compiler_db_path, ast_db_path = _build_reader_artifacts(repo_tmp_path)

    context = load_endpoint_context(
        compiler_db_path=compiler_db_path,
        ast_db_path=ast_db_path,
        method="post",
        path="/devices/{serial}",
        buffer_pool_size=_TEST_DB_BUFFER_POOL_SIZE,
    )

    assert context["endpoint"]["projection_row"]["operationId"] == "createDevice"
    assert context["endpoint"]["raw_openapi"]["x-cliParam"]["commandName"] == (
        "device create"
    )
    assert [p["projection_row"]["name"] for p in context["parameters"]] == ["serial"]
    assert context["parameters"][0]["raw_openapi"]["schema"]["x-parameter-source"] == (
        "path-template"
    )
    assert context["parameters"][0]["schema"]["raw_openapi"] == {
        "pattern": "^[A-Z0-9]+$",
        "type": "string",
        "x-parameter-source": "path-template",
    }
    assert context["request_bodies"][0]["schema"]["projection_row"]["component_id"] == (
        "central:schemas:DeviceCreate"
    )
    assert context["responses"][0]["schema"]["projection_row"]["component_id"] == (
        "central:schemas:Device"
    )
    assert [
        command["projection_row"]["commandName"]
        for command in context["cli_commands"]
    ] == ["device create"]
    assert context["yang_paths"][0]["projection_row"]["yangPath"] == (
        "/aruba/device/create"
    )


def test_schema_context_walks_properties_targets_and_raw_detail(
    repo_tmp_path: Path,
) -> None:
    compiler_db_path, ast_db_path = _build_reader_artifacts(repo_tmp_path)

    compiler_db = None
    ast_db = None
    try:
        compiler_db = lb.Database(
            str(compiler_db_path),
            buffer_pool_size=_TEST_DB_BUFFER_POOL_SIZE,
        )
        ast_db = lb.Database(
            str(ast_db_path),
            buffer_pool_size=_TEST_DB_BUFFER_POOL_SIZE,
        )
        compiler_conn = lb.Connection(compiler_db)
        ast_conn = lb.Connection(ast_db)
        context = fetch_schema_context(
            compiler_conn=compiler_conn,
            ast_conn=ast_conn,
            component_id="central:schemas:DeviceCreate",
        )

        assert context["schema"]["raw_openapi"]["x-path"] == "/aruba/device/create"
        props = {prop["projection_row"]["name"]: prop for prop in context["properties"]}
        assert set(props) == {"serial", "tags"}
        assert props["serial"]["raw_openapi"]["minLength"] == 12
        assert props["serial"]["projection_row"]["pattern"] == "^[A-Z0-9]+$"
        assert props["serial"]["projection_row"]["defaultValue"] == '"UNKNOWN"'
        assert props["serial"]["projection_row"]["minLength"] == 12
        assert props["serial"]["projection_row"]["maxLength"] == 32
        assert props["serial"]["semantic_summary"]["x-path"] == (
            "/aruba/device/create/serial"
        )
        assert props["tags"]["schema"]["projection_row"]["component_id"] == (
            "central:schemas:Tag"
        )
        assert props["tags"]["item_schema"]["projection_row"]["component_id"] == (
            "central:schemas:Tag"
        )

        envelope = fetch_schema_context(
            compiler_conn=compiler_conn,
            ast_conn=ast_conn,
            component_id="central:schemas:DeviceEnvelope",
        )
        assert [entry["kind"] for entry in envelope["composition"]] == ["allOf", "allOf"]
        assert any(
            entry["schema"]["projection_row"]["component_id"]
            == "central:schemas:DeviceCreate"
            for entry in envelope["composition"]
        )

        tag_map = fetch_schema_context(
            compiler_conn=compiler_conn,
            ast_conn=ast_conn,
            component_id="central:schemas:TagMap",
        )
        assert tag_map["value_schemas"][0]["projection_row"]["component_id"] == (
            "central:schemas:Tag"
        )

        tag_alias = fetch_schema_context(
            compiler_conn=compiler_conn,
            ast_conn=ast_conn,
            component_id="central:schemas:TagAlias",
        )
        assert tag_alias["references"][0]["schema"]["projection_row"]["component_id"] == (
            "central:schemas:Tag"
        )
    finally:
        if ast_db is not None:
            ast_db.close()
        if compiler_db is not None:
            compiler_db.close()


def test_endpoint_context_can_omit_raw_payloads(repo_tmp_path: Path) -> None:
    compiler_db_path, ast_db_path = _build_reader_artifacts(repo_tmp_path)

    context = load_endpoint_context(
        compiler_db_path=compiler_db_path,
        ast_db_path=ast_db_path,
        method="POST",
        path="/devices/{serial}",
        include_raw=False,
        buffer_pool_size=_TEST_DB_BUFFER_POOL_SIZE,
    )

    assert "raw_openapi" not in context["endpoint"]
    assert "rawJson" not in context["endpoint"]["ast_node"]
    assert "raw_openapi" not in context["parameters"][0]
    assert "raw_openapi" not in context["parameters"][0]["schema"]
