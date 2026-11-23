"""qlsafe.py: the PC side's boundaries (dc-qalink 0.2, lane ql-fix-pc, 2026-11-23). Standard library only. MIT licence.

One place for the checks every PC entry point makes before it touches a file, allocates for a peer, or treats a
datagram as the console's:

  confine(root, rel)      a name the console (or a disc descriptor) gave, below `root`: the real path of the target
                          after links are followed, or None when it leaves the root (or names nothing)
  inside(root, path)      the same for a path the caller already joined or gave whole (asset imports, MCP files)
  read_capped(path, n)    a file's bytes, refused before reading when it is over `n` bytes
  b64_capped(text, n)     base64 decoded, refused before decoding when the result would be over `n` bytes
  EndAssembler            the run's ending (END head + tail pieces), bounded and run-specific, that says whether the
                          declared tail is held whole with no overlap (the CLI and the bridge both use it)

  open_inside(root, path) a file opened first and then checked on the opened object (round 2 of the audit): the real
  open_confined(root,rel) path of the open descriptor (/proc/self/fd, else the real path whose device and inode must
                          be the descriptor's) must be a regular file below the root, or the file is closed and refused

Serving code opens with open_inside / open_confined, so a link swapped in after a check cannot change what is read:
what is checked is what was opened. confine() and inside() remain for indexing and listing."""
import base64, binascii, os, stat, struct, zlib


class Refused(ValueError):
    """A request outside a boundary (a path outside its root, a size over its limit)."""


def _real(p):
    return os.path.normcase(os.path.realpath(p))


def inside(root, path, want='file'):
    """The real path of `path` when it (after links) is `root` itself or below it, and is a `want` ('file', 'dir' or
    None for either); else None. `root` None refuses everything."""
    if not root or path is None:
        return None
    r = _real(root)
    p = _real(path)
    try:
        if os.path.commonpath([r, p]) != r:
            return None
    except ValueError:   # another drive (Windows)
        return None
    if want == 'file' and not os.path.isfile(p):
        return None
    if want == 'dir' and not os.path.isdir(p):
        return None
    return os.path.realpath(path)


def confine(root, rel, want='file'):
    """`rel` is a name relative to `root` ('/' or '\\' separated) from a peer or a descriptor: no drive, no absolute
    path, no '..' or empty part, and its target (after links) below the root. The real path, or None."""
    if not root or rel is None:
        return None
    rel = rel.replace('\\', '/')
    if '\0' in rel:
        return None
    rel = rel.lstrip('/')
    parts = rel.split('/')
    if not rel or any(x in ('', '.', '..') for x in parts) or ':' in parts[0]:
        return None
    return inside(root, os.path.join(root, *parts), want)


def _win_handle_path(fd):
    """Windows: the final path of the object an open descriptor names (GetFinalPathNameByHandleW), else None."""
    try:
        import ctypes, msvcrt
        from ctypes import wintypes
        h = msvcrt.get_osfhandle(fd)
        f = ctypes.windll.kernel32.GetFinalPathNameByHandleW
        f.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        buf = ctypes.create_unicode_buffer(32768)
        n = f(h, buf, 32768, 0)
        if not 0 < n < 32768:
            return None
        p = buf.value
        if p.startswith('\\\\?\\UNC\\'):
            p = '\\\\' + p[8:]
        elif p.startswith('\\\\?\\'):
            p = p[4:]
        return p
    except Exception:
        return None


def _fd_path(fd):
    """The real path of the object an open descriptor names, from the descriptor itself: Linux /proc/self/fd, Windows
    GetFinalPathNameByHandleW. None where the system cannot say (the caller then refuses)."""
    if os.name == 'nt':
        return _win_handle_path(fd)
    try:
        p = os.readlink('/proc/self/fd/%d' % fd)
    except OSError:
        p = None
    if p is None and hasattr(os, 'F_GETPATH'):   # macOS
        try:
            import fcntl
            p = fcntl.fcntl(fd, os.F_GETPATH, b'\0' * 1024).split(b'\0', 1)[0].decode()
        except OSError:
            p = None
    return p if p and p.startswith('/') and not p.endswith(' (deleted)') else None


def _below(root_real, real):
    try:
        return os.path.commonpath([root_real, os.path.normcase(real)]) == root_real
    except ValueError:   # another drive
        return False


def _checked(f, name, root_real):
    """An opened file kept only when the descriptor itself says it is a regular file below root_real (round 3: both
    the identity and the containment come from the opened descriptor, on every platform)."""
    try:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise Refused('%s is not a regular file' % name)
        real = _fd_path(f.fileno())
        if real is None:
            raise Refused('%s: this system cannot say what an open file is; refused' % name)
        if not _below(root_real, real):
            raise Refused('%s is not below its root' % name)
        return f
    except BaseException:
        f.close()
        raise


