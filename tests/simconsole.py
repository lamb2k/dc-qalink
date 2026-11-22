#!/usr/bin/env python3
"""simconsole.py: a simulated console for the bridge's host tests (dc-qalink 0.2, lane ql-data). Pure Python, any OS.

The loader's side of protocol 2 (beacons, LOAD / DATA / ACK / VERIFY / VERDICT, RUN / EXEC, the report by REPQ) and a
program's side with the live-data additions (pc/bridge/wire.py): its HELLO, serial lines, LIVEDEF on SUB, a record a
picture at `rate`, numbered trace events, game log lines, stills on GRAB, CMD answered with CMDACK (pause, resume,
step, warp, option; pad acknowledged), RESET. The program "runs" for `secs=<s>` of its arguments (default 3), then
DONE with a report; `crash` in the arguments ends it CRASHED; `nolive` makes a program without the live additions
(GRAB and CMD unanswered, no records). Its build string starts with "sim", so the bridge labels its numbers "mock".

  python simconsole.py --pc 127.0.0.1:52060 --lport 52061
"""
import argparse, hashlib, os, random, socket, struct, sys, threading, time, zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'pc'))
import dcql_loop as cl            # noqa: E402
from bridge import wire           # noqa: E402

DEF = '''program simcubes.elf
rate 60
phase draw 14000 - Drawing
phase logic 6000 - Logic
phase wait - - Pacer wait
phase frame 16667 total Picture
ctr tris 0 Scene Triangles
ctr drops 1 Guards Dropped pictures
ctr heap_fail 1 Guards Heap failures
lane 1 Loads
lane 2 Pad script
preset start Scene Start of the scene
preset burst Scene Burst: 60 cubes
option cubes int 8 96 24 Cubes in the ring
option filter enum nearest,bilinear bilinear Texture filter
'''


