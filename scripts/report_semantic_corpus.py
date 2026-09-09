"""Report Task 1 -> L1 AST -> L2 semantic carry-through for cached specs."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from netops_api_navigator.compiler.ast_builder import build_ast_from_resolved  # noqa: E402
from netops_api_navigator.compiler.frontend import ResolvedSpec, resolve_spec  # noqa: E402
from netops_api_navigator.compiler.semantic_builder import build_semantic_overlay  # noqa: E402
from netops_api_navigator.compiler.semantic_metrics import compute_semantic_metrics  # noqa: E402


def _default_cache() -> Path:
    repo = Path(__file__).resolve().parent.parent
    hydrated = repo / "tmp" / "test_fixtures" / "central_spec_cache"
    if hydrated.is_dir():
        return hydrated
    return repo / "build" / "spec_cache" / "central"


def _ratio(count: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round(count / total, 4)


def _process_one(path_text: str) -> dict[str, Any]:
    path = Path(path_text)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {
            "status": "input_failed",
            "failure": {
                "source": f"central/{path.name}",
                "title": path.name,
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
            },
        }
    outcome = resolve_spec(raw, source=f"central/{path.name}")
    if not isinstance(outcome, ResolvedSpec):
        return {
            "status": "task1_failed",
            "failure": {
                "source": outcome.source,
                "title": outcome.title,
                "error_type": outcome.error_type,
                "error": outcome.error[:500],
            },
        }
    try:
        ast_graph = build_ast_from_resolved(outcome)
        semantic = build_semantic_overlay(ast_graph)
    except Exception as exc:  # noqa: BLE001 - report per-spec compiler failures
        return {
            "status": "ast_semantic_failed",
            "failure": {
                "source": outcome.source,
                "title": outcome.title,
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
            },
        }
    return {
        "status": "ok",
        "metrics": compute_semantic_metrics([semantic]),
    }


def _empty_aggregate() -> dict[str, Any]:
    return {
        "node_kind_counts": Counter(),
        "edge_kind_counts": Counter(),
        "total_nodes": 0,
        "total_edges": 0,
        "coverage": {},
    }


def _merge_metrics(aggregate: dict[str, Any], metrics: dict[str, Any]) -> None:
    aggregate["node_kind_counts"].update(metrics.get("node_kind_counts", {}))
    aggregate["edge_kind_counts"].update(metrics.get("edge_kind_counts", {}))
    aggregate["total_nodes"] += metrics.get("total_nodes", 0)
    aggregate["total_edges"] += metrics.get("total_edges", 0)
    for name, coverage in metrics.get("coverage", {}).items():
        target = aggregate["coverage"].setdefault(name, {"count": 0, "total": 0})
        target["count"] += coverage.get("count", 0)
        target["total"] += coverage.get("total", 0)


def _finish_aggregate(aggregate: dict[str, Any]) -> dict[str, Any]:
    coverage = {}
    for name, values in sorted(aggregate["coverage"].items()):
        count = values["count"]
        total = values["total"]
        coverage[name] = {
            "count": count,
            "total": total,
            "ratio": _ratio(count, total),
        }
    return {
        "node_kind_counts": dict(sorted(aggregate["node_kind_counts"].items())),
        "edge_kind_counts": dict(sorted(aggregate["edge_kind_counts"].items())),
        "total_nodes": aggregate["total_nodes"],
        "total_edges": aggregate["total_edges"],
        "coverage": coverage,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Summarize compiler semantic carry-through for cached OAS specs."
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=_default_cache(),
        help="Directory containing cached OpenAPI JSON specs.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=min(8, max(1, (os.cpu_count() or 2) - 1)),
        help="Parallel worker processes for per-spec resolution/build.",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=100,
        help="Print progress to stderr every N completed specs.",
    )
    args = parser.parse_args()

    files = sorted(args.cache_dir.rglob("*.json"))
    if not files:
        print(f"ERROR: no JSON specs found under {args.cache_dir}", file=sys.stderr)
        return 1

    resolved_count = 0
    input_failures: list[dict] = []
    failures: list[dict] = []
    ast_failures: list[dict] = []
    semantic_graph_count = 0
    aggregate = _empty_aggregate()
    worker_count = max(1, args.workers)
    print(
        f"processing {len(files)} specs with {worker_count} worker(s)",
        file=sys.stderr,
    )
    if worker_count == 1:
        iterator = ((_process_one(str(path)), path) for path in files)
        for completed, (result, path) in enumerate(iterator, start=1):
            if result["status"] not in {"input_failed", "task1_failed"}:
                resolved_count += 1
            if result["status"] == "ok":
                semantic_graph_count += 1
                _merge_metrics(aggregate, result["metrics"])
            elif result["status"] == "input_failed":
                input_failures.append(result["failure"])
            elif result["status"] == "task1_failed":
                failures.append(result["failure"])
            else:
                ast_failures.append(result["failure"])
            if completed == 1 or completed % args.progress_every == 0 or completed == len(files):
                print(f"completed {completed}/{len(files)} {path.name}", file=sys.stderr)
    else:
        with ProcessPoolExecutor(max_workers=worker_count) as pool:
            future_to_path = {pool.submit(_process_one, str(path)): path for path in files}
            for completed, future in enumerate(as_completed(future_to_path), start=1):
                path = future_to_path[future]
                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001 - keep bulk reports moving
                    result = {
                        "status": "worker_failed",
                        "failure": {
                            "source": f"central/{path.name}",
                            "title": path.name,
                            "error_type": type(exc).__name__,
                            "error": str(exc)[:500],
                        },
                    }
                if result["status"] not in {"input_failed", "task1_failed", "worker_failed"}:
                    resolved_count += 1
                if result["status"] == "ok":
                    semantic_graph_count += 1
                    _merge_metrics(aggregate, result["metrics"])
                elif result["status"] == "input_failed":
                    input_failures.append(result["failure"])
                elif result["status"] == "task1_failed":
                    failures.append(result["failure"])
                else:
                    ast_failures.append(result["failure"])
                if completed == 1 or completed % args.progress_every == 0 or completed == len(files):
                    print(f"completed {completed}/{len(files)} {path.name}", file=sys.stderr)

    metrics = _finish_aggregate(aggregate)
    metrics["carry_through"] = {
        "raw_spec_count": len(files),
        "input_failed_count": len(input_failures),
        "task1_resolved_count": resolved_count,
        "task1_failed_count": len(failures),
        "ast_semantic_failed_count": len(ast_failures),
        "semantic_graph_count": semantic_graph_count,
    }
    metrics["failure_samples"] = {
        "input": input_failures[:25],
        "task1": failures[:25],
        "ast_semantic": ast_failures[:25],
    }
    print(json.dumps(metrics, indent=2, sort_keys=True))
    return 2 if input_failures or ast_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