def _open_ro(path, dir_fd=None):
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NONBLOCK', 0)
    try:
        fd = os.open(path, flags, dir_fd=dir_fd) if dir_fd is not None else os.open(path, flags)
    except OSError as e:
        raise Refused('%s: %s' % (os.path.basename(str(path)), e.strerror or e))
    if hasattr(os, 'set_blocking'):
        try:
            os.set_blocking(fd, True)
        except OSError:
            pass
    return os.fdopen(fd, 'rb')


def open_inside(root, path):
    """`path` opened for reading, then checked on the opened object: a regular file whose real path (of the open
    descriptor) is below `root`. The open file, or Refused (the file is closed first)."""
    if not root or path is None:
        raise Refused('no root')
    return _checked(_open_ro(path), os.path.basename(str(path)), _real(root))


def read_inside(root, path, limit):
    """A file below `root` read through the descriptor that was checked (open, check, read: no second open), refused
    when it is over `limit` bytes."""
    with open_inside(root, path) as f:
        n = os.fstat(f.fileno()).st_size
        if n > limit:
            raise Refused('%s is %d bytes, over the %d-byte limit' % (os.path.basename(str(path)), n, limit))
        data = f.read(limit + 1)
    if len(data) > limit:
        raise Refused('%s grew over the %d-byte limit while read' % (os.path.basename(str(path)), limit))
    return data


class Anchor:
    """A served folder anchored once, for the lifetime of the service that serves it (round 3): its real path and
    identity are taken when the service starts, and (where the system has dir_fd) it is held open, so every name is
    opened relative to that very directory object. Each open is checked on its descriptor against the anchor: a folder
    swapped or relinked afterwards never becomes the new authority."""

    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.real = _real(self.root)
        st = os.stat(self.real)
        if not stat.S_ISDIR(st.st_mode):
            raise Refused('%s is not a folder' % root)
        self.ident = (st.st_dev, st.st_ino)
        self.fd = None
        if os.open in getattr(os, 'supports_dir_fd', ()) and hasattr(os, 'O_DIRECTORY'):
            self.fd = os.open(self.real, os.O_RDONLY | os.O_DIRECTORY)
            dst = os.fstat(self.fd)
            if (dst.st_dev, dst.st_ino) != self.ident:
                self.close()
                raise Refused('%s changed while it was anchored' % root)

    def open(self, rel):
        """A peer's name (the rules of confine) below the anchored folder, opened and checked; Refused otherwise."""
        if rel is None or '\0' in rel:
            raise Refused('a NUL in the name')
        rel = rel.replace('\\', '/').lstrip('/')
        parts = rel.split('/')
        if not rel or any(x in ('', '.', '..') for x in parts) or ':' in parts[0]:
            raise Refused('%r is not a plain name below the root' % rel[:80])
        if self.fd is not None:   # resolved from the anchored directory object itself
            f = _open_ro(os.path.join(*parts), dir_fd=self.fd)
            now = _fd_path(self.fd)
            root_real = os.path.normcase(now) if now else self.real
        else:                     # no dir_fd (Windows): the anchored folder must still be the same object
            st = os.stat(self.real)
            if (st.st_dev, st.st_ino) != self.ident:
                raise Refused('the served folder %s was replaced' % self.root)
            f = _open_ro(os.path.join(self.real, *parts))
            root_real = self.real
        return _checked(f, os.path.basename(rel), root_real)

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None


def open_confined(root, rel):
    """A peer's name below `root` (the rules of confine) opened and checked on the opened object; Refused otherwise."""
    if not root or rel is None:
        raise Refused('no root')
    rel = rel.replace('\\', '/')
    if '\0' in rel:
        raise Refused('a NUL in the name')
    rel = rel.lstrip('/')
    parts = rel.split('/')
    if not rel or any(x in ('', '.', '..') for x in parts) or ':' in parts[0]:
        raise Refused('%r is not a plain name below the root' % rel[:80])
    return open_inside(root, os.path.join(root, *parts))


def read_capped(path, limit):
    """A file's bytes; Refused before any read when it is over `limit` bytes (and when it grows past it meanwhile)."""
    n = os.path.getsize(path)
    if n > limit:
        raise Refused('%s is %d bytes, over the %d-byte limit' % (os.path.basename(path), n, limit))
    with open(path, 'rb') as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise Refused('%s grew over the %d-byte limit while read' % (os.path.basename(path), limit))
    return data


def b64_capped(text, limit):
    """base64 text decoded; Refused before decoding when it would be over `limit` bytes, or is not base64."""
    if not isinstance(text, (str, bytes)):
        raise Refused('base64 must be text')
    if len(text) > (limit + 2) // 3 * 4 + 4:
        raise Refused('base64 of %d characters decodes to over the %d-byte limit' % (len(text), limit))
    try:
        out = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as e:
        raise Refused('not base64: %s' % e)
    if len(out) > limit:
        raise Refused('decodes to %d bytes, over the %d-byte limit' % (len(out), limit))
    return out