class SimConsole:
    def __init__(self, pc, lport, seed=7, drop_rec=0.0, slow_ms=0.0):
        self.pc = pc
        self.s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.s.bind(('127.0.0.1', lport))
        self.rng = random.Random(seed)
        self.boot = 1
        self.t_boot = time.time()
        self.state = 0
        self.session = 0
        self.load = None
        self.chunks = {}
        self.report = b''
        self.report_run = 0
        self.report_state = 0
        self.report_exit = 0
        self.prog = None
        self.drop_rec = drop_rec       # the share of REC datagrams lost on the way (a gap test)
        self.slow_ms = slow_ms         # added to every drawing phase (the planted-delay control)
        self.stop = False
        self.lock = threading.Lock()
        threading.Thread(target=self.loop, daemon=True).start()

    def send(self, typ, session, a=0, b=0, payload=b''):
        self.s.sendto(cl.frame(typ, session, a, b, payload), self.pc)

    def hello_payload(self, role, run=0):
        p = struct.pack('<16I', self.boot, int((time.time() - self.t_boot) * 1000), self.state, self.session,
                        len(self.chunks), self.load['nchunks'] if self.load else 0, 0, 0, self.report_run,
                        self.report_state, len(self.report), zlib.crc32(self.report) & 0xFFFFFFFF,
                        self.report_exit & 0xFFFFFFFF, struct.unpack('<I', socket.inet_aton('127.0.0.1'))[0],
                        8 << 20, 0)
        p += (b'sim loader 0.2' if role == 1 else b'client').ljust(32, b'\0')
        p += struct.pack('<4I', 2, role, 2, 2120) + bytes(8) + struct.pack('<II', run, 2)
        return p

    # ---------------------------------------------------------------- loader
    def loop(self):
        last_beacon = 0
        self.s.settimeout(0.05)
        while not self.stop:
            if self.prog is None and time.time() - last_beacon > 1.0:
                self.send(cl.HELLO, 0, payload=self.hello_payload(1))
                last_beacon = time.time()
            try:
                d, _ = self.s.recvfrom(4096)
            except (socket.timeout, ConnectionResetError):
                continue
            except OSError:
                return
            m = cl.unframe(d)
            if m is None:
                continue
            with self.lock:
                if self.prog is not None:
                    self.prog.on(m)
                else:
                    self.on_loader(m)

    def on_loader(self, m):
        typ, sess, a, b, pl, _ = m
        if typ == cl.PING:
            self.send(cl.HELLO, 0, payload=self.hello_payload(1))
        elif typ == cl.LOAD:
            comp_len, raw_len, csize, nchunks, codec, flags, deadline, addr = struct.unpack_from('<IIIIBBHI', pl, 0)
            if sess != self.session or self.load is None:
                self.session, self.chunks, self.state = sess, {}, 1
                sha = pl[24:56]
                crc, alen = struct.unpack_from('<IH', pl, 56)
                self.load = {'comp_len': comp_len, 'raw_len': raw_len, 'nchunks': nchunks, 'codec': codec, 'sha': sha,
                             'crc': crc, 'args': pl[64:64 + alen].decode('latin-1'), 'verdict': None}
            self.ack()
        elif typ == cl.DATA and self.load and sess == self.session:
            self.chunks[a] = pl
            if len(self.chunks) % 4 == 0 or len(self.chunks) == self.load['nchunks']:
                self.ack()
        elif typ == cl.VERIFY and self.load and sess == self.session:
            self.verify()
        elif typ == cl.RUN and sess == self.session:
            if self.state != 2:
                self.send(cl.ERR, sess, cl.ERRORS and 8)
                return
            self.send(cl.EXEC, sess)
            self.state = 4
            self.prog = Program(self, sess, self.load['args'], self.load.get('raw', b''))
        elif typ == cl.REPQ and sess == self.report_run:
            for k in range(b):
                off = a + k * cl.CHUNK
                if off < len(self.report):
                    self.send(cl.REPORT, sess, off, 0, self.report[off:off + cl.CHUNK])
        elif typ == cl.REPDONE and sess == self.report_run:
            self.report_state = 0
            self.send(cl.HELLO, 0, payload=self.hello_payload(1))

    def ack(self):
        n = self.load['nchunks']
        cum = 0
        while cum in self.chunks:
            cum += 1
        bm = bytearray(min(256, (n - cum + 7) // 8 + 1))
        for i in self.chunks:
            k = i - cum
            if 0 <= k < len(bm) * 8:
                bm[k >> 3] |= 1 << (k & 7)
        self.send(cl.ACK, self.session, cum, len(bm), struct.pack('<IIII', len(self.chunks), 0, 0, n) + bytes(bm))
        if cum == n:
            self.verify()

    def verify(self):
        L = self.load
        if len(self.chunks) < L['nchunks']:
            return
        comp = b''.join(self.chunks[i] for i in range(L['nchunks']))
        code = 0
        if zlib.crc32(comp) & 0xFFFFFFFF != L['crc']:
            code = 1
        else:
            try:
                raw = zlib.decompress(comp, -15) if L['codec'] else comp
            except zlib.error:
                raw, code = b'', 2
            if not code and len(raw) != L['raw_len']:
                code = 3
            if not code and hashlib.sha256(raw).digest() != L['sha']:
                code = 4
            L['raw'] = raw
        self.state = 2 if code == 0 else 3
        self.send(cl.VERDICT, self.session, code, 0, cl.ERRORS.get(code, '?').encode())

    def back(self, state, code, report):
        """The program left: a new boot of the loader with the report pending."""
        self.prog = None
        self.boot += 1
        self.t_boot = time.time()
        self.state, self.load, self.chunks = 0, None, {}
        self.report_run, self.report_state, self.report_exit, self.report = self.session, state, code, report

    def close(self):
        self.stop = True
        if self.prog:
            self.prog.stop = True
        self.s.close()


class Program:
    def __init__(self, con, run, args, raw):
        self.con, self.run, self.args = con, run, args
        self.live = 'nolive' not in args
        self.secs = 3.0 if args.strip() else 15.0   # a push with no arguments (the MCP's push_build) runs longer
        for w in args.split():
            if w.startswith('secs='):
                self.secs = float(w[5:])
        self.report = [b'program sha256=%s bytes=%d args=%s\n' % (hashlib.sha256(raw).hexdigest().encode(), len(raw),
                                                                  args.encode())]
        self.sub = 0
        self.every = 1
        self.paused = False
        self.step_left = 0
        self.n = 0
        self.trace_seq = 0
        self.line_seq = 0
        self.stop = False
        self.reset = False
        self.grabs = []
        self.cubes = 24
        threading.Thread(target=self.main, daemon=True).start()

    def send(self, typ, a=0, b=0, payload=b''):
        self.con.send(typ, self.run, a, b, payload)

    def on(self, m):
        typ, sess, a, b, pl, _ = m
        if typ == cl.PING:
            self.hello()
        elif typ == cl.RESET:
            self.send(cl.RESETACK, self.run)
            self.reset = True
        elif not self.live:
            return
        elif typ == wire.SUB:
            self.sub, self.every = a, max(1, b)
            text = DEF.encode()
            self.send(wire.LIVEDEF, 1, 0 | (1 << 8), text)
        elif typ == wire.GRAB:
            self.grabs.append(a)
        elif typ == wire.CMD:
            ok, text = 0, b''
            if b == wire.CMD_PAUSE:
                self.paused = True
            elif b == wire.CMD_RESUME:
                self.paused, self.step_left = False, 0
            elif b == wire.CMD_STEP:
                self.paused, self.step_left = True, struct.unpack_from('<I', pl, 0)[0]
            elif b == wire.CMD_WARP:
                if pl.decode() not in ('start', 'burst'):
                    ok, text = 2, b'no preset ' + pl
                else:
                    self.trace(1, 0, 'warp ' + pl.decode())
            elif b == wire.CMD_OPTION:
                k, _, v = pl.decode().partition('=')
                if k == 'cubes':
                    self.cubes = int(v)
                else:
                    ok, text = 2, b'no option ' + k.encode()
            elif b == wire.CMD_PAD:
                pass
            else:
                ok = 1
            self.send(wire.CMDACK, a, ok, struct.pack('<I', self.n) + text)
        elif typ == cl.STILLACK:
            self.still_ack = (a, b)

    def hello(self):
        self.send(cl.HELLO, 0, 0, self.con.hello_payload(2, self.run))

    def line(self, text):
        self.send(cl.LINE, self.line_seq, 0, text.encode())
        self.line_seq += 1

    def log(self, lv, text):
        if self.sub & wire.CH_LOG:
            self.send(cl.LINE, self.line_seq, lv, struct.pack('<I', self.n) + text.encode())
            self.line_seq += 1

    def trace(self, lane, d, label):
        if self.sub & wire.CH_TRACE:
            self.send(wire.TRACE, self.trace_seq, 1, wire.pack_trace([(self.n, d, lane, label)]))
        self.trace_seq += 1

    def still(self, req):
        w, h = 160, 120
        pix = bytearray()
        for y in range(h):
            for x in range(w):
                v = (((x + self.n) // 20) & 1) * 0xF800 | ((y // 15) & 7) << 7
                pix += struct.pack('<H', v)
        crc = zlib.crc32(bytes(pix)) & 0xFFFFFFFF
        idx = len(self.report) + 100
        head = struct.pack('<8I', idx, self.n, w, h, 1, len(pix), crc, req)
        self.still_ack = None
        self.send(cl.STILL, cl.STILL_HEAD, idx, head)
        for off in range(0, len(pix), cl.CHUNK):
            self.send(cl.STILL, off, idx, bytes(pix[off:off + cl.CHUNK]))
        for _ in range(20):
            self.send(cl.STILL, cl.STILL_END, idx, head)
            time.sleep(0.05)
            if self.still_ack and self.still_ack[0] == idx and self.still_ack[1] == 0:
                return True
        return False

    def main(self):
        time.sleep(0.2)
        self.hello()
        self.line('simcubes: up, args "%s"' % self.args)
        t0 = time.time()
        recs = []
        rng = self.con.rng
        crash = 'crash' in self.args
        while not self.stop and not self.reset and time.time() - t0 < self.secs:
            time.sleep(1 / 60.0)
            while self.grabs:
                self.still(self.grabs.pop(0))
            if self.paused and self.step_left == 0:
                if recs and self.live and self.sub & wire.CH_REC:   # what was drawn before the pause goes out
                    self.send(wire.REC, recs[0][0], len(recs), wire.pack_recs(recs))
                    recs = []
                continue
            if self.step_left:
                self.step_left -= 1
            self.n += 1
            draw = 9.0 + 3.0 * (self.cubes / 24.0) + rng.random() * 2 + self.con.slow_ms
            if self.n % 97 == 0:
                draw += 8.0
            logic = 3.0 + rng.random()
            wait = max(0.0, 16.667 - draw - logic)
            frame = max(16.667, draw + logic)
            recs.append((self.n, [int(draw * 1000), int(logic * 1000), int(wait * 1000), int(frame * 1000)],
                         [self.cubes * 12, 0, 0], 3, [1.0 * self.n, 0.0, 2.5]))
            if self.n % 60 == 1:
                self.trace(1, 30, 'load ring %d' % (self.n // 60))
                self.log(1, 'picture %d, %d cubes' % (self.n, self.cubes))
            if self.n % 150 == 0:
                self.log(2, 'slow picture %d' % self.n)
            if len(recs) >= 3 and self.live and self.sub & wire.CH_REC:
                if rng.random() >= self.con.drop_rec:
                    keep = [r for r in recs if r[0] % self.every == 0]
                    self.send(wire.REC, keep[0][0] if keep else 0, len(keep), wire.pack_recs(keep))
                recs = []
            if self.n % 30 == 0:
                self.line('simcubes: picture %d' % self.n)
        if self.stop:
            return
        self.report.append(b'pictures %d\n@ Pictures = %d n\n' % (self.n, self.n))
        if self.reset:
            self.report.append(b'\nRESET by the PC\n')
            state, code = 6, 0
        elif crash:
            self.report.append(b'\nCRASH event 180 pc 8c0123a4 pr 8c012000 sp 8cfdff00\n')
            state, code = 3, -1
        else:
            state, code = 2, 0
        self.report.append(b'cl-link: up, lines %d\n' % self.line_seq)
        time.sleep(0.3)
        with self.con.lock:
            self.con.back(state, code, b''.join(self.report))
            self.con.send(cl.HELLO, 0, payload=self.con.hello_payload(1))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--pc', default='127.0.0.1:52060')
    ap.add_argument('--lport', type=int, default=52061)
    ap.add_argument('--slow-ms', type=float, default=0.0)
    a = ap.parse_args()
    h, _, p = a.pc.partition(':')
    c = SimConsole((h, int(p)), a.lport, slow_ms=a.slow_ms)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        c.close()


if __name__ == '__main__':
    main()
