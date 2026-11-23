#!/usr/bin/env python3
"""test_autoconnect.py: the bridge links by itself (owner, 2026-11-22: "we should also make it autoconnect").

Simulated loaders beacon from loopback addresses (127.0.0.x stand in for the LAN; the bridge never gets a /v0/link):
 1. a first loader beacons: linked within 2 s of its first beacon, remembered by its identity, not its address;
 2. it goes silent (a run that reboots, a power cycle): the link is lost; it comes back on a NEW address with a new
    DHCP lease: relinked within 2 s, with no call;
 3. a new bridge on the same data folder hears two loaders: it picks the remembered one;
 4. keyed by the adapter MAC once HELLO carries it: one console across a loader update and a new address;
 5. a console remembered by its loader before the MAC is found and re-keyed by MAC;
 6. MAC, GDEMU slot and version and the stub's record shown in the device, discovery and describe();
 controls (must not link): autoconnect off (then turned on with POST /v0/settings semantics: links); two unknown
 loaders and nothing remembered (waits for a click).
Linux (WSL) or Windows; 127.0.0.2 and up must be bindable (Linux: always; Windows: usually).

  python tests/test_autoconnect.py [--out DIR]
"""
import argparse, json, os, shutil, socket, struct, sys, tempfile, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'pc'))
import dcql_loop as cl                 # noqa: E402
from bridge.core import Bridge, BridgeUdp   # noqa: E402

RESULTS = []


def record(name, ok, **kw):
    RESULTS.append(dict(name=name, ok=bool(ok), **kw))
    print('%-70s %s %s' % (name, 'PASS' if ok else 'FAIL', json.dumps(kw, default=str)[:220]), flush=True)


def hello(console_ip, sha8, boot=1, mac=None, slot=0, stub=None, gdemu_ver=0):
    """A loader HELLO: 128 bytes (before 456ac57), 136 with `mac` (MAC and GDEMU slot), 148 with `stub` too
    (step, err, gdemu, entries) and GDEMU's version."""
    p = struct.pack('<16I', boot, 1000, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                    struct.unpack('<I', socket.inet_aton(console_ip))[0], 0, 0) + \
        b'sim loader (autoconnect test)'.ljust(32, b'\0') + struct.pack('<4I', 2, 1, 2, 2120) + bytes.fromhex(sha8) + \
        struct.pack('<II', 0, 0)
    if mac is not None:
        p += bytes.fromhex(mac.replace(':', '')) + struct.pack('<H', slot)
    if stub is not None:
        step, err, gd, entries = stub
        p += struct.pack('<BBHII', step, entries, gd, err, gdemu_ver)
    return cl.frame(cl.HELLO, 0, payload=p)


class Beacon:
    """A loader beaconing once every 0.5 s from `src` (as the loader does) until stopped."""
    def __init__(self, src, port, console_ip, sha8, boot=1, **kw):
        self.s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.s.bind((src, 0))
        self.src, self.port, self.d = src, port, hello(console_ip, sha8, boot, **kw)
        self.stop_ev = threading.Event()
        self.first = None
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while not self.stop_ev.is_set():
            if self.first is None:
                self.first = time.time()
            self.s.sendto(self.d, ('127.0.0.1', self.port))
            self.stop_ev.wait(0.5)

    def stop(self):
        self.stop_ev.set()
        time.sleep(0.05)
        self.s.close()


def wait_for(f, secs):
    end = time.time() + secs
    while time.time() < end:
        if f():
            return True
        time.sleep(0.05)
    return f()


