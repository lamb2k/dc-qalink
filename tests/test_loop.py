#!/usr/bin/env python3
"""test_loop.py: host tests of the control loop (dc-qalink 0.2; first written for the RE4 Dreamcast console loop, 2026-11-19).

The loader's own protocol code (engine/loader/cl_core.c, cl_util.c) built for the PC as cl_host, the PC sender
(consoleloop.py) and a bad line between them (simlink.py). Every check has a control that must fail.

  python3 tests/test_loop.py --host <cl_host> --out <evidence dir> [--program <elf or bin>] [--quick]

Writes <out>/results.json and <out>/results.md; exit 0 only if every case passed.
"""
import argparse, hashlib, json, os, random, subprocess, sys, time, zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "pc"))
import dcql_loop as cl   # noqa: E402
from simlink import SimLink, Direction   # noqa: E402

RESULTS = []


def record(name, ok, **kw):
    RESULTS.append(dict(name=name, ok=bool(ok), **kw))
    print('%-46s %s %s' % (name, 'PASS' if ok else 'FAIL', json.dumps(kw)[:300]), flush=True)


# ------------------------------------------------------------------------------------------------ unit: codec, hash
def unit_tests(host, tmp, program):
    samples = {
        'program': program,
        'random 70000': random.Random(5).randbytes(70000),
        'zeros 300000': bytes(300000),
        'text 50000': (b'the quick brown fox jumps over the lazy dog %d\n' * 1200)[:50000],
        'one byte': b'Q',
    }
    variants = [('level 9', dict(level=9)), ('level 1', dict(level=1)), ('stored', dict(level=0)),
                ('fixed', dict(level=6, strategy=zlib.Z_FIXED)), ('huffman only', dict(level=6, strategy=zlib.Z_HUFFMAN_ONLY)),
                ('rle', dict(level=6, strategy=zlib.Z_RLE))]
    bad = 0
    n = 0
    for sname, raw in samples.items():
        for vname, kw in variants:
            c = zlib.compressobj(kw.get('level', 9), zlib.DEFLATED, -15, 9, kw.get('strategy', zlib.Z_DEFAULT_STRATEGY))
            comp = c.compress(raw) + c.flush()
            src, dst = os.path.join(tmp, 'u.def'), os.path.join(tmp, 'u.out')
            open(src, 'wb').write(comp)
            r = subprocess.run([host, '--inflate', src, dst], capture_output=True)
            n += 1
            if r.returncode != 0 or open(dst, 'rb').read() != raw:
                bad += 1
                print('  inflate mismatch: %s / %s rc %d %s' % (sname, vname, r.returncode, r.stderr[:200]))
    record('inflate equals zlib (5 inputs x 6 encodings)', bad == 0, cases=n, mismatches=bad)
    # malformed streams: truncations and random bytes must end in an error, never a crash or a wrong success
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    comp = c.compress(program) + c.flush()
    crashes = wrong = 0
    rng = random.Random(9)
    cases = [comp[:k] for k in (1, 2, 10, len(comp) // 2, len(comp) - 1)]
    cases += [rng.randbytes(rng.randrange(1, 4000)) for _ in range(150)]
    for i, case in enumerate(cases):
        src, dst = os.path.join(tmp, 'm.def'), os.path.join(tmp, 'm.out')
        open(src, 'wb').write(case)
        r = subprocess.run([host, '--inflate', src, dst], capture_output=True)
        if r.returncode not in (0, 1):
            crashes += 1
        elif r.returncode == 0 and i < 5:
            wrong += 1   # a truncated stream must not decode as complete
    record('inflate refuses malformed streams (155)', crashes == 0 and wrong == 0, crashes=crashes,
           truncated_accepted=wrong)
    bad = 0
    for sname, raw in samples.items():
        p = os.path.join(tmp, 's.bin')
        open(p, 'wb').write(raw)
        out = subprocess.run([host, '--sha256', p], capture_output=True, text=True).stdout.strip()
        bad += out != hashlib.sha256(raw).hexdigest()
    record('sha256 equals hashlib (5 inputs)', bad == 0, mismatches=bad)
    # framing: CRC catches every single-bit flip of a datagram
    d = cl.frame(cl.DATA, 0x1234, 7, 0, program[:1024])
    missed = 0
    for bit in range(len(d) * 8):
        b = bytearray(d)
        b[bit >> 3] ^= 1 << (bit & 7)
        if cl.unframe(bytes(b)) is not None:
            missed += 1
    record('datagram CRC catches every 1-bit flip', missed == 0 and cl.unframe(d) is not None, bits=len(d) * 8,
           missed=missed)


# ------------------------------------------------------------------------------------------------ end to end
class Rig:
    port = 53100

    def __init__(self, host, out, name, up=None, down=None, max_raw=None, seed=1):
        Rig.port += 10
        p = Rig.port
        self.dir = os.path.join(out, name)
        os.makedirs(self.dir, exist_ok=True)
        self.log_path = os.path.join(self.dir, 'loader.log')
        self.logf = open(self.log_path, 'w')
        args = [host, '--lport', str(p + 1), '--peer', '127.0.0.1:%d' % (p + 2), '--dir', self.dir]
        if max_raw:
            args += ['--max-raw', str(max_raw)]
        self.proc = subprocess.Popen(args, stdout=self.logf, stderr=subprocess.STDOUT)
        self.link = SimLink(p + 3, p + 2, ('127.0.0.1', p + 1), up, down, seed).start()
        self.pc = ['--udp', '127.0.0.1:%d' % (p + 3), '--bind', str(p + 4)]
        time.sleep(0.3)

    def cl(self, *args):
        return cl.main(self.pc + list(args))

    def loader_log(self):
        self.logf.flush()
        return open(self.log_path).read()

    def close(self):
        self.proc.terminate()
        self.proc.wait(5)
        self.link.close()
        self.logf.close()

    def run_json(self, session):
        return json.load(open(os.path.join(self.dir, 'run-%08x.json' % session)))


def e2e_run(host, out, name, program_path, raw, args='report', up=None, down=None, session=None, expect='DONE',
            extra=(), max_raw=None, seed=1):
    session = session or random.getrandbits(32) | 1
    r = Rig(host, out, name, up, down, max_raw, seed)
    try:
        rc = r.cl('run', program_path, '--args', args, '--session', '%08x' % session, '--out', r.dir, '--wait', '60',
                  *extra)
        res = r.run_json(session) if os.path.exists(os.path.join(r.dir, 'run-%08x.json' % session)) else {}
        img = os.path.join(r.dir, 'exec-%08x.bin' % session)
        ran = open(img, 'rb').read() if os.path.exists(img) else None
        rep = res.get('report') or {}
        return dict(rc=rc, res=res, ran=ran, rep=rep, log=r.loader_log(), link=r.link.report(), dir=r.dir)
    finally:
        r.close()


def e2e_tests(host, out, program_path, raw, quick):
    sha = hashlib.sha256(raw).hexdigest()

    def summary(x):
        t = x['res'].get('transfer', {})
        return dict(rc=x['rc'], seconds=t.get('seconds'), comp=t.get('comp_bytes'), resent=t.get('resent'),
                    goodput_Bps=t.get('goodput_Bps'), report=x['rep'].get('state'), crc_ok=x['rep'].get('crc_ok'),
                    link=x['link'])

    # 1 clean line
    x = e2e_run(host, out, 'clean', program_path, raw)
    ok = x['rc'] == 0 and x['ran'] == raw and x['rep'].get('crc_ok') and ('sha256=' + sha) in x['rep'].get('text_head', '')
    record('clean: program runs, report returns', ok, **summary(x))

    # 2 bad line both ways
    bad = lambda s: Direction(drop=0.10, corrupt=0.05, dup=0.05, reorder_ms=150)
    x = e2e_run(host, out, 'lossy', program_path, raw, 'report big:20000', bad(1), bad(2), seed=3)
    rejected = x['res'].get('transfer', {}).get('console_ack', {}).get('bad_crc', 0)
    ok = x['rc'] == 0 and x['ran'] == raw and x['rep'].get('crc_ok') and x['rep'].get('bytes', 0) > 20000
    record('lossy: 10% loss, 5% corrupt, 5% dup, reorder', ok and rejected > 0, console_bad_crc=rejected, **summary(x))

    # 3 line dead for 12 s in the middle of the transfer (the loader redials; the PC resumes with LOAD)
    dead = [(1.5, 13.5)]
    slow = lambda: Direction(rate=6000, latency_ms=40, outages=dead)
    x = e2e_run(host, out, 'outage', program_path, raw, 'report', slow(), slow())
    resumes = x['log'].count('resumes at')
    ok = x['rc'] == 0 and x['ran'] == raw and resumes >= 1
    record('outage: 12 s dead line mid-transfer, resume', ok, resume_loads=resumes, **summary(x))

    # 4 PC sender killed halfway and started again with a new session: only the missing chunks travel
    r = Rig(host, out, 'restart', Direction(rate=8000), Direction(rate=8000))
    try:
        p = subprocess.Popen([sys.executable, os.path.join(HERE, "..", "pc", "dcql_loop.py")] + r.pc +
                             ['send', program_path, '--session', '0000a001', '--out', r.dir],
                             stdout=open(os.path.join(r.dir, 'first.log'), 'w'), stderr=subprocess.STDOUT)
        time.sleep(max(3.0, len(cl.Image(raw).comp) / 8000 / 2))
        p.kill()
        p.wait()
        time.sleep(1.0)
        rc = r.cl('run', program_path, '--session', '0000a002', '--args', 'report', '--out', r.dir, '--wait', '60')
        res = r.run_json(0xa002)
        log = r.loader_log()
        line = [l for l in log.split('\n') if 'resumes at' in l]
        have = int(line[0].split('resumes at ')[1].split('/')[0]) if line else 0
        n = res['transfer']['chunks']
        sent_second = r.link.down.counts['in']
        ran = open(os.path.join(r.dir, 'exec-0000a002.bin'), 'rb').read()
        ok = rc == 0 and ran == raw and have > 0 and res['report']['crc_ok']
        record('restart: new PC session resumes the image', ok, chunks=n, held_at_resume=have, rc=rc)
    finally:
        r.close()

    # 5 corruption controls: refused before it runs
    for kind, want in (('stream', 1), ('payload', 4), ('declared', 4)):
        session = 0xC0DE0000 | want << 4 | len(kind)
        x = e2e_run(host, out, 'corrupt-' + kind, program_path, raw, 'report', session=session,
                    extra=('--corrupt', kind))
        t = x['res'].get('transfer', {})
        ok = (t.get('verdict') == want and x['ran'] is None and 'EXEC' not in x['log'] and
              str(x['res'].get('run', '')).startswith('refused') and x['rc'] != 0)
        record('control: corrupt %s refused, never runs' % kind, ok, verdict=t.get('verdict'),
               verdict_text=t.get('verdict_text'), run=x['res'].get('run'), exec_file=x['ran'] is not None)
    # and the same rig accepts the good image afterwards (the refusal was the image, not the rig)
    # 6 run outcomes through the hand-off area
    for args, want in (('crash', 'CRASHED'), ('timeout', 'TIMEOUT'), ('none', 'LOST')):
        x = e2e_run(host, out, 'outcome-' + args, program_path, raw, args)
        ok = x['rep'].get('state') == want and x['ran'] == raw
        record('outcome %s reported as %s' % (args, want), ok, got=x['rep'].get('state'), rc=x['rc'])

    # 7 too large for the console: refused at LOAD
    x = e2e_run(host, out, 'oversize', program_path, raw, 'report', max_raw=len(raw) - 1)
    record('control: oversize image refused at LOAD', x['rc'] != 0 and x['ran'] is None, rc=x['rc'])

    # 8 the measured line: 1.39 KB/s each way, 300 ms latency, 1% loss
    if not quick:
        line = lambda: Direction(rate=1390, latency_ms=300, drop=0.01, qcap=4096)
        part = raw[:60000]
        pp = os.path.join(out, 'part60k.bin')
        open(pp, 'wb').write(part)
        x = e2e_run(host, out, 'modem-rate', pp, part, 'report', line(), line(), seed=11)
        t = x['res'].get('transfer', {})
        eff = (t.get('goodput_Bps') or 0) / (1390.0 * 1024 / (1024 + 20 + 36))   # of the payload the line can carry
        record('modem rate 1390 B/s: 60 KB raw image', x['rc'] == 0 and x['ran'] == part and eff > 0.75,
               efficiency=round(eff, 3), **summary(x))


# ------------------------------------------------------------------------------------------------ protocol 2 (0.2)
class FakeLoop:
    def __init__(self):
        self.sent = []

    def send(self, type_, session, a=0, b=0, payload=b''):
        self.sent.append((type_, session, a, b, payload))


def v2_tests(host, out):
    import socket as so, struct as st
    # 1 version: the console (cl_host) answers a version-1 datagram with ERR "protocol version", and runs nothing
    r = Rig(host, out, 'version')
    try:
        s = so.socket(so.AF_INET, so.SOCK_DGRAM)
        s.bind(('127.0.0.1', 0))
        s.settimeout(0.5)
        img = cl.Image(b'\x09\x00' * 600)
        s.sendto(cl.frame(cl.LOAD, 0x1111, payload=img.load_payload(), version=1), ('127.0.0.1', Rig.port + 3))
        got = []
        t0 = time.time()
        while time.time() - t0 < 4 and not any(g[0] == cl.ERR for g in got):
            try:
                d = s.recv(4096)
            except so.timeout:
                continue
            m = cl.unframe(d, any_version=True)
            if m:
                got.append(m)
        errs = [g for g in got if g[0] == cl.ERR and g[1] == 0x1111]
        ok = bool(errs) and errs[0][2] == 14 and 'load 00001111' not in r.loader_log()
        record('version: a v1 LOAD is refused (ERR protocol version)', ok, answers=[cl.TYPE_NAMES.get(g[0]) for g in got][:6])
        # control: the same LOAD as version 2 is taken (an ACK for that session)
        s.sendto(cl.frame(cl.LOAD, 0x2222, payload=img.load_payload()), ('127.0.0.1', Rig.port + 3))
        acks = []
        t0 = time.time()
        while time.time() - t0 < 4 and not acks:
            try:
                m = cl.unframe(s.recv(4096))
            except so.timeout:
                continue
            if m and m[0] == cl.ACK and m[1] == 0x2222:
                acks.append(m)
        record('version control: the same LOAD as v2 is acknowledged', bool(acks))
        s.close()
    finally:
        r.close()
    # 2 the PC tool refuses a console of another version
    msg = cl.frame(cl.HELLO, 0, payload=bytes(96), version=1)
    class OneShot:
        def __init__(self): self.d = [msg]
        def recv(self, t): return self.d.pop() if self.d else None
        def send(self, d): pass
    try:
        cl.Loop(OneShot()).recv(0.1)
        refused = False
    except cl.VersionMismatch:
        refused = True
    record('version: the PC tool refuses a v1 loader', refused)
    # 3 the file service: below its root only, and the bytes as on disk
    root = os.path.join(out, 'files')
    os.makedirs(os.path.join(root, 'A'), exist_ok=True)
    data = random.Random(3).randbytes(20000)
    open(os.path.join(root, 'A', 'x.bin'), 'wb').write(data)
    open(os.path.join(out, 'secret.txt'), 'w').write('outside the root')
    fs = cl.FileService(root, print)
    fl = FakeLoop()
    fs.on_open(fl, 7, 1, b'A/x.bin')
    fs.on_open(fl, 7, 2, b'../secret.txt')
    fs.on_open(fl, 7, 3, b'A/../../secret.txt')
    fs.on_open(fl, 7, 4, b'missing.bin')
    infos = [st.unpack('<iIII', p[4]) for p in fl.sent if p[0] == cl.FINFO]
    h = infos[0][1]
    fl.sent.clear()
    got = bytearray()
    for off in range(0, len(data), cl.FREAD_MAX):
        fs.on_read(fl, 7, 10 + off, st.pack('<III', h, off, min(cl.FREAD_MAX, len(data) - off)))
    for p in fl.sent:
        got += p[4]
    ok = infos[0][0] == 0 and infos[0][2] == len(data) and [i[0] for i in infos[1:]] == [-2, -2, -2] and bytes(got) == data
    record('file service: root only, bytes as on disk', ok, statuses=[i[0] for i in infos], bytes=len(got))
    # 4 stills: a lost block is asked for again; the PNG appears only when whole and its CRC matches
    w, hgt = 64, 40
    pix = bytes(random.Random(4).randbytes(w * hgt * 2))
    crc = zlib.crc32(pix) & 0xFFFFFFFF
    sk = cl.StillSink(out, 0xabc, print)
    fl = FakeLoop()
    head = st.pack('<8I', 1, 77, w, hgt, 1, len(pix), crc, 0)
    sk.on_still(fl, cl.STILL_HEAD, 1, head)
    blocks = list(range(0, len(pix), cl.CHUNK))
    for off in blocks[:-1]:
        sk.on_still(fl, off, 1, pix[off:off + cl.CHUNK])
    sk.on_still(fl, cl.STILL_END, 1, head)
    first = fl.sent[-1]
    sk.on_still(fl, blocks[-1], 1, pix[blocks[-1]:])
    sk.on_still(fl, cl.STILL_END, 1, head)
    second = fl.sent[-1]
    png = os.path.join(out, 'still-00000abc-001-p00077.png')
    ok = first[3] == 1 and second[3] == 0 and os.path.exists(png) and open(png, 'rb').read(8) == b'\x89PNG\r\n\x1a\n'
    record('stills: a lost block is asked again, the PNG only when whole', ok, missing_first=first[3],
           missing_after=second[3])
    # control: a still whose CRC does not match is not saved
    sk2 = cl.StillSink(out, 0xabd, print)
    sk2.on_still(fl, cl.STILL_HEAD, 2, st.pack('<8I', 2, 78, w, hgt, 1, len(pix), crc ^ 1, 0))
    for off in blocks:
        sk2.on_still(fl, off, 2, pix[off:off + cl.CHUNK])
    sk2.on_still(fl, cl.STILL_END, 2, head)
    record('stills control: a CRC mismatch is not saved', not os.path.exists(os.path.join(out, 'still-00000abd-002-p00078.png'))
           and sk2.saved and sk2.saved[0]['crc_ok'] is False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--program', help='ELF or bin to send (default: a synthetic 200 KB image)')
    ap.add_argument('--quick', action='store_true')
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    if a.program:
        raw = cl.load_program(a.program)
        program_path = a.program
    else:
        rng = random.Random(7)
        words = [rng.getrandbits(16) for _ in range(512)]
        raw = b''.join(words[rng.randrange(512)].to_bytes(2, 'little') for _ in range(100000))
        program_path = os.path.join(a.out, 'synthetic.bin')
        open(program_path, 'wb').write(raw)
    print('program %s: %d bytes, sha256 %s' % (program_path, len(raw), hashlib.sha256(raw).hexdigest()))
    t0 = time.time()
    unit_tests(a.host, a.out, raw)
    v2_tests(a.host, a.out)
    e2e_tests(a.host, a.out, program_path, raw, a.quick)
    passed = sum(r['ok'] for r in RESULTS)
    json.dump({'program': program_path, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
               'seconds': round(time.time() - t0, 1), 'results': RESULTS}, open(os.path.join(a.out, 'results.json'), 'w'),
              indent=2)
    with open(os.path.join(a.out, 'results.md'), 'w') as f:
        f.write('# dc-qalink control loop host tests\n\nprogram %s (%d bytes)\n\n| case | result | details |\n|---|---|---|\n' % (
            os.path.basename(program_path), len(raw)))
        for r in RESULTS:
            d = {k: v for k, v in r.items() if k not in ('name', 'ok')}
            f.write('| %s | %s | %s |\n' % (r['name'], 'PASS' if r['ok'] else 'FAIL', json.dumps(d)[:400].replace('|', '/')))
    print('%d/%d passed in %.0f s' % (passed, len(RESULTS), time.time() - t0))
    return 0 if passed == len(RESULTS) else 1


if __name__ == '__main__':
    sys.exit(main())
