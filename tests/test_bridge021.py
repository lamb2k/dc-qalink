#!/usr/bin/env python3
"""test_bridge021.py: the 0.2.1 bridge fixes from the owner's first real-console session with v0.2.0 (2026-11-23).
Linux; Python 3.10+, standard library.

  python tests/test_bridge021.py [--out DIR] [--pc DIR] [--controls]

Rows (pinned in PINNED, see tests/ctlrows.py):
  E1 hand-played runs: a run with no deadline is not capped at 900 s while the console streams; any traffic from the
     console counts as heard (disc requests alone keep a 30 s room load running); lost only after RUN_SILENT_S (60 s)
     of silence
  E2 printed lines: GET /v0/log (MCP read_log) includes the program's lines as level "serial"; level=serial filters
  E3 docs: a real console needs --allow-lan, and the Windows Firewall rule for the bridge's python.exe
  E4 the control harness itself

--controls with --pc naming v0.2.0's pc/ (88d9379): each row with an expected discrepancy must fail with exactly it,
but E1.disc_only_30s, whose control is fb6b344 (the 8 s rule: --control-rev fb6b344)."""
import argparse, os, shutil, socket, sys, tempfile, time, types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ctlrows   # noqa: E402

PINNED = {
    'E1.no_deadline_cap': 'lost at the 900 s cap while streaming',
    'E1.silence_lost': None,
    'E1.disc_only_30s': 'lost while disc requests flowed',
    'E2.log_lines': 'serial lines missing',
    'E3.docs': 'docs stale',
    'E4.harness': None,
}


def free_udp():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_for(f, secs):
    end = time.time() + secs
    while time.time() < end:
        if f():
            return True
        time.sleep(0.05)
    return f()


class Clock:
    """core's `time` with a movable wall clock (time.time() + offset); monotonic and sleep are the real ones."""
    def __init__(self, real):
        self.real, self.offset = real, 0.0

    def __getattr__(self, k):
        return getattr(self.real, k)

    def time(self):
        return self.real.time() + self.offset


def e1(R, ctx):
    core, root = ctx['core'], ctx['root']
    from simconsole import SimConsole

    def bridge(name, **kw):
        udp, lport = free_udp(), free_udp()
        b = core.Bridge(os.path.join(root, name), ('127.0.0.1', udp), '127.0.0.1:%d' % lport, 'mock',
                        autoconnect=False, **kw)
        con = SimConsole(('127.0.0.1', udp), lport)
        wait_for(lambda: b.loop.hello is not None, 8)
        return b, con

    def no_cap():
        b, con = bridge('e1-cap')
        clock = Clock(core.time)
        core.time = clock
        try:
            r = b.submit('hand.bin', b'\x00\x09' * 4000, 'report secs=6', 0)   # no deadline: hand-played
            wait_for(lambda: r.state == 'running', 20)
            time.sleep(1.0)
            clock.offset += 1000.0   # 1,000 s of play pass on the bridge's wall clock; the console keeps streaming
            r.done.wait(40)
        finally:
            core.time = clock.real
            con.close()
            b.close()
        ok = r.state == 'done'
        return ok, 'lost at the 900 s cap while streaming' if r.state == 'lost' else 'ran to its end' if ok else \
            'state %s' % r.state, {'state': r.state, 'error': r.error}
    R.row('E1.no_deadline_cap', 'a run with no deadline still streaming after 1,000 s is not lost (ends done)', no_cap)

    def silence():
        b, con = bridge('e1-sil')
        b.RUN_SILENT_S = 5.0   # the 60 s rule, shortened for the test (the mechanism, not the figure)
        try:
            r = b.submit('quiet.bin', b'\x00\x09' * 4000, 'report secs=120', 0)
            wait_for(lambda: r.state == 'running' and con.prog is not None, 20)
            time.sleep(1.0)
            con.send = lambda *a, **k: None   # the console goes silent: no rows, lines, beacons
            t0 = time.time()
            r.done.wait(30)
            secs = time.time() - t0
        finally:
            con.close()
            b.close()
        ok = r.state == 'lost' and 'silent for 5 s' in (r.error or '') and secs < 12
        return ok, 'lost after silence' if ok else 'state %s' % r.state, {'state': r.state, 'error': r.error,
                                                                          'seconds': round(secs, 1)}
    R.row('E1.silence_lost', 'positive: a console silent past RUN_SILENT_S (set to 5 s here) makes a no-deadline '
          'run lost', silence)

    def disc_only():
        import struct, threading
        import dcql_loop as cl
        import dcql_disc
        ov = os.path.join(root, 'e1-ov')
        os.makedirs(ov, exist_ok=True)
        open(os.path.join(ov, 'ROOM.BIN'), 'wb').write(b'r' * 4096)
        b, con = bridge('e1-disc', overlay=ov)
        stop = []
        try:
            r = b.submit('room.bin', b'\x00\x09' * 4000, 'report secs=120', 0)
            wait_for(lambda: r.state == 'running' and con.prog is not None, 20)
            time.sleep(1.0)
            real = con.send
            con.send = lambda *a, **k: None   # the program goes quiet but for its disc requests (a room load)

            def disc():
                req = 1
                while not stop:
                    real(dcql_disc.DOPEN, r.sid, req, 0, struct.pack('<I', 0) + b'ROOM.BIN')
                    req += 1
                    time.sleep(12.0)   # one disc request every 12 s (a slow room load: one request took 12.45 s)
            threading.Thread(target=disc, daemon=True).start()
            time.sleep(30.0)
            state = r.state
        finally:
            stop.append(1)
            con.close()
            b.close()
        ok = state == 'running'
        return ok, 'lost while disc requests flowed' if state == 'lost' else 'running' if ok else 'state %s' % state, {
            'state_at_30s': state, 'error': r.error}
    R.row('E1.disc_only_30s', 'a no-deadline run silent for 30 s but for a disc request every 12 s stays running', disc_only)


