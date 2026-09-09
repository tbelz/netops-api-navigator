"""Tests for resolving compiler projection rows back to L2/L1 source detail."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
import shutil
import tempfile

import pytest

from netops_api_navigator.compiler.ast_builder import build_ast_graph
from netops_api_navigator.compiler.ast_writer import build_ast_database
from netops_api_navigator.compiler.detail_reader import (
    ProjectionRowNotFoundError,
    UnknownProjectionTableError,
    load_projection_detail,
)
from netops_api_navigator.compiler.projection_writer import (
    build_compiler_projection_database,
)
from netops_api_navigator.compiler.semantic_builder import build_semantic_overlay
from netops_api_navigator.compiler.semantic_writer import write_semantic_database

pytestmark = [pytest.mark.compiler, pytest.mark.unit]

_TEST_DB_BUFFER_POOL_SIZE = 256 * 1024 * 1024


@pytest.fixture
def repo_tmp_path() -> Iterator[Path]:
    repo_tmp = Path(__file__).resolve().parent.parent / "tmp"
    repo_tmp.mkdir(exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix="test_compiler_detail_", dir=repo_tmp))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_projection_detail_recovers_raw_openapi_not_in_typed_projection(
    repo_tmp_path: Path,
) -> None:
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "Detail", "version": "1.0"},
        "paths": {
            "/devices": {
                "post": {
                    "operationId": "createDevice",
                    "x-cliCommand": {
                        "commandName": "device create",
                        "commandUse": "create a device",
                    },
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/DeviceCreate"}
                            }
                        },
                    },
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
        "components": {
            "headers": {
                "X-Central-Trace": {
                    "description": "trace",
                    "required": False,
                    "schema": {"type": "string"},
                    "x-release-stage": "private-preview",
                }
            },
            "schemas": {
                "DeviceCreate": {
                    "type": "object",
                    "required": ["serial"],
                    "properties": {
                        "serial": {
                            "type": "string",
                            "description": "device serial",
                            "minLength": 12,
                            "x-yang-hint": "/device/serial",
                        }
                    },
                }
            },
        },
    }
    ast = build_ast_graph(spec, source="central/detail")
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

    property_detail = load_projection_detail(
        compiler_db_path=compiler_db_path,
        ast_db_path=ast_db_path,
        table_name="Property",
        row_id="central:schemas:DeviceCreate#prop:serial",
        buffer_pool_size=_TEST_DB_BUFFER_POOL_SIZE,
    )

    assert property_detail["projection_row"]["name"] == "serial"
    assert property_detail["projection_row"]["minLength"] == 12
    assert property_detail["projection_row"]["constraintsJson"] == '{"minLength":12}'
    assert property_detail["semantic_node"]["kind"] == "Property"
    assert property_detail["semantic_summary"]["xExtensions"] == {
        "x-yang-hint": "/device/serial"
    }
    assert property_detail["raw_openapi"] == {
        "description": "device serial",
        "minLength": 12,
        "type": "string",
        "x-yang-hint": "/device/serial",
    }
    assert property_detail["provenance"]["json_pointer"] == (
        "/components/schemas/DeviceCreate/properties/serial"
    )
    assert property_detail["provenance"]["ingestion_status"] == "strict_valid"
    assert property_detail["provenance"]["ingestion_error_type"] == ""

    header_detail = load_projection_detail(
        compiler_db_path=compiler_db_path,
        ast_db_path=ast_db_path,
        table_name="SchemaComponent",
        row_id="central:headers:X-Central-Trace",
        buffer_pool_size=_TEST_DB_BUFFER_POOL_SIZE,
    )

    assert header_detail["semantic_node"] is None
    assert header_detail["raw_openapi"]["x-release-stage"] == "private-preview"
    assert header_detail["raw_openapi"]["schema"] == {"type": "string"}


def test_projection_detail_returns_all_matching_provenance_rows(
    repo_tmp_path: Path,
) -> None:
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "Shared", "version": "1.0"},
        "paths": {},
        "components": {
            "schemas": {
                "Shared": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                }
            }
        },
    }
    first_ast = build_ast_graph(spec, source="central/one")
    second_ast = build_ast_graph(
        {**spec, "info": {"title": "Shared Again", "version": "1.0"}},
        source="central/two",
    )
    ast_db_path = repo_tmp_path / "knowledge_db_ast"
    compiler_db_path = repo_tmp_path / "knowledge_db_compiler"
    semantics = [build_semantic_overlay(first_ast), build_semantic_overlay(second_ast)]
    build_ast_database(
        ast_db_path,
        [first_ast, second_ast],
        buffer_pool_size=_TEST_DB_BUFFER_POOL_SIZE,
    )
    write_semantic_database(ast_db_path, semantics, buffer_pool_size=_TEST_DB_BUFFER_POOL_SIZE)
    build_compiler_projection_database(
        compiler_db_path,
        [first_ast, second_ast],
        semantics,
        buffer_pool_size=_TEST_DB_BUFFER_POOL_SIZE,
    )

    detail = load_projection_detail(
        compiler_db_path=compiler_db_path,
        ast_db_path=ast_db_path,
        table_name="SchemaComponent",
        row_id="central:schemas:Shared",
        buffer_pool_size=_TEST_DB_BUFFER_POOL_SIZE,
    )

    assert detail["projection_row"]["component_id"] == "central:schemas:Shared"
    assert [row["source"] for row in detail["provenance_rows"]] == [
        "central/one",
        "central/two",
    ]
    assert detail["provenance"]["source"] == "central/one"


def test_projection_detail_prefers_strict_valid_primary_provenance(
    repo_tmp_path: Path,
) -> None:
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "Shared", "version": "1.0"},
        "paths": {},
        "components": {
            "schemas": {
                "Shared": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                }
            }
        },
    }
    degraded_ast = build_ast_graph(spec, source="central/degraded")
    degraded_ast.spec_row["ingestion_status"] = "degraded"
    degraded_ast.spec_row["ingestion_error_type"] = "validation"
    strict_ast = build_ast_graph(
        {**spec, "info": {"title": "Strict", "version": "1.0"}},
        source="central/strict",
    )
    ast_db_path = repo_tmp_path / "knowledge_db_ast"
    compiler_db_path = repo_tmp_path / "knowledge_db_compiler"
    semantics = [build_semantic_overlay(degraded_ast), build_semantic_overlay(strict_ast)]
    build_ast_database(
        ast_db_path,
        [degraded_ast, strict_ast],
        buffer_pool_size=_TEST_DB_BUFFER_POOL_SIZE,
    )
    write_semantic_database(ast_db_path, semantics, buffer_pool_size=_TEST_DB_BUFFER_POOL_SIZE)
    build_compiler_projection_database(
        compiler_db_path,
        [degraded_ast, strict_ast],
        semantics,
        buffer_pool_size=_TEST_DB_BUFFER_POOL_SIZE,
    )

    detail = load_projection_detail(
        compiler_db_path=compiler_db_path,
        ast_db_path=ast_db_path,
        table_name="SchemaComponent",
        row_id="central:schemas:Shared",
        buffer_pool_size=_TEST_DB_BUFFER_POOL_SIZE,
    )

    assert detail["provenance"]["source"] == "central/strict"
    assert detail["provenance"]["ingestion_status"] == "strict_valid"
    assert [row["ingestion_status"] for row in detail["provenance_rows"]] == [
        "strict_valid",
        "degraded",
    ]


def test_projection_detail_rejects_unknown_table(repo_tmp_path: Path) -> None:
    with pytest.raises(UnknownProjectionTableError):
        load_projection_detail(
            compiler_db_path=repo_tmp_path / "missing_compiler",
            ast_db_path=repo_tmp_path / "missing_ast",
            table_name="NotATable",
            row_id="row",
            buffer_pool_size=_TEST_DB_BUFFER_POOL_SIZE,
        )


def test_projection_detail_raises_when_row_is_missing(repo_tmp_path: Path) -> None:
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "Missing", "version": "1.0"},
        "paths": {"/ping": {"get": {"responses": {"200": {"description": "ok"}}}}},
    }
    ast = build_ast_graph(spec, source="central/missing")
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

    with pytest.raises(ProjectionRowNotFoundError):
        load_projection_detail(
            compiler_db_path=compiler_db_path,
            ast_db_path=ast_db_path,
            table_name="ApiEndpoint",
            row_id="GET:/missing",
            buffer_pool_size=_TEST_DB_BUFFER_POOL_SIZE,
        )
