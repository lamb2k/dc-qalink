"""render.py: the PC side of the round trip. A reference picture of a DQA1 asset at 640x480 on the shared camera
(docs/ASSETS.md: the same formulas the console viewer and QA Link Cloud's preview use), written as the console's
16-bit screen would hold it, and the difference view between that and the console's grab.

The reference draws what was sent (the DQA1 file's own texture bytes, decoded), not the source picture, so a
difference is the console's drawing against the contract, not the converter's loss (reported separately)."""
import math

import numpy as np

from . import dqa, pvrtex

W, H = 640, 480
DEFAULT_CAMERA = {'yaw': math.radians(35), 'pitch': math.radians(28), 'dist': 3.1, 'fov': math.radians(50)}
DEFAULT_LIGHT = {'dir': [0.4, 0.75, 0.5], 'ambient': 0.25, 'diffuse': 0.75}


def camera_of(c):
    """The camera contract: yaw, pitch, fov in radians; dist in units of the model's bounding radius (1.1..100)."""
    c = dict(DEFAULT_CAMERA, **(c or {}))
    c['dist'] = max(1.1, min(100.0, float(c['dist'])))
    c['fov'] = max(0.05, min(2.8, float(c['fov'])))
    return {k: float(c[k]) for k in ('yaw', 'pitch', 'dist', 'fov')}


def light_of(l):
    l = dict(DEFAULT_LIGHT, **(l or {}))
    d = np.array(l['dir'], np.float64)
    return {'dir': list(d / np.linalg.norm(d)), 'ambient': float(l['ambient']), 'diffuse': float(l['diffuse'])}


