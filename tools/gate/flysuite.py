#!/usr/bin/env python3
r"""flysuite.py: the integration gate's Flycast suite (dc-qalink 0.2, tools/gate). Windows, Python 3.10+.

Two commands:

  flysuite.py machine [--cpu-max 85] [--cpu-wait 1200] [--min-free C:=28 E:=20]
      Waits until the PC's CPU use is under --cpu-max (sampled over 3 s), at most --cpu-wait seconds, and checks the
      free space of the named drives (GB). Exit 0 go, 20 a drive is short, 21 the CPU never fell (the gate stops; that
      is the machine, not a lane's fault).

  flysuite.py run --flycast <flycast.exe> --config <transients.txt> --disc <disc.gdi> --tree <dc-qalink copy>
                  --build <folder with testprog.elf, liveprog.elf> --pc-ip <this PC> --port P --lport LP --ctl C
                  --http H --mcp-http M --out <folder> --plan suite.json [--plan extra.json ...] [--lock <file>]
      One Flycast boot (started from its own path on a desktop of its own: no window focus, no input), the loader
      disc staged by gate.sh, then the plan's rows in two phases:
        phase "loop":   dcql_loop.py through a pinned relay (UDP P for the loader, 127.0.0.1:C for the tool);
        phase "bridge": the bridge (qalink_bridge.py, only-local, --console <pc-ip>:LP, HTTP H, MCP HTTP M), pushes
                        through its API, the MCP test against it, and deltas between rows.
      Then the cited numbers re-measured. Writes <out>/fly.json and <out>/fly.md. Exit 0 every row as it must, 1 a row
      failed, 9 the guard refused (another loader heard: nothing is sent to it), 20/21 machine.

The guard (a standing rule since a lane's bridge pushed a program to a real console, 2026-11-22): before Flycast
starts, UDP P (and the default loader port 52060 when no other program holds it) is heard for 5 s; any datagram from
an address that is not this PC's refuses the run. While it runs the relay and the bridge talk only to this PC's
addresses; a datagram from anywhere else stops the suite. Port 52060 is never used for the suite.

Plan rows (JSON): {"name", "phase": "loop"|"bridge", "kind": "loop"|"push"|"mcp"|"delta", ...}
  loop:  "args": [dcql_loop.py arguments; {testprog} {liveprog} {out} substituted], "timeout", "expect" (exit code),
         "must": {"state": "DONE", "lines": 5, "stills": 3, "reset_rung": "reset", "report_has": [...],
                  "transfer_refused": true, "report_min_bytes": n}
  push:  "program": "liveprog"|"testprog", "args": "...", "wait": s, "must": {"state": "done",
         "numbers": {"<label>": [min, max]}}
  mcp:   "timeout", "must": {"passed": 9}
  delta: "a", "b" (row names), "number": "<label>", "range": [min, max]   (b minus a)
  loop "must" also takes "report_regex": {"<name>": "<regex, one number group>"} (the number is cited as at:<name>).
  phase "disc" (last): a lane's own disc (gate.sh builds it from the plan's "own_discs" into --discs <dir>/<name>/),
         booted alone with no relay; nothing is sent to it. "disc": "<name>", "until": "<serial text>", "timeout",
         "serial_regex": {"<name>": "<regex, one number group>"}, "after": [{"name", "args": [python arguments;
         {out} {tree} {serial} substituted], "expect": 0, "grep": "<regex a line must match>", "regex": {...}}],
         "at_sum": {"<name>": [names]}, "must_at": {"<name>": [min, max]}.
Extra plans add rows, replace rows by name ("replace": true) or drop them ("drop": [names]); "cites" are listed as
{"lane", "what", "row", "from": "number:<label>"|"seconds"|"json:<a.b.c>"|"mcp:<record name prefix>:<field>",
 "cited", "unit", "tol" (relative, default 0.25)}.
Flycast is a function check: every time here is tier "Flycast", never a console number.
"""
import argparse, ctypes as C, glob, hashlib, json, os, re, selectors, shutil, socket, subprocess, sys, threading, time
import urllib.error, urllib.request, uuid

LOADER_PORT = 52060   # the real console's loader port: never used by the suite
BRIDGE_ARGS = []      # extra bridge arguments from the plans ({build} = the build folder, {out} = the suite's folder),
                      # e.g. --viewer {build}/viewer.elf, --asset-root {out} (the generated assets are written below it)


# ----------------------------------------------------------------------------------------------------------- machine
def cpu_percent(span=3.0):
    k = C.windll.kernel32
    ft = [C.c_ulonglong() for _ in range(3)]

    def times():
        k.GetSystemTimes(C.byref(ft[0]), C.byref(ft[1]), C.byref(ft[2]))
        return ft[0].value, ft[1].value, ft[2].value
    i0, k0, u0 = times()
    time.sleep(span)
    i1, k1, u1 = times()
    tot = (k1 - k0) + (u1 - u0)
    return 100.0 * (1.0 - (i1 - i0) / tot) if tot else 0.0


def machine(a):
    out = {'drives': {}}
    for spec in a.min_free:
        d, _, gb = spec.partition('=')
        free = shutil.disk_usage(d + '\\').free / 1e9
        out['drives'][d] = round(free, 1)
        if free < float(gb):
            out['stop'] = '%s has %.1f GB free, under %s GB' % (d, free, gb)
            print(json.dumps(out))
            return 20
    t0 = time.time()
    while True:
        cpu = cpu_percent()
        out['cpu'] = round(cpu, 1)
        if cpu < a.cpu_max:
            out['waited_s'] = round(time.time() - t0)
            print(json.dumps(out))
            return 0
        if time.time() - t0 > a.cpu_wait:
            out['stop'] = 'CPU %.0f%% for %d s' % (cpu, a.cpu_wait)
            print(json.dumps(out))
            return 21
        print('cpu %.0f%%: waiting' % cpu, file=sys.stderr, flush=True)
        time.sleep(30)


