#!/usr/bin/env python3
"""test_still.py: a still over a link that loses chosen datagrams (dc-qalink 0.2, lane ql-core6).

The console's own sender (loader/cl_still.h, the code client.c runs) is built for this PC as a small shared library and
driven against both PC receivers (pc/dcql_loop.py StillSink, the one first_console.py and `dcql_loop.py run` use, and
pc/bridge/core.py Stills, the bridge's). Time is simulated: each datagram costs 0.8 ms, an END's wait its 300 ms when
no answer comes. Every case is one 640x480 RGB565 picture (600 blocks of 1 KiB), as the console's screen grab.

The console's case (first-run9 row 4, 2026-11-22): the first grab's head was lost; the PC dropped every block and END
of that still and never answered; the client sent the whole picture 12 times: "NOT HELD in 43483 ms".

  python3 tests/test_still.py [--out <dir>]

Cases: clean; the first head lost (must be held within 2 s, on both receivers); one block lost; the first END lost;
the first head and the first repeat lost. Controls that must fail: every head lost (not held); the ql-core5 sender
(the head once, the same rounds) with the first head lost (not held, about 42 s, the console's 43.5 s).

Round 3 (lane ql-fix-dc, 2026-11-23): a picture that changes while it is sent (a moving game, a held game that still
renders, the in-game menu over it: the frame buffer on screen is read in pieces while the program draws). The sender
now takes the CRC of each block as sent and the END carries the picture's CRC (cl_still.h). Cases: moving, on both
receivers; moving with one block lost (its copy sent again differs). Control that must fail: the same moving picture
with the CRC taken before sending (crcs NULL: the code path of dccfe98, where client.c computed it first).
"""
import argparse, ctypes, json, os, random, struct, subprocess, sys, tempfile, zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
sys.path.insert(0, os.path.join(ROOT, 'pc'))
import dcql_loop as cl   # noqa: E402
from bridge import core as bcore   # noqa: E402

SEND_S, WAIT_S = 0.0008, 0.300
W, H = 640, 480

WRAP = r'''
#include "cl_still.h"
typedef void (*send_fn)(uint32_t a, uint32_t index, const uint8_t *p, uint32_t n);
typedef int (*wait_fn)(uint32_t ms);
static send_fn g_s;
static wait_fn g_w;
static void s_(void *c, uint32_t a, uint16_t i, const void *p, uint16_t n) { (void)c; g_s(a, i, (const uint8_t *)p, n); }
static int w_(void *c, unsigned ms) { (void)c; return g_w(ms); }
int run_still(send_fn s, wait_fn w, uint8_t *bits, uint32_t bits_size, uint32_t *crcs, const uint8_t *src,
              uint32_t row, uint32_t stride, uint32_t bytes, uint32_t index, const uint8_t *head, uint32_t head_len,
              uint32_t *n) {
    g_s = s; g_w = w;
    const cl_still_io_t io = {0, s_, w_};
    cl_still_count_t c = {0, 0, 0, 0, 0};
    const int r = cl_still_rounds(&io, bits, bits_size, crcs, src, row, stride, bytes, (uint16_t)index, head,
                                  (uint16_t)head_len, &c);
    n[0] = c.rounds; n[1] = c.heads; n[2] = c.blocks; n[3] = c.ends; n[4] = c.acks;
    return r;
}
'''

SEND_FN = ctypes.CFUNCTYPE(None, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint32)
WAIT_FN = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_uint32)


def build(tmp):
    src = os.path.join(tmp, 'still_wrap.c')
    so = os.path.join(tmp, 'still_wrap.so')
    open(src, 'w').write(WRAP)
    subprocess.check_call(['cc', '-O1', '-Wall', '-Werror', '-shared', '-fPIC', '-I', os.path.join(ROOT, 'loader'), '-I',
                           os.path.join(ROOT, 'include'), '-o', so, src])
    lib = ctypes.CDLL(so)
    lib.run_still.restype = ctypes.c_int
    return lib


