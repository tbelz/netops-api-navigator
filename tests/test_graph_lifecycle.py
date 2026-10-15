"""Persistence and shutdown regressions, without Central access."""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
from unittest.mock import Mock

import pytest
import real_ladybug as lb

from netops_api_navigator.config import Settings
from netops_api_navigator.graph.manager import GraphManager
from netops_api_navigator.graph.ipc_server import GraphIPCServer

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(('message', 'expected'), [
    ('Runtime exception: Corrupted wal file. Read out invalid WAL record type.', True),
    ('IO exception: Cannot read from file: graph fileDescriptor: 7 '
     'numBytesRead: 0 numBytesToRead: 4096 position: 4901969379328', True),
    ('IO exception: Cannot read from file: graph numBytesRead: 128 numBytesToRead: 4096', True),
    ('IO exception: Cannot read from file: graph numBytesRead: 4096 numBytesToRead: 4096', False),
    ('Permission denied: /data/graph.wal', False),
    ('Could not set lock on file : /data/corrupt-graph.wal', False),
    ('Cannot open file graph.wal: Input/output error', False),
    ('Network unreachable', False),
])
def test_corruption_diagnostics_do_not_match_paths_or_unrelated_io(message, expected):
    from netops_api_navigator.server import _is_runtime_db_corruption_error
    assert _is_runtime_db_corruption_error(RuntimeError(message)) is expected


def test_unreadable_runtime_graph_and_sidecars_are_never_replaced(tmp_path, monkeypatch):
    from netops_api_navigator import server
    settings = Settings(graph_db_path=tmp_path/'graph', knowledge_release_repo='test/repo')
    files = [settings.graph_db_path, tmp_path/'graph.wal', tmp_path/'graph.shadow',
             tmp_path/'manifest.json']
    for p in files:
        p.write_bytes(b'original evidence')
    manager = Mock()
    manager.initialize.side_effect = RuntimeError('Corrupted wal file')
    monkeypatch.setattr(server, 'GraphManager', lambda path: manager)
    download = Mock(side_effect=AssertionError('must not download'))
    monkeypatch.setattr(server, '_download_runtime_knowledge_db', download)
    with pytest.raises(server.StartupError, match='preserved'):
        server._initialize_runtime_graph(settings)
    download.assert_not_called()
    manager.close.assert_called_once()
    assert all(p.read_bytes() == b'original evidence' for p in files)


def test_existing_runtime_skips_download_even_when_open_fails(tmp_path, monkeypatch):
    from netops_api_navigator import server
    path = tmp_path/'graph'
    path.write_bytes(b'existing')
    monkeypatch.setattr(server, '_download_runtime_knowledge_db',
                        Mock(side_effect=AssertionError('must not replace existing graph')))
    monkeypatch.setattr(server, '_initialize_runtime_graph',
                        Mock(side_effect=RuntimeError('Could not set lock on file')))
    with pytest.raises(RuntimeError, match='lock'):
        server.create_server(Settings(graph_db_path=path, knowledge_release_repo='test/repo'))
    assert path.read_bytes() == b'existing'


def test_close_waits_for_result_consumption_and_rejects_late_queries(monkeypatch, tmp_path):
    import netops_api_navigator.graph.manager as module
    entered, release, closed = (threading.Event() for _ in range(3))
    class Result:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def rows_as_dict(self):
            entered.set()
            assert release.wait(5)
            return [{'value': 1}]
    class Connection:
        def __init__(self, db): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, *args, **kwargs): return Result()
    monkeypatch.setattr(module.lb, 'Connection', Connection)
    manager = GraphManager(tmp_path/'graph')
    manager._db = object()
    rows = []
    query = threading.Thread(target=lambda: rows.extend(manager.query('RETURN 1')))
    closer = threading.Thread(target=lambda: (manager.close(), closed.set()))
    query.start()
    assert entered.wait(5)
    closer.start()
    try:
        assert not closed.wait(0.1)
    finally:
        release.set()
        query.join(5)
        closer.join(5)
    assert closed.is_set() and rows == [{'value': 1}]
    with pytest.raises(RuntimeError, match='not initialized'):
        manager.query('RETURN 1')