def new_bridge(data, auto=True):
    return Bridge(data, ('127.0.0.1', 0), 'auto', 'flycast', autoconnect=auto)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out')
    a = ap.parse_args()
    root = tempfile.mkdtemp(prefix='ql-autoconnect-')
    A, B = '11' * 8, '22' * 8
    try:
        d1 = os.path.join(root, 'one')
        b = new_bridge(d1)
        port = b.t.port
        s1 = Beacon('127.0.0.2', port, '192.0.2.235', A)
        ok = wait_for(lambda: b.t.chosen_ip == '127.0.0.2' and b.link_state == 'up', 3)
        record('1 first loader: linked by itself, no /v0/link', ok and time.time() - s1.first < 2.5,
               seconds=round(time.time() - s1.first, 2), link=b.link_state, chosen=b.t.chosen_ip)
        rem = json.load(open(os.path.join(d1, 'consoles.json')))
        record('1 remembered by the loader\'s identity, not its address', list(rem) == ['loader:%s:sim loader (autoconnect test)' % A],
               remembered=rem)
        s1.stop()
        lost = wait_for(lambda: b.link_state == 'lost', 12)
        record('2 the loader goes silent: link lost', lost, link=b.link_state)
        s2 = Beacon('127.0.0.3', port, '192.0.2.240', A, boot=2)
        ok = wait_for(lambda: b.t.chosen_ip == '127.0.0.3' and b.link_state == 'up', 3)
        record('2 back on a new address: relinked within 2 s, no call', ok and time.time() - s2.first < 2.5,
               seconds=round(time.time() - s2.first, 2), chosen=b.t.chosen_ip, console_ip=(b.loop.hello or {}).get('console_ip'))
        s2.stop()
        b.close()

        b = new_bridge(d1)
        other = Beacon('127.0.0.5', b.t.port, '192.0.2.50', B)
        time.sleep(0.3)
        known = Beacon('127.0.0.4', b.t.port, '192.0.2.241', A, boot=3)
        ok = wait_for(lambda: b.t.chosen_ip == '127.0.0.4', 3)
        time.sleep(1.0)
        record('3 two loaders, one remembered: the remembered one is linked', ok and b.t.chosen_ip == '127.0.0.4',
               chosen=b.t.chosen_ip, discovery=[(x['address'], x['remembered']) for x in b.t.discovery()])
        other.stop(); known.stop(); b.close()

        b = new_bridge(os.path.join(root, 'off'), auto=False)
        s = Beacon('127.0.0.6', b.t.port, '192.0.2.60', A)
        time.sleep(3.0)
        record('control: autoconnect off: heard, not linked', b.t.chosen_ip is None and len(b.t.discovery()) == 1,
               chosen=b.t.chosen_ip)
        b.set_autoconnect(True)
        ok = wait_for(lambda: b.t.chosen_ip == '127.0.0.6', 2)
        record('control: turned on (POST /v0/settings): linked at the next beacon', ok, chosen=b.t.chosen_ip)
        s.stop(); b.close()

        b = new_bridge(os.path.join(root, 'two'))
        x1 = Beacon('127.0.0.7', b.t.port, '192.0.2.70', A)
        x2 = Beacon('127.0.0.8', b.t.port, '192.0.2.80', B)
        time.sleep(0.2)
        first = b.t.chosen_ip
        time.sleep(2.5)
        # the first beacon of the first loader may link before the second is heard (that is the "only one heard" rule);
        # what must not happen is a link once both are heard and neither is remembered, so start from a cleared choice
        with b.t.lock:
            b.t.chosen_ip = None; b.t.peer = None; b.t.chosen_ident = None; b.t.remembered = {}
        time.sleep(2.0)
        record('control: two unknown loaders, nothing remembered: waits for a click', b.t.chosen_ip is None,
               chosen=b.t.chosen_ip, first=first)
        x1.stop(); x2.stop(); b.close()

        # keyed by the adapter MAC from HELLO (loaders from 456ac57 on; ql-core2): one console across a loader update
        # and a new address. M is the console's MAC; C a new loader build (self-update) on the same console.
        M, M2, C = '00:d0:f1:02:a3:b4', '00:d0:f1:99:88:77', '33' * 8
        dm = os.path.join(root, 'mac')
        b = new_bridge(dm)
        s = Beacon('127.0.0.10', b.t.port, '192.0.2.235', A, mac=M, slot=158)
        ok = wait_for(lambda: b.t.chosen_ip == '127.0.0.10', 3)
        rem = json.load(open(os.path.join(dm, 'consoles.json')))
        record('4 a loader with a MAC: linked and remembered by its MAC', ok and list(rem) == ['mac:' + M]
               and rem['mac:' + M]['gdemu_slot'] == 158, remembered=rem)
        s.stop(); b.close()
        b = new_bridge(dm)
        s = Beacon('127.0.0.11', b.t.port, '192.0.2.236', C, boot=2, mac=M, slot=158)
        ok = wait_for(lambda: b.t.chosen_ip == '127.0.0.11', 3)
        record('4 same MAC, a new loader build and a new address: linked by itself', ok, chosen=b.t.chosen_ip)
        s.stop(); b.close()
        b = new_bridge(dm)
        s = Beacon('127.0.0.12', b.t.port, '192.0.2.237', A, mac=M2, slot=158)
        time.sleep(2.5)
        record('control: another console with the remembered loader build but another MAC: not linked (waits)',
               b.t.chosen_ip is None and len(b.t.discovery()) == 1, chosen=b.t.chosen_ip)
        s.stop(); b.close()

        # a console remembered by its loader before the loader sent the MAC: found by the loader key, then kept by MAC
        dmig = os.path.join(root, 'migrate')
        os.makedirs(dmig)
        json.dump({'loader:%s:sim loader (autoconnect test)' % A: {'last_ip': '192.0.2.235'}},
                  open(os.path.join(dmig, 'consoles.json'), 'w'))
        b = new_bridge(dmig)
        s = Beacon('127.0.0.13', b.t.port, '192.0.2.235', A, mac=M, slot=158,
                   stub=(6, 0x48000, 0x104, 1), gdemu_ver=(5 << 16) | (14 << 8))
        ok = wait_for(lambda: b.t.chosen_ip == '127.0.0.13' and b.loop.hello is not None, 3)
        rem = json.load(open(os.path.join(dmig, 'consoles.json')))
        record('5 remembered by its loader before the MAC: linked, now kept by MAC only', ok and list(rem) == ['mac:' + M],
               remembered=list(rem))
        dev = b.device()
        disc = b.t.discovery()[0]
        text = cl.describe(b.loop.hello)
        record('6 MAC, slot, GDEMU version and the stub\'s record in the device, discovery and describe()',
               dev['mac'] == M and dev['gdemu_slot'] == 158 and dev['gdemu_version'] == '5.14.0'
               and dev['stub']['step'] == 6 and 'GDEMU select refused, code 4' in dev['stub_text']
               and disc['mac'] == M and disc['stub']['err'] == 0x48000
               and 'mac %s slot 158 gdemu 5.14.0' % M in text and 'stub step 6 jump (white)' in text,
               device={k: dev[k] for k in ('mac', 'gdemu_slot', 'gdemu_version', 'stub_text')}, describe=text[-120:])
        s.stop(); b.close()
        h128 = cl.parse_hello(cl.unframe(hello('192.0.2.2', A), any_version=True)[4])
        record('control: a 128-byte HELLO (loader before 456ac57): no MAC, keyed by its loader',
               h128['mac'] is None and h128['stub'] is None and h128['gdemu_slot'] is None
               and BridgeUdp.ident(h128).startswith('loader:'),
               mac=h128['mac'])

        # the lane-run guard (integrator ruling 2026-11-22: no lane run sends anything to a real console). 127.0.0.9
        # stands for a console on the LAN: the bridge's is_local is told it is not this PC.
        for only_local in (True, False):
            b = Bridge(os.path.join(root, 'lan-%s' % only_local), ('127.0.0.1', 0), 'auto', 'flycast',
                       only_local=only_local)
            b.t.is_local = lambda ip: ip != '127.0.0.9'
            lan = Beacon('127.0.0.9', b.t.port, '192.0.2.235', A)
            time.sleep(2.0)
            if only_local:
                try:
                    b.t.choose('127.0.0.9')
                    refused = False
                except PermissionError:
                    refused = True
                record('only-local (the default): a LAN console is listed, never linked; /v0/link refused',
                       b.t.chosen_ip is None and refused and len(b.t.discovery()) == 1 and b.t.remote_refused > 0,
                       chosen=b.t.chosen_ip, remote_refused=b.t.remote_refused)
            else:
                record('control: --allow-lan (a person\'s choice): the same console is linked', b.t.chosen_ip == '127.0.0.9',
                       chosen=b.t.chosen_ip)
            lan.stop(); b.close()
        try:
            # a documentation address (TEST-NET-1) and an unused port: were the guard to fail, nothing reaches a console
            Bridge(os.path.join(root, 'fixed'), ('127.0.0.1', 0), '192.0.2.235:52999', 'flycast').close()
            refused = False
        except PermissionError:
            refused = True
        record('only-local: --console <a LAN address> refuses to start', refused)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        json.dump({'results': RESULTS}, open(os.path.join(a.out, 'autoconnect-results.json'), 'w'), indent=1)
    print('%d of %d passed' % (sum(r['ok'] for r in RESULTS), len(RESULTS)))
    return 0 if all(r['ok'] for r in RESULTS) else 1


if __name__ == '__main__':
    sys.exit(main())
