#!/usr/bin/env python3
"""test_safety2.py: the PC side's boundaries, round 2 of the 0.2 audit (lane ql-fix-pc, 2026-11-23). Linux (symbolic
links, /proc); Python 3.10+, standard library (numpy for the PNG row, as pc/assets needs it).

  python tests/test_safety2.py [--out DIR] [--pc DIR] [--controls]

Rows (pinned in PINNED, see tests/ctlrows.py): B1 the listener's LAN opt-in, B2 file boundaries (plugin and loader
disc routes, GDI tracks pinned on the opened object, a run's subfolder fails closed), B3 bounded state (file services
and disc handles retired and capped, ELF span, PNG size and HELLO's report length checked before allocation, the run
queue capped, finished runs drop their program, MCP uploads checked before they are serialized), B4 evidence kept
(a valid END survives reset-ladder exhaustion and a failed or bad-CRC loader report; the way back named from
evidence), B5 still receivers (completion receipts, abandoned transfers expire, exact block lengths), B6 the control
harness itself.

--controls with --pc naming 2ff9b9d's pc/ (the tree the round-2 audit read): every row with an expected discrepancy
must fail with exactly it; an error is a control FAIL."""
import argparse, io, json, os, shutil, socket, struct, subprocess, sys, tempfile, threading, time, urllib.error, \
    urllib.request, zipfile, zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ctlrows   # noqa: E402

