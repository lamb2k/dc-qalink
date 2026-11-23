#!/usr/bin/env python3
"""test_safety4.py: the PC side, round 4 of the 0.2 audit (the confirmation round; lane ql-fix-pc, 2026-11-23). Linux;
Python 3.10+, standard library. Helpers shared with tests/test_safety3.py.

  python tests/test_safety4.py [--out DIR] [--pc DIR] [--controls]

Rows (pinned in PINNED, see tests/ctlrows.py):
  D1 stills: a CRC-failed grab whose sender gives up is released (502) within its deadline and the paused program
     resumed; a stream of failed captures stays bounded, one record per capture
  D2 file grant: ownership on every lookup (a cached service included); a changed link retires the old one; no
     external owner while a pushed run is active; the session listing reads through the checked descriptor
  D3 memory: an MCP field is measured before it is encoded; a session export is streamed
  D4 CLI END: an END that arrives during the loader fetch is kept; a whole report whose CRC is known bad is never success
  D5 docs and the serial window: exactly the last 20,000 lines; the contracts as the code is
  D6 the control harness itself

--controls with --pc naming df476a2's pc/ (the tree the round-4 audit read): every row with an expected discrepancy
must fail with exactly it; an error is a control FAIL."""
import argparse, json, os, shutil, socket, struct, sys, tempfile, threading, time, tracemalloc, urllib.error, \
    urllib.request, zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ctlrows   # noqa: E402
import test_safety3 as t3   # noqa: E402  (helpers: mock bridge, HELLO, END heads, fake CLI loop)

PINNED = {
    'D1.grab_exhausted': 'grab never released',
    'D1.failed_bounded': 'failed records unbounded',
    'D2.cached_owner': 'cached service kept the grant',
    'D2.link_change': 'old link kept its service',
    'D2.hello_during_run': 'external owner during run',
    'D2.session_listing': 'listed linked session',
    'D3.mcp_field_memory': 'built oversized field',
    'D3.session_stream': 'export read whole',
    'D4.cli_end_during_fetch': 'END lost during fetch',
    'D4.whole_bad_crc': 'known-bad whole accepted',
    'D5.serial_window': 'trimmed inside window',
    'D5.docs': 'docs stale',
    'D6.harness': None,
}

