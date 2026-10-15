"""Opt-in native Codex restart probe; isolated graph copies and GET-only seeds.

Run with uv run python scripts/probe_codex_graph_restart.py --help.
No model turn is submitted. Detailed evidence stays under repo-local tmp/.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import shutil
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def worker():
    from netops_api_navigator import server
    root = Path(os.environ['CODEX_GRAPH_PROBE_ROOT'])

    def event(name, **fields):
        with (root/'lifecycle.jsonl').open('a') as out:
            out.write(json.dumps(dict(event=name, pid=os.getpid(), **fields))+'\n')

    original_seeds = server._run_auto_seeds
    original_close = server.ServerRuntime.close
    original_create = server.create_server

    def seeds(*args, **kwargs):
        event('seeds_start')
        try:
            return original_seeds(*args, **kwargs)
        finally:
            event('seeds_done', statuses={k: v['status'] for k, v in args[0].seed_status.items()})

    def close(runtime):
        event('close_start')
        try:
            original_close(runtime)
        except BaseException as exc:
            event('close_error', kind=type(exc).__name__)
            import traceback
            with (root/'close-error.log').open('a') as out:
                traceback.print_exc(file=out)
            raise
        event('close_done')

    def create(*args, **kwargs):
        runtime = original_create(*args, **kwargs)
        if os.environ.get('CODEX_GRAPH_PROBE_SETTLED') == '1' and runtime.seed_thread:
            runtime.seed_thread.join()
        return runtime

    server._run_auto_seeds = seeds
    server.ServerRuntime.close = close
    server.create_server = create
    event('start')
    try:
        return server.main([])
    finally:
        event('main_done')


async def probe(args):
    (ROOT/'tmp').mkdir(exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='codex-graph-probe-', dir=ROOT/'tmp'))
    for name in ('graph_db', 'manifest.json'):
        shutil.copyfile(args.seed_dir/name, root/name)
    print(json.dumps({'evidence': str(root)}), flush=True)
    command_args = ['--directory', str(ROOT), 'run', '--frozen']
    if args.env_file:
        command_args += ['--env-file', str(args.env_file.resolve())]
    command_args += ['env', 'MCP_PROFILE=full', 'READ_ONLY=true', 'KNOWLEDGE_RELEASE_REPO=',
                     'KNOWLEDGE_RELEASE_TAG=', 'KNOWLEDGE_ASSET_SHA256=',
                     'MCP_COMPILER_TOOLS=false', 'MCP_RUNTIME_HYDRATION=false',
                     'MCP_KNOWLEDGE_PROJECTION=legacy',
                     'GRAPH_DB_PATH='+str(root/'graph_db'),
                     'SCRIPT_LIBRARY_PATH='+str(root/'scripts'),
                     'CODEX_GRAPH_PROBE_ROOT='+str(root),
                     'CODEX_GRAPH_PROBE_SETTLED='+str(int(args.settled)),
                     'python', str(Path(__file__).resolve()), '--worker']
    for iteration in range(args.restarts):
        with (root/f'codex-{iteration}.log').open('wb') as log:
            proc = await asyncio.create_subprocess_exec(
                args.codex, 'app-server', '--listen', 'stdio://',
                '-c', 'mcp_servers.arubacentral.command='+json.dumps(shutil.which('uv')),
                '-c', 'mcp_servers.arubacentral.args='+json.dumps(command_args),
                '-c', 'mcp_servers.arubacentral.enabled=true',
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=log,
            )

            async def rpc(index, method, params):
                proc.stdin.write((json.dumps(dict(id=index, method=method, params=params))+'\n').encode())
                await proc.stdin.drain()
                while True:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=120)
                    if not line:
                        raise RuntimeError('Codex app-server closed stdout')
                    reply = json.loads(line)
                    if reply.get('id') == index:
                        if 'error' in reply:
                            raise RuntimeError(str(reply['error']))
                        return reply['result']

            try:
                await rpc(1, 'initialize', dict(
                    clientInfo=dict(name='graph_restart_probe', version='1'),
                    capabilities=dict(experimentalApi=True)))
                proc.stdin.write(b'{"method":"initialized"}\n')
                await proc.stdin.drain()
                result = await rpc(2, 'mcpServerStatus/list',
                                   dict(serverName='arubacentral', detail='toolsAndAuthOnly'))
                status = next(s for s in result['data'] if s['name'] == 'arubacentral')
                tools = sorted(status.get('tools', {}))
                print(json.dumps(dict(iteration=iteration, tools=tools,
                                      tools_error=status.get('toolsError'))), flush=True)
                assert not status.get('toolsError') and 'query_graph' in tools
            finally:
                proc.stdin.close()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=10)
                except asyncio.TimeoutError:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=10)
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()
            # Status/list may itself tear down its temporary server. Wait for
            # worker evidence, never infer seed completion from app-server stderr.
            for _ in range(100):
                events = (root/'lifecycle.jsonl').read_text()
                if events.count('"close_done"') > iteration:
                    break
                await asyncio.sleep(.1)
            else:
                raise RuntimeError('Server did not complete graph cleanup')
    print(json.dumps({'restarts_passed': args.restarts, 'settled': args.settled}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--codex', default=shutil.which('codex'))
    parser.add_argument('--seed-dir', type=Path, help='Unopened verified graph_db and manifest.json')
    parser.add_argument('--env-file', type=Path, help='Optional credentials file; never printed')
    parser.add_argument('--settled', action='store_true', help='Wait for seeds before tool discovery')
    parser.add_argument('--restarts', type=int, default=3)
    args = parser.parse_args()
    if args.worker:
        raise SystemExit(worker())
    if not args.codex or not args.seed_dir or args.restarts < 1:
        parser.error('--codex, --seed-dir and a positive --restarts are required')
    asyncio.run(probe(args))