def to565(rgb):
    """The 16-bit screen (dithering off, the viewer's default): each channel cut to 5-6-5 bits, then read back as the
    bridge writes a grab's PNG (dcql_loop.png_rgb565: c * 255 // 31, c * 255 // 63)."""
    r, g, b = (rgb[..., i].astype(np.int32) for i in range(3))
    r5, g6, b5 = r >> 3, g >> 2, b >> 3
    return np.stack([r5 * 255 // 31, g6 * 255 // 63, b5 * 255 // 31], -1).astype(np.uint8)


def project(v, cam):
    cy, sy, cp, sp = math.cos(cam['yaw']), math.sin(cam['yaw']), math.cos(cam['pitch']), math.sin(cam['pitch'])
    f = (H / 2) / math.tan(cam['fov'] / 2)
    x, y, z = v['x'].astype(np.float64), v['y'].astype(np.float64), v['z'].astype(np.float64)
    x1, z1 = x * cy - z * sy, x * sy + z * cy
    y1, z2 = y * cp - z1 * sp, y * sp + z1 * cp
    iw = 1.0 / (cam['dist'] - z2)
    sx, sy_ = W / 2 + x1 * f * iw, H / 2 - y1 * f * iw
    nx, ny, nz = (v[k].astype(np.float64) / 127.0 for k in ('nx', 'ny', 'nz'))
    nx1, nz1 = nx * cy - nz * sy, nx * sy + nz * cy
    ny1, nz2 = ny * cp - nz1 * sp, ny * sp + nz1 * cp
    return sx, sy_, iw, np.stack([nx1, ny1, nz2], -1)


def shade_colours(argb, s):
    """The viewer's shade_argb(): k = int(s * 256), channel * k >> 8, s at most 1."""
    k = (np.minimum(s, 1.0) * 256).astype(np.int64)
    c = argb.astype(np.int64)
    r, g, b = ((c >> 16 & 255) * k) >> 8, ((c >> 8 & 255) * k) >> 8, ((c & 255) * k) >> 8
    return np.stack([r, g, b, c >> 24 & 255], -1).astype(np.float64)


def sample(levels, mip, u, v, filt, lod):
    lv = 0
    if mip:
        lv = int(max(0, min(len(levels) - 1, math.floor(lod + 0.5))))
    t = levels[lv].astype(np.float64)
    th, tw = t.shape[:2]
    if filt == 'bilinear':
        x, y = u * tw - 0.5, v * th - 0.5
        x0, y0 = np.floor(x), np.floor(y)
        fx, fy = (x - x0)[:, None], (y - y0)[:, None]
        xi, yi = x0.astype(np.int64), y0.astype(np.int64)
        g = lambda a, b: t[(b % th), (a % tw)]
        return (g(xi, yi) * (1 - fx) * (1 - fy) + g(xi + 1, yi) * fx * (1 - fy) + g(xi, yi + 1) * (1 - fx) * fy +
                g(xi + 1, yi + 1) * fx * fy)
    return t[(np.floor(v * th).astype(np.int64) % th), (np.floor(u * tw).astype(np.int64) % tw)]


def render(asset, camera=None, light=None, bg=(0, 0, 0), filt='nearest', view=None, cull='cw'):
    """(480, 640, 3) uint8: what the console's screen should hold. asset: dqa.parse() of the bytes sent."""
    cam, li = camera_of(camera), light_of(light)
    out = np.empty((H, W, 3), np.float64)
    out[:] = np.array(bg, np.float64)
    texs = asset['textures']
    levels = [pvrtex.decode(t) for t in texs]
    if view is None:
        view = 'flat' if asset['kind'] == dqa.KIND_TEXTURE else 'model'
    if view in ('flat', 'mips') or not asset['strips']:
        if not texs:
            return to565(out)
        t, lv = texs[0], levels[0]
        quads = []
        if view == 'mips' and t['mip']:
            x, s = 8, t['w']
            k = 0
            while s >= 1 and x + s <= W:
                quads.append((x, (H - s) // 2, lv[k]))
                x += s + 8
                if s == 1:
                    break
                s >>= 1
                k += 1
        else:
            w, h = min(t['w'], W), min(t['h'], H)
            quads.append(((W - w) // 2, (H - h) // 2, lv[0][:h, :w]))
        for x0, y0, im in quads:
            h, w = im.shape[:2]
            src = im.astype(np.float64)
            dst = out[y0:y0 + h, x0:x0 + w]
            if t['alpha']:
                a = src[..., 3:4] / 255.0
                dst[:] = np.floor(src[..., :3] * a + dst * (1 - a))
            else:
                dst[:] = src[..., :3]
        return to565(out)
    # the model: strips to triangles, the viewer's transform and shading, culling, a depth buffer
    v = asset['verts']
    sx, sy, iw, n = project(v, cam)
    d = np.maximum(0.0, n @ np.array(li['dir']))
    col = shade_colours(v['argb'], li['ambient'] + li['diffuse'] * d)
    zb = np.zeros((H, W), np.float64)
    tri_list = []
    for first, cnt, tex in asset['strips']:
        for i in range(cnt - 2):
            a, b, c = first + i, first + i + 1, first + i + 2
            if i % 2:
                a, b = b, a
            tri_list.append((a, b, c, tex))
    opaque = [t for t in tri_list if t[3] < 0 or not texs[t[3]]['alpha']]
    blended = [t for t in tri_list if t[3] >= 0 and texs[t[3]]['alpha']]
    blended.sort(key=lambda t: iw[list(t[:3])].mean())      # far first (larger 1/w is nearer)
    for group, blend in ((opaque, False), (blended, True)):
        for a, b, c, tex in group:
            area = (sx[b] - sx[a]) * (sy[c] - sy[a]) - (sx[c] - sx[a]) * (sy[b] - sy[a])
            if area == 0 or (cull == 'cw' and area > 0) or (cull == 'ccw' and area < 0):
                continue
            x0, x1 = max(0, int(math.floor(min(sx[a], sx[b], sx[c])))), min(W - 1, int(math.ceil(max(sx[a], sx[b], sx[c]))))
            y0, y1 = max(0, int(math.floor(min(sy[a], sy[b], sy[c])))), min(H - 1, int(math.ceil(max(sy[a], sy[b], sy[c]))))
            if x0 > x1 or y0 > y1:
                continue
            py, px = np.mgrid[y0:y1 + 1, x0:x1 + 1]
            # pixel centres, nudged a hair right and down: a centre exactly on an edge belongs to the triangle on its
            # top or left only (the chip's fill rule; Flycast run f1: the flag's bottom row was drawn here, not there)
            px, py = px.ravel() + 0.5 + 1.7e-5, py.ravel() + 0.5 + 1.3e-5
            w0 = ((sx[b] - px) * (sy[c] - py) - (sx[c] - px) * (sy[b] - py)) / area
            w1 = ((sx[c] - px) * (sy[a] - py) - (sx[a] - px) * (sy[c] - py)) / area
            w2 = 1 - w0 - w1
            inside = (w0 >= 0) & (w1 >= 0) & (w2 >= 0)
            if not inside.any():
                continue
            px, py, w0, w1, w2 = px[inside], py[inside], w0[inside], w1[inside], w2[inside]
            z = w0 * iw[a] + w1 * iw[b] + w2 * iw[c]
            X, Y = px.astype(np.int64), py.astype(np.int64)
            keep = z > zb[Y, X]
            if not keep.any():
                continue
            X, Y, z, w0, w1, w2 = X[keep], Y[keep], z[keep], w0[keep], w1[keep], w2[keep]
            pa, pb, pc = w0 * iw[a] / z, w1 * iw[b] / z, w2 * iw[c] / z     # perspective-correct weights
            cc = pa[:, None] * col[a] + pb[:, None] * col[b] + pc[:, None] * col[c]
            if tex >= 0:
                t = texs[tex]
                u = pa * v['u'][a] + pb * v['u'][b] + pc * v['u'][c]
                vv = pa * v['v'][a] + pb * v['v'][b] + pc * v['v'][c]
                # the level: texels a pixel over the triangle
                uvarea = abs((v['u'][b] - v['u'][a]) * (v['v'][c] - v['v'][a]) - (v['u'][c] - v['u'][a]) * (v['v'][b] - v['v'][a]))
                lod = 0.5 * math.log2(max(1e-12, uvarea * t['w'] * t['h'] / abs(area))) if t['mip'] else 0.0
                tx = sample(levels[tex], t['mip'], u, vv, filt, lod)
                rgb = np.floor(tx[:, :3] * cc[:, :3] / 255.0)
                if blend:
                    al = tx[:, 3:4] / 255.0
                    rgb = np.floor(rgb * al + out[Y, X] * (1 - al))
            else:
                rgb = np.floor(cc[:, :3])
            out[Y, X] = rgb
            if not blend:
                zb[Y, X] = z
    return to565(np.clip(out, 0, 255))


def difference(pc, console, threshold=8):
    """Numbers and a picture for the difference view: per pixel the largest channel difference."""
    a, b = pc.astype(np.int32), console[..., :3].astype(np.int32)
    d = np.abs(a - b).max(-1)
    mse = float(np.mean((a - b) ** 2))
    stats = {'pixels': int(d.size), 'differ': int((d > 0).sum()), 'over': int((d > threshold).sum()),
             'threshold': threshold, 'mean_abs': round(float(np.abs(a - b).mean()), 4), 'max': int(d.max()),
             'psnr': 99.0 if mse == 0 else round(10 * math.log10(255.0 ** 2 / mse), 2)}
    pic = np.zeros(pc.shape[:2] + (3,), np.uint8)
    amp = np.clip(d * 4, 0, 255).astype(np.uint8)
    pic[..., 0] = amp
    pic[..., 1] = np.where(d > threshold, amp // 2, 0)
    grey = (a.mean(-1) * 0.25).astype(np.uint8)
    for i in range(3):
        pic[..., i] = np.where(d == 0, grey, pic[..., i])
    return stats, pic


def side_by_side(pc, console, diff):
    gap = np.full((H, 8, 3), 40, np.uint8)
    return np.concatenate([pc, gap, console[..., :3], gap, diff], 1)
