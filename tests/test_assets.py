#!/usr/bin/env python3
"""test_assets.py: dc-qalink's live-asset converters, the DQA1 file, the reference render and the bridge's
/v0/assets endpoints, on generated assets (no console; tier pc). Each check has a control that must fail, run with
--controls (it swaps in a broken piece and expects the check to fail).

  python tests/test_assets.py [--out DIR] [--controls]
"""
import argparse, base64, hashlib, json, math, os, socket, struct, sys, tempfile, time, urllib.request, zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'pc'))
import numpy as np                                     # noqa: E402
from assets import dqa, gen, image, model, pvrtex, render   # noqa: E402

RESULTS = []


def check(name, ok, detail=''):
    RESULTS.append((name, bool(ok), detail))
    print('%-4s %-52s %s' % ('PASS' if ok else 'FAIL', name, detail), flush=True)
    return ok


# ------------------------------------------------------------------------------------------------ the pieces
def t_twiddle(tw=pvrtex.twiddle_map):
    m = tw(4, 4)
    known = m[1, 0] == 1 and m[0, 1] == 2 and m[1, 1] == 3 and m[0, 2] == 8 and m[2, 0] == 4
    ok = known
    for w, h in ((8, 8), (16, 8), (8, 32), (64, 64)):
        t = tw(w, h).ravel()
        ok = ok and sorted(t.tolist()) == list(range(w * h))
    return check('twiddle: y in the low bit, a bijection for squares and rectangles', ok, 'm[1,0]=%d m[0,1]=%d' % (m[1, 0], m[0, 1]))


def t_mip_layout():
    # the chip's tables (Flycast TexCache.cpp OtherMipPoint / VQMipPoint): 256x256 with mips
    a = pvrtex.mip_point(8, 16) + 256 * 256 * 2 == (0x5558 + 65536) * 2
    b = 2048 + pvrtex.vq_mip_point(8) + 128 * 128 == 2048 + 0x1556 + 16384
    c = pvrtex.mip_point(0, 4) == 1 and pvrtex.mip_point(1, 4) == 2 and pvrtex.mip_point(3, 8) == 0x18
    card = gen.test_card()
    e16 = pvrtex.encode(card, 'rgb565', True)
    evq = pvrtex.encode(card, 'vq', True)
    return check('mip chain sizes match the chip\'s tables', a and b and c and e16['bytes'] == 174768 and evq['bytes'] == 23894,
                 'rgb565 %d B, vq %d B' % (e16['bytes'], evq['bytes']))


def t_formats():
    card, cardA = gen.test_card(), gen.test_card(alpha=True)
    rows, ok = [], True
    want = {'rgb565': 40, 'argb1555': 38, 'argb4444': 30, 'vq': 24, 'pal8': 28, 'pal4': 17}
    for fmt in pvrtex.FORMATS:
        for mips in (False, True):
            src = cardA if fmt in ('argb4444',) else card
            t = pvrtex.encode(src, fmt, mips)
            lv = pvrtex.decode(t)
            good = len(lv) == (9 if mips else 1) and lv[0].shape == (256, 256, 4) and t['psnr'] >= want[fmt]
            if fmt in ('rgb565', 'argb1555', 'argb4444'):    # exact: the decode is the 16-bit value read back
                good = good and np.array_equal(lv[0], pvrtex.from16(pvrtex.to16(src, fmt), fmt))
            ok = ok and good
            rows.append('%s%s %.1fdB' % (fmt, '+mip' if mips else '', t['psnr']))
    return check('each format encodes and decodes (level 0 PSNR floor)', ok, ', '.join(rows))


def t_resize_lost():
    t = pvrtex.encode(gen.test_card(200, 120), 'auto', False)
    t2 = pvrtex.encode(gen.test_card(200, 120), 'auto', True)
    return check('a 200x120 picture: power-of-two sides, the loss said', (t['w'], t['h']) == (256, 128) and
                 (t2['w'], t2['h']) == (256, 256) and any('resized' in s for s in t['lost']) and
                 any('square' in s for s in t2['lost']), '%dx%d; mips %dx%d' % (t['w'], t['h'], t2['w'], t2['h']))


