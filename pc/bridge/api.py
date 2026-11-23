"""http.py: contract 1 (docs/BRIDGE_API.md) over HTTP/1.1 on 127.0.0.1, standard library only (ThreadingHTTPServer;
the live stream is Server-Sent Events)."""
import base64, hashlib, io, json, os, queue, re, shutil, threading, time, zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .core import CONTRACT, BRIDGE_VERSION
from . import assets
import qlsafe   # noqa: E402  (the shared boundaries; core put pc/ on the path)

LEVELS = {'read': 0, 'control': 1, 'run': 2}
CORS = {'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Headers': 'Content-Type, Last-Event-ID, X-Contract, X-By',
        'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
        'Access-Control-Expose-Headers': 'Content-Disposition, X-Report-CRC-OK'}
MAX_BODY = 16 << 20
PLUGIN_MAX = 4 << 20            # one decoder plugin (an ES module)
LOADER_DISC_MAX = 64 << 20      # the loader disc folder served as a ZIP (a loader's disc is a few MiB)


def multipart(body, ctype):
    """{name: (filename or None, bytes)} from a multipart/form-data body."""
    m = re.search(r'boundary="?([^";]+)"?', ctype or '')
    if not m:
        raise ValueError('multipart without a boundary')
    sep = b'--' + m.group(1).encode('latin-1')
    out = {}
    for part in body.split(sep)[1:]:
        if part.startswith(b'--'):
            break
        part = part[2:] if part.startswith(b'\r\n') else part
        head, _, data = part.partition(b'\r\n\r\n')
        if data.endswith(b'\r\n'):
            data = data[:-2]
        h = head.decode('latin-1')
        nm = re.search(r'name="([^"]*)"', h)
        fn = re.search(r'filename="([^"]*)"', h)
        if nm:
            out[nm.group(1)] = (fn.group(1) if fn else None, data)
    return out


