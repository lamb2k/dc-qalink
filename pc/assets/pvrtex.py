"""pvrtex.py: pictures to Dreamcast textures in the graphics chip's own form, and back.

Formats: RGB565, ARGB1555, ARGB4444 (twiddled), VQ (256-entry codebook of 2x2 blocks, twiddled indices), PAL4 and
PAL8 (twiddled indices, palette in RGB565 / ARGB4444 / ARGB8888), each with an optional mip chain (square textures).
The memory layout follows the chip: twiddled = bit-interleaved with y in the lowest bit, rectangles as squares side
by side; a mip chain starts with padding and runs from 1x1 up to the full size (16-bit: 1x1 at texel 3, 2x2 at 4,
4x4 at 8, ...; VQ: the codebook, then 1x1 at index 0, 2x2 at 1, 4x4 at 2, 8x8 at 6, ...).

encode() returns the bytes for graphics memory, the palette, the format word and a report of what was lost; decode()
reads those bytes back into RGBA levels (the PC side of the round trip; the console's own picture is the check)."""
import numpy as np

from . import image

# PVR_TXRFMT_* (KallistiOS dc/pvr.h)
VQ_ENABLE = 1 << 30
ARGB1555, RGB565, ARGB4444, PAL4BPP, PAL8BPP = 0 << 27, 1 << 27, 2 << 27, 5 << 27, 6 << 27
NONTWIDDLED = 1 << 26
PAL_ARGB1555, PAL_RGB565, PAL_ARGB4444, PAL_ARGB8888 = 0, 1, 2, 3
FORMATS = ('rgb565', 'argb1555', 'argb4444', 'vq', 'pal4', 'pal8')
MIN_SIZE, MAX_SIZE = 8, 1024