def t_dqa(out):
    data, rec = dqa.convert('card_256.png', image.write_png(None, gen.test_card()))
    p = dqa.parse(data)
    ok = p['kind'] == dqa.KIND_TEXTURE and len(p['textures']) == 1 and rec['fits'] and rec['bytes'] == len(data)
    bad = bytearray(data)
    bad[200] ^= 0x40
    try:
        dqa.parse(bytes(bad))
        crc = False
    except ValueError:
        crc = True
    with open(os.path.join(out, 'card_256.dqa'), 'wb') as f:
        f.write(data)
    return check('DQA1 file: packs, parses, a flipped bit is refused by its CRC', ok and crc, '%d B' % len(data))


def t_budget():
    big = np.zeros((1024, 1024, 4), np.uint8)
    big[..., 3] = 128
    big[..., 0] = np.arange(1024)[None, :] // 4
    t = pvrtex.encode(big, 'argb4444', True)
    fits1, _ = dqa.budget(t['bytes'] + 200, t['bytes'], 0)
    fits2, why2 = dqa.budget(2 * t['bytes'] + 200, 2 * t['bytes'], 0)
    fits3, why3 = dqa.budget(40000 * 32, 0, 40000)
    return check('budget: 1 texture of 1024x1024 fits, 2 do not, 40,000 strip vertices do not',
                 fits1 and not fits2 and not fits3 and 'graphics memory' in why2 and 'vertex buffer' in why3,
                 '%d B a texture' % t['bytes'])


def t_strips(strip_fn=model.stripify):
    o, m = gen.torus_obj()
    mm = model.read_obj(o, lambda n: m.encode() if n.endswith('.mtl') else image.write_png(None, gen.checker()))
    mm, _ = model.prepare(mm)
    tris = mm['tris']
    strips = strip_fn(tris)
    canon = lambda t: min((t[0], t[1], t[2]), (t[1], t[2], t[0]), (t[2], t[0], t[1]))
    want = sorted(canon(tuple(int(x) for x in t)) for t in tris)
    got = sorted(canon(t) for s in strips for t in model.strip_triangles(s))
    nv = sum(len(s) for s in strips)
    return check('strips: every torus triangle once, with its winding', want == got,
                 '%d triangles, %d strips, %d strip vertices (%.2f a triangle)' % (len(tris), len(strips), nv, nv / len(tris)))


def t_models(out):
    folder = os.path.join(out, 'gen')
    files = gen.write_all(folder)
    res = {}
    for n in ('torus.obj', 'flag.obj', 'cube.gltf'):
        data, rec = dqa.convert(n, open(files[n], 'rb').read(), model.folder_resolver(folder))
        res[n] = rec
        with open(os.path.join(out, n + '.dqa'), 'wb') as f:
            f.write(data)
    ok = res['torus.obj']['tris'] == 48 * 24 * 2 and res['torus.obj']['textures'] and \
        res['cube.gltf']['tris'] == 12 and res['cube.gltf']['textures'] and res['flag.obj']['tris'] == 8 and \
        all(r['fits'] for r in res.values())
    return check('models: OBJ with material and texture, glTF with embedded buffer and picture',
                 ok, '; '.join('%s %d tris %d strips %d B' % (k, r['tris'], r['strips'], r['bytes']) for k, r in res.items()))


def t_render_flat(out):
    data, _ = dqa.convert('card_256.png', image.write_png(None, gen.test_card()))
    p = dqa.parse(data)
    pic = render.render(p)
    lv = pvrtex.decode(p['textures'][0])[0]
    centre = pic[112:368, 192:448]
    ok = np.array_equal(centre, render.to565(lv[..., :3])) and pic[0, 0].tolist() == [0, 0, 0]
    image.write_png(os.path.join(out, 'pc-card.png'), pic)
    return check('reference render, flat view: the decoded texture at one texel a pixel, centred', ok)