def e2(R, ctx):
    live, root = ctx['live'], ctx['root']

    def log_lines():
        os.makedirs(os.path.join(root, 'e2'), exist_ok=True)
        s = live.Session(os.path.join(root, 'e2'), 'e2-1', {'name': 'dc'}, 'mock', 'e2')
        s.add({'t': 'row', 'n': 5, 'ph': {}})
        s.add({'t': 'log', 'n': 5, 'lv': 'info', 'ph': 'play', 'msg': 'a log'})
        s.add({'t': 'line', 'run': '00000001', 'seq': 0, 'text': 'printed hello'})
        s.add({'t': 'row', 'n': 9, 'ph': {}})
        s.add({'t': 'line', 'run': '00000001', 'seq': 1, 'text': 'printed again'})
        every = s.query_log()['lines']
        only = s.query_log(level='serial')['lines']
        info = s.query_log(level='info')['lines']
        s.close()
        ser = [x for x in every if x['lv'] == 'serial']
        ok = [(x['n'], x['msg']) for x in ser] == [(5, 'printed hello'), (9, 'printed again')] and \
            len(only) == 2 and all(x['lv'] == 'serial' for x in only) and [x['msg'] for x in info] == ['a log']
        return ok, 'serial lines missing' if not ser else 'included' if ok else 'wrong lines', {
            'all': every, 'serial': only}
    R.row('E2.log_lines', 'GET /v0/log includes printed lines as level "serial" at the latest picture; level=serial '
          'filters', log_lines)


def e3(R, ctx):
    top = os.path.join(ctx['pcdir'], '..')

    def docs():
        rel = open(os.path.join(top, 'docs', 'RELEASE_0.2.md'), encoding='utf-8').read()
        rp = os.path.join(top, 'README.md')   # (the gate takes an old tree as pc, tools, docs only)
        rd = open(rp, encoding='utf-8').read() if os.path.isfile(rp) else ''
        api = open(os.path.join(top, 'docs', 'BRIDGE_API.md'), encoding='utf-8').read()
        chk = rel.split('console checklist', 1)[-1]
        miss = [w for t, s, w in (
            (chk, 'qalink_bridge.py --allow-lan', 'RELEASE checklist: --allow-lan'),
            (chk, 'Windows Firewall', 'RELEASE checklist: firewall rule'),
            (rd, 'qalink_bridge.py --allow-lan', 'README: --allow-lan for a console'),
            (rd, 'Windows Firewall', 'README: firewall rule'),
            (api, 'has no time cap', 'BRIDGE_API: no-deadline runs'),
            (api, '`level=serial`', 'BRIDGE_API: serial lines in /v0/log')) if s not in t]
        return not miss, 'docs stale' if miss else 'docs true', {'missing': miss}
    R.row('E3.docs', 'a real console needs --allow-lan and the firewall rule (checklist, README); API contracts',
          docs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out')
    ap.add_argument('--pc', default=os.path.join(HERE, '..', 'pc'))
    ap.add_argument('--controls', action='store_true')
    ap.add_argument('--control-rev', choices=('88d9379', 'fb6b344'), default='88d9379')
    a = ap.parse_args()
    pcdir = os.path.abspath(a.pc)
    sys.path.insert(0, pcdir)
    from bridge import core, live
    import simconsole   # noqa: F401
    sys.path.insert(0, pcdir)
    root = tempfile.mkdtemp(prefix='ql-021-')
    if a.controls and a.control_rev == 'fb6b344':   # the 8 s rule: only the disc row's control runs there
        pinned = {k: None for k in PINNED}
        pinned['E1.disc_only_30s'] = PINNED['E1.disc_only_30s']
    else:
        pinned = dict(PINNED, **{'E1.disc_only_30s': None})   # v0.2.0 waited 900 s: the 8 s rule is its control
    R = ctlrows.Rows(pinned, a.controls)
    ctx = {'core': core, 'live': live, 'pcdir': pcdir, 'root': root}
    try:
        for part in (e1, e2, e3):
            try:
                part(R, ctx)
            except Exception as e:
                print('%s: setup error %s: %s' % (part.__name__, type(e).__name__, e), flush=True)
        R.row('E4.harness', 'the control harness: an error, a pass, a wrong discrepancy or a missing row fails a control',
              lambda: (ctlrows.self_check(), 'harness', {}))
    finally:
        shutil.rmtree(root, ignore_errors=True)
    rc = R.verdict(pcdir if a.controls else 'this tree', a.out)
    sys.stdout.flush()
    os._exit(rc)


if __name__ == '__main__':
    main()
