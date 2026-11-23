#!/usr/bin/env python3
"""dcql_loop.py: dc-qalink's control loop, PC side (0.2). Python 3 standard library (+ the ssh client for --pi).

The console runs the loader disc (loader/). Over the broadband adapter (or the modem and a DreamPi) it beacons HELLO on
UDP 52060. This tool pushes a program to it, runs it, serves the program's files, takes its serial lines, stills and
report back, and brings the console back to the loader without a hand on it. Protocol: include/dcql_loop.h (version 2;
a loader of another version is refused, and it refuses this tool's datagrams the same way).

  dcql_loop.py --udp 192.0.2.50 status                  the loader's beacon: state, transport, pending report
  dcql_loop.py --udp 192.0.2.50 push prog.elf           send and verify only
  dcql_loop.py --udp 192.0.2.50 run prog.elf --args "x" --deadline 300 --files packs/ --out results/
                 (serves /pc/... from packs/, writes serial-<run>.txt, still-<run>-NNN.png and report-<run>.txt)
  dcql_loop.py --udp ... run prog.elf --reset-after 20    RESET the program after 20 s (then the restart ladder)
  dcql_loop.py --udp ... reset                            RESET whatever runs now
  dcql_loop.py --udp ... report --out results/            collect a pending report
  dcql_loop.py --udp ... run prog.elf --loader LOADER.BIN self-update first: push this loader if the console's differs
  dcql_loop.py power                                      the last rung: power-cycle the console (pc/power.py)
  Modem + DreamPi: --pi <ssh target> instead of --udp (a relay piped over ssh; nothing installed on the Pi).
  Flycast: `dcql_loop.py relay-local` first (binds 52060 before the loader beacons), then --udp 127.0.0.1:52059 --bind 0.

Program: an ELF (its PT_LOAD segments, loaded at 0x8C010000) or a raw 1ST_READ-style .bin. Deflate level 9 unless
--no-compress. Every run writes <out>/run-<session>.json (each step's seconds, rates, retransmits, the file service's
totals, the stills, the way back) beside the report.

The restart ladder when a program does not come back: RESET (the program's link thread jumps to the loader's return
stub) -> the watchdog (only if the program armed it: wdt=1) -> power cycle (pc/power.py: a plug adapter, or the
words "power cycle needed"). Exit codes: 0 ok, 2 no loader, 3 refused, 4 did not come back, 5 bad report,
6 power cycle needed.

Controls (tests): --corrupt stream|payload|declared sends a damaged image the console must refuse, then RUN anyway.
"""
import argparse, base64, hashlib, json, os, queue, random, shlex, socket, struct, subprocess, sys, threading, time, zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qlsafe   # noqa: E402  (the shared boundaries: roots, limits, the run's ending)

MAGIC = b'CL'
VERSION = 2
HDR = 16
DGRAM_MAX = 1200
CHUNK = 1024
PORT = 52060
LOAD_ADDR = 0x8C010000
HANDOFF_ADDR = 0x8CFE0000
IMAGE_END = HANDOFF_ADDR - 0x11000   # the loader's stack (arch_exec_at's trampoline) sits below the hand-off area
FREAD_MAX = 8 * 1024

HELLO, ACK, VERDICT, REPORT, EXEC, ERR = 0x01, 0x02, 0x03, 0x04, 0x05, 0x06
LINE, STILL, FOPEN, FREAD, FCLOSE, RESETACK = 0x07, 0x08, 0x09, 0x0A, 0x0B, 0x0C
LOAD, DATA, RUN, REPQ, REPDONE, PING, ABORT, VERIFY = 0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17
RESET, FINFO, FDATA, STILLACK = 0x18, 0x19, 0x1A, 0x1B
END, ENDACK, END_HEAD = 0x21, 0x1F, 0xFFFFFFFF   # ql-core4: the run's ending over the link before the full restart
TYPE_NAMES = {HELLO: 'HELLO', ACK: 'ACK', VERDICT: 'VERDICT', REPORT: 'REPORT', EXEC: 'EXEC', ERR: 'ERR', LINE: 'LINE',
              STILL: 'STILL', FOPEN: 'FOPEN', FREAD: 'FREAD', FCLOSE: 'FCLOSE', RESETACK: 'RESETACK', LOAD: 'LOAD',
              DATA: 'DATA', RUN: 'RUN', REPQ: 'REPQ', REPDONE: 'REPDONE', PING: 'PING', ABORT: 'ABORT',
              VERIFY: 'VERIFY', RESET: 'RESET', FINFO: 'FINFO', FDATA: 'FDATA', STILLACK: 'STILLACK', END: 'END',
              ENDACK: 'ENDACK'}
ERRORS = {0: 'ok', 1: 'stream crc', 2: 'inflate', 3: 'raw length', 4: 'sha256', 5: 'size', 6: 'load address',
          7: 'no memory', 8: 'not verified', 9: 'unknown session', 10: 'bad load', 11: 'codec', 12: 'no report',
          13: 'busy', 14: 'protocol version'}
PC_TYPES = frozenset((LOAD, DATA, RUN, REPQ, REPDONE, PING, ABORT, VERIFY, RESET, FINFO, FDATA, STILLACK,
                      0x1c, 0x1d, 0x1e, ENDACK, 0x2a, 0x2b))   # PC -> console (live: SUB, GRAB, CMD; ENDACK;
                                                                # disc: DINFO, DDATA)
STATES = {0: 'IDLE', 1: 'RECEIVING', 2: 'VERIFIED', 3: 'FAILED', 4: 'EXEC'}
RUN_STATES = {0: 'NONE', 1: 'RUNNING', 2: 'DONE', 3: 'CRASHED', 4: 'TIMEOUT', 5: 'LOST', 6: 'RESET'}
NETS = {0: '-', 1: 'modem', 2: 'adapter'}
STILL_HEAD, STILL_END = 0xFFFFFFFF, 0xFFFFFFFE
LOAD_F_LOADER = 1
HF_PUSHED, HF_STUB = 1, 2


class VersionMismatch(RuntimeError):
    pass


# ------------------------------------------------------------------------------------------------ framing
def frame(type_, session, a=0, b=0, payload=b'', version=VERSION):
    assert len(payload) <= DGRAM_MAX - HDR - 4
    h = MAGIC + bytes([version, type_]) + struct.pack('<IIHH', session & 0xFFFFFFFF, a & 0xFFFFFFFF, len(payload), b)
    body = h + payload
    return body + struct.pack('<I', zlib.crc32(body) & 0xFFFFFFFF)


def unframe(d, any_version=False):
    """(type, session, a, b, payload, version) or None when the datagram fails its checks."""
    if len(d) < HDR + 4 or len(d) > DGRAM_MAX or d[:2] != MAGIC:
        return None
    session, a, ln, b = struct.unpack_from('<IIHH', d, 4)
    if HDR + ln + 4 != len(d):
        return None
    if zlib.crc32(d[:HDR + ln]) & 0xFFFFFFFF != struct.unpack_from('<I', d, HDR + ln)[0]:
        return None
    if d[2] != VERSION and not any_version:
        return None
    return d[3], session, a, b, d[HDR:HDR + ln], d[2]


HELLO_MIN, HELLO_LEN = 96, 164   # the oldest loader's HELLO; the current one (include/dcql_loop.h CL_HELLO_LEN)


def decode_hello(d, want_role=None):
    """The one HELLO decoder for a whole datagram: the frame's length, CRC and protocol version checked
    (unframe), its type HELLO and a payload of at least HELLO_MIN bytes, then parse_hello. (hello dict, run) or None."""
    m = unframe(d)
    if m is None or m[0] != HELLO:
        return None
    try:
        h = parse_hello(m[4])
    except ValueError:
        return None
    if want_role is not None and h['role'] != want_role:
        return None
    return h, m[1]