W, H = 32, 32
PIX = bytes(range(256)) * (W * H * 2 // 256)


def still_head(idx, req, crc):
    return struct.pack('<8I', idx, 1, W, H, 1, len(PIX), crc, req)


def send_copy(feed, cl, idx, head, bad=True):
    feed(cl.STILL_HEAD, idx, head)
    for off in range(0, len(PIX), cl.CHUNK):
        blk = PIX[off:off + cl.CHUNK]
        if bad and off == 0:
            blk = bytes([blk[0] ^ 0xFF]) + blk[1:]
        feed(off, idx, blk)
    feed(cl.STILL_END, idx, head)


# ------------------------------------------------------------------------------------------------ D1
def d1(R, ctx):
    cl, core, root, wire = ctx['cl'], ctx['core'], ctx['root'], ctx['wire']
    crc = zlib.crc32(PIX) & 0xFFFFFFFF

    def grab_exhausted():
        b = t3.mock_bridge(core, root, 'd1-g')
        RUN = 0x4242
        b.client, b.client_run = {'role': 2, 'run_id': RUN}, RUN
        b.declaration = {'phases': []}   # a program with live data: paused for the read
        cmds = []
        b.command = lambda c, who='api': cmds.append(c['cmd']) or (200, {})
        grabs = []

        def console(req):   # two corrupt copies, then the sender gives up (silence)
            feed = lambda a, idx, pl: b.tap((cl.STILL, RUN, a, idx, pl))
            for _ in range(2):
                send_copy(feed, cl, 7, still_head(7, req, crc))
                time.sleep(0.1)

        def send(typ, session, a=0, bb=0, payload=b''):
            if typ == wire.GRAB and not grabs:
                grabs.append(a)
                threading.Thread(target=console, args=(a,), daemon=True).start()
        b.loop.send = send
        out = {}

        def run():
            t0 = time.time()
            out['res'] = b.grab(timeout=8.0)
            out['s'] = time.time() - t0
        th = threading.Thread(target=run, daemon=True)
        th.start()
        th.join(20)
        hung = th.is_alive()
        if not hung:
            b.close()
        code = out.get('res', (None,))[0]
        ok = not hung and code == 502 and out['s'] <= 9.0 and cmds[:1] == ['pause'] and cmds[-1:] == ['resume']
        return ok, 'grab never released' if hung else 'released %s' % code, {
            'code': code, 'seconds': round(out.get('s', -1), 2), 'commands': cmds}
    R.row('D1.grab_exhausted', 'a CRC-failed grab whose sender gives up answers 502 within its deadline and resumes '
          'the paused program', grab_exhausted)

    def failed_bounded():
        st = core.Stills(os.path.join(root, 'd1-f'), lambda s: None)
        st.META_KEEP = 16
        lp = t3.FakeLoop()
        emits = {}
        feed = lambda a, idx, pl: st.on_still(lp, 1, a, idx, pl, 'mock',
                                              lambda j: emits.__setitem__(j['id'], emits.get(j['id'], 0) + 1))
        for i in range(100):
            head = still_head(100 + i, 0, crc)
            send_copy(feed, cl, 100 + i, head)
            send_copy(feed, cl, 100 + i, head)   # a second corrupt copy of the same capture
            for s in st.cur.values():             # then the sender gives up: quiet for a minute
                s['t0'] -= 60
                s['t_last'] = s.get('t_last', s['t0']) - 60
        if hasattr(st, 'expire'):
            st.expire()
        n, per = len(st.meta), max(emits.values() or [0])
        ok = n <= 16 and per == 1 and len(emits) >= 99
        return ok, 'failed records unbounded' if n > 16 else 'bounded' if ok else 'records per capture %d' % per, {
            'records': n, 'max_records_per_capture': per, 'captures_recorded': len(emits), 'pending': len(st.cur)}
    R.row('D1.failed_bounded', '100 failed captures (2 corrupt copies each): records bounded, one per capture',
          failed_bounded)


# ------------------------------------------------------------------------------------------------ D2
def d2(R, ctx):
    cl, core, root, sim = ctx['cl'], ctx['core'], ctx['root'], ctx['sim']
    files = os.path.join(root, 'd2-files')
    os.makedirs(os.path.join(files, 'sub'))
    open(os.path.join(files, 'top.bin'), 'wb').write(b'TOP')
    open(os.path.join(files, 'sub', 'in.bin'), 'wb').write(b'IN')

    def bridge(name):
        b = t3.mock_bridge(core, root, name, files_root=files)
        lp = t3.FakeLoop()
        b.loop.send = lp.send
        return b, lp

    def prog_hello(run):
        import types
        ns = types.SimpleNamespace(boot=2, t_boot=time.time() - 1, state=0, session=0, chunks={}, load=None,
                                   report_run=0, report_state=0, report=b'', report_exit=0, bad_crc=False, stub_step=0)
        return sim.SimConsole.hello_payload(ns, 2, run)

    def cached_owner():
        b, lp = bridge('d2-c')
        X = 0x1111
        try:
            b.client, b.client_run = {'role': 2, 'run_id': X}, X   # an external program, whole share
            b.tap((cl.FOPEN, X, 1, 0, b'top.bin'))
            first = t3.finfo_status(lp.sent, 1)
            r = core.Run('d2.bin', b'\0' * 64, '', 0, 'sub', None, 'test')   # then a pushed run with a subfolder
            b.runs[r.id] = r
            b.active = r
            b.tap((cl.FOPEN, X, 2, 0, b'top.bin'))   # the earlier program's cached service, asked again
            again = t3.finfo_status(lp.sent, 2)
        finally:
            b.active = None
            b.close()
        ok = first == 0 and again != 0
        return ok, 'cached service kept the grant' if again == 0 else 'checked', {'first': first, 'again': again}
    R.row('D2.cached_owner', 'ownership is checked on every lookup: a cached service loses its grant to the active run',
          cached_owner)

    def link_change():
        b, lp = bridge('d2-l')
        X, Y = 0x2222, 0x3333
        try:
            b.client, b.client_run = {'role': 2, 'run_id': X}, X
            b.tap((cl.FOPEN, X, 1, 0, b'top.bin'))
            had = X in b.files
            b.tap((cl.HELLO, Y, 0, 0, prog_hello(Y)))   # another program links
            kept = X in b.files
            linked = b.client_run
        finally:
            b.close()
        ok = had and not kept and linked == Y
        return ok, 'old link kept its service' if kept else 'retired', {'had': had, 'kept': kept,
                                                                        'linked': '%x' % (linked or 0)}
    R.row('D2.link_change', 'a program linking under another ID retires the previous link\'s file service', link_change)

    def hello_during_run():
        b, lp = bridge('d2-h')
        Y = 0x4444
        try:
            r = core.Run('d2h.bin', b'\0' * 64, '', 0, 'sub', None, 'test')
            b.runs[r.id] = r
            b.active = r
            b.tap((cl.HELLO, Y, 0, 0, prog_hello(Y)))   # a different, nonzero program ID while the run is active
            b.tap((cl.FOPEN, Y, 1, 0, b'top.bin'))
            y = t3.finfo_status(lp.sent, 1)
            b.tap((cl.FOPEN, r.sid, 2, 0, b'in.bin'))
            own = t3.finfo_status(lp.sent, 2)
        finally:
            b.active = None
            b.close()
        ok = y != 0 and own == 0
        return ok, 'external owner during run' if y == 0 else 'refused', {'other_id': y, 'active_run': own}
    R.row('D2.hello_during_run', 'while a pushed run is active, another program ID gets no /pc', hello_during_run)

    def session_listing():
        from bridge.api import serve
        b = t3.mock_bridge(core, root, 'd2-s')
        out = os.path.join(root, 'd2-out')
        os.makedirs(out, exist_ok=True)
        open(os.path.join(out, 'x.jsonl'), 'w').write('{"t":"meta","label":"SECRET-LABEL"}\n')
        port = t3.free_port()
        serve(b, '127.0.0.1', port, 'run')
        try:
            os.symlink(os.path.join(out, 'x.jsonl'), os.path.join(b.sessions_dir, 'evil.jsonl'))
            open(os.path.join(b.sessions_dir, 'good.jsonl'), 'w').write('{"t":"meta","label":"GOOD"}\n')
            c, body = t3.http_get(port, '/v0/sessions')
        finally:
            b.close()
        leaked = b'SECRET-LABEL' in body
        ok = c == 200 and not leaked and b'GOOD' in body
        return ok, 'listed linked session' if leaked else 'left out', {'code': c}
    R.row('D2.session_listing', 'the session listing reads each file through the checked descriptor (a link is left out)',
          session_listing)


# ------------------------------------------------------------------------------------------------ D3
def d3(R, ctx):
    core, root = ctx['core'], ctx['root']

    def mcp_field():
        from bridge import mcp_server
        sent = []

        def urlopen(r, timeout=None):
            sent.append(len(r.data or b''))
            raise urllib.error.URLError('test: no bridge')
        saved = mcp_server.urllib.request.urlopen
        mcp_server.urllib.request.urlopen = urlopen
        big, esc = 'p' * (17 << 20), '\x01' * (3 << 20)   # over the limit plainly; over it only once escaped (x6)
        peaks = []
        try:
            bc = mcp_server.BridgeClient('http://127.0.0.1:1', 'test')
            for s in (big, esc):
                tracemalloc.start()
                c, _ = bc.call('POST', '/v0/assets', {'path': s, 'name': 'x.png'})
                peaks.append(tracemalloc.get_traced_memory()[1])
                tracemalloc.stop()
        finally:
            mcp_server.urllib.request.urlopen = saved
        ok = max(peaks) < (2 << 20) and not sent
        return ok, 'built oversized field' if max(peaks) >= (16 << 20) else 'measured' if ok else 'peak %d' % max(
            peaks), {'peaks': peaks, 'sent': sent}
    R.row('D3.mcp_field_memory', 'an MCP field over the limit (17 MiB plain, 3 MiB escaped x6) is refused with no '
          'oversized copy (peak < 2 MiB)', mcp_field)

    def session_stream():
        from bridge.api import serve
        b = t3.mock_bridge(core, root, 'd3-s')
        line = ('{"t":"row","n":1,"pad":"%s"}\n' % ('x' * 1000)).encode()
        with open(os.path.join(b.sessions_dir, 'big.jsonl'), 'wb') as f:
            for _ in range((32 << 20) // len(line)):
                f.write(line)
        port = t3.free_port()
        serve(b, '127.0.0.1', port, 'run')
        got = 0
        try:
            tracemalloc.start()
            with urllib.request.urlopen('http://127.0.0.1:%d/v0/sessions/big' % port, timeout=60) as r:
                while True:
                    c = r.read(1 << 16)
                    if not c:
                        break
                    got += len(c)
            peak = tracemalloc.get_traced_memory()[1]
            tracemalloc.stop()
        finally:
            b.close()
        ok = got >= (31 << 20) and peak < (4 << 20)
        return ok, 'export read whole' if peak >= (24 << 20) else 'streamed' if ok else 'peak %d' % peak, {
            'bytes': got, 'peak': peak}
    R.row('D3.session_stream', 'a 32 MiB session export is streamed (peak < 4 MiB)', session_stream)


# ------------------------------------------------------------------------------------------------ D4
def d4(R, ctx):
    cl, root, sim = ctx['cl'], ctx['root'], ctx['sim']
    SID = 0x61616161
    rep = (b'pictures 60\n@ Pictures = 60 n\n' * 8)[:200]
    crc = zlib.crc32(rep) & 0xFFFFFFFF

    def cli_run(name, lp_ends, fetch):
        out = os.path.join(root, 'd4-' + name)
        os.makedirs(out)
        prog = os.path.join(out, 'p.bin')
        open(prog, 'wb').write(b'\x00\x09' * 64)
        a = argparse.Namespace(cmd='run', program=prog, args='', corrupt=None, loader=None, no_compress=False,
                               session=SID, deadline=0, wmax=16, no_collect=False, out=out, files=None, disc=None,
                               overlay=None, whole=False, reset_after=None, reset_wait=20.0, wait=5.0, tail=False)
        hb = t3.hello(cl, sim, report_run=SID, report_state=2, report=rep, boot=3)
        lp = t3.FakeCliLoop(t3.hello(cl, sim), hb, lp_ends, 'wait', None)
        lp.fetch = lambda h, o: fetch(lp, h, o)
        rc = cl.session_main(a, lp, lambda s: None)
        return rc, json.load(open(os.path.join(out, 'run-%08x.json' % SID)))

    def during_fetch():
        ends = [(cl.END, SID, 0, len(rep), rep), (cl.END, SID, 0xFFFFFFFF, 200, t3.end_head(2, 200, crc, 0, 200))]

        def fetch(lp, h, o):   # the END arrives while the loader's report is fetched; then the fetch fails
            if lp.on_other:
                for m in ends:
                    lp.on_other(m)
            raise RuntimeError('report incomplete after 30 requests')
        rc, res = cli_run('fetch', None, fetch)
        # and the real fetch: what it does not consume (an END) goes to on_other
        t = t3.QueueTransport()
        t.q = [cl.frame(cl.END, SID, 0xFFFFFFFF, 200, t3.end_head(2, 200, crc, 0, 200)),
               cl.frame(cl.REPORT, SID, 0, 0, rep)]
        lp = cl.Loop(t, log=lambda s: None)
        seen = []
        lp.on_other = seen.append
        lp.wait = lambda *a, **k: None
        lp.fetch_report({'report_run': SID, 'report_len': len(rep), 'report_crc': crc, 'report_state': 2,
                         'report_exit': 0}, None)
        fwd = any(m[0] == cl.END for m in seen)
        ok = rc == 0 and bool(res.get('end')) and fwd
        return ok, 'END lost during fetch' if not res.get('end') else 'kept' if ok else 'forwarded %s' % fwd, {
            'rc': rc, 'evidence': res.get('evidence'), 'real_fetch_forwarded_end': fwd}
    R.row('D4.cli_end_during_fetch', 'CLI: an END that arrives during the loader fetch is kept and decides the result',
          during_fetch)

    def whole_bad():
        ends = [(cl.END, SID, 0, len(rep), rep),
                (cl.END, SID, 0xFFFFFFFF, 200, t3.end_head(2, 200, crc ^ 1, 0, 200))]   # whole, CRC known wrong

        def fetch(lp, h, o):
            raise RuntimeError('console: no report')
        rc, res = cli_run('whole', ends, fetch)
        ev = (res.get('evidence') or {}).get('end')
        ok = rc != 0 and ev == 'whole crc bad'
        return ok, 'known-bad whole accepted' if rc == 0 else 'refused' if ok else 'evidence %s' % ev, {
            'rc': rc, 'evidence': res.get('evidence')}
    R.row('D4.whole_bad_crc', 'CLI: a whole END report whose CRC is known bad is never a successful result', whole_bad)


# ------------------------------------------------------------------------------------------------ D5
def d5(R, ctx):
    cl, core, root, pcdir = ctx['cl'], ctx['core'], ctx['root'], ctx['pcdir']

    def serial_window():
        b = t3.mock_bridge(core, root, 'd5-s')
        try:
            r = core.Run('s.bin', b'\0' * 64, '', 0, None, None, 'test')
            b.runs[r.id] = r
            for i in range(20001):
                b.tap((cl.LINE, r.sid, i, 0, b'line %d\n' % i))
            n, first = len(r.serial), r.serial[0][0]
        finally:
            b.close()
        ok = n == 20000 and first == 1
        return ok, 'trimmed inside window' if n < 20000 else 'window' if ok else 'held %d' % n, {
            'held': n, 'first_seq': first}
    R.row('D5.serial_window', 'exactly the last 20,000 serial lines are kept (20,001 sent)', serial_window)

    def docs():
        d = os.path.join(pcdir, '..', 'docs')
        api = open(os.path.join(d, 'BRIDGE_API.md'), encoding='utf-8').read()
        mcp = open(os.path.join(d, 'MCP.md'), encoding='utf-8').read()
        rel = open(os.path.join(d, 'RELEASE_0.2.md'), encoding='utf-8').read()
        gate = open(os.path.join(pcdir, '..', 'tools', 'gate', 'gate.sh'), encoding='utf-8').read()
        want = [(api, 'exactly the last 20,000 lines', 'BRIDGE_API: serial window'),
                (api, 'so the count never passes 512', 'BRIDGE_API: run bound'),
                (api, '**502** when the picture failed its CRC', 'BRIDGE_API: grab 502 contract'),
                (api, 'an absolute deadline', 'BRIDGE_API: grab deadline'),
                (api, 'While a pushed run is active no other program', 'BRIDGE_API: file grant during a run'),
                (api, 'checked on every request', 'BRIDGE_API: grant on every lookup'),
                (api, 'the session listing and session export', 'BRIDGE_API: listing/export descriptors'),
                (mcp, 'measured before its JSON is built', 'MCP: encoding bound'),
                (gate, 'test_safety4', 'gate.sh: test_safety4 rows')]
        miss = [w for text, s, w in want if s not in text]
        n4, c4 = len(PINNED), len([v for v in PINNED.values() if v is not None])
        if '`test_safety4` (%d pinned rows, %d controls against df476a2' % (n4, c4) not in rel:
            miss.append('RELEASE: test_safety4 counts (%d rows, %d controls)' % (n4, c4))
        if 'Windows' not in rel.split('### Deferred', 1)[-1]:
            miss.append('RELEASE: the Windows directory window deferred')
        return not miss, 'docs stale' if miss else 'docs true', {'missing': miss}
    R.row('D5.docs', 'the serial, run-bound, grab, file-grant and MCP contracts and the suite counts as published', docs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out')
    ap.add_argument('--pc', default=os.path.join(HERE, '..', 'pc'), help='the pc/ tree to test')
    ap.add_argument('--controls', action='store_true', help='the control: pinned rows must fail as named on this --pc')
    a = ap.parse_args()
    pcdir = os.path.abspath(a.pc)
    sys.path.insert(0, pcdir)
    import dcql_loop as cl
    from bridge import core, wire
    import simconsole as sim
    sys.path.insert(0, pcdir)   # simconsole puts this checkout's pc/ first: the tree under test goes first again
    root = tempfile.mkdtemp(prefix='ql-safety4-')
    R = ctlrows.Rows(PINNED, a.controls)
    ctx = {'cl': cl, 'core': core, 'wire': wire, 'pcdir': pcdir, 'root': root, 'sim': sim}
    try:
        for part in (d1, d2, d3, d4, d5):
            try:
                part(R, ctx)
            except Exception as e:   # a setup error: its rows go missing, and a missing pinned row is a FAIL
                print('%s: setup error %s: %s' % (part.__name__, type(e).__name__, e), flush=True)
        R.row('D6.harness', 'the control harness: an error, a pass, a wrong discrepancy or a missing row fails a control',
              lambda: (ctlrows.self_check(), 'harness', {}))
    finally:
        shutil.rmtree(root, ignore_errors=True)
    rc = R.verdict(pcdir if a.controls else 'this tree', a.out)
    sys.stdout.flush()
    os._exit(rc)   # a hung grab thread on an old tree must not hold the process


if __name__ == '__main__':
    main()
