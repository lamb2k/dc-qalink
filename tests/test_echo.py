#!/usr/bin/env python3
"""test_echo.py: the PC's own broadcast looped back must never be taken for the console (first console run,
2026-11-22: `dcql_loop.py --udp auto` learnt this PC as the console from its own looped-back PING and sent every LOAD to
itself). The PC tool's socket sends a PING to its own address (exactly what Windows hands back for a broadcast), then
a simulated loader beacons from another port. The peer must be the loader.

  python tests/test_echo.py [--old <dcql_loop.py of be1edc1e4>]   with --old, that version must FAIL (the control)
"""
import argparse, importlib.util, json, os, socket, struct, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'pc'))


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def hello(cl, role=1):
    p = struct.pack('<16I', 1, 1000, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, struct.unpack('<I', socket.inet_aton('192.168.1.235'))[0],
                    0, 0) + b'echo-test loader'.ljust(32, b'\0') + struct.pack('<4I', 2, role, 2, 2120) + bytes(8) + \
        struct.pack('<II', 0, 2)
    return cl.frame(cl.HELLO, 0, payload=p)


def run_loop(cl):
    t = cl.UdpTransport('auto', 0)
    port = t.s.getsockname()[1]
    t.s.sendto(cl.frame(cl.PING, 0), ('127.0.0.1', port))       # our own broadcast, looped back
    con = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    con.bind(('127.0.0.1', 0))
    time.sleep(0.05)
    con.sendto(hello(cl), ('127.0.0.1', port))
    end = time.time() + 2
    while time.time() < end and t.peer is None:
        t.recv(0.1)
    for _ in range(3):
        t.recv(0.05)
    peer, want = t.peer, con.getsockname()
    t.close()
    con.close()
    return peer, want


def run_bridge():
    import dcql_loop as cl
    from bridge.core import BridgeUdp
    t = BridgeUdp('127.0.0.1', 0, 'auto')
    t.s.sendto(cl.frame(cl.PING, 0), ('127.0.0.1', t.port))
    con = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    con.bind(('127.0.0.1', 0))
    time.sleep(0.05)
    con.sendto(hello(cl), ('127.0.0.1', t.port))
    end = time.time() + 2
    while time.time() < end and t.peer is None:
        t.recv(0.1)
    for _ in range(3):
        t.recv(0.05)
    out = (t.peer, con.getsockname(), t.echoes, t.discovery())
    t.close()
    con.close()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--old')
    a = ap.parse_args()
    res = []
    import dcql_loop as cl
    peer, want = run_loop(cl)
    res.append(('dcql_loop --udp auto: the peer is the loader, not our echo', peer == want, peer, want))
    peer, want, echoes, disc = run_bridge()
    res.append(('bridge: the peer is the loader; the echo counted and dropped', peer == want and echoes == 1 and
                len(disc) == 1 and disc[0]['console_ip'] == '192.168.1.235', peer, want))
    if a.old:
        old = load(a.old, 'dcql_loop_old')
        peer, want = run_loop(old)
        res.append(('control: be1edc1e4\'s dcql_loop takes its own echo (must fail)', peer != want, peer, want))
    for name, ok, *kw in res:
        print('%-66s %s %s' % (name, 'PASS' if ok else 'FAIL', json.dumps(kw)))
    return 0 if all(r[1] for r in res) else 1


if __name__ == '__main__':
    sys.exit(main())
