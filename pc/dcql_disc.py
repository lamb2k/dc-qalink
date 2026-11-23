"""dcql_disc.py: the network disc, PC side (dc-qalink 0.2, lane ql-disc, 2026-11-22). MIT licence.

Serves a program's /cd (lib/dcql_disc.c) from this PC: a base disc (a folder: the built disc tree; or an image:
.gdi with its tracks, .iso) and an overlay folder whose files win. Wire: lib/dcql_disc.h (DOPEN/DINFO, DREAD/DDATA,
DCLOSE, DLOG). Standard library only.

    svc = DiscService(base='disc-tree/', overlay='changed/', whole=False)
    svc.manifest()        # every file: path, size, sha256, side ('disc' = read from GDEMU, 'pc' = served), changed
    svc.handle(loop, typ, run, a, b, payload)   # from the link's datagram tap; True when it was a disc datagram

Modes: overlay (default): the console reads every file from its own disc except the overlay's files that differ from
the base (or every overlay file when no base is given); whole: every file from the PC (base, with the overlay on top).
"""
import hashlib, json, os, struct, sys, threading, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qlsafe   # noqa: E402  (the shared boundaries)

DOPEN, DREAD, DCLOSE, DINFO, DDATA, DLOG = 0x27, 0x28, 0x29, 0x2A, 0x2B, 0x2C   # (0x21 is END, ql-core4)
DISC_TYPES_FROM_CONSOLE = frozenset((DOPEN, DREAD, DCLOSE, DLOG))
DISC_TYPES_FROM_PC = frozenset((DINFO, DDATA))
DO_MOUNT, DO_DIR, DO_OVERLAY = 1, 2, 4
DI_OVERLAY, DI_DIR = 1, 2
DS_OK, DS_NOENT, DS_BASE = 0, -2, -3
DT_WHOLE, DT_OVERFLOW = 1, 2
MAGIC = 0x314D5144   # "DQM1"
CHUNK = 1024
READ_MAX = 32 * 1024
DL_OPEN, DL_READ, DL_CLOSE = 1, 2, 3


def norm(path):
    """The disc's name of a file: '/' separators, no leading '/', no ISO 9660 version (';1')."""
    p = path.replace('\\', '/').lstrip('/')
    return p.split(';', 1)[0]


def key(path):
    return norm(path).upper()


def fnv(path):
    """lib/dcql_disc.c dcql_disc_hash: FNV-1a 32 of the upper-cased normalised name."""
    h = 2166136261
    for c in key(path).encode('latin-1', 'replace'):
        h = ((h ^ c) * 16777619) & 0xFFFFFFFF
    return h


def sha256_file(read, size):
    h = hashlib.sha256()
    off = 0
    while off < size:
        b = read(off, min(1 << 20, size - off))
        if not b:
            break
        h.update(b)
        off += len(b)
    return h.hexdigest()


# ------------------------------------------------------------------------------------------------ sources
class FolderSource:
    """A folder as a disc: every file below it, by its path relative to the folder."""

    kind = 'folder'

    def __init__(self, root):
        self.root = os.path.abspath(root)
        if not os.path.isdir(self.root):
            raise FileNotFoundError('no folder %s' % root)
        self.files = {}   # KEY -> (name as stored, size, real path)
        self.skipped = 0  # entries whose target (links followed) is not a file below the root: never indexed
        for d, dirs, names in os.walk(self.root):
            dirs.sort()
            for n in sorted(names):
                p = os.path.join(d, n)
                real = qlsafe.inside(self.root, p)
                if real is None:
                    self.skipped += 1
                    continue
                rel = norm(os.path.relpath(p, self.root))
                self.files[key(rel)] = (rel, os.path.getsize(real), real)

    def describe(self):
        return {'kind': 'folder', 'path': self.root, 'files': len(self.files)}

    def size(self, k):
        return self.files[k][1]

    def read(self, k, off, n):
        try:   # opened, then checked on the opened object: a link swapped in since the index is refused
            f = qlsafe.open_inside(self.root, self.files[k][2])
        except qlsafe.Refused as e:
            raise PermissionError('%s is no longer a file below %s (%s)' % (self.files[k][0], self.root, e))
        with f:
            f.seek(off)
            return f.read(n)


