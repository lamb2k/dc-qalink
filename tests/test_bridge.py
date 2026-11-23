#!/usr/bin/env python3
"""test_bridge.py: host tests of the bridge (contract 1) against the simulated console (simconsole.py) and, for the
push/run loop under a bad line, the loader's host build (cl_host) behind simlink.py. Every check has a control that
must fail. Any OS; Python 3.10+.

  python tests/test_bridge.py --out <evidence dir> [--host <cl_host>]
"""
import argparse, hashlib, json, os, socket, struct, sys, tempfile, threading, time, urllib.error, urllib.request, uuid, zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'pc'))
import dcql_loop as cl                   # noqa: E402
from bridge.core import Bridge          # noqa: E402
from bridge.api import serve            # noqa: E402
from bridge.live import Session         # noqa: E402
from simconsole import SimConsole       # noqa: E402

RESULTS = []
PROGRAM = b'\x00\x09' * 30000 + os.urandom(4000)   # a raw 1ST_READ-style image (64 KB): the sim does not run it


def record(name, ok, **kw):
    RESULTS.append(dict(name=name, ok=bool(ok), **kw))
    print('%-58s %s %s' % (name, 'PASS' if ok else 'FAIL', json.dumps(kw, default=str)[:260]), flush=True)


def free_port(kind=socket.SOCK_STREAM):
    s = socket.socket(socket.AF_INET, kind)
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Rig:
    def __init__(self, out, name, level='run', **sim):
        self.dir = os.path.join(out, name)
        os.makedirs(self.dir, exist_ok=True)
        self.udp, self.lport, self.http = free_port(socket.SOCK_DGRAM), free_port(socket.SOCK_DGRAM), free_port()
        self.b = Bridge(self.dir, ('127.0.0.1', self.udp), '127.0.0.1:%d' % self.lport, None, None, None, None, level)
        self.srv = serve(self.b, '127.0.0.1', self.http, level)
        self.sim = SimConsole(('127.0.0.1', self.udp), self.lport, **sim)
        self.base = 'http://127.0.0.1:%d' % self.http
        self.wait(lambda: self.get('/v0/status')['link'] == 'up', 8)

    def req(self, method, path, body=None, headers=None, raw=False):
        r = urllib.request.Request(self.base + path, data=body, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(r, timeout=60) as f:
                data = f.read()
                return f.status, (data if raw else json.loads(data))
        except urllib.error.HTTPError as e:
            data = e.read()
            return e.code, (data if raw else json.loads(data or b'{}'))

    def linked(self):
        p = self.get('/v0/status')['device']['program']
        return bool(p and p['linked'])

    def get(self, path, raw=False):
        return self.req('GET', path, raw=raw)[1]

    def post(self, path, obj=None):
        return self.req('POST', path, json.dumps(obj or {}).encode(), {'Content-Type': 'application/json'})

    def push(self, raw=PROGRAM, args='secs=2', sha=None, name='sim.bin'):
        bnd = uuid.uuid4().hex
        parts = []
        for k, v in (('sha256', sha if sha is not None else hashlib.sha256(raw).hexdigest()), ('args', args),
                     ('deadline_s', '60')):
            parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (bnd, k, v)).encode())
        parts.append(('--%s\r\nContent-Disposition: form-data; name="program"; filename="%s"\r\n'
                      'Content-Type: application/octet-stream\r\n\r\n' % (bnd, name)).encode() + raw + b'\r\n')
        body = b''.join(parts) + ('--%s--\r\n' % bnd).encode()
        return self.req('POST', '/v0/runs', body, {'Content-Type': 'multipart/form-data; boundary=%s' % bnd})

    def wait_run(self, rid, timeout=40):
        end = time.time() + timeout
        while time.time() < end:
            r = self.get('/v0/runs/%s' % rid)
            if r['state'] not in ('queued', 'sending', 'running'):
                return r
            time.sleep(0.2)
        return self.get('/v0/runs/%s' % rid)

    def wait(self, pred, timeout):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if pred():
                    return True
            except (KeyError, TypeError, urllib.error.URLError):
                pass
            time.sleep(0.1)
        return False

    def close(self):
        self.sim.close()
        self.srv.shutdown()
        self.b.close()


