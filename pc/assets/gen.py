"""gen.py: generated test assets (no game data, no downloaded art): a test card picture with and without alpha, a
torus OBJ with its material and checker texture, a one-sided flag OBJ (culling), and a glTF cube with an embedded
buffer and picture. `python -m pc.assets.gen <folder>` writes them all."""
import base64, json, math, os, struct, sys

import numpy as np

from . import image


def test_card(w=256, h=256, alpha=False):
    y, x = np.mgrid[0:h, 0:w]
    im = np.zeros((h, w, 4), np.uint8)
    im[..., 0] = (x * 255 // max(1, w - 1)).astype(np.uint8)
    im[..., 1] = (y * 255 // max(1, h - 1)).astype(np.uint8)
    im[..., 2] = np.where(((x // 16) + (y // 16)) % 2 == 0, 230, 40).astype(np.uint8)
    # a few hard-edged shapes: a white ring, a black bar, colour squares
    r = np.hypot(x - w / 2, y - h / 2)
    ring = (r > w * 0.30) & (r < w * 0.34)
    im[ring, :3] = 255
    im[(y > h * 0.80) & (y < h * 0.86) & (x > w * 0.1) & (x < w * 0.9), :3] = 0
    for i, c in enumerate([(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]):
        im[int(h * 0.08):int(h * 0.18), int(w * (0.1 + 0.2 * i)):int(w * (0.2 + 0.2 * i)), :3] = c
    im[..., 3] = 255
    if alpha:
        im[..., 3] = np.clip(255 - r * 255 / (w * 0.5), 0, 255).astype(np.uint8)
    return im


def checker(n=128, cells=8, a=(235, 235, 235), b=(214, 96, 40)):
    y, x = np.mgrid[0:n, 0:n]
    k = ((x * cells // n) + (y * cells // n)) % 2
    im = np.zeros((n, n, 4), np.uint8)
    im[..., :3] = np.where(k[..., None] == 0, np.array(a, np.uint8), np.array(b, np.uint8))
    im[..., 3] = 255
    return im


def torus_obj(R=1.0, r=0.38, nu=48, nv=24, tex='torus_checker.png'):
    v, vt, vn, f = [], [], [], []
    for i in range(nu + 1):
        for j in range(nv + 1):
            u, w = i / nu * 2 * math.pi, j / nv * 2 * math.pi
            cu, su, cv, sv = math.cos(u), math.sin(u), math.cos(w), math.sin(w)
            v.append(((R + r * cv) * cu, r * sv, (R + r * cv) * su))
            vn.append((cv * cu, sv, cv * su))
            vt.append((i / nu * 4, 1 - j / nv * 2))
    for i in range(nu):
        for j in range(nv):
            a = i * (nv + 1) + j + 1
            b = a + nv + 1
            # outward-facing counter-clockwise (right-handed)
            f.append((a, a + 1, b))
            f.append((b, a + 1, b + 1))
    lines = ['# dc-qalink generated test torus', 'mtllib torus.mtl', 'usemtl checker']
    lines += ['v %.6f %.6f %.6f' % p for p in v] + ['vt %.6f %.6f' % t for t in vt] + ['vn %.6f %.6f %.6f' % n for n in vn]
    lines += ['f %d/%d/%d %d/%d/%d %d/%d/%d' % (a, a, a, b, b, b, c, c, c) for a, b, c in f]
    mtl = 'newmtl checker\nKd 1.0 1.0 1.0\nmap_Kd %s\n' % tex
    return '\n'.join(lines) + '\n', mtl


def flag_obj():
    """A one-sided square facing +z (towards a camera at yaw 0), split in four colours: culling and orientation."""
    lines = ['# dc-qalink generated one-sided flag', 'mtllib flag.mtl']
    pts = [(-0.7, -0.5), (0, -0.5), (0.7, -0.5), (-0.7, 0), (0, 0), (0.7, 0), (-0.7, 0.5), (0, 0.5), (0.7, 0.5)]
    lines += ['v %.3f %.3f 0' % p for p in pts] + ['vn 0 0 1']
    mats = ['red', 'green', 'blue', 'yellow']
    quads = [(1, 2, 5, 4), (2, 3, 6, 5), (4, 5, 8, 7), (5, 6, 9, 8)]
    for m, (a, b, c, d) in zip(mats, quads):
        lines += ['usemtl %s' % m, 'f %d//1 %d//1 %d//1 %d//1' % (a, b, c, d)]
    mtl = ''.join('newmtl %s\nKd %s\n' % (m, k) for m, k in zip(mats, ['1 0 0', '0 1 0', '0 0 1', '1 1 0']))
    return '\n'.join(lines) + '\n', mtl


def cube_gltf(tex_png):
    """A unit cube, 24 vertices with normals and UVs, one material with an embedded picture (data URIs)."""
    faces = [((1, 0, 0), (0, 0, -1), (0, 1, 0)), ((-1, 0, 0), (0, 0, 1), (0, 1, 0)), ((0, 1, 0), (1, 0, 0), (0, 0, -1)),
             ((0, -1, 0), (1, 0, 0), (0, 0, 1)), ((0, 0, 1), (1, 0, 0), (0, 1, 0)), ((0, 0, -1), (-1, 0, 0), (0, 1, 0))]
    P, N, T, I = [], [], [], []
    for n, uax, vax in faces:
        n, uax, vax = np.array(n, float), np.array(uax, float), np.array(vax, float)
        base = len(P)
        for su, sv in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            P.append(0.5 * (n + su * uax + sv * vax))
            N.append(n)
            T.append(((su + 1) / 2, 1 - (sv + 1) / 2))
        I += [base, base + 1, base + 2, base, base + 2, base + 3]
    pos = np.array(P, np.float32).tobytes()
    nrm = np.array(N, np.float32).tobytes()
    uv = np.array(T, np.float32).tobytes()
    idx = np.array(I, np.uint16).tobytes()
    buf = pos + nrm + uv + idx
    js = {'asset': {'version': '2.0', 'generator': 'dc-qalink gen.py'}, 'scene': 0, 'scenes': [{'nodes': [0]}],
          'nodes': [{'mesh': 0, 'rotation': [0, 0.1305262, 0, 0.9914449]}],
          'meshes': [{'primitives': [{'attributes': {'POSITION': 0, 'NORMAL': 1, 'TEXCOORD_0': 2}, 'indices': 3,
                                      'material': 0}]}],
          'materials': [{'pbrMetallicRoughness': {'baseColorTexture': {'index': 0}, 'baseColorFactor': [1, 1, 1, 1]}}],
          'textures': [{'source': 0}],
          'images': [{'uri': 'data:image/png;base64,' + base64.b64encode(tex_png).decode('ascii'), 'name': 'cube.png'}],
          'buffers': [{'byteLength': len(buf), 'uri': 'data:application/octet-stream;base64,' + base64.b64encode(buf).decode('ascii')}],
          'bufferViews': [{'buffer': 0, 'byteOffset': 0, 'byteLength': len(pos)},
                          {'buffer': 0, 'byteOffset': len(pos), 'byteLength': len(nrm)},
                          {'buffer': 0, 'byteOffset': len(pos) + len(nrm), 'byteLength': len(uv)},
                          {'buffer': 0, 'byteOffset': len(pos) + len(nrm) + len(uv), 'byteLength': len(idx)}],
          'accessors': [{'bufferView': 0, 'componentType': 5126, 'count': 24, 'type': 'VEC3',
                         'min': [-0.5, -0.5, -0.5], 'max': [0.5, 0.5, 0.5]},
                        {'bufferView': 1, 'componentType': 5126, 'count': 24, 'type': 'VEC3'},
                        {'bufferView': 2, 'componentType': 5126, 'count': 24, 'type': 'VEC2'},
                        {'bufferView': 3, 'componentType': 5123, 'count': 36, 'type': 'SCALAR'}]}
    return json.dumps(js).encode('utf-8')


def write_all(folder):
    os.makedirs(folder, exist_ok=True)
    out = {}

    def put(name, data):
        p = os.path.join(folder, name)
        with open(p, 'wb') as f:
            f.write(data)
        out[name] = p
    put('card_256.png', image.write_png(None, test_card()))
    put('card_alpha_256.png', image.write_png(None, test_card(alpha=True)))
    put('card_200x120.png', image.write_png(None, test_card(200, 120)))
    put('torus_checker.png', image.write_png(None, checker()))
    o, m = torus_obj()
    put('torus.obj', o.encode())
    put('torus.mtl', m.encode())
    o, m = flag_obj()
    put('flag.obj', o.encode())
    put('flag.mtl', m.encode())
    put('cube.gltf', cube_gltf(image.write_png(None, checker(64, 4, (240, 240, 240), (40, 90, 200)))))
    return out


if __name__ == '__main__':
    for k, v in write_all(sys.argv[1] if len(sys.argv) > 1 else 'gen-assets').items():
        print(k, v)