def parse_hello(p):
    """A HELLO payload, every length from the oldest loader's 96 bytes to the current 164: fields a shorter HELLO
    lacks read as None (or their old defaults). ValueError when it is shorter than HELLO_MIN."""
    if len(p) < HELLO_MIN:
        raise ValueError('HELLO of %d bytes, under %d' % (len(p), HELLO_MIN))
    v = struct.unpack_from('<16I', p, 0)
    keys = ('boot_count', 'uptime_ms', 'state', 'session', 'have', 'nchunks', 'rx_bytes', 'rx_bad', 'report_run',
            'report_state', 'report_len', 'report_crc', 'report_exit', 'console_ip', 'max_raw', 'verdict')
    h = dict(zip(keys, v))
    h['report_exit'] = struct.unpack('<i', struct.pack('<I', h['report_exit']))[0]
    h['verdict'] = struct.unpack('<i', struct.pack('<I', h['verdict']))[0]
    h['build'] = p[64:96].split(b'\0', 1)[0].decode('latin-1')
    h['console_ip'] = socket.inet_ntoa(struct.pack('<I', h['console_ip']))
    if len(p) >= 128:
        proto, role, net, stub = struct.unpack_from('<4I', p, 96)
        h.update(proto=proto, role=role, net=NETS.get(net, net), stub_bytes=stub, loader_sha8=p[112:120].hex(),
                 run_id=struct.unpack_from('<I', p, 120)[0], flags=struct.unpack_from('<I', p, 124)[0])
    else:
        h.update(proto=1, role=1, net='modem', stub_bytes=0, loader_sha8='', run_id=0, flags=0)
    # additive v2 fields (protocol stays 2): the adapter MAC and GDEMU slot (ql-core2, 136 bytes), then the stub's
    # record of the last way back and GDEMU's version (ql-data, 148 bytes); absent fields read as None / no record
    mac = p[128:134] if len(p) >= 136 else b''
    h['mac'] = ':'.join('%02x' % b for b in mac) if mac and any(mac) else None
    h['gdemu_slot'] = struct.unpack_from('<H', p, 134)[0] if len(p) >= 136 else None
    if len(p) >= 148:
        step, entries, gd, err, ver = struct.unpack_from('<BBHII', p, 136)
        h['stub'] = {'step': step, 'step_name': STUB_STEPS.get(step, str(step)), 'err': err, 'gdemu': gd,
                     'entries': entries} if step else None
        h['gdemu_ver'] = '%d.%d.%d' % (ver >> 16, (ver >> 8) & 255, ver & 255) if ver else None
    else:
        h['stub'] = None
        h['gdemu_ver'] = None
    # ql-core3 (164 bytes): where the way-back record came through and the rest of it (CL_HELLO_WB). Raw words so
    # every reader decodes the same values; tools/first_console.py words them
    if len(p) >= HELLO_LEN:
        step, entries, gd, err, ver, where, aux, run, sense = struct.unpack_from('<BBHIIIIII', p, 136)
        h['way_back'] = {'step': step, 'entries': entries, 'gdemu': gd, 'err': err, 'gdemu_ver': ver, 'where': where,
                         'aux': aux, 'run': run, 'sense': sense}
    else:
        h['way_back'] = None
    return h


# the stub's steps (include/dcql_loop.h CL_STUB_STEP_*), and the screen colour each shows on the TV
STUB_STEPS = {1: 'entered (blue)', 2: 'quiet (cyan)', 3: 'GD init (yellow)', 4: 'read (orange)', 5: 'SHA ok (green)',
              6: 'jump (white)', 7: 'reboot (red)', 8: 'GDEMU select (magenta)'}


def stub_text(s):
    """One line for the stub's record: where the last way back stopped."""
    if not s:
        return 'no stub record'
    t = 'stub step %d %s err %x' % (s['step'], s['step_name'], s['err'])
    if s['err'] & 0x8000:
        t += ' (GDEMU select refused, code %d)' % ((s['err'] >> 16) & 255)
    if s['gdemu']:
        t += ' gdemu %x' % s['gdemu']
    return t + ' entries %d' % s['entries']


# ------------------------------------------------------------------------------------------------ program images
def elf_image(data):
    """Flat image of an SH-4 ELF's PT_LOAD file bytes (BSS excluded: KallistiOS clears it): (load address, bytes)."""
    if data[:4] != b'\x7fELF' or data[4] != 1 or data[5] != 1:
        raise ValueError('not a 32-bit little-endian ELF')
    phoff, = struct.unpack_from('<I', data, 28)
    phentsize, phnum = struct.unpack_from('<HH', data, 42)
    segs = []
    for i in range(phnum):
        t, off, va, pa, fs, ms, fl, al = struct.unpack_from('<8I', data, phoff + i * phentsize)
        if t == 1 and fs:
            segs.append((pa & 0x1FFFFFFF | 0x80000000 if pa else va, data[off:off + fs]))
    if not segs:
        raise ValueError('no loadable segment')
    lo = min(s[0] for s in segs)
    hi = max(s[0] + len(s[1]) for s in segs)
    img = bytearray(hi - lo)
    for va, b in segs:
        img[va - lo:va - lo + len(b)] = b
    return lo, bytes(img)


def load_program(path):
    data = open(path, 'rb').read()
    if data[:4] == b'\x7fELF':
        lo, img = elf_image(data)
        if lo != LOAD_ADDR:
            raise ValueError('%s loads at %08x, the loader runs programs at %08x' % (path, lo, LOAD_ADDR))
        return img
    return data


class Image:
    def __init__(self, raw, compress=True, corrupt=None):
        self.raw = raw
        self.sha = hashlib.sha256(raw).digest()
        self.codec = 1 if compress else 0
        self.comp = self._pack(raw)
        self.crc = zlib.crc32(self.comp) & 0xFFFFFFFF
        self.corrupt = corrupt
        if corrupt == 'stream':      # damaged after the stream CRC was taken: the console's stream CRC must refuse it
            self.comp = flip(self.comp)
        elif corrupt == 'payload':   # a self-consistent stream of different bytes under the declared SHA-256
            self.comp = self._pack(flip(raw))
            self.crc = zlib.crc32(self.comp) & 0xFFFFFFFF
        elif corrupt == 'declared':  # the right bytes, the wrong declared hash
            self.sha = hashlib.sha256(raw + b'x').digest()
        elif corrupt:
            raise ValueError('--corrupt stream|payload|declared')
        self.nchunks = (len(self.comp) + CHUNK - 1) // CHUNK

    def _pack(self, raw):
        if not self.codec:
            return raw
        c = zlib.compressobj(9, zlib.DEFLATED, -15, 9)
        return c.compress(raw) + c.flush()

    def chunk(self, i):
        return self.comp[i * CHUNK:(i + 1) * CHUNK]

    def load_payload(self, args='', deadline=0, flags=0):
        a = args.encode('latin-1')[:512]
        p = struct.pack('<IIIIBBHI', len(self.comp), len(self.raw), CHUNK, self.nchunks, self.codec, flags,
                        deadline & 0xFFFF, LOAD_ADDR) + self.sha + struct.pack('<IHH', self.crc, len(a), 0)
        assert len(p) == 64
        return p + a


def flip(b):
    b = bytearray(b)
    i = len(b) // 2
    b[i] ^= 0x5A
    return bytes(b)


