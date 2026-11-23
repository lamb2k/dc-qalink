#!/usr/bin/env python3
"""test_safety.py: the PC side's boundaries (lane ql-fix-pc, from the 0.2 audit of 2026-11-23). Linux (symbolic links,
/proc/net/udp); Python 3.10+, standard library.

  python tests/test_safety.py [--out DIR] [--pc DIR] [--controls]

Rows by audit item: A1 the UDP bind and the LAN sender filter, A2 root confinement (/pc, folder disc, GDI tracks,
asset paths and model dependencies, MCP files), A3 limits before reads and allocation (stills, open files, HTTP
Content-Length, MCP base64), A4 the bridge keeps a run whose END came before a full restart that cleared the report,
and the END assembler's cover, A5 the one HELLO decoder (way-back words, CRC and version checked) and first_console.

--pc DIR runs the rows against another pc/ tree. --controls is the control: with --pc naming 1990fb9's pc/ (the code
the audit read) every item must FAIL; it exits 0 only then and prints one "control A<n>" row per item."""
import argparse, json, os, shutil, socket, struct, subprocess, sys, tempfile, threading, time, urllib.error, \
    urllib.request, zlib

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = []


def record(item, name, ok, **kw):
    RESULTS.append({'item': item, 'name': name, 'ok': bool(ok), **kw})
    print('%-3s %-74s %s %s' % (item, name, 'PASS' if ok else 'FAIL', json.dumps(kw, default=str)[:200]), flush=True)


def guarded(item, name, f):
    try:
        f()
    except Exception as e:   # old code without a helper fails its row; it never stops the rest
        record(item, name, False, error='%s: %s' % (type(e).__name__, e))


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


def hello_dgram(build='sim loader (safety)', role=1, n=164, where=0x13, run=0xabcd1234, version=2, bad_crc=False):
    p = struct.pack('<16I', 1, 5000, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                    struct.unpack('<I', socket.inet_aton('192.0.2.235'))[0], 8 << 20, 0)
    p += build.encode().ljust(32, b'\0')
    p += struct.pack('<4I', 2, role, 2, 0) + b'\x11' * 8 + struct.pack('<II', 0, 0)
    p += bytes.fromhex('020000000001') + struct.pack('<H', 1)
    p += struct.pack('<BBHIIIIII', 17, 1, 0, 0, 0, where, 0x0102, run, 0x2a05)
    p = p[:n]
    h = b'CL' + bytes([version, 0x01]) + struct.pack('<IIHH', 0, 0, len(p), 0)
    crc = zlib.crc32(h + p) & 0xFFFFFFFF
    return h + p + struct.pack('<I', crc ^ (1 if bad_crc else 0))


