"""Re-measure skeleton/glossary/components sizes from the cached specs.

Reads ./build/spec_cache/, runs the three projections per endpoint, and
prints size statistics.  No DB rebuild required.

Output is deterministic and intended to be redirected to a log file.
"""

from __future__ import annotations

import json
import sys
import statistics
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from netops_api_navigator.oas_normalize import (  # noqa: E402
    normalize as normalize_spec,
    project_components,
    project_glossary,
    project_skeleton,
)


def _load_specs(cache_dir: Path) -> list[dict]:
    """Load all cached normalized specs from the spec_cache layout used by
    the build pipeline.  Each provider directory holds either ``*.json``
    files (one per spec) or per-category subdirectories.
    """
    specs: list[dict] = []
    for json_path in cache_dir.rglob("*.json"):
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"  skip {json_path}: {exc}", file=sys.stderr)
            continue
        if not isinstance(data, dict) or "paths" not in data:
            continue
        try:
            normalized = normalize_spec(data)
        except Exception as exc:
            print(f"  normalize failed {json_path}: {exc}", file=sys.stderr)
            continue
        specs.append(normalized)
    return specs


def _percentile(values: list[int], p: float) -> int:
    if not values:
        return 0
    s = sorted(values)
    k = int(round((p / 100.0) * (len(s) - 1)))
    return s[k]


def _stats(name: str, sizes: list[int]) -> None:
    if not sizes:
        print(f"{name:10} <no data>")
        return
    print(
        f"{name:10} sum={sum(sizes):10} min={min(sizes):6} "
        f"p50={_percentile(sizes, 50):6} p90={_percentile(sizes, 90):6} "
        f"p95={_percentile(sizes, 95):6} p99={_percentile(sizes, 99):6} "
        f"max={max(sizes):6} mean={int(statistics.mean(sizes))}"
    )


def main() -> int:
    cache_dir = REPO_ROOT / "build" / "spec_cache"
    if not cache_dir.is_dir():
        print(f"spec cache not found at {cache_dir}", file=sys.stderr)
        return 1

    specs = _load_specs(cache_dir)
    print(f"Loaded {len(specs)} specs from {cache_dir}")

    skeleton_sizes: list[int] = []
    glossary_sizes: list[int] = []
    components_sizes: list[int] = []
    by_endpoint: list[tuple[int, str, str]] = []  # (skel_bytes, method, path)
    methods = ("get", "post", "put", "patch", "delete", "head", "options")

    seen: set[tuple[str, str]] = set()
    for spec in specs:
        for path, item in (spec.get("paths") or {}).items():
            if not isinstance(item, dict):
                continue
            for m in methods:
                if not isinstance(item.get(m), dict):
                    continue
                key = (m.upper(), path)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    skel = project_skeleton(spec, m.upper(), path)
                    gloss = project_glossary(spec, m.upper(), path)
                    comps = project_components(spec, m.upper(), path)
                except Exception as exc:
                    print(
                        f"  projection failed for {m.upper()} {path}: {exc}",
                        file=sys.stderr,
                    )
                    continue
                skel_b = len(json.dumps(skel)) if skel else 0
                gloss_b = len(json.dumps(gloss)) if gloss else 0
                comp_b = len(json.dumps(comps)) if comps else 0
                if skel_b:
                    skeleton_sizes.append(skel_b)
                    by_endpoint.append((skel_b, m.upper(), path))
                if gloss_b:
                    glossary_sizes.append(gloss_b)
                if comp_b:
                    components_sizes.append(comp_b)

    print(f"\nEndpoints projected: {len(skeleton_sizes)}")
    _stats("skeleton", skeleton_sizes)
    _stats("glossary", glossary_sizes)
    _stats("components", components_sizes)

    print("\nTop 20 skeletons by size:")
    by_endpoint.sort(reverse=True)
    for n, (b, m, p) in enumerate(by_endpoint[:20], 1):
        print(f"  {n:>3}. {b:>7} B  {m:6} {p}")

    print("\nCumulative top-N share of total skeleton bytes:")
    total = sum(skeleton_sizes) or 1
    cum = 0
    for n in (1, 5, 10, 20, 50, 100, 200, 500, 1000):
        if n > len(by_endpoint):
            break
        cum = sum(b for b, _, _ in by_endpoint[:n])
        pct = 100.0 * cum / total
        print(f"  top {n:>4}: {pct:5.1f}% of bytes  (cum {cum // 1024} KB)")
    print(f"  TOTAL : {total // 1024} KB across {len(skeleton_sizes)} endpoints")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
