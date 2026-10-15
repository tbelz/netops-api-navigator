"""Exercise official extensions with an empty cache and a disposable database."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import ladybug as lb


def smoke(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=False)
    for iteration in range(2):
        db = lb.Database(str(directory / 'graph'), buffer_pool_size=64 * 1024 * 1024)
        conn = lb.Connection(db)
        try:
            conn.execute(f'CALL home_directory={json.dumps(directory.resolve().as_posix())}').close()
            for extension in ('fts', 'algo'):
                conn.execute(f'INSTALL {extension}').close()
                conn.execute(f'LOAD EXTENSION {extension}').close()
            if iteration == 0:
                conn.execute('CREATE NODE TABLE Probe(id INT64, text STRING, PRIMARY KEY(id))').close()
                conn.execute('CREATE REL TABLE Edge(FROM Probe TO Probe)').close()
                conn.execute("CREATE (a:Probe {id:1,text:'network alerts'}), "
                             "(b:Probe {id:2,text:'device inventory'}), (a)-[:Edge]->(b)").close()
                conn.execute("CALL CREATE_FTS_INDEX('Probe','probe_fts',['text'])").close()
            with conn.execute("CALL QUERY_FTS_INDEX('Probe','probe_fts','alerts') RETURN node.id") as result:
                assert result.get_all() == [[1]]
            conn.execute("CALL project_graph('probe_graph',['Probe'],['Edge'])").close()
            with conn.execute("CALL page_rank('probe_graph') RETURN count(*)") as result:
                assert result.get_all() == [[2]]
            del result
        finally:
            conn.close()
            del conn
            db.close()
        print(json.dumps({'engine': lb.__version__, 'iteration': iteration, 'fts': 'passed', 'algo': 'passed'}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True, help='New disposable directory; must not exist.')
    smoke(parser.parse_args().directory)