# ------------------------------------------------------------------------------------------------ A1
def a1(cl, Bridge, pcdir, root):
    def udp_local_addr(port):
        """The actual bound address of a UDP port from the kernel's table (not from the bridge's own words)."""
        for ln in open('/proc/net/udp').read().splitlines()[1:]:
            loc = ln.split()[1]
            ip, p = loc.split(':')
            if int(p, 16) == port:
                return socket.inet_ntoa(struct.pack('<I', int(ip, 16)))
        return None

    def bridge_proc(extra):
        http = free_port()
        d = tempfile.mkdtemp(dir=root)
        p = subprocess.Popen([sys.executable, '-u', os.path.join(pcdir, 'qalink_bridge.py'), '--port', str(http),
                              '--udp-port', '0', '--data', d, '--quiet'] + extra,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            port = None
            end = time.time() + 15
            while time.time() < end and port is None:
                try:
                    with urllib.request.urlopen('http://127.0.0.1:%d/v0/discovery' % http, timeout=2) as f:
                        port = json.loads(f.read())['port']
                except (OSError, ValueError):
                    time.sleep(0.2)
            return port, (udp_local_addr(port) if port else None)
        finally:
            p.terminate()
            p.wait(10)

    def bind_default():
        port, addr = bridge_proc([])
        record('A1', 'default bridge: the UDP socket is bound to 127.0.0.1 (kernel table)', addr == '127.0.0.1',
               port=port, bound=addr)
    guarded('A1', 'default bridge: the UDP socket is bound to 127.0.0.1 (kernel table)', bind_default)

    def bind_flycast():
        port, addr = bridge_proc(['--console', '127.0.0.5:1'])
        record('A1', 'a --console of this PC (Flycast): bound on that address, still not every one',
               addr == '127.0.0.5', bound=addr)
    guarded('A1', 'a --console of this PC (Flycast): bound on that address, still not every one', bind_flycast)

    def bind_lan():
        port, addr = bridge_proc(['--allow-lan'])
        record('A1', 'positive: --allow-lan (a person\'s choice) binds every address', addr == '0.0.0.0', bound=addr)
    guarded('A1', 'positive: --allow-lan (a person\'s choice) binds every address', bind_lan)

    def lan_dropped():
        b = Bridge(os.path.join(root, 'a1-lan'), ('127.0.0.1', 0), 'auto', 'flycast', only_local=True)
        b.t.is_local = lambda ip: ip != '127.0.0.9'   # 127.0.0.9 stands for a console on the LAN
        seen = []
        real = cl.unframe

        def spy(d, *a, **k):
            seen.append(d)
            return real(d, *a, **k)
        cl.unframe = spy
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind(('127.0.0.9', 0))
            for _ in range(6):
                s.sendto(hello_dgram(), ('127.0.0.1', b.t.port))
                time.sleep(0.2)
            time.sleep(0.5)
            s.close()
        finally:
            cl.unframe = real
        n = len(b.t.discovery())
        b.close()
        record('A1', 'only-local: a LAN sender\'s HELLO is dropped before parsing (not decoded, not listed)',
               not seen and n == 0, decoded=len(seen), listed=n)
    guarded('A1', 'only-local: a LAN sender\'s HELLO is dropped before parsing (not decoded, not listed)', lan_dropped)

    def local_ok():
        b = Bridge(os.path.join(root, 'a1-local'), ('127.0.0.1', 0), 'auto', 'flycast', only_local=True)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(('127.0.0.2', 0))
        ok = False
        for _ in range(15):
            s.sendto(hello_dgram(), ('127.0.0.1', b.t.port))
            if wait_for(lambda: b.t.chosen_ip == '127.0.0.2', 0.2):
                ok = True
                break
        s.close()
        b.close()
        record('A1', 'positive: a loader on this PC (Flycast) is still heard and linked', ok)
    guarded('A1', 'positive: a loader on this PC (Flycast) is still heard and linked', local_ok)


# ------------------------------------------------------------------------------------------------ A2
def a2(cl, dcql_disc, pcdir, root):
    outside = os.path.join(root, 'outside')
    base = os.path.join(root, 'served')
    os.makedirs(outside)
    os.makedirs(os.path.join(base, 'sub'))
    open(os.path.join(outside, 'secret.txt'), 'w').write('secret')
    open(os.path.join(base, 'ok.txt'), 'w').write('ok')
    os.symlink(os.path.join(outside, 'secret.txt'), os.path.join(base, 'link.txt'))
    os.symlink(outside, os.path.join(base, 'sub', 'out'))

    def files():
        fs = cl.FileService(base, None)
        ok = fs.path('ok.txt') is not None
        bad = [r for r in ('link.txt', 'sub/out/secret.txt', '../outside/secret.txt', '/etc/passwd') if fs.path(r)]
        record('A2', '/pc: a link (file or folder) out of the root is refused; a plain file is served',
               ok and not bad, served_outside=bad)
    guarded('A2', '/pc: a link (file or folder) out of the root is refused; a plain file is served', files)

    def folder_disc():
        src = dcql_disc.FolderSource(base)
        names = sorted(v[0] for v in src.files.values())
        record('A2', 'folder disc / overlay: a linked file out of the root is not indexed or opened',
               names == ['ok.txt'], indexed=names)
    guarded('A2', 'folder disc / overlay: a linked file out of the root is not indexed or opened', folder_disc)

    def gdi():
        iso = bytearray(18 * 2048)
        iso[16 * 2048:16 * 2048 + 6] = b'\x01CD001'
        rec = bytearray(34)
        rec[0] = 34
        struct.pack_into('<I', rec, 2, 17)
        struct.pack_into('<I', rec, 10, 2048)
        iso[16 * 2048 + 156:16 * 2048 + 190] = rec
        open(os.path.join(outside, 'track01.iso'), 'wb').write(iso)
        gd = os.path.join(root, 'gdi')
        os.makedirs(gd)
        open(os.path.join(gd, 'track01.iso'), 'wb').write(iso)
        open(os.path.join(gd, 'good.gdi'), 'w').write('1\n1 0 4 2048 track01.iso 0\n')
        open(os.path.join(gd, 'bad.gdi'), 'w').write('1\n1 0 4 2048 ../outside/track01.iso 0\n')
        good = dcql_disc.ImageSource(os.path.join(gd, 'good.gdi'))
        try:
            dcql_disc.ImageSource(os.path.join(gd, 'bad.gdi'))
            refused = False
        except ValueError:
            refused = True
        record('A2', 'GDI: a track named out of the descriptor\'s folder is refused (one beside it reads)',
               refused and good.tracks, refused=refused)
    guarded('A2', 'GDI: a track named out of the descriptor\'s folder is refused (one beside it reads)', gdi)

    def model_deps():
        sys.path.insert(0, pcdir)
        from assets import model
        r = model.folder_resolver(base)
        got_ok = r('ok.txt')
        leaks = [n for n in ('link.txt', '../outside/secret.txt', os.path.join(outside, 'secret.txt')) if r(n)]
        record('A2', 'model dependencies: names out of the root (links, .., absolute) read as absent',
               got_ok == b'ok' and not leaks, leaked=leaks)
    guarded('A2', 'model dependencies: names out of the root (links, .., absolute) read as absent', model_deps)

    def asset_path():
        from bridge.assets import Assets
        from bridge.core import Bridge
        b = Bridge(os.path.join(root, 'a2-assets'), ('127.0.0.1', 0), '127.0.0.1:1', 'mock', files_root=base,
                   autoconnect=False)
        try:
            a = Assets(b)
            took = []
            a.add = lambda name, data, *x, **k: took.append(name) or {'id': 'x'}
            refused = []
            for p in (os.path.join(base, 'link.txt'), os.path.join(outside, 'secret.txt')):
                try:
                    a.add_path(p, {})
                except (PermissionError, ValueError, OSError):
                    refused.append(p)
            a.add_path(os.path.join(base, 'ok.txt'), {})
        finally:
            b.close()
        record('A2', 'asset import: a path out of the asset root (or a link out) is refused before reading',
               len(refused) == 2 and took == ['ok.txt'], took=took)
    guarded('A2', 'asset import: a path out of the asset root (or a link out) is refused before reading', asset_path)

    def mcp_path():
        from bridge import mcp_server
        bc = mcp_server.BridgeClient('http://127.0.0.1:1', 'test')
        bc.root = base
        calls = []
        bc.call = lambda *x, **k: calls.append(x) or (202, {'id': '1'})
        c1, _ = bc.push({'path': os.path.join(outside, 'secret.txt')})
        c2, _ = bc.push({'path': os.path.join(base, 'link.txt')})
        c3, _ = bc.push({'path': os.path.join(base, 'ok.txt')})
        record('A2', 'MCP push_build: a file out of its root is refused before reading (one inside is sent)',
               c1 == 403 and c2 == 403 and c3 == 202 and len(calls) == 1, codes=[c1, c2, c3])
    guarded('A2', 'MCP push_build: a file out of its root is refused before reading (one inside is sent)', mcp_path)


# ------------------------------------------------------------------------------------------------ A3
class FakeLoop:
    def __init__(self):
        self.sent = []

    def send(self, *a):
        self.sent.append(a)


def a3(cl, root):
    def stills():
        sk = cl.StillSink(None, 1, lambda s: None)
        lp = FakeLoop()
        sk.on_still(lp, cl.STILL_HEAD, 1, struct.pack('<8I', 1, 1, 1, 1, 1, 64 << 20, 0, 0))   # 1 x 1 claiming 64 MiB
        sk.on_still(lp, cl.STILL_HEAD, 2, struct.pack('<8I', 2, 1, 640, 480, 7, 640 * 480 * 2, 0, 0))   # format 7
        sk.on_still(lp, cl.STILL_HEAD, 3, b'\x01' * 20)   # short head
        bad = len(sk.cur)
        for i in range(10, 20):   # ten valid 640 x 480 heads: only the pending budget's worth are taken
            sk.on_still(lp, cl.STILL_HEAD, i, struct.pack('<8I', i, 1, 640, 480, 1, 640 * 480 * 2, 0, 0))
        held = sum(len(s['data']) for s in sk.cur.values() if s.get('data') is not None)
        record('A3', 'stills (CLI): inconsistent heads allocate nothing; pending stills capped (count, bytes)',
               bad == 0 and len(sk.cur) <= 4 and held <= (4 << 20), inconsistent_held=bad, pending=len(sk.cur),
               bytes=held)
    guarded('A3', 'stills (CLI): inconsistent heads allocate nothing; pending stills capped (count, bytes)', stills)

    def stills_bridge():
        from bridge.core import Stills
        st = Stills(os.path.join(root, 'a3-stills'), lambda s: None)
        lp = FakeLoop()
        st.on_still(lp, 1, cl.STILL_HEAD, 1, struct.pack('<8I', 1, 1, 1, 1, 1, 64 << 20, 0, 0), 'mock', None)
        bad = len(st.cur)
        for i in range(10, 20):
            st.on_still(lp, 1, cl.STILL_HEAD, i, struct.pack('<8I', i, 1, 640, 480, 1, 640 * 480 * 2, 0, 0), 'mock',
                        None)
        record('A3', 'stills (bridge): an inconsistent head allocates nothing; pending stills capped',
               bad == 0 and len(st.cur) <= 4, inconsistent_held=bad, pending=len(st.cur))
    guarded('A3', 'stills (bridge): an inconsistent head allocates nothing; pending stills capped', stills_bridge)

    def fds():
        d = os.path.join(root, 'a3-files')
        os.makedirs(d)
        open(os.path.join(d, 'f.bin'), 'wb').write(b'x' * 100)
        fs = cl.FileService(d, None)
        lp = FakeLoop()
        for req in range(1, 101):
            fs.on_open(lp, 1, req, b'f.bin')
        n = len(fs.handles)
        for h in list(fs.handles):
            fs.on_close(h)
        record('A3', '/pc: open files a program holds are capped (100 distinct opens)', n <= 32, held=n)
    guarded('A3', '/pc: open files a program holds are capped (100 distinct opens)', fds)

    def http_len():
        from bridge.core import Bridge
        from bridge.api import serve
        b = Bridge(os.path.join(root, 'a3-http'), ('127.0.0.1', 0), '127.0.0.1:1', 'mock', autoconnect=False)
        port = free_port()
        serve(b, '127.0.0.1', port, 'run')
        codes = []
        try:
            for n in ('-1', '+5', '99999999999'):
                s = socket.create_connection(('127.0.0.1', port), timeout=5)
                s.sendall(('POST /v0/settings HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n'
                           'Content-Length: %s\r\n\r\n{"autoconnect": true}' % n).encode())
                s.shutdown(socket.SHUT_WR)
                try:
                    codes.append(int(s.recv(64).split()[1]))
                except (OSError, IndexError, ValueError):
                    codes.append(None)
                s.close()
        finally:
            b.close()
        record('A3', 'HTTP: Content-Length negative, signed or over the limit is refused (413) before reading',
               codes == [413, 413, 413], codes=codes)
    guarded('A3', 'HTTP: Content-Length negative, signed or over the limit is refused (413) before reading', http_len)

    def mcp_b64():
        from bridge import mcp_server
        bc = mcp_server.BridgeClient('http://127.0.0.1:1', 'test')
        calls = []
        bc.call = lambda *x, **k: calls.append(x) or (202, {'id': '1'})
        c, _ = bc.push({'base64': 'A' * (24 << 20)})
        c2, _ = bc.push({'base64': '!!notbase64!!'})
        record('A3', 'MCP push_build: base64 over the limit (or not base64) refused before decoding',
               c == 413 and c2 == 413 and not calls, codes=[c, c2], sent=len(calls))
    guarded('A3', 'MCP push_build: base64 over the limit (or not base64) refused before decoding', mcp_b64)


# ------------------------------------------------------------------------------------------------ A4
def a4(cl, root):
    def bridge_end():
        sys.path.insert(1, HERE)
        from simconsole import SimConsole
        from bridge.core import Bridge
        udp, lport = free_port(socket.SOCK_DGRAM), free_port(socket.SOCK_DGRAM)
        b = Bridge(os.path.join(root, 'a4'), ('127.0.0.1', udp), '127.0.0.1:%d' % lport, 'mock', autoconnect=False)
        sim = SimConsole(('127.0.0.1', udp), lport)
        try:
            wait_for(lambda: b.loop.hello is not None, 8)
            r = b.submit('end.bin', b'\x00\x09' * 4000, 'report endonly secs=1', 30)
            r.done.wait(60)
            rep = r.report or b''
            record('A4', 'bridge: a valid END, then a loader back with an empty report = the run\'s result, not lost',
                   r.state == 'done' and rep.startswith(b'program sha256=') and r.report_crc_ok,
                   state=r.state, error=r.error, report_bytes=len(rep))
        finally:
            sim.close()
            b.close()
    guarded('A4', 'bridge: a valid END, then a loader back with an empty report = the run\'s result, not lost',
            bridge_end)

    def assembler():
        import qlsafe
        rep = b'0123456789' * 30
        head = struct.pack('<IiIIIIIIIIIIIIII', 2, 0, 0, 0, 0, 0, len(rep), zlib.crc32(rep) & 0xFFFFFFFF, 0, len(rep),
                           0, 0, 0, 0, 0, 1)
        lp = FakeLoop()
        ok = qlsafe.EndAssembler(7)
        cl.end_on(ok, lp, 7, 0, rep[:150], lambda s: None, time.time())
        cl.end_on(ok, lp, 7, 150, rep[150:], lambda s: None, time.time())
        cl.end_on(ok, lp, 7, 0xFFFFFFFF, head, lambda s: None, time.time())
        whole = ok.result()
        ov = qlsafe.EndAssembler(7)   # 300 bytes in pieces that overlap (0..160, 150..300): counts reach 310
        for o, piece in ((0, rep[:160]), (150, rep[150:])):
            ov.on(7, o, piece)
        ov.on(7, 0xFFFFFFFF, head)
        gap = qlsafe.EndAssembler(7)   # a hole (0..100, 200..300)
        for o, piece in ((0, rep[:100]), (200, rep[200:]), (0xFFFFFFFF, head)):
            gap.on(7, o, piece)
        other = qlsafe.EndAssembler(7)
        other.on(8, 0, rep)            # another run's piece
        big = qlsafe.EndAssembler(7)
        big.on(7, 0xFFFFFFFF, struct.pack('<IiIIIIIIIIIIIIII', 2, 0, 0, 0, 0, 0, 1 << 20, 0, 0, 1 << 20, 0, 0, 0, 0, 0,
                                          1))   # a 1 MiB tail is over the bound
        acks = [x for x in lp.sent if x[0] == cl.ENDACK]
        record('A4', 'END assembler: whole tail ENDACK 0; overlap and hole not complete; other run, oversize refused',
               whole['crc_ok'] and whole['complete'] and acks and acks[-1][3] == 0
               and not ov.result()['complete'] and ov.missing() > 0 and not gap.result()['complete']
               and gap.missing() == 100 and not other.pieces and big.head is None,
               overlap_missing=ov.missing(), gap_missing=gap.missing(), ack=acks[-1] if acks else None)
    guarded('A4', 'END assembler: whole tail ENDACK 0; overlap and hole not complete; other run, oversize refused',
            assembler)


# ------------------------------------------------------------------------------------------------ A5
def a5(cl, pcdir):
    def decoder():
        d = hello_dgram()
        h, run = cl.decode_hello(d)
        wb = h['way_back']
        bad = [cl.decode_hello(hello_dgram(bad_crc=True)), cl.decode_hello(hello_dgram(version=3)),
               cl.decode_hello(hello_dgram(n=90)), cl.decode_hello(d[:-1])]
        old = cl.decode_hello(hello_dgram(n=148))[0]
        record('A5', 'one HELLO decoder: 164-byte way-back words read; bad CRC, version, short, truncated refused',
               wb['where'] == 0x13 and wb['run'] == 0xabcd1234 and wb['sense'] == 0x2a05 and wb['aux'] == 0x0102
               and h['mac'] == '02:00:00:00:00:01' and all(x is None for x in bad) and old['way_back'] is None
               and old['mac'] == h['mac'], way_back=wb, refused=[x is None for x in bad])
    guarded('A5', 'one HELLO decoder: 164-byte way-back words read; bad CRC, version, short, truncated refused',
            decoder)

    def first_console():
        sys.path.insert(0, os.path.join(pcdir, '..', 'tools'))
        import first_console as fc
        good = fc.way_back_record(hello_dgram())
        bad = fc.way_back_record(hello_dgram(bad_crc=True))
        prog = fc.way_back_record(hello_dgram(role=2))
        record('A5', 'first_console uses it: a good beacon decodes; a bad CRC or a program\'s HELLO does not',
               good and good['run'] == 0xabcd1234 and 'the full restart taken' in good['text'] and bad is None
               and prog is None, good=(good or {}).get('text'))
    guarded('A5', 'first_console uses it: a good beacon decodes; a bad CRC or a program\'s HELLO does not',
            first_console)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out')
    ap.add_argument('--pc', default=os.path.join(HERE, '..', 'pc'), help='the pc/ tree to test')
    ap.add_argument('--controls', action='store_true', help='the control: every item must FAIL on this --pc')
    a = ap.parse_args()
    pcdir = os.path.abspath(a.pc)
    sys.path.insert(0, pcdir)
    import dcql_loop as cl
    import dcql_disc
    from bridge.core import Bridge
    root = tempfile.mkdtemp(prefix='ql-safety-')
    t0 = time.time()
    try:
        a1(cl, Bridge, pcdir, root)
        a2(cl, dcql_disc, pcdir, root)
        a3(cl, root)
        a4(cl, root)
        a5(cl, pcdir)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    items = sorted({r['item'] for r in RESULTS})
    failed_items = sorted({r['item'] for r in RESULTS if not r['ok']})
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        json.dump({'pc': pcdir, 'controls': a.controls, 'results': RESULTS},
                  open(os.path.join(a.out, 'safety-results.json'), 'w'), indent=1)
    if a.controls:
        for it in items:
            print('control %s (on %s: must fail) %s' % (it, pcdir, 'PASS' if it in failed_items else 'FAIL'))
        print('controls: %d of %d items failed as they must' % (len(failed_items), len(items)))
        return 0 if failed_items == items and len(items) == 5 else 1
    bad = [r['name'] for r in RESULTS if not r['ok']]
    print('%d of %d passed in %.1f s%s' % (len(RESULTS) - len(bad), len(RESULTS), time.time() - t0,
                                          '' if not bad else '; FAILED: ' + '; '.join(bad)))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
