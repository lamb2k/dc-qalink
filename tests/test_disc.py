#!/usr/bin/env python3
"""test_disc.py: the network disc's PC side and the build records, on this PC with no console (lane ql-disc).

  python tests/test_disc.py [--out DIR]

A small console model speaks the wire (DOPEN/DINFO, DREAD/DDATA with piece masks, DCLOSE, DLOG) to pc/dcql_disc.py:
mount table, overlay sides, whole-disc mode from a folder, an .iso and a GD-style .gdi, directory listings, a resend of
missing pieces, the read log; then the bridge: a push makes a build record, a session is format 2 and stamps the
build on every record, a disc read lands in it, an old format-1 file still lists. Each check has a control that must
fail (marked CONTROL)."""
import hashlib, json, os, shutil, struct, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [os.path.join(ROOT, 'pc'), os.path.join(ROOT, 'tools')]
import dcql_disc as dd          # noqa: E402
import netdisc_testdata as td   # noqa: E402

results = []


def check(name, ok, detail=''):
    results.append((name, bool(ok), detail))
    print('%s %s%s' % ('PASS' if ok else 'FAIL', name, (': ' + detail) if detail else ''), flush=True)


class FakeLoop:
    def __init__(self, drop=()):
        self.out = []
        self.drop = set(drop)   # (type, b) pairs to lose once
        self.dropped = 0

    def send(self, typ, run, a=0, b=0, payload=b''):
        if (typ, b) in self.drop:
            self.drop.discard((typ, b))
            self.dropped += 1
            return
        self.out.append((typ, run, a, b, bytes(payload)))