# row id -> the discrepancy 2ff9b9d must show (None: a new-tree-only row)
PINNED = {
    'B1.cli_wildcard': 'bound 0.0.0.0',
    'B1.core_wildcard': 'bound 0.0.0.0',
    'B1.allow_lan': None,
    'B1.flycast_exception': None,
    'B2.plugin_link': 'served linked file',
    'B2.loader_zip_link': 'zipped linked file',
    'B2.gdi_swap': 'read swapped track',
    'B2.subfolder_closed': 'widened to root',
    'B2.subfolder_api': None,
    'B3.files_retired': 'descriptors kept',
    'B3.files_capped': 'descriptors kept',
    'B3.disc_capped': 'handles uncapped',
    'B3.disc_retired': 'handles kept',
    'B3.elf_span': 'allocated from span',
    'B3.png_dims': 'allocated from header',
    'B3.report_len': 'requested unchecked length',
    'B3.report_offsets': 'stored unsolicited block',
    'B3.queue_capped': 'queue uncapped',
    'B3.raw_dropped': 'payload kept',
    'B3.mcp_upload': 'serialized oversize',
    'B4.ladder_exhausted': 'lost',
    'B4.bad_crc_report': 'bad loader report published',
    'B4.failed_report': 'lost',
    'B4.label_full_restart': 'labelled stub',
    'B4.label_stub': None,
    'B5.bridge_receipt': 'reopened',
    'B5.cli_receipts_bounded': 'kept every completed still',
    'B5.bridge_expire': 'slots held',
    'B5.cli_expire': 'slots held',
    'B5.bridge_exact_len': 'short block counted',
    'B5.cli_exact_len': 'short block counted',
    'B6.harness': None,
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


def udp_local_addr(port):
    """A UDP port's bound address from the kernel's table."""
    for ln in open('/proc/net/udp').read().splitlines()[1:]:
        ip, p = ln.split()[1].split(':')
        if int(p, 16) == port:
            return socket.inet_ntoa(struct.pack('<I', int(ip, 16)))
    return None


def nfds():
    return len(os.listdir('/proc/self/fd'))


class FakeLoop:
    def __init__(self):
        self.sent = []

    def send(self, *a):
        self.sent.append(a)


def http_get(port, path):
    try:
        with urllib.request.urlopen('http://127.0.0.1:%d%s' % (port, path), timeout=10) as f:
            return f.status, f.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# ------------------------------------------------------------------------------------------------ B1
def b1(R, ctx):
    cl, core, pcdir, root = ctx['cl'], ctx['core'], ctx['pcdir'], ctx['root']

    def cli_wildcard():
        http = free_port()
        d = tempfile.mkdtemp(dir=root)
        p = subprocess.Popen([sys.executable, '-u', os.path.join(pcdir, 'qalink_bridge.py'), '--port', str(http),
                              '--udp-port', '0', '--udp-host', '0.0.0.0', '--data', d, '--quiet'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        port, rc = None, None
        end = time.time() + 15
        while time.time() < end and port is None and rc is None:
            rc = p.poll()
            try:
                with urllib.request.urlopen('http://127.0.0.1:%d/v0/discovery' % http, timeout=2) as f:
                    port = json.loads(f.read())['port']
            except (OSError, ValueError):
                time.sleep(0.2)
        bound = udp_local_addr(port) if port else None
        if p.poll() is None:
            p.terminate()
        p.wait(10)
        err = p.stderr.read().decode('latin-1')
        if bound:
            return False, 'bound %s' % bound, {'bound': bound}
        return rc == 2 and '--allow-lan' in err, 'refused to start', {'rc': rc, 'stderr': err.strip()[-160:]}
    R.row('B1.cli_wildcard', '--udp-host 0.0.0.0 without --allow-lan: the bridge refuses to start', cli_wildcard)

    def core_wildcard():
        try:
            b = core.Bridge(os.path.join(root, 'b1-core'), ('0.0.0.0', 0), 'auto', 'flycast', only_local=True)
        except PermissionError as e:
            return True, 'refused', {'error': str(e)[:120]}
        bound = udp_local_addr(b.t.port)
        b.close()
        return False, 'bound %s' % bound, {'bound': bound}
    R.row('B1.core_wildcard', 'Bridge class: a wildcard bind with only-local is refused (kernel table)', core_wildcard)

    def allow_lan():
        b = core.Bridge(os.path.join(root, 'b1-lan'), ('0.0.0.0', 0), 'auto', 'flycast', only_local=False)
        bound = udp_local_addr(b.t.port)
        b.close()
        return bound == '0.0.0.0', 'bound %s' % bound, {'bound': bound}
    R.row('B1.allow_lan', 'positive: with --allow-lan the wildcard is bound (kernel table)', allow_lan)

    def flycast_exception():
        real = core.local_addresses
        core.local_addresses = lambda: {'127.0.0.1', '192.0.2.10'}   # this PC's LAN address (documentation range)
        try:
            ok_fly = core.bind_refused('192.0.2.10', '192.0.2.10:52060', True)
            typed = core.bind_refused('192.0.2.10', 'auto', True)
            other = core.bind_refused('192.0.2.10', '192.0.2.99:52060', True)
            wild = core.bind_refused('0.0.0.0', '0.0.0.0:52060', True)
            lan = core.bind_refused('192.0.2.10', 'auto', False)
        finally:
            core.local_addresses = real
        ok = ok_fly is None and typed and other and wild and lan is None
        return ok, 'one exception' if ok else 'wrong', {'flycast': ok_fly, 'typed': bool(typed), 'other': bool(other),
                                                         'wild': bool(wild)}
    R.row('B1.flycast_exception', 'positive: the one exception, --console of this PC\'s own address, binds only it',
          flycast_exception)


# ------------------------------------------------------------------------------------------------ B2
def b2(R, ctx):
    cl, core, dcql_disc, root = ctx['cl'], ctx['core'], ctx['dcql_disc'], ctx['root']
    outside = os.path.join(root, 'b2-outside')
    os.makedirs(outside)
    open(os.path.join(outside, 'secret.js'), 'w').write('SECRET')

    def api_bridge(**kw):
        from bridge.api import serve
        b = core.Bridge(os.path.join(root, 'b2-api-%d' % len(os.listdir(root))), ('127.0.0.1', 0), '127.0.0.1:1',
                        'mock', autoconnect=False, **kw)
        port = free_port()
        serve(b, '127.0.0.1', port, 'run')
        return b, port

    def plugin_link():
        plug = os.path.join(root, 'b2-plugins')
        os.makedirs(plug)
        open(os.path.join(plug, 'ok.js'), 'w').write('export const ok = 1')
        os.symlink(os.path.join(outside, 'secret.js'), os.path.join(plug, 'evil.js'))
        b, port = api_bridge()
        b.asset_plugins = plug
        try:
            c1, body1 = http_get(port, '/v0/plugins/evil.js')
            c2, body2 = http_get(port, '/v0/plugins/ok.js')
        finally:
            b.close()
        leaked = c1 == 200 and b'SECRET' in body1
        return not leaked and c2 == 200, 'served linked file' if leaked else 'refused', {'link': c1, 'plain': c2}
    R.row('B2.plugin_link', 'GET /v0/plugins: a plugin linked out of its folder is refused (a plain one served)',
          plugin_link)

    def loader_zip_link():
        ld = os.path.join(root, 'b2-loader')
        os.makedirs(ld)
        open(os.path.join(ld, 'disc.gdi'), 'w').write('1\n')
        os.symlink(os.path.join(outside, 'secret.js'), os.path.join(ld, 'leak.txt'))
        b, port = api_bridge(loader_disc=ld)
        try:
            c, body = http_get(port, '/v0/loader')
        finally:
            b.close()
        names = zipfile.ZipFile(io.BytesIO(body)).namelist() if c == 200 else []
        leaked = 'leak.txt' in names
        return c == 200 and names == ['disc.gdi'], 'zipped linked file' if leaked else 'refused', {'names': names}
    R.row('B2.loader_zip_link', 'GET /v0/loader: a file linked out of the loader disc folder is not zipped',
          loader_zip_link)

    def gdi_swap():
        def iso(vol):
            d = bytearray(18 * 2048)
            d[16 * 2048:16 * 2048 + 6] = b'\x01CD001'
            d[16 * 2048 + 40:16 * 2048 + 44] = vol
            rec = bytearray(34)
            rec[0] = 34
            struct.pack_into('<I', rec, 2, 17)
            struct.pack_into('<I', rec, 10, 2048)
            d[16 * 2048 + 156:16 * 2048 + 190] = rec
            return bytes(d)
        gd = os.path.join(root, 'b2-gdi')
        os.makedirs(gd)
        open(os.path.join(gd, 'track03.iso'), 'wb').write(iso(b'GOOD'))
        open(os.path.join(outside, 'evil.iso'), 'wb').write(iso(b'EVIL'))
        open(os.path.join(gd, 'disc.gdi'), 'w').write('1\n3 0 4 2048 track03.iso 0\n')
        src = dcql_disc.ImageSource(os.path.join(gd, 'disc.gdi'))
        os.remove(os.path.join(gd, 'track03.iso'))   # the track swapped for a link out while the disc is served
        os.symlink(os.path.join(outside, 'evil.iso'), os.path.join(gd, 'track03.iso'))
        try:
            vol = bytes(src.sector(16)[40:44])
        except (OSError, ValueError, PermissionError) as e:
            return True, 'refused', {'error': str(e)[:100]}
        return vol == b'GOOD', 'read swapped track' if vol == b'EVIL' else 'pinned', {'volume': vol.decode('latin-1')}
    R.row('B2.gdi_swap', 'GDI: a track swapped for a link out after indexing is never read (opened object pinned)',
          gdi_swap)

    def subfolder_closed():
        base = os.path.join(root, 'b2-files')
        os.makedirs(os.path.join(base, 'narrow'))
        open(os.path.join(base, 'wide.txt'), 'w').write('wide')
        b = core.Bridge(os.path.join(root, 'b2-sub'), ('127.0.0.1', 0), '127.0.0.1:1', 'mock', files_root=base,
                        autoconnect=False)
        try:
            r = core.Run('x.bin', b'\0' * 16, '', 0, 'nosuch', None, 'test')
            b.runs[r.id] = r
            fs = b.file_service(r.sid)
            served = fs.path('wide.txt') is not None
            r2 = core.Run('y.bin', b'\0' * 16, '', 0, 'narrow', None, 'test')
            b.runs[r2.id] = r2
            narrow = b.file_service(r2.sid)
            nar_ok = narrow.path('wide.txt') is None and os.path.basename(narrow.root or '') == 'narrow'
        finally:
            b.close()
        return not served and nar_ok, 'widened to root' if served else 'closed', {'served_root': served,
                                                                                 'narrow_ok': nar_ok}
    R.row('B2.subfolder_closed', 'a run\'s unavailable /pc subfolder serves nothing (never the whole root)',
          subfolder_closed)

    def subfolder_api():
        base = os.path.join(root, 'b2-files2')
        os.makedirs(base)
        b, port = api_bridge(files_root=base)
        try:
            raw = b'\0' * 64
            import hashlib
            req = urllib.request.Request('http://127.0.0.1:%d/v0/runs' % port, data=raw, method='POST', headers={
                'Content-Type': 'application/octet-stream', 'X-Build-Sha256': hashlib.sha256(raw).hexdigest()})
            code = urllib.request.urlopen(req, timeout=10).status
        except urllib.error.HTTPError as e:
            code = e.code
        finally:
            b.close()
        # an octet-stream push names no subfolder: accepted; the multipart field is checked in push() (same code path)
        return code == 202, 'accepted', {'code': code}
    R.row('B2.subfolder_api', 'positive: a push without a subfolder is still accepted (202)', subfolder_api)


# ------------------------------------------------------------------------------------------------ B3
def b3(R, ctx):
    cl, core, dcql_disc, root, pcdir = ctx['cl'], ctx['core'], ctx['dcql_disc'], ctx['root'], ctx['pcdir']
    files = os.path.join(root, 'b3-files')
    os.makedirs(files)
    open(os.path.join(files, 'f.bin'), 'wb').write(b'x' * 100)

    def fopen(b, run, req=1):
        b.tap((cl.FOPEN, run, req, 0, b'f.bin'))

    def files_retired():
        b = core.Bridge(os.path.join(root, 'b3-ret'), ('127.0.0.1', 0), '127.0.0.1:1', 'mock', files_root=files,
                        autoconnect=False)
        try:
            n0 = nfds()
            run = 0x1234
            for req in range(1, 6):
                fopen(b, run, req)
            held = nfds() - n0
            b.client, b.client_run = {'role': 2}, run   # the program's link, then lost
            b.client_left('test: link lost')
            after = nfds() - n0
        finally:
            b.close()
        return held == 5 and after == 0, 'descriptors kept' if after else 'retired', {'held': held, 'after': after}
    R.row('B3.files_retired', 'a program\'s /pc descriptors are closed when its link is lost', files_retired)

    def files_capped():
        b = core.Bridge(os.path.join(root, 'b3-cap'), ('127.0.0.1', 0), '127.0.0.1:1', 'mock', files_root=files,
                        autoconnect=False)
        try:
            n0 = nfds()
            for run in range(1, 41):   # 40 runs, each holding one open file, none ending
                fopen(b, run)
            held = nfds() - n0
            closed_all = None
        finally:
            b.close()
            closed_all = nfds() - n0
        return held <= 4 and closed_all <= 0, 'descriptors kept' if held > 4 else 'capped', {
            'held_by_40_runs': held, 'after_close': closed_all}
    R.row('B3.files_capped', 'file services across runs are capped (40 runs hold at most 4 descriptors)', files_capped)

    ov = os.path.join(root, 'b3-overlay')
    os.makedirs(ov)
    open(os.path.join(ov, 'A.BIN'), 'wb').write(b'a' * 10)

    def dopen(target, run, req, loop):
        target.handle(loop, dcql_disc.DOPEN, run, req, 0, struct.pack('<I', 0) + b'A.BIN')

    def disc_capped():
        ds = dcql_disc.DiscService(None, ov)
        lp = FakeLoop()
        for req in range(1, 201):
            dopen(ds, 7, req, lp)
        n = len(ds.handles)
        return n <= 64, 'handles uncapped' if n > 64 else 'capped', {'handles': n}
    R.row('B3.disc_capped', 'disc handles are capped (200 opens held: at most 64)', disc_capped)

    def disc_retired():
        b = core.Bridge(os.path.join(root, 'b3-disc'), ('127.0.0.1', 0), '127.0.0.1:1', 'mock', overlay=ov,
                        autoconnect=False)
        try:
            for req in range(1, 11):
                b.tap((dcql_disc.DOPEN, 0x55, req, 0, struct.pack('<I', 0) + b'A.BIN'))
            held = len(b.disc.handles)
            b.client, b.client_run = {'role': 2}, 0x55
            b.client_left('test: link lost')
            after = len(b.disc.handles)
        finally:
            b.close()
        return held == 10 and after == 0, 'handles kept' if after else 'retired', {'held': held, 'after': after}
    R.row('B3.disc_retired', 'a program\'s disc handles are dropped when its link is lost', disc_retired)

    def elf_span():
        hdr = bytearray(52)
        hdr[:6] = b'\x7fELF\x01\x01'
        struct.pack_into('<I', hdr, 28, 52)
        struct.pack_into('<HH', hdr, 42, 32, 2)
        ph = struct.pack('<8I', 1, 116, 0x8C010000, 0, 4, 4, 5, 4) + \
            struct.pack('<8I', 1, 120, 0x8C010000 + (256 << 20), 0, 4, 4, 5, 4)
        elf = bytes(hdr) + ph + b'ABCDEFGH'
        asked = []
        real = bytearray

        def spy(*a):
            if a and isinstance(a[0], int):
                asked.append(a[0])
            return real(*a)
        cl.bytearray = spy   # module globals come before builtins: every bytearray(n) elf_image makes is seen
        try:
            try:
                lo, img = cl.elf_image(elf)
                got = len(img)
            except ValueError as e:
                got = 'refused: %s' % str(e)[:80]
        finally:
            del cl.bytearray
        big = [n for n in asked if n >= (64 << 20)]
        return not big and isinstance(got, str), 'allocated from span' if big else 'refused', {'got': got,
                                                                                           'allocated': big}
    R.row('B3.elf_span', 'ELF: a load span over the program limit is refused before the image is allocated', elf_span)

    def png_dims():
        sys.path.insert(0, pcdir)
        from assets import image
        w = h = 60000
        ihdr = struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0)

        def chunk(t, d):
            return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xFFFFFFFF)
        png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr) + chunk(b'IDAT', zlib.compress(b'\0' * 64)) + \
            chunk(b'IEND', b'')
        asked = []
        real = image.np

        class NP:
            def __getattr__(self, k):
                return getattr(real, k)

            @staticmethod
            def _ask(shape):
                n = 1
                for x in (shape if isinstance(shape, tuple) else (shape,)):
                    n *= x
                asked.append(n)
                if n >= (64 << 20):   # never really allocated: the spy stops it and records the request
                    raise MemoryError('spy: %d elements asked from the header' % n)

            def zeros(self, shape, *a, **k):
                self._ask(shape)
                return real.zeros(shape, *a, **k)

            def full(self, shape, *a, **k):
                self._ask(shape)
                return real.full(shape, *a, **k)
        image.np = NP()
        try:
            try:
                image.read_png(png)
                out = 'decoded'
            except MemoryError:
                out = 'allocated'
            except ValueError as e:
                out = 'refused: %s' % str(e)[:80]
        finally:
            image.np = real
        big = [n for n in asked if n >= (64 << 20)]
        return not big and out.startswith('refused'), 'allocated from header' if big else out.split(':')[0], {
            'out': out, 'asked': big}
    R.row('B3.png_dims', 'PNG: 60000 x 60000 in the header is refused before inflating or allocating', png_dims)

    class FakeT:
        def __init__(self, frames=()):
            self.q = list(frames)
            self.sent = []

        def send(self, d):
            m = cl.unframe(d)
            if m and m[0] == cl.REPQ and m[2] >= (1 << 20):
                raise OverflowError('REPQ for offset %d of an unchecked length' % m[2])
            self.sent.append(m)

        def recv(self, timeout):
            if self.q:
                return self.q.pop(0)
            time.sleep(min(timeout, 0.01))
            return None

    def report_len():
        lp = cl.Loop(FakeT(), lambda s: None)
        h = {'report_run': 9, 'report_len': 0xFFFFF000, 'report_crc': 0, 'report_state': 2, 'report_exit': 0}
        sent = []
        real_send = lp.send

        def send(t, s, a=0, b=0, payload=b''):
            sent.append((t, a, b))
            if t == cl.REPQ:
                raise OverflowError('REPQ sent for a report of %d bytes' % h['report_len'])
            return real_send(t, s, a, b, payload)
        lp.send = send
        try:
            lp.fetch_report(h, None)
            out = 'fetched'
        except OverflowError:
            out = 'requested'
        except RuntimeError as e:
            out = 'refused: %s' % str(e)[:80]
        return out.startswith('refused') and 'maximum' in out, 'requested unchecked length' if out == 'requested' \
            else out.split(':')[0], {'out': out}
    R.row('B3.report_len', 'HELLO\'s report length over the console\'s maximum is refused before any request', report_len)

    def report_offsets():
        rep = bytes(range(256)) * 8   # 2048 bytes, two blocks
        frames = [cl.frame(cl.REPORT, 9, 0, 0, rep[:1024]), cl.frame(cl.REPORT, 9, 512, 0, b'J' * 1024),
                  cl.frame(cl.REPORT, 9, 1024, 0, rep[1024:])]
        lp = cl.Loop(FakeT(frames), lambda s: None)
        lp.wait = lambda *a, **k: None   # the REPDONE wait
        h = {'report_run': 9, 'report_len': len(rep), 'report_crc': zlib.crc32(rep) & 0xFFFFFFFF, 'report_state': 2,
             'report_exit': 0}
        r = lp.fetch_report(h, None)
        return r['crc_ok'], 'stored unsolicited block' if not r['crc_ok'] else 'ignored', {'crc_ok': r['crc_ok']}
    R.row('B3.report_offsets', 'report blocks at offsets not requested (or a wrong length) are not stored',
          report_offsets)

    def queue_capped():
        b = core.Bridge(os.path.join(root, 'b3-queue'), ('127.0.0.1', 0), '127.0.0.1:1', 'mock', autoconnect=False)
        n_ok, refused_n, big_refused = 0, None, None
        try:
            for i in range(20):
                try:
                    b.submit('q%d.bin' % i, b'\0' * 1024, 'secs=1', 5)
                    n_ok += 1
                except ValueError:
                    refused_n = i
                    break
            b.queue.queue.clear()
            got_big = 0
            for i in range(6):
                try:
                    b.submit('big%d.bin' % i, b'\0' * (15 << 20), '', 5)
                    got_big += 1
                except ValueError:
                    big_refused = i
                    break
        finally:
            b.queue.queue.clear()
            b.close()
        ok = refused_n is not None and n_ok <= 9 and big_refused is not None and got_big * 15 <= 64
        return ok, 'queue uncapped' if refused_n is None else 'capped', {'accepted': n_ok, 'big_accepted': got_big}
    R.row('B3.queue_capped', 'the run queue is capped in count (20 pushes) and bytes (6 x 15 MiB)', queue_capped)

    def mcp_upload():
        from bridge import mcp_server
        bc = mcp_server.BridgeClient('http://127.0.0.1:1', 'test')
        sizes = []
        bc.call = lambda method, path, body=None, **k: sizes.append(len(json.dumps(body))) or (200, {})
        res, err, _ = mcp_server.handle(bc, 'upload_asset', {'base64': 'A' * (17 << 20), 'name': 'x.png'})
        sent_big = [n for n in sizes if n > (16 << 20)]
        refused = res is None and err and '413' in err
        return refused and not sent_big, 'serialized oversize' if sent_big else 'refused', {
            'error': (err or '')[:100], 'serialized': sent_big}
    R.row('B3.mcp_upload', 'MCP upload_asset: base64 over the body limit is refused before it is serialized',
          mcp_upload)


