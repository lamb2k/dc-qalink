"""model.py: OBJ and glTF 2.0 (.gltf with its .bin or data URIs, .glb) to one triangle list, normalised for the
shared camera (centred on its bounding box, scaled to a bounding sphere of radius 1), then to triangle strips.

A mesh is a dict: pos (n,3) float32, nrm (n,3), uv (n,2), col (n,4) uint8, tris (m,3) int, tex (m,) int (texture index
per triangle, -1 none), textures (list of RGBA pictures or None), names (texture names)."""
import base64, io, json, os, struct

import numpy as np

from . import image


# ------------------------------------------------------------------------------------------------ OBJ
def read_obj(text, resolve=None):
    """`resolve(name) -> bytes or None` reads files the OBJ names (its .mtl and the textures)."""
    P, T, N = [], [], []
    faces = []                 # (material, [(vi, ti, ni), ...])
    mats, cur = {}, None
    for line in text.splitlines():
        s = line.split('#', 1)[0].split()
        if not s:
            continue
        k = s[0]
        if k == 'v':
            P.append([float(x) for x in s[1:4]])
        elif k == 'vt':
            T.append([float(s[1]), float(s[2]) if len(s) > 2 else 0.0])
        elif k == 'vn':
            N.append([float(x) for x in s[1:4]])
        elif k == 'f':
            ref = []
            for c in s[1:]:
                parts = (c.split('/') + ['', ''])[:3]
                idx = []
                for j, p in enumerate(parts):
                    if not p:
                        idx.append(None)
                        continue
                    i = int(p)
                    n = (len(P), len(T), len(N))[j]
                    idx.append(i - 1 if i > 0 else n + i)
                ref.append(tuple(idx))
            faces.append((cur, ref))
        elif k == 'usemtl':
            cur = s[1] if len(s) > 1 else None
        elif k == 'mtllib' and resolve:
            data = resolve(' '.join(s[1:]))
            if data:
                mats.update(read_mtl(data.decode('utf-8', 'replace')))
    tex_names, textures, mat_tex = [], [], {}
    for name, m in mats.items():
        if m.get('map') and resolve:
            data = resolve(m['map'])
            if data:
                mat_tex[name] = len(textures)
                tex_names.append(m['map'])
                textures.append(image.read_image(data))
    pos, nrm, uv, col, tris, tex = [], [], [], [], [], []
    hasn = bool(N)
    for mat, ref in faces:
        if len(ref) < 3:
            continue
        kd = mats.get(mat, {}).get('kd', (1.0, 1.0, 1.0))
        c = [int(round(255 * max(0.0, min(1.0, x)))) for x in kd] + [255]
        ti = mat_tex.get(mat, -1)
        base = len(pos)
        for vi, tt, ni in ref:
            pos.append(P[vi])
            uv.append(T[tt] if tt is not None and tt < len(T) else [0.0, 0.0])
            nrm.append(N[ni] if ni is not None and ni < len(N) else [0.0, 0.0, 0.0])
            col.append(c)
        for j in range(1, len(ref) - 1):
            tris.append([base, base + j, base + j + 1])
            tex.append(ti)
    m = {'pos': np.array(pos, np.float32).reshape(-1, 3), 'nrm': np.array(nrm, np.float32).reshape(-1, 3),
         'uv': np.array(uv, np.float32).reshape(-1, 2), 'col': np.array(col, np.uint8).reshape(-1, 4),
         'tris': np.array(tris, np.int64).reshape(-1, 3), 'tex': np.array(tex, np.int64), 'textures': textures,
         'names': tex_names, 'has_normals': hasn}
    m['uv'][:, 1] = 1.0 - m['uv'][:, 1]        # OBJ's v runs up; the chip's runs down
    return m


def read_mtl(text):
    mats, cur = {}, None
    for line in text.splitlines():
        s = line.split('#', 1)[0].split()
        if not s:
            continue
        if s[0] == 'newmtl':
            cur = mats.setdefault(' '.join(s[1:]), {})
        elif cur is not None and s[0] == 'Kd':
            cur['kd'] = tuple(float(x) for x in s[1:4])
        elif cur is not None and s[0] == 'map_Kd':
            cur['map'] = s[-1]
    return mats