def png_rgb565(path, w, h, data):
    """An RGB565 picture (little-endian words, row-major) as an 8-bit RGB PNG (zlib only)."""
    rows = bytearray()
    for y in range(h):
        rows.append(0)
        r = data[y * w * 2:(y + 1) * w * 2]
        for x in range(w):
            v = r[2 * x] | (r[2 * x + 1] << 8)
            rows += bytes(((v >> 11) * 255 // 31, (v >> 5 & 63) * 255 // 63, (v & 31) * 255 // 31))

    def chunk(t, d):
        return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xFFFFFFFF)
    png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
    png += chunk(b'IDAT', zlib.compress(bytes(rows), 6)) + chunk(b'IEND', b'')
    open(path, 'wb').write(png)


# ------------------------------------------------------------------------------------------------ transports
RELAY = r'''
import base64, os, select, socket, sys
port = int(sys.argv[1]) if len(sys.argv) > 1 else 52060
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('0.0.0.0', port))
peer = None
w = sys.stdout
w.write('I relay up udp %d\n' % port); w.flush()
buf = b''
while True:
    r, _, _ = select.select([s, 0], [], [], 5.0)
    if s in r:
        d, a = s.recvfrom(4096)
        if a != peer:
            peer = a
            w.write('I console %s:%d\n' % a)
        w.write('R ' + base64.b64encode(d).decode() + '\n'); w.flush()
    if 0 in r:
        c = os.read(0, 65536)
        if not c:
            break
        buf += c
        while b'\n' in buf:
            line, buf = buf.split(b'\n', 1)
            if line.startswith(b'S ') and peer:
                s.sendto(base64.b64decode(line[2:]), peer)
'''


def big_buffers(s):
    for opt in (socket.SO_RCVBUF, socket.SO_SNDBUF):
        try:
            s.setsockopt(socket.SOL_SOCKET, opt, 4 << 20)
        except OSError:
            pass


class UdpTransport:
    """--udp host[:port]: that console. --udp auto: whichever console beacons first (a loader whose LINK.TXT names
    this PC, or `host peer`): the peer is learnt from the first well-formed datagram, until then sends are broadcast."""
    def __init__(self, remote, bind_port):
        host, _, port = remote.partition(':')
        self.auto = host == 'auto'
        self.remote = ('255.255.255.255' if self.auto else host, int(port or PORT))
        self.s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if self.auto:
            self.s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        big_buffers(self.s)
        self.s.bind(('127.0.0.1' if host.startswith('127.') else '0.0.0.0', bind_port))
        self.peer = None

    def send(self, d):
        self.s.sendto(d, self.peer or self.remote)

    def recv(self, timeout):
        self.s.settimeout(max(timeout, 0.001))
        try:
            d, a = self.s.recvfrom(4096)
            m = unframe(d, any_version=True)
            if m is not None and m[0] in PC_TYPES:
                return None   # our own datagram (a broadcast Windows loops back to this socket): never the console's
            if self.auto and self.peer is None:
                # learn the console only from a loader's HELLO (first console run, 2026-11-22: the PC's own looped-back
                # broadcast was taken as the console and every LOAD went to this PC)
                if m is None or m[0] != HELLO or len(m[4]) < 96:
                    return None
                h = parse_hello(m[4])
                if h['role'] == 2:
                    return None
                self.peer = a
                print('%s console at %s:%d (the loader says its address is %s)' % (time.strftime('%H:%M:%S'), a[0], a[1],
                                                                                 h['console_ip']), flush=True)
            return d
        except (socket.timeout, BlockingIOError):
            return None
        except ConnectionResetError:   # Windows: ICMP port unreachable
            return None

    def close(self):
        self.s.close()


def relay_local(port, ctl, log):
    """The ssh relay's job as a standing process on this PC (Flycast). Binds UDP <port> (the loader's) before the loader
    beacons and keeps it bound: a Windows socket that once took an ICMP port-unreachable (a beacon to a closed port)
    stops passing datagrams down in Flycast. Console datagrams go to the newest client on 127.0.0.1:<ctl>; a client's
    go to the console. Beacons with no client are dropped. Clients: dcql_loop.py --udp 127.0.0.1:<ctl> --bind 0 ..."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    big_buffers(s)
    s.bind(('0.0.0.0', port))
    c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    big_buffers(c)
    c.bind(('127.0.0.1', ctl))
    import selectors
    sel = selectors.DefaultSelector()
    sel.register(s, selectors.EVENT_READ)
    sel.register(c, selectors.EVENT_READ)
    console = client = None
    log('relay-local: udp %d (console side), 127.0.0.1:%d (clients)' % (port, ctl))
    while True:
        for key, _ in sel.select(5.0):
            try:
                d, a = key.fileobj.recvfrom(4096)
            except (ConnectionResetError, OSError):
                continue
            if key.fileobj is s:
                if a != console:
                    console = a
                    log('relay-local: console at %s:%d' % a)
                if client:
                    try:
                        c.sendto(d, client)
                    except OSError:
                        pass
            else:
                if a != client:
                    client = a
                    log('relay-local: client %s:%d' % a)
                if console:
                    s.sendto(d, console)


class SshRelayTransport:
    def __init__(self, pi, port, log):
        code = base64.b64encode(RELAY.encode()).decode()
        remote = 'python3 -u -c %s %d' % (shlex.quote('import base64;exec(base64.b64decode("%s"))' % code), port)
        self.p = subprocess.Popen(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-o',
                                   'ServerAliveInterval=15', pi, remote],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
        self.q = queue.Queue()
        self.log = log
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        for line in self.p.stdout:
            line = line.rstrip(b'\r\n')
            if line.startswith(b'R '):
                self.q.put(base64.b64decode(line[2:]))
            elif line.startswith(b'I '):
                self.log('relay: ' + line[2:].decode('latin-1'))
        self.q.put(None)

    def send(self, d):
        self.p.stdin.write(b'S ' + base64.b64encode(d) + b'\n')
        self.p.stdin.flush()

    def recv(self, timeout):
        try:
            d = self.q.get(timeout=max(timeout, 0.001))
        except queue.Empty:
            return None
        if d is None:
            raise RuntimeError('the ssh relay ended (is the Pi reachable, and python3 on it?)')
        return d

    def close(self):
        try:
            self.p.stdin.close()
            self.p.terminate()
        except OSError:
            pass


# ------------------------------------------------------------------------------------------------ the program's side
def disc_service(a, stamp, out):
    """The network disc (pc/dcql_disc.py) from --disc / --overlay / --whole, or None. Its reads go to
    <out>/disc-reads.jsonl (file, offset, bytes, ms, side)."""
    if not getattr(a, 'disc', None) and not getattr(a, 'overlay', None):
        return None
    import dcql_disc
    os.makedirs(out, exist_ok=True)
    f = open(os.path.join(out, 'disc-reads.jsonl'), 'a', encoding='utf-8')
    t0 = time.time()

    def record(r):
        r.setdefault('s', round(time.time() - t0, 3))
        f.write(json.dumps(r, separators=(',', ':')) + '\n')
        f.flush()
    svc = dcql_disc.DiscService(a.disc, a.overlay, a.whole, stamp, record)
    m = svc.manifest()
    json.dump(m, open(os.path.join(out, 'disc-manifest.json'), 'w'), indent=1)
    stamp('network disc: %s, %d files, %d from the PC, manifest %s' % (m['mode'], m['count'], m['served'],
                                                                     m['sha256'][:16]))
    return svc


class FileService:
    """/pc/<path> for the running program: read-only, below one root folder, no '..'. Answers FOPEN with FINFO and
    FREAD with FDATA blocks (the client re-asks for what it lost)."""

    def __init__(self, root, log):
        self.root = os.path.abspath(root) if root else None
        self.log = log
        self.handles = {}
        self.next = 1
        self.stats = {'opened': 0, 'refused': 0, 'reads': 0, 'bytes': 0, 'first_at': None, 'last_at': None, 'files': {}}

    OPEN_MAX = 32    # open files a program may hold at once (each is a descriptor on this PC)

    def path(self, rel):
        """The real path of /pc/<rel> when its target, links followed, is a file below the root; else None."""
        return qlsafe.confine(self.root, rel)

    def on_open(self, loop, run, req, payload):
        rel = payload.decode('latin-1', 'replace')
        p = self.path(rel)
        now = time.time()
        if self.log and (rel not in self.stats['files'] or p is None):
            self.log('file service: %s %s' % (rel, 'refused' if p is None else 'open'))
        if p is None:
            self.stats['refused'] += 1
            loop.send(FINFO, run, req, 0, struct.pack('<iIII', -2, 0, 0, 0))
            return
        key = os.path.normcase(p)
        h = next((k for k, v in self.handles.items() if v[2] == key and v[3] == req), None)
        if h is None and len(self.handles) >= self.OPEN_MAX:   # a cap on descriptors, before one more is opened
            self.stats['refused'] += 1
            self.stats['too_many'] = self.stats.get('too_many', 0) + 1
            loop.send(FINFO, run, req, 0, struct.pack('<iIII', -24, 0, 0, 0))
            return
        if h is None:   # a repeated FOPEN (lost answer) keeps its handle
            h = self.next
            self.next += 1
            f = open(p, 'rb')
            self.handles[h] = (f, os.path.getsize(p), key, req)
            self.stats['opened'] += 1
            self.stats['files'].setdefault(rel, 0)
            self.stats['first_at'] = self.stats['first_at'] or now
        loop.send(FINFO, run, req, 0, struct.pack('<iIII', 0, h, self.handles[h][1], 0))

    def on_read(self, loop, run, req, payload):
        if len(payload) < 12:
            return
        h, off, n = struct.unpack_from('<III', payload, 0)
        if h not in self.handles or n > FREAD_MAX:
            return
        f, size = self.handles[h][0], self.handles[h][1]
        f.seek(off)
        data = f.read(min(n, max(0, size - off)))
        for k in range(0, len(data), CHUNK):
            loop.send(FDATA, run, req, k // CHUNK, data[k:k + CHUNK])
        self.stats['reads'] += 1
        self.stats['bytes'] += len(data)
        self.stats['last_at'] = time.time()

    def on_close(self, h):
        v = self.handles.pop(h, None)
        if v:
            v[0].close()

    def summary(self):
        s = dict(self.stats)
        s['files'] = len(s['files'])
        if s['first_at'] and s['last_at']:
            s['seconds'] = round(s['last_at'] - s['first_at'], 3)
            s['Bps'] = round(s['bytes'] / max(s['last_at'] - s['first_at'], 1e-6), 1)
        s.pop('first_at')
        s.pop('last_at')
        return s


PIX_RGB565 = 1
STILL_W_MAX, STILL_H_MAX = 1024, 1024        # the loader sends at most 640 x 480 (loader/client.c still_send)
STILL_PENDING = 4                            # stills being received at once, per receiver
STILL_PENDING_BYTES = 4 << 20                # and their bytes together


def still_head(payload):
    """A STILL head (cl_still_head_t) checked before anything is allocated for it: 32 bytes, RGB565, a picture of
    1..1024 x 1..1024 whose byte count is exactly width x height x 2. A dict, or None."""
    if len(payload) < 32:
        return None
    index, picture, w, h, fmt, nbytes, crc, req = struct.unpack_from('<8I', payload, 0)
    if fmt != PIX_RGB565 or not 0 < w <= STILL_W_MAX or not 0 < h <= STILL_H_MAX or nbytes != w * h * 2:
        return None
    return {'index': index, 'picture': picture, 'w': w, 'h': h, 'bytes': nbytes, 'crc': crc, 'req': req}


def still_room(cur, nbytes, replacing=None):
    """Whether one more still of nbytes fits the pending budget (count and bytes), `replacing` not counted."""
    live = [s for k, s in cur.items() if k != replacing and not s['done']]
    return len(live) < STILL_PENDING and sum(s['bytes'] for s in live) + nbytes <= STILL_PENDING_BYTES


class StillSink:
    def __init__(self, out, run, log):
        self.out, self.run, self.log = out, run, log
        self.cur = {}
        self.saved = []
        self.refused = 0

    def on_still(self, loop, a, b, payload):
        idx = b
        st = self.cur.get(idx)
        if a == STILL_HEAD:
            hd = still_head(payload)
            if hd is None:
                self.refused += 1
                return
            if st is None or st['bytes'] != hd['bytes']:
                if not still_room(self.cur, hd['bytes'], idx):
                    self.refused += 1
                    return
                self.cur[idx] = st = {'picture': hd['picture'], 'w': hd['w'], 'h': hd['h'], 'bytes': hd['bytes'],
                                      'crc': hd['crc'], 'data': bytearray(hd['bytes']), 'have': set(),
                                      't0': time.time(), 'done': False}
            return
        if st is None:
            return
        if a == STILL_END:
            nblocks = (st['bytes'] + CHUNK - 1) // CHUNK
            bits = bytearray((nblocks + 7) // 8)
            for k in st['have']:
                bits[k >> 3] |= 1 << (k & 7)
            missing = nblocks - len(st['have'])
            if not missing and not st['done']:
                ok = (zlib.crc32(bytes(st['data'])) & 0xFFFFFFFF) == st['crc']
                name = 'still-%08x-%03d-p%05d.png' % (self.run, idx, st['picture'])
                if ok and self.out:
                    os.makedirs(self.out, exist_ok=True)
                    png_rgb565(os.path.join(self.out, name), st['w'], st['h'], bytes(st['data']))
                st['done'] = True
                st['data'] = None   # held as a PNG now: only its block set stays, for a repeated END
                self.saved.append({'index': idx, 'picture': st['picture'], 'crc_ok': ok, 'file': name if ok else None,
                                   'seconds': round(time.time() - st['t0'], 3), 'bytes': st['bytes']})
                self.log('still %d (picture %d) %s, %d bytes in %.2f s' % (idx, st['picture'], 'saved' if ok else
                                                                           'CRC MISMATCH', st['bytes'], time.time() - st['t0']))
            loop.send(STILLACK, self.run, idx, min(missing, 0xFFFF), bytes(bits[:DGRAM_MAX - HDR - 4]))
            return
        k = a // CHUNK
        if not st['done'] and a % CHUNK == 0 and a + len(payload) <= st['bytes']:
            st['data'][a:a + len(payload)] = payload
            st['have'].add(k)


# ------------------------------------------------------------------------------------------------ the session
class Loop:
    def __init__(self, transport, log=print, verbose=False):
        self.t = transport
        self.log = log
        self.verbose = verbose
        self.wmax = 16
        self.hello = None
        self.client_hello = None
        self.stats = {'tx_pkts': 0, 'tx_bytes': 0, 'rx_pkts': 0, 'rx_bytes': 0, 'rx_bad': 0}
        self.on_other = None   # called with messages the waits do not consume (lines, files, stills during a run)

    def send(self, type_, session, a=0, b=0, payload=b''):
        d = frame(type_, session, a, b, payload)
        self.t.send(d)
        self.stats['tx_pkts'] += 1
        self.stats['tx_bytes'] += len(d)

    def recv(self, timeout):
        d = self.t.recv(timeout)
        if d is None:
            return None
        self.stats['rx_pkts'] += 1
        self.stats['rx_bytes'] += len(d)
        m = unframe(d, any_version=True)
        if m is None:
            self.stats['rx_bad'] += 1
            return None
        if m[5] != VERSION:
            raise VersionMismatch('the console speaks control-loop protocol %d; this tool speaks %d: refused '
                                  '(use the dc-qalink release that matches the loader disc)' % (m[5], VERSION))
        if m[0] == HELLO and len(m[4]) >= 96:
            h = parse_hello(m[4])
            h['at'] = time.time()
            if h['role'] == 2:
                self.client_hello = h
            else:
                self.hello = h
        if m[0] == ERR and m[2] == 14:
            raise VersionMismatch('the console refused this tool: protocol version (it speaks another)')
        if self.verbose:
            self.log('  <- %s s=%08x a=%d b=%d len=%d' % (TYPE_NAMES.get(m[0], m[0]), m[1], m[2], m[3], len(m[4])))
        return m[:5]

    def wait(self, pred, timeout, every=None, poke=None):
        """Receives until pred(msg) returns non-None; calls poke() every `every` seconds."""
        end = time.time() + timeout
        last = 0.0
        while time.time() < end:
            if poke and every and time.time() - last >= every:
                poke()
                last = time.time()
            m = self.recv(min(0.2, max(0.0, end - time.time())))
            if m is not None:
                r = pred(m)
                if r is not None:
                    return r
                if self.on_other:
                    self.on_other(m)
        return None

    def wait_hello(self, timeout, pred=lambda h: True):
        def p(m):
            if m[0] == HELLO and self.hello and self.hello.get('at', 0) >= time.time() - 1 and pred(self.hello) and \
                    parse_hello(m[4])['role'] != 2:
                return self.hello
            return None
        return self.wait(p, timeout, every=5.0, poke=lambda: self.send(PING, 0))

    # -------------------------------------------------------------------------------------------- transfer
    def transfer(self, img, session, args='', deadline=0, link_wait=180.0, flags=0):
        t0 = time.time()
        acked = bytearray(img.nchunks)
        sent_at, sent_n, sent_seq = {}, {}, {}
        inflight = set()
        retx = []                 # chunks to send again, oldest first
        seq = 0                   # send order, for loss detection from the ack bitmap
        window, wmax, wmin = 4.0, float(self.wmax), 2.0
        srtt, rto, min_rtt = None, 2.0, 1e9
        resent = fast = timeouts = 0
        cut_at = 0.0
        last_ack = time.time()
        ack_info = {}
        load = img.load_payload(args, deadline, flags)

        def handshake(wait_s):
            return self.wait(lambda m: m if m[1] == session and m[0] in (ACK, VERDICT, ERR) else None, wait_s,
                             every=2.0, poke=lambda: self.send(LOAD, session, payload=load))

        def cut(now):
            nonlocal window, cut_at
            if now - cut_at > (srtt or 1.0):
                window = max(wmin, window / 2)
                cut_at = now

        m = handshake(30.0)
        if m is None:
            raise RuntimeError('no answer to LOAD within 30 s')
        pending = [m]
        nxt = 0
        verdict = None
        while True:
            for m in pending:
                if m[0] == ERR:
                    raise RuntimeError('console refused the load: %s' % ERRORS.get(m[2], m[2]))
                if m[0] == VERDICT:
                    verdict = (m[2], m[4].decode('latin-1'))
                if m[0] == ACK and m[1] == session:
                    last_ack = time.time()
                    cum = m[2]
                    have, bad, dup, n = struct.unpack_from('<IIII', m[4], 0)
                    ack_info = {'have': have, 'bad_crc': bad, 'dup': dup}
                    bm = m[4][16:16 + m[3]]
                    if have < sum(acked):
                        self.log('console holds %d chunks, %d were acknowledged: restarting from its view' %
                                 (have, sum(acked)))
                        acked = bytearray(img.nchunks)
                        inflight.clear()
                        retx.clear()
                        sent_n.clear()
                        nxt = 0
                    newly = [i for i in range(min(cum, img.nchunks)) if not acked[i]]
                    for k in range(len(bm) * 8):
                        i = cum + k
                        if i < img.nchunks and (bm[k >> 3] >> (k & 7)) & 1 and not acked[i]:
                            newly.append(i)
                    now = time.time()
                    top = -1
                    for i in newly:
                        acked[i] = 1
                        inflight.discard(i)
                        top = max(top, sent_seq.get(i, -1))
                        if sent_n.get(i) == 1:
                            sample = now - sent_at[i]
                            srtt = sample if srtt is None else 0.875 * srtt + 0.125 * sample
                            min_rtt = min(min_rtt, sample)
                        queue_s = (srtt or 0.0) - min_rtt
                        if queue_s < 1.0:
                            window = min(wmax, window + 1.0 / window)
                        elif queue_s > 2.0:
                            window = max(wmin, window - 1.0 / window)
                    if srtt is not None:
                        rto = max(0.25 if min_rtt < 0.05 else 1.5, 2.5 * srtt)
                    for i in sorted(inflight, key=lambda j: sent_seq[j]):
                        if sent_seq[i] + 3 <= top:
                            inflight.discard(i)
                            retx.append(i)
                            fast += 1
                            cut(now)
            pending = []
            if verdict is not None:
                break
            now = time.time()
            for i in sorted(inflight, key=lambda j: sent_seq[j]):
                if now - sent_at[i] > rto:
                    inflight.discard(i)
                    retx.append(i)
                    timeouts += 1
                    cut(now)
            if all(acked):
                m = self.wait(lambda m: m if m[1] == session and m[0] in (VERDICT, ERR) else None, 60.0,
                              every=2.0, poke=lambda: self.send(VERIFY, session))
                if m is None:
                    raise RuntimeError('all chunks acknowledged but no verdict')
                pending = [m]
                continue
            while len(inflight) < int(window):
                i = None
                while retx:
                    j = retx.pop(0)
                    if not acked[j] and j not in inflight:
                        i = j
                        break
                if i is None:
                    while nxt < img.nchunks and (acked[nxt] or nxt in inflight or sent_n.get(nxt)):
                        nxt += 1
                    if nxt >= img.nchunks:
                        break
                    i = nxt
                    nxt += 1
                if sent_n.get(i):
                    resent += 1
                self.send(DATA, session, i, 0, img.chunk(i))
                sent_at[i] = time.time()
                sent_n[i] = sent_n.get(i, 0) + 1
                sent_seq[i] = seq
                seq += 1
                inflight.add(i)
            if time.time() - last_ack > 10.0:
                self.log('no ack for 10 s (have %d/%d): waiting for the loader, then resuming' % (sum(acked), img.nchunks))
                if self.wait_hello(link_wait) is None:
                    raise RuntimeError('the loader did not come back within %.0f s' % link_wait)
                m = handshake(30.0)
                if m is None:
                    raise RuntimeError('no answer to the resume LOAD')
                retx.extend(sorted(i for i in inflight))
                inflight.clear()
                pending = [m]
                last_ack = time.time()
                continue
            m = self.recv(0.02)
            if m is not None:
                pending = [m]
        t1 = time.time()
        return {'session': '%08x' % session, 'raw_bytes': len(img.raw), 'comp_bytes': len(img.comp),
                'chunks': img.nchunks, 'resent': resent, 'fast_resends': fast, 'timeouts': timeouts,
                'seconds': round(t1 - t0, 3), 'goodput_Bps': round(len(img.comp) / max(t1 - t0, 1e-6), 1),
                'raw_Bps': round(len(img.raw) / max(t1 - t0, 1e-6), 1), 'srtt_s': round(srtt or 0, 4),
                'min_rtt_s': round(min_rtt if min_rtt < 1e9 else 0, 4), 'final_window': round(window, 2),
                'console_ack': ack_info, 'verdict': verdict[0], 'verdict_text': verdict[1]}

    def run_verified(self, session):
        r = self.wait(lambda m: m if m[1] == session and m[0] in (EXEC, ERR) else None, 15.0,
                      every=2.0, poke=lambda: self.send(RUN, session))
        if r is None:
            return 'no answer'
        if r[0] == ERR and r[2] == 8:
            # "not verified" for a RUN we re-sent: the first RUN may have run the program and its EXEC been lost (the
            # loader that answers is the next boot). Its beacon says whether this session's report is the last run.
            h = self.wait_hello(6.0)
            if h and h.get('report_run') == session and h.get('report_state', 0) != 0:
                self.log('RUN %08x refused as not verified by a loader whose last run it is: it ran (EXEC lost)' % session)
                self.ran_back = h
                return 'exec'
        return 'exec' if r[0] == EXEC else 'refused: %s' % ERRORS.get(r[2], r[2])

    def fetch_report(self, h, out_dir):
        run, length, crc = h['report_run'], h['report_len'], h['report_crc']
        blocks = {}
        need = [o for o in range(0, max(length, 1), CHUNK)] if length else []
        tries = 0
        t0 = time.time()
        while need and tries < 30:
            tries += 1
            first = need[0]
            count = min(8, len(need))
            self.send(REPQ, run, first, count)
            end = time.time() + 3.0 + count * 1.0
            last = None
            while time.time() < end:
                m = self.recv(0.2)
                if m and m[0] == REPORT and m[1] == run:
                    blocks[m[2]] = m[4]
                    last = time.time()
                    if all(o in blocks for o in need[:count]):
                        break
                elif last and time.time() - last > 2.5:
                    break
                if m and m[0] == ERR and m[1] == run:
                    raise RuntimeError('console: %s' % ERRORS.get(m[2], m[2]))
            need = [o for o in need if o not in blocks]
        if need:
            raise RuntimeError('report incomplete after %d requests' % tries)
        data = b''.join(blocks[o] for o in sorted(blocks))[:length]
        secs = time.time() - t0
        ok = (zlib.crc32(data) & 0xFFFFFFFF) == crc and len(data) == length
        path = None
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
            path = os.path.join(out_dir, 'report-%08x.txt' % run)
            open(path, 'wb').write(data)
        if ok:
            self.wait(lambda m: m if m[0] == HELLO and self.hello['report_state'] == 0 else None, 10.0,
                      every=2.0, poke=lambda: self.send(REPDONE, run))
        return {'run': '%08x' % run, 'state': RUN_STATES.get(h['report_state'], h['report_state']),
                'exit': h['report_exit'], 'bytes': len(data), 'crc_ok': ok, 'path': path,
                'seconds': round(secs, 3), 'Bps': round(len(data) / max(secs, 1e-6), 1), 'requests': tries,
                'text_head': data[:400].decode('latin-1')}

    def push_loader(self, path, stamp):
        """Self-update: send a newer loader (CL_LOAD_F_LOADER) and wait for it to beacon as the pushed loader."""
        raw = load_program(path)
        sha8 = hashlib.sha256(raw).hexdigest()[:16]
        h = self.hello
        if h and h.get('loader_sha8') == sha8:
            stamp('loader up to date (%s%s)' % (sha8, ', pushed' if h['flags'] & HF_PUSHED else ', from the disc'))
            return {'pushed': False, 'sha8': sha8}
        img = Image(raw)
        session = random.getrandbits(32) | 1
        res = self.transfer(img, session, '', 0, flags=LOAD_F_LOADER)
        if res['verdict'] != 0:
            raise RuntimeError('the console refused the loader: %s' % res['verdict_text'])
        t0 = time.time()
        self.run_verified(session)
        hb = self.wait_hello(60.0, lambda h: h.get('loader_sha8') == sha8 and h['flags'] & HF_PUSHED)
        if hb is None:
            raise RuntimeError('the pushed loader did not beacon within 60 s')
        stamp('loader %s pushed: %d bytes in %.2f s, beaconing %.2f s after RUN' % (sha8, len(raw), res['seconds'],
                                                                                   time.time() - t0))
        return {'pushed': True, 'sha8': sha8, 'transfer_s': res['seconds'], 'back_s': round(time.time() - t0, 3)}


def describe(h):
    return ('loader "%s" proto %d %s boot %d up %.0f s ip %s state %s session %08x %d/%d chunks; stub %d B%s; '
            'last run %08x %s, report %d bytes' %
            (h['build'], h['proto'], h['net'], h['boot_count'], h['uptime_ms'] / 1000.0, h['console_ip'],
             STATES.get(h['state'], h['state']), h['session'], h['have'], h['nchunks'], h['stub_bytes'],
             ' (pushed loader)' if h['flags'] & HF_PUSHED else '', h['report_run'],
             RUN_STATES.get(h['report_state'], '?'), h['report_len'])) + \
        ('; mac %s slot %s gdemu %s; %s' % (h.get('mac') or 'none', h.get('gdemu_slot') or 'none',
                                            h.get('gdemu_ver') or 'none', stub_text(h.get('stub'))))


def power_cycle(stamp):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import power
    return power.cycle(stamp)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--pi', default=os.environ.get('QALINK_PI'), help='ssh target of the DreamPi (relay mode)')
    ap.add_argument('--udp', help='host[:port] of the console (direct mode, no relay)')
    ap.add_argument('--bind', type=int, default=PORT, help='local UDP port in direct mode (default %d)' % PORT)
    ap.add_argument('--port', type=int, default=PORT, help='the loader port on the Pi (relay mode)')
    ap.add_argument('-v', '--verbose', action='store_true')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('status')
    for name in ('push', 'send', 'run'):
        s = sub.add_parser(name)
        s.add_argument('program')
        s.add_argument('--args', default='')
        s.add_argument('--deadline', type=int, default=0, help='seconds the client library allows the program')
        s.add_argument('--no-compress', action='store_true')
        s.add_argument('--corrupt', choices=('stream', 'payload', 'declared'))
        s.add_argument('--session', type=lambda x: int(x, 16), help='hex; default random (same value resumes)')
        s.add_argument('--out', default='.')
        s.add_argument('--wait', type=float, default=900.0, help='seconds to wait for the loader after EXEC')
        s.add_argument('--no-collect', action='store_true')
        s.add_argument('--wmax', type=int, default=16, help='most chunks in flight (default 16)')
        s.add_argument('--files', help='folder served to the program as /pc/ (adapter)')
        s.add_argument('--disc', help='the network disc\'s base: the built disc folder or an image (.gdi/.iso)')
        s.add_argument('--overlay', help='the network disc\'s overlay folder: its files win over the base')
        s.add_argument('--whole', action='store_true', help='serve the whole disc (else only changed files)')
        s.add_argument('--reset-after', type=float, help='send RESET this many seconds after EXEC')
        s.add_argument('--reset-wait', type=float, default=20.0, help='seconds RESET may take before the next rung')
        s.add_argument('--loader', help='self-update: this loader (ELF or LOADER.BIN) first, if the console runs another')
        s.add_argument('--tail', action='store_true', help='print the program\'s serial lines as they come')
    r = sub.add_parser('report')
    r.add_argument('--out', default='.')
    rs = sub.add_parser('reset', help='RESET whatever runs now; then the restart ladder')
    rs.add_argument('--wait', type=float, default=20.0)
    sub.add_parser('power', help='the last rung: power-cycle the console (pc/power.py)')
    sv = sub.add_parser('serve', aliases=['tail', 'stills'],
                        help='serve programs a launcher on the console started (files, lines, stills); tail = '
                             'serve --tail; stills = serve, keeping the PNGs')
    sv.add_argument('--files', help='folder served as /pc/')
    sv.add_argument('--disc', help='the network disc\'s base (folder or .gdi/.iso)')
    sv.add_argument('--overlay', help='the network disc\'s overlay folder')
    sv.add_argument('--whole', action='store_true', help='serve the whole disc')
    sv.add_argument('--out', default='.')
    sv.add_argument('--for', dest='seconds', type=float, default=3600.0, help='stop after this many seconds')
    sv.add_argument('--until', help='stop when a serial line contains this text')
    sv.add_argument('--tail', action='store_true')
    sub.add_parser('relay', help='print the relay program the ssh mode runs on the Pi')
    rl = sub.add_parser('relay-local', help='the relay as a standing process on this PC (Flycast)')
    rl.add_argument('--ctl', type=int, default=PORT - 1, help='client port on 127.0.0.1 (default %d)' % (PORT - 1))
    a = ap.parse_args(argv)
    stamp = lambda s: print('%s %s' % (time.strftime('%H:%M:%S'), s), flush=True)
    if a.cmd == 'relay':
        print(RELAY)
        return 0
    if a.cmd == 'power':
        return 0 if power_cycle(stamp) else 6
    if a.cmd == 'relay-local':
        relay_local(a.port, a.ctl, stamp)
        return 0
    if a.udp:
        t = UdpTransport(a.udp, a.bind)
    elif a.pi:
        t = SshRelayTransport(a.pi, a.port, stamp)
    else:
        ap.error('--pi or --udp')
    loop = Loop(t, stamp, a.verbose)
    try:
        if a.cmd in ('serve', 'tail', 'stills'):
            a.tail = a.tail or a.cmd == 'tail'
            return serve(a, loop, stamp)
        return session_main(a, loop, stamp)
    except VersionMismatch as e:
        stamp('refused: %s' % e)
        return 3
    except RuntimeError as e:
        stamp('error: %s' % e)
        return 3
    finally:
        t.close()


def serve(a, loop, stamp):
    """Serve whatever program a launcher on the console runs (a scene runner from its own disc, not the PC's push):
    its /pc/ files, its serial lines (serial-<run>.txt) and its stills, run by run, for --for seconds or --until a
    line. Needs no loader beacon. Returns 0, or 1 if --until never came."""
    os.makedirs(a.out, exist_ok=True)
    files = FileService(a.files, stamp)
    disc = disc_service(a, stamp, a.out)
    sinks, serials, seen = {}, {}, {'lines': 0, 'hit': False}
    t0 = time.time()

    def other(m):
        typ, run, aa, bb, pl = m
        if disc is not None and disc.handle(loop, typ, run, aa, bb, pl):
            return
        if typ == HELLO and run not in serials:
            stamp('program run %08x link up' % run)
            serials[run] = open(os.path.join(a.out, 'serial-%08x.txt' % run), 'w', encoding='latin-1', newline='')
        if typ == LINE:
            txt = pl.decode('latin-1')
            f = serials.get(run)
            if f is None:
                f = serials[run] = open(os.path.join(a.out, 'serial-%08x.txt' % run), 'w', encoding='latin-1', newline='')
            f.write(txt if txt.endswith('\n') else txt + '\n')
            f.flush()
            seen['lines'] += 1
            if a.tail:
                print('  | ' + txt.rstrip('\n'), flush=True)
            if a.until and a.until in txt:
                seen['hit'] = True
        elif typ == FOPEN:
            files.on_open(loop, run, aa, pl)
        elif typ == FREAD:
            files.on_read(loop, run, aa, pl)
        elif typ == FCLOSE:
            files.on_close(aa)
        elif typ == STILL:
            sinks.setdefault(run, StillSink(a.out, run, stamp)).on_still(loop, aa, bb, pl)
    loop.on_other = other
    while time.time() - t0 < a.seconds and not seen['hit']:
        loop.recv(0.2)
    loop.on_other = None
    for f in serials.values():
        f.close()
    summary = {'seconds': round(time.time() - t0, 1), 'runs': ['%08x' % r for r in serials], 'lines': seen['lines'],
               'files': files.summary(), 'stills': {('%08x' % r): k.saved for r, k in sinks.items()},
               'disc': disc.summary() if disc is not None else None}
    json.dump(summary, open(os.path.join(a.out, 'serve-%d.json' % int(t0)), 'w'), indent=2)
    stamp('serve: %s' % json.dumps(summary)[:400])
    return 0 if not a.until or seen['hit'] else 1


def ladder(loop, run, wait_s, stamp, b0=None):
    """RESET, then the next rungs. Returns (rung that worked or None, seconds, the loader's HELLO or None)."""
    t0 = time.time()
    got = {'ack': None}

    def pred(m):
        if m[0] == RESETACK:
            got['ack'] = got['ack'] or time.time() - t0
        # ql-core4: a loader started since the RESET (its uptime is shorter than the wait) is back too: after a full
        # restart where nothing survived, its boot count starts at 1 again and equals b0
        if m[0] == HELLO and parse_hello(m[4])['role'] != 2 and loop.hello and (
                b0 is None or loop.hello['boot_count'] != b0 or loop.hello['uptime_ms'] < 1000 * (time.time() - t0)):
            return loop.hello
        return None
    hb = loop.wait(pred, wait_s, every=2.0, poke=lambda: loop.send(RESET, run) if got['ack'] is None else None)
    if hb is not None:
        stamp('back to the loader %.2f s after RESET (acknowledged %s)' % (
            time.time() - t0, '%.2f s' % got['ack'] if got['ack'] is not None else 'no'))
        return 'reset', round(time.time() - t0, 3), hb, got['ack']
    stamp('RESET unanswered for %.0f s (acknowledged: %s): %s' % (
        wait_s, got['ack'] is not None, 'acknowledged; the loader is not back yet' if got['ack'] is not None
        else 'the program\'s link thread is not running'))
    stamp('next rung: the watchdog, if the program armed it (wdt=1): waiting 10 s more for the loader')
    hb = loop.wait(pred, 10.0, every=5.0, poke=lambda: loop.send(PING, 0))
    if hb is not None:
        return 'watchdog or reboot', round(time.time() - t0, 3), hb, got['ack']
    stamp('last rung: power cycle')
    power_cycle(stamp)
    return None, round(time.time() - t0, 3), None, got['ack']


def session_main(a, loop, stamp):
    h = loop.wait_hello(60.0)
    if h is None:
        stamp('no beacon from the loader in 60 s (is it UP on the console screen?)')
        return 2
    stamp(describe(h))
    if a.cmd == 'status':
        return 0
    if a.cmd == 'reset':
        rung, secs, hb, _ = ladder(loop, 0, a.wait, stamp, h['boot_count'])
        return 0 if hb else 6
    if a.cmd == 'report':
        if h['report_state'] in (0, 1):
            stamp('no report pending')
            return 1
        rep = loop.fetch_report(h, a.out)
        stamp('report %s' % json.dumps(rep))
        return 0 if rep['crc_ok'] else 1
    result = {'program': os.path.abspath(a.program), 'args': a.args, 'corrupt': a.corrupt, 'net': h['net'],
              'loader': {'build': h['build'], 'sha8': h['loader_sha8'], 'stub_bytes': h['stub_bytes']}}
    if h['report_state'] not in (0, 1) and not a.corrupt:
        rep = loop.fetch_report(h, a.out)
        stamp('earlier report collected first: %s %s' % (rep['state'], rep['path']))
    loop.wmax = max(2, a.wmax)
    if a.loader:
        result['self_update'] = loop.push_loader(a.loader, stamp)
        h = loop.hello
    raw = load_program(a.program)
    if LOAD_ADDR + len(raw) + 4 > IMAGE_END:
        stamp('program too large: %d bytes' % len(raw))
        return 2
    img = Image(raw, not a.no_compress, a.corrupt)
    session = a.session if a.session is not None else random.getrandbits(32) | 1
    stamp('image %d bytes -> %d (%s), %d chunks, sha256 %s, session %08x' % (
        len(raw), len(img.comp), 'deflate' if img.codec else 'raw', img.nchunks, hashlib.sha256(raw).hexdigest(),
        session))
    res = loop.transfer(img, session, a.args, a.deadline)
    stamp('transfer %s' % json.dumps(res))
    result['transfer'] = res
    rc = 0 if res['verdict'] == 0 else 3
    if a.cmd == 'run' and (res['verdict'] == 0 or a.corrupt):
        r = loop.run_verified(session)
        stamp('run: %s' % r)
        result['run'] = r
        if r in ('exec', 'no answer') and not a.no_collect:
            rc = collect(a, loop, stamp, session, result, h)
        elif r not in ('exec', 'no answer'):
            rc = 3
    result['link'] = loop.stats
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, 'run-%08x.json' % session), 'w') as f:
        json.dump(result, f, indent=2)
    return rc


def end_on(asm, loop, session, a, pl, stamp, t0):
    """One END datagram into the run's assembler; a head is answered with ENDACK (b = declared tail bytes not yet held
    as one non-overlapping cover). Shared by the CLI and the bridge."""
    first = asm.head is None
    got = asm.on(session, a, pl)
    if got != 'head':
        return got
    e = asm.head
    if first:
        asm.at = time.time()
        stamp('END from the program %.2f s after EXEC: %s exit %d, report %d bytes (%d in this END), %s' % (
            asm.at - t0, RUN_STATES.get(e['state'], e['state']), e['exit'], e['report_len'], e['sent_len'],
            'sent from an exception' if e['path'] == 2 else 'from its thread'))
    loop.send(ENDACK, session, session, min(0xFFFF, asm.missing()))
    return got


def end_report(asm, session, out):
    """The run's result from its END (None without a head): written to <out>/report-<run>.txt when out is set."""
    r0 = asm.result()
    if r0 is None:
        return None
    e, tail = r0['end'], r0['tail']
    path = os.path.join(out, 'report-%08x.txt' % session) if out else None
    if path:
        os.makedirs(out, exist_ok=True)
        with open(path, 'wb') as f:
            f.write(tail)
    st = RUN_STATES.get(e['state'], e['state'])
    r = {'run': '%08x' % session, 'state': st, 'exit': e['exit'], 'bytes': e['report_len'], 'tail_bytes': len(tail),
         'complete': r0['complete'], 'whole': r0['whole'], 'overlap': r0['overlap'], 'crc_ok': r0['crc_ok'],
         'path': path, 'by': 'END over the link', 'end': e}
    if st == 'CRASHED':
        r['crash'] = {'event': '%03x' % e['crash_evt'], 'pc': '%08x' % e['crash_pc'], 'pr': '%08x' % e['crash_pr'],
                      'sp': '%08x' % e['crash_sp']}
    return r


def collect(a, loop, stamp, session, result, h):
    """After EXEC: serve the program (files, lines, stills), RESET it if asked, wait for the loader, take the report."""
    b0 = (loop.hello or {}).get('boot_count')
    t0 = time.time()
    files = FileService(a.files, stamp)
    disc = disc_service(a, stamp, a.out)
    stills = StillSink(a.out, session, stamp)
    lines = {'n': 0, 'gaps': 0, 'next': 0, 'first_at': None}
    os.makedirs(a.out, exist_ok=True)
    serial = open(os.path.join(a.out, 'serial-%08x.txt' % session), 'w', encoding='latin-1', newline='') if a.out else None
    marks = {}

    def other(m):
        typ, run, aa, bb, pl = m
        if run != session:
            return
        if disc is not None and disc.handle(loop, typ, run, aa, bb, pl):
            return
        if typ == LINE:
            lines['n'] += 1
            lines['first_at'] = lines['first_at'] or time.time() - t0
            if aa > lines['next']:
                lines['gaps'] += aa - lines['next']
            lines['next'] = max(lines['next'], aa + 1)
            txt = pl.decode('latin-1')
            if serial:
                serial.write(txt if txt.endswith('\n') else txt + '\n')
                serial.flush()
            if a.tail:
                print('  | ' + txt.rstrip('\n'), flush=True)
        elif typ == FOPEN:
            files.on_open(loop, run, aa, pl)
        elif typ == FREAD:
            files.on_read(loop, run, aa, pl)
        elif typ == FCLOSE:
            files.on_close(aa)
        elif typ == STILL:
            stills.on_still(loop, aa, bb, pl)
        elif typ == HELLO and loop.client_hello and 'client_up_s' not in marks:
            marks['client_up_s'] = round(time.time() - t0, 3)
            stamp('program\'s link up %.2f s after EXEC' % marks['client_up_s'])
        elif typ == END:
            end_datagram(aa, pl)
    loop.on_other = other
    ending = qlsafe.EndAssembler(session)

    def end_datagram(aa, pl):
        """ql-core4: the run's ending, sent by the program before its full restart (include/dcql_loop.h CL_END)."""
        end_on(ending, loop, session, aa, pl, stamp, t0)

    def end_result():
        return end_report(ending, session, a.out)

    def back(m):
        if m[0] == HELLO and parse_hello(m[4])['role'] != 2 and loop.hello and \
                (loop.hello['boot_count'] != b0 or loop.hello['uptime_ms'] < 1000 * (time.time() - t0)):
            return loop.hello
        return None
    hb = getattr(loop, 'ran_back', None)   # run_verified already saw the loader back with this run's report
    rung = None
    if hb is None and a.reset_after is not None:
        hb = loop.wait(back, a.reset_after)
        if hb is None:
            stamp('RESET after %.0f s' % a.reset_after)
            rung, secs, hb, ack = ladder(loop, session, a.reset_wait, stamp, b0)
            result['reset'] = {'rung': rung, 'seconds': secs, 'acknowledged_s': ack}
    if hb is None and rung is None and a.reset_after is None:
        hb = loop.wait(back, a.wait, every=None)
    loop.on_other = None
    if serial:
        serial.close()
    result['program_side'] = {'files': files.summary(), 'lines': {k: v for k, v in lines.items() if k != 'next'},
                              'stills': stills.saved, **marks}
    er = end_result()
    if er:
        result['end'] = er
    if disc is not None:
        result['program_side']['disc'] = disc.summary()
        stamp('network disc: %s' % json.dumps(result['program_side']['disc'])[:600])
    if hb is None:
        stamp('the loader did not come back' + ('' if a.reset_after is not None else ' within %.0f s' % a.wait))
        result['report'] = er
        return 6 if a.reset_after is not None else 4
    result['back_after_s'] = round(time.time() - t0, 3)
    if ending.at is not None:   # ql-core4: the program's end to the loader's first beacon heard again
        result['end_to_beacon_s'] = round(time.time() - ending.at, 3)
        stamp('end to beacon %.2f s (END at %.2f s after EXEC, loader up %.1f s at its beacon)' % (
            result['end_to_beacon_s'], ending.at - t0, hb['uptime_ms'] / 1000.0))
    if hb['report_run'] != session or hb['report_state'] in (0, 1) or (er and not hb['report_len']):
        if er:   # ql-core4: back by the full restart, the run's ending already in hand: the result is the END's
            stamp('loader back after %.2f s by the full restart; the run\'s result from its END: %s exit %d, report %d '
                  'of %d bytes (%s) -> %s; the loader says: %s' % (
                      time.time() - t0, er['state'], er['exit'], er['tail_bytes'], er['bytes'],
                      'crc ok' if er['crc_ok'] else 'whole' if er['whole'] else 'its tail', er['path'], describe(hb)))
            if er.get('crash'):
                stamp('crash: event %(event)s pc %(pc)s pr %(pr)s sp %(sp)s' % er['crash'])
            result['report'] = er
            return 0 if er['state'] in ('DONE', 'RESET') else 5
        stamp('loader back after %.1f s without this run (power cycle?): %s' % (time.time() - t0, describe(hb)))
        result['report'] = None
        result['lost'] = RUN_STATES.get(hb['report_state'], hb['report_state']) if hb['report_run'] == session else None
        return 4
    stamp('loader back after %.2f s: %s' % (time.time() - t0, describe(hb)))
    rep = loop.fetch_report(hb, a.out)
    result['report'] = rep
    stamp('report %s %s exit %d, %d bytes, crc %s -> %s' % (rep['run'], rep['state'], rep['exit'], rep['bytes'],
                                                         rep['crc_ok'], rep['path']))
    if not rep['crc_ok'] or rep['state'] not in ('DONE', 'RESET'):
        return 5
    return 0


if __name__ == '__main__':
    sys.exit(main())
