#!/usr/bin/env python3
"""test_safety3.py: the PC side's boundaries, round 3 of the 0.2 audit (lane ql-fix-pc, 2026-11-23). Linux (symbolic
links, /proc); Python 3.10+, standard library.

  python tests/test_safety3.py [--out DIR] [--pc DIR] [--controls]

Rows (pinned in PINNED, see tests/ctlrows.py), one group per blocker of the round-3 audit:
  C1 files: a run that is not the active run or the linked program is refused /pc (never the whole share); a served
     folder is anchored once (replacing it does not move the grant); model dependencies, MCP build reads and session
     export open first and check the opened descriptor; no descriptor path means refused
  C2 ELF conversion: overlapping segments cannot copy past the allowed span
  C3 admission and records: parallel submissions reserve under one lock; serial lines, in-memory sessions and the
     disc log's names are bounded; an MCP request is bounded while it is built
  C4 results: the CLI's loader report never overwrites the END's file before its CRC checks; a complete tail with no
     whole CRC is a valid CLI result; an END sent while EXEC is awaited is kept; the bridge takes an END that arrives
     during the loader fetch; a loader report collected late is attached to its own run
  C5 stills: a CRC mismatch is never a full ACK or a receipt; a fresh copy is asked for and then held
  C6 the published contract: run history bound after insertion, RESETACK not a rung, an inferred full restart named
     so, the docs saying what the code and gate do
  C7 the control harness itself

--controls with --pc naming ac45161's pc/ (the tree the round-3 audit read): every row with an expected discrepancy
must fail with exactly it; an error is a control FAIL."""
import argparse, json, os, shutil, socket, struct, sys, tempfile, threading, time, tracemalloc, types, urllib.error, \
    urllib.request, zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ctlrows   # noqa: E402

# row id -> the discrepancy ac45161 must show (None: a new-tree-only row)
PINNED = {
    'C1.unknown_run': 'whole share to unknown run',
    'C1.root_swap': 'followed replaced root',
    'C1.dep_swap': 'read swapped file',
    'C1.mcp_swap': 'read swapped file',
    'C1.session_link': 'served linked session',
    'C1.no_fd_path': None,
    'C2.elf_overlap': 'copied past span',
    'C3.parallel_admission': 'admitted past cap',
    'C3.serial_bounded': 'serial unbounded',
    'C3.sessions_bounded': 'sessions unbounded',
    'C3.disc_log_bounded': 'log names unbounded',
    'C3.mcp_request': 'serialized oversize',
    'C4.cli_report_file': 'END file overwritten',
    'C4.cli_complete_tail': 'complete tail refused',
    'C4.cli_end_before_exec': 'END lost before EXEC',
    'C4.bridge_late_end': 'bad loader report published',
    'C4.late_report_attached': 'late report dropped',
    'C5.bridge_crc': 'full ack on bad crc',
    'C5.cli_crc': 'full ack on bad crc',
    'C6.runs_bound': 'over the bound',
    'C6.resetack': 'recorded as stub rung',
    'C6.way_back_inferred': 'asserted full restart',
    'C6.docs': 'docs stale',
    'C7.harness': None,
}


def free_port(kind=socket.SOCK_STREAM):
    s = socket.socket(socket.AF_INET, kind)
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


class FakeLoop:
    def __init__(self):
        self.sent = []

    def send(self, *a):
        self.sent.append(a)


def finfo_status(sent, req):
    for x in reversed(sent):
        if x[2] == req:
            return struct.unpack_from('<i', x[4], 0)[0]
    return None


def mock_bridge(core, root, name, **kw):
    return core.Bridge(os.path.join(root, name), ('127.0.0.1', 0), '127.0.0.1:1', 'mock', autoconnect=False, **kw)


def hello(cl, sim, **kw):
    """A loader HELLO as the simulated console sends it (tests/simconsole.py), parsed by this tree's dcql_loop."""
    ns = types.SimpleNamespace(boot=2, t_boot=time.time() - 1, state=0, session=0, chunks={}, load=None, report_run=0,
                               report_state=0, report=b'', report_exit=0, bad_crc=False, stub_step=0)
    for k, v in kw.items():
        setattr(ns, k, v)
    h = cl.parse_hello(sim.SimConsole.hello_payload(ns, 1))
    for k, v in (('loader_sha8', 'sim'), ('stub_bytes', 0), ('net', 'adapter'), ('build', 'sim')):
        h.setdefault(k, v)
    return h


