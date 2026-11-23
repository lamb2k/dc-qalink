"""dqa.py: the DQA1 live-asset file (example/viewer/dqa.h is the C side; docs/ASSETS.md) and the one call that turns a
dropped file into it: convert(). The budget is the one QA Link Cloud's Assets workspace shows and the console viewer
enforces: the asset block a quarter of main memory (4 MiB), its textures half of graphics memory (4 MiB), and the
viewer's vertex buffer (1 MiB, 32 bytes a strip vertex) for one picture."""
import hashlib, struct, zlib

import numpy as np

from . import image, model, pvrtex

MAGIC = b'DQA1'
KIND_TEXTURE, KIND_MODEL = 1, 2
BLOCK_BYTES = 4 << 20           # main memory for live assets: a quarter of 16 MiB
VRAM_BUDGET = 4 << 20           # graphics memory for live assets: half of 8 MiB
VBUF_BYTES = 1 << 20            # the viewer's vertex buffer for one picture
STRIP_VERT = 32
HEAD = struct.Struct('<4sHHII16sIIIIII4fII')     # 80 bytes
TEX = struct.Struct('<IHHIIIHHBBHI')            # 32 bytes
STRIP = struct.Struct('<IHHII')                 # 16 bytes
VERT = np.dtype([('x', '<f4'), ('y', '<f4'), ('z', '<f4'), ('nx', 'i1'), ('ny', 'i1'), ('nz', 'i1'), ('pad', 'i1'),
                 ('u', '<f4'), ('v', '<f4'), ('argb', '<u4'), ('res', '<u4')])
TEXTURE_EXT = ('.png', '.jpg', '.jpeg', '.bmp', '.tga', '.gif', '.webp')
MODEL_EXT = ('.obj', '.gltf', '.glb')
assert HEAD.size == 80 and TEX.size == 32 and STRIP.size == 16 and VERT.itemsize == 32


def _align(n, a=32):
    return (n + a - 1) // a * a


def kind_of(name):
    low = name.lower()
    if low.endswith(TEXTURE_EXT):
        return 'texture'
    if low.endswith(MODEL_EXT):
        return 'model'
    raise ValueError('%s: a texture (PNG, JPEG) or a model (OBJ, glTF, GLB)' % name)


def pack(kind, textures, strips, verts, src_sha, centre=(0, 0, 0), scale=1.0):
    """DQA1 bytes. textures: encode() dicts; strips: (first, n, tex); verts: VERT array."""
    pal_fmts = {t['palfmt'] for t in textures if t.get('palfmt') is not None}
    if len(pal_fmts) > 1:
        raise ValueError('one palette format for every texture of an asset')
    pal_fmt = pal_fmts.pop() if pal_fmts else 0xffffffff
    tex_off = _align(HEAD.size)
    strip_off = _align(tex_off + TEX.size * len(textures))
    vert_off = _align(strip_off + STRIP.size * len(strips))
    pos = _align(vert_off + VERT.itemsize * len(verts))
    blobs, recs = [], []
    for t in textures:
        d_off = pos
        pos = _align(pos + len(t['data']))
        p_off = pos
        pal = np.array(t['pal'], '<u4').tobytes()
        pos = _align(pos + len(pal))
        blobs.append((d_off, t['data']))
        blobs.append((p_off, pal))
        recs.append(TEX.pack(t['txrfmt'], t['w'], t['h'], d_off, len(t['data']), p_off, len(t['pal']),
                             t.get('pal_base', 0) if t['pal'] else 0, 1 if t['mip'] else 0, t['alpha'], 0, 0))
    buf = bytearray(pos)
    for i, r in enumerate(recs):
        buf[tex_off + i * TEX.size:tex_off + (i + 1) * TEX.size] = r
    for i, (first, n, tex) in enumerate(strips):
        buf[strip_off + i * STRIP.size:strip_off + (i + 1) * STRIP.size] = STRIP.pack(first, n, 0xffff if tex < 0 else tex, 0, 0)
    buf[vert_off:vert_off + VERT.itemsize * len(verts)] = verts.tobytes()
    for o, b in blobs:
        buf[o:o + len(b)] = b
    alpha = any(t['alpha'] for t in textures)
    head = HEAD.pack(MAGIC, kind, 1, len(buf), 0, src_sha[:16], len(textures), len(strips), len(verts), tex_off,
                     strip_off, vert_off, float(centre[0]), float(centre[1]), float(centre[2]), float(scale),
                     1 if alpha else 0, pal_fmt)
    buf[:HEAD.size] = head
    struct.pack_into('<I', buf, 12, zlib.crc32(bytes(buf[16:])) & 0xffffffff)
    return bytes(buf)