class Link:
    """The PC's side: a receiver, the datagrams lost by number (per kind), the answers back to the sender."""

    def __init__(self, kind, tmp, lose):
        self.kind, self.lose = kind, lose
        self.t = 0.0
        self.seen = {'head': 0, 'block': 0, 'end': 0}
        self.lost = []
        self.ack = None
        self.held = []
        if kind == 'loop':
            self.sink = cl.StillSink(None, 0xabc, lambda s: None)
        else:
            self.sink = bcore.Stills(os.path.join(tmp, 'bridge-%d' % random.getrandbits(32)), lambda s: None)

    def send(self, type_, run, a, b, payload):   # the receiver's answer: STILLACK (a = index, b = missing)
        if type_ == cl.STILLACK:
            self.ack = (b, bytes(payload))

    def datagram(self, a, index, data):
        self.t += SEND_S
        k = 'head' if a == cl.STILL_HEAD else 'end' if a == cl.STILL_END else 'block'
        n = self.seen[k]
        self.seen[k] += 1
        if (k, n) in self.lose or (k, '*') in self.lose:
            self.lost.append((k, n))
            return
        if self.kind == 'loop':
            self.sink.on_still(self, a, index, data)
            self.held = [s for s in self.sink.saved if s['crc_ok']]
        else:
            self.sink.on_still(self, 0xabc, a, index, data, 'host', lambda j: self.held.append(j) if j['crc_ok'] else 0)