def end_head(state, rep_len, crc, sent_off, sent_len):
    return struct.pack('<IiIIIIIIIIIIIIII', state, 0, 0, 0, 0, 0, rep_len, crc, sent_off, sent_len, 0, 1000, 0, 0,
                       200, 1)


# ------------------------------------------------------------------------------------------------ C1
def c1(R, ctx):
    cl, core, root = ctx['cl'], ctx['core'], ctx['root']
    files = os.path.join(root, 'c1-files')
    os.makedirs(os.path.join(files, 'sub'))
    open(os.path.join(files, 'top.bin'), 'wb').write(b'TOP')
    open(os.path.join(files, 'sub', 'in.bin'), 'wb').write(b'IN')
    outside = os.path.join(root, 'c1-outside')
    os.makedirs(outside)
    open(os.path.join(outside, 'secret.bin'), 'wb').write(b'SECRET')

    def unknown_run():
        b = mock_bridge(core, root, 'c1-u', files_root=files)
        lp = FakeLoop()
        b.loop.send = lp.send
        try:
            r = core.Run('c1.bin', b'\0' * 64, '', 0, 'sub', None, 'test')
            b.runs[r.id] = r
            b.active = r
            b.tap((cl.FOPEN, 0x7777, 1, 0, b'top.bin'))      # a run ID that is neither the active run nor linked
            b.tap((cl.FOPEN, r.sid, 2, 0, b'in.bin'))         # the active run, inside its subfolder
            b.tap((cl.FOPEN, r.sid, 3, 0, b'top.bin'))        # the active run, outside its subfolder
            u, a_in, a_top = finfo_status(lp.sent, 1), finfo_status(lp.sent, 2), finfo_status(lp.sent, 3)
            kept = len(b.files)
        finally:
            b.active = None
            b.close()
        ok = u != 0 and a_in == 0 and a_top != 0 and kept <= 1
        return ok, 'whole share to unknown run' if u == 0 else 'refused', {'unknown': u, 'active_in': a_in,
                                                                          'active_top': a_top, 'services': kept}
    R.row('C1.unknown_run', 'a /pc request from a run that is not active or linked is refused, never the whole share',
          unknown_run)

    def root_swap():
        sub2 = os.path.join(files, 'sub2')
        os.makedirs(sub2)
        open(os.path.join(sub2, 'in.bin'), 'wb').write(b'IN2')
        fs = cl.FileService(sub2, None)
        lp = FakeLoop()
        fs.on_open(lp, 1, 1, b'in.bin')
        first = finfo_status(lp.sent, 1)
        os.rename(sub2, sub2 + '.old')            # the served folder replaced by a link to another folder
        os.symlink(outside, sub2)
        fs.on_open(lp, 1, 2, b'secret.bin')
        fs.on_open(lp, 1, 3, b'in.bin')
        swapped, again = finfo_status(lp.sent, 2), finfo_status(lp.sent, 3)
        fs.close()
        ok = first == 0 and swapped != 0 and again == 0
        return ok, 'followed replaced root' if swapped == 0 else 'anchored', {'first': first, 'swapped': swapped,
                                                                              'anchored_again': again}
    R.row('C1.root_swap', 'a served folder replaced after its service started never becomes the new authority',
          root_swap)

    def swap_hook(qlsafe, target):
        """The race made certain: the file is replaced by a link to an outside file between the check and the open
        (the old code checked a name, then opened it again; the new code opens first, then checks the descriptor)."""
        done = []

        def swap():
            if not done:
                done.append(1)
                os.unlink(target)
                os.symlink(os.path.join(outside, 'secret.bin'), target)
        saved = {}
        oi = qlsafe.inside

        def inside(*a, **k):
            r = oi(*a, **k)
            swap()
            return r
        saved['inside'] = oi
        qlsafe.inside = inside
        if hasattr(qlsafe, '_open_ro'):
            oo = qlsafe._open_ro
            saved['_open_ro'] = oo

            def open_ro(*a, **k):
                swap()
                return oo(*a, **k)
            qlsafe._open_ro = open_ro
        return lambda: [setattr(qlsafe, k, v) for k, v in saved.items()]

    def dep_swap():
        import qlsafe
        from assets import model
        mdir = os.path.join(root, 'c1-model')
        os.makedirs(mdir)
        dep = os.path.join(mdir, 'tex.bin')
        open(dep, 'wb').write(b'TEXTURE')
        undo = swap_hook(qlsafe, dep)
        try:
            got = model.folder_resolver(mdir, mdir)('tex.bin')
        finally:
            undo()
        leaked = got == b'SECRET'
        return not leaked and got in (None, b'TEXTURE'), 'read swapped file' if leaked else 'refused', {
            'got': got}
    R.row('C1.dep_swap', 'a model dependency is read through the descriptor that was checked (a swapped file '
          'is refused)', dep_swap)

    def mcp_swap():
        import qlsafe
        from bridge import mcp_server
        mdir = os.path.join(root, 'c1-mcp')
        os.makedirs(mdir)
        prog = os.path.join(mdir, 'prog.bin')
        open(prog, 'wb').write(b'PROGRAM')
        bc = mcp_server.BridgeClient('http://127.0.0.1:1', 'test', root=mdir)
        sent = []

        def call(method, path, body=None, ctype=None, **k):
            sent.append(body)
            return 202, {'id': 'x'}
        bc.call = call
        undo = swap_hook(qlsafe, prog)
        try:
            code, _ = bc.push({'path': 'prog.bin'})
        finally:
            undo()
        leaked = any(isinstance(x, (bytes, bytearray)) and b'SECRET' in x or
                     isinstance(x, dict) and 'SECRET' in json.dumps(x, default=str) for x in sent)
        return not leaked, 'read swapped file' if leaked else 'refused', {'code': code, 'sent': len(sent)}
    R.row('C1.mcp_swap', 'an MCP build read is the checked descriptor (a swapped file is refused)', mcp_swap)

    def session_link():
        from bridge.api import serve
        b = mock_bridge(core, root, 'c1-s')
        port = free_port()
        serve(b, '127.0.0.1', port, 'run')
        try:
            os.symlink(os.path.join(outside, 'secret.bin'), os.path.join(b.sessions_dir, 'evil.jsonl'))
            open(os.path.join(b.sessions_dir, 'good.jsonl'), 'w').write('{"t":"meta"}\n')
            c1_, body1 = http_get(port, '/v0/sessions/evil')
            c2_, body2 = http_get(port, '/v0/sessions/good')
        finally:
            b.close()
        leaked = c1_ == 200 and b'SECRET' in body1
        ok = not leaked and c2_ == 200 and b'meta' in body2
        return ok, 'served linked session' if leaked else 'refused', {'evil': c1_, 'good': c2_}
    R.row('C1.session_link', 'session export opens and checks the file (a linked session is refused)', session_link)

    def no_fd_path():
        import qlsafe
        saved = qlsafe._fd_path
        qlsafe._fd_path = lambda fd: None   # a system that cannot say what an open file is
        try:
            try:
                qlsafe.open_inside(files, os.path.join(files, 'top.bin')).close()
                refused = False
            except qlsafe.Refused:
                refused = True
        finally:
            qlsafe._fd_path = saved
        return refused, 'refused' if refused else 'trusted a second name lookup', {}
    R.row('C1.no_fd_path', 'positive: with no descriptor path the open is refused (never a second name lookup)',
          no_fd_path)