# ------------------------------------------------------------------------------------------------------------ Flycast
from ctypes import wintypes as W   # noqa: E402  (Windows only, like the rest of this file)


class Startup(C.Structure):
    _fields_ = [('cb', W.DWORD), ('reserved', W.LPWSTR), ('desktop', W.LPWSTR), ('title', W.LPWSTR),
                *[(n, W.DWORD) for n in ('x', 'y', 'cx', 'cy', 'cols', 'rows', 'fill', 'flags')],
                ('show', W.WORD), ('reserved_size', W.WORD), ('reserved_data', C.c_void_p),
                ('stdin', W.HANDLE), ('stdout', W.HANDLE), ('stderr', W.HANDLE)]


class ProcessInfo(C.Structure):
    _fields_ = [('process', W.HANDLE), ('thread', W.HANDLE), ('pid', W.DWORD), ('tid', W.DWORD)]


def launch_flycast(exe, config, disc, out, desk_name):
    """Flycast from its own path, settings as -config transients, on a desktop of its own (nothing on the user's)."""
    import msvcrt
    from ctypes import wintypes as W
    user, kernel = C.WinDLL('user32', use_last_error=True), C.WinDLL('kernel32', use_last_error=True)
    user.CreateDesktopW.restype = W.HANDLE
    desk = user.CreateDesktopW(desk_name, None, None, 0, 0x000f01ff, None)
    if not desk:
        raise C.WinError(C.get_last_error())
    args = [exe]
    for line in open(config, encoding='utf-8').read().splitlines():
        if line.strip() and not line.startswith('#'):
            args += ['-config', line.strip()]
    args.append(disc)
    si = Startup()
    si.cb = C.sizeof(si); si.desktop = desk_name; si.title = desk_name; si.flags = 1 | 0x100; si.show = 0
    serial = open(os.path.join(out, 'serial.txt'), 'wb'); null = open(os.devnull, 'rb')
    si.stdout = si.stderr = msvcrt.get_osfhandle(serial.fileno()); si.stdin = msvcrt.get_osfhandle(null.fileno())
    os.set_handle_inheritable(si.stdout, True); os.set_handle_inheritable(si.stdin, True)
    pi = ProcessInfo()
    cmd = C.create_unicode_buffer(subprocess.list2cmdline(args))
    try:
        if not kernel.CreateProcessW(exe, cmd, None, None, True, 0x08000000, None, os.path.dirname(config), C.byref(si),
                                     C.byref(pi)):
            raise C.WinError(C.get_last_error())
        kernel.CloseHandle(W.HANDLE(pi.thread))
    finally:
        serial.close(); null.close()
    return kernel, user, desk, pi


def stop_flycast(h):
    from ctypes import wintypes as W
    kernel, user, desk, pi = h
    if kernel.WaitForSingleObject(W.HANDLE(pi.process), 0) == 258:
        kernel.TerminateProcess(W.HANDLE(pi.process), 0)
        kernel.WaitForSingleObject(W.HANDLE(pi.process), 10000)
    kernel.CloseHandle(W.HANDLE(pi.process)); user.CloseDesktop(W.HANDLE(desk))


