#!/usr/bin/env python3
"""netdisc_testdata.py: generated test data for the network disc (lane ql-disc). Nothing from any game.

  python tools/netdisc_testdata.py OUT [--iso] [--big MiB]

writes
  OUT/base/NETDISC/...     the disc's files (copy them onto the test disc: the real disc's side), LIST.TXT naming them
  OUT/overlay/NETDISC/...  the developer's changes: BIG.BIN and SMALL2/5 changed, NEW.BIN added, SAME.BIN identical to
                           the base (must still be read from the disc), LIST.TXT naming the view with the overlay on
  OUT/expect.json          every file's sha256 and side
  OUT/base.iso, base.gdi   (--iso) the base as an ISO 9660 image (2048-byte sectors), and as a GD-style .gdi whose
                           data track starts at LBA 45000 (2352-byte raw sectors), for serving a whole image

Also a module: iso_image(files, lba0=0, raw=False) -> bytes (a minimal ISO 9660 level 1 writer)."""
import hashlib, json, os, struct, sys


def stream(seed, n):
    out = bytearray()
    k = 0
    while len(out) < n:
        out += hashlib.sha256(b'%s:%d' % (seed.encode(), k)).digest()
        k += 1
    return bytes(out[:n])


def both16(v):
    return struct.pack('<H', v) + struct.pack('>H', v)


def both32(v):
    return struct.pack('<I', v) + struct.pack('>I', v)


DATE7 = bytes([126, 11, 22, 12, 0, 0, 0])


def dirrec(name, lba, size, is_dir):
    nb = name if isinstance(name, bytes) else name.encode('ascii')
    ln = 33 + len(nb)
    ln += ln & 1
    r = bytes([ln, 0]) + both32(lba) + both32(size) + DATE7 + bytes([2 if is_dir else 0, 0, 0]) + both16(1) + \
        bytes([len(nb)]) + nb
    return r.ljust(ln, b'\0')