def http_get(port, path):
    try:
        with urllib.request.urlopen('http://127.0.0.1:%d%s' % (port, path), timeout=10) as f:
            return f.status, f.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# ------------------------------------------------------------------------------------------------ C2
def c2(R, ctx):
    cl = ctx['cl']

    def elf(segs, payload):
        n = len(segs)
        hdr = bytearray(52)
        hdr[:6] = b'\x7fELF\x01\x01'
        struct.pack_into('<I', hdr, 28, 52)
        struct.pack_into('<HH', hdr, 42, 32, n)
        off0 = 52 + 32 * n
        ph = b''.join(struct.pack('<8I', 1, off0 + o, va, 0, fs, fs, 5, 4) for va, o, fs in segs)
        return bytes(hdr) + ph + payload

    def overlap():
        seg = 64 << 10
        bad = elf([(0x8C010000, 0, seg)] * 400, b'\x11' * seg)   # 400 x 64 KiB, all the same bytes: 25 MiB copied
        good = elf([(0x8C010000, 0, 8), (0x8C010008, 8, 8)], b'ABCDEFGH12345678')
        g = cl.elf_image(good)
        tracemalloc.start()
        try:
            cl.elf_image(bad)
            refused = False
        except ValueError:
            refused = True
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        ok = refused and peak < (4 << 20) and g == (0x8C010000, b'ABCDEFGH12345678')
        return ok, 'refused' if refused else 'copied past span', {'peak_bytes': peak, 'input_bytes': len(bad)}
    R.row('C2.elf_overlap', 'overlapping ELF segments cannot copy more than the allowed span (400 x 64 KiB)', overlap)


