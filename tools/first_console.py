#!/usr/bin/env python3
"""first_console.py: the first run of dc-qalink's control loop on a real Dreamcast with a broadband adapter, as one
command and a plain pass/fail table (dc-qalink 0.2). Python 3 standard library; drives pc/dcql_loop.py.

The console runs the loader disc (its LINK.TXT names this PC, or `host peer`); this PC and the console are on one
network. Nothing here touches a firewall: Windows may ask once whether Python may use private networks.

  python tools/first_console.py --program testprog.elf [--console 192.168.1.23] [--out first-run] [--beacon-wait 300]

Steps, each a row of the table:
  1 beacon     wait for the loader's HELLO (its address is learnt from it unless --console names it)
  2 push+run   send the client test program, verify it, run it, read its report back (5 lines over the link)
  3 screen x3  the same program grabs the console's screen 3 times; the PNGs land in --out
  4 reset      the program waits; the PC sends RESET; the console must come back to the loader by the return stub
  5 back       the loader's beacon after all of it (the console never left the GDEMU slot)
Exit code 0 when every row passes. Numbers are console numbers only when run on a console.
"""
import argparse, glob, json, os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
LOOP = os.path.normpath(os.path.join(HERE, '..', 'pc', 'dcql_loop.py'))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--program', required=True, help="the client test program (loader/ build: testprog.elf)")
    ap.add_argument('--console', default='auto', help='the console address (default: learnt from its beacon)')
    ap.add_argument('--bind', type=int, default=52060, help='this PC\'s UDP port (the LINK.TXT port; default 52060)')
    ap.add_argument('--out', default='first-run')
    ap.add_argument('--beacon-wait', type=float, default=300.0, help='seconds to wait for the first beacon')
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
        ok2, _ = step('2 push+run', ['run', a.program, '--args', 'report lines:5', '--deadline', '60', '--wait', '90'],
                      150, out='run')
        ok3, _ = step('3 screen x3', ['run', a.program, '--args', 'report screen:3', '--deadline', '120', '--wait',
                                      '150'], 210, out='screen')
        pngs = sorted(glob.glob(os.path.join(a.out, 'screen', 'still-*.png')))
        if ok3 and len(pngs) != 3:
            rows[-1]['pass'] = False
            rows[-1]['detail'] = '%d screen grabs arrived, 3 expected' % len(pngs)
        ok4, t4 = step('4 reset', ['run', a.program, '--args', 'waitreset', '--deadline', '120', '--reset-after', '5',
                                   '--reset-wait', '20'], 150, out='reset')
        if ok4 and 'back to the loader' not in t4:
            rows[-1]['pass'] = False
            rows[-1]['detail'] = 'no "back to the loader" after RESET'
        step('5 back', ['status'], 90)
    print('\n%-12s %-5s %8s  %s' % ('step', 'result', 'seconds', 'detail'))
    for r in rows:
        print('%-12s %-5s %8.1f  %s' % (r['step'], 'PASS' if r['pass'] else 'FAIL', r['seconds'],
                                        r.get('detail', 'rc %s' % r['rc'])))
    allok = rows and all(r['pass'] for r in rows) and len(rows) == 5
    print('\n%s' % ('ALL PASS: push, run, report, screen grabs and RESET work on this console' if allok else
                    'NOT ALL PASS: the logs are in %s' % os.path.abspath(a.out)))
    with open(os.path.join(a.out, 'first-run.json'), 'w') as f:
        json.dump({'rows': rows, 'all_pass': bool(allok), 'at': time.strftime('%Y-%m-%d %H:%M:%S')}, f, indent=1)
    return 0 if allok else 1


if __name__ == '__main__':
    sys.exit(main())