def test_ipc_stop_drains_active_write_and_wakes_idle_connections():
    entered, release, stopped = (threading.Event() for _ in range(3))
    class Manager:
        def execute(self, *args):
            entered.set()
            assert release.wait(5)
            return []
    server = GraphIPCServer(Manager())
    server.start()
    idle = socket.create_connection((server.host, server.port))
    active = socket.create_connection((server.host, server.port))
    active.sendall((json.dumps(dict(token=server.capability_token,
                                   method='execute', cypher='CREATE'))+'\n').encode())
    assert entered.wait(5)
    closer = threading.Thread(target=lambda: (server.stop(), stopped.set()))
    closer.start()
    try:
        assert not stopped.wait(0.1)
    finally:
        release.set()
        closer.join(5)
        idle.close()
        active.close()
    assert stopped.is_set()


def test_seed_shutdown_cancels_current_process_and_skips_next(tmp_path, monkeypatch):
    from netops_api_navigator import server
    settings = Settings(graph_db_path=tmp_path/'graph', script_library_path=tmp_path,
                        knowledge_release_repo='')
    (tmp_path/'first.py').write_text(
        "from pathlib import Path\nimport time\nPath('started').touch()\ntime.sleep(60)\n")
    (tmp_path/'second.py').write_text("from pathlib import Path\nPath('unexpected').touch()\n")
    monkeypatch.setattr(server, '_get_auto_run_seeds', lambda *a: ['first.py', 'second.py'])
    runtime = server.ServerRuntime(settings, Mock(), Mock(), None, None, True, False)
    runtime.seed_thread = threading.Thread(target=server._run_auto_seeds, args=(runtime, {}))
    runtime.seed_thread.start()
    import time
    deadline = time.monotonic()+5
    while not (tmp_path/'started').exists() and time.monotonic() < deadline:
        time.sleep(.01)
    try:
        assert (tmp_path/'started').exists()
    finally:
        runtime.close()
    assert not (tmp_path/'unexpected').exists()
    assert all(v['status'] == 'cancelled' for v in runtime.seed_status.values())
    runtime.graph_manager.close.assert_called_once()


# The worker uses a synthetic graph and no network, and exercises the real CLI
# signal/finally path with either an idle server or an active IPC seed writer.
WORKER = r'''
import json, os, threading, time
from pathlib import Path
import real_ladybug as lb
from netops_api_navigator import server
from netops_api_navigator.config import Settings
from netops_api_navigator.graph.manager import GraphManager
from netops_api_navigator.graph.ipc_server import GraphIPCServer
from netops_api_navigator.central_helpers import GraphHelper
from types import SimpleNamespace
root = Path(os.environ['TEST_GRAPH_ROOT'])
gm = GraphManager(root/'graph')
gm._db = lb.Database(str(gm.db_path))
gm.execute('CREATE NODE TABLE IF NOT EXISTS Marker(id INT64, value INT64, PRIMARY KEY(id))')
previous = gm.query('MATCH (n:Marker) RETURN count(n) AS count')[0]['count']
gm.execute('CREATE (:Marker {id: $id})', {'id': previous})
settings = Settings(graph_db_path=gm.db_path, knowledge_release_repo='')
runtime = server.ServerRuntime(settings, None, gm, None, None, True, False)
runtime.ipc_server = GraphIPCServer(gm)
runtime.ipc_server.start()
os.environ.update(runtime.ipc_server.environment)
def seeds():
    helper = GraphHelper()
    while not runtime.stop_event.is_set():
        helper.execute('MATCH (n:Marker) SET n.value = 42')
        (root/'writing').touch()
if os.environ['TEST_ACTIVE'] == '1':
    runtime.seed_thread = threading.Thread(target=seeds)
    runtime.seed_thread.start()
def run(**kwargs):
    (root/'ready').write_text(str(previous))
    while True:
        line = sys.stdin.readline()
        if not line: return
import sys
runtime.mcp = SimpleNamespace(run=run)
server.load_settings = lambda: settings
server.create_server = lambda settings: runtime
server.main([])
'''