# ------------------------------------------------------------------------------------------------ C3
def c3(R, ctx):
    cl, core, dcql_disc, root = ctx['cl'], ctx['core'], ctx['dcql_disc'], ctx['root']

    def parallel():
        b = mock_bridge(core, root, 'c3-p')
        real = b.make_build

        def slow(*a, **k):
            time.sleep(0.15)   # the window between admission and queueing, made wide
            return real(*a, **k)
        b.make_build = slow
        ok_n, bar = [0], threading.Barrier(24)
        lock = threading.Lock()

        def one(i):
            bar.wait()
            try:
                b.submit('p%d.bin' % i, b'\0' * 1024, '', 5)
                with lock:
                    ok_n[0] += 1
            except ValueError:
                pass
        th = [threading.Thread(target=one, args=(i,)) for i in range(24)]
        try:
            [t.start() for t in th]
            [t.join(30) for t in th]
            accepted = ok_n[0]
            b.queue.queue.clear()

            def boom(*a, **k):
                raise OSError('test: the build record failed')
            b.make_build = boom
            for i in range(20):   # failures release what they reserved
                try:
                    b.submit('f%d.bin' % i, b'\0' * 1024, '', 5)
                except (OSError, ValueError):
                    pass
            b.make_build = real
            try:
                b.submit('after.bin', b'\0' * 1024, '', 5)
                after = True
            except ValueError:
                after = False
        finally:
            b.queue.queue.clear()
            b.close()
        ok = accepted <= 9 and after
        return ok, 'admitted past cap' if accepted > 9 else 'reserved', {'accepted_of_24': accepted,
                                                                         'accepted_after_20_failures': after}
    R.row('C3.parallel_admission', '24 parallel submissions are admitted against one reserved count (at most 8 + '
          'the running one); failures release their place', parallel)

    def serial():
        b = mock_bridge(core, root, 'c3-s')
        try:
            r = core.Run('s.bin', b'\0' * 64, '', 0, None, None, 'test')
            b.runs[r.id] = r
            for i in range(25000):
                b.tap((cl.LINE, r.sid, i, 0, b'line %d\n' % i))
            n = len(r.serial)
        finally:
            b.close()
        return n <= 20000, 'serial unbounded' if n >= 25000 else 'bounded', {'held': n, 'sent': 25000}
    R.row('C3.serial_bounded', 'a run\'s serial lines held in memory are bounded (25000 sent)', serial)

    def sessions():
        b = mock_bridge(core, root, 'c3-ss')
        try:
            for i in range(40):
                b.new_session(0x100 + i, {'name': 's%d' % i})
            n = len(b.sessions)
            files = len(os.listdir(b.sessions_dir))
        finally:
            b.close()
        return n <= 32 and files >= 40, 'sessions unbounded' if n >= 40 else 'bounded', {'in_memory': n,
                                                                                         'files': files}
    R.row('C3.sessions_bounded', 'sessions held in memory are bounded (40 made; their files stay)', sessions)

    def disc_log():
        ov = os.path.join(root, 'c3-ov')
        os.makedirs(ov, exist_ok=True)
        ds = dcql_disc.DiscService(None, ov)
        lp = FakeLoop()
        for fd in range(1000):
            pl = struct.pack('<BBHI', dcql_disc.DL_OPEN, 0, fd, 10) + b'N' * 1000 + b'\0'
            ds.handle(lp, dcql_disc.DLOG, 9, 0, 0, pl)
        n = len(ds.fds)
        longest = max((len(v) for v in ds.fds.values()), default=0)
        return n <= 256 and longest <= 256, 'log names unbounded' if n >= 1000 or longest > 256 else 'bounded', {
            'names': n, 'longest': longest}
    R.row('C3.disc_log_bounded', 'the disc log\'s open names are bounded in count and length (1000 x 1000 bytes)',
          disc_log)

    def mcp_request():
        from bridge import mcp_server
        sizes = []

        def urlopen(r, timeout=None):
            sizes.append(len(r.data or b''))
            raise urllib.error.URLError('test: no bridge')
        saved = mcp_server.urllib.request.urlopen
        mcp_server.urllib.request.urlopen = urlopen
        try:
            bc = mcp_server.BridgeClient('http://127.0.0.1:1', 'test')
            res, err, _ = mcp_server.handle(bc, 'upload_asset', {'path': 'p' * (17 << 20), 'name': 'x.png'})
        finally:
            mcp_server.urllib.request.urlopen = saved
        big = [n for n in sizes if n > (16 << 20)]
        return not big, 'serialized oversize' if big else 'refused', {'sent_sizes': sizes, 'error': (err or '')[:80]}
    R.row('C3.mcp_request', 'an MCP request over the body limit (a 17 MiB path) is refused while it is built',
          mcp_request)


