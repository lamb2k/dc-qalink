"""image.py: read PNG (standard library) or any format Pillow reads, write PNG, resize. Pictures are numpy uint8
arrays of shape (h, w, 4), RGBA."""
import io, struct, zlib

import numpy as np

try:
    from PIL import Image as _PIL
except ImportError:            # Pillow is optional
    _PIL = None

PIXELS_MAX = 4096 * 4096       # a picture's pixels, checked from its header before anything is inflated or allocated
                               # (audit round 2): 64 MiB as RGBA; Dreamcast textures are at most 1024 x 1024


def _png_rows(raw, w, h, bpp, stride):
    out = np.zeros((h, stride), np.uint8)
    prev = np.zeros(stride, np.int32)
    pos = 0
    for y in range(h):
        ft = raw[pos]
        line = np.frombuffer(raw, np.uint8, stride, pos + 1).astype(np.int32)
        pos += 1 + stride
        if ft == 0:
            cur = line
        elif ft == 2:
            cur = (line + prev) & 255
        else:   # sub, average, paeth need the left byte: one pass over the line
            cur = np.zeros(stride, np.int32)
            for i in range(stride):
                a = cur[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                if ft == 1:
                    p = a
                elif ft == 3:
                    p = (a + b) >> 1
                else:
                    pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                    p = a if pa <= pb and pa <= pc else b if pb <= pc else c
                cur[i] = (line[i] + p) & 255
        out[y] = cur
        prev = cur
    return out


def read_png(data):
    """A non-interlaced 8-bit PNG (grey, grey+alpha, RGB, RGBA, palette) with the standard library and numpy."""
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('not a PNG')
    pos, idat, plte, trns = 8, [], None, None
    w = h = depth = ctype = inter = None
    while pos < len(data):
        n, typ = struct.unpack('>I4s', data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        pos += 12 + n
        if typ == b'IHDR':
            w, h, depth, ctype, _, _, inter = struct.unpack('>IIBBBBB', body)
        elif typ == b'PLTE':
            plte = np.frombuffer(body, np.uint8).reshape(-1, 3)
        elif typ == b'tRNS':
            trns = body
        elif typ == b'IDAT':
            idat.append(body)
        elif typ == b'IEND':
            break
    if depth != 8 or inter:
        raise ValueError('PNG: only 8-bit, non-interlaced without Pillow (depth %s, interlace %s)' % (depth, inter))
    if ctype not in (0, 2, 3, 4, 6):
        raise ValueError('PNG: colour type %s' % ctype)
    if not w or not h or w * h > PIXELS_MAX:
        raise ValueError('PNG: %s x %s pixels refused (at most %d pixels)' % (w, h, PIXELS_MAX))
    ch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    need = h * (1 + w * ch)          # the filtered rows' exact size: the inflate stops there
    z = zlib.decompressobj()
    raw = z.decompress(b''.join(idat), need + 1)
    if len(raw) != need:
        raise ValueError('PNG: image data inflates to %s%d bytes, the header says %d' % (
            'over ' if len(raw) > need else '', len(raw), need))
    px = _png_rows(raw, w, h, ch, w * ch).reshape(h, w, ch)
    out = np.full((h, w, 4), 255, np.uint8)
    if ctype == 3:
        out[..., :3] = plte[px[..., 0]]
        if trns:
            a = np.full(256, 255, np.uint8)
            a[:len(trns)] = np.frombuffer(trns, np.uint8)
            out[..., 3] = a[px[..., 0]]
    elif ctype in (0, 4):
        out[..., 0] = out[..., 1] = out[..., 2] = px[..., 0]
        if ctype == 4:
            out[..., 3] = px[..., 1]
    else:
        out[..., :ch] = px
    return out


def read_image(data):
    """RGBA (h, w, 4) from PNG or JPEG bytes (JPEG and other formats need Pillow)."""
    if _PIL is not None:
        im = _PIL.open(io.BytesIO(data))
        if im.width * im.height > PIXELS_MAX or not im.width or not im.height:   # the header, before decoding
            raise ValueError('picture %d x %d refused (at most %d pixels)' % (im.width, im.height, PIXELS_MAX))
        return np.array(im.convert('RGBA'), np.uint8)
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return read_png(data)
    raise ValueError('this picture needs Pillow (pip install pillow): only PNG is read without it')


def write_png(path_or_none, rgba):
    """PNG bytes of an (h, w, 3|4) uint8 array (standard library); written to path when given."""
    a = np.ascontiguousarray(rgba, np.uint8)
    h, w, ch = a.shape
    raw = b''.join(b'\x00' + a[y].tobytes() for y in range(h))
    chunk = lambda t, b: struct.pack('>I', len(b)) + t + b + struct.pack('>I', zlib.crc32(t + b) & 0xffffffff)
    png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 6 if ch == 4 else 2, 0, 0, 0)) + \
        chunk(b'IDAT', zlib.compress(raw, 6)) + chunk(b'IEND', b'')
    if path_or_none:
        with open(path_or_none, 'wb') as f:
            f.write(png)
    return png


def resize(rgba, w, h):
    """Resized to (h, w): Pillow's Lanczos when installed, else a box filter (down) / nearest (up) with numpy."""
    if rgba.shape[1] == w and rgba.shape[0] == h:
        return rgba
    if _PIL is not None:
        return np.array(_PIL.fromarray(rgba, 'RGBA').resize((w, h), _PIL.LANCZOS), np.uint8)
    ys = (np.arange(h) * rgba.shape[0] // h)
    xs = (np.arange(w) * rgba.shape[1] // w)
    return rgba[ys][:, xs]
