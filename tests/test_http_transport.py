"""One graph owner must serve multiple independently closing MCP sessions."""
from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest
import ladybug as lb
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route
from starlette.testclient import TestClient

from netops_api_navigator.http_transport import LocalBearerAuth

pytestmark = pytest.mark.unit
TOKEN = "test-only-token-" + "a" * 32


def test_auth_protects_every_request_and_rejects_duplicate_headers():
    async def endpoint(request):
        return Response(status_code=204)
    app = LocalBearerAuth(Starlette(routes=[Route('/mcp', endpoint, methods=['GET', 'POST', 'DELETE'])]), TOKEN)
    with TestClient(app) as client:
        for method in ('GET', 'POST', 'DELETE'):
            assert client.request(method, '/mcp').status_code == 401
            assert client.request(method, '/mcp', headers={'Authorization': 'Bearer wrong'}).status_code == 401
            assert client.request(method, '/mcp', headers={'Authorization': f'Bearer {TOKEN}'}).status_code == 204
        assert client.get('/mcp', headers=[('Authorization', f'Bearer {TOKEN}'), ('Authorization', f'Bearer {TOKEN}')]).status_code == 401


@pytest.mark.parametrize('token', ['', 'short', 'a' * 32 + '\n', 'ü' * 32, 'a' * 32 + '\x7f'])
def test_http_rejects_invalid_token_before_creating_runtime(monkeypatch, token):
    from netops_api_navigator import server
    monkeypatch.setenv('MCP_HTTP_TOKEN', token)
    monkeypatch.setattr(server, 'create_server', lambda *a: pytest.fail('must reject before graph access'))
    assert server.main(['--transport', 'streamable-http']) == 2


@pytest.mark.integration
@pytest.mark.timeout(90)
def test_multiple_http_sessions_share_graph_and_disconnect_independently(tmp_path):
    # No inherited credentials, downloads, or production data paths.
    env = {k: v for k, v in os.environ.items() if not k.startswith(
        ('CENTRAL_', 'GREENLAKE_', 'GLP_', 'MCP_', 'KNOWLEDGE_', 'GRAPH_'))}
    env.update(KNOWLEDGE_RELEASE_REPO='', GRAPH_DB_PATH=str(tmp_path/'graph'),
               SCRIPT_LIBRARY_PATH=str(tmp_path/'scripts'), SPEC_CACHE_DIR=str(tmp_path/'cache'),
               DOCS_PATH=str(tmp_path/'docs'), READ_ONLY='false', MCP_HTTP_TOKEN=TOKEN)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    url = f'http://127.0.0.1:{port}/mcp'
    with (tmp_path/'server.log').open('w+') as log:
        proc = subprocess.Popen([sys.executable, '-m', 'netops_api_navigator.server',
                                 '--transport', 'streamable-http', '--port', str(port)],
                                env=env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 40
            while True:
                assert proc.poll() is None, 'HTTP server exited during startup'
                try:
                    if httpx.get(url, timeout=0.5).status_code == 401:
                        break
                except httpx.TransportError:
                    pass
                assert time.monotonic() < deadline, 'HTTP startup timed out'
                time.sleep(0.1)

            async def exercise():
                headers = {'Authorization': f'Bearer {TOKEN}'}
                async with streamablehttp_client(url, headers=headers) as (r1, w1, id1):
                    async with ClientSession(r1, w1) as first:
                        await first.initialize()
                        assert 'query_graph' in {t.name for t in (await first.list_tools()).tools}
                        assert not (await first.call_tool('write_graph', {'cypher': "CREATE (:Site {scopeId:'persisted', name:'HTTP test'})"})).isError
                        async with streamablehttp_client(url, headers=headers) as (r2, w2, id2):
                            async with ClientSession(r2, w2) as second:
                                await second.initialize()
                                assert id1() and id2() and id1() != id2()
                                result = await second.call_tool('query_graph', {'cypher': "MATCH (n:Site {scopeId:'persisted'}) RETURN n.scopeId"})
                                assert not result.isError and 'persisted' in str(result)
                        # DELETE of the second session must not close the runtime.
                        assert not (await first.call_tool('query_graph', {'cypher': 'RETURN 42 AS answer'})).isError
                async with streamablehttp_client(url, headers=headers) as (r3, w3, _):
                    async with ClientSession(r3, w3) as third:
                        await third.initialize()
                        result = await third.call_tool('query_graph', {'cypher': "MATCH (n:Site {scopeId:'persisted'}) RETURN n.scopeId"})
                        assert not result.isError and 'persisted' in str(result)
                # A connected idle SSE stream must not hold shutdown forever.
                stopped = False
                try:
                    async with streamablehttp_client(url, headers=headers) as (r4, w4, _):
                        async with ClientSession(r4, w4) as fourth:
                            await fourth.initialize()
                            proc.terminate()
                            await asyncio.to_thread(proc.wait, timeout=12)
                            stopped = True
                except ExceptionGroup:
                    if not stopped:
                        raise
                assert stopped
            asyncio.run(exercise())
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
                pytest.fail('HTTP server did not shut down gracefully')
            log.seek(0)
            output = log.read()
            assert TOKEN not in output
    # All sessions closed; clean process shutdown preserved the shared state.
    db = lb.Database(str(tmp_path/'graph'))
    conn = lb.Connection(db)
    try:
        with conn.execute("MATCH (n:Site {scopeId:'persisted'}) RETURN n.scopeId") as result:
            assert result.get_all() == [['persisted']]
    finally:
        conn.close()
        db.close()