def parse(data):
    """The DQA1 file as dicts (the reference render reads this, so the PC draws exactly what was sent)."""
    (magic, kind, ver, nbytes, crc, sha, ntex, nstrips, nverts, tex_off, strip_off, vert_off, cx, cy, cz, sc, flags,
     pal_fmt) = HEAD.unpack_from(data, 0)
    if magic != MAGIC or ver != 1 or nbytes != len(data):
        raise ValueError('not a DQA1 file')
    if zlib.crc32(data[16:]) & 0xffffffff != crc:
        raise ValueError('DQA1 CRC mismatch')
    texs = []
    for i in range(ntex):
        word, w, h, d_off, d_len, p_off, p_n, p_base, mip, alpha, _, _ = TEX.unpack_from(data, tex_off + i * TEX.size)
        pal = list(np.frombuffer(data, '<u4', p_n, p_off)) if p_n else []
        texs.append({'txrfmt': word, 'w': w, 'h': h, 'data': data[d_off:d_off + d_len], 'mip': bool(mip), 'alpha': alpha,
                     'pal': pal, 'palfmt': pal_fmt if p_n else None, 'pal_base': p_base if p_n else None})
    strips = [STRIP.unpack_from(data, strip_off + i * STRIP.size)[:3] for i in range(nstrips)]
    verts = np.frombuffer(data, VERT, nverts, vert_off)
    return {'kind': kind, 'textures': texs, 'strips': [(a, n, -1 if t == 0xffff else t) for a, n, t in strips],
            'verts': verts, 'centre': (cx, cy, cz), 'scale': sc, 'pal_fmt': pal_fmt, 'bytes': nbytes, 'src_sha': sha.hex()}


def budget(nbytes, vram, nverts):
    """(fits, reason or None, numbers): the live-asset budget, the same rule as the app and the viewer."""
    reasons = []
    if nbytes > BLOCK_BYTES:
        reasons.append('the asset needs %d B of main memory; live assets get %d B (a quarter of 16 MB)' % (nbytes, BLOCK_BYTES))
    if vram > VRAM_BUDGET:
        reasons.append('its textures need %d B of graphics memory; live assets get %d B (half of 8 MB)' % (vram, VRAM_BUDGET))
    if nverts * STRIP_VERT > VBUF_BYTES:
        reasons.append('%d strip vertices need %d B of the vertex buffer for one picture; the viewer has %d B'
                       % (nverts, nverts * STRIP_VERT, VBUF_BYTES))
    return not reasons, '; '.join(reasons) or None


