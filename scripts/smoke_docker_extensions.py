"""Mandatory Docker startup, FTS and algo smoke with networking disabled."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main(image: str) -> None:
    root = Path(__file__).resolve().parents[1]
    # The container filesystem is disposable, but both factory cycles share
    # the same graph: the second cycle exercises writes to persisted FTS indexes.
    code = """
from pathlib import Path
from netops_api_navigator.config import Settings
from netops_api_navigator.server import create_server
from netops_api_navigator.extensions import load_extensions, preflight_extensions
import ladybug as lb
preflight_extensions()
db = lb.Database(buffer_pool_size=64*1024*1024)
with lb.Connection(db) as c:
    load_extensions(c)
    c.execute('CREATE NODE TABLE Probe(id INT64, PRIMARY KEY(id))').close()
    c.execute('CREATE REL TABLE Edge(FROM Probe TO Probe)').close()
    c.execute('CREATE (a:Probe {id:1}), (b:Probe {id:2}), (a)-[:Edge]->(b)').close()
    c.execute("CALL project_graph('probe',['Probe'],['Edge'])").close()
    with c.execute("CALL page_rank('probe') RETURN count(*)") as r:
        assert r.get_all() == [[2]]
db.close()
for iteration in range(2):
    runtime = create_server(Settings(graph_db_path=Path('/data/graph'),
        script_library_path=Path('/scripts/library'), knowledge_release_repo=''),
        start_background=False)
    try:
        gm = runtime.graph_manager
        assert gm.fts_available
        if iteration == 0:
            gm.execute("CREATE (s:Script {filename:'sentinel.py',description:'bundle sentinel'})")
        assert gm.query("CALL QUERY_FTS_INDEX('Script','script_fts','sentinel') "
                        "RETURN node.filename AS name") == [{'name':'sentinel.py'}]
        print('offline startup, seed sync, FTS: passed', iteration)
    finally:
        runtime.close()
"""
    subprocess.run(["docker", "run", "--rm", "--network", "none", "--entrypoint",
                    "python", image, "-c", code], check=True, timeout=120)
    subprocess.run([
        sys.executable, str(root / "scripts/smoke_mcp_stdio.py"), "--",
        "docker", "run", "-i", "--rm", "--network", "none",
        "-e", "KNOWLEDGE_RELEASE_REPO=", image, "--profile", "workshop",
    ], check=True, timeout=90)


if __name__ == "__main__":
    main(sys.argv[1])