# -------------------------------------------------------------------------------------------------- guard and relay
def local_addresses(pc_ip):
    loc = {'127.0.0.1', '0.0.0.0', pc_ip}
    try:
        loc |= set(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    return loc


def is_local(ip, loc):
    return ip.startswith('127.') or ip in loc


def guard(ports, loc, seconds, say):
    """Hear the ports for `seconds`; True when nothing came from another address."""
    socks = []
    for p in ports:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind(('0.0.0.0', p))     # no SO_REUSEADDR: a port another program holds is not taken from it
        except OSError:
            say('guard: UDP %d is held by another program: not heard' % p)
            s.close()
            continue
        s.setblocking(False)
        socks.append((p, s))
    sel = selectors.DefaultSelector()
    for p, s in socks:
        sel.register(s, selectors.EVENT_READ, p)
    end, foreign = time.time() + seconds, []
    while time.time() < end:
        for key, _ in sel.select(0.2):
            try:
                d, a = key.fileobj.recvfrom(4096)
            except OSError:
                continue
            if not is_local(a[0], loc):
                foreign.append('%s:%d on UDP %d (%d bytes)' % (a[0], a[1], key.data, len(d)))
    for _, s in socks:
        s.close()
    say('guard: heard UDP %s for %.0f s: %s' % ([p for p, _ in socks], seconds,
                                                 'nothing from another address' if not foreign else 'REFUSED ' + ', '.join(foreign)))
    return not foreign, foreign


class Relay(threading.Thread):
    """Pinned relay: the loader's datagrams only from this PC's addresses (Flycast maps the emulated console onto
    them); a datagram from any other address stops it, and nothing is ever sent to that address."""

    def __init__(self, port, ctl, loc, log):
        super().__init__(daemon=True)
        self.s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        self.s.bind(('0.0.0.0', port))
        self.c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.c.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        self.c.bind(('127.0.0.1', ctl))
        self.loc, self.log, self.run_flag, self.tripped = loc, log, True, None
        self.console = self.client = None

    def run(self):
        sel = selectors.DefaultSelector()
        sel.register(self.s, selectors.EVENT_READ)
        sel.register(self.c, selectors.EVENT_READ)
        while self.run_flag:
            for key, _ in sel.select(0.5):
                try:
                    d, a = key.fileobj.recvfrom(4096)
                except OSError:
                    continue
                if key.fileobj is self.s:
                    if not is_local(a[0], self.loc):
                        self.tripped = '%s:%d' % a
                        self.log('relay: REFUSED a datagram from %s:%d (not this PC); stopping, nothing sent to it' % a)
                        self.run_flag = False
                        break
                    if a != self.console:
                        self.console = a
                        self.log('relay: console (Flycast) at %s:%d' % a)
                    if self.client:
                        try:
                            self.c.sendto(d, self.client)
                        except OSError:
                            pass
                elif a[0] == '127.0.0.1':
                    self.client = a
                    if self.console:
                        self.s.sendto(d, self.console)
        self.s.close(); self.c.close()

    def stop(self):
        self.run_flag = False
        self.join(5)


# ------------------------------------------------------------------------------------------------------------ bridge
class BridgeClient:
    def __init__(self, url):
        self.url = url

    def get(self, path, timeout=10):
        with urllib.request.urlopen(self.url + path, timeout=timeout) as f:
            return json.loads(f.read())

    def call(self, method, path, body=None, timeout=180):
        req = urllib.request.Request(self.url + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={'Content-Type': 'application/json', 'X-By': 'dc-qalink gate flysuite.py'})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as f:
                return f.status, json.loads(f.read())
        except urllib.error.HTTPError as e:
            b = e.read()
            try:
                return e.code, json.loads(b)
            except ValueError:
                return e.code, {'error': b[:300].decode('latin-1')}

    def push(self, path, args):
        raw = open(path, 'rb').read()
        b = uuid.uuid4().hex
        parts = [('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (b, k, v)).encode()
                 for k, v in (('sha256', hashlib.sha256(raw).hexdigest()), ('args', args), ('name', os.path.basename(path)))]
        parts.append(('--%s\r\nContent-Disposition: form-data; name="program"; filename="%s"\r\n\r\n' %
                      (b, os.path.basename(path))).encode() + raw + b'\r\n')
        body = b''.join(parts) + ('--%s--\r\n' % b).encode()
        r = urllib.request.Request(self.url + '/v0/runs', data=body, method='POST', headers={
            'Content-Type': 'multipart/form-data; boundary=%s' % b, 'X-By': 'dc-qalink gate flysuite.py'})
        with urllib.request.urlopen(r, timeout=30) as f:
            return json.loads(f.read())


# ------------------------------------------------------------------------------------------------------------- plans
def load_plans(paths):
    rows, cites, notes = [], [], []
    global BRIDGE_ARGS
    for p in paths:
        plan = json.load(open(p, encoding='utf-8'))
        BRIDGE_ARGS += plan.get('bridge_args', [])
        drop = set(plan.get('drop', []))
        rows = [r for r in rows if r['name'] not in drop]
        for r in plan.get('rows', []):
            if r.get('replace'):
                rows = [x if x['name'] != r['name'] else r for x in rows]
                if not any(x is r for x in rows):
                    rows.append(r)
            else:
                if any(x['name'] == r['name'] for x in rows):
                    raise SystemExit('plan %s: row %s already exists (use "replace": true)' % (p, r['name']))
                rows.append(r)
        cites += plan.get('cites', [])
        if plan.get('note'):
            notes.append(plan['note'])
    return rows, cites, notes


def subst(x, m):
    for k, v in m.items():
        x = x.replace('{%s}' % k, v)
    return x


def run_json(d):
    fs = sorted(glob.glob(os.path.join(d, 'run-*.json')), key=os.path.getmtime)
    return json.load(open(fs[-1], encoding='utf-8')) if fs else None


def check_loop(r, rd, res):
    """The row's "must" against the run's JSON and report. Returns a list of (check, ok, seen)."""
    must, out = r.get('must', {}), []
    j = run_json(rd)
    res['json'] = {k: j.get(k) for k in ('transfer', 'run', 'back_after_s', 'reset', 'program_side', 'lost', 'end',
                                         'end_to_beacon_s')} if j else None
    rep = (j or {}).get('report') or {}
    end = (j or {}).get('end') or {}
    res['report'] = {k: rep.get(k) for k in ('state', 'exit', 'bytes', 'crc_ok')} if rep else None
    text = ''
    if rep.get('path') and os.path.isfile(rep['path']):
        text = open(rep['path'], encoding='latin-1').read()
    else:   # a result that came by END (the full restart, nothing kept): the PC tool writes report-<run>.txt from it
        fs = sorted(glob.glob(os.path.join(rd, 'report-*.txt')), key=os.path.getmtime)
        text = open(fs[-1], encoding='latin-1').read() if fs else ''
    at = {}   # the program's headline lines: "@ <name> = <number> ..."
    for line in text.splitlines():
        if line.startswith('@ ') and '=' in line:
            k, _, v = line[2:].partition('=')
            try:
                at[k.strip()] = float(v.split()[0])
            except (ValueError, IndexError):
                pass
    res['at'] = at
    for k, want in must.items():
        if k == 'state':
            if rep:
                out.append(('report state %s' % want, rep.get('state') == want and rep.get('crc_ok') is True,
                            '%s crc %s' % (rep.get('state'), rep.get('crc_ok'))))
            else:
                es = end.get('state') if isinstance(end, dict) else None
                ok = es == want and end.get('crc_ok', True) is not False
                out.append(('state %s (by END: no report kept)' % want, ok, '%s crc %s' % (es, end.get('crc_ok')) if end
                            else 'no report and no END'))
        elif k == 'lines':
            ln = ((j or {}).get('program_side') or {}).get('lines') or {}
            out.append(('%d lines, no gaps' % want, ln.get('n') == want and ln.get('gaps') == 0, ln))
        elif k == 'stills':
            st = ((j or {}).get('program_side') or {}).get('stills')
            n = len(st) if isinstance(st, (list, dict)) else (st or 0)
            pngs = len(glob.glob(os.path.join(rd, 'still-*.png')))
            out.append(('%d stills saved as PNG' % want, n == want and pngs == want, 'json %s, png %d' % (n, pngs)))
        elif k == 'reset_rung':
            rr = ((j or {}).get('reset') or {}).get('rung')
            out.append(('back by rung %s' % want, rr == want, rr))
        elif k == 'report_has':
            for w in want:
                out.append(('report has "%s"' % w, w in text, len(text)))
        elif k == 'report_lacks':
            for w in want:
                out.append(('report has no "%s"' % w, bool(text) and w not in text, len(text)))
        elif k == 'report_exit':
            ex = rep.get('exit') if rep else (end.get('exit') if isinstance(end, dict) else None)
            out.append(('program exit %s' % want, ex == want, ex))
        elif k == 'report_by':
            out.append(('result came by %s' % want, want in str(rep.get('by', '')), rep.get('by')))
        elif k == 'crash_registers':
            # the END's registers sit in the report's "end" (a run with nothing kept) or in the run's end.end, with the
            # PC tool's decoded end.crash beside them (a run whose record was kept: ql-core6's gate, row c6-03)
            cands = [rep.get('end'), end.get('end') if isinstance(end, dict) else None, end]
            e = next((c for c in cands if isinstance(c, dict) and c.get('crash_pc')), {})
            cr = end.get('crash') if isinstance(end, dict) else None
            cr = cr if isinstance(cr, dict) else {}
            ok = (bool(e.get('crash_pc')) and bool(e.get('crash_evt'))) or (bool(cr.get('pc')) and bool(cr.get('event')))
            out.append(('crash registers in the END', ok, 'evt %s pc %s' % (e.get('crash_evt') or cr.get('event'),
                                                                            e.get('crash_pc') or cr.get('pc'))))
        elif k == 'report_min_bytes':
            out.append(('report at least %d bytes' % want, (rep.get('bytes') or 0) >= want, rep.get('bytes')))
        elif k == 'transfer_refused':
            v = ((j or {}).get('transfer') or {}).get('verdict')
            out.append(('transfer refused by the loader', v not in (None, 0), v))
        elif k == 'report_regex':   # {"name": "<regex with one number group>"}: the number goes to res['at'][name]
            for name, rx in want.items():
                mt = re.search(rx, text)
                if mt:
                    at[name] = float(mt.group(1))
                out.append(('report matches /%s/' % rx[:50], bool(mt), mt.group(0)[:80] if mt else len(text)))
        else:
            out.append(('unknown must %s' % k, False, None))
    return out


def numbers_of(run):
    return {n['label']: n['value'] for n in ((run.get('summary') or {}).get('numbers') or [])}


def main_run(a):
    out = os.path.abspath(a.out)
    os.makedirs(out, exist_ok=True)
    log = open(os.path.join(out, 'fly.log'), 'a', encoding='utf-8')

    def say(s):
        line = '%s %s' % (time.strftime('%H:%M:%S'), s)
        print(line, flush=True); log.write(line + '\n'); log.flush()
    for p in (a.port, a.lport, a.ctl, a.http, a.mcp_http):
        if p == LOADER_PORT:
            raise SystemExit('refused: port %d is the real console loader port' % p)
    rows, cites, notes = load_plans(a.plan)
    if a.only:   # a measuring run: only these rows, and only the cites that read them
        keep = set(a.only.split(','))
        rows = [r for r in rows if r['name'] in keep]
        cites = [c for c in cites if c['row'] in keep]
    py = sys.executable
    loop_py = os.path.join(a.tree, 'pc', 'dcql_loop.py')
    m = {'testprog': os.path.join(a.build, 'testprog.elf'), 'liveprog': os.path.join(a.build, 'liveprog.elf'),
         'build': a.build, 'fixtures': a.fixtures or os.path.join(out, 'fixtures'), 'disc': a.disc}
    loc = local_addresses(a.pc_ip)
    result = {'tier': 'Flycast (function check; Halo patched build)', 'flycast': a.flycast, 'disc': a.disc,
              'ports': {'loader': a.port, 'console_lport': a.lport, 'ctl': a.ctl, 'http': a.http, 'mcp_http': a.mcp_http},
              'rows': [], 'cites': [], 'notes': notes, 'started': time.strftime('%Y-%m-%d %H:%M:%S')}
    lock = None
    if a.lock:
        try:
            fd = os.open(a.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, ('%d %s\n' % (os.getpid(), time.strftime('%H:%M:%S'))).encode()); os.close(fd)
            lock = a.lock
        except FileExistsError:
            say('another Flycast of this lane holds %s: not started' % a.lock)
            return 22
    fly = relay = bridge = t_boot = None
    rc = 0
    try:
        ok, foreign = guard([a.port, LOADER_PORT], loc, 5.0, say)
        result['guard'] = {'ok': ok, 'foreign': foreign}
        if not ok:
            return 9
        relay = Relay(a.port, a.ctl, loc, say)
        relay.start()
        t_boot = time.time()
        fly = launch_flycast(a.flycast, a.config, a.disc, out, 'dcql-gate-%d' % os.getpid())
        say('flycast pid %d: %s %s' % (fly[3].pid, a.flycast, a.disc))
        time.sleep(a.boot_wait)
        rowres = {}

        def loop_rows(phase, only=None):
            for r in rows:
                if r.get('phase', 'loop') != phase or (only and r['name'] != only):
                    continue
                if relay.tripped:
                    break
                rd = os.path.join(out, r['name'])
                os.makedirs(rd, exist_ok=True)
                mm = dict(m, out=rd)
                args = [py, '-u', loop_py, '--udp', '127.0.0.1:%d' % a.ctl, '--bind', '0'] + [subst(x, mm) for x in r['args']]
                t0 = time.time()
                with open(os.path.join(rd, 'tool.log'), 'w', encoding='utf-8') as f:
                    try:
                        code = subprocess.run(args, stdout=f, stderr=subprocess.STDOUT, cwd=rd, timeout=r.get('timeout', 150),
                                              creationflags=0x08000000).returncode
                    except subprocess.TimeoutExpired:
                        code = 'timeout'
                res = {'name': r['name'], 'phase': phase, 'what': r.get('what', ''), 'control': r.get('control', False),
                       'console_only': r.get('console_only', False),
                       'rc': code, 'expect': r.get('expect', 0), 'seconds': round(time.time() - t0, 2)}
                checks = [('exit code %s' % res['expect'], code == res['expect'], code)] + check_loop(r, rd, res)
                res['checks'] = [{'check': c, 'ok': bool(o), 'seen': s} for c, o, s in checks]
                res['ok'] = all(o for _, o, _ in checks)
                rowres[r['name']] = res
                result['rows'].append(res)
                say('row %-22s rc %-7s %6.2f s %s' % (r['name'], code, res['seconds'], 'ok' if res['ok'] else
                                                        'FAILED: ' + '; '.join('%s (%s)' % (c, s) for c, o, s in checks if not o)))
        loop_rows('loop')
        relay.stop()
        if relay.tripped:
            result['guard']['tripped'] = relay.tripped
            return 9
        brows = [r for r in rows if r.get('phase') == 'bridge']
        if brows:
            url = 'http://127.0.0.1:%d' % a.http
            bc = BridgeClient(url)
            bargs = [py, '-u', os.path.join(a.tree, 'pc', 'qalink_bridge.py'), '--port', str(a.http), '--udp-port',
                     str(a.port), '--data', os.path.join(out, 'bridge'), '--device', 'flycast', '--console',
                     '%s:%d' % (a.pc_ip, a.lport), '--selftest', m['testprog'], '--mcp-http', str(a.mcp_http)] + \
                [subst(x, {'build': a.build, 'out': out}) for x in BRIDGE_ARGS]   # {out}: this suite's folder
            bridge = subprocess.Popen(bargs, stdout=open(os.path.join(out, 'bridge.out'), 'w'), stderr=subprocess.STDOUT,
                                      cwd=os.path.join(a.tree, 'pc'), creationflags=0x08000000)
            say('bridge pid %d on http %d, udp %d, console pinned to %s:%d' % (bridge.pid, a.http, a.port, a.pc_ip, a.lport))
            t0, st = time.time(), None
            while time.time() - t0 < 90:
                try:
                    st = bc.get('/v0/status')
                    if st.get('link') == 'up':
                        break
                except OSError:
                    pass
                time.sleep(0.5)
            foreign = []
            try:
                foreign = [x for x in bc.get('/v0/discovery').get('seen', []) if not is_local(x.get('address', ''), loc)]
            except OSError:
                pass
            dev = (st or {}).get('device') or {}
            res = {'name': 'bridge-link', 'phase': 'bridge', 'what': 'the bridge links Flycast\'s loader (only-local, pinned)',
                   'seconds': round(time.time() - t0, 2)}
            checks = [('link up', (st or {}).get('link') == 'up', (st or {}).get('link')),
                      ('only-local', (st or {}).get('only_local') is True, (st or {}).get('only_local')),
                      ('no loader at another address', not foreign, foreign),
                      ('device MAC from HELLO', bool(dev.get('mac')), dev.get('mac'))]
            res['checks'] = [{'check': c, 'ok': bool(o), 'seen': s} for c, o, s in checks]
            res['ok'] = all(o for _, o, _ in checks)
            result['rows'].append(res)
            say('row bridge-link %s %s' % ('ok' if res['ok'] else 'FAILED', json.dumps(res['checks'])[:300]))
            if foreign:
                return 9
            if res['ok']:
                for r in brows:
                    res = {'name': r['name'], 'phase': 'bridge', 'what': r.get('what', ''), 'control': r.get('control', False)}
                    t0 = time.time()
                    checks = []
                    if r['kind'] == 'push':
                        run = bc.push(m[r.get('program', 'liveprog')], r.get('args', ''))
                        rid = run['id']
                        while time.time() - t0 < r.get('wait', 120):
                            run = bc.get('/v0/runs/%s' % rid)
                            if run.get('state') not in ('queued', 'sending', 'running'):
                                break
                            time.sleep(0.5)
                        json.dump(run, open(os.path.join(out, '%s.json' % r['name']), 'w'), indent=1)
                        res.update(run=rid, state=run.get('state'), times=run.get('times'), numbers=numbers_of(run))
                        want = r.get('must', {})
                        if 'state' in want:
                            checks.append(('state %s' % want['state'], run.get('state') == want['state'], run.get('state')))
                        for lab, (lo, hi) in want.get('numbers', {}).items():
                            v = res['numbers'].get(lab)
                            checks.append(('%s in [%s, %s]' % (lab, lo, hi), v is not None and lo <= v <= hi, v))
                    elif r['kind'] == 'mcp':
                        rd = os.path.join(out, r['name'])
                        env = dict(os.environ, PYTHONIOENCODING='utf-8')
                        with open(os.path.join(out, '%s.log' % r['name']), 'w', encoding='utf-8') as f:
                            try:
                                code = subprocess.run([py, '-u', os.path.join(a.tree, 'tests', 'test_mcp.py'), '--out', rd,
                                                       '--bridge-url', url, '--program', m['liveprog']], stdout=f,
                                                      stderr=subprocess.STDOUT, timeout=r.get('timeout', 300), env=env,
                                                      creationflags=0x08000000).returncode
                            except subprocess.TimeoutExpired:
                                code = 'timeout'
                        mr = []
                        try:
                            mr = json.load(open(os.path.join(rd, 'mcp-results.json'), encoding='utf-8'))['results']
                        except (OSError, ValueError, KeyError):
                            pass
                        res.update(rc=code, mcp=mr, passed=sum(1 for x in mr if x['ok']), total=len(mr))
                        want = r.get('must', {}).get('passed', 9)
                        checks.append(('MCP %d of %d' % (want, want), code == 0 and res['passed'] == want == res['total'],
                                       '%d of %d, rc %s' % (res['passed'], res['total'], code)))
                    elif r['kind'] == 'assets':
                        # live assets (docs/ASSETS.md): push the viewer, or send a generated asset, view it on the
                        # console, grab, and compare with the PC render; "then" is a second view (no resend)
                        if r['op'] == 'viewer':
                            t1 = time.time()   # an earlier row's program (MCP's 60 s liveprog) ends first
                            while time.time() - t1 < 180:
                                try:
                                    if not bc.get('/v0/status').get('active_run'):
                                        break
                                except OSError:
                                    pass
                                time.sleep(1.0)
                            code, v = bc.call('POST', '/v0/assets/viewer', {'timeout_s': 90, 'secs': 300})
                            res.update(code=code, viewer={k: v.get(k) for k in ('linked', 'run', 'state', 'vfree', 'seconds')})
                            checks.append(('viewer pushed and linked', code == 200 and v.get('linked') is True,
                                           '%s %s' % (code, v.get('error') or v.get('linked'))))
                        else:
                            gdir = os.path.join(out, 'gen')
                            if not os.path.isdir(gdir):
                                sys.path.insert(0, os.path.join(a.tree, 'pc'))
                                from assets import gen as agen   # the merged tree's generator: generated assets only
                                agen.write_all(gdir)
                            spec = dict(r['asset'])
                            spec['path'] = os.path.join(gdir, spec.pop('gen'))
                            code, ar = bc.call('POST', '/v0/assets', spec)
                            res.update(asset={k: ar.get(k) for k in ('id', 'state', 'bytes', 'psnr', 'reason')})
                            checks.append(('asset converted', code == 200 and bool(ar.get('id')), '%s %s' % (code, ar.get('state'))))
                            for key, body in (('view', r.get('view_args', {})), ('then', r.get('then'))):
                                if body is None or not ar.get('id'):
                                    continue
                                code, vr = bc.call('POST', '/v0/assets/%s/view' % ar['id'], body)
                                res[key] = {'code': code, 'state': vr.get('state'), 'times': vr.get('times'),
                                            'diff': {k: (vr.get('diff') or {}).get(k) for k in ('pixels', 'differ', 'over', 'max', 'psnr')},
                                            'error': vr.get('error')}
                                checks.append(('%s: on the console (live)' % key, code == 200 and vr.get('state') == 'live',
                                               '%s %s' % (code, vr.get('state') or vr.get('error'))))
                                for mk, lim in (r.get('must', {}).get(key) or {}).items():
                                    seen = (vr.get('diff') or {}).get(mk.replace('_max', ''))
                                    checks.append(('%s: %s at most %s' % (key, mk.replace('_max', ''), lim),
                                                   seen is not None and seen <= lim, seen))
                    elif r['kind'] == 'delta':
                        ra, rb = rowres.get(r['a'], {}), rowres.get(r['b'], {})
                        va, vb = (ra.get('numbers') or {}).get(r['number']), (rb.get('numbers') or {}).get(r['number'])
                        d = None if va is None or vb is None else round(vb - va, 4)
                        lo, hi = r['range']
                        res.update(a=va, b=vb, delta=d)
                        checks.append(('%s: %s minus %s in [%s, %s]' % (r['number'], r['b'], r['a'], lo, hi),
                                       d is not None and lo <= d <= hi, d))
                    res['seconds'] = round(time.time() - t0, 2)
                    res['checks'] = [{'check': c, 'ok': bool(o), 'seen': s} for c, o, s in checks]
                    res['ok'] = bool(checks) and all(o for _, o, _ in checks)
                    rowres[r['name']] = res
                    result['rows'].append(res)
                    say('row %-22s %6.2f s %s %s' % (r['name'], res['seconds'], 'ok' if res['ok'] else 'FAILED',
                                                     json.dumps(res.get('numbers') or res['checks'])[:300]))
        # phase "tail": loop rows that may leave the emulated console unable to come back (a hang with interrupts off:
        # Flycast has no watchdog reset), run last through a new pinned relay
        if any(r.get('phase') == 'tail' for r in rows):
            if bridge:
                # the last bridge row may have left a program running (MCP's push_build runs liveprog for 60 s): wait
                # until the bridge has no run and the loader beacons again, so the tail starts on an idle loader
                t0, idle = time.time(), False
                while time.time() - t0 < 180:
                    try:
                        st = bc.get('/v0/status')
                        if not st.get('active_run') and st.get('link') == 'up' and \
                                not ((st.get('device') or {}).get('program') or {}).get('linked'):
                            idle = True
                            break
                    except OSError:
                        pass
                    time.sleep(1.0)
                say('tail: the loader is %s after %.0f s' % ('idle' if idle else 'NOT idle', time.time() - t0))
                subprocess.run(['taskkill', '/PID', str(bridge.pid), '/T', '/F'], capture_output=True, creationflags=0x08000000)
                try:
                    bridge.wait(10)
                except subprocess.TimeoutExpired:
                    bridge.kill()
                bridge = None
                time.sleep(1.0)
            # a fresh boot for each tail row (the first ql-core4 gates: after the bridge phase the loader beaconed but
            # did not answer LOAD; ql-core6's first gate: a tail row's hang leaves Flycast, which has no watchdog,
            # hung for the next): each starts on a loader that only its own relay has spoken to
            for tr in [r['name'] for r in rows if r.get('phase') == 'tail']:
                if fly:
                    stop_flycast(fly)
                fly = None
                relay = Relay(a.port, a.ctl, loc, say)
                relay.start()
                fly = launch_flycast(a.flycast, a.config, a.disc, out, 'dcql-gate-tail-%d' % os.getpid())
                say('tail: flycast restarted for %s, pid %d' % (tr, fly[3].pid))
                time.sleep(a.boot_wait)
                loop_rows('tail', tr)
                relay.stop()
                if relay.tripped:
                    result['guard']['tripped'] = relay.tripped
                    return 9
        # phase "disc": a lane's own disc (its program alone, no loader), booted on a fresh Flycast; its serial line
        # is the evidence. Nothing is sent to it and no relay runs: the guard is heard again first.
        drows = [r for r in rows if r.get('phase') == 'disc']
        if drows:
            if bridge:   # nothing of the loader's runs may talk to the lane's program
                subprocess.run(['taskkill', '/PID', str(bridge.pid), '/T', '/F'], capture_output=True, creationflags=0x08000000)
                try:
                    bridge.wait(10)
                except subprocess.TimeoutExpired:
                    bridge.kill()
                bridge = None
            if fly:
                stop_flycast(fly)
                fly = None
            for r in drows:
                rd = os.path.join(out, r['name'])
                os.makedirs(rd, exist_ok=True)
                res = {'name': r['name'], 'phase': 'disc', 'what': r.get('what', ''), 'control': r.get('control', False),
                       'console_only': r.get('console_only', False)}
                checks, at = [], {}
                disc = os.path.join(a.discs or '', r['disc'], 'disc.gdi')
                ok, foreign = guard([a.port, LOADER_PORT], loc, 5.0, say)
                if not ok:
                    result['guard']['tripped'] = foreign
                    return 9
                if not os.path.isfile(disc):
                    checks.append(('the disc %s was built' % r['disc'], False, disc))
                else:
                    t0 = time.time()
                    fly = launch_flycast(a.flycast, a.config, disc, rd, 'dcql-gate-disc-%d' % os.getpid())
                    say('disc %s: flycast pid %d' % (r['disc'], fly[3].pid))
                    until, ser, seen = r.get('until', 'QLDONE'), os.path.join(rd, 'serial.txt'), False
                    while time.time() - t0 < r.get('timeout', 300):
                        time.sleep(2.0)
                        try:
                            with open(ser, 'rb') as f:
                                if until.encode() in f.read():
                                    seen = True
                                    break
                        except OSError:
                            pass
                    stop_flycast(fly)
                    fly = None
                    res['seconds'] = round(time.time() - t0, 1)
                    checks.append(('serial says %s' % until, seen, '%s s' % res['seconds']))
                    text = open(ser, encoding='latin-1').read() if os.path.isfile(ser) else ''
                    for name, rx in r.get('serial_regex', {}).items():
                        mt = re.search(rx, text)
                        if mt:
                            at[name] = float(mt.group(1))
                        checks.append(('serial matches /%s/' % rx[:50], bool(mt), mt.group(0)[:80] if mt else None))
                    mm = dict(m, out=rd, tree=a.tree, serial=ser)
                    for x in r.get('after', []):
                        args = [py, '-I', '-u'] + [subst(y, mm) for y in x['args']]
                        lp = os.path.join(rd, '%s.log' % x['name'])
                        with open(lp, 'w', encoding='utf-8') as f:
                            try:
                                code = subprocess.run(args, stdout=f, stderr=subprocess.STDOUT, cwd=rd, timeout=x.get('timeout', 300),
                                                      creationflags=0x08000000).returncode
                            except subprocess.TimeoutExpired:
                                code = 'timeout'
                        lt = open(lp, encoding='utf-8', errors='replace').read()
                        checks.append(('%s exit %s' % (x['name'], x.get('expect', 0)), code == x.get('expect', 0), code))
                        if x.get('grep'):
                            checks.append(('%s: a line matches /%s/' % (x['name'], x['grep'][:50]),
                                           re.search(x['grep'], lt, re.M) is not None, lt.strip().splitlines()[-1:] if lt.strip() else None))
                        for name, rx in x.get('regex', {}).items():
                            mt = re.search(rx, lt)
                            if mt:
                                at[name] = float(mt.group(1))
                            checks.append(('%s matches /%s/' % (x['name'], rx[:50]), bool(mt), mt.group(0)[:80] if mt else None))
                    for name, parts in r.get('at_sum', {}).items():
                        if all(p in at for p in parts):
                            at[name] = sum(at[p] for p in parts)
                    for name, (lo, hi) in r.get('must_at', {}).items():
                        v = at.get(name)
                        checks.append(('%s in [%s, %s]' % (name, lo, hi), isinstance(v, float) and lo <= v <= hi, v))
                res['at'] = at
                res['checks'] = [{'check': c, 'ok': bool(o), 'seen': s} for c, o, s in checks]
                res['ok'] = bool(checks) and all(o for _, o, _ in checks)
                rowres[r['name']] = res
                result['rows'].append(res)
                say('row %-22s %s %s' % (r['name'], 'ok' if res['ok'] else 'FAILED: ' + '; '.join(
                    '%s (%s)' % (c, s) for c, o, s in checks if not o), json.dumps(at)[:200]))
        # the cited numbers, re-measured
        for c in cites:
            row = rowres.get(c['row'], {})
            src, v = c['from'], None
            if src == 'seconds':
                v = row.get('seconds')
            elif src.startswith('number:'):
                v = (row.get('numbers') or {}).get(src[7:])
            elif src.startswith('json:'):
                v = row
                for k in src[5:].split('.'):
                    v = v.get(k) if isinstance(v, dict) else None
            elif src.startswith('at:'):
                v = (row.get('at') or {}).get(src[3:])
            elif src.startswith('mcp:'):
                _, pre, field = src.split(':', 2)
                hit = [x for x in row.get('mcp', []) if x['name'].startswith(pre)]
                v = hit[0].get(field) if hit else None
            tol = c.get('tol', 0.25)
            if 'range' in c:   # a cited range (lo, hi), widened by tol on each side
                lo, hi = c['range']
                ok = isinstance(v, (int, float)) and lo * (1 - tol) <= v <= hi * (1 + tol)
            else:
                ok = isinstance(v, (int, float)) and abs(v - c['cited']) <= tol * abs(c['cited'])
            result['cites'].append(dict(c, measured=v, ok=ok))
            say('cite %-40s cited %s %s, measured %s: %s' % (c['what'][:40], c['cited'], c.get('unit', ''), v,
                                                             'ok' if ok else 'NOT REPRODUCED'))
    finally:
        if fly:
            stop_flycast(fly)
        if relay and relay.is_alive():
            relay.stop()
        if bridge:
            subprocess.run(['taskkill', '/PID', str(bridge.pid), '/T', '/F'], capture_output=True, creationflags=0x08000000)
            try:
                bridge.wait(10)
            except subprocess.TimeoutExpired:
                bridge.kill()
        say('flycast, relay and bridge stopped (by pid)')
        if lock:
            os.remove(lock)
        result['ended'] = time.strftime('%Y-%m-%d %H:%M:%S')
        result['boot_to_end_s'] = round(time.time() - t_boot, 1) if t_boot else None
        json.dump(result, open(os.path.join(out, 'fly.json'), 'w', encoding='utf-8'), indent=1, default=str)
        write_md(result, os.path.join(out, 'fly.md'))
    bad = [r for r in result['rows'] if not r['ok']] + [c for c in result['cites'] if not c['ok']]
    want_rows = [r['name'] for r in rows]
    missing = [n for n in want_rows if n not in [x['name'] for x in result['rows']]]
    if missing:
        say('rows not run: %s' % missing)
    return 1 if bad or missing else rc


def write_md(res, path):
    L = ['# Flycast suite (tier: %s)' % res['tier'], '',
         'Flycast `%s`, disc `%s`, ports %s; %s to %s.' % (res['flycast'], res['disc'], res['ports'], res['started'],
                                                         res.get('ended')), '',
         'Guard: %s' % json.dumps(res.get('guard')), '',
         '| row | what | control | exit / state | seconds | verdict | failed checks |', '|---|---|---|---|---|---|---|']
    for r in res['rows']:
        bad = '; '.join('%s (saw %s)' % (c['check'], json.dumps(c['seen'], default=str)[:80]) for c in r['checks'] if not c['ok'])
        es = r.get('rc', r.get('state', ''))
        kind = 'console-only (listed)' if r.get('console_only') else 'yes' if r.get('control') else ''
        L.append('| %s | %s | %s | %s | %s | %s | %s |' % (r['name'], r.get('what', ''), kind,
                                                          es, r.get('seconds'), 'PASS' if r['ok'] else 'FAIL', bad))
    nums = [r for r in res['rows'] if r.get('numbers')]
    if nums:
        L += ['', '| row | numbers (Flycast) |', '|---|---|']
        for r in nums:
            L.append('| %s | %s |' % (r['name'], ', '.join('%s %s' % (k, v) for k, v in r['numbers'].items())))
    if res['cites']:
        L += ['', '## Cited numbers, re-measured (Flycast)', '', '| lane | what | cited | measured | tolerance | verdict |',
              '|---|---|---|---|---|---|']
        for c in res['cites']:
            L.append('| %s | %s | %s %s | %s | %d%% | %s |' % (c.get('lane', ''), c['what'], c['cited'], c.get('unit', ''),
                                                             c['measured'], round(100 * c.get('tol', 0.25)),
                                                             'reproduced' if c['ok'] else 'NOT REPRODUCED'))
    open(path, 'w', encoding='utf-8').write('\n'.join(L) + '\n')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    mc = sub.add_parser('machine')
    mc.add_argument('--cpu-max', type=float, default=85.0)
    mc.add_argument('--cpu-wait', type=float, default=1200.0)
    mc.add_argument('--min-free', nargs='*', default=[])
    r = sub.add_parser('run')
    for k in ('flycast', 'config', 'disc', 'tree', 'build', 'pc_ip', 'out'):
        r.add_argument('--' + k.replace('_', '-'), required=True)
    for k in ('port', 'lport', 'ctl', 'http', 'mcp_http'):
        r.add_argument('--' + k.replace('_', '-'), type=int, required=True)
    r.add_argument('--plan', action='append', required=True)
    r.add_argument('--boot-wait', type=float, default=20.0)
    r.add_argument('--lock')
    r.add_argument('--fixtures', help='test data the rows name as {fixtures} (a lane plan\'s disc_prep makes it)')
    r.add_argument('--only', help='comma-separated row names: a measuring run of only these rows (gate.sh GATE_FLY_ONLY)')
    r.add_argument('--discs', help='a lane\'s own discs (phase "disc"): <dir>/<name>/disc.gdi, built by gate.sh')
    a = ap.parse_args()
    return machine(a) if a.cmd == 'machine' else main_run(a)


if __name__ == '__main__':
    sys.exit(main())