# ------------------------------------------------------------------------------------------------ C4
class QueueTransport:
    def __init__(self):
        self.q = []
        self.sent = []

    def send(self, d):
        self.sent.append(d)

    def recv(self, timeout):
        if self.q:
            return self.q.pop(0)
        time.sleep(min(timeout, 0.05))
        return None


class FakeCliLoop:
    """What session_main and collect use of a Loop: the END is delivered while RUN is answered ('run') or while the
    loader is awaited ('wait'), through on_other as the real Loop does."""

    def __init__(self, h0, hb, ends, when, fetch):
        self.hello, self.hb, self.ends, self.when, self.fetch = h0, hb, ends, when, fetch
        self.on_other, self.client_hello, self.ran_back = None, None, None
        self.stats, self.wmax, self.sent = {}, 16, []

    def send(self, *a):
        self.sent.append(a)

    def deliver(self):
        if self.on_other and self.ends:
            for m in self.ends:
                self.on_other(m)
            self.ends = None

    def wait_hello(self, t, pred=None):
        return self.hello

    def transfer(self, img, session, args, deadline):
        return {'verdict': 0, 'verdict_text': 'ok', 'seconds': 0.01, 'goodput_Bps': 1, 'resent': 0}

    def run_verified(self, session):
        if self.when == 'run':
            self.deliver()
        return 'exec'

    def wait(self, pred, timeout, every=None, poke=None):
        if self.when == 'wait':
            self.deliver()
        self.hello = self.hb
        return self.hb

    def fetch_report(self, h, out):
        return self.fetch(h, out)