class ImageSource:
    """A disc image's ISO 9660 file system: .iso (2048- or 2352-byte sectors) or .gdi (its data tracks; the file system
    of a GD-ROM's high-density area starts in the track at LBA 45000). Directory extents are absolute LBAs."""

    kind = 'image'

    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.tracks = []   # (lba, sector bytes, data offset in a sector, file)
        low = path.lower()
        if low.endswith('.gdi'):
            base = os.path.dirname(self.path)
            lines = [ln.split() for ln in open(self.path, encoding='latin-1').read().splitlines() if ln.strip()]
            for t in lines[1:]:
                # number, lba, type (4 data, 0 audio), sector size, file (may be quoted), offset
                lba, typ, ss = int(t[1]), int(t[2]), int(t[3])
                name = ' '.join(t[4:-1]).strip('"')
                if typ == 4:
                    # a track is a file beside the .gdi (links followed): a name that leaves its folder is refused;
                    # a track that is simply absent is left out (only the tracks the file system needs are read)
                    tp = qlsafe.confine(base, name, want=None)
                    if tp is None:
                        raise ValueError('%s: track %s is not beside the descriptor' % (path, name))
                    if os.path.isfile(tp):
                        # opened once here and checked on the opened object (audit round 2): every later read uses
                        # this descriptor, so a track or folder changed while the disc is served cannot change it
                        try:
                            f = qlsafe.open_inside(base, tp)
                        except qlsafe.Refused as e:
                            raise ValueError('%s: track %s refused (%s)' % (path, name, e))
                        self.tracks.append((lba, ss, 16 if ss == 2352 else 0, tp, f))
            hd = [t for t in self.tracks if t[0] >= 45000]
            self.fs_lba = (hd or self.tracks)[0][0]
        elif low.endswith('.iso') or low.endswith('.bin'):
            ss = 2048
            with open(self.path, 'rb') as f:
                f.seek(16 * 2352 + 16 + 1)
                if f.read(5) == b'CD001':
                    ss = 2352
            self.tracks.append((0, ss, 16 if ss == 2352 else 0, self.path, open(self.path, 'rb')))
            self.fs_lba = 0
        elif low.endswith('.cdi'):
            raise NotImplementedError('.cdi images are not read yet: convert it to .gdi or .iso, or serve its folder')
        else:
            raise ValueError('not a disc image: %s (.gdi, .iso)' % path)
        self.tracks.sort(key=lambda t: t[0])
        self._lock = threading.Lock()
        pvd = self.sector(self.fs_lba + 16)
        if pvd[1:6] != b'CD001' or pvd[0] != 1:
            raise ValueError('no ISO 9660 volume in %s' % path)
        self.volume = pvd[40:72].decode('latin-1').strip()
        self.files = {}
        self._walk(pvd[156:156 + 34], '')

    def describe(self):
        return {'kind': 'image', 'path': self.path, 'volume': self.volume, 'files': len(self.files),
                'tracks': len(self.tracks)}

    def sector(self, lba):
        t = None
        for tr in self.tracks:
            if tr[0] <= lba:
                t = tr
        if t is None:
            raise ValueError('LBA %d before the first data track' % lba)
        with self._lock:   # the track's own descriptor, opened and checked once (never reopened by name)
            t[4].seek((lba - t[0]) * t[1] + t[2])
            return t[4].read(2048).ljust(2048, b'\0')

    def _records(self, lba, size):
        out = []
        for s in range((size + 2047) // 2048):
            d = self.sector(lba + s)
            i = 0
            while i < 2048 and d[i]:
                n = d[i]
                out.append(d[i:i + n])
                i += n
        return out

    def _walk(self, rec, prefix, depth=0):
        lba, size = struct.unpack_from('<I', rec, 2)[0], struct.unpack_from('<I', rec, 10)[0]
        for r in self._records(lba, size):
            nl = r[32]
            name = r[33:33 + nl]
            if name in (b'\0', b'\1'):
                continue
            nm = name.decode('latin-1').split(';', 1)[0].rstrip('.')
            elba, esize, flags = struct.unpack_from('<I', r, 2)[0], struct.unpack_from('<I', r, 10)[0], r[25]
            rel = prefix + nm
            if flags & 2:
                if depth < 16:
                    self._walk(r, rel + '/', depth + 1)
            else:
                self.files[key(rel)] = (rel, esize, elba)

    def size(self, k):
        return self.files[k][1]

    def read(self, k, off, n):
        _, size, lba = self.files[k]
        n = max(0, min(n, size - off))
        out = bytearray()
        while n > 0:
            s, o = divmod(off, 2048)
            d = self.sector(lba + s)[o:o + n]
            out += d
            off += len(d)
            n -= len(d)
        return bytes(out)


def open_source(path):
    if path is None:
        return None
    return FolderSource(path) if os.path.isdir(path) else ImageSource(path)


# ------------------------------------------------------------------------------------------------ the service
class DiscService:
    def __init__(self, base=None, overlay=None, whole=False, log=None, record=None, hash_cache=None):
        self.base = open_source(base) if isinstance(base, str) else base
        self.overlay = FolderSource(overlay) if overlay else None
        if whole and self.base is None:
            raise ValueError('whole-disc mode needs a base (a disc folder or image)')
        self.whole = bool(whole)
        self.log = log or (lambda s: None)
        self.record = record or (lambda r: None)
        self.lock = threading.RLock()
        self.handles = {}       # handle -> dict(kind, key, src, size, run, data(for virtual)); at most HANDLES_MAX
        self.by_req = {}        # (run, req) -> handle (a repeated DOPEN keeps its handle)
        self.next = 1
        self.fds = {}           # (run, fd) -> path (from DLOG opens)
        self.reads = []         # the program's reads (DLOG), newest last
        self.stats = {'opens': 0, 'refused': 0, 'base_told': 0, 'requests': 0, 'resend_requests': 0, 'pieces': 0,
                      'bytes': 0, 'req_us': [], 'log_records': 0, 'first_at': None, 'last_at': None}
        self._cache = {}
        self._cache_path = hash_cache
        if hash_cache and os.path.isfile(hash_cache):
            try:
                self._cache = json.load(open(hash_cache, encoding='utf-8'))
            except (OSError, ValueError):
                self._cache = {}
        self._manifest = self._build_manifest()
        if hash_cache:
            try:
                json.dump(self._cache, open(hash_cache, 'w', encoding='utf-8'))
            except OSError:
                pass

    # ------------------------------------------------------------------ manifest
    def _sha(self, src, k):
        p = src.files[k]
        if isinstance(src, FolderSource):
            st = os.stat(p[2])
            ck = '%s|%d|%d' % (p[2], st.st_size, int(st.st_mtime))
        else:
            ck = '%s|%s|%d|%d' % (src.path, k, p[1], p[2])
        h = self._cache.get(ck)
        if h is None:
            h = self._cache[ck] = sha256_file(lambda o, n: src.read(k, o, n), src.size(k))
        return h

    def _build_manifest(self):
        files = {}
        if self.base:
            for k, v in self.base.files.items():
                files[k] = {'path': v[0], 'size': v[1], 'sha256': self._sha(self.base, k),
                            'side': 'pc' if self.whole else 'disc', 'source': 'base', 'changed': False}
        if self.overlay:
            for k, v in self.overlay.files.items():
                h = self._sha(self.overlay, k)
                b = files.get(k)
                same = b is not None and b['sha256'] == h and b['size'] == v[1]
                files[k] = {'path': v[0], 'size': v[1], 'sha256': h, 'source': 'overlay',
                            'side': 'pc' if (self.whole or not same) else 'disc', 'changed': not same,
                            'base_sha256': b['sha256'] if b else None, 'new': b is None}
        rows = [files[k] for k in sorted(files)]
        canon = '\n'.join('%s %d %s %s' % (r['path'], r['size'], r['sha256'], r['side']) for r in rows)
        return {'mode': 'whole' if self.whole else 'overlay', 'files': rows,
                'sha256': hashlib.sha256(canon.encode('utf-8')).hexdigest(),
                'base': self.base.describe() if self.base else None,
                'overlay': self.overlay.describe() if self.overlay else None,
                'served': sum(1 for r in rows if r['side'] == 'pc'), 'count': len(rows)}

    def manifest(self):
        return self._manifest

    def served(self):
        """KEY -> (source, key) of every file the PC serves."""
        out = {}
        for r in self._manifest['files']:
            if r['side'] == 'pc':
                k = key(r['path'])
                out[k] = self.overlay if r['source'] == 'overlay' else self.base
        return out

    def file_bytes(self, path, off=0, n=None):
        """A disc file as the console would see it with the network disc on (overlay first), for the app's viewers."""
        k = key(path)
        for src in (self.overlay, self.base):
            if src is not None and k in src.files:
                size = src.size(k)
                return src.read(k, off, size - off if n is None else n)
        raise FileNotFoundError(path)

    def table(self):
        m = self._manifest
        if self.whole:
            ents = []
        else:
            ents = sorted((fnv(r['path']), r['size']) for r in m['files'] if r['side'] == 'pc')
        flags = DT_WHOLE if self.whole else 0
        head = struct.pack('<IIII', MAGIC, len(ents), flags, m['count']) + bytes.fromhex(m['sha256'])[:16]
        return head + b''.join(struct.pack('<II', h, s) for h, s in ents)

    def listing(self, rel):
        """A directory's entries as the console reads them: {u32 size | 0xffffffff dir, u16 len, name}."""
        pre = key(rel).rstrip('/')
        pre = pre + '/' if pre else ''
        names = {}
        for r in self._manifest['files']:
            k = key(r['path'])
            if not k.startswith(pre):
                continue
            rest = r['path'][len(pre):]
            if '/' in rest:
                names.setdefault(rest.split('/', 1)[0], 0xFFFFFFFF)
            else:
                names[rest] = r['size']
        if pre and not names:
            return None
        out = bytearray()
        for n in sorted(names):
            b = n.encode('latin-1', 'replace')
            out += struct.pack('<IH', names[n], len(b)) + b
        return bytes(out)

    # ------------------------------------------------------------------ the wire
    def handle(self, loop, typ, run, a, b, pl):
        if typ not in DISC_TYPES_FROM_CONSOLE:
            return False
        with self.lock:
            if typ == DOPEN:
                self._open(loop, run, a, pl)
            elif typ == DREAD:
                self._read(loop, run, a, pl)
            elif typ == DCLOSE:
                if len(pl) >= 8:
                    self._timing(*struct.unpack_from('<II', pl, 0))
                self.handles.pop(a, None)
            elif typ == DLOG:
                self._log(run, pl)
        return True

    HANDLES_MAX = 64            # open disc handles for the bridge's lifetime (virtual listings are held in memory)
    DS_MFILE = -24              # too many open (as the /pc service answers)

    def retire(self, run):
        """A run's handles and request ids dropped (run end, the program's link lost)."""
        with self.lock:
            for h in [h for h, v in self.handles.items() if v.get('run') == run]:
                self.handles.pop(h, None)
            self.by_req = {k: v for k, v in self.by_req.items() if k[0] != run}
            self.fds = {k: v for k, v in self.fds.items() if k[0] != run}

    def _info(self, loop, run, req, status, h=0, size=0, flags=0):
        loop.send(DINFO, run, req, 0, struct.pack('<iIII', status, h, size, flags))

    def _open(self, loop, run, req, pl):
        if len(pl) < 4:
            return
        flags = struct.unpack_from('<I', pl, 0)[0]
        rel = pl[4:].decode('latin-1', 'replace')
        h = self.by_req.get((run, req))
        if h is not None and h in self.handles:   # a repeated DOPEN (its answer lost)
            v = self.handles[h]
            return self._info(loop, run, req, DS_OK, h, v['size'], v['flags'])
        if len(self.handles) >= self.HANDLES_MAX:   # a cap before one more is made
            self.stats['refused'] += 1
            self.stats['too_many'] = self.stats.get('too_many', 0) + 1
            return self._info(loop, run, req, self.DS_MFILE)
        if flags & DO_MOUNT:
            data = self.table()
            v = {'kind': 'virtual', 'data': data, 'size': len(data), 'flags': 0, 'path': '(mount table)'}
            self.log('disc: mount by run %08x: %s, %d of %d files from the PC, manifest %s' % (
                run, self._manifest['mode'], self._manifest['served'], self._manifest['count'],
                self._manifest['sha256'][:16]))
            self.record({'t': 'disc', 'op': 'mount', 'run': '%08x' % run, 'mode': self._manifest['mode'],
                         'served': self._manifest['served'], 'files': self._manifest['count'],
                         'manifest': self._manifest['sha256']})
        elif flags & DO_DIR:
            data = self.listing(rel)
            if data is None:
                self.stats['refused'] += 1
                return self._info(loop, run, req, DS_NOENT)
            v = {'kind': 'virtual', 'data': data, 'size': len(data), 'flags': DI_DIR, 'path': rel}
        else:
            k = key(rel)
            src = self.served().get(k)
            if src is None:
                known = (self.base and k in self.base.files) or (self.overlay and k in self.overlay.files)
                if flags & DO_OVERLAY and known:
                    self.stats['base_told'] += 1   # a table hash collision or overflow: the console's own disc has it
                    return self._info(loop, run, req, DS_BASE)
                if flags & DO_OVERLAY:
                    return self._info(loop, run, req, DS_BASE)
                self.stats['refused'] += 1
                self.log('disc: %s refused (not on the served disc)' % rel)
                return self._info(loop, run, req, DS_NOENT)
            v = {'kind': 'file', 'src': src, 'key': k, 'size': src.size(k), 'path': norm(rel),
                 'flags': DI_OVERLAY if src is self.overlay else 0}
            self.stats['opens'] += 1
        h = self.next
        self.next += 1
        v['run'] = run
        self.handles[h] = v
        self.by_req[(run, req)] = h
        if len(self.by_req) > 4096:
            self.by_req = dict(list(self.by_req.items())[-1024:])
        self._info(loop, run, req, DS_OK, h, v['size'], v['flags'])

    def _timing(self, prev_id, prev_us):
        if prev_id and prev_us and len(self.stats['req_us']) < 100000:
            self.stats['req_us'].append(prev_us)

    def _read(self, loop, run, req, pl):
        if len(pl) < 16:
            return
        h, off, n, mask = struct.unpack_from('<IIII', pl, 0)
        if len(pl) >= 24:
            self._timing(*struct.unpack_from('<II', pl, 16))
        v = self.handles.get(h)
        if v is None or n > READ_MAX:
            return
        now = time.time()
        self.stats['first_at'] = self.stats['first_at'] or now
        self.stats['last_at'] = now
        self.stats['requests'] += 1
        full = (1 << ((n + CHUNK - 1) // CHUNK)) - 1 if n < 32 * CHUNK else 0xFFFFFFFF
        if mask & full != full:
            self.stats['resend_requests'] += 1
        n = max(0, min(n, v['size'] - off))
        data = v['data'][off:off + n] if v['kind'] == 'virtual' else v['src'].read(v['key'], off, n)
        for i in range((len(data) + CHUNK - 1) // CHUNK):
            if mask & (1 << i):
                piece = data[i * CHUNK:(i + 1) * CHUNK]
                loop.send(DDATA, run, req, i, piece)
                self.stats['pieces'] += 1
                self.stats['bytes'] += len(piece)

    def _log(self, run, pl):
        i = 0
        while i + 4 <= len(pl):
            kind, side, fd = pl[i], pl[i + 1], struct.unpack_from('<H', pl, i + 2)[0]
            sd = 'pc' if side == 1 else 'disc'
            if kind == DL_OPEN and i + 8 <= len(pl):
                size = struct.unpack_from('<I', pl, i + 4)[0]
                end = pl.index(b'\0', i + 8) if b'\0' in pl[i + 8:] else len(pl)
                path = pl[i + 8:end].decode('latin-1', 'replace')
                self.fds[(run, fd)] = path
                self.record({'t': 'disc', 'op': 'open', 'run': '%08x' % run, 'path': path, 'size': size, 'side': sd})
                i = end + 1
            elif kind == DL_READ and i + 20 <= len(pl):
                off, n, us, calls = struct.unpack_from('<IIII', pl, i + 4)
                r = {'t': 'disc', 'op': 'read', 'run': '%08x' % run, 'path': self.fds.get((run, fd), 'fd %d' % fd),
                     'off': off, 'bytes': n, 'ms': round(us / 1000.0, 3), 'calls': calls, 'side': sd}
                self.reads.append(r)
                if len(self.reads) > 20000:
                    del self.reads[:10000]
                self.record(dict(r))
                i += 20
            elif kind == DL_CLOSE:
                self.fds.pop((run, fd), None)
                i += 4
            else:
                break
            self.stats['log_records'] += 1

    def summary(self):
        s = {k: v for k, v in self.stats.items() if k not in ('req_us', 'first_at', 'last_at')}
        us = sorted(self.stats['req_us'])
        if us:
            s['request_ms'] = {'n': len(us), 'mean': round(sum(us) / len(us) / 1000.0, 3),
                               'p50': round(us[len(us) // 2] / 1000.0, 3),
                               'p99': round(us[int(0.99 * (len(us) - 1))] / 1000.0, 3),
                               'worst': round(us[-1] / 1000.0, 3)}
        if self.stats['first_at'] and self.stats['last_at'] and self.stats['last_at'] > self.stats['first_at']:
            s['seconds'] = round(self.stats['last_at'] - self.stats['first_at'], 3)
        by_side = {'pc': [0, 0, 0.0], 'disc': [0, 0, 0.0]}
        for r in self.reads:
            a = by_side[r['side']]
            a[0] += 1
            a[1] += r['bytes']
            a[2] += r['ms']
        s['program_reads'] = {k: {'records': v[0], 'bytes': v[1], 'ms': round(v[2], 3),
                                  'MBps': round(v[1] / (v[2] * 1000.0), 3) if v[2] else None} for k, v in by_side.items()}
        s['manifest'] = self._manifest['sha256']
        s['mode'] = self._manifest['mode']
        return s