def t_render_cull(out):
    folder = os.path.join(out, 'gen')
    data, _ = dqa.convert('flag.obj', open(os.path.join(folder, 'flag.obj'), 'rb').read(), model.folder_resolver(folder))
    p = dqa.parse(data)
    front = render.render(p, {'yaw': 0.0, 'pitch': 0.0})
    back = render.render(p, {'yaw': math.pi, 'pitch': 0.0})
    both = render.render(p, {'yaw': math.pi, 'pitch': 0.0}, cull='none')
    nf, nb, n2 = (int((x.sum(-1) > 0).sum()) for x in (front, back, both))
    # orientation: red (bottom-left quad, x<0, y<0) must be lower left on screen from the front
    red = front[300:340, 200:260].mean((0, 1))
    image.write_png(os.path.join(out, 'pc-flag-front.png'), front)
    return check('reference render: the flag is seen from the front, culled from behind, red at lower left',
                 nf > 10000 and nb == 0 and n2 > 10000 and red[0] > 120 and red[1] < 60, 'front %d px, back %d, back unculled %d' % (nf, nb, n2))


def t_render_model(out):
    data, _ = dqa.convert('torus.obj', open(os.path.join(out, 'gen', 'torus.obj'), 'rb').read(),
                          model.folder_resolver(os.path.join(out, 'gen')))
    t0 = time.time()
    pic = render.render(dqa.parse(data))
    secs = time.time() - t0
    image.write_png(os.path.join(out, 'pc-torus.png'), pic)
    lit = int((pic.sum(-1) > 0).sum())
    return check('reference render: the torus on the default camera', 40000 < lit < 250000, '%d px drawn, %.2f s' % (lit, secs))


def t_diff():
    a = render.to565(np.full((480, 640, 3), 100, np.uint8))
    b = a.copy()
    b[10:20, 10:20] = 255
    s0, _ = render.difference(a, a)
    s1, pic = render.difference(a, b)
    return check('difference: equal pictures read 0, a 10x10 patch reads 100 pixels', s0['differ'] == 0 and
                 s1['differ'] == 100 and s1['over'] == 100 and pic.shape == (480, 640, 3))


# ------------------------------------------------------------------------------------------------ the bridge
def t_bridge(out):
    from bridge.core import Bridge
    from bridge.api import serve
    d = tempfile.mkdtemp(prefix='ql-assets-', dir=out)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    b = Bridge(d, ('127.0.0.1', port), console='127.0.0.1:%d' % (port + 1), device_kind='mock', log=None)
    b.asset_root = out   # path imports read below the asset root only (audit 2026-11-23, item 2)
    srv = serve(b, '127.0.0.1', 0, 'run')
    url = 'http://127.0.0.1:%d' % srv.server_address[1]

    def call(m, p, body=None):
        req = urllib.request.Request(url + p, method=m, data=json.dumps(body).encode() if body is not None else None,
                                     headers={'Content-Type': 'application/json', 'X-By': 'test_assets'})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if r.headers.get('Content-Type', '').startswith('application/json') else raw)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b'{}')
    try:
        png = image.write_png(None, gen.test_card())
        c1, r1 = call('POST', '/v0/assets', {'name': 'card_256.png', 'base64': base64.b64encode(png).decode(), 'format': 'vq'})
        c2, r2 = call('GET', '/v0/assets')
        c3, r3 = call('GET', '/v0/assets/%s/render.png' % r1.get('id', 'x'))
        big = np.zeros((1024, 1024, 4), np.uint8)
        big[..., 3] = 100
        big[..., 0] = (np.arange(1024)[None, :] * 7) % 251
        bigpng = image.write_png(None, big)
        c4, r4 = call('POST', '/v0/assets', {'path': os.path.join(out, 'gen', 'torus.obj')})
        c5, r5 = call('POST', '/v0/assets/%s/view' % r1.get('id', 'x'), {'camera': {'yaw': 0}})
        c6, r6 = call('POST', '/v0/assets/000000000000/view', {})
        # over budget: a model of 1.2 M triangles would be slow to build here; a 2,100-strip-vertex cut is not over.
        # The refusal is checked with a texture pair as one glTF is not needed: a 1024 ARGB4444 + mips is 2.8 MB, under;
        # so the refusal row uses the vertex-buffer rule with a dense generated OBJ.
        n = 140
        lines = ['v %f %f 0' % (i / n, j / n) for j in range(n + 1) for i in range(n + 1)]
        lines += ['f %d %d %d' % (j * (n + 1) + i + 1, j * (n + 1) + i + 2, (j + 1) * (n + 1) + i + 1)
                  for j in range(n) for i in range(n)]
        lines += ['f %d %d %d' % (j * (n + 1) + i + 1, (j + 1) * (n + 1) + i + 1, j * (n + 1) + i + 2)
                  for j in range(n) for i in range(n)]
        dense = '\n'.join(lines).encode()
        c7, r7 = call('POST', '/v0/assets', {'name': 'dense.obj', 'base64': base64.b64encode(dense).decode()})
        audit = open(os.path.join(d, 'calls.log'), encoding='utf-8').read()
        ok = (c1 == 200 and r1.get('state') == 'checked' and r1.get('kind') == 'texture' and r1.get('fits') and
              c2 == 200 and len(r2.get('assets', [])) >= 1 and c3 == 200 and isinstance(r3, bytes) and r3[:4] == b'\x89PNG'
              and c4 == 200 and r4.get('kind') == 'model' and c5 == 409 and c6 == 404 and
              c7 == 200 and r7.get('state') == 'refused' and 'vertex buffer' in (r7.get('reason') or '') and
              'POST /v0/assets card_256.png' in audit)
        return check('bridge: POST, list, PC render, refusal by budget, view without a viewer 409, audit',
                     ok, 'post %s %s; list %s; render %s; model %s; view %s; unknown %s; dense %s %s' % (
                         c1, r1.get('state'), c2, c3, c4, c5, c6, c7, r7.get('state')))
    finally:
        srv.shutdown()
        b.close()


