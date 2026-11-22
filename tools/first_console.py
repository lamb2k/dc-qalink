#!/usr/bin/env python3
"""first_console.py: the first run of dc-qalink's control loop on a real Dreamcast with a broadband adapter, as one
command and a plain pass/fail table (dc-qalink 0.2). Python 3 standard library; drives pc/dcql_loop.py.

The console runs the loader disc (its LINK.TXT names this PC, or `host peer`); this PC and the console are on one
network. Nothing here touches a firewall: Windows may ask once whether Python may use private networks.

  python tools/first_console.py --program testprog.elf [--console 192.168.1.23] [--out first-run] [--beacon-wait 300]

Steps, each a row of the table (ql-core2: the "ret" row first, every program run with diag=1 so each step is on the
TV for about a second; the screen colour while the return stub runs: blue entered, cyan quiet, magenta GDEMU image,
yellow disc init, violet the drive's own read, orange read, green checked, white jump, red reboot; before a reboot
the stub shows red and three rows of bars for about 10 s: tall = 1, short = 0, four to a hex digit; and with
--console each run row also prints the record of the way back the loader carries in its beacon, ql-core3):
  1 beacon     wait for the loader's HELLO (its address is learnt from it unless --console names it)
  2 ret        the test program returns at once without its link: exec and the way back alone
  3 push+run   send the client test program, verify it, run it, read its report back (5 lines over the link)
  4 screen x3  the same program grabs the console's screen 3 times; the PNGs land in --out
  5 reset      the program waits; the PC sends RESET; the console must come back to the loader by the return stub
  6 back       the loader's beacon after all of it (the console never left the GDEMU slot)
  --switch adds 7 disc switch: the program hands in DONE, selects the loader's GDEMU image and reboots through the
  BIOS (the fallback of every way back); the loader must come back with that run's report.
Exit code 0 when every row passes. Numbers are console numbers only when run on a console.
"""
import argparse, glob, json, os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
LOOP = os.path.normpath(os.path.join(HERE, '..', 'pc', 'dcql_loop.py'))

RUNG_STAGE = {1: 'drive not ready', 2: 'no way back recorded', 3: 'read failed', 4: 'SHA-256 mismatch'}
STEP_NAME = {1: 'entered', 2: 'quiet', 3: 'GD init', 4: 'read', 5: 'checked', 6: 'jump', 7: 'reboot', 8: 'GDEMU select',
             9: 'drive read'}


