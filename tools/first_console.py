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
  --switch adds 8 disc switch: the program ends through the client (its END to the PC), selects the loader's GDEMU
  image and reboots through the BIOS; END must come (ql-core5).
  --hang adds, last (ql-core5): 9 wdt armed (a good program with wdt=1: END, WTCSR read back e7), 10 hang (interrupts
  off with wdt=1: the console must restart by itself; no beacon in 60 s = power-cycle), and when 10 passes 11 after
  (one short run: WTCSR as the BIOS left it, WOVF = the last reset was the watchdog's).
--focus (ql-core6) runs only: 1 beacon, 4a and 4b screen x3 (3 PNGs, "screens 3 of 3 held", END within 30 s),
  9 wdt armed, 10 hang, 11 after.
Exit code 0 when every row passes. Numbers are console numbers only when run on a console.
"""
import argparse, glob, json, os, re, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
LOOP = os.path.normpath(os.path.join(HERE, '..', 'pc', 'dcql_loop.py'))
sys.path.insert(0, os.path.dirname(LOOP))
import dcql_loop as cl   # noqa: E402  (the one HELLO decoder: frame length, CRC and version checked)

RUNG_STAGE = {1: 'drive not ready', 2: 'no way back recorded', 3: 'read failed', 4: 'SHA-256 mismatch'}
STEP_NAME = {1: 'entered', 2: 'quiet', 3: 'GD init', 4: 'read', 5: 'checked', 6: 'jump', 7: 'reboot', 8: 'GDEMU select',
             9: 'drive read'}


def way_back(console, port, wait_s):
    """Listen (send nothing) for the loader's HELLO from `console` and decode the last way back's record it carries
    (include/dcql_loop.h CL_HELLO_STUB and CL_HELLO_WB). None when no such beacon came in wait_s."""
    import socket
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
            if src[0] != console:
                continue
            wb = way_back_record(d)
            if wb is not None:
                return wb
    finally:
        s.close()
    return None


def way_back_record(d):
    """The way-back record of one datagram: a loader's HELLO through dcql_loop.decode_hello (the frame's length, CRC
    and protocol version checked, then the shared parser), with the 164-byte HELLO's way-back words; else None."""
    got = cl.decode_hello(d, want_role=1)
    if got is None or got[0].get('way_back') is None:
        return None
    w = got[0]['way_back']
    stp, err, where, aux, run, sense = w['step'], w['err'], w['where'], w['aux'], w['run'], w['sense']
    kept = [n for b, n in ((1, 'hand-off'), (2, 'RAM 8M'), (4, 'sound RAM'), (8, 'video RAM'),
                           (16, 'hand-off put back from main RAM copy'),
                           (32, 'hand-off put back from sound RAM copy'),
                           (64, 'hand-off put back from the adapter\'s memory')) if where & b]
    if stp in (16, 17):   # ql-core4: the full restart's crumb (no stub)
        text = ('run %08x: %s; kept by %s%s%s' % (
            run, 'the full restart taken' if stp == 17 else 'NO ENDING (watchdog, reset or power)',
            ', '.join(kept) or 'nothing', '; the loader started on the saved address' if where & 256 else '',
            '' if where & 0x70 else ' (no saved copy survived: the crumb only)'))
        return {'where': where, 'step': stp, 'err': err, 'aux': aux, 'run': run, 'text': text}
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
    text += '; drive sense key %d ASC %02x' % (sense & 15, (sense >> 8) & 255)
    if not where & 0x7f:
        text = 'no record: the loader found none in any place (a cold boot, or nothing survived)'
    if where & 256:
        text += '; the loader started on the saved address'
    return {'where': where, 'step': stp, 'err': err, 'aux': aux, 'run': run, 'text': text}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--program', required=True, help="the client test program (loader/ build: testprog.elf)")
    ap.add_argument('--console', default='auto', help='the console address (default: learnt from its beacon)')
    ap.add_argument('--bind', type=int, default=52060, help='this PC\'s UDP port (the LINK.TXT port; default 52060)')
    ap.add_argument('--out', default='first-run')
    ap.add_argument('--beacon-wait', type=float, default=300.0, help='seconds to wait for the first beacon')
    ap.add_argument('--switch', action='store_true', help='also row 7: the disc switch (GDEMU image, BIOS reboot)')
    ap.add_argument('--no-diag', action='store_true', help='run the program without diag=1 (no 1 s holds)')
    ap.add_argument('--stub', action='store_true', help='the loader disc says "return stub": the old rows (the quick '
                    'in-RAM way back must keep the run, so "ret" must hand in its report; plus the drive\'s own read)')
    ap.add_argument('--hang', action='store_true', help='also the watchdog row: the program hangs with interrupts off')
    ap.add_argument('--focus', action='store_true', help='ql-core6 sheet: only beacon, screen x3 twice, wdt armed, '
                    'hang (last) and after')
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    rows = []

    def step(name, args, timeout, ok_rc=0, out=None):
        ok_rcs = ok_rc if isinstance(ok_rc, tuple) else (ok_rc,)
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
        ok = rc in ok_rcs
        rows.append({'step': name, 'pass': ok, 'rc': rc, 'seconds': round(secs, 1), 'log': log})
        for ln in text.splitlines():   # ql-core4: the full restart's own numbers, from dcql_loop's lines
            if 'end to beacon' in ln or 'END from the program' in ln or 'program\'s link up' in ln:
                key = 'end_to_beacon' if 'end to beacon' in ln else 'end' if 'END from' in ln else 'link'
                rows[-1][key] = ln.split(' ', 1)[1] if ' ' in ln else ln
        print('%-12s %-4s %6.1f s  (rc %s, %s)' % (name, 'PASS' if ok else 'FAIL', secs, rc, log), flush=True)
        for key in ('link', 'end', 'end_to_beacon'):
            if key in rows[-1]:
                print('             %s' % rows[-1][key], flush=True)
        if out and a.console != 'auto':   # the loader's own record of how the way back ended (listen only)
            wb = way_back(a.console, a.bind, 75.0)
            rows[-1]['way_back'] = wb
            print('             way back: %s' % (wb['text'] if wb else 'no beacon with a record in 75 s'), flush=True)
        return ok, text

    def report_line(out, key):
        for p in sorted(glob.glob(os.path.join(a.out, out, 'report-*.txt'))):
            for ln in open(p, encoding='latin-1').read().splitlines():
                if ln.startswith(key):
                    return ln
        return None

    def screen_row(name, out):
        """3 grabs of the screen: 3 PNGs, the report's "screens 3 of 3 held", END within 30 s of EXEC (ql-core6: one
        lost head lost a whole grab and held END back 45 s, first-run9 row 4); the cl-still line printed."""
        ok, _ = step(name, ['run', a.program, '--args', 'report screen:3', '--deadline', '120', '--wait', '150'], 210,
                     out=out)
        pngs = sorted(glob.glob(os.path.join(a.out, out, 'still-*.png')))
        held = report_line(out, 'screens ')
        cs = report_line(out, 'cl-still:')
        rows[-1]['still'] = cs
        print('             %s; %s' % (held or 'no "screens" line', cs or 'no cl-still line'), flush=True)
        m = re.search(r'END from the program ([0-9.]+) s after EXEC', rows[-1].get('end', ''))
        end_s = float(m.group(1)) if m else None
        if ok and len(pngs) != 3:
            rows[-1]['pass'] = False
            rows[-1]['detail'] = '%d screen grabs arrived, 3 expected' % len(pngs)
        elif ok and held != 'screens 3 of 3 held':
            rows[-1]['pass'] = False
            rows[-1]['detail'] = 'the program says: %s' % held
        elif ok and (end_s is None or end_s > 30.0):
            rows[-1]['pass'] = False
            rows[-1]['detail'] = 'END %s s after EXEC (30 s at most)' % end_s
        return rows[-1]['pass']

    def watchdog_rows(dg):
        """The watchdog rows, last (a console that does not come back needs a power cycle). ql-core6: the watchdog
        resets 5.25 ms after its last feed (not 21 ms), fed every 1 ms, manual reset: read back e7 when counting."""
        # 9: a good program with the watchdog armed: END as usual (rc 0), WTCSR read back e7 and the clock measured; a
        # restart without END here (rc 4) is a false watchdog reset of a good program
        ok9, _ = step('9 wdt armed', ['run', a.program, '--args', 'report lines:5 wdt=1' + dg, '--deadline', '90',
                                      '--wait', '150'], 210, out='wdtarmed')
        w9, c9 = report_line('wdtarmed', 'cl-wdt:'), report_line('wdtarmed', 'cl-wdt clock:')
        rows[-1]['wdt'], rows[-1]['wdt_clock'] = w9, c9
        print('             %s\n             %s' % (w9 or 'no cl-wdt line in the report', c9 or 'no cl-wdt clock line'),
              flush=True)
        if ok9 and (not w9 or 'after arming e7' not in w9):
            rows[-1]['pass'] = False
            rows[-1]['detail'] = 'the watchdog did not read back as counting (WTCSR e7)'
        elif ok9 and (not c9 or 'measured' not in c9):
            rows[-1]['pass'] = False
            rows[-1]['detail'] = 'the watchdog\'s clock was not measured'
        # 10: interrupts off for good with the watchdog armed: the console must restart by itself, no ending (rc 4, or
        # 5 = LOST from the crumb). No beacon in 60 s after the hang: power-cycle, and note the TV (frozen picture,
        # black, or the logo looping)
        ok10, _ = step('10 hang', ['run', a.program, '--args', 'report lines:2 hangoff wdt=1' + dg, '--deadline', '90',
                                   '--wait', '150'], 210, ok_rc=(4, 5), out='hang')
        if ok10:   # back by itself: one more short run reads WTCSR as the BIOS left it (WOVF = the watchdog's reset)
            step('11 after', ['run', a.program, '--args', 'report lines:1', '--deadline', '60', '--wait', '120'], 180,
                 out='after')
            w11 = report_line('after', 'cl-wdt:')
            rows[-1]['wdt'] = w11
            print('             %s' % (w11 or 'no cl-wdt line in the report'), flush=True)

    dg = '' if a.no_diag else ' diag=1'
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
    elif not a.focus:
        # ql-core4: with the full restart (the default) "ret" has no link, so no END: the console must come back (rc 0
        # when a saved copy survived the reboot, 4 when nothing did); with --stub the stub must keep the run (rc 0)
        step('2 ret', ['run', a.program, '--args', 'ret' + dg, '--deadline', '60', '--wait', '120'], 180,
             ok_rc=0 if a.stub else (0, 4), out='ret')
        if a.stub:
            step('2b stubata', ['run', a.program, '--args', 'stubata' + dg, '--deadline', '60', '--wait', '120'], 180,
                 out='stubata')
        ok2, _ = step('3 push+run', ['run', a.program, '--args', 'report lines:5' + dg, '--deadline', '120', '--wait',
                                     '180'], 240, out='run')
        screen_row('4 screen x3', 'screen')
        ok4, t4 = step('5 reset', ['run', a.program, '--args', 'waitreset', '--deadline', '120', '--reset-after', '15',
                                   '--reset-wait', '30'], 180, out='reset')
        if ok4 and 'back to the loader' not in t4:
            rows[-1]['pass'] = False
            rows[-1]['detail'] = 'no "back to the loader" after RESET'
        # ql-core4: a crash hands in CRASHED (rc 5) with its registers, by END sent from the exception handler
        ok5, t5 = step('6 crash', ['run', a.program, '--args', 'report lines:2 crash', '--deadline', '90', '--wait',
                                   '150'], 210, ok_rc=5, out='crash')
        if ok5 and 'crash: event' not in t5 and 'CRASHED' not in t5:
            rows[-1]['pass'] = False
            rows[-1]['detail'] = 'back, but no CRASHED with registers'
        step('7 back', ['status'], 90)
        if a.switch:   # ql-core5: the disc switch ends through the client like every ending: its END must come (rc 0)
            ok8, t8 = step('8 disc switch', ['run', a.program, '--args', 'gdreboot' + dg, '--deadline', '60', '--wait',
                                             '120'], 180, out='switch')
            if ok8 and 'END from the program' not in t8:
                rows[-1]['pass'] = False
                rows[-1]['detail'] = 'back, but no END from the program'
        if a.hang:
            watchdog_rows(dg)

    if ok and a.focus:   # ql-core6's sheet: only the rows that matter (the watchdog rows last)
        screen_row('4a screen x3', 'screen')
        screen_row('4b screen x3', 'screen2')
        watchdog_rows(dg)
    print('\n%-12s %-5s %8s  %s' % ('step', 'result', 'seconds', 'detail'))
    for r in rows:
        print('%-12s %-5s %8.1f  %s' % (r['step'], 'PASS' if r['pass'] else 'FAIL', r['seconds'],
                                        r.get('detail', 'rc %s' % r['rc'])))
    for r in rows:
        if 'end_to_beacon' in r:
            print('  %-10s %s' % (r['step'].split()[0], r['end_to_beacon']))
    n_hang = 0 if not (a.hang or a.focus) else 3 if any(r['step'] == '11 after' for r in rows) else 2
    n_rows = 1 + 2 + n_hang if a.focus else 7 + bool(a.switch) + bool(a.stub) + n_hang
    allok = rows and all(r['pass'] for r in rows) and len(rows) == n_rows
    print('\n%s' % ('ALL PASS: push, run, report, screen grabs and RESET work on this console' if allok else
                    'NOT ALL PASS: the logs are in %s' % os.path.abspath(a.out)))
    with open(os.path.join(a.out, 'first-run.json'), 'w') as f:
        json.dump({'rows': rows, 'all_pass': bool(allok), 'at': time.strftime('%Y-%m-%d %H:%M:%S')}, f, indent=1)
    return 0 if allok else 1


if __name__ == '__main__':
    sys.exit(main())