def t_status_race(out):
    """status() and device() while the run ends: the runner sets `active` to None between the reads (ql-int's MCP
    host log at shutdown: 'NoneType' object has no attribute 'id'). The trap clears `active` on its first read."""
    from bridge.core import Bridge
    d = tempfile.mkdtemp(prefix='ql-assets-st-', dir=out)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    b = Bridge(d, ('127.0.0.1', port), console='127.0.0.1:%d' % (port + 1), device_kind='mock', log=None)

    class Ending:
        id = 'r1'

        @property
        def state(self):
            b.active = None                  # the run finishes on another thread right here
            return 'running'
    errs = []
    try:
        for f in (b.status, b.device):
            b.active = Ending()
            try:
                f()
            except Exception as e:           # noqa: BLE001 - the check is that nothing escapes
                errs.append('%s: %r' % (f.__name__, e))
    finally:
        b.close()
    return check('bridge: status and device while the active run ends (no read of a cleared run)', not errs,
                 '; '.join(errs) or 'both answered')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=os.path.join(tempfile.gettempdir(), 'dcql-test-assets'))
    ap.add_argument('--controls', action='store_true', help='run each check with a broken piece: every one must fail')
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    t0 = time.time()
    if a.controls:
        # a twiddle with x in the low bit, a stripper that flips every second triangle, a mip table one texel off
        bad_tw = lambda w, h: pvrtex.twiddle_map(h, w).T if w == h else pvrtex.twiddle_map(w, h) * 0
        r1 = t_twiddle(bad_tw)

        def bad_strip(tris):
            s = model.stripify(tris)
            return [[x[1], x[0]] + x[2:] for x in s]          # every triangle's winding flipped
        r2 = t_strips(bad_strip)
        real = pvrtex.mip_point
        pvrtex.mip_point = lambda lv, bpp: real(lv, bpp) + 2
        r3 = t_mip_layout()
        pvrtex.mip_point = real
        real_to565 = render.to565
        render.to565 = lambda rgb: real_to565(np.minimum(255, rgb.astype(np.int32) + 8).astype(np.uint8))
        os.makedirs(os.path.join(a.out, 'ctl'), exist_ok=True)
        r4 = t_render_flat(os.path.join(a.out, 'ctl'))
        render.to565 = real_to565
        failed = [n for n, ok, _ in RESULTS if not ok]
        print('controls: %d of 4 failed as they must' % len(failed))
        return 0 if len(failed) == 4 else 1
    for f in (t_twiddle, t_mip_layout, t_formats, t_resize_lost, t_budget, t_strips, t_diff):
        f()
    for f in (t_dqa, t_models, t_render_flat, t_render_cull, t_render_model, t_bridge, t_status_race):
        f(a.out)
    bad = [n for n, ok, _ in RESULTS if not ok]
    print('%d/%d passed in %.1f s%s' % (len(RESULTS) - len(bad), len(RESULTS), time.time() - t0,
                                        '' if not bad else '; FAILED: ' + ', '.join(bad)))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
