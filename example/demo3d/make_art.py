#!/usr/bin/env python3
"""make_art.py: writes the sample game's art, all of it made here in code (MIT, dc-qalink 0.3). Standard library only.

  disc/DEMO3D/FLOOR.BMP   the floor tile, 128x128, 24-bit (open it in any paint program, draw, save as 24-bit BMP)
  disc/DEMO3D/CRAFT.OBJ   the craft, a few dozen triangles (any 3D tool that saves OBJ can change it)
  art/FLOOR_ALT.BMP       a second tile in other colours: copy it over FLOOR.BMP to see a texture swap on the next push

Run it again only to start the art over: the files are kept in the repository as they are."""
import math
import os
import struct

HERE = os.path.dirname(os.path.abspath(__file__))


def bmp(path, w, h, pix):
    """pix(x, y) -> (r, g, b), y from the top. A plain bottom-up 24-bit BMP."""
    stride = (w * 3 + 3) & ~3
    rows = []
    for y in range(h - 1, -1, -1):
        r = bytearray()
        for x in range(w):
            cr, cg, cb = pix(x, y)
            r += bytes((cb, cg, cr))
        rows.append(bytes(r) + b'\0' * (stride - w * 3))
    data = b''.join(rows)
    head = b'BM' + struct.pack('<IHHI', 54 + len(data), 0, 0, 54) + \
        struct.pack('<IiiHHIIiiII', 40, w, h, 1, 24, 0, len(data), 2835, 2835, 0, 0)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, 'wb').write(head + data)


def tile(base, line, glow):
    def pix(x, y):
        e = min(x, y, 127 - x, 127 - y)                       # distance to the tile's edge
        r = math.hypot(x - 63.5, y - 63.5)
        k = 0.55 + 0.05 * (((x // 8) ^ (y // 8)) & 1)         # a faint inner check
        if e < 3:
            k = 0.25
        elif e < 6:
            k = 0.95
        c = [int(v * k) for v in base]
        if abs(r - 38) < 2.2 or (abs(x - y) < 1.5 and 20 < x < 108) or (abs(x + y - 127) < 1.5 and 20 < x < 108):
            c = list(line)
        if r < 7:
            c = list(glow)
        return tuple(max(0, min(255, v)) for v in c)
    return pix


def craft(path):
    """A delta-wing dart: x right, y up, z forward (the nose at +z). Faces may wind either way: the game turns each
    triangle outward from the model's centre."""
    v = [(0.0, 0.18, 1.35),                          # 1 nose
         (-0.28, 0.32, 0.2), (0.28, 0.32, 0.2),      # 2, 3 canopy shoulders
         (0.0, 0.48, -0.15),                         # 4 canopy top
         (-1.05, 0.02, -0.75), (1.05, 0.02, -0.75),  # 5, 6 wing tips
         (-0.3, -0.12, -0.55), (0.3, -0.12, -0.55),  # 7, 8 belly back
         (0.0, -0.18, 0.35),                         # 9 belly front
         (0.0, 0.75, -0.85),                         # 10 fin top
         (0.0, 0.25, -0.8)]                          # 11 tail
    f = [(1, 2, 3), (2, 4, 3), (1, 5, 2), (1, 3, 6), (2, 5, 11), (3, 11, 6), (2, 11, 4), (3, 4, 11),
         (1, 9, 5), (1, 6, 9), (9, 7, 5), (9, 6, 8), (9, 8, 7), (5, 7, 11), (6, 11, 8), (7, 8, 11),
         (4, 10, 11), (4, 11, 10)]
    with open(path, 'w', newline='\n') as o:
        o.write('# CRAFT.OBJ: dc-qalink sample game (demo3d), made by make_art.py, MIT\n')
        for p in v:
            o.write('v %.3f %.3f %.3f\n' % p)
        for t in f:
            o.write('f %d %d %d\n' % t)


if __name__ == '__main__':
    bmp(os.path.join(HERE, 'disc', 'DEMO3D', 'FLOOR.BMP'), 128, 128, tile((70, 150, 160), (230, 240, 245), (255, 210, 90)))
    bmp(os.path.join(HERE, 'art', 'FLOOR_ALT.BMP'), 128, 128, tile((190, 90, 60), (255, 230, 170), (120, 220, 255)))
    craft(os.path.join(HERE, 'disc', 'DEMO3D', 'CRAFT.OBJ'))
    print('wrote disc/DEMO3D/FLOOR.BMP, disc/DEMO3D/CRAFT.OBJ, art/FLOOR_ALT.BMP')