def convert(name, data, resolve=None, fmt='auto', mips=True, max_tex=1024, seed=1):
    """A dropped file (texture or model) to (DQA1 bytes, record). The record carries what QA Link Cloud's asset row
    holds (name, kind, tris, strips, stripVerts, texW, texH, note) and the bridge's AssetResult (bytes, sha256, ram,
    vram, state, reason), plus what the conversion lost."""
    sha = hashlib.sha256(data).digest()
    kind = kind_of(name)
    lost = []
    if kind == 'texture':
        rgba = image.read_image(data)
        t = pvrtex.encode(rgba, fmt, mips, max_size=max_tex, seed=seed)
        lost += t['lost']
        out = pack(KIND_TEXTURE, [t], [], np.zeros(0, VERT), sha)
        tris = strips_n = sverts = 0
        texw, texh = t['w'], t['h']
        psnr = t['psnr']
        note = '%dx%d %s, twiddled%s, %d B' % (t['w'], t['h'], t['fmt'].upper(), ', mip maps' if t['mip'] else '', t['bytes'])
        texinfo = [{'name': name, 'w': t['w'], 'h': t['h'], 'format': t['fmt'], 'mip': t['mip'], 'bytes': t['bytes'],
                    'psnr': t['psnr'], 'src': [int(rgba.shape[1]), int(rgba.shape[0])]}]
    else:
        m = model.load(name, data, resolve)
        m, (centre, scale) = model.prepare(m)
        if m.get('dropped_degenerate'):
            lost.append('%d degenerate triangles dropped' % m['dropped_degenerate'])
        if not m.get('has_normals', True):
            lost.append('normals made (smooth by position): the file had none')
        texs, texinfo = [], []
        for i, im in enumerate(m['textures']):
            t = pvrtex.encode(im, fmt if fmt in ('auto', 'rgb565', 'argb1555', 'argb4444', 'vq') else 'auto', mips,
                              max_size=max_tex, seed=seed)
            lost += ['texture %s: %s' % (m['names'][i], s) for s in t['lost']]
            texs.append(t)
            texinfo.append({'name': m['names'][i], 'w': t['w'], 'h': t['h'], 'format': t['fmt'], 'mip': t['mip'],
                            'bytes': t['bytes'], 'psnr': t['psnr'], 'src': [int(im.shape[1]), int(im.shape[0])]})
        strips, vlist = [], []
        first = 0
        for ti in sorted(set(int(x) for x in m['tex'])):
            sel = m['tris'][m['tex'] == ti]
            for s in model.stripify(sel):
                strips.append((first, len(s), ti))
                vlist.extend(s)
                first += len(s)
        idx = np.array(vlist, np.int64)
        v = np.zeros(len(idx), VERT)
        v['x'], v['y'], v['z'] = m['pos'][idx, 0], m['pos'][idx, 1], m['pos'][idx, 2]
        n = np.clip(np.rint(m['nrm'][idx] * 127), -127, 127).astype(np.int8)
        v['nx'], v['ny'], v['nz'] = n[:, 0], n[:, 1], n[:, 2]
        v['u'], v['v'] = m['uv'][idx, 0], m['uv'][idx, 1]
        c = m['col'][idx].astype(np.uint32)
        v['argb'] = c[:, 3] << 24 | c[:, 0] << 16 | c[:, 1] << 8 | c[:, 2]
        out = pack(KIND_MODEL, texs, strips, v, sha, centre, scale)
        tris, strips_n, sverts = len(m['tris']), len(strips), len(v)
        lost.append('positions normalised: centre %s, radius %.4g scaled to 1 (the shared camera frames radius 1)'
                    % ([round(float(x), 4) for x in centre], float(scale)))
        lost.append('normals to 8 bits a component')
        texw, texh = (texs[0]['w'], texs[0]['h']) if texs else (0, 0)
        psnr = min((t['psnr'] for t in texs), default=None)
        note = '%d triangles, %d strips, %d strip vertices%s' % (tris, strips_n, sverts,
               ', %d texture%s' % (len(texs), 's' if len(texs) > 1 else '') if texs else ', no texture')
    vram = sum(_align(len(t['data'])) for t in parse(out)['textures'])
    fits, reason = budget(len(out), vram, sverts)
    rec = {'name': name, 'kind': kind, 'bytes': len(out), 'source_bytes': len(data), 'sha256': sha.hex(),
           'dqa_sha256': hashlib.sha256(out).hexdigest(), 'tris': tris, 'strips': strips_n, 'stripVerts': sverts,
           'texW': texw, 'texH': texh, 'textures': texinfo, 'ram': len(out), 'vram': vram, 'fits': fits,
           'reason': reason, 'state': 'checked' if fits else 'refused', 'lost': lost, 'psnr': psnr,
           'note': note if fits else 'over budget, not sent', 'budget': {'ram': BLOCK_BYTES, 'vram': VRAM_BUDGET,
                                                                         'vbuf': VBUF_BYTES}}
    return out, rec