def c4(R, ctx):
    cl, core, root, sim = ctx['cl'], ctx['core'], ctx['root'], ctx['sim']
    SID = 0x51515151
    rep = (b'pictures 60\n@ Pictures = 60 n\n' * 8)[:200]
    crc = zlib.crc32(rep) & 0xFFFFFFFF

    def report_file():
        out = os.path.join(root, 'c4-rf')
        os.makedirs(out)
        final = os.path.join(out, 'report-%08x.txt' % SID)
        open(final, 'wb').write(b'GOOD END TAIL')
        res = {}
        for name, word in (('bad', crc ^ 1), ('good', crc)):
            t = QueueTransport()
            t.q = [cl.frame(cl.REPORT, SID, 0, 0, rep)]
            lp = cl.Loop(t, log=lambda s: None)
            if name == 'good':   # the loader clears its report once taken: a HELLO with no report
                lp.wait = lambda *a, **k: None
            h = {'report_run': SID, 'report_len': len(rep), 'report_crc': word, 'report_state': 2, 'report_exit': 0}
            r = lp.fetch_report(h, out)
            res[name] = (r['crc_ok'], r['path'], open(final, 'rb').read())
            if name == 'bad':
                kept = open(final, 'rb').read() == b'GOOD END TAIL'
                apart = bool(r['path']) and r['path'] != final and open(r['path'], 'rb').read() == rep
        good_ok = res['good'][0] and res['good'][1] == final and res['good'][2] == rep
        ok = kept and apart and good_ok
        return ok, 'END file overwritten' if not kept else 'kept apart', {
            'bad_path': os.path.basename(res['bad'][1] or ''), 'good_replaced': good_ok}
    R.row('C4.cli_report_file', 'CLI: a loader report with a bad CRC goes to its own file; the END\'s file is '
          'replaced only by a report whose CRC checks', report_file)

    def cli_run(name, ends, when, hb, fetch):
        out = os.path.join(root, 'c4-' + name)
        os.makedirs(out)
        prog = os.path.join(out, 'p.bin')
        open(prog, 'wb').write(b'\x00\x09' * 64)
        a = argparse.Namespace(cmd='run', program=prog, args='', corrupt=None, loader=None, no_compress=False,
                               session=SID, deadline=0, wmax=16, no_collect=False, out=out, files=None, disc=None,
                               overlay=None, whole=False, reset_after=None, reset_wait=20.0, wait=5.0, tail=False)
        lp = FakeCliLoop(hello(cl, sim), hb, ends, when, fetch)
        rc = cl.session_main(a, lp, lambda s: None)
        result = json.load(open(os.path.join(out, 'run-%08x.json' % SID)))
        return rc, result

    def complete_tail():
        tail = rep[100:]   # the declared tail: the last 100 bytes of a 200-byte report (no whole-report CRC here)
        ends = [(cl.END, SID, 100, len(tail), tail), (cl.END, SID, 0xFFFFFFFF, 200, end_head(2, 200, crc, 100, 100))]
        hb = hello(cl, sim, report_run=SID, report_state=2, report=rep, boot=3)

        def fetch(h, out):
            return {'run': '%08x' % SID, 'state': 'DONE', 'exit': 0, 'bytes': 200, 'crc_ok': False, 'path': None,
                    'seconds': 0.1, 'Bps': 1, 'requests': 1, 'text_head': ''}
        rc, res = cli_run('tail', ends, 'wait', hb, fetch)
        ev = res.get('evidence') or {}
        ok = rc == 0 and ev.get('end') == 'complete' and (res.get('report') or {}).get('complete')
        return ok, 'complete tail refused' if rc == 5 else 'valid' if ok else 'rc %d' % rc, {'rc': rc, 'evidence': ev}
    R.row('C4.cli_complete_tail', 'CLI: a complete declared tail with no whole-report CRC is a valid result (its '
          'evidence says so)', complete_tail)

    def before_exec():
        ends = [(cl.END, SID, 0, len(rep), rep), (cl.END, SID, 0xFFFFFFFF, 200, end_head(2, 200, crc, 0, 200))]
        hb = hello(cl, sim, boot=3)   # back by the full restart: no saved report

        def fetch(h, out):
            raise RuntimeError('no report to fetch')
        rc, res = cli_run('exec', ends, 'run', hb, fetch)
        got = (res.get('report') or {}).get('crc_ok')
        ok = rc == 0 and got
        return ok, 'END lost before EXEC' if not res.get('end') else 'kept', {'rc': rc, 'end': bool(res.get('end'))}
    R.row('C4.cli_end_before_exec', 'CLI: an END that arrives while EXEC is awaited is kept (the handler is in '
          'place from RUN)', before_exec)

    def bridge_late_end():
        from simconsole import SimConsole
        udp, lport = free_port(socket.SOCK_DGRAM), free_port(socket.SOCK_DGRAM)
        b = core.Bridge(os.path.join(root, 'c4-b'), ('127.0.0.1', udp), '127.0.0.1:%d' % lport, 'mock',
                        autoconnect=False)
        con = SimConsole(('127.0.0.1', udp), lport)
        try:
            wait_for(lambda: b.loop.hello is not None, 8)
            r = b.submit('c4.bin', b'\x00\x09' * 4000, 'report lateend badcrc secs=1', 30)
            r.done.wait(90)
        finally:
            con.close()
            b.close()
        ev = getattr(r, 'evidence', None) or {}
        ok = r.state == 'done' and r.report_crc_ok and ev.get('loader_report') == 'bad crc' and \
            ev.get('end') == 'whole crc ok'
        found = 'bad loader report published' if r.state == 'done' and not r.report_crc_ok else \
            'END taken' if ok else 'state %s' % r.state
        return ok, found, {'state': r.state, 'crc_ok': r.report_crc_ok, 'evidence': ev, 'error': r.error}
    R.row('C4.bridge_late_end', 'bridge: an END that arrives during the loader-report fetch decides the result',
          bridge_late_end)

    def late_report():
        b = mock_bridge(core, root, 'c4-late')
        try:
            a_run = core.Run('a.bin', b'\0' * 64, '', 0, None, None, 'test')
            a_run.state, a_run.error = 'lost', 'the loader did not come back within 60 s'
            a_run.done.set()
            b.runs[a_run.id] = a_run
            b_run = core.Run('b.bin', b'\x00\x09' * 64, '', 0, None, None, 'test')
            b.runs[b_run.id] = b_run
            h = hello(cl, sim, report_run=a_run.sid, report_state=2, report=rep)

            class L:
                hello, ran_back, on_other = h, None, None

                def wait_hello(self, t, pred=None):
                    return h

                def fetch_report(self, hh, out):
                    path = None
                    if out:
                        os.makedirs(out, exist_ok=True)
                        path = os.path.join(out, 'report-%08x.txt' % a_run.sid)
                        open(path, 'wb').write(rep)
                    return {'run': '%08x' % a_run.sid, 'state': 'DONE', 'exit': 0, 'bytes': len(rep), 'crc_ok': True,
                            'path': path, 'seconds': 0.1, 'Bps': 1, 'requests': 1, 'text_head': ''}

                def transfer(self, *a):
                    return {'verdict': 1, 'verdict_text': 'test: refused', 'seconds': 0.0, 'goodput_Bps': 0,
                            'resent': 0}

                def send(self, *a):
                    pass
            real = b.loop
            b.loop = L()
            try:
                b._run(b_run)
            finally:
                b.loop = real
        finally:
            b.close()
        ok = a_run.state == 'done' and a_run.report == rep and a_run.report_crc_ok
        return ok, 'late report dropped' if a_run.report is None else 'attached', {'a_state': a_run.state,
                                                                                 'b_state': b_run.state}
    R.row('C4.late_report_attached', 'bridge: a report collected late by the next worker is attached to its own run',
          late_report)