# ------------------------------------------------------------------------------------------------ layout
def twiddle_map(w, h):
    """(h, w) array: the twiddled index of each texel."""
    m = min(w, h)
    ys, xs = np.mgrid[0:h, 0:w]
    sx, sy = xs % m, ys % m
    t = np.zeros((h, w), np.int64)
    bit = 0
    k = 1
    while k < m:
        t |= ((sy & k) != 0).astype(np.int64) << bit
        t |= ((sx & k) != 0).astype(np.int64) << (bit + 1)
        bit += 2
        k <<= 1
    q = (xs // m) if w > h else (ys // m)
    return q * m * m + t


def mip_point(level, bpp):
    """Byte offset of mip level `level` (size 2**level) in a 16/8/4-bit chain (the chip's own table)."""
    texels = 3 + sum(4 ** j for j in range(level))
    return texels * bpp // 8


def vq_mip_point(level):
    """Index offset after the codebook of VQ mip level `level`."""
    return 0 if level == 0 else 1 + sum(4 ** j for j in range(level - 1))


def pow2_levels(n):
    return n.bit_length() - 1


# ------------------------------------------------------------------------------------------------ 16-bit pixels
def to16(rgba, fmt):
    r, g, b, a = (rgba[..., i].astype(np.int32) for i in range(4))
    q = lambda v, bits: (v * ((1 << bits) - 1) + 127) // 255
    if fmt == 'rgb565':
        return (q(r, 5) << 11 | q(g, 6) << 5 | q(b, 5)).astype(np.uint16)
    if fmt == 'argb1555':
        return ((a >= 128).astype(np.int32) << 15 | q(r, 5) << 10 | q(g, 5) << 5 | q(b, 5)).astype(np.uint16)
    if fmt == 'argb4444':
        return (q(a, 4) << 12 | q(r, 4) << 8 | q(g, 4) << 4 | q(b, 4)).astype(np.uint16)
    raise ValueError(fmt)


def from16(v, fmt):
    v = v.astype(np.int32)
    ex = lambda x, bits: (x << (8 - bits)) | (x >> (2 * bits - 8)) if bits >= 4 else x
    out = np.empty(v.shape + (4,), np.uint8)
    if fmt == 'rgb565':
        out[..., 0], out[..., 1], out[..., 2] = ex(v >> 11 & 31, 5), ex(v >> 5 & 63, 6), ex(v & 31, 5)
        out[..., 3] = 255
    elif fmt == 'argb1555':
        out[..., 0], out[..., 1], out[..., 2] = ex(v >> 10 & 31, 5), ex(v >> 5 & 31, 5), ex(v & 31, 5)
        out[..., 3] = np.where(v >> 15 & 1, 255, 0)
    elif fmt == 'argb4444':
        e4 = lambda x: x << 4 | x
        out[..., 0], out[..., 1], out[..., 2], out[..., 3] = e4(v >> 8 & 15), e4(v >> 4 & 15), e4(v & 15), e4(v >> 12 & 15)
    else:
        raise ValueError(fmt)
    return out


def pal_entry(rgba, palfmt):
    if palfmt == PAL_ARGB8888:
        c = rgba.astype(np.uint32)
        return c[..., 3] << 24 | c[..., 0] << 16 | c[..., 1] << 8 | c[..., 2]
    return to16(rgba, {PAL_RGB565: 'rgb565', PAL_ARGB1555: 'argb1555', PAL_ARGB4444: 'argb4444'}[palfmt]).astype(np.uint32)


def pal_decode(entries, palfmt):
    e = np.asarray(entries, np.uint32)
    if palfmt == PAL_ARGB8888:
        return np.stack([(e >> 16) & 255, (e >> 8) & 255, e & 255, e >> 24], -1).astype(np.uint8)
    return from16(e.astype(np.uint16), {PAL_RGB565: 'rgb565', PAL_ARGB1555: 'argb1555', PAL_ARGB4444: 'argb4444'}[palfmt])


# ------------------------------------------------------------------------------------------------ helpers
def alpha_kind(rgba):
    a = rgba[..., 3]
    if a.min() >= 250:
        return 0
    return 1 if np.all((a < 6) | (a > 249)) else 2


def mip_chain(rgba):
    """Full size first, down to 1x1, by 2x2 averages."""
    out = [rgba]
    cur = rgba.astype(np.float32)
    while cur.shape[0] > 1:
        cur = (cur[0::2, 0::2] + cur[1::2, 0::2] + cur[0::2, 1::2] + cur[1::2, 1::2]) / 4.0
        out.append(np.clip(np.rint(cur), 0, 255).astype(np.uint8))
    return out


def kmeans(x, k, iters=10, seed=1):
    """Codebook (k, d) and labels for vectors x (n, d), float32. Deterministic."""
    rng = np.random.default_rng(seed)
    u = np.unique(x, axis=0)
    if len(u) <= k:
        cb = np.zeros((k, x.shape[1]), np.float32)
        cb[:len(u)] = u
        return cb, assign(x, cb[:len(u)]), len(u)
    cb = u[rng.choice(len(u), k, replace=False)].astype(np.float32)
    for _ in range(iters):
        lab = assign(x, cb)
        sums = np.zeros_like(cb)
        np.add.at(sums, lab, x)
        cnt = np.bincount(lab, minlength=k).astype(np.float32)
        empty = cnt == 0
        cb[~empty] = sums[~empty] / cnt[~empty, None]
        if empty.any():   # re-seed empty cells with the worst-served vectors
            d = ((x - cb[lab]) ** 2).sum(1)
            worst = np.argsort(-d)[:int(empty.sum())]
            cb[empty] = x[worst]
    return cb, assign(x, cb), k


def assign(x, cb, chunk=16384):
    cn = (cb ** 2).sum(1)
    out = np.empty(len(x), np.int64)
    for i in range(0, len(x), chunk):
        xs = x[i:i + chunk]
        out[i:i + chunk] = np.argmin(cn[None, :] - 2.0 * xs @ cb.T, axis=1)
    return out


def psnr(a, b):
    mse = float(np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2))
    return 99.0 if mse == 0 else round(10 * np.log10(255.0 ** 2 / mse), 2)