@pytest.mark.skipif(os.name == 'nt', reason='Windows terminate does not deliver SIGTERM')
@pytest.mark.parametrize('active', [False, True])
@pytest.mark.parametrize('shutdown', ['eof', 'sigterm'])
def test_repeated_process_shutdown_preserves_graph(tmp_path, active, shutdown):
    import time
    env = {**os.environ, 'TEST_GRAPH_ROOT': str(tmp_path), 'TEST_ACTIVE': str(int(active))}
    for iteration in range(3):
        (tmp_path/'ready').unlink(missing_ok=True)
        (tmp_path/'writing').unlink(missing_ok=True)
        with (tmp_path/f'worker-{iteration}.log').open('w') as log:
            proc = subprocess.Popen([sys.executable, '-c', WORKER], env=env,
                                    stdin=subprocess.PIPE, stdout=log, stderr=log)
            try:
                deadline = time.monotonic()+10
                while time.monotonic() < deadline:
                    if (tmp_path/'ready').exists() and (
                        not active or (tmp_path/'writing').exists()
                    ): break
                    assert proc.poll() is None
                    time.sleep(.01)
                assert (tmp_path/'ready').read_text() == str(iteration)
                if active:
                    assert (tmp_path/'writing').exists()
                if shutdown == 'sigterm':
                    proc.send_signal(signal.SIGTERM)
                else:
                    proc.stdin.close()
                assert proc.wait(timeout=10) == (143 if shutdown == 'sigterm' else 0)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
                if proc.stdin and not proc.stdin.closed:
                    proc.stdin.close()
    with lb.Database(str(tmp_path/'graph'), read_only=True) as db:
        with lb.Connection(db) as conn:
            with conn.execute('MATCH (n:Marker) RETURN count(n)') as result:
                assert result.get_next()[0] == 3


def test_existing_graph_rejects_projection_change_without_replacement(tmp_path):
    from netops_api_navigator.server import StartupError, _check_knowledge_pin
    (tmp_path/'manifest.json').write_text(json.dumps({'knowledge_asset': 'knowledge_db.tar.gz'}))
    with pytest.raises(StartupError, match='MCP_KNOWLEDGE_PROJECTION'):
        _check_knowledge_pin(Settings(graph_db_path=tmp_path/'graph', knowledge_projection='v2'))