# ------------------------------------------------------------------------------------------------ C5
def c5(R, ctx):
    cl, core, root = ctx['cl'], ctx['core'], ctx['root']
    W, H = 32, 32
    pix = bytes(range(256)) * (W * H * 2 // 256)
    head = struct.pack('<8I', 5, 1, W, H, 1, len(pix), zlib.crc32(pix) & 0xFFFFFFFF, 0)

    def acks(lp):
        return [x[3] for x in lp.sent if x[0] == cl.STILLACK]

    def run(send, tgt, receipts):
        lp = FakeLoop()
        send(tgt, lp, cl.STILL_HEAD, 5, head)
        for off in range(0, len(pix), cl.CHUNK):
            blk = pix[off:off + cl.CHUNK]
            if off == 0:
                blk = bytes([blk[0] ^ 0xFF]) + blk[1:]   # one byte damaged on the way (right length)
            send(tgt, lp, off, 5, blk)
        send(tgt, lp, cl.STILL_END, 5, head)
        first = acks(lp)[-1]
        held_receipt = receipts() > 0
        send(tgt, lp, cl.STILL_END, 5, head)          # the program asks again with no new bytes: still not complete
        again = acks(lp)[-1]
        for off in range(0, len(pix), cl.CHUNK):      # the fresh copy
            send(tgt, lp, off, 5, pix[off:off + cl.CHUNK])
        send(tgt, lp, cl.STILL_END, 5, head)
        fresh = acks(lp)[-1]
        return first, again, held_receipt, fresh

    def bridge():
        got = []
        st = core.Stills(os.path.join(root, 'c5-b'), lambda s: None)
        first, again, rec, fresh = run(lambda t, lp, a, b, pl: t.on_still(lp, 1, a, b, pl, 'mock', got.append), st,
                                       lambda: len(st.receipts))
        held = [j for j in got if j['crc_ok']]
        ok = first > 0 and again > 0 and not rec and fresh == 0 and len(held) == 1
        return ok, 'full ack on bad crc' if first == 0 else 'fresh copy asked', {
            'first_missing': first, 'again': again, 'receipt_after_bad': rec, 'after_fresh': fresh, 'held': len(held)}
    R.row('C5.bridge_crc', 'bridge: a still whose CRC fails gets no full ACK or receipt; a fresh copy is held',
          bridge)

    def cli():
        out = os.path.join(root, 'c5-c')
        sk = cl.StillSink(out, 1, lambda s: None)
        first, again, rec, fresh = run(lambda t, lp, a, b, pl: t.on_still(lp, a, b, pl), sk, lambda: len(sk.receipts))
        good = [x for x in sk.saved if x['crc_ok']]
        ok = first > 0 and again > 0 and not rec and fresh == 0 and len(good) == 1 and \
            os.path.isfile(os.path.join(out, good[0]['file']))
        return ok, 'full ack on bad crc' if first == 0 else 'fresh copy asked', {
            'first_missing': first, 'again': again, 'receipt_after_bad': rec, 'after_fresh': fresh,
            'saved': sk.saved}
    R.row('C5.cli_crc', 'CLI: a still whose CRC fails gets no full ACK or receipt; a fresh copy is saved', cli)


# ------------------------------------------------------------------------------------------------ C6
def c6(R, ctx):
    cl, core, root, sim, pcdir = ctx['cl'], ctx['core'], ctx['root'], ctx['sim'], ctx['pcdir']

    def runs_bound():
        b = mock_bridge(core, root, 'c6-r')
        keep = b.RUNS_KEEP
        try:
            for i in range(keep + 8):
                r = b.submit('r.bin', b'\0' * 64, '', 5)
                b.queue.queue.clear()
                r.done.set()
            n, n_order = len(b.runs), len(b.run_order)
        finally:
            b.close()
        return n <= keep and n_order <= keep, 'over the bound' if n > keep else 'bounded', {
            'kept': n, 'bound': keep, 'made': keep + 8}
    R.row('C6.runs_bound', 'the run history holds at most RUNS_KEEP runs after each insertion', runs_bound)

    def resetack():
        b = mock_bridge(core, root, 'c6-a')
        recs = []
        real = b.record
        b.record = lambda r, s=None: recs.append(dict(r)) or real(r, s)
        try:
            b.tap((cl.RESETACK, 0x1234, 0, 0, b''))
        finally:
            b.close()
        rung = [r for r in recs if r.get('rung')]
        acked = [r for r in recs if r.get('acknowledged')]
        return not rung and acked, 'recorded as stub rung' if rung else 'acknowledgement', {'records': recs}
    R.row('C6.resetack', 'a RESETACK is recorded as an acknowledgement, never as a rung', resetack)

    def inferred():
        w0 = cl.way_back_of(hello(cl, sim, stub_step=0))
        w6 = cl.way_back_of(hello(cl, sim, stub_step=6))
        w17 = cl.way_back_of(hello(cl, sim, stub_step=17))
        ok = w0 == 'full restart (inferred)' and w6 == 'stub' and w17 == 'full restart'
        return ok, 'asserted full restart' if w0 == 'full restart' else 'inferred' if ok else str(w0), {
            'none': w0, 'jump': w6, 'full_restart_crumb': w17}
    R.row('C6.way_back_inferred', 'no stub record: the way back is "full restart (inferred)"; the crumb names it, the '
          'jump names "stub"', inferred)

    def docs():
        d = os.path.join(pcdir, '..', 'docs')
        api = open(os.path.join(d, 'BRIDGE_API.md'), encoding='utf-8').read()
        rel = open(os.path.join(d, 'RELEASE_0.2.md'), encoding='utf-8').read()
        gate = open(os.path.join(pcdir, '..', 'tools', 'gate', 'gate.sh'), encoding='utf-8').read()
        miss = []
        if '`tail_bytes`' not in api:
            miss.append('BRIDGE_API: END field tail_bytes')
        if '`power`' not in api:
            miss.append('BRIDGE_API: rung power')
        if 'full restart (inferred)' not in api:
            miss.append('BRIDGE_API: inferred full restart')
        if '`reset_ack`' not in api:
            miss.append('BRIDGE_API: RESETACK as reset_ack')
        if 'after insertion' not in api:
            miss.append('BRIDGE_API: run history bound after insertion')
        if '20 tests and five control rows' in rel:
            miss.append('RELEASE: the old suite count')
        n3 = len(PINNED)
        c3 = len([v for v in PINNED.values() if v is not None])
        if 'test_safety3' not in gate:
            miss.append('gate.sh: test_safety3 rows')
        if '%d pinned rows' % n3 not in rel or '%d controls' % c3 not in rel:
            miss.append('RELEASE: test_safety3 counts (%d rows, %d controls)' % (n3, c3))
        return not miss, 'docs stale' if miss else 'docs true', {'missing': miss}
    R.row('C6.docs', 'the published contract says what the code and gate do (fields, rungs, bounds, counts)', docs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out')
    ap.add_argument('--pc', default=os.path.join(HERE, '..', 'pc'), help='the pc/ tree to test')
    ap.add_argument('--controls', action='store_true', help='the control: pinned rows must fail as named on this --pc')
    a = ap.parse_args()
    pcdir = os.path.abspath(a.pc)
    sys.path.insert(0, pcdir)
    import dcql_loop as cl
    import dcql_disc
    from bridge import core
    import simconsole as sim
    sys.path.insert(0, pcdir)   # simconsole puts this checkout's pc/ first: the tree under test goes first again
    root =tempfile.mkdtemp(prefix='ql-safety3-')
    R = ctlrows.Rows(PINNED, a.controls)
    ctx = {'cl': cl, 'core': core, 'dcql_disc': dcql_disc, 'pcdir': pcdir, 'root': root, 'sim': sim}
    try:
        for part in (c1, c2, c3, c4, c5, c6):
            try:
                part(R, ctx)
            except Exception as e:   # a setup error: its rows go missing, and a missing pinned row is a FAIL
                print('%s: setup error %s: %s' % (part.__name__, type(e).__name__, e), flush=True)
        R.row('C7.harness', 'the control harness: an error, a pass, a wrong discrepancy or a missing row fails a control',
              lambda: (ctlrows.self_check(), 'harness', {}))
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return R.verdict(pcdir if a.controls else 'this tree', a.out)


if __name__ == '__main__':
    sys.exit(main())