def fit_size(rgba, lost, square=False, max_size=MAX_SIZE):
    """Power-of-two sides between 8 and max_size (nearest power, down when equally near)."""
    h, w = rgba.shape[:2]
    p = lambda n: max(MIN_SIZE, min(max_size, 1 << max(0, int(round(np.log2(max(1, n)))))))
    nw, nh = p(w), p(h)
    if square and nw != nh:
        nw = nh = max(nw, nh)
        lost.append('mip maps need a square texture: %dx%d made %dx%d' % (w, h, nw, nh))
    if (nw, nh) != (w, h):
        lost.append('size %dx%d resized to %dx%d (the chip takes power-of-two sides, 8 to %d)' % (w, h, nw, nh, max_size))
        rgba = image.resize(rgba, nw, nh)
    return rgba


# ------------------------------------------------------------------------------------------------ encode
def encode(rgba, fmt='auto', mips=True, palfmt=None, pal_base=0, max_size=MAX_SIZE, seed=1):
    """A picture (h, w, 4) uint8 to the chip's form. Returns a dict: data (bytes for graphics memory), pal (list of u32
    entries, already in palfmt), palfmt, txrfmt (the polygon header's format word, palette bank included), w, h, mip,
    alpha (0 none, 1 one bit, 2 full), fmt, lost (list of plain sentences), psnr (level 0 against the input after
    resizing), src_psnr (against the input as given, when the size did not change)."""
    lost = []
    src = rgba
    ak = alpha_kind(rgba)
    if fmt == 'auto':
        fmt = ('rgb565', 'argb1555', 'argb4444')[ak]
    if fmt not in FORMATS:
        raise ValueError('format %s: one of %s' % (fmt, ', '.join(FORMATS)))
    rgba = fit_size(rgba, lost, square=mips, max_size=max_size)
    h, w = rgba.shape[:2]
    levels = mip_chain(rgba) if mips else [rgba]
    levels_small_first = levels[::-1]
    if fmt == 'rgb565' and ak:
        lost.append('alpha dropped (RGB565 has none)')
    if fmt == 'argb1555' and ak == 2:
        lost.append('alpha cut to one bit (ARGB1555)')
    depth = {'rgb565': 'colour to 5-6-5 bits', 'argb1555': 'colour to 5 bits a channel', 'argb4444': 'colour and alpha to 4 bits a channel'}
    out = {'w': w, 'h': h, 'mip': bool(mips), 'alpha': 0 if fmt == 'rgb565' else ak, 'fmt': fmt, 'pal': [], 'palfmt': None}
    if fmt in ('rgb565', 'argb1555', 'argb4444'):
        lost.append(depth[fmt])
        word = {'rgb565': RGB565, 'argb1555': ARGB1555, 'argb4444': ARGB4444}[fmt]
        if mips:
            total = mip_point(pow2_levels(w), 16) + w * h * 2
            buf = bytearray(total)
            for lv, im in enumerate(levels_small_first):
                s = im.shape[0]
                tw = np.zeros(s * s, np.uint16)
                tw[twiddle_map(s, s).ravel()] = to16(im, fmt).ravel()
                o = mip_point(lv, 16)
                buf[o:o + s * s * 2] = tw.tobytes()
            data = bytes(buf)
        else:
            tw = np.zeros(w * h, np.uint16)
            tw[twiddle_map(w, h).ravel()] = to16(rgba, fmt).ravel()
            data = tw.tobytes()
        out.update(data=data, txrfmt=word)
    elif fmt == 'vq':
        cfmt = ('rgb565', 'argb1555', 'argb4444')[ak]
        blocks = []
        for im in levels:
            if im.shape[0] >= 2 and im.shape[1] >= 2:
                s = im.astype(np.float32)
                blocks.append(np.concatenate([s[0::2, 0::2], s[1::2, 0::2], s[0::2, 1::2], s[1::2, 1::2]], -1).reshape(-1, 16))
        x = np.concatenate(blocks)
        if not ak:
            x = x.reshape(-1, 4, 4)[:, :, :3].reshape(-1, 12)
        cb, _, used = kmeans(x, 256, seed=seed)
        cbf = cb.reshape(256, 4, -1)
        if not ak:
            cbf = np.concatenate([cbf, np.full((256, 4, 1), 255, np.float32)], -1)
        cb16 = to16(np.clip(np.rint(cbf), 0, 255).astype(np.uint8), cfmt)        # (256, 4)
        dec = from16(cb16, cfmt).astype(np.float32)                              # what the chip will show
        dflat = dec.reshape(256, 16) if ak else dec[:, :, :3].reshape(256, 12)
        codebook = cb16.astype('<u2').tobytes()
        lost.append('VQ: 2x2 blocks to %d codewords (%s)' % (used, depth[cfmt]))

        def indices(im):
            s = im.astype(np.float32)
            bl = np.concatenate([s[0::2, 0::2], s[1::2, 0::2], s[0::2, 1::2], s[1::2, 1::2]], -1)
            bh, bw = bl.shape[:2]
            v = bl.reshape(-1, 16) if ak else bl.reshape(-1, 4, 4)[:, :, :3].reshape(-1, 12)
            lab = assign(v, dflat).astype(np.uint8)
            tw = np.zeros(bh * bw, np.uint8)
            tw[twiddle_map(bw, bh).ravel()] = lab
            return tw.tobytes()
        if mips:
            n = pow2_levels(w)
            buf = bytearray(vq_mip_point(n) + (w // 2) * (h // 2))
            # the 1x1 level: the codeword whose first texel is nearest the 1x1 colour
            one = levels[-1].astype(np.float32).reshape(4)[:4 if ak else 3]
            buf[0] = int(np.argmin(((dec[:, 0, :4 if ak else 3] - one) ** 2).sum(1)))
            for lv in range(1, n + 1):
                ix = indices(levels_small_first[lv])
                o = vq_mip_point(lv)
                buf[o:o + len(ix)] = ix
            data = codebook + bytes(buf)
        else:
            data = codebook + indices(rgba)
        out.update(data=data, txrfmt=VQ_ENABLE | {'rgb565': RGB565, 'argb1555': ARGB1555, 'argb4444': ARGB4444}[cfmt],
                   alpha=ak)
    else:   # pal4 / pal8
        ncol = 16 if fmt == 'pal4' else 256
        if palfmt is None:
            palfmt = PAL_ARGB4444 if ak else PAL_RGB565
        allpx = np.concatenate([im.reshape(-1, 4) for im in levels]).astype(np.float32)
        cb, _, used = kmeans(allpx if ak else allpx[:, :3], ncol, seed=seed)
        if not ak:
            cb = np.concatenate([cb, np.full((ncol, 1), 255, np.float32)], 1)
        cbq = np.clip(np.rint(cb), 0, 255).astype(np.uint8)
        pal = pal_entry(cbq, palfmt)
        dec = pal_decode(pal, palfmt).astype(np.float32)
        dsel = dec if ak else dec[:, :3]
        lost.append('%d colours from a palette of %d (%s)' % (used, ncol, {PAL_RGB565: 'RGB565', PAL_ARGB1555: 'ARGB1555',
                    PAL_ARGB4444: 'ARGB4444', PAL_ARGB8888: 'ARGB8888'}[palfmt]))

        def pack(im):
            s = im.shape
            v = im.reshape(-1, 4).astype(np.float32)
            lab = assign(v if ak else v[:, :3], dsel).astype(np.uint8)
            tw = np.zeros(s[0] * s[1], np.uint8)
            tw[twiddle_map(s[1], s[0]).ravel()] = lab
            if fmt == 'pal8':
                return tw.tobytes()
            if len(tw) == 1:
                return bytes([int(tw[0]) | int(tw[0]) << 4])
            return (tw[0::2] | (tw[1::2] << 4)).astype(np.uint8).tobytes()
        bpp = 4 if fmt == 'pal4' else 8
        if mips:
            buf = bytearray(mip_point(pow2_levels(w), bpp) + w * h * bpp // 8)
            for lv, im in enumerate(levels_small_first):
                b = pack(im)
                o = mip_point(lv, bpp)
                buf[o:o + len(b)] = b
            data = bytes(buf)
        else:
            data = pack(rgba)
        if ncol == 16:
            if pal_base % 16:
                raise ValueError('a PAL4 palette starts on a multiple of 16')
            bank = PAL4BPP | (pal_base // 16) << 21
        else:
            if pal_base % 256:
                raise ValueError('a PAL8 palette starts on a multiple of 256')
            bank = PAL8BPP | (pal_base // 256) << 25
        out.update(data=data, txrfmt=bank, pal=[int(v) for v in pal], palfmt=palfmt, pal_base=pal_base)
    lv0 = decode(out)[0]
    out['psnr'] = psnr(lv0, rgba) if out['alpha'] else psnr(lv0[..., :3], rgba[..., :3])
    out['src_psnr'] = out['psnr'] if src.shape == rgba.shape else None
    out['lost'] = lost
    out['bytes'] = len(out['data'])
    return out


# ------------------------------------------------------------------------------------------------ decode
def decode(t):
    """RGBA levels, full size first, from a texture dict (data, txrfmt, w, h, mip, pal, palfmt) as encode() makes it
    and as the DQA reader returns it."""
    data, word, w, h, mip = t['data'], t['txrfmt'], t['w'], t['h'], t['mip']
    pix = (word >> 27) & 7
    vq = bool(word & VQ_ENABLE)
    sizes = [(w >> k, h >> k) for k in range(pow2_levels(w) + 1)] if mip else [(w, h)]
    out = []
    if vq:
        cfmt = {0: 'argb1555', 1: 'rgb565', 2: 'argb4444'}[pix]
        cb = from16(np.frombuffer(data[:2048], '<u2').reshape(256, 4), cfmt)
        n = pow2_levels(w)
        for lw, lh in sizes:
            if mip:
                lv = pow2_levels(lw)
                o = 2048 + vq_mip_point(lv)
            else:
                o = 2048
            if lw == 1:
                out.append(cb[data[o]][0].reshape(1, 1, 4))
                continue
            bw, bh = lw // 2, lh // 2
            ix = np.frombuffer(data, np.uint8, bw * bh, o)[twiddle_map(bw, bh)]
            blk = cb[ix]                                           # (bh, bw, 4, 4): texels (0,0) (0,1) (1,0) (1,1)
            im = np.empty((lh, lw, 4), np.uint8)
            im[0::2, 0::2], im[1::2, 0::2], im[0::2, 1::2], im[1::2, 1::2] = blk[:, :, 0], blk[:, :, 1], blk[:, :, 2], blk[:, :, 3]
            out.append(im)
        return out
    if pix in (5, 6):
        bpp = 4 if pix == 5 else 8
        base = ((word >> 21) & 63) * 16 if bpp == 4 else ((word >> 25) & 3) * 256
        pal = pal_decode(t['pal'], t['palfmt'])
        if t.get('pal_base') is not None:
            base = 0          # the dict's own palette starts at its pal_base
        for lw, lh in sizes:
            o = mip_point(pow2_levels(lw), bpp) if mip else 0
            if bpp == 8:
                tw = np.frombuffer(data, np.uint8, lw * lh, o)
            else:
                raw = np.frombuffer(data, np.uint8, max(1, lw * lh // 2), o)
                tw = np.empty(max(2, lw * lh), np.uint8)
                tw[0::2], tw[1::2] = raw & 15, raw >> 4
            out.append(pal[(tw[twiddle_map(lw, lh)] + base).clip(0, len(pal) - 1)])
        return out
    fmt = {0: 'argb1555', 1: 'rgb565', 2: 'argb4444'}[pix]
    for lw, lh in sizes:
        o = mip_point(pow2_levels(lw), 16) if mip else 0
        v = np.frombuffer(data, '<u2', lw * lh, o)[twiddle_map(lw, lh)]
        out.append(from16(v, fmt))
    return out
