"""core.py: the bridge's link side. One UDP socket owns the link to one console (or one Flycast); a pump thread reads it
while nothing runs, the run worker reads it while a run is on, and every datagram passes the same tap, so lines, files,
stills, records, trace and log are handled the same way whoever is reading. Sends never wait for the reader."""
import hashlib, itertools, json, os, queue, random, re, socket, struct, sys, threading, time, zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import dcql_loop as cl   # noqa: E402
import dcql_disc         # noqa: E402  (the network disc, lane ql-disc)
from . import wire       # noqa: E402
from .live import Session   # noqa: E402

CONTRACT = 1
BRIDGE_VERSION = 'dc-qalink bridge 0.2.0'
RUN_STATE_OF = {'DONE': 'done', 'CRASHED': 'crashed', 'TIMEOUT': 'timeout', 'RESET': 'reset', 'LOST': 'lost'}
SID_RE = re.compile(r'^[0-9a-f]{8}$')


def now_iso():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def local_addresses():
    out = {'127.0.0.1'}
    try:
        for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            out.add(a[4][0])
    except OSError:
        pass
    return out


# ------------------------------------------------------------------------------------------------ transport
class BridgeUdp:
    """The loader's UDP port, bound for as long as the bridge runs (a Windows socket that once took an ICMP
    port-unreachable stops passing datagrams in Flycast, so it is never closed between runs). Every well-formed beacon
    is recorded for discovery; only datagrams from the chosen console reach the loop."""

    STALE_S = 4.0   # a chosen loader silent this long may be replaced by its own beacons from a new address

    def __init__(self, bind_host, bind_port, console=None, autoconnect=True, remember=None, only_local=False):
        self.s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        cl.big_buffers(self.s)
        self.s.bind((bind_host, bind_port))
        self.port = self.s.getsockname()[1]
        self.fixed = None
        if console and console != 'auto':
            h, _, p = console.partition(':')
            self.fixed = (h, int(p or cl.PORT))
        self.peer = self.fixed
        self.chosen_ip = self.fixed[0] if self.fixed else None
        self.seen = {}           # ip -> discovery row
        self.echoes = 0          # our own datagrams looped back (dropped)
        self.log = None
        self.lock = threading.RLock()
        self.locals = local_addresses()
        # autoconnect (owner, 2026-11-22): link without a click to a console linked before (or the only one heard when
        # none is remembered), and relink by itself when it comes back from a run or a reboot, on a new address too.
        # Consoles are remembered by the loader's identity, not by address (DHCP may change it).
        self.autoconnect = autoconnect
        # only_local: talk only to a loader at one of this PC's own addresses (Flycast, the simulated console); a real
        # console on the LAN is listed in discovery and never linked or sent to (lane and test runs; 2026-11-22 a lane
        # run linked the owner's console because it beaconed before Flycast booted)
        self.only_local = only_local
        self.remote_refused = 0
        self.remember_path = remember
        self.remembered = {}
        self.chosen_ident = None
        if remember and os.path.isfile(remember):
            try:
                self.remembered = json.load(open(remember, encoding='utf-8'))
            except (OSError, ValueError):
                self.remembered = {}

    @staticmethod
    def ident(h):
        """The console's identity: the adapter MAC from HELLO (offset 128, loaders from 456ac57 on), one console across
        address changes and loader updates; a loader without it (or the modem: all zero) by its build and the first
        bytes of its SHA-256."""
        mac = h.get('mac')
        return 'mac:%s' % mac if mac else BridgeUdp.loader_key(h)

    @staticmethod
    def loader_key(h):
        return 'loader:%s:%s' % (h.get('loader_sha8', ''), h.get('build', ''))

    def is_remembered(self, h):
        """Remembered by its MAC, or by its loader's identity (a console remembered before its loader sent the MAC)."""
        return self.ident(h) in self.remembered or self.loader_key(h) in self.remembered

    @staticmethod
    def card(h):
        """What HELLO says about the console beyond its address: MAC, GDEMU slot and version, the stub's last record."""
        return {'mac': h.get('mac'), 'gdemu_slot': h.get('gdemu_slot'), 'gdemu_version': h.get('gdemu_ver'),
                'stub': h.get('stub'), 'stub_text': cl.stub_text(h.get('stub')) if h.get('stub') else None}

    def is_local(self, ip):
        return ip.startswith('127.') or ip in self.locals

    def choose(self, ip, why='link'):
        with self.lock:
            if ip not in self.seen and not self.fixed:
                raise KeyError(ip)
            if self.only_local and not self.is_local(ip):
                self.remote_refused += 1
                raise PermissionError('this bridge talks only to this PC (--only-local): %s is not' % ip)
            self.chosen_ip = ip
            self.peer = self.seen[ip]['addr'] if ip in self.seen else self.fixed
            h = (self.seen.get(ip) or {}).get('hello')
            if h:
                self.chosen_ident = self.ident(h)
                self.remembered[self.chosen_ident] = {'last_ip': h['console_ip'], 'source': ip, 'build': h['build'],
                                                      'mac': h.get('mac'), 'gdemu_slot': h.get('gdemu_slot'),
                                                      'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
                if self.chosen_ident.startswith('mac:'):   # now known by its MAC: drop the loader-identity entry
                    self.remembered.pop(self.loader_key(h), None)
                if self.remember_path:
                    try:
                        json.dump(self.remembered, open(self.remember_path, 'w', encoding='utf-8'), indent=1)
                    except OSError:
                        pass
                if self.log:
                    self.log('console %s (%s): beacons from %s:%d, the loader says its address is %s (%s)' % (
                        'linked' if why == 'link' else why, self.chosen_ident, ip, self.peer[1], h['console_ip'], h['build']))

    def _auto(self, ip, h):
        """On a loader's HELLO from `ip`: link to it if autoconnect says so (called with the lock held)."""
        if self.fixed or not self.autoconnect or ip == self.chosen_ip:
            return
        if self.only_local and not self.is_local(ip):
            self.remote_refused += 1
            return
        idt = self.ident(h)
        now = time.time()
        if self.chosen_ip is None:
            loaders = [r for r in self.seen.values() if r.get('hello') and now - r['last'] < self.STALE_S]
            # a console linked before; else, when none is remembered, the only loader heard. Two unknown loaders, or an
            # unknown one beside remembered ones: wait for a click (then it is remembered).
            if self.is_remembered(h) or (not self.remembered and len(loaders) == 1):
                self.choose(ip, 'autoconnected')
            return
        cur = self.seen.get(self.chosen_ip)
        if (cur is None or now - cur['last'] > self.STALE_S) and (idt == self.chosen_ident or self.is_remembered(h)):
            self.choose(ip, 'relinked at a new address')

    def send(self, d):
        if self.peer and not (self.only_local and not self.is_local(self.peer[0])):   # never a send off this PC
            try:
                self.s.sendto(d, self.peer)
            except OSError:
                pass

    def recv(self, timeout):
        self.s.settimeout(max(timeout, 0.001))
        try:
            d, a = self.s.recvfrom(4096)
        except (socket.timeout, BlockingIOError, ConnectionResetError):
            return None
        except OSError:
            time.sleep(min(timeout, 0.05))
            return None
        m = cl.unframe(d, any_version=True)
        if m is None:
            return d if a[0] == self.chosen_ip else None   # counted as bad by the loop
        if m[0] in cl.PC_TYPES:
            # a PC-to-console datagram is ours (or another PC tool's) looped back, from this PC's own address: never
            # the console. A console's HELLO from this PC's own address is Flycast's (it maps the emulated console
            # onto the PC's address), so a source address alone cannot decide (first console run, 2026-11-22).
            self.echoes += 1
            return None
        if m[0] == cl.HELLO and len(m[4]) >= 96:
            try:
                h = cl.parse_hello(m[4])
            except struct.error:
                h = None
            if h:
                with self.lock:
                    row = self.seen.setdefault(a[0], {'first': time.time(), 'beacons': 0})
                    row.update(addr=a if h['role'] != 2 else row.get('addr', a), last=time.time(), proto=m[5])
                    if h['role'] != 2:
                        row['beacons'] += 1
                        row['hello'] = h
                    if h['role'] != 2:   # the peer is learnt only from a loader's HELLO
                        self._auto(a[0], h)
        if a[0] != self.chosen_ip or (self.only_local and not self.is_local(a[0])):
            return None
        if self.peer is None or self.peer[0] == a[0]:
            self.peer = a        # the reply goes where the console speaks from (Flycast maps the port)
        return d

    def discovery(self):
        with self.lock:
            out = []
            for ip, r in sorted(self.seen.items()):
                h = r.get('hello') or {}
                out.append({'id': 'dc-' + ip.replace('.', '-'), 'console_ip': h.get('console_ip', ip), 'address': ip,
                            'port': r['addr'][1], 'kind': 'flycast' if ip in self.locals else 'console',
                            'transport': h.get('net', '-'), 'loader': h.get('build', ''), 'protocol': r.get('proto'),
                            'beacons': r['beacons'], 'first_s_ago': round(time.time() - r['first'], 1),
                            'last_s_ago': round(time.time() - r['last'], 1), 'chosen': ip == self.chosen_ip,
                            'ident': self.ident(h) if h else None, 'remembered': bool(h) and self.is_remembered(h),
                            'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(r['last'])), **self.card(h)})
            return out

    def close(self):
        self.s.close()


class TapLoop(cl.Loop):
    def __init__(self, transport, log, tap):
        super().__init__(transport, log)
        self.tap = tap

    def recv(self, timeout):
        m = super().recv(timeout)
        if m is not None:
            try:
                self.tap(m)
            except Exception as e:   # a fault in one record must never stop the link
                self.log('tap: %r' % e)
        return m


# ------------------------------------------------------------------------------------------------ stills
class Stills:
    """STILL blocks from the program into PNG files. Each still is kept with its run, picture and, for a grab the PC
    asked for, the request id (the head's reserved word)."""

    def __init__(self, folder, log):
        self.folder = folder
        os.makedirs(folder, exist_ok=True)
        self.log = log
        self.cur = {}
        self.meta = {}           # id -> json
        self.waiters = {}        # grab request id -> list of still ids (filled when held)

    def on_still(self, loop, run, a, b, payload, tier, emit):
        key = (run, b)
        st = self.cur.get(key)
        if a == cl.STILL_HEAD:
            index, picture, w, h, fmt, nbytes, crc, req = struct.unpack('<8I', payload[:32])
            if st is None or st['bytes'] != nbytes or st['done']:
                self.cur[key] = {'picture': picture, 'w': w, 'h': h, 'bytes': nbytes, 'crc': crc, 'req': req,
                                 'data': bytearray(nbytes), 'have': set(), 't0': time.time(), 'done': False}
            return
        if st is None:
            return
        if a == cl.STILL_END:
            nblocks = (st['bytes'] + cl.CHUNK - 1) // cl.CHUNK
            bits = bytearray((nblocks + 7) // 8)
            for k in st['have']:
                bits[k >> 3] |= 1 << (k & 7)
            missing = nblocks - len(st['have'])
            if not missing and not st['done']:
                st['done'] = True
                crc = st['crc']
                if len(payload) >= 32:   # a grab's CRC is of the bytes as sent (video memory may change under it)
                    crc_end = struct.unpack_from('<I', payload, 24)[0]
                    if crc == 0:
                        crc = crc_end
                ok = (zlib.crc32(bytes(st['data'])) & 0xFFFFFFFF) == crc
                sid = '%08x-%d' % (run, b)
                path = os.path.join(self.folder, 'still-%s.png' % sid)
                if ok:
                    cl.png_rgb565(path, st['w'], st['h'], bytes(st['data']))
                j = {'id': sid, 'run': '%08x' % run, 'index': b, 'picture': st['picture'], 'width': st['w'],
                     'height': st['h'], 'format': 'rgb565', 'crc_ok': ok, 'bytes': st['bytes'],
                     'seconds': round(time.time() - st['t0'], 3), 'request': st['req'] or None, 'tier': tier,
                     'at': now_iso(), 'png': path if ok else None}
                self.meta[sid] = j
                self.log('still %s (picture %d) %s in %.2f s' % (sid, st['picture'], 'held' if ok else 'CRC MISMATCH',
                                                               j['seconds']))
                if st['req']:
                    self.waiters.setdefault(st['req'], []).append(sid)
                emit(j)
                self.cur.pop(key, None)
            loop.send(cl.STILLACK, run, b, min(missing, 0xFFFF), bytes(bits[:cl.DGRAM_MAX - cl.HDR - 4]))
            return
        k = a // cl.CHUNK
        if a % cl.CHUNK == 0 and a + len(payload) <= st['bytes']:
            st['data'][a:a + len(payload)] = payload
            st['have'].add(k)


# ------------------------------------------------------------------------------------------------ runs
class Run:
    _n = itertools.count(1)

    def __init__(self, name, raw, args, deadline_s, files, reset_after_s, by, commit=None):
        self.sid = random.getrandbits(32) | 1
        self.commit = commit
        self.build = None        # the build record's id (Bridge.make_build)
        self.id = '%08x' % self.sid
        self.name, self.raw, self.args = name, raw, args
        self.sha256 = hashlib.sha256(raw).hexdigest()
        self.deadline_s, self.files, self.reset_after_s, self.by = deadline_s, files, reset_after_s, by
        self.state = 'queued'
        self.queued_at, self.ended_at = now_iso(), None
        self.times = {}
        self.way_back = None
        self.report = None
        self.report_crc_ok = None
        self.crash = None
        self.error = None
        self.files_stats = None
        self.serial = []
        self.serial_gaps = 0
        self.stills = []
        self.transfer = None
        self.summary = None
        self.reset_flag = threading.Event()
        self.done = threading.Event()

    def json(self, tier, device_name):
        j = {'id': self.id, 'program': {'name': self.name, 'sha256': self.sha256, 'bytes': len(self.raw)},
             'args': self.args, 'state': self.state, 'times': self.times, 'way_back': self.way_back, 'by': self.by,
             'queued_at': self.queued_at, 'ended_at': self.ended_at, 'device': device_name, 'tier': tier,
             'deadline_s': self.deadline_s, 'files': self.files_stats, 'stills': self.stills,
             'serial_lines': len(self.serial), 'error': self.error, 'summary': self.summary, 'build': self.build}
        if self.crash:
            j['crash'] = self.crash
        if self.transfer:
            j['transfer'] = self.transfer
        return j


NUM_RE = re.compile(r'^@\s*(?P<label>[^=]+?)\s*=\s*(?P<value>-?[0-9.]+)\s*(?P<unit>\S*)\s*$')
CRASH_RE = re.compile(r'CRASH event (\w+) pc (\w+) pr (\w+) sp (\w+)')


def parse_summary(text, state, exit_code, seconds, tier, session):
    """The parsed summary beside the text report: exit, state, seconds, pictures, headline numbers (report lines of the
    form "@ label = value unit", and the live summary's phase means when the program sent records), notes, crash."""
    numbers, notes = [], []
    for line in text.splitlines():
        m = NUM_RE.match(line.strip())
        if m:
            numbers.append({'label': m['label'], 'value': float(m['value']), 'unit': m['unit'], 'tier': tier})
        elif line.startswith(('cl-link:', 'cl-wdt:', 'DEADLINE', 'RESET by the PC', 'ABORT')):
            notes.append(line.strip())
    pictures = 0
    if session and session.max_n:
        su = session.summary(1)
        pictures = su['pictures']
        for p in su['phases']:
            numbers.append({'label': '%s mean' % p['label'], 'value': p['mean'], 'unit': 'ms', 'tier': tier})
            numbers.append({'label': '%s worst' % p['label'], 'value': p['worst'], 'unit': 'ms', 'tier': tier})
    out = {'exit': exit_code, 'state': state, 'seconds': seconds, 'pictures': pictures, 'numbers': numbers,
           'notes': notes, 'tier': tier}
    m = CRASH_RE.search(text)
    if m:
        out['crash'] = {'event': m[1], 'pc': m[2], 'pr': m[3], 'sp': m[4]}
    return out


# ------------------------------------------------------------------------------------------------ the bridge
class LinkLock:
    """The link's lock: re-entrant, and it counts who waits so the idle pump steps aside. A plain lock is not fair:
    the pump re-took it at once and a run queued behind it waited 46 s in a host test (2026-11-22)."""
    def __init__(self):
        self._l = threading.RLock()
        self._w = 0
        self._m = threading.Lock()

    @property
    def waiting(self):
        return self._w > 0

    def acquire(self, timeout=-1):
        return self._l.acquire(timeout=timeout)

    def release(self):
        self._l.release()

    def __enter__(self):
        with self._m:
            self._w += 1
        try:
            self._l.acquire()
        finally:
            with self._m:
                self._w -= 1
        return self

    def __exit__(self, *exc):
        self._l.release()


class Bridge:
    def __init__(self, data_dir, bind=('0.0.0.0', cl.PORT), console='auto', device_kind=None, files_root=None,
                 selftest=None, loader_disc=None, level='run', log=None, autoconnect=True, only_local=True,
                 disc=None, overlay=None, whole=False):
        self.data_dir = os.path.abspath(data_dir)
        os.makedirs(self.data_dir, exist_ok=True)
        self.sessions_dir = os.path.join(self.data_dir, 'sessions')
        os.makedirs(self.sessions_dir, exist_ok=True)
        self.logf = open(os.path.join(self.data_dir, 'bridge.log'), 'a', encoding='utf-8')
        self._ext_log = log
        self.t = BridgeUdp(bind[0], bind[1], console, autoconnect, os.path.join(self.data_dir, 'consoles.json'),
                           only_local)
        if self.t.fixed and only_local and not self.t.is_local(self.t.fixed[0]):
            self.t.close()
            self.logf.close()
            raise PermissionError('--console %s is not this PC; a real console needs --allow-lan (a person\'s choice)'
                                  % self.t.fixed[0])
        self.t.log = self.log
        self.loop = TapLoop(self.t, self.log, self.tap)
        self.link_lock = LinkLock()
        self.device_kind = device_kind
        self.files_root = files_root
        self.selftest_path = selftest
        self.loader_disc = loader_disc
        self.level = level
        self.stills = Stills(os.path.join(self.data_dir, 'stills'), self.log)
        self.runs = {}
        self.run_order = []
        self.queue = queue.Queue()
        self.active = None
        self.sessions = {}
        self.current = None          # the session of the linked program (or the last one)
        self.client = None           # the program's HELLO
        self.client_run = None
        self.client_hello_run = 0
        self.def_ver = None
        self.def_parts = {}
        self.declaration = None
        self.files = {}              # run -> FileService
        self.cmd_n = itertools.count(1)
        self.cmd_wait = {}
        self.grab_n = itertools.count(int(time.time()) & 0xFFFF << 8 | 1)
        self.paused = False
        self.listeners = set()
        self.listeners_lock = threading.Lock()
        self.link_state = 'searching'
        self.rx_hist = []
        self.stop_flag = threading.Event()
        self.audit = open(os.path.join(self.data_dir, 'calls.log'), 'a', encoding='utf-8')
        # builds (lane ql-disc): the workspace's anchor; one record per distinct program + disc + loader + args + tier
        self.builds_path = os.path.join(self.data_dir, 'builds.json')
        self.builds = {}
        if os.path.isfile(self.builds_path):
            try:
                self.builds = json.load(open(self.builds_path, encoding='utf-8'))
            except (OSError, ValueError):
                self.builds = {}
        self.build = None            # the current build's id (the last pushed, or the linked program's)
        self.disc_args = (disc, overlay, bool(whole))
        self.disc = None
        if disc or overlay:
            self.disc = dcql_disc.DiscService(disc, overlay, whole, self.log, self.record,
                                              os.path.join(self.data_dir, 'disc-hashes.json'))
            m = self.disc.manifest()
            self.log('network disc: %s, %d files, %d from the PC, manifest %s' % (m['mode'], m['count'], m['served'],
                                                                                m['sha256'][:16]))
        threading.Thread(target=self._pump, daemon=True, name='bridge-pump').start()
        threading.Thread(target=self._worker, daemon=True, name='bridge-runs').start()
        threading.Thread(target=self._ticker, daemon=True, name='bridge-status').start()
        self.log('%s, contract %d, udp %s:%d, console %s, data %s' % (BRIDGE_VERSION, CONTRACT, bind[0], self.t.port,
                                                                    console, self.data_dir))

    # ------------------------------------------------------------------ plumbing
    def log(self, s):
        line = '%s %s' % (time.strftime('%H:%M:%S'), s)
        self.logf.write(line + '\n')
        self.logf.flush()
        if self._ext_log:
            self._ext_log(line)

    def audit_call(self, who, level, what, result):
        self.audit.write(json.dumps({'at': now_iso(), 'who': who, 'level': level, 'call': what, 'result': result,
                                     'build': self.build}) + '\n')
        self.audit.flush()

    def tier(self):
        if self.device_kind:
            return self.device_kind
        h = self.loop.hello or {}
        if h.get('build', '').startswith('sim'):
            return 'mock'
        return 'flycast' if (self.t.chosen_ip or '') in self.t.locals else 'console'

    def device(self):
        h = self.loop.hello
        kind = self.tier()
        name = {'flycast': 'Flycast on this PC', 'console': 'Dreamcast', 'mock': 'Simulated console (host test)'}[kind]
        d = {'id': 'dc-' + (self.t.chosen_ip or 'none').replace('.', '-'), 'kind': kind if kind != 'mock' else 'flycast',
             'tier': kind, 'name': name, 'transport': 'adapter' if not h or h.get('net') != 'modem' else 'modem',
             'address': self.t.chosen_ip, 'console_ip': h['console_ip'] if h else None, 'link': self.link_state,
             'state': cl.STATES.get(h['state'], '?').lower() if h else None,
             'loader': {'build': h['build'], 'sha8': h['loader_sha8'], 'protocol': h['proto'],
                        'stub_bytes': h['stub_bytes'], 'boot_count': h['boot_count'],
                        'pushed': bool(h['flags'] & cl.HF_PUSHED)} if h else None,
             'ident': self.t.ident(h) if h else None, **self.t.card(h or {}),
             'program': {'run': '%08x' % self.client_run, 'linked': self.client is not None,
                         'hello_run': '%08x' % self.client_hello_run} if self.client_run else None}
        if self.active:
            d['state'] = 'running' if self.active.state == 'running' else self.active.state
            d['last_run'] = {'id': self.active.id, 'state': self.active.state}
        elif self.run_order:
            r = self.runs[self.run_order[-1]]
            d['last_run'] = {'id': r.id, 'state': r.state, 'report_bytes': len(r.report or b'')}
        return d

    def status(self):
        now = time.time()
        rate = sum(b for t, b in self.rx_hist if now - t < 2.0) / 2.0
        return {'contract': CONTRACT, 'bridge': BRIDGE_VERSION, 'link': self.link_state, 'device': self.device(),
                'session': self.current.id if self.current else None, 'rate_bps': round(rate, 1),
                'mode': 'summary' if (self.loop.hello or {}).get('net') == 'modem' else 'full', 'paused': self.paused,
                'n': self.current.max_n if self.current else 0, 'level': self.level, 'tier': self.tier(),
                'queue': [r.id for r in list(self.queue.queue)], 'active_run': self.active.id if self.active else None,
                'autoconnect': self.t.autoconnect, 'remembered': len(self.t.remembered),
                'only_local': self.t.only_local, 'remote_refused': self.t.remote_refused, 'build': self.build,
                'disc': self.disc.manifest()['mode'] if self.disc else None}

    # ------------------------------------------------------------------ builds (lane ql-disc)
    def rescan_disc(self):
        """The overlay read again (a developer changed files since the bridge started); the hash cache keeps it cheap."""
        if self.disc is None:
            return None
        d, o, w = self.disc_args
        self.disc = dcql_disc.DiscService(d, o, w, self.log, self.record, os.path.join(self.data_dir, 'disc-hashes.json'))
        return self.disc.manifest()

    def make_build(self, name, sha256, nbytes, args, commit=None, by='api'):
        """The build record of a push: its id is a hash of what makes it this build (program, disc manifest, loader,
        arguments, tier), so the same push again is the same build and its runs gather under it."""
        h = self.loop.hello or {}
        m = self.rescan_disc() if self.disc else None
        loader = {'build': h.get('build'), 'sha8': h.get('loader_sha8'), 'protocol': h.get('proto')} if h else None
        tier = self.tier()
        ident = '|'.join([sha256, m['sha256'] if m else '-', (loader or {}).get('sha8') or '-', args or '', tier])
        bid = 'b' + hashlib.sha256(ident.encode('utf-8')).hexdigest()[:12]
        b = self.builds.get(bid)
        if b is None:
            b = self.builds[bid] = {
                'id': bid, 'program': {'name': name, 'sha256': sha256, 'bytes': nbytes, 'commit': commit},
                'disc': {'manifest': m['sha256'], 'mode': m['mode'], 'files': m['count'], 'served': m['served'],
                         'base': m['base'], 'overlay': m['overlay']} if m else None,
                'loader': loader, 'args': args or '', 'tier': tier, 'created': now_iso(), 'by': by, 'runs': []}
        elif commit and not b['program'].get('commit'):
            b['program']['commit'] = commit
        b['last'] = now_iso()
        self.build = bid
        self.save_builds()
        return b

    def save_builds(self):
        try:
            json.dump(self.builds, open(self.builds_path, 'w', encoding='utf-8'), indent=1)
        except OSError:
            pass

    def build_list(self):
        return sorted(self.builds.values(), key=lambda b: b.get('last') or b['created'], reverse=True)

    def set_autoconnect(self, on):
        with self.t.lock:
            self.t.autoconnect = bool(on)
        self.log('autoconnect %s' % ('on' if on else 'off'))
        return self.status()

    def emit(self, r):
        """A record or event to every live listener (and into the current session when it belongs there)."""
        with self.listeners_lock:
            ls = list(self.listeners)
        for q in ls:
            try:
                q.put_nowait(r)
            except queue.Full:
                pass

    def record(self, r, session=None):
        s = session or self.current
        if s is not None:
            s.add(r)         # its listener emits it
        else:
            r.setdefault('s', 0.0)
            self.emit(r)

    def new_session(self, run_sid, program):
        sid = '%s-%08x' % (time.strftime('%Y%m%d-%H%M%S'), run_sid)
        dev = self.device()
        r = self.runs.get('%08x' % run_sid)
        build = self.builds.get(r.build) if r is not None and r.build else None
        s = Session(self.sessions_dir, sid, {'kind': dev['kind'], 'name': dev['name'],
                                             'transport': 'bba' if dev['transport'] == 'adapter' else 'modem'},
                    self.tier(), '%s on %s' % (program.get('name') or 'program', dev['name']), program, build)
        s.listeners.append(self.emit)
        if self.current is not None and self.current is not s:
            self.current.close('another program linked')
        self.sessions[sid] = s
        self.current = s
        self.emit(dict(s.meta, s=0.0))
        return s

    def session_for(self, run_sid):
        if self.current is not None and self.current.id.endswith('%08x' % run_sid):
            return self.current
        r = self.runs.get('%08x' % run_sid)
        return self.new_session(run_sid, {'name': r.name if r else 'program (started on the console)',
                                          'bytes': len(r.raw) if r else None, 'hash': r.sha256 if r else None})

    # ------------------------------------------------------------------ the tap
    def tap(self, m):
        typ, run, a, b, pl = m
        self.rx_hist.append((time.time(), len(pl) + 20))
        if len(self.rx_hist) > 4000:
            del self.rx_hist[:2000]
        tier = self.tier()
        if self.disc is not None and typ in dcql_disc.DISC_TYPES_FROM_CONSOLE:
            self.disc.handle(self.loop, typ, run, a, b, pl)
            return
        if typ == cl.HELLO and len(pl) >= 96:
            h = cl.parse_hello(pl)
            if h['role'] == 2:
                h['hello_run'] = h['run_id']   # as the program's HELLO said it (before 6465d72 a client said 0)
                if not h['run_id'] and self.active is not None:
                    # the program on Flycast reported run 0 (its loader hand-off carries no run id: ql-core2's
                    # side); a program that links while a pushed run is on is that run (Flycast f2)
                    h['run_id'] = self.active.sid
                fresh = self.client is None or self.client_run != h['run_id']
                self.client, self.client_run = h, h['run_id']
                self.client_hello_run = h['hello_run']   # kept after the program leaves (self.client goes None)
                if fresh:
                    s = self.session_for(h['run_id'])
                    self.record({'t': 'link', 'state': 'up', 'role': 'program', 'run': '%08x' % h['run_id'],
                                 'console_ip': h['console_ip'], 'transport': 'adapter'}, s)
                    self.def_ver, self.def_parts = None, {}
                    self.subscribe()
            else:
                was = self.link_state
                self.link_state = 'up'
                if was != 'up':
                    self.record({'t': 'link', 'state': 'up', 'role': 'loader', 'console_ip': h['console_ip'],
                                 'transport': 'adapter' if h['net'] != 'modem' else 'modem',
                                 'beacons': (self.t.seen.get(self.t.chosen_ip) or {}).get('beacons', 0)})
                    self.record(dict({'t': 'hello'}, **self.device()))
                if self.client is not None:   # the loader beacons only when no program runs
                    self.client_left('loader beaconing')
            return
        s = self.current
        if typ == cl.LINE:
            if b == 0:
                r = self.runs.get('%08x' % run)
                if r is not None:
                    if r.serial and a > r.serial[-1][0] + 1:
                        r.serial_gaps += a - r.serial[-1][0] - 1
                    r.serial.append((a, pl.decode('latin-1')))
                self.record({'t': 'line', 'run': '%08x' % run, 'seq': a, 'text': pl.decode('latin-1').rstrip('\n')}, s)
            elif len(pl) >= 4:
                n = struct.unpack_from('<I', pl, 0)[0]
                self.record({'t': 'log', 'n': n or None, 'lv': wire.LOG_LEVELS.get(b, 'info'), 'ph': 'play' if n else 'load',
                             'msg': pl[4:].decode('utf-8', 'replace').rstrip('\n')}, s)
        elif typ == cl.FOPEN:
            fs = self.file_service(run)
            fs.on_open(self.loop, run, a, pl)
            self.record({'t': 'file', 'run': '%08x' % run, 'path': pl.decode('latin-1'), 'op': 'open'}, s)
        elif typ == cl.FREAD:
            self.file_service(run).on_read(self.loop, run, a, pl)
        elif typ == cl.FCLOSE:
            self.file_service(run).on_close(a)
        elif typ == cl.STILL:
            def held(j):
                rr = self.runs.get(j['run'])
                j['build'] = rr.build if rr is not None else self.build
                if rr is not None:
                    rr.stills.append(j['id'])
                self.record({'t': 'still', 'run': j['run'], 'id': j['id'], 'picture': j['picture'], 'crc_ok': j['crc_ok']}, s)
                if j['crc_ok']:
                    self.record({'t': 'grab', 'n': j['picture'], 'w': j['width'], 'h': j['height'], 'fmt': 'png',
                                 'url': '/v0/stills/%s' % j['id'], 'label': 'still %s' % j['id'], 'kind': 'console'}, s)
            self.stills.on_still(self.loop, run, a, b, pl, tier, held)
        elif typ == wire.LIVEDEF:
            if self.def_ver != a:
                self.def_ver, self.def_parts = a, {}
            self.def_parts[b & 0xFF] = pl.decode('utf-8', 'replace')
            if len(self.def_parts) == max(1, b >> 8):
                self.declaration = wire.parse_def(''.join(self.def_parts[k] for k in sorted(self.def_parts)))
                if s is not None:
                    s.set_declaration(self.declaration)
        elif typ == wire.REC and s is not None:
            m_ = s.meta
            keys = [p['key'] for p in m_['phases']]
            ckeys = [c['key'] for c in m_['counters']]
            for n, ph, ctr, room, pos in wire.parse_recs(pl, b):
                row = {'t': 'row', 'n': n, 'ph': {keys[i] if i < len(keys) else 'p%d' % i: round(v / 1000.0, 3)
                                                  for i, v in enumerate(ph)}}
                if room is not None:
                    row['x'] = {'room': room, 'pos': pos}
                s.add(row)
                if ctr:
                    v = {ckeys[i] if i < len(ckeys) else 'c%d' % i: x for i, x in enumerate(ctr)}
                    if any(s.ctr.get(k) != x for k, x in v.items()):
                        s.add({'t': 'ctr', 'n': n, 'v': v})
        elif typ == wire.TRACE and s is not None:
            if s.trace_seq is not None and a > s.trace_seq:
                s.trace_lost += a - s.trace_seq
            lanes = (self.declaration or {}).get('lanes', {})
            evs = wire.parse_trace(pl, b)
            s.trace_seq = max(s.trace_seq or 0, a + len(evs))
            for n, d, lane, label in evs:
                s.add({'t': 'trace', 'lane': lanes.get(lane, 'lane %d' % lane), 'n': n, 'd': d, 'label': label})
        elif typ == wire.CMDACK:
            w = self.cmd_wait.get(a)
            if w:
                n = struct.unpack_from('<I', pl, 0)[0] if len(pl) >= 4 else None
                w['ack'] = {'status': b, 'n': n, 'text': pl[4:].decode('utf-8', 'replace')}
                w['ev'].set()
        elif typ == cl.RESETACK:
            self.record({'t': 'reset', 'run': '%08x' % a, 'rung': 'stub', 'acknowledged': True}, s)

    def client_left(self, why):
        if self.client is None:
            return
        run = self.client_run
        self.client = None
        self.paused = False
        self.record({'t': 'link', 'state': 'lost', 'role': 'program', 'run': '%08x' % run, 'why': why})
        if self.current is not None:
            self.current.flush()

    def file_service(self, run):
        fs = self.files.get(run)
        if fs is None:
            r = self.runs.get('%08x' % run)
            root = self.files_root
            if r is not None and r.files and self.files_root:
                cand = os.path.join(self.files_root, r.files)
                root = cand if os.path.isdir(cand) else self.files_root
            fs = self.files[run] = cl.FileService(root, self.log)
        return fs

    def subscribe(self, channels=wire.CH_REC | wire.CH_TRACE | wire.CH_LOG, every=1):
        if self.client_run is not None:
            self.loop.send(wire.SUB, self.client_run, channels, every)

    # ------------------------------------------------------------------ threads
    def _pump(self):
        while not self.stop_flag.is_set():
            if self.link_lock.waiting:
                time.sleep(0.01)   # a run, a reset or a command wants the link: let it in (the lock is not fair)
                continue
            if self.link_lock.acquire(timeout=0.5):
                try:
                    self.loop.recv(0.2)
                except cl.VersionMismatch as e:
                    self.link_state = 'refused'
                    self.log('refused: %s' % e)
                    time.sleep(1.0)
                except Exception as e:
                    self.log('pump: %r' % e)
                    time.sleep(0.2)
                finally:
                    self.link_lock.release()

    def _ticker(self):
        last_ping = 0
        while not self.stop_flag.is_set():
            time.sleep(1.0)
            h = self.loop.hello
            if h and time.time() - h.get('at', 0) > 8 and self.link_state == 'up' and self.active is None and \
                    self.client is None:
                self.link_state = 'lost'
                self.record({'t': 'link', 'state': 'lost', 'role': 'loader'})
            if self.client and time.time() - last_ping > 3:
                last_ping = time.time()
            if self.current is not None:
                self.current.flush()
            st = self.status()
            self.emit({'t': 'status', 's': self.current.s() if self.current else 0.0, 'link': st['link'],
                       'rate_bps': st['rate_bps'], 'paused': self.paused, 'n': st['n'], 'mode': st['mode'],
                       'session': st['session'], 'active_run': st['active_run']})

    def _worker(self):
        while not self.stop_flag.is_set():
            run = self.queue.get()
            if run is None:
                return
            self.active = run
            try:
                self._run(run)
            except Exception as e:
                run.error = '%s: %s' % (type(e).__name__, e)
                run.state = 'lost' if run.state in ('running',) else 'refused'
                self.log('run %s: %s' % (run.id, run.error))
            finally:
                run.ended_at = now_iso()
                run.raw_keep = None
                self.active = None
                run.done.set()
                self.record({'t': 'back' if run.state not in ('refused',) else 'verdict', 'run': run.id,
                             'state': run.state, 'way_back': run.way_back, 'seconds': run.times.get('run_s'),
                             'error': run.error})

    # ------------------------------------------------------------------ runs
    def submit(self, name, raw, args='', deadline_s=0, files=None, reset_after_s=None, by='api', commit=None):
        if cl.LOAD_ADDR + len(raw) + 4 > cl.IMAGE_END:
            raise ValueError('program too large: %d bytes' % len(raw))
        r = Run(name, raw, args, int(deadline_s or 0), files, reset_after_s, by, commit)
        b = self.make_build(name, r.sha256, len(raw), args, commit, by)
        r.build = b['id']
        b['runs'].append(r.id)
        self.save_builds()
        self.runs[r.id] = r
        self.run_order.append(r.id)
        self.queue.put(r)
        self.log('run %s queued: %s %d bytes by %s' % (r.id, name, len(raw), by))
        return r

    def _run(self, run):
        loop = self.loop
        t_start = time.time()
        with self.link_lock:
            h = loop.wait_hello(30.0)
            if h is None:
                run.state, run.error = 'lost', 'no beacon from the loader in 30 s'
                return
            if h['report_state'] not in (0, 1):
                try:
                    old = loop.fetch_report(h, None)
                    self.log('earlier report %s collected first (%s)' % (old['run'], old['state']))
                except RuntimeError as e:
                    self.log('earlier report: %s' % e)
            try:
                raw = program_image(run.raw)
            except ValueError as e:
                run.state, run.error = 'refused', str(e)
                return
            img = cl.Image(raw)
            session = self.new_session(run.sid, {'name': run.name, 'bytes': len(run.raw), 'hash': run.sha256})
            run.state = 'sending'
            self.record({'t': 'push', 'run': run.id, 'chunks_have': 0, 'chunks': img.nchunks, 'bytes': len(img.comp)},
                        session)
            res = loop.transfer(img, run.sid, run.args, run.deadline_s)
            run.transfer = res
            run.times['push_s'] = res['seconds']
            self.record({'t': 'push', 'run': run.id, 'chunks_have': img.nchunks, 'chunks': img.nchunks,
                         'bytes_per_s': res['goodput_Bps'], 'resent': res['resent']}, session)
            ok = res['verdict'] == 0
            self.record({'t': 'verdict', 'run': run.id, 'ok': ok, 'why': None if ok else res['verdict_text']}, session)
            if not ok:
                run.state, run.error = 'refused', res['verdict_text']
                return
            b0 = (loop.hello or {}).get('boot_count')
            t_exec = time.time()
            rv = loop.run_verified(run.sid)
            if rv not in ('exec', 'no answer'):
                run.state, run.error = 'refused', rv
                return
            run.state = 'running'
            self.record({'t': 'exec', 'run': run.id}, session)
            hb = getattr(loop, 'ran_back', None)
            loop.ran_back = None

            def back(m):
                if m[0] == cl.HELLO and cl.parse_hello(m[4])['role'] != 2 and loop.hello and \
                        (loop.hello['boot_count'] != b0 or loop.hello['uptime_ms'] < 1000 * (time.time() - t_exec)):
                    return loop.hello
                return None
            wait_s = (run.deadline_s + 60) if run.deadline_s else 900
            reset_at = t_exec + run.reset_after_s if run.reset_after_s else None
            while hb is None and time.time() - t_exec < wait_s:
                if run.reset_flag.is_set() or (reset_at and time.time() >= reset_at):
                    rung, secs, hb, ack = cl.ladder(loop, run.sid, 20.0, self.log, b0)
                    run.way_back = {'reset': 'stub', 'watchdog or reboot': 'watchdog', None: 'power'}.get(rung, rung)
                    self.record({'t': 'reset', 'run': run.id, 'rung': run.way_back, 'seconds': secs}, session)
                    if hb is None:
                        run.state, run.error = 'lost', 'power cycle needed'
                        return
                    break
                hb = loop.wait(back, 1.0)
            self.client_left('program ended')
            run.times['run_s'] = round(time.time() - t_exec, 3)
            run.files_stats = self.file_service(run.sid).summary()
            if self.disc is not None:
                run.files_stats = dict(run.files_stats, disc=self.disc.summary())
            if hb is None:
                run.state, run.error = 'lost', 'the loader did not come back within %d s' % wait_s
                return
            if hb['report_run'] != run.sid or hb['report_state'] in (0, 1):
                run.state = 'lost'
                run.error = 'loader back without this run (power cycle?)'
                return
            rep = loop.fetch_report(hb, os.path.join(self.data_dir, 'reports'))
            run.times['report_s'] = rep['seconds']
            run.report = open(rep['path'], 'rb').read()
            run.report_crc_ok = rep['crc_ok']
            run.state = RUN_STATE_OF.get(rep['state'], 'lost')
            hdr_way = 'stub' if run.way_back is None else run.way_back
            run.way_back = hdr_way
            text = run.report.decode('latin-1')
            run.summary = parse_summary(text, run.state, rep['exit'], run.times['run_s'], self.tier(), session)
            if 'crash' in run.summary:
                run.crash = run.summary['crash']
            run.times['total_s'] = round(time.time() - t_start, 3)
            session.add({'t': 'end', 'n': session.max_n, 'reason': '%s, exit %s' % (run.state, rep['exit'])})
            session.flush()

    # ------------------------------------------------------------------ control
    def command(self, c, who='api'):
        """pause / resume / step / warp / option / pad to the linked program (answered by its CMDACK), reset (the
        ladder). Returns (http code, body)."""
        cmd = c.get('cmd')
        cid = next(self.cmd_n)
        if cmd == 'reset':
            return self.reset(c.get('level', 'return'))
        if cmd == 'memory':
            return 501, {'ok': False, 'id': 'c%d' % cid, 'error': 'memory reads are not in this loader yet'}
        try:
            code, payload = wire.pack_cmd_payload(c)
        except (ValueError, KeyError, TypeError) as e:
            return 400, {'ok': False, 'id': 'c%d' % cid, 'error': str(e)}
        if self.client is None:
            return 409, {'ok': False, 'id': 'c%d' % cid, 'error': 'no program linked: commands go to a running program'}
        steps = c.get('steps') if cmd == 'pad' else None
        w = {'ev': threading.Event(), 'ack': None}
        self.cmd_wait[cid] = w
        try:
            for _ in range(3):
                self.loop.send(wire.CMD, self.client_run, cid, code, payload)
                if w['ev'].wait(1.0):
                    break
        finally:
            self.cmd_wait.pop(cid, None)
        ack = w['ack']
        if ack is None:
            return 501, {'ok': False, 'id': 'c%d' % cid, 'error': 'not in this program\'s link (no answer to %s)' % cmd}
        if ack['status'] == 1:
            return 501, {'ok': False, 'id': 'c%d' % cid, 'error': ack['text'] or 'the program has no %s' % cmd}
        ok = ack['status'] == 0
        if ok and cmd == 'pause':
            self.paused = True
        elif ok and cmd in ('resume',):
            self.paused = False
        elif ok and cmd == 'step':
            self.paused = True
        if ok and steps and len(steps) > 1:
            threading.Thread(target=self._pad_steps, args=(steps,), daemon=True).start()
        if ok and self.current is not None:
            self.current.add({'t': 'trace', 'lane': 'Commands', 'n': ack['n'] or 0, 'd': 0,
                              'label': '%s %s' % (cmd, c.get('preset') or c.get('name') or c.get('count') or '')})
        return 200, {'ok': ok, 'id': 'c%d' % cid, 'n': ack['n'], 'error': None if ok else ack['text'] or 'refused'}

    def _pad_steps(self, steps):
        rate = (self.declaration or {}).get('rate', 60) or 60
        for st in steps[1:]:
            time.sleep(max(1, int(steps[0].get('frames') or 1)) / rate)
            steps = steps[1:]
            code, payload = wire.pack_cmd_payload({'cmd': 'pad', 'state': st})
            if self.client is None:
                return
            self.loop.send(wire.CMD, self.client_run, next(self.cmd_n), code, payload)

    def reset(self, level='return'):
        if level == 'power':
            return self.power()
        run = self.active
        if run is not None:   # the worker owns the link while a run is on: it climbs the ladder once the program runs
            run.reset_flag.set()
            run.done.wait(40.0)
            return 200, {'ok': run.way_back in ('stub', 'watchdog'), 'id': 'reset', 'rung': run.way_back or 'none',
                         'run': run.id}
        h = self.loop.hello or {}
        if self.client is None and h and time.time() - h.get('at', 0) < 3.0 and h.get('state') in (0, 2, 3):
            # nothing running and the loader beaconing: already back at the loader (Flycast run f2 waited 30 s here)
            return 200, {'ok': True, 'id': 'reset', 'rung': 'none needed', 'seconds': 0.0}
        with self.link_lock:
            b0 = (self.loop.hello or {}).get('boot_count')
            rung, secs, hb, ack = cl.ladder(self.loop, 0, 20.0, self.log, b0)
        self.client_left('reset')
        return 200, {'ok': hb is not None, 'id': 'reset', 'rung': {'reset': 'stub', 'watchdog or reboot': 'watchdog'}.get(
            rung, 'none'), 'seconds': secs}

    def power(self):
        msgs = []
        ok = cl.power_cycle(msgs.append)
        return 200, {'ok': bool(ok), 'id': 'power', 'done': bool(ok), 'say': '; '.join(msgs) or 'power cycle needed'}

    def grab(self, timeout=20.0):
        """A picture of the console's screen now: GRAB to the linked program, which sends its displayed picture as a
        still from its link side. Returns (code, still json)."""
        if self.client is None:
            return 409, {'error': 'no program linked: a grab comes from a running program\'s link (the loader has none)'}
        # a program with live data holds still while its picture is read (the read takes about 0.7 s on the adapter and
        # a moving game would change the buffer under it); it goes on afterwards if it was running
        hold = self.declaration is not None and not self.paused
        if hold and self.command({'cmd': 'pause'})[0] != 200:
            hold = False
        try:
            return self._grab(timeout)
        finally:
            if hold:
                self.command({'cmd': 'resume'})

    def _grab(self, timeout):
        req = next(self.grab_n) & 0xFFFFFFFF
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.client is None:
                return 410, {'error': 'the program left before its picture arrived'}
            self.loop.send(wire.GRAB, self.client_run, req, 0)
            end = time.time() + 2.0
            while time.time() < end:
                got = self.stills.waiters.get(req)
                if got:
                    j = self.stills.meta[got[0]]
                    return (200 if j['crc_ok'] else 502), dict(j, request=req)
                # a grab under way keeps the program busy: wait while blocks arrive
                if any(st['req'] == req for st in self.stills.cur.values()):
                    end = time.time() + 2.0
                time.sleep(0.05)
        return 504, {'error': 'no picture from the program in %.0f s (does its link answer GRAB?)' % timeout}

    def selftest(self, by='api'):
        if not self.selftest_path or not os.path.isfile(self.selftest_path):
            return 501, {'error': 'no self-test program set up (start the bridge with --selftest <testprog.elf>)'}
        raw = open(self.selftest_path, 'rb').read()
        r = self.submit(os.path.basename(self.selftest_path), raw, 'report screen:1', 120, None, None, by)
        return 202, r

    def close(self):
        self.stop_flag.set()
        self.queue.put(None)
        if self.current is not None:
            self.current.close('bridge stopped')
        self.t.close()


def program_image(data):
    if data[:4] == b'\x7fELF':
        lo, img = cl.elf_image(data)
        if lo != cl.LOAD_ADDR:
            raise ValueError('the program loads at %08x, the loader runs programs at %08x' % (lo, cl.LOAD_ADDR))
        return img
    return data