def make_handler(bridge, level='run'):
    class H(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        server_version = 'dc-qalink-bridge/0.2'

        def log_message(self, fmt, *args):
            pass

        # ------------------------------------------------------------ replies
        def reply(self, code, body, ctype='application/json', extra=None):
            if ctype == 'application/json':
                if isinstance(body, dict):
                    body = dict(body, contract=CONTRACT)
                data = json.dumps(body).encode('utf-8')
            else:
                data = body if isinstance(body, bytes) else body.encode('utf-8')
            self.send_response(code)
            for k, v in dict(CORS, **(extra or {})).items():
                self.send_header(k, v)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def err(self, code, msg, **kw):
            self.reply(code, dict({'error': msg}, **kw))

        def body(self):
            """The request body, read once. Every POST reads it (do_POST drains it first): an unread body on a
            keep-alive connection became the start of the next request, so the app's CORS preflight after
            POST /v0/selftest got a 400 with no CORS headers (Flycast run f4, 2026-11-22)."""
            if getattr(self, '_body', None) is None:
                cl_ = (self.headers.get('Content-Length') or '0').strip()
                # a length is digits only and at most MAX_BODY (a negative or signed one read to the end of the
                # stream: audit 2026-11-23, item 3); refused before any read, and the connection closed
                if not cl_.isdigit() or int(cl_) > MAX_BODY:
                    self.close_connection = True
                    self._body = b''
                    raise ValueError('Content-Length %r refused (0..%d bytes)' % (cl_[:20], MAX_BODY))
                n = int(cl_)
                self._body = self.rfile.read(n) if n else b''
            return self._body

        def jbody(self):
            b = self.body()
            return json.loads(b.decode('utf-8')) if b else {}

        def who(self):
            return self.headers.get('X-By') or 'local %s' % self.client_address[0]

        def allowed(self, need):
            if LEVELS[need] > LEVELS[level]:
                self.err(403, 'this bridge was started at level %s; %s needs %s' % (level, self.path, need))
                return False
            return True

        def contract_ok(self, q):
            want = self.headers.get('X-Contract') or (q.get('contract') or [None])[0]
            if want is not None and str(want).split('.')[0] != str(CONTRACT):
                self.err(409, 'contract mismatch: this bridge speaks contract %d, the client %s' % (CONTRACT, want),
                         theirs=want, ours=CONTRACT)
                return False
            return True

        def do_OPTIONS(self):
            self.send_response(204)
            for k, v in CORS.items():
                self.send_header(k, v)
            self.send_header('Content-Length', '0')
            self.end_headers()

        # ------------------------------------------------------------ routes
        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            p = u.path.rstrip('/') or '/'
            if not self.contract_ok(q):
                return
            qi = lambda k, d=None: int(q[k][0]) if k in q and q[k][0] != '' else d
            try:
                if p == '/v0/status':
                    return self.reply(200, bridge.status())
                if p == '/v0/device':
                    return self.reply(200, bridge.device())
                if p == '/v0/devices':
                    return self.reply(200, {'devices': [bridge.device()] if bridge.loop.hello else []})
                if p == '/v0/discovery':
                    return self.reply(200, {'listening': True, 'port': bridge.t.port, 'seen': bridge.t.discovery()})
                if p == '/v0/live':
                    return self.live(q)
                if p == '/v0/runs':
                    tier, dn = bridge.tier(), bridge.device()['name']
                    return self.reply(200, {'runs': [bridge.runs[i].json(tier, dn) for i in reversed(bridge.run_order)]})
                m = re.match(r'^/v0/runs/([0-9a-f]{8})(/report|/serial)?$', p)
                if m:
                    r = bridge.runs.get(m[1])
                    if r is None:
                        return self.err(404, 'no run %s' % m[1])
                    if m[2] == '/report':
                        if r.report is None:
                            return self.err(404, 'run %s has no report (state %s)' % (r.id, r.state))
                        return self.reply(200, r.report, 'text/plain; charset=latin-1',
                                          {'X-Report-CRC-OK': '1' if r.report_crc_ok else '0'})
                    if m[2] == '/serial':
                        out, prev = [], None
                        for seq, text in r.serial:
                            if prev is not None and seq > prev + 1:
                                out.append('[gap: %d lines lost]\n' % (seq - prev - 1))
                            out.append(text if text.endswith('\n') else text + '\n')
                            prev = seq
                        return self.reply(200, ''.join(out), 'text/plain; charset=latin-1')
                    j = r.json(bridge.tier(), bridge.device()['name'])
                    j['report'] = r.report.decode('latin-1') if r.report is not None else None
                    return self.reply(200, j)
                m = re.match(r'^/v0/stills/([0-9a-f]{8}-\d+)(\.json|\.png)?$', p)
                if m:
                    j = bridge.stills.meta.get(m[1])
                    if j is None:
                        return self.err(404, 'no still %s' % m[1])
                    if m[2] == '.json':
                        return self.reply(200, {k: v for k, v in j.items() if k != 'png'})
                    if not j['png']:
                        return self.err(410, 'still %s failed its CRC; no picture' % m[1])
                    return self.reply(200, open(j['png'], 'rb').read(), 'image/png')
                if p == '/v0/stills':
                    return self.reply(200, {'stills': [{k: v for k, v in j.items() if k != 'png'}
                                                       for j in bridge.stills.meta.values()]})
                if p == '/v0/sessions':
                    return self.reply(200, {'sessions': self.sessions()})
                m = re.match(r'^/v0/sessions/([\w.-]+)$', p)
                if m:
                    path = os.path.join(bridge.sessions_dir, m[1] + '.jsonl')
                    if not os.path.isfile(path):
                        return self.err(404, 'no session %s' % m[1])
                    s = bridge.sessions.get(m[1])
                    if s is not None:
                        s.flush()
                    return self.reply(200, open(path, 'rb').read(), 'application/x-ndjson')
                s = bridge.current
                if p == '/v0/summary':
                    if s is None:
                        return self.err(404, 'no session yet: run a program that sends records')
                    su = s.summary(qi('slowest', 5))
                    return self.reply(200, su)
                if p == '/v0/trace':
                    if s is None:
                        return self.reply(200, {'from': 0, 'to': 0, 'events': [], 'truncated': False})
                    return self.reply(200, s.query_trace(qi('from', 0), qi('to'), (q.get('lane') or [None])[0],
                                                         max(1, min(5000, qi('limit', 500)))))
                if p == '/v0/log':
                    if s is None:
                        return self.reply(200, {'from': 0, 'to': 0, 'lines': [], 'truncated': False})
                    return self.reply(200, s.query_log(qi('from', 0), qi('to'), (q.get('level') or [None])[0],
                                                       (q.get('q') or [None])[0], max(1, min(5000, qi('limit', 500)))))
                if p == '/v0/presets':
                    d = bridge.declaration or {}
                    return self.reply(200, {'program': d.get('program', ''), 'presets': d.get('presets', []),
                                            'options': d.get('options', []), 'scripts': [],
                                            'linked': bridge.client is not None})
                if p == '/v0/loader':
                    return self.loader()
                # builds and the network disc (lane ql-disc)
                if p == '/v0/builds':
                    return self.reply(200, {'builds': bridge.build_list(), 'current': bridge.build_id})
                m = re.match(r'^/v0/builds/(b[0-9a-f]{12})$', p)
                if m:
                    b = bridge.builds.get(m[1])
                    return self.reply(200, b) if b else self.err(404, 'no build %s' % m[1])
                if p == '/v0/disc':
                    if bridge.disc is None:
                        return self.reply(200, {'mode': None, 'files': [], 'note': 'no network disc: start the bridge '
                                                'with --disc <folder or .gdi/.iso> and/or --overlay <folder>'})
                    return self.reply(200, dict(bridge.disc.manifest(), stats=bridge.disc.summary()))
                if p == '/v0/plugins':
                    d = bridge.asset_plugins
                    names = sorted(n for n in os.listdir(d) if n.endswith(('.js', '.mjs'))) if d and os.path.isdir(d) else []
                    return self.reply(200, {'plugins': [{'name': n, 'url': '/v0/plugins/' + n} for n in names],
                                            'folder': bool(d)})
                m = re.match(r'^/v0/plugins/([\w.-]+\.m?js)$', p)
                if m:
                    d = bridge.asset_plugins
                    try:   # opened, then checked on the opened object: a plugin linked out of its folder is refused
                        with qlsafe.open_confined(d, m[1]) as fh:
                            js = fh.read(PLUGIN_MAX + 1)
                    except qlsafe.Refused:
                        return self.err(404, 'no plugin %s' % m[1])
                    if len(js) > PLUGIN_MAX:
                        return self.err(413, 'plugin %s is over %d bytes' % (m[1], PLUGIN_MAX))
                    return self.reply(200, js, 'text/javascript; charset=utf-8')
                if p == '/v0/disc/reads':
                    if bridge.disc is None:
                        return self.reply(200, {'reads': []})
                    lim = max(1, min(5000, qi('limit', 500)))
                    return self.reply(200, {'reads': bridge.disc.reads[-lim:], 'total': len(bridge.disc.reads)})
                if p == '/v0/disc/file':
                    if bridge.disc is None:
                        return self.err(404, 'no network disc')
                    path = (q.get('path') or [''])[0]
                    try:
                        data = bridge.disc.file_bytes(path)
                    except FileNotFoundError:
                        return self.err(404, 'no file %s on the served disc' % path)
                    return self.reply(200, data, 'application/octet-stream')
                if p == '/v0/calls':
                    return self.reply(200, {'note': 'see calls.log in the bridge data folder'})
                if p.startswith('/v0/assets') and assets.handle(self, bridge, 'GET', p, q):   # docs/ASSETS.md
                    return
                return self.err(404, 'no route %s' % p)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                bridge.log('GET %s: %r' % (p, e))
                try:
                    self.err(500, '%s: %s' % (type(e).__name__, e))
                except OSError:
                    pass

        def do_POST(self):
            self._body = None
            try:
                self.body()
            except ValueError as e:
                return self.err(413, str(e))
            u = urlparse(self.path)
            q = parse_qs(u.query)
            p = u.path.rstrip('/')
            if not self.contract_ok(q):
                return
            who = self.who()
            try:
                if p == '/v0/runs':
                    if not self.allowed('run'):
                        return
                    try:
                        r = self.push(who)
                    except qlsafe.Refused as e:   # the run queue is full (count or bytes): try again later
                        return self.err(429, str(e))
                    if r is None:
                        return
                    bridge.audit_call(who, 'run', 'POST /v0/runs %s' % r.name, r.id)
                    return self.reply(202, r.json(bridge.tier(), bridge.device()['name']))
                if p == '/v0/selftest':
                    if not self.allowed('run'):
                        return
                    code, r = bridge.selftest(who)
                    bridge.audit_call(who, 'run', 'POST /v0/selftest', code)
                    return self.reply(code, r if isinstance(r, dict) else r.json(bridge.tier(), bridge.device()['name']))
                if p == '/v0/command':
                    c = self.jbody()
                    need = 'control'
                    if not self.allowed(need):
                        return
                    code, res = bridge.command(c, who)
                    bridge.audit_call(who, need, 'command %s' % json.dumps(c)[:200], res)
                    return self.reply(code, res)
                if p in ('/v0/reset', '/v0/power'):
                    if not self.allowed('control'):
                        return
                    code, res = bridge.reset(self.jbody().get('level', 'return')) if p == '/v0/reset' else bridge.power()
                    bridge.audit_call(who, 'control', p, res)
                    return self.reply(code, res)
                if p == '/v0/stills':
                    if not self.allowed('control'):
                        return
                    code, res = bridge.grab(max(1.0, min(60.0, float(self.jbody().get('timeout_s') or 20))))
                    bridge.audit_call(who, 'control', 'POST /v0/stills', res.get('id') or res.get('error'))
                    return self.reply(code, {k: v for k, v in res.items() if k != 'png'})
                if p == '/v0/link':
                    if not self.allowed('control'):
                        return
                    b = self.jbody()
                    ip = b.get('console_ip') or b.get('address') or (b.get('device') or '').replace('dc-', '').replace('-', '.')
                    try:
                        bridge.t.choose(ip)
                    except KeyError:
                        return self.err(404, 'no loader heard at %s' % ip)
                    except PermissionError as e:
                        bridge.audit_call(who, 'control', 'POST /v0/link %s' % ip, 'refused: only local')
                        return self.err(403, str(e))
                    bridge.audit_call(who, 'control', 'POST /v0/link %s' % ip, 'ok')
                    return self.reply(200, bridge.status())
                if p == '/v0/settings':
                    if not self.allowed('control'):
                        return
                    b = self.jbody()
                    if not isinstance(b.get('autoconnect'), bool):
                        return self.err(400, 'settings: {"autoconnect": true|false}')
                    bridge.audit_call(who, 'control', 'POST /v0/settings autoconnect=%s' % b['autoconnect'], 'ok')
                    return self.reply(200, bridge.set_autoconnect(b['autoconnect']))
                if p == '/v0/memory/read':
                    return self.err(501, 'memory reads are not in this loader yet')
                if p.startswith('/v0/assets') and assets.handle(self, bridge, 'POST', p, q):   # docs/ASSETS.md
                    return
                return self.err(404, 'no route %s' % p)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except (ValueError, KeyError) as e:
                self.err(400, str(e))
            except Exception as e:
                bridge.log('POST %s: %r' % (p, e))
                self.err(500, '%s: %s' % (type(e).__name__, e))

        # ------------------------------------------------------------ pieces
        def push(self, who):
            ctype = self.headers.get('Content-Type') or ''
            body = self.body()
            if ctype.startswith('multipart/form-data'):
                f = multipart(body, ctype)
                if 'program' not in f:
                    self.err(400, 'multipart needs a "program" file')
                    return None
                name, raw = f['program']
                field = lambda k, d='': f[k][1].decode('utf-8') if k in f else d
                sha, args = field('sha256'), field('args')
                deadline, files, reset_after = field('deadline_s', '0'), field('files', '') or None, field('reset_after_s')
                name = field('name') or name or 'program.elf'
                commit = field('commit') or None   # ql-disc: the build record's git commit, when the pusher knows it
            else:   # application/octet-stream with headers (scripts)
                raw, name = body, self.headers.get('X-Build-Name') or 'program.elf'
                sha, args = self.headers.get('X-Build-Sha256', ''), self.headers.get('X-Args', '')
                deadline, files, reset_after = self.headers.get('X-Deadline-S', '0'), None, None
                commit = self.headers.get('X-Build-Commit') or None
            got = hashlib.sha256(raw).hexdigest()
            if not sha:
                self.err(400, 'a push needs the sha256 of the program (the bridge takes builds only with a matching hash)')
                return None
            if sha.lower() != got:
                self.err(400, 'sha256 does not match the bytes received', received=got, declared=sha)
                return None
            if files and ('/' in files or '\\' in files or '..' in files):
                self.err(400, 'files names a served folder the bridge knows, never a path')
                return None
            if files and (not bridge.files_root or qlsafe.confine(bridge.files_root, files, want='dir') is None):
                # audit round 2: a run asking for a subfolder gets it or is refused, never the whole /pc root
                self.err(400, 'files: %r is not a folder below the served /pc root' % files)
                return None
            if commit and not re.match(r'^[0-9a-fA-F]{7,40}$', commit):
                self.err(400, 'commit: a git commit id (7 to 40 hex digits)')
                return None
            return bridge.submit(name, raw, args, int(float(deadline or 0)), files,
                                 float(reset_after) if reset_after else None, who, commit)

        def sessions(self):
            out = []
            for fn in sorted(os.listdir(bridge.sessions_dir), reverse=True):
                if not fn.endswith('.jsonl'):
                    continue
                sid = fn[:-6]
                s = bridge.sessions.get(sid)
                if s is not None:
                    out.append(s.info())
                    continue
                path = os.path.join(bridge.sessions_dir, fn)
                try:
                    with open(path, encoding='utf-8') as fh:
                        meta = json.loads(fh.readline())
                except (OSError, ValueError):
                    continue
                out.append({'id': sid, 'label': meta.get('label'), 'device': (meta.get('device') or {}).get('name'),
                            'started': meta.get('recorded'), 'recorded': meta.get('recorded'), 'tier': meta.get('tier'),
                            'pictures': None, 'bytes': os.path.getsize(path), 'version': meta.get('version', 1),
                            'build': (meta.get('build') or {}).get('id')})   # ql-disc: format 2 names its build
            return out

        def loader(self):
            src = bridge.loader_disc
            if not src or not os.path.isdir(src):
                return self.err(501, 'no loader disc set up (start the bridge with --loader-disc <folder with disc.gdi>)')
            buf = io.BytesIO()
            total = 0
            with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
                for fn in sorted(os.listdir(src)):
                    try:   # each file opened, then checked on the opened object (links out of the folder refused)
                        fh = qlsafe.open_confined(src, fn)
                    except qlsafe.Refused:
                        continue
                    with fh:
                        total += os.fstat(fh.fileno()).st_size
                        if total > LOADER_DISC_MAX:
                            return self.err(413, 'the loader disc folder is over %d bytes' % LOADER_DISC_MAX)
                        with z.open(fn, 'w') as out:
                            shutil.copyfileobj(fh, out, 1 << 20)
            return self.reply(200, buf.getvalue(), 'application/zip',
                              {'Content-Disposition': 'attachment; filename="dc-qalink-loader-disc.zip"'})

        def live(self, q):
            """Server-Sent Events. First `hello` (contract, session, device, meta), then the current session's records
            from after picture `from` (or Last-Event-ID), then everything as it arrives. A row's event id is its picture,
            so a reconnect resumes with no repeat and a hole stays a gap."""
            frm = q.get('from', [None])[0] or self.headers.get('Last-Event-ID')
            frm = int(frm) if frm and str(frm).isdigit() else 0
            want_session = (q.get('session') or [None])[0]
            sub = queue.Queue(maxsize=20000)
            with bridge.listeners_lock:
                bridge.listeners.add(sub)
            try:
                self.send_response(200)
                for k, v in CORS.items():
                    self.send_header(k, v)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Cache-Control', 'no-cache')
                self.send_header('X-Accel-Buffering', 'no')
                self.end_headers()
                s = bridge.current
                if s is not None and want_session and want_session != s.id:
                    frm = 0
                hello = {'t': 'hello', 'contract': CONTRACT, 'bridge': BRIDGE_VERSION, 's': s.s() if s else 0.0,
                         'session': s.id if s else None, 'device': bridge.device(), 'meta': dict(s.meta) if s else None,
                         'resume_from': frm, 'link': bridge.link_state}
                self.wfile.write(self.sse(hello))
                if s is not None:
                    with s.lock:
                        back = [r for r in s.records if r.get('n') is None or (r['n'] or 0) > frm or r['t'] == 'meta']
                    buf = bytearray()
                    for r in back:
                        buf += self.sse(r)
                        if len(buf) > 65536:
                            self.wfile.write(bytes(buf))
                            buf.clear()
                    self.wfile.write(bytes(buf))
                self.wfile.flush()
                last_beat = time.time()
                while not bridge.stop_flag.is_set():
                    try:
                        r = sub.get(timeout=1.0)
                    except queue.Empty:
                        if time.time() - last_beat > 10:
                            self.wfile.write(b': keep-alive\n\n')
                            self.wfile.flush()
                            last_beat = time.time()
                        continue
                    buf = bytearray(self.sse(r))
                    while not sub.empty() and len(buf) < 65536:
                        buf += self.sse(sub.get_nowait())
                    self.wfile.write(bytes(buf))
                    self.wfile.flush()
                    last_beat = time.time()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass
            finally:
                with bridge.listeners_lock:
                    bridge.listeners.discard(sub)
                self.close_connection = True

        @staticmethod
        def sse(r):
            head = 'id: %d\n' % r['n'] if r.get('t') == 'row' else ''
            return (head + 'data: ' + json.dumps(r, separators=(',', ':')) + '\n\n').encode('utf-8')
    return H


def serve(bridge, host='127.0.0.1', port=52080, level='run'):
    if host not in ('127.0.0.1', 'localhost', '::1'):
        raise ValueError('the bridge serves this PC only (127.0.0.1)')
    srv = ThreadingHTTPServer((host, port), make_handler(bridge, level))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True, name='bridge-http').start()
    bridge.log('HTTP contract %d on http://%s:%d (level %s)' % (CONTRACT, host, srv.server_address[1], level))
    return srv