class Console:
    """The console side as lib/dcql_disc.c does it, in Python, against a DiscService."""
    RUN = 0x1234abcd

    def __init__(self, svc, loop):
        self.svc, self.loop, self.req = svc, loop, 100

    def open(self, path, flags=0):
        self.req += 1
        self.loop.out.clear()
        self.svc.handle(self.loop, dd.DOPEN, self.RUN, self.req, 0, struct.pack('<I', flags) + path.encode())
        (typ, _, a, _, pl), = self.loop.out
        assert typ == dd.DINFO and a == self.req
        return struct.unpack('<iIII', pl)

    def read(self, h, off, n, mask=None):
        self.req += 1
        pieces = (n + 1023) // 1024
        mask = (1 << pieces) - 1 if mask is None else mask
        self.loop.out.clear()
        self.svc.handle(self.loop, dd.DREAD, self.RUN, self.req, 0, struct.pack('<IIIIII', h, off, n, mask, 0, 0))
        got = {b: pl for typ, _, a, b, pl in self.loop.out if typ == dd.DDATA and a == self.req}
        return got

    def read_all(self, h, size, block=8192):
        out = bytearray()
        for off in range(0, size, block):
            n = min(block, size - off)
            got = self.read(h, off, n)
            missing = [i for i in range((n + 1023) // 1024) if i not in got]
            if missing:   # the resend: only the missing pieces
                m = sum(1 << i for i in missing)
                got.update(self.read(h, off, n, m))
            out += b''.join(got[i] for i in sorted(got))
        return bytes(out)

    def table(self):
        st, h, size, _ = self.open('/cd', dd.DO_MOUNT)
        raw = self.read_all(h, size)
        magic, count, flags, files = struct.unpack_from('<IIII', raw, 0)
        ents = [struct.unpack_from('<II', raw, 32 + 8 * i) for i in range(count)]
        return magic, flags, files, ents


def main():
    out = sys.argv[sys.argv.index('--out') + 1] if '--out' in sys.argv else tempfile.mkdtemp(prefix='ql-disc-')
    if os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(out)
    exp = td.make(out, iso=True)
    base, over = os.path.join(out, 'base'), os.path.join(out, 'overlay')

    # ---------------------------------------------------------------- overlay mode
    svc = dd.DiscService(base, over)
    m = svc.manifest()
    side = {r['path'].split('/')[-1]: r['side'] for r in m['files']}
    want = {n: v['side'] for n, v in exp.items()}
    check('manifest sides (changed and new from the PC, unchanged and identical copies from the disc)',
          all(side.get(n) == s for n, s in want.items()), json.dumps({n: side.get(n) for n in sorted(want)}))
    check('identical overlay copy is read from the disc (SAME.BIN)', side.get('SAME.BIN') == 'disc')
    loop = FakeLoop()
    c = Console(svc, loop)
    magic, flags, files, ents = c.table()
    served = sorted(dd.fnv('NETDISC/' + n) for n, s in want.items() if s == 'pc')
    check('mount table: magic, overlay flags, the served files by hash', magic == dd.MAGIC and flags == 0 and
          sorted(e[0] for e in ents) == served and files == m['count'], '%d entries' % len(ents))
    check('hash: case, leading slash and ";1" do not matter',
          dd.fnv('/netdisc/big.bin;1') == dd.fnv('NETDISC/BIG.BIN'))
    st, h, size, fl = c.open('NETDISC/BIG.BIN', dd.DO_OVERLAY)
    data = c.read_all(h, size)
    check('overlay file read whole matches the overlay', st == 0 and fl & dd.DI_OVERLAY and
          hashlib.sha256(data).hexdigest() == exp['BIG.BIN']['sha256'], '%d B' % len(data))
    st2 = c.open('NETDISC/MID.BIN', dd.DO_OVERLAY)[0]
    check('an unchanged file asked in overlay mode is sent back to the disc (status -3)', st2 == dd.DS_BASE)
    st3 = c.open('NETDISC/NOPE.BIN', 0)[0]
    check('a file on neither side is refused (-2)', st3 == dd.DS_NOENT)
    # resend: lose two pieces of one block, ask again for only those
    loop2 = FakeLoop(drop={(dd.DDATA, 3), (dd.DDATA, 6)})
    c2 = Console(svc, loop2)
    c2.req = 5000   # request ids are unique per run (a repeated id is a repeated DOPEN: the same handle)
    st, h, size, _ = c2.open('NETDISC/NEW.BIN', dd.DO_OVERLAY)
    first = c2.read(h, 0, 8192)
    again = c2.read(h, 0, 8192, (1 << 3) | (1 << 6))
    check('a resend asks for and gets only the missing pieces', sorted(first) == [0, 1, 2, 4, 5, 7] and
          sorted(again) == [3, 6] and svc.stats['resend_requests'] >= 1,
          'first %s again %s resend requests %d' % (sorted(first), sorted(again), svc.stats['resend_requests']))
    # CONTROL: a wrong expected hash must be seen as wrong
    bad = hashlib.sha256(data[:-1] + bytes([data[-1] ^ 1])).hexdigest()
    check('CONTROL a one-byte change is caught by the hash', bad != exp['BIG.BIN']['sha256'])
    # directory listing (whole mode lists both sides)
    st, h, size, fl = c.open('NETDISC', dd.DO_DIR)
    lst = c.read_all(h, size)
    names, i = [], 0
    while i < len(lst):
        sz, n = struct.unpack_from('<IH', lst, i)
        names.append(lst[i + 6:i + 6 + n].decode())
        i += 6 + n
    check('directory listing has every file of the view', st == 0 and fl & dd.DI_DIR and
          sorted(names) == sorted(exp), ' '.join(sorted(names)))
    # DLOG: an open, two merged reads, a close
    rec = []
    svc.record = rec.append
    pl = struct.pack('<BBHI', dd.DL_OPEN, 1, 3, 1048576) + b'NETDISC/BIG.BIN\0' + \
        struct.pack('<BBHIIII', dd.DL_READ, 1, 3, 0, 65536, 51234, 32) + struct.pack('<BBH', dd.DL_CLOSE, 0, 3)
    svc.handle(loop, dd.DLOG, c.RUN, 1, 0, pl)
    rd = [r for r in rec if r['op'] == 'read']
    check('read log: file, offset, bytes, ms, side', len(rd) == 1 and rd[0]['path'] == 'NETDISC/BIG.BIN' and
          rd[0]['bytes'] == 65536 and rd[0]['ms'] == 51.234 and rd[0]['side'] == 'pc', json.dumps(rd))

    # ---------------------------------------------------------------- whole mode: folder, .iso, .gdi agree
    shas = {}
    for label, src in (('folder', base), ('iso', os.path.join(out, 'base.iso')), ('gdi', os.path.join(out, 'base.gdi'))):
        w = dd.DiscService(src, over, whole=True)
        cw = Console(w, FakeLoop())
        _, flags, _, ents = cw.table()
        got = {}
        for r in w.manifest()['files']:
            st, h, size, _ = cw.open(r['path'])
            got[r['path'].upper()] = hashlib.sha256(cw.read_all(h, size)).hexdigest() if st == 0 else 'refused %d' % st
        shas[label] = got
        check('whole disc from the %s: every file from the PC, the overlay on top' % label,
              flags & dd.DT_WHOLE and not ents and all(r['side'] == 'pc' for r in w.manifest()['files']) and
              got.get('NETDISC/BIG.BIN') == exp['BIG.BIN']['sha256'], '%d files' % len(got))
    check('the folder, the .iso and the .gdi serve the same bytes', shas['folder'] == shas['iso'] == shas['gdi'])
    gdi_only = dd.ImageSource(os.path.join(out, 'base.gdi'))
    folder = dd.FolderSource(base)
    check('image reader: names and sizes as the folder', {k: v[1] for k, v in gdi_only.files.items()} ==
          {k: v[1] for k, v in folder.files.items()}, gdi_only.volume)
    try:
        dd.ImageSource(os.path.join(out, 'expect.json'))
        check('CONTROL not an image is refused', False)
    except ValueError:
        check('CONTROL not an image is refused', True)
    try:
        dd.DiscService(None, over, whole=True)
        check('CONTROL whole mode without a base is refused', False)
    except ValueError:
        check('CONTROL whole mode without a base is refused', True)

    # ---------------------------------------------------------------- the bridge: builds and session format 2
    from bridge.core import Bridge
    from bridge import live
    data_dir = os.path.join(out, 'bridge')
    b = Bridge(data_dir, ('127.0.0.1', 0), '127.0.0.1:1', 'mock', None, None, None, 'run', None, autoconnect=False,
               only_local=True, disc=base, overlay=over)
    try:
        prog = b'\x00' * 4096 + b'test program'
        r1 = b.submit('prog.bin', prog, 'a=1', 0, None, None, 'test', 'abc1234')
        r2 = b.submit('prog.bin', prog, 'a=1', 0, None, None, 'test')
        r3 = b.submit('prog.bin', prog, 'a=2', 0, None, None, 'test')
        bl = b.builds[r1.build]
        check('a push makes a build record (program, commit, disc manifest, loader, args, tier)',
              bl['program']['sha256'] == hashlib.sha256(prog).hexdigest() and bl['program']['commit'] == 'abc1234' and
              bl['disc']['manifest'] == m['sha256'] and bl['args'] == 'a=1' and bl['tier'] == 'mock')
        check('the same push again is the same build; other arguments are another', r1.build == r2.build and
              r3.build != r1.build and bl['runs'] == [r1.id, r2.id])
        s = b.new_session(r1.sid, {'name': 'prog.bin'})
        b.tap((dd.DLOG, r1.sid, 1, 0, struct.pack('<BBHI', dd.DL_OPEN, 1, 1, 9000) + b'NETDISC/SMALL5.BIN\0' +
               struct.pack('<BBHIIII', dd.DL_READ, 1, 1, 0, 9000, 1500, 5)))
        s.add({'t': 'log', 'n': 1, 'lv': 'info', 'ph': 'play', 'msg': 'x'})
        s.flush()
        lines = [json.loads(x) for x in open(s.path, encoding='utf-8')]
        meta = lines[0]
        recs = lines[1:]
        check('session format 2: meta carries the build record', meta['version'] == 2 and
              meta['build']['id'] == r1.build)
        check('every record carries the build id, disc reads included', recs and all(x.get('build') == r1.build
              for x in recs) and any(x['t'] == 'disc' and x.get('op') == 'read' for x in recs),
              '%d records' % len(recs))
        check('the run says its build', r1.json('mock', 'x')['build'] == r1.build)
        old = os.path.join(b.sessions_dir, '20260101-000000-00000001.jsonl')
        open(old, 'w').write(json.dumps({'t': 'meta', 'format': live.FORMAT, 'version': 1, 'id': 'old', 'label': 'v1',
                                         'device': {'name': 'x'}, 'tier': 'mock', 'recorded': 'x'}) + '\n')
        check('builds persist in builds.json', r1.build in json.load(open(b.builds_path)))
        st = b.status()
        check('status names the current build and the disc mode', st['build'] == r3.build and st['disc'] == 'overlay')
        # CONTROL: a session without a build carries none (the stamp is the build's, not a constant)
        s0 = live.Session(None, 'x', {'name': 'x'}, 'mock', 'x')
        r0 = s0.add({'t': 'log', 'n': 1, 'lv': 'info', 'ph': 'play', 'msg': 'x'})
        check('CONTROL a session with no build stamps nothing', 'build' not in r0)
    finally:
        b.close()
    fails = [r for r in results if not r[1]]
    print('%d/%d passed' % (len(results) - len(fails), len(results)))
    json.dump([{'check': n, 'ok': o, 'detail': d} for n, o, d in results], open(os.path.join(out, 'results.json'), 'w'),
              indent=1)
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