def sse_reader(base, path, seconds, out):
    """Reads the live stream for `seconds`; appends each event's JSON (and the id) to out."""
    def go():
        try:
            with urllib.request.urlopen(base + path, timeout=seconds + 5) as f:
                end = time.time() + seconds
                ev_id = None
                for line in f:
                    line = line.decode().rstrip('\n')
                    if line.startswith('id: '):
                        ev_id = int(line[4:])
                    elif line.startswith('data: '):
                        out.append((ev_id, json.loads(line[6:])))
                        ev_id = None
                    if time.time() > end:
                        break
        except Exception as e:
            out.append((None, {'t': 'error', 'error': repr(e)}))
    th = threading.Thread(target=go, daemon=True)
    th.start()
    return th


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=os.path.join(tempfile.gettempdir(), 'ql-bridge-tests'))
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    t_all = time.time()

    # 1. discovery, status, contract, levels ------------------------------------------------------------
    R = Rig(a.out, 'main')
    st = R.get('/v0/status')
    record('status: link up, contract 1, tier mock (simulated console)', st['link'] == 'up' and st['contract'] == 1
           and st['tier'] == 'mock', link=st['link'], tier=st['tier'])
    d = R.get('/v0/discovery')
    record('discovery: the loader beacon heard', len(d['seen']) == 1 and d['seen'][0]['beacons'] >= 1,
           seen=[(x['address'], x['beacons'], x['loader']) for x in d['seen']])
    code, body = R.req('GET', '/v0/status', headers={'X-Contract': '0'})
    record('control: a client of contract 0 is refused (409)', code == 409, code=code)

    # 2. push and run, live stream ------------------------------------------------------------------------
    events = []
    th = sse_reader(R.base, '/v0/live', 9.0, events)
    time.sleep(0.3)
    code, run = R.push(args='secs=3')
    record('push: multipart with a matching sha256 answered 202 with an opaque id', code == 202 and len(run['id']) == 8,
           code=code, id=run.get('id'))
    rid = run['id']
    R.wait(lambda: R.linked() and R.get('/v0/status')['n'] > 10, 20)
    code, still = R.post('/v0/stills')
    png = R.get('/v0/stills/%s' % still.get('id', 'x'), raw=True) if code == 200 else b''
    record('grab now (POST /v0/stills) during the run: a PNG of the screen', code == 200 and png[:8] == b'\x89PNG\r\n\x1a\n'
           and still['width'] == 160, code=code, id=still.get('id'), bytes=len(png))
    code, c1 = R.post('/v0/command', {'cmd': 'pause'})
    n1 = R.get('/v0/status')['n']
    time.sleep(0.6)
    n2 = R.get('/v0/status')['n']
    code2, c2 = R.post('/v0/command', {'cmd': 'step', 'count': 5})
    time.sleep(0.4)
    n3 = R.get('/v0/status')['n']
    code3, c3 = R.post('/v0/command', {'cmd': 'resume'})
    record('pause holds the picture count; step 5 moves it by 5', code == 200 and c1['ok'] and abs(n2 - n1) <= 3 and
           code2 == 200 and 4 <= n3 - n2 <= 8 and code3 == 200, n=(n1, n2, n3))
    code, w = R.post('/v0/command', {'cmd': 'warp', 'preset': 'nowhere'})
    code_ok, w2 = R.post('/v0/command', {'cmd': 'warp', 'preset': 'burst'})
    record('warp: a registered preset ok; control: an unknown one refused', code_ok == 200 and w2['ok'] and
           not w['ok'], good=w2, bad=w)
    code, o = R.post('/v0/command', {'cmd': 'option', 'name': 'cubes', 'value': 60})
    record('option: cubes = 60 acknowledged', code == 200 and o['ok'], res=o)
    code, bad = R.post('/v0/command', {'cmd': 'fly'})
    record('control: an unknown command is 400', code == 400, code=code)
    pres = R.get('/v0/presets')
    record('presets: what the program registered', [p['id'] for p in pres['presets']] == ['start', 'burst'] and
           len(pres['options']) == 2, presets=[p['id'] for p in pres['presets']])
    r = R.wait_run(rid)
    th.join(8)
    rep = R.get('/v0/runs/%s/report' % rid, raw=True)
    record('run: done, report with CRC, parsed summary beside it', r['state'] == 'done' and b'pictures' in rep and
           r['summary']['exit'] == 0 and r['summary']['tier'] == 'mock' and r['summary']['pictures'] > 60,
           state=r['state'], pictures=r['summary']['pictures'], numbers=len(r['summary']['numbers']))
    ser = R.get('/v0/runs/%s/serial' % rid, raw=True)
    record('serial lines of the run', b'simcubes: up' in ser, lines=ser.count(b'\n'))
    types = {}
    for _, e in events:
        types[e['t']] = types.get(e['t'], 0) + 1
    rows = [e for i, e in events if e['t'] == 'row']
    ids_ok = all(i == e['n'] for i, e in events if e['t'] == 'row')
    record('live (SSE): hello, meta, rows, trace, log, run events; a row\'s event id is its picture',
           all(types.get(k) for k in ('hello', 'meta', 'row', 'trace', 'log', 'push', 'verdict', 'exec', 'grab'))
           and ids_ok and all('s' in e for _, e in events if e['t'] != 'hello'), types=types)
    su = R.get('/v0/summary?slowest=3')
    ph = {p['key']: p for p in su['phases']}
    record('summary: 4 phases with budgets, guards 0, slowest 3, tier mock, no gaps',
           set(ph) == {'draw', 'logic', 'wait', 'frame'} and ph['draw']['budget'] == 14.0 and su['missing'] == 0 and
           all(g['value'] == 0 for g in su['guards']) and len(su['slowest']) == 3 and su['tier'] == 'mock',
           draw_mean=ph['draw']['mean'], measured=su['measured'], missing=su['missing'])
    tr = R.get('/v0/trace?lane=Loads')
    lg = R.get('/v0/log?level=warn')
    record('trace by lane and log by level', tr['events'] and all(e['lane'] == 'Loads' for e in tr['events']) and
           all(x['lv'] in ('warn', 'err') for x in lg['lines']), trace=len(tr['events']), warn=len(lg['lines']))
    # replay: the session file gives the same numbers
    sess = R.get('/v0/sessions')['sessions']
    sid = su['session']
    text = R.get('/v0/sessions/%s' % sid, raw=True).decode()
    recs = [json.loads(x) for x in text.splitlines() if x.strip()]
    s2 = Session(None, 'replay', recs[0]['device'], recs[0]['tier'], 'replay')
    metas = [x for x in recs if x['t'] == 'meta']
    s2.meta.update({k: metas[-1][k] for k in ('phases', 'counters', 'lanes', 'rate')})
    for x in recs[1:]:
        if x['t'] != 'meta':
            s2.add(dict(x))
    su2 = s2.summary(3)
    same = all(abs(p['mean'] - q['mean']) < 1e-9 and p['worst'] == q['worst'] for p, q in zip(su['phases'], su2['phases']))
    record('replay: the session file replays to the same numbers', recs[0]['t'] == 'meta' and same and
           su2['pictures'] == su['pictures'] and any(x['id'] == sid for x in sess), pictures=su2['pictures'])
    # controls on push
    code, body = R.push(sha='0' * 64)
    record('control: a push whose sha256 does not match is refused (400), nothing queued', code == 400 and
           'does not match' in body['error'], code=code)
    code, body = R.push(sha='')
    record('control: a push without a sha256 is refused', code == 400, code=code)

    # 3. reset and crash -----------------------------------------------------------------------------------
    code, run = R.push(args='secs=30')
    R.wait(lambda: R.get('/v0/runs/%s' % run['id'])['state'] == 'running', 20)
    time.sleep(0.5)
    t0 = time.time()
    code, rs = R.post('/v0/command', {'cmd': 'reset', 'level': 'return'})
    r = R.wait_run(run['id'])
    # ql-fix-pc round 2: the rung is what was tried (RESET); the way back is named from the loader's own record (the
    # simulated loader reports the stub's jump after a RESET), never from the rung
    record('reset (the ladder\'s first rung) brings a running program back', r['state'] == 'reset' and
           rs.get('rung') == 'reset' and r.get('way_back') == 'stub' and rs.get('way_back') == 'stub',
           state=r['state'], rung=rs.get('rung'), way_back=r.get('way_back'), seconds=round(time.time() - t0, 2))
    code, run = R.push(args='secs=1 crash')
    r = R.wait_run(run['id'])
    record('crash: state crashed, the crash parsed from the report', r['state'] == 'crashed' and
           r.get('crash', {}).get('pc') == '8c0123a4', state=r['state'], crash=r.get('crash'))
    code, run = R.push(args='secs=12 nolive')
    R.wait(lambda: R.get('/v0/runs/%s' % run['id'])['state'] == 'running' and R.linked(), 20)
    code_g, g = R.post('/v0/stills', {'timeout_s': 3})
    code_c, c = R.post('/v0/command', {'cmd': 'pause'})
    R.post('/v0/reset')
    R.wait_run(run['id'])
    record('control: a program without the live additions: grab 504, commands 501', code_g == 504 and code_c == 501,
           grab=code_g, cmd=code_c)
    code_g2, _ = R.post('/v0/stills')
    record('control: grab with no program linked is 409', code_g2 == 409, code=code_g2)
    calls = open(os.path.join(R.dir, 'calls.log')).read().splitlines()
    record('every call logged with who made it and its level', len(calls) >= 10 and all('"who"' in x and '"level"' in x
                                                                                        for x in calls), calls=len(calls))
    # keep-alive: a POST whose body the route does not use, then a CORS preflight on the same connection (the app's
    # self-test after POST /v0/selftest, Flycast run f4). An unread body turns the preflight into a 400 without CORS.
    import http.client
    hc = http.client.HTTPConnection('127.0.0.1', int(R.base.rsplit(':', 1)[1]), timeout=10)
    hc.request('POST', '/v0/reset?x=1', body=b'{"level": "return", "pad": "' + b'x' * 300 + b'"}',
               headers={'Content-Type': 'application/json'})
    r1 = hc.getresponse(); r1.read()
    hc.request('OPTIONS', '/v0/runs/abcd1234', headers={'Origin': 'app://studio', 'Access-Control-Request-Method': 'GET',
                                                        'Access-Control-Request-Headers': 'x-contract'})
    r2 = hc.getresponse(); r2.read()
    hc.request('POST', '/v0/memory/read', body=b'{"unused": true}', headers={'Content-Type': 'application/json'})
    r3 = hc.getresponse(); r3.read()
    hc.request('GET', '/v0/status', headers={'X-Contract': '1'})
    r4 = hc.getresponse(); r4.read()
    hc.close()
    record('keep-alive: a POST body never bleeds into the next request (preflight 204 with CORS)',
           r2.status == 204 and r2.getheader('Access-Control-Allow-Origin') == '*' and r4.status == 200,
           post=r1.status, preflight=r2.status, memory=r3.status, status=r4.status)
    R.close()

    # 4. gaps and the planted delay --------------------------------------------------------------------------
    G = Rig(a.out, 'gaps', drop_rec=0.3)
    code, run = G.push(args='secs=3')
    G.wait_run(run['id'])
    su = G.get('/v0/summary')
    rows_n = su['measured']
    record('a dropped datagram shows as a gap, never as wrong data (30% of REC lost)', su['missing'] > 20 and
           su['missing'] + len([1]) > 0 and rows_n + su['missing'] >= su['pictures'] - 31, missing=su['missing'],
           pictures=su['pictures'])
    G.close()
    base = {}
    for name, slow in (('base', 0.0), ('slow2', 2.0)):
        S = Rig(a.out, 'delay-' + name, slow_ms=slow)
        code, run = S.push(args='secs=3')
        S.wait_run(run['id'])
        base[name] = {p['key']: p['mean'] for p in S.get('/v0/summary')['phases']}
        S.close()
    dd = base['slow2']['draw'] - base['base']['draw']
    record('control: a planted 2 ms in drawing reads +2 ms (within 0.5)', abs(dd - 2.0) < 0.5, delta_ms=round(dd, 3))

    # 5. permission level --------------------------------------------------------------------------------------
    L = Rig(a.out, 'level-read', level='read')
    code, body = L.push()
    code2, _ = L.post('/v0/command', {'cmd': 'pause'})
    record('control: a bridge at level read refuses push (403) and commands (403)', code == 403 and code2 == 403,
           push=code, cmd=code2)
    L.close()

    ok = all(r['ok'] for r in RESULTS)
    json.dump({'results': RESULTS, 'seconds': round(time.time() - t_all, 1), 'ok': ok},
              open(os.path.join(a.out, 'results.json'), 'w'), indent=1)
    print('%d of %d passed in %.1f s' % (sum(r['ok'] for r in RESULTS), len(RESULTS), time.time() - t_all))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