# ------------------------------------------------------------------------------------------------ the run's ending
END_HEAD = 0xFFFFFFFF
END_LEN = 64
END_TAIL_MAX = 32 * 1024    # the loader's tail is at most 32 KiB (loader/client.c); more than this is refused
END_PIECES_MAX = 256
END_KEYS = ('state', 'exit', 'crash_evt', 'crash_pc', 'crash_pr', 'crash_sp', 'report_len', 'report_crc', 'sent_off',
            'sent_len', 'flags', 'uptime_ms', 'wdt_feeds', 'wdt_gap_us', 'link_up_ms', 'path')


def parse_end(pl):
    """cl_end_t (include/dcql_loop.h) from an END head's payload, or None when it is short or inconsistent."""
    if len(pl) < END_LEN:
        return None
    e = dict(zip(END_KEYS, struct.unpack_from('<IiIIIIIIIIIIIIII', pl, 0)))
    if e['sent_len'] > END_TAIL_MAX or e['sent_off'] + e['sent_len'] > e['report_len']:
        return None
    return e


class EndAssembler:
    """The END of one run: its head and the pieces of its report tail. Bounded (END_TAIL_MAX bytes, END_PIECES_MAX
    pieces, only this run's datagrams); `missing()` counts the declared tail bytes not held as one contiguous,
    non-overlapping cover, which is what the ENDACK carries; `result()` the ending once the head is in."""

    def __init__(self, run):
        self.run = run & 0xFFFFFFFF
        self.head = None
        self.pieces = {}        # offset -> bytes
        self.held = 0
        self.refused = 0
        self.at = None

    def on(self, run, a, pl):
        """An END datagram (run, a, payload). Returns 'head', 'piece' or None (refused / another run's)."""
        if (run & 0xFFFFFFFF) != self.run:
            self.refused += 1
            return None
        if a == END_HEAD:
            e = parse_end(pl)
            if e is None:
                self.refused += 1
                return None
            if self.head is not None and (e['report_len'], e['report_crc'], e['sent_off'], e['sent_len']) != (
                    self.head['report_len'], self.head['report_crc'], self.head['sent_off'], self.head['sent_len']):
                self.pieces, self.held = {}, 0   # a different ending of the same run: the newer head wins
            self.head = e
            self._trim()
            return 'head'
        if not pl or a in self.pieces and self.pieces[a] == bytes(pl):
            return 'piece' if pl else None
        if len(self.pieces) >= END_PIECES_MAX or self.held + len(pl) > END_TAIL_MAX:
            self.refused += 1
            return None
        if self.head is not None and not (self.head['sent_off'] <= a and a + len(pl) <=
                                          self.head['sent_off'] + self.head['sent_len']):
            self.refused += 1
            return None
        old = self.pieces.get(a)
        self.held += len(pl) - (len(old) if old else 0)
        self.pieces[a] = bytes(pl)
        return 'piece'

    def _trim(self):
        lo, hi = self.head['sent_off'], self.head['sent_off'] + self.head['sent_len']
        for o in [o for o, v in self.pieces.items() if o < lo or o + len(v) > hi]:
            self.held -= len(self.pieces.pop(o))
            self.refused += 1

    def cover(self):
        """(bytes covered from sent_off onward as one run of abutting pieces, True when some piece overlaps another)."""
        if self.head is None:
            return 0, False
        cur = self.head['sent_off']
        overlap = False
        for o in sorted(self.pieces):
            v = self.pieces[o]
            if o < cur:
                overlap = True
                continue
            if o > cur:
                break
            cur = o + len(v)
        return cur - self.head['sent_off'], overlap

    def missing(self):
        """Declared tail bytes not yet held whole (the ENDACK's b); the whole tail when no head is in."""
        if self.head is None:
            return 0xFFFF
        _, overlap = self.cover()
        cur, held = self.head['sent_off'], 0   # the declared range's bytes held by any piece (their union)
        for o in sorted(self.pieces):
            end = o + len(self.pieces[o])
            if end > cur:
                held += end - max(o, cur)
                cur = end
        missing = max(0, self.head['sent_len'] - held)
        return max(1, missing) if overlap else missing   # an overlap is never a whole tail

    def tail(self):
        if self.head is None:
            return b''
        got, _ = self.cover()
        out = bytearray()
        cur = self.head['sent_off']
        for o in sorted(self.pieces):
            if o == cur and cur - self.head['sent_off'] < got:
                out += self.pieces[o]
                cur += len(self.pieces[o])
        return bytes(out[:self.head['sent_len']])

    def result(self):
        """The ending, or None with no head: state, exit, the tail, whether it is complete (every declared byte held
        once) and whole (the tail is the whole report) and its CRC agrees."""
        e = self.head
        if e is None:
            return None
        got, overlap = self.cover()
        tail = self.tail()
        complete = not overlap and got == e['sent_len']
        whole = complete and e['sent_off'] == 0 and len(tail) == e['report_len']
        return {'end': e, 'tail': tail, 'complete': complete, 'whole': whole, 'overlap': overlap,
                'crc_ok': whole and (zlib.crc32(tail) & 0xFFFFFFFF) == e['report_crc'], 'refused': self.refused}