# ------------------------------------------------------------------------------------------------ B4 (simulated console)
def b4(R, ctx):
    cl, core, root = ctx['cl'], ctx['core'], ctx['root']
    from simconsole import SimConsole
    udp, lport = free_port(socket.SOCK_DGRAM), free_port(socket.SOCK_DGRAM)
    b = core.Bridge(os.path.join(root, 'b4'), ('127.0.0.1', udp), '127.0.0.1:%d' % lport, 'mock', autoconnect=False)
    sim = SimConsole(('127.0.0.1', udp), lport)
    ctx['b4_runs'] = runs = {}
    try:
        wait_for(lambda: b.loop.hello is not None, 8)

        def run(args, reset_after=None, timeout=90):
            r = b.submit('b4.bin', b'\x00\x09' * 4000, args, 30, None, reset_after)
            r.done.wait(timeout)
            wait_for(lambda: b.loop.hello is not None and b.active is None, 5)
            time.sleep(1.2)   # the loader beacons again before the next push
            return r

        def label_full():
            r = runs['plain'] = run('report secs=1')
            ok = r.state == 'done' and r.way_back == 'full restart'
            return ok, 'labelled stub' if r.way_back == 'stub' else 'way_back %s' % r.way_back, {
                'state': r.state, 'way_back': r.way_back}
        R.row('B4.label_full_restart', 'an ending with no stub record is named "full restart", never "stub"', label_full)

        def raw_dropped():
            r = runs.get('plain')
            if r is None:
                raise RuntimeError('no finished run')
            return r.raw is None, 'payload kept' if r.raw is not None else 'dropped', {
                'raw_bytes': len(r.raw) if r.raw is not None else None}
        R.row('B3.raw_dropped', 'a finished run keeps its numbers, not its program bytes', raw_dropped)

        def label_stub():
            r = run('report secs=30', reset_after=1.5)
            ok = r.state == 'reset' and r.way_back == 'stub' and getattr(r, 'reset_rung', None) == 'reset'
            return ok, 'way_back %s' % r.way_back, {'state': r.state, 'way_back': r.way_back,
                                                    'rung': getattr(r, 'reset_rung', None)}
        R.row('B4.label_stub', 'positive: a RESET answered by the stub\'s jump is named "stub" (rung kept apart)',
              label_stub)

        def bad_crc():
            r = run('report badcrc secs=1')
            ev = getattr(r, 'evidence', None) or {}
            ok = r.state == 'done' and r.report_crc_ok and ev.get('loader_report') == 'bad crc' and \
                ev.get('end') == 'whole crc ok'
            found = 'bad loader report published' if r.state == 'done' and not r.report_crc_ok else \
                'END kept' if ok else 'state %s' % r.state
            return ok, found, {'state': r.state, 'crc_ok': r.report_crc_ok, 'evidence': ev, 'error': r.error}
        R.row('B4.bad_crc_report', 'a valid END beats a loader report whose CRC fails (evidence records both)', bad_crc)

        def failed_report():
            r = run('report fetchfail secs=1')
            ev = getattr(r, 'evidence', None) or {}
            ok = r.state == 'done' and r.report_crc_ok and str(ev.get('loader_report', '')).startswith('failed')
            return ok, r.state if not ok else 'END kept', {'state': r.state, 'evidence': ev, 'error': r.error}
        R.row('B4.failed_report', 'a valid END survives a loader report that cannot be fetched', failed_report)

        def ladder_exhausted():
            r = run('report noreturn secs=1', reset_after=2.5, timeout=120)
            ev = getattr(r, 'evidence', None) or {}
            ok = r.state == 'done' and r.report_crc_ok and ev.get('loader_back') is False and r.way_back is None
            return ok, r.state if not ok else 'END kept', {'state': r.state, 'error': r.error, 'evidence': ev,
                                                           'way_back': r.way_back}
        R.row('B4.ladder_exhausted', 'a valid END survives reset-ladder exhaustion (loader never back)',
              ladder_exhausted)
    finally:
        sim.close()
        b.close()


