#!/usr/bin/env python3
"""shots.py: pictures and checks for the in-game menu example (lane ql-menu). MIT licence. Needs Pillow; the QR checks
also need zxing-cpp (pip install zxing-cpp), an independent decoder.

  shots.py host <dir>                 host test output: <name>.raw -> <name>.png; QR texts decoded (and the control)
  shots.py serial <serial.txt> <dir>  the console example's serial line (EX_SCRIPT=1): QLSHOT stills -> PNG,
                                      QLMEAS lines -> meas.txt, QLSNAP texts checked against the stills' QR codes
  shots.py diff <a.png> <b.png>       differing pixels (count, and over 8 levels)
Exit 1 if a check fails.
"""
import base64, re, struct, sys
from pathlib import Path
from PIL import Image

FAILS = []


def check(ok, what):
    print(('ok   ' if ok else 'FAIL ') + what)
    if not ok:
        FAILS.append(what)


def rgb565(data):
    px = struct.unpack('<%dH' % (len(data) // 2), data)
    out = bytearray()
    for p in px:
        r, g, b = (p >> 11) & 31, (p >> 5) & 63, p & 31
        out += bytes(((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)))
    return Image.frombytes('RGB', (640, 480), bytes(out))


def decode_qr(img):
    import zxingcpp
    res = zxingcpp.read_barcodes(img)
    return [r.text for r in res if 'QR' in str(r.format)]


def matrix_image(rows, scale=6):
    n = len(rows)
    q = 4
    im = Image.new('L', ((n + 2 * q) * scale,) * 2, 255)
    for r, line in enumerate(rows):
        for c, ch in enumerate(line):
            if ch == '1':
                im.paste(0, ((c + q) * scale, (r + q) * scale, (c + q + 1) * scale, (r + q + 1) * scale))
    return im


def host(d):
    d = Path(d)
    for raw in sorted(d.glob('*.raw')):
        rgb565(raw.read_bytes()).save(raw.with_suffix('.png'))
        print('png', raw.with_suffix('.png').name)
    for t in sorted(d.glob('qr-*.txt'), key=lambda p: int(p.stem[3:])):
        lines = t.read_text().split('\n')
        text, n, rows = lines[0], int(lines[1]), lines[2:2 + int(lines[1])]
        if n == 0:
            continue
        got = decode_qr(matrix_image(rows))
        check(got == [text], 'QR %d bytes, version %d: decodes to the text' % (len(text), (n - 17) // 4))
        # the control: damage a third of the bottom-right data area (beyond what level L corrects): must not decode to the text
        bad = [list(r) for r in rows]
        for r in range(9, n):
            for c in range(9, n):
                if (r * 7 + c * 3) % 3 == 0:
                    bad[r][c] = '0' if bad[r][c] == '1' else '1'
        got = decode_qr(matrix_image([''.join(r) for r in bad]))
        check(got != [text], 'QR %d bytes control (damaged): does not decode to the text (%s)' % (len(text), got or 'nothing'))


def serial(path, d):
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    frame = bytearray(640 * 480 * 2)
    name, meas, snaps, shots = None, [], {}, []
    last = None
    for line in Path(path).read_bytes().decode('latin1').splitlines():
        if line.startswith('QLSHOT '):
            name = line.split()[1]
        elif name and line.startswith('R') and ' ' in line and line[1].isdigit():
            y, b = line[1:].split(' ', 1)
            rle = base64.b64decode(b)
            row = bytearray()
            for i in range(0, len(rle), 3):
                row += bytes(rle[i + 1:i + 3]) * (rle[i] + 1)
            if len(row) != 1280:
                check(False, 'row %s of %s: %d bytes' % (y, name, len(row)))
                continue
            frame[int(y) * 1280:(int(y) + 1) * 1280] = row
        elif line.startswith('QLEND '):
            im = rgb565(bytes(frame))
            im.save(d / (name + '.png'))
            shots.append(name)
            last, name = name, None
        elif line.startswith('QLMEAS') or line.startswith('QLSTART') or line.startswith('QLDONE'):
            meas.append(line)
        elif line.startswith('QLSNAP '):
            snaps[last] = line[7:]
    (d / 'meas.txt').write_text('\n'.join(meas) + '\n')
    print('stills', len(shots), 'clip frames', sum(1 for s in shots if s.startswith('clip-')))
    for k, v in snaps.items():
        if not v:
            continue
        got = decode_qr(Image.open(d / (k + '.png')))
        check(got == [v], 'console still %s: its QR decodes to the text the console reported (%r)' % (k, v))
    check('QLDONE' in ''.join(meas), 'the run finished (QLDONE)')


def diff(a, b):
    A, B = Image.open(a).convert('RGB'), Image.open(b).convert('RGB')
    pa, pb = A.tobytes(), B.tobytes()
    n = over = 0
    for i in range(0, len(pa), 3):
        if pa[i:i + 3] != pb[i:i + 3]:
            n += 1
            if max(abs(pa[i + k] - pb[i + k]) for k in range(3)) > 8:
                over += 1
    print('diff %s %s: %d pixels differ, %d over 8 levels' % (Path(a).name, Path(b).name, n, over))
    return n


if __name__ == '__main__':
    cmd = sys.argv[1]
    if cmd == 'host':
        host(sys.argv[2])
    elif cmd == 'serial':
        serial(sys.argv[2], sys.argv[3])
    elif cmd == 'diff':
        diff(sys.argv[2], sys.argv[3])
    print('%d failed' % len(FAILS))
    sys.exit(1 if FAILS else 0)