# ------------------------------------------------------------------------------------------------ glTF
_CT = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}
_NC = {'SCALAR': 1, 'VEC2': 2, 'VEC3': 3, 'VEC4': 4, 'MAT4': 16}


def read_gltf(data, resolve=None):
    if data[:4] == b'glTF':
        _, _, total = struct.unpack_from('<III', data, 0)
        pos, js, binc = 12, None, None
        while pos < total:
            n, typ = struct.unpack_from('<II', data, pos)
            body = data[pos + 8:pos + 8 + n]
            if typ == 0x4E4F534A:
                js = json.loads(body.decode('utf-8'))
            elif typ == 0x004E4942:
                binc = body
            pos += 8 + n
    else:
        js, binc = json.loads(data.decode('utf-8')), None

    def uri_bytes(u):
        if u.startswith('data:'):
            return base64.b64decode(u.split(',', 1)[1])
        b = resolve(u) if resolve else None
        if b is None:
            raise ValueError('glTF names %s, which was not given' % u)
        return b
    buffers = [binc if 'uri' not in b else uri_bytes(b['uri']) for b in js.get('buffers', [])]

    def view(i):
        v = js['bufferViews'][i]
        b = buffers[v['buffer']]
        o = v.get('byteOffset', 0)
        return b[o:o + v['byteLength']], v.get('byteStride')

    def acc(i):
        a = js['accessors'][i]
        dt, nc = _CT[a['componentType']], _NC[a['type']]
        raw, stride = view(a['bufferView'])
        o, cnt, isz = a.get('byteOffset', 0), a['count'], np.dtype(dt).itemsize * nc
        if stride and stride != isz:
            arr = np.stack([np.frombuffer(raw, dt, nc, o + k * stride) for k in range(cnt)])
        else:
            arr = np.frombuffer(raw, dt, cnt * nc, o).reshape(cnt, nc)
        if a.get('normalized') and dt != np.float32:
            arr = arr.astype(np.float32) / float(np.iinfo(dt).max)
        return arr

    textures, names, tex_of_img = [], [], {}

    def texture_of(mat):
        info = ((mat or {}).get('pbrMetallicRoughness') or {}).get('baseColorTexture')
        if not info:
            return -1
        src = js['textures'][info['index']]['source']
        if src not in tex_of_img:
            im = js['images'][src]
            b = view(im['bufferView'])[0] if 'bufferView' in im else uri_bytes(im['uri'])
            tex_of_img[src] = len(textures)
            textures.append(image.read_image(bytes(b)))
            names.append(im.get('name') or im.get('uri', 'image %d' % src)[:60])
        return tex_of_img[src]

    def local(n):
        if 'matrix' in n:
            return np.array(n['matrix'], np.float64).reshape(4, 4).T
        t = np.array(n.get('translation', [0, 0, 0]), np.float64)
        x, y, z, w = n.get('rotation', [0, 0, 0, 1])
        s = np.array(n.get('scale', [1, 1, 1]), np.float64)
        r = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                      [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                      [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
        m = np.eye(4)
        m[:3, :3] = r * s
        m[:3, 3] = t
        return m
    out = {'pos': [], 'nrm': [], 'uv': [], 'col': [], 'tris': [], 'tex': []}
    count = [0]
    hasn = [True]

    def add_mesh(mi, mat4):
        for prim in js['meshes'][mi]['primitives']:
            mode = prim.get('mode', 4)
            if mode not in (4, 5):
                continue
            at = prim['attributes']
            p = acc(at['POSITION']).astype(np.float64)
            n = len(p)
            p = (np.c_[p, np.ones(n)] @ mat4.T)[:, :3]
            if 'NORMAL' in at:
                nr = acc(at['NORMAL']).astype(np.float64) @ np.linalg.inv(mat4[:3, :3])
            else:
                nr = np.zeros((n, 3))
                hasn[0] = False
            uv = acc(at['TEXCOORD_0']).astype(np.float32) if 'TEXCOORD_0' in at else np.zeros((n, 2), np.float32)
            mat = js['materials'][prim['material']] if 'material' in prim else None
            f = ((mat or {}).get('pbrMetallicRoughness') or {}).get('baseColorFactor', [1, 1, 1, 1])
            if 'COLOR_0' in at:
                c = acc(at['COLOR_0']).astype(np.float32)
                if c.max() > 1.0:
                    c = c / 255.0
                if c.shape[1] == 3:
                    c = np.c_[c, np.ones(n)]
            else:
                c = np.ones((n, 4), np.float32)
            c = np.clip(np.rint(c * np.array(f) * 255), 0, 255).astype(np.uint8)
            idx = acc(prim['indices']).ravel().astype(np.int64) if 'indices' in prim else np.arange(n)
            if mode == 5:
                t = [[idx[i], idx[i + 1], idx[i + 2]] if i % 2 == 0 else [idx[i + 1], idx[i], idx[i + 2]]
                     for i in range(len(idx) - 2)]
                idx = np.array(t, np.int64).ravel()
            if np.linalg.det(mat4[:3, :3]) < 0:
                idx = idx.reshape(-1, 3)[:, [0, 2, 1]].ravel()
            tri = idx.reshape(-1, 3) + count[0]
            ti = texture_of(mat)
            out['pos'].append(p); out['nrm'].append(nr); out['uv'].append(uv); out['col'].append(c)
            out['tris'].append(tri); out['tex'].append(np.full(len(tri), ti, np.int64))
            count[0] += n

    def walk(ni, parent):
        nd = js['nodes'][ni]
        m = parent @ local(nd)
        if 'mesh' in nd:
            add_mesh(nd['mesh'], m)
        for c in nd.get('children', []):
            walk(c, m)
    scene = js.get('scenes', [{'nodes': list(range(len(js.get('nodes', []))))}])[js.get('scene', 0)]
    for ni in scene.get('nodes', []):
        walk(ni, np.eye(4))
    if not out['pos']:
        raise ValueError('glTF: no triangle meshes in the scene')
    cat = lambda k, dt: np.concatenate(out[k]).astype(dt)
    return {'pos': cat('pos', np.float32), 'nrm': cat('nrm', np.float32), 'uv': cat('uv', np.float32),
            'col': cat('col', np.uint8), 'tris': cat('tris', np.int64), 'tex': cat('tex', np.int64),
            'textures': textures, 'names': names, 'has_normals': hasn[0]}


# ------------------------------------------------------------------------------------------------ preparation
def prepare(m):
    """Normals where missing (smooth, by position), welded vertices, and the normalisation: centre at the bounding
    box's centre, bounding sphere radius 1. Returns the mesh and (centre, scale)."""
    pos, tris = m['pos'].astype(np.float64), m['tris']
    if len(tris) == 0:
        raise ValueError('the model has no triangles')
    nrm = m['nrm'].astype(np.float64)
    bad = np.linalg.norm(nrm, axis=1) < 1e-6
    if bad.any():
        fn = np.cross(pos[tris[:, 1]] - pos[tris[:, 0]], pos[tris[:, 2]] - pos[tris[:, 0]])
        key = np.unique(np.round(pos, 5), axis=0, return_inverse=True)[1].ravel()
        acc = np.zeros((key.max() + 1, 3))
        for k in range(3):
            np.add.at(acc, key[tris[:, k]], fn)
        nrm[bad] = acc[key[bad]]
    ln = np.linalg.norm(nrm, axis=1, keepdims=True)
    nrm = np.where(ln > 1e-12, nrm / np.maximum(ln, 1e-12), [0.0, 1.0, 0.0])
    lo, hi = pos.min(0), pos.max(0)
    centre = (lo + hi) / 2
    r = float(np.max(np.linalg.norm(pos - centre, axis=1))) or 1.0
    m = dict(m, pos=((pos - centre) / r).astype(np.float32), nrm=nrm.astype(np.float32))
    # weld: identical position, normal, uv, colour become one vertex (strips share them)
    q = np.c_[np.round(m['pos'], 6), np.round(m['nrm'], 4), np.round(m['uv'], 6), m['col']]
    _, first, inv = np.unique(q, axis=0, return_index=True, return_inverse=True)
    inv = inv.ravel()
    m.update(pos=m['pos'][first], nrm=m['nrm'][first], uv=m['uv'][first], col=m['col'][first], tris=inv[m['tris']])
    keep = (m['tris'][:, 0] != m['tris'][:, 1]) & (m['tris'][:, 1] != m['tris'][:, 2]) & (m['tris'][:, 0] != m['tris'][:, 2])
    m['dropped_degenerate'] = int((~keep).sum())
    m['tris'], m['tex'] = m['tris'][keep], m['tex'][keep]
    return m, (centre.astype(np.float32), np.float32(r))


def stripify(tris, max_len=1000):
    """Triangle strips (lists of vertex indices) keeping each triangle's winding: triangle i of a strip is
    (v[i], v[i+1], v[i+2]) for even i and (v[i+1], v[i], v[i+2]) for odd i. Greedy: from each unused triangle, the
    longest of its three starts. Every triangle lands in exactly one strip."""
    tris = [tuple(int(x) for x in t) for t in tris]
    edge = {}
    for i, (a, b, c) in enumerate(tris):
        for e in ((a, b), (b, c), (c, a)):
            edge.setdefault(e, []).append(i)
    used = [False] * len(tris)

    def third(t, p, q):
        a, b, c = tris[t]
        for x, y, z in ((a, b, c), (b, c, a), (c, a, b)):
            if x == p and y == q:
                return z
        return None

    def grow(start, rot):
        a, b, c = tris[start]
        v = [(a, b, c), (b, c, a), (c, a, b)][rot]
        s, taken = list(v), {start}
        while len(s) < max_len:
            p, q = s[-2], s[-1]
            i = len(s) - 2            # the index of the next triangle in the strip
            d = (p, q) if i % 2 == 0 else (q, p)
            nxt = None
            for t in edge.get(d, ()):
                if not used[t] and t not in taken and third(t, *d) is not None:
                    nxt = t
                    break
            if nxt is None:
                break
            s.append(third(nxt, *d))
            taken.add(nxt)
        return s, taken
    strips = []
    for t in range(len(tris)):
        if used[t]:
            continue
        best = max((grow(t, r) for r in range(3)), key=lambda st: len(st[0]))
        for k in best[1]:
            used[k] = True
        strips.append(best[0])
    return strips


def strip_triangles(s):
    return [(s[i], s[i + 1], s[i + 2]) if i % 2 == 0 else (s[i + 1], s[i], s[i + 2]) for i in range(len(s) - 2)]


def load(name, data, resolve=None):
    low = name.lower()
    if low.endswith('.obj'):
        return read_obj(data.decode('utf-8', 'replace'), resolve)
    if low.endswith('.gltf') or low.endswith('.glb'):
        return read_gltf(data, resolve)
    raise ValueError('%s: models are OBJ, glTF or GLB' % name)


DEP_MAX = 16 << 20   # one file a model names (its .mtl, a texture, a .bin), at most


def folder_resolver(folder, root=None, limit=DEP_MAX):
    """Files a model names, read beside it: each target (links followed) must be a file below `root` (default the
    model's own folder) and at most `limit` bytes, or it reads as absent (audit 2026-11-23, item 2)."""
    import qlsafe   # pc/qlsafe.py (pc/ is on the path wherever pc/assets is imported)
    root = root or folder

    def r(rel):
        if not rel or '\0' in rel:
            return None
        p = qlsafe.inside(root, os.path.join(folder, rel.replace('\\', '/')))
        if p is None:
            return None
        try:
            return qlsafe.read_capped(p, limit)
        except (qlsafe.Refused, OSError):
            return None
    return r