def way_back(console, port, wait_s):
    """Listen (send nothing) for the loader's HELLO from `console` and decode the last way back's record it carries
    (include/dcql_loop.h CL_HELLO_WB, ql-core3). None when no such beacon came in wait_s."""
    import socket, struct
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(('', port))
    except OSError:
        return None
    s.settimeout(1.0)
    t_end = time.time() + wait_s
    try:
        while time.time() < t_end:
            try:
                d, src = s.recvfrom(2048)
            except socket.timeout:
                continue
            if src[0] != console or len(d) < 16 + 156 + 4 or d[:2] != b'CL' or d[3] != 0x01:
                continue
            pl = d[16:16 + 156]
            if struct.unpack_from('<I', pl, 100)[0] != 1:   # role LOADER only
                continue
            where, stp, err, aux, run = struct.unpack_from('<5I', pl, 136)
            kept = [n for b, n in ((1, 'hand-off'), (2, 'RAM 8M'), (4, 'sound RAM'), (8, 'video RAM'),
                                   (16, 'hand-off put back from main RAM copy'),
                                   (32, 'hand-off put back from sound RAM copy')) if where & b]
            rungs = []
            for i, name in enumerate(('BIOS', 'drive', 'after select')):
                b = (err >> (8 * i)) & 255
                if b:
                    rungs.append('%s: %s code %d' % (name, RUNG_STAGE.get(b >> 4, 'stage %d' % (b >> 4)), b & 15))
            sel = err >> 24
            text = ('run %08x step %d (%s), %s; select %s; BIOS answer err1 %d err2 %d after %d CMD_INIT tries; kept by %s'
                    % (run, stp, STEP_NAME.get(stp, '?'), '; '.join(rungs) or 'no rung failed',
                       'not tried' if not sel else 'taken' if sel == 0x10 else 'refused code %d' % (sel & 15),
                       aux & 255, (aux >> 8) & 255, aux >> 24, ', '.join(kept) or 'nothing (no record survived)'))
            if not where & 15:
                text = 'no record: the loader found none in any place (a cold boot, or nothing survived)'
            return {'where': where, 'step': stp, 'err': err, 'aux': aux, 'run': run, 'text': text}
    finally:
        s.close()
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--program', required=True, help="the client test program (loader/ build: testprog.elf)")
    ap.add_argument('--console', default='auto', help='the console address (default: learnt from its beacon)')
    ap.add_argument('--bind', type=int, default=52060, help='this PC\'s UDP port (the LINK.TXT port; default 52060)')
    ap.add_argument('--out', default='first-run')
    ap.add_argument('--beacon-wait', type=float, default=300.0, help='seconds to wait for the first beacon')
    ap.add_argument('--switch', action='store_true', help='also row 7: the disc switch (GDEMU image, BIOS reboot)')
    ap.add_argument('--no-diag', action='store_true', help='run the program without diag=1 (no 1 s holds)')
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    rows = []

    def step(name, args, timeout, ok_rc=0, out=None):
        cmd = [sys.executable, '-u', LOOP, '--udp', a.console, '--bind', str(a.bind)] + args
        if out:
            cmd += ['--out', os.path.join(a.out, out)]
        t0 = time.time()
        log = os.path.join(a.out, '%s.log' % name.split()[0])
        with open(log, 'w') as f:
            try:
                rc = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, timeout=timeout).returncode
            except subprocess.TimeoutExpired:
                rc = 'timeout'
        secs = time.time() - t0
        text = open(log, encoding='latin-1').read()
        rows.append({'step': name, 'pass': rc == ok_rc, 'rc': rc, 'seconds': round(secs, 1), 'log': log})
        print('%-12s %-4s %6.1f s  (rc %s, %s)' % (name, 'PASS' if rc == ok_rc else 'FAIL', secs, rc, log), flush=True)
        if out and a.console != 'auto':   # the loader's own record of how the way back ended (listen only)
            wb = way_back(a.console, a.bind, 75.0)
            rows[-1]['way_back'] = wb
            print('             way back: %s' % (wb['text'] if wb else 'no beacon with a record in 75 s'), flush=True)
        return rc == ok_rc, text

    print('first console run: %s; waiting up to %.0f s for the loader\'s beacon' % (
        'the console that beacons first' if a.console == 'auto' else a.console, a.beacon_wait), flush=True)
    ok, text = False, ''
    t_end = time.time() + a.beacon_wait
    while not ok and time.time() < t_end:   # status waits 60 s itself; ask again until the wait is spent
        ok, text = step('1 beacon', ['status'], 90)
        if not ok:
            rows.pop()
    if not ok:
        rows.append({'step': '1 beacon', 'pass': False, 'rc': 2, 'seconds': a.beacon_wait, 'log': '-'})
        print('1 beacon     FAIL: no beacon in %.0f s. Check: the adapter\'s cable and link light, the IP line on the '
              'TV (same network as this PC?), the PC line (this PC\'s address?), Windows\' private-network prompt.'
              % a.beacon_wait)
    else:
        dg = '' if a.no_diag else ' diag=1'
        step('2 ret', ['run', a.program, '--args', 'ret' + dg, '--deadline', '60', '--wait', '120'], 180, out='ret')
        ok2, _ = step('3 push+run', ['run', a.program, '--args', 'report lines:5' + dg, '--deadline', '120', '--wait',
                                     '180'], 240, out='run')
        ok3, _ = step('4 screen x3', ['run', a.program, '--args', 'report screen:3', '--deadline', '120', '--wait',
                                      '150'], 210, out='screen')
        pngs = sorted(glob.glob(os.path.join(a.out, 'screen', 'still-*.png')))
        if ok3 and len(pngs) != 3:
            rows[-1]['pass'] = False
            rows[-1]['detail'] = '%d screen grabs arrived, 3 expected' % len(pngs)
        ok4, t4 = step('5 reset', ['run', a.program, '--args', 'waitreset', '--deadline', '120', '--reset-after', '15',
                                   '--reset-wait', '30'], 180, out='reset')
        if ok4 and 'back to the loader' not in t4:
            rows[-1]['pass'] = False
            rows[-1]['detail'] = 'no "back to the loader" after RESET'
        step('6 back', ['status'], 90)
        if a.switch:
            step('7 disc switch', ['run', a.program, '--args', 'gdreboot' + dg, '--deadline', '60', '--wait', '120'],
                 180, out='switch')
    print('\n%-12s %-5s %8s  %s' % ('step', 'result', 'seconds', 'detail'))
    for r in rows:
        print('%-12s %-5s %8.1f  %s' % (r['step'], 'PASS' if r['pass'] else 'FAIL', r['seconds'],
                                        r.get('detail', 'rc %s' % r['rc'])))
    allok = rows and all(r['pass'] for r in rows) and len(rows) == (7 if a.switch else 6)
    print('\n%s' % ('ALL PASS: push, run, report, screen grabs and RESET work on this console' if allok else
                    'NOT ALL PASS: the logs are in %s' % os.path.abspath(a.out)))
    with open(os.path.join(a.out, 'first-run.json'), 'w') as f:
        json.dump({'rows': rows, 'all_pass': bool(allok), 'at': time.strftime('%Y-%m-%d %H:%M:%S')}, f, indent=1)
    return 0 if allok else 1


if __name__ == '__main__':
    sys.exit(main())