def iso_image(files, lba0=0, raw=False, volume='NETDISC_TEST'):
    """files: {"DIR/NAME.EXT": bytes}. One level of directories below the root is enough for the tests. Extents are
    absolute LBAs (lba0 + sector index), as on a GD-ROM's high-density area when lba0 = 45000."""
    dirs = sorted({p.rsplit('/', 1)[0] for p in files if '/' in p})
    sectors = {}
    nxt = 20                                   # 16 PVD, 17 terminator, 18/19 path tables (empty)
    dir_lba = {'': nxt}
    nxt += 1
    for d in dirs:
        dir_lba[d] = nxt
        nxt += 1
    file_lba = {}
    for p in sorted(files):
        file_lba[p] = nxt
        nxt += max(1, (len(files[p]) + 2047) // 2048)
    total = nxt

    def dir_sector(d):
        me, parent = dir_lba[d], dir_lba['']
        recs = [dirrec(b'\0', lba0 + me, 2048, True), dirrec(b'\1', lba0 + parent, 2048, True)]
        if d == '':
            recs += [dirrec(x, lba0 + dir_lba[x], 2048, True) for x in dirs]
            names = [p for p in sorted(files) if '/' not in p]
        else:
            names = [p for p in sorted(files) if p.rsplit('/', 1)[0] == d and '/' in p]
        recs += [dirrec(p.rsplit('/', 1)[-1] + ';1', lba0 + file_lba[p], len(files[p]), False) for p in names]
        b = b''.join(recs)
        assert len(b) <= 2048, 'one sector a directory in this writer'
        return b.ljust(2048, b'\0')
    pvd = bytearray(2048)
    pvd[0] = 1
    pvd[1:6] = b'CD001'
    pvd[6] = 1
    pvd[8:40] = b'DC-QALINK'.ljust(32)
    pvd[40:72] = volume.encode().ljust(32)
    pvd[80:88] = both32(total)
    pvd[120:124] = both16(1)
    pvd[124:128] = both16(1)
    pvd[128:132] = both16(2048)
    pvd[132:140] = both32(10)
    pvd[140:144] = struct.pack('<I', lba0 + 18)
    pvd[148:152] = struct.pack('>I', lba0 + 19)
    pvd[156:190] = dirrec(b'\0', lba0 + dir_lba[''], 2048, True)
    pvd[881] = 1
    sectors[16] = bytes(pvd)
    term = bytearray(2048)
    term[0] = 255
    term[1:6] = b'CD001'
    term[6] = 1
    sectors[17] = bytes(term)
    for d in dir_lba:
        sectors[dir_lba[d]] = dir_sector(d)
    for p, lba in file_lba.items():
        data = files[p]
        for k in range(max(1, (len(data) + 2047) // 2048)):
            sectors[lba + k] = data[k * 2048:(k + 1) * 2048].ljust(2048, b'\0')
    out = bytearray()
    for i in range(total):
        s = sectors.get(i, bytes(2048))
        if raw:   # Mode 1 raw sector: sync, header (MSF + mode), data, EDC/ECC left zero (readers here skip them)
            out += b'\x00' + b'\xff' * 10 + b'\x00' + bytes(3) + b'\x01' + s + bytes(288)
        else:
            out += s
    return bytes(out)


def make(out, iso=False, big_mib=1):
    big = big_mib * 1024 * 1024
    base = {'BIG.BIN': stream('big', big), 'MID.BIN': stream('mid', 256 * 1024 + 123),
            'SAME.BIN': stream('same', 40000)}
    for k in range(8):
        base['SMALL%d.BIN' % k] = stream('small%d' % k, 3000 + 1111 * k)
    over = {'BIG.BIN': stream('big-v2', big), 'SMALL2.BIN': stream('small2-v2', 5222),
            'SMALL5.BIN': stream('small5-v2', 9000), 'NEW.BIN': stream('new', 70000), 'SAME.BIN': base['SAME.BIN']}
    sha = lambda b: hashlib.sha256(b).hexdigest()
    base_list = ''.join('%s %d %s disc\n' % (n, len(b), sha(b)) for n, b in sorted(base.items()))
    base['LIST.TXT'] = base_list.encode()
    view = dict(base)
    view.update(over)
    side = {n: 'pc' if (n in over and over[n] != base.get(n)) else 'disc' for n in view if n != 'LIST.TXT'}
    over_list = ''.join('%s %d %s %s\n' % (n, len(view[n]), sha(view[n]), side[n]) for n in sorted(side))
    over['LIST.TXT'] = over_list.encode()
    for name, files in (('base', base), ('overlay', over)):
        d = os.path.join(out, name, 'NETDISC')
        os.makedirs(d, exist_ok=True)
        for n, b in files.items():
            open(os.path.join(d, n), 'wb').write(b)
    expect = {n: {'size': len(view[n]) if n != 'LIST.TXT' else len(over_list), 'sha256': sha(view[n] if n != 'LIST.TXT'
              else over['LIST.TXT']), 'side': side.get(n, 'pc')} for n in list(side) + ['LIST.TXT']}
    json.dump({'files': expect, 'generator': 'tools/netdisc_testdata.py'}, open(os.path.join(out, 'expect.json'), 'w'),
              indent=1)
    if iso:
        tree = {'NETDISC/' + n: b for n, b in base.items()}
        open(os.path.join(out, 'base.iso'), 'wb').write(iso_image(tree))
        open(os.path.join(out, 'track03.bin'), 'wb').write(iso_image(tree, 45000, raw=True))
        open(os.path.join(out, 'base.gdi'), 'w').write('3\n1 0 4 2352 track01.bin 0\n2 600 0 2352 track02.raw 0\n'
                                                       '3 45000 4 2352 track03.bin 0\n')
    return expect


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if not args:
        sys.exit(__doc__)
    mib = 1
    if '--big' in sys.argv:
        mib = int(sys.argv[sys.argv.index('--big') + 1])
        args = [a for a in args if a != str(mib)]
    e = make(args[0], '--iso' in sys.argv, mib)
    print('netdisc test data in %s: %d files in the view, %d from the PC' % (
        args[0], len(e), sum(1 for v in e.values() if v['side'] == 'pc')))