# ------------------------------------------------------------------------------------------------ B5
def b5(R, ctx):
    cl, core, root = ctx['cl'], ctx['core'], ctx['root']
    W, H = 32, 32
    pix = bytes(range(256)) * (W * H * 2 // 256)
    head = struct.pack('<8I', 5, 1, W, H, 1, len(pix), zlib.crc32(pix) & 0xFFFFFFFF, 0)

    def bridge_send(st, lp, a, b, pl):
        st.on_still(lp, 1, a, b, pl, 'mock', lambda j: None)

    def cli_send(sk, lp, a, b, pl):
        sk.on_still(lp, a, b, pl)

    def complete(send, tgt, lp, idx=5):
        send(tgt, lp, cl.STILL_HEAD, idx, head)
        for off in range(0, len(pix), cl.CHUNK):
            send(tgt, lp, off, idx, pix[off:off + cl.CHUNK])
        send(tgt, lp, cl.STILL_END, idx, head)

    def acks(lp):
        return [x[3] for x in lp.sent if x[0] == cl.STILLACK]

    def bridge_receipt():
        st = core.Stills(os.path.join(root, 'b5-r'), lambda s: None)
        lp = FakeLoop()
        complete(bridge_send, st, lp)
        first = acks(lp)[-1]
        bridge_send(st, lp, cl.STILL_HEAD, 5, head)    # the ACK was lost: the program repeats head and END
        bridge_send(st, lp, cl.STILL_END, 5, head)
        again = acks(lp)[-1]
        reopened = bool(st.cur)
        return first == 0 and again == 0 and not reopened, 'reopened' if reopened or again else 'receipt', {
            'first_missing': first, 'repeat_missing': again, 'pending': len(st.cur)}
    R.row('B5.bridge_receipt', 'bridge: a repeated head and END after a lost ACK answer complete, not reopened',
          bridge_receipt)

    def cli_bounded():
        sk = cl.StillSink(None, 1, lambda s: None)
        lp = FakeLoop()
        for i in range(100):
            complete(cli_send, sk, lp, 1000 + i)
        kept = len(sk.cur)
        n = len(getattr(sk, 'receipts', ()))
        cli_send(sk, lp, cl.STILL_END, 1099, head)   # the newest still's END repeated: still complete
        return kept == 0 and n <= 64 and acks(lp)[-1] == 0, 'kept every completed still' if kept >= 100 else \
            'bounded', {'kept': kept, 'receipts': n}
    R.row('B5.cli_receipts_bounded', 'CLI: completed stills leave a bounded receipt (100 stills: at most 64 kept)',
          cli_bounded)

    def big_head(i):
        return struct.pack('<8I', i, 1, 640, 480, 1, 640 * 480 * 2, 0, 0)

    def expire(send, tgt, cur):
        lp = FakeLoop()
        for i in range(4):
            send(tgt, lp, cl.STILL_HEAD, 10 + i, big_head(10 + i))
        for s in cur().values():   # abandoned: nothing for a minute
            s['t0'] -= 60
            if 't_last' in s:
                s['t_last'] -= 60
        send(tgt, lp, cl.STILL_HEAD, 20, big_head(20))
        taken = any((k == 20 or (isinstance(k, tuple) and k[1] == 20)) for k in cur())
        return taken, 'expired' if taken else 'slots held', {'pending': len(cur())}

    R.row('B5.bridge_expire', 'bridge: abandoned stills expire, a new still takes the slot',
          lambda: (lambda st: expire(bridge_send, st, lambda: st.cur))(core.Stills(os.path.join(root, 'b5-e'),
                                                                                    lambda s: None)))
    R.row('B5.cli_expire', 'CLI: abandoned stills expire, a new still takes the slot',
          lambda: (lambda sk: expire(cli_send, sk, lambda: sk.cur))(cl.StillSink(None, 1, lambda s: None)))

    def exact(send, tgt):
        lp = FakeLoop()
        send(tgt, lp, cl.STILL_HEAD, 5, head)
        send(tgt, lp, 0, 5, pix[:10])                  # block 0 short: not present
        send(tgt, lp, 1024, 5, pix[1024:2048])
        send(tgt, lp, cl.STILL_END, 5, head)
        m = acks(lp)[-1]
        return m == 1, 'short block counted' if m == 0 else 'exact', {'missing': m}
    R.row('B5.bridge_exact_len', 'bridge: a block is present only at its exact length (a short one is missing)',
          lambda: exact(bridge_send, core.Stills(os.path.join(root, 'b5-x'), lambda s: None)))
    R.row('B5.cli_exact_len', 'CLI: a block is present only at its exact length (a short one is missing)',
          lambda: exact(cli_send, cl.StillSink(None, 1, lambda s: None)))


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
    root = tempfile.mkdtemp(prefix='ql-safety2-')
    R = ctlrows.Rows(PINNED, a.controls)
    ctx = {'cl': cl, 'core': core, 'dcql_disc': dcql_disc, 'pcdir': pcdir, 'root': root}
    try:
        for part in (b1, b2, b3, b4, b5):
            try:
                part(R, ctx)
            except Exception as e:   # a setup error: its rows go missing, and a missing pinned row is a FAIL
                print('%s: setup error %s: %s' % (part.__name__, type(e).__name__, e), flush=True)
        R.row('B6.harness', 'the control harness: an error, a pass, a wrong discrepancy or a missing row fails a control',
              lambda: (ctlrows.self_check(), 'harness', {}))
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return R.verdict(pcdir if a.controls else 'this tree', a.out)


if __name__ == '__main__':
    sys.exit(main())