def c_sender(lib, link, pix, index, head, sent_crc=True, moving=None):
    """sent_crc: the CRC of the bytes as sent (round 3), else taken before sending (crcs NULL). moving: a random
    generator that redraws part of the picture after every block sent (a program drawing during the grab)."""
    bits = (ctypes.c_uint8 * ((W * H * 2 // cl.CHUNK) // 8 + 16))()
    n = (ctypes.c_uint32 * 5)()
    crcs = (ctypes.c_uint32 * ((len(pix) + cl.CHUNK - 1) // cl.CHUNK))() if sent_crc else None
    buf = (ctypes.c_uint8 * len(pix)).from_buffer_copy(pix)

    def s(a, i, p, ln):
        link.datagram(a, i, ctypes.string_at(p, ln))
        if moving is not None and a < len(pix):   # the program draws on: a few rows change under the sender
            y = moving.randrange(H - 8)
            ctypes.memset(ctypes.addressof(buf) + y * W * 2, moving.getrandbits(8), 8 * W * 2)

    def w(ms):
        if link.ack is None:
            link.t += ms / 1000.0
            return -1
        missing, bm = link.ack
        link.ack = None
        link.t += 0.001
        ctypes.memmove(bits, bm, min(len(bm), ctypes.sizeof(bits)))
        return missing

    sf, wf = SEND_FN(s), WAIT_FN(w)
    hb = (ctypes.c_uint8 * len(head)).from_buffer_copy(head)
    r = lib.run_still(sf, wf, bits, ctypes.sizeof(bits), crcs, buf, W * 2, W * 2, len(pix), index, hb, len(head), n)
    return r, {'rounds': n[0], 'heads': n[1], 'blocks': n[2], 'ends': n[3], 'answers': n[4]}


def old_sender(link, pix, index, head):
    """ql-core5's client.c still_send, order kept: the head once, then up to 12 rounds of the missing blocks, each
    followed by up to 10 ENDs of 300 ms."""
    blocks = (len(pix) + cl.CHUNK - 1) // cl.CHUNK
    have = bytearray(blocks)
    link.datagram(cl.STILL_HEAD, index, head)
    sent = 0
    for rnd in range(12):
        for k in range(blocks):
            if not have[k]:
                link.datagram(k * cl.CHUNK, index, pix[k * cl.CHUNK:(k + 1) * cl.CHUNK])
                sent += 1
        acked = None
        for _ in range(10):
            link.datagram(cl.STILL_END, index, head)
            if link.ack is not None:
                acked, link.ack = link.ack, None
                link.t += 0.001
                break
            link.t += WAIT_S
        if acked is not None:
            for k in range(blocks):
                have[k] = (acked[1][k >> 3] >> (k & 7)) & 1 if (k >> 3) < len(acked[1]) else 0
            if acked[0] == 0:
                return 0, {'rounds': rnd + 1, 'blocks': sent}
    return -2, {'rounds': 12, 'blocks': sent}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=None)
    a = ap.parse_args()
    rng = random.Random(6)
    pix = bytes(rng.getrandbits(8) for _ in range(W * H * 2))
    crc = zlib.crc32(pix) & 0xFFFFFFFF
    results, allok = [], True
    with tempfile.TemporaryDirectory(dir='/dev/shm' if os.path.isdir('/dev/shm') else None) as tmp:
        lib = build(tmp)
        cases = [
            ('clean', set(), 'held', 1.0),
            ('the first head lost (the console\'s case)', {('head', 0)}, 'held', 2.0),
            ('one block lost', {('block', 123)}, 'held', 2.0),
            ('the first END lost', {('end', 0)}, 'held', 2.0),
            ('the first head and its first repeat lost', {('head', 0), ('head', 1)}, 'held', 3.0),
            ('CONTROL: every head lost', {('head', '*')}, 'not held', None),
        ]
        index = 1
        for kind in ('loop', 'bridge'):
            for name, lose, want, max_s in cases:
                head = cl.struct.pack('<8I', index, 7, W, H, 1, len(pix), crc, 0) if hasattr(cl, 'struct') else \
                    __import__('struct').pack('<8I', index, 7, W, H, 1, len(pix), crc, 0)
                link = Link(kind, tmp, lose)
                r, n = c_sender(lib, link, pix, index, head)
                got = 'held' if (r == 0 and len(link.held) == 1) else 'not held'
                ok = got == want and (max_s is None or link.t <= max_s)
                allok &= ok
                results.append({'receiver': kind, 'case': name, 'want': want, 'got': got, 'rc': r,
                                'seconds_sim': round(link.t, 2), 'lost': link.lost[:4], 'sent': n, 'pass': ok})
                print('%-4s %-6s %-46s %-8s rc %2d  %6.2f s sim  %s' % ('PASS' if ok else 'FAIL', kind, name, got, r,
                                                                       link.t, n), flush=True)
                index += 1
        # round 3: a picture that changes while it is sent
        mcases = [
            ('moving picture (the CRC of the bytes as sent)', set(), True, 'held', 2.0),
            ('moving picture, one block lost', {('block', 77)}, True, 'held', 3.0),
            ('CONTROL: moving picture, CRC before sending (dccfe98)', set(), False, 'not held', None),
        ]
        for kind in ('loop', 'bridge'):
            for name, lose, sent_crc, want, max_s in mcases:
                head = struct.pack('<8I', index, 7, W, H, 1, len(pix), crc, 0)
                link = Link(kind, tmp, lose)
                r, n = c_sender(lib, link, pix, index, head, sent_crc=sent_crc, moving=random.Random(index))
                got = 'held' if (r == 0 and len(link.held) == 1) else 'not held'
                ok = got == want and (max_s is None or link.t <= max_s)
                allok &= ok
                results.append({'receiver': kind, 'case': name, 'want': want, 'got': got, 'rc': r,
                                'seconds_sim': round(link.t, 2), 'lost': link.lost[:4], 'sent': n, 'pass': ok})
                print('%-4s %-6s %-46s %-8s rc %2d  %6.2f s sim  %s' % ('PASS' if ok else 'FAIL', kind, name[:46], got,
                                                                       r, link.t, n), flush=True)
                index += 1
        # control: the ql-core5 sender, the console's loss
        head = struct.pack('<8I', index, 7, W, H, 1, len(pix), crc, 0)
        link = Link('loop', tmp, {('head', 0)})
        r, n = old_sender(link, pix, index, head)
        ok = r != 0 and link.t > 30.0
        allok &= ok
        results.append({'receiver': 'loop', 'case': 'CONTROL: the ql-core5 sender, the first head lost', 'want':
                        'not held, over 30 s', 'got': 'held' if r == 0 else 'not held', 'rc': r,
                        'seconds_sim': round(link.t, 2), 'sent': n, 'pass': ok})
        print('%-4s %-6s %-46s %-8s rc %2d  %6.2f s sim  %s  (console: NOT HELD in 43.5 s)' % (
            'PASS' if ok else 'FAIL', 'loop', 'CONTROL: the ql-core5 sender, head lost', 'held' if r == 0 else
            'not held', r, link.t, n), flush=True)
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        json.dump({'results': results, 'all_pass': allok}, open(os.path.join(a.out, 'test_still.json'), 'w'), indent=1)
    print('test_still: %s (%d cases)' % ('ALL PASS' if allok else 'FAIL', len(results)))
    return 0 if allok else 1


if __name__ == '__main__':
    sys.exit(main())
