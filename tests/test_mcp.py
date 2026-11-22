#!/usr/bin/env python3
"""test_mcp.py: the local MCP with a real MCP client (the Python SDK's stdio client), started the way Claude Code or
Codex start it (`qalink_bridge.py --mcp stdio --bridge <url>`), against a bridge and either the simulated console
(default: a bridge on free ports, host only) or a bridge already running (--bridge-url, e.g. one linked to Flycast).
Controls that must fail: bad arguments refused, a server at level read neither lists nor accepts push_build.

  python tests/test_mcp.py --out <dir> [--bridge-url http://127.0.0.1:52080 --program liveprog.elf --args "secs=20"]
"""
import argparse, base64, json, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'pc'))
RESULTS = []


def record(name, ok, **kw):
    RESULTS.append(dict(name=name, ok=bool(ok), **kw))
    print('%-62s %s %s' % (name, 'PASS' if ok else 'FAIL', json.dumps(kw, default=str)[:240]), flush=True)


async def session_run(url, level, fn):
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    p = StdioServerParameters(command=sys.executable, args=[os.path.join(HERE, '..', 'pc', 'qalink_bridge.py'), '--mcp',
                                                            'stdio', '--bridge', url, '--mcp-level', level])
    async with stdio_client(p) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            return await fn(s)


def structured(res):
    sc = getattr(res, 'structured_content', None)
    if sc is not None:
        return sc
    for c in res.content:
        if getattr(c, 'type', '') == 'text':
            try:
                return json.loads(c.text)
            except ValueError:
                return {'text': c.text}
    return {}


def main():
    import anyio
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='mcp-test')
    ap.add_argument('--bridge-url')
    ap.add_argument('--program', help='an ELF or BIN for push_build (default: a 64 KB raw image for the simulator)')
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    rig = None
    url = a.bridge_url
    if not url:
        from test_bridge import Rig
        rig = Rig(a.out, 'mcp-rig')
        url = rig.base
    tools_json = json.load(open(os.path.join(HERE, '..', 'pc', 'bridge', 'mcp-tools.json'), encoding='utf-8'))
    names = [t['name'] for t in tools_json['tools']]
    prog = open(a.program, 'rb').read() if a.program else b'\x00\x09' * 32000
    out = {}

    async def full(s):
        lt = await s.list_tools()
        out['names'] = [t.name for t in lt.tools]
        out['status'] = structured(await s.call_tool('link_status', {}))
        out['bad'] = await s.call_tool('step', {'count': 'many'})
        t0 = time.time()
        # the tool list's push_build has no arguments field: the program runs with none (liveprog: 60 s)
        out['push'] = structured(await s.call_tool('push_build', {'base64': base64.b64encode(prog).decode(),
                                                                  'name': os.path.basename(a.program or 'sim.bin')}))
        out['push_s'] = round(time.time() - t0, 2)
        return out

    anyio.run(session_run, url, 'run', full)
    record('list: the 23 tools of mcp-tools.json, same names', out['names'] == names, count=len(out['names']))
    record('link_status: contract 1, tier given', out['status'].get('contract') == 1 and out['status'].get('tier') in
           ('mock', 'flycast', 'console'), link=out['status'].get('link'), tier=out['status'].get('tier'))
    bad = out['bad']
    record('control: bad arguments refused ("whole number")', bad.is_error and 'whole number' in bad.content[0].text,
           text=bad.content[0].text[:80])
    pr = out['push']
    record('push_build: queued with the sha256 of the bytes sent', pr.get('state') in ('queued', 'sending', 'running') and
           pr.get('program', {}).get('sha256') == __import__('hashlib').sha256(prog).hexdigest(), id=pr.get('id'),
           state=pr.get('state'), seconds=out['push_s'])

    # wait until the program is linked, then grab and summary through MCP
    import urllib.request
    def st():
        with urllib.request.urlopen(url + '/v0/status', timeout=10) as f:
            return json.loads(f.read())
    end = time.time() + 120
    while time.time() < end:
        d = st()['device']
        if d.get('program') and d['program'].get('linked') and st()['n'] > 30:
            break
        time.sleep(0.5)
    later = {}

    async def look(s):
        t0 = time.time()
        later['grab'] = await s.call_tool('grab_screen', {})
        later['grab_s'] = round(time.time() - t0, 2)
        later['summary'] = structured(await s.call_tool('live_summary', {}))
        later['presets'] = structured(await s.call_tool('list_presets', {}))
        return later
    anyio.run(session_run, url, 'run', look)
    g = later['grab']
    imgs = [c for c in g.content if getattr(c, 'type', '') == 'image']
    png = base64.b64decode(imgs[0].data) if imgs else b''
    if png:
        open(os.path.join(a.out, 'mcp-grab.png'), 'wb').write(png)
    gs = structured(g)
    record('grab_screen: a PNG image block and its picture number', not g.is_error and png[:8] == b'\x89PNG\r\n\x1a\n',
           w=gs.get('w'), h=gs.get('h'), n=gs.get('n'), tier=gs.get('tier'), bytes=len(png), seconds=later['grab_s'])
    su = later['summary']
    record('live_summary: phases with tier, measured pictures', bool(su.get('phases')) and su.get('measured', 0) > 0 and
           su.get('tier') in ('mock', 'flycast', 'console'), tier=su.get('tier'), phases=[p['key'] for p in su.get('phases', [])],
           measured=su.get('measured'), missing=su.get('missing'))
    record('list_presets: what the program registered', bool(later['presets'].get('presets')),
           presets=[p['id'] for p in later['presets'].get('presets', [])])

    ro = {}

    async def readonly(s):
        ro['names'] = [t.name for t in (await s.list_tools()).tools]
        ro['push'] = await s.call_tool('push_build', {'base64': 'AA=='})
        return ro
    anyio.run(session_run, url, 'read', readonly)
    record('control: level read lists 11 tools and refuses push_build', len(ro['names']) == 11 and
           'push_build' not in ro['names'] and ro['push'].is_error, count=len(ro['names']))
    log = os.path.join(os.path.expanduser('~'), '.dc-qalink', 'bridge', 'mcp-calls.log')
    lines = open(log, encoding='utf-8').read().splitlines()[-12:] if os.path.exists(log) else []
    record('every MCP call logged with who and its level', lines and all('"who"' in x and '"level"' in x for x in lines),
           last=lines[-1][:160] if lines else None)
    if rig:
        rig.close()
    json.dump({'results': RESULTS}, open(os.path.join(a.out, 'mcp-results.json'), 'w'), indent=1)
    print('%d of %d passed' % (sum(r['ok'] for r in RESULTS), len(RESULTS)))
    return 0 if all(r['ok'] for r in RESULTS) else 1


if __name__ == '__main__':
    sys.exit(main())