def test_live_database_owner_is_not_replaced(tmp_path):
    db = lb.Database(str(tmp_path/'graph'))
    conn = lb.Connection(db)
    try:
        conn.execute('CREATE NODE TABLE Marker(id INT64, PRIMARY KEY(id))').close()
        conn.execute('CREATE (:Marker {id: 7})').close()
        code = """
import sys
from pathlib import Path
from netops_api_navigator import server
from netops_api_navigator.config import Settings
def forbidden(*args, **kwargs):
    raise AssertionError('Downloader must not replace an owned graph')
server._download_runtime_knowledge_db = forbidden
try:
    server.create_server(Settings(graph_db_path=Path(sys.argv[1]), knowledge_release_repo='test/repo'))
except RuntimeError as exc:
    assert 'lock' in str(exc).lower(), str(exc)
else:
    raise AssertionError('Second process unexpectedly opened the graph')
"""
        result = subprocess.run([sys.executable, '-c', code, str(tmp_path/'graph')],
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        with conn.execute('MATCH (n:Marker) RETURN n.id') as rows:
            assert rows.get_next() == [7]
    finally:
        conn.close()
        db.close()


@pytest.mark.parametrize('failure', ['write', 'flush'])
def test_closed_client_stderr_does_not_abort_cleanup(failure):
    import structlog
    from netops_api_navigator.logging import _DisconnectSafeStderr
    stream = Mock()
    getattr(stream, failure).side_effect = BrokenPipeError('client exited')
    logger = structlog.PrintLogger(_DisconnectSafeStderr(stream))
    logger.info('graph_ipc_stopped')
    writes = stream.write.call_count
    logger.info('graph_closed')
    assert stream.write.call_count == writes


def test_sigterm_during_factory_finishes_ownership_transfer_then_closes(monkeypatch):
    from netops_api_navigator import server
    runtime = Mock()
    previous = signal.getsignal(signal.SIGTERM)
    def create(settings):
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        return runtime
    monkeypatch.setattr(server, 'load_settings', lambda: Settings())
    monkeypatch.setattr(server, 'create_server', create)
    assert server.main([]) == 128 + signal.SIGTERM
    runtime.mcp.run.assert_not_called()
    runtime.close.assert_called_once()
    assert signal.getsignal(signal.SIGTERM) is previous


def test_seed_thread_start_failure_preserves_error_and_closes_resources(tmp_path, monkeypatch):
    from netops_api_navigator import server
    client, graph, ipc = Mock(), Mock(), Mock()
    ipc.environment = {}
    class FailedThread:
        ident = None
        def __init__(self, **kwargs): pass
        def start(self): raise RuntimeError('thread capacity exhausted')
        def join(self): raise AssertionError('must not join an unstarted thread')
    monkeypatch.setattr(server.threading, 'Thread', FailedThread)
    monkeypatch.setattr(server, 'GraphIPCServer', lambda graph: ipc)
    monkeypatch.setattr(server, '_create_central_client', lambda *a, **k: (client, False))
    monkeypatch.setattr(server, '_initialize_runtime_graph', lambda settings: graph)
    monkeypatch.setattr(server, '_load_api_tree', lambda *args: '')
    monkeypatch.setattr(server, '_prepare_script_library', lambda *args: None)
    settings = Settings(graph_db_path=tmp_path/'graph', knowledge_release_repo='')
    with pytest.raises(RuntimeError, match='thread capacity exhausted'):
        server.create_server(settings)
    ipc.stop.assert_called_once()
    client.close.assert_called_once()
    graph.close.assert_called_once()


@pytest.mark.parametrize('detached', [False, True])
def test_seed_cancellation_bounds_descendant_pipe_wait(tmp_path, detached):
    import time
    from netops_api_navigator.tools.execution import _run_script
    settings = Settings(graph_db_path=tmp_path/'graph', script_library_path=tmp_path,
                        knowledge_release_repo='')
    (tmp_path/'seed.py').write_text(
        'import subprocess, sys, time\nfrom pathlib import Path\n'
        f'p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], '
        f'start_new_session={detached and os.name != "nt"})\n'
        'Path("descendant.pid").write_text(str(p.pid))\ntime.sleep(60)\n'
    )
    stop = threading.Event()
    results = []
    worker = threading.Thread(target=lambda: results.append(
        json.loads(_run_script(settings, 'seed.py', stop_event=stop))))
    worker.start()
    try:
        deadline = time.monotonic()+5
        while not (tmp_path/'descendant.pid').exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert (tmp_path/'descendant.pid').exists()
        stop.set()
        worker.join(5)
        finished_without_pipe_peer = not worker.is_alive()
    finally:
        stop.set()
        if (tmp_path/'descendant.pid').exists():
            try:
                os.kill(int((tmp_path/'descendant.pid').read_text()), signal.SIGTERM)
            except ProcessLookupError:
                pass
        worker.join(5)
    assert finished_without_pipe_peer
    assert results[0]['exit_code'] == -1


def test_second_graph_owner_has_actionable_diagnostic(tmp_path, monkeypatch):
    from netops_api_navigator import server
    manager = Mock()
    manager.initialize.side_effect = RuntimeError('IO exception: Could not set lock on file : graph')
    monkeypatch.setattr(server, 'GraphManager', lambda path: manager)
    with pytest.raises(server.StartupError, match='--transport streamable-http'):
        server._initialize_runtime_graph(Settings(graph_db_path=tmp_path/'graph'))
    manager.close.assert_called_once()
