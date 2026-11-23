"""assets.py: the bridge's live-asset endpoints (contract 1, additive; docs/ASSETS.md). Plan L5b.

POST a texture or model (or a path on this PC), the bridge converts it (pc/assets), checks the live-asset budget
(the one QA Link Cloud's Assets workspace shows), stores the DQA1 file and, when asked, sends it to the console viewer
(example/viewer, pushed through the loader), sets the shared camera, grabs the console's picture, renders the same view
on this PC and returns both with the difference and the measured cost. A watched file is converted and sent again when
it is saved. Every round trip is written to <data>/assets/roundtrips.jsonl (with the build record once the bridge keeps
one: ql-disc's L2b item 7).

The converters need numpy (Pillow optional); without it these endpoints answer 501 and the rest of the bridge runs."""
import base64, hashlib, itertools, json, math, os, re, threading, time

import qlsafe   # pc/qlsafe.py: roots and limits (audit 2026-11-23)

ASSET_MAX = 16 << 20   # one imported file (a path or base64), the HTTP body limit

VIEWER_PROGRAM = 'viewer.elf'
STATES = {0: 'none', 1: 'loading', 2: 'ready', 3: 'refused', 4: 'error'}


def _lib():
    from assets import dqa, image, render     # pc/assets (pc/ is on the path: qalink_bridge.py puts it there)
    return dqa, image, render


class Assets:
    def __init__(self, bridge):
        self.b = bridge
        self.dir = os.path.join(bridge.data_dir, 'assets')
        self.views_dir = os.path.join(self.dir, 'views')
        os.makedirs(self.views_dir, exist_ok=True)
        self.recs = {}               # id -> record
        self.order = []
        self.views = {}
        self.view_order = []
        self.seq = itertools.count(int(time.time()) % 100000 * 10 + 1)
        self.lock = threading.RLock()       # one round trip at a time (one console)
        self.viewer_run = None
        self.loaded = None           # (asset id, console seq) the viewer holds
        self.last_view = {}
        self.watches = {}
        self.roundtrips = open(os.path.join(self.dir, 'roundtrips.jsonl'), 'a', encoding='utf-8')
        try:
            _lib()
            self.missing = None
        except ImportError as e:
            self.missing = 'live assets need numpy on the bridge\'s Python (pip install numpy pillow): %s' % e

    # ------------------------------------------------------------------ conversion
    def add(self, name, data, resolve=None, fmt='auto', mips=True, source_path=None):
        dqa, _, _ = _lib()
        t0 = time.time()
        out, rec = dqa.convert(name, data, resolve, fmt=fmt, mips=mips)
        rec['id'] = rec['dqa_sha256'][:12]
        rec['file'] = rec['id'] + '.dqa'
        rec['convert_s'] = round(time.time() - t0, 3)
        rec['source_path'] = source_path
        rec['added'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        rec['console'] = None
        rec['tier'] = 'pc'
        rec['build'] = self.build_id()
        with open(os.path.join(self.dir, rec['file']), 'wb') as f:
            f.write(out)
        with self.lock:
            self.recs[rec['id']] = rec
            if rec['id'] in self.order:
                self.order.remove(rec['id'])
            self.order.append(rec['id'])
        self.b.log('asset %s: %s %s, %d B (ram %d, vram %d) %s in %.2f s' % (rec['id'], rec['kind'], name, rec['bytes'],
                   rec['ram'], rec['vram'], rec['state'], rec['convert_s']))
        return rec

    def build_id(self):
        """The bridge's current build record id when it keeps one (ql-disc's L2b item 7); None before that lands."""
        bid = getattr(self.b, 'build_id', None)       # ql-disc c8a22d7: Bridge.build_id, Bridge.build the record
        if bid:
            return bid
        br = getattr(self.b, 'build', None)
        return br.get('id') if isinstance(br, dict) else None

    # ------------------------------------------------------------------ the viewer on the console
    def viewer_linked(self):
        d = self.b.declaration or {}
        return self.b.client is not None and d.get('program') == VIEWER_PROGRAM

    def viewer_status(self):
        c = self.ctr()
        return {'linked': self.viewer_linked(), 'run': self.viewer_run, 'elf': self.elf(), 'tier': self.b.tier(),
                'loaded': self.loaded[0] if self.loaded and self.viewer_linked() else None,
                'state': STATES.get(c.get('state')) if self.viewer_linked() else None,
                'vfree': c.get('vfree') if self.viewer_linked() else None}

    def elf(self):
        return getattr(self.b, 'viewer_elf', None) or os.environ.get('DCQL_VIEWER_ELF')

    def start_viewer(self, who='api', timeout=90.0, secs=900):
        """Push the viewer through the loader (the bridge's own run queue) and wait until its link is up."""
        if self.viewer_linked():
            return 200, self.viewer_status()
        elf = self.elf()
        if not elf or not os.path.isfile(elf):
            return 501, {'error': 'no viewer program set up (start the bridge with --viewer <viewer.elf>, built from '
                                  'example/viewer)'}
        raw = open(elf, 'rb').read()
        r = self.b.submit(VIEWER_PROGRAM, raw, 'secs=%d' % secs, secs + 60, None, None, who)
        # the viewer's /pc is this store (its run only; registered before the program can ask)
        from dcql_loop import FileService
        self.b.files[r.sid] = FileService(self.dir, self.b.log)
        self.viewer_run = r.id
        self.loaded = None
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.viewer_linked() and (self.b.client_run == r.sid):
                c = self.ctr()
                if 'state' in c or time.time() - t0 > 5:
                    return 200, dict(self.viewer_status(), seconds=round(time.time() - t0, 3))
            if r.state in ('refused', 'lost', 'done', 'crashed', 'timeout') and r.done.is_set():
                return 502, {'error': 'the viewer run ended: %s %s' % (r.state, r.error or '')}
            time.sleep(0.1)
        return 504, {'error': 'the viewer did not link in %.0f s' % timeout, 'run': r.id}

    def ctr(self):
        s = self.b.current
        return dict(s.ctr) if s is not None else {}

    def option(self, name, value):
        code, res = self.b.command({'cmd': 'option', 'name': name, 'value': value}, 'assets')
        if code != 200 or not res.get('ok'):
            raise RuntimeError('viewer option %s=%s: %s' % (name, value, res.get('error')))
        return res

    def wait_ctr(self, pred, timeout):
        t0 = time.time()
        while time.time() - t0 < timeout:
            c = self.ctr()
            if pred(c):
                return c
            if not self.viewer_linked():
                raise RuntimeError('the viewer left')
            time.sleep(0.02)
        raise TimeoutError('the viewer did not answer in %.0f s' % timeout)

    def send(self, aid, timeout=60.0):
        """The asset to the viewer: load=<seq>:<file>, then wait for its state. Returns (state, seconds, why)."""
        rec = self.recs[aid]
        if not rec['fits']:
            return 'refused', 0.0, rec['reason']
        seq = next(self.seq)
        t0 = time.time()
        self.option('load', '%d:%s' % (seq, rec['file']))
        c = self.wait_ctr(lambda c: c.get('asset') == seq and c.get('state') in (2, 3, 4), timeout)
        secs = round(time.time() - t0, 3)
        st = STATES[c['state']]
        why = None
        if st != 'ready':
            s = self.b.current
            logs = [l['msg'] for l in (s.logs if s else []) if ('asset %d ' % seq) in l['msg']]
            why = logs[-1] if logs else 'the viewer said %s' % st
        self.loaded = (aid, seq) if st == 'ready' else None
        rec['console'] = {'state': st, 'send_s': secs, 'why': why, 'tier': self.b.tier(), 'seq': seq}
        rec['state'] = 'live' if st == 'ready' else 'refused'
        self.b.record({'t': 'asset', 'id': aid, 'name': rec['name'], 'state': rec['state'], 'seconds': secs,
                       'why': why})
        return st, secs, why

    def set_view(self, cam, light, bg, filt, view, cull, timeout=10.0):
        _, _, render = _lib()
        o = {'cam': '%.5f,%.5f,%.5f,%.5f' % (cam['yaw'], cam['pitch'], cam['dist'], cam['fov']),
             'light': '%.5f,%.5f,%.5f,%.4f,%.4f' % (tuple(light['dir']) + (light['ambient'], light['diffuse'])),
             'bg': '%02x%02x%02x' % tuple(bg), 'filter': 1 if filt == 'bilinear' else 0,
             'cull': {'none': 0, 'ccw': 1, 'cw': 2}[cull], 'view': view}
        for k, v in o.items():
            if self.last_view.get(k) != v:
                self.option(k, v)
                self.last_view[k] = v
        seq = next(self.seq)
        self.option('seq', seq)
        self.wait_ctr(lambda c: c.get('seq') == seq, timeout)
        return seq, (self.b.current.max_n if self.b.current else 0)

    def cost(self, n0, pictures=60, timeout=10.0):
        """The viewer's own numbers over `pictures` pictures after the view was set: means and worst, ms."""
        t0 = time.time()
        s = self.b.current
        while s is not None and s.max_n < n0 + 4 + pictures and time.time() - t0 < timeout:
            time.sleep(0.05)
        if s is None:
            return None
        with s.lock:
            rows = [s.rows[k] for k in range(n0 + 4, n0 + 4 + pictures) if k in s.rows]
        c = self.ctr()
        out = {'pictures': len(rows), 'window': [n0 + 4, n0 + 3 + pictures], 'tier': self.b.tier(),
               'verts': c.get('verts'), 'strips': c.get('strips'), 'texture_bytes': c.get('txbytes'),
               'vertex_buffer_bytes': c.get('vtxbuf'), 'graphics_free': c.get('vfree')}
        for k, label in (('submit', 'submit_ms'), ('ta', 'ta_ms'), ('render', 'render_ms'), ('frame', 'frame_ms')):
            v = [r['ph'][k] for r in rows if k in r['ph']]
            out[label] = {'mean': round(sum(v) / len(v), 3), 'worst': round(max(v), 3)} if v else None
        return out

    # ------------------------------------------------------------------ the round trip
    def view(self, aid, body, who='api'):
        if self.missing:
            return 501, {'error': self.missing}
        dqa, image, render = _lib()
        rec = self.recs.get(aid)
        if rec is None:
            return 404, {'error': 'no asset %s' % aid}
        cam, light = render.camera_of(body.get('camera')), render.light_of(body.get('light'))
        bg = tuple(int(x) for x in (body.get('bg') or [0, 0, 0]))[:3]
        filt = 'bilinear' if body.get('filter') == 'bilinear' else 'nearest'
        view = body.get('view') or ('flat' if rec['kind'] == 'texture' else 'model')
        cull = body.get('cull') or 'cw'
        if view not in ('model', 'flat', 'mips') or cull not in ('none', 'cw', 'ccw'):
            return 400, {'error': 'view model|flat|mips, cull none|cw|ccw'}
        data = open(os.path.join(self.dir, rec['file']), 'rb').read()
        parsed = dqa.parse(data)
        vid = '%s-%d' % (aid, next(self.seq))
        times = {}
        T = time.time()
        with self.lock:
            t = time.time()
            if not self.viewer_linked():
                if not body.get('start_viewer'):
                    return 409, {'error': 'the viewer is not running: POST /v0/assets/viewer, or start_viewer: true'}
                code, st = self.start_viewer(who)
                if code != 200:
                    return code, st
            times['viewer_s'] = round(time.time() - t, 3)
            t = time.time()
            if not self.loaded or self.loaded[0] != aid:
                st, secs, why = self.send(aid)
                if st != 'ready':
                    return 200, {'id': vid, 'asset': aid, 'state': rec['state'], 'why': why, 'console': rec['console']}
            else:   # already on the console (the same id is the same converted file): a re-upload reset the record to
                rec['state'] = 'live'                       # 'checked'; what the console holds is still this asset
                rec['console'] = rec.get('console') or {'state': 'ready', 'send_s': 0.0, 'why': None,
                                                        'tier': self.b.tier(), 'seq': self.loaded[1]}
            times['send_s'] = round(time.time() - t, 3)
            t = time.time()
            vseq, n0 = self.set_view(cam, light, bg, filt, view, cull)
            times['set_s'] = round(time.time() - t, 3)
            t = time.time()
            measure = max(0, min(600, int(body.get('measure', 60))))   # 0: no cost window (a quicker live edit)
            cost = self.cost(n0, measure) if measure else None
            times['cost_s'] = round(time.time() - t, 3)
            t = time.time()
            code, still = self.b.grab(20.0)
            times['grab_s'] = round(time.time() - t, 3)
            if code != 200:
                return code, dict(still, id=vid, stage='grab')
        t = time.time()
        pc = render.render(parsed, cam, light, bg, filt, view, cull)
        times['render_s'] = round(time.time() - t, 3)
        t = time.time()
        con = image.read_png(open(still['png'], 'rb').read())[..., :3]
        stats, diff = render.difference(pc, con)
        base = os.path.join(self.views_dir, vid)
        image.write_png(base + '-pc.png', pc)
        image.write_png(base + '-diff.png', diff)
        image.write_png(base + '-side.png', render.side_by_side(pc, con, diff))
        times['diff_s'] = round(time.time() - t, 3)
        times['round_trip_s'] = round(times['send_s'] + times['set_s'] + times['grab_s'], 3)
        times['total_s'] = round(time.time() - T, 3)
        v = {'id': vid, 'asset': aid, 'name': rec['name'], 'state': rec['state'], 'camera': cam, 'light': light,
             'bg': list(bg), 'filter': filt, 'view': view, 'cull': cull, 'tier': self.b.tier(), 'times': times,
             'console': {'still': still['id'], 'url': '/v0/stills/%s' % still['id'], 'picture': still.get('picture')},
             'pc': {'url': '/v0/assets/views/%s/pc.png' % vid}, 'diff': dict(stats, url='/v0/assets/views/%s/diff.png' % vid),
             'side': {'url': '/v0/assets/views/%s/side.png' % vid}, 'cost': cost, 'build': self.build_id(),
             'run': self.viewer_run, 'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'by': who,
             'console_still_png': still['png']}
        with self.lock:
            self.views[vid] = v
            self.view_order.append(vid)
            rec['last_view'] = vid
            rec['cost'] = cost
        self.roundtrips.write(json.dumps({k: x for k, x in v.items() if k != 'console_still_png'}) + '\n')
        self.roundtrips.flush()
        self.b.record({'t': 'asset_view', 'id': vid, 'asset': aid, 'diff_over': stats['over'], 'times': times})
        return 200, {k: x for k, x in v.items() if k != 'console_still_png'}

    # ------------------------------------------------------------------ watching files
    def root(self):
        """The folder path imports and watches may read below: --asset-root, else --files; None refuses them."""
        return getattr(self.b, 'asset_root', None) or self.b.files_root

    def confined(self, path):
        """(real path, None) for a file below the asset root, or (None, (code, error))."""
        root = self.root()
        if not root:
            return None, (403, 'path imports need a folder: start the bridge with --asset-root (or --files)')
        real = qlsafe.inside(root, path) if path else None
        if real is None:
            return None, (403 if path and os.path.exists(path) else 404,
                          '%s is not a file below the asset root' % (path or '(no path)'))
        return real, None

    def watch(self, path, body):
        path, why = self.confined(path)
        if path is None:
            return why[0], {'error': why[1]}
        if path in self.watches:
            return 200, {'watching': path, 'asset': self.watches[path]['asset']}
        rec = self.add_path(path, body)
        w = {'asset': rec['id'], 'mtime': os.path.getmtime(path), 'body': body, 'changes': 0, 'stop': threading.Event()}
        self.watches[path] = w
        threading.Thread(target=self._watch, args=(path, w), daemon=True, name='asset-watch').start()
        return 200, {'watching': path, 'asset': rec['id'], 'record': rec}

    def add_path(self, path, body):
        from assets import model
        real, why = self.confined(path)
        if real is None:
            raise PermissionError(why[1])
        data = qlsafe.read_inside(self.root(), real, ASSET_MAX)   # the checked descriptor is the one read
        return self.add(os.path.basename(real), data, model.folder_resolver(os.path.dirname(real), self.root()),
                        body.get('format', 'auto'), body.get('mips', True), path)

    def _watch(self, path, w):
        while not w['stop'].is_set() and not self.b.stop_flag.is_set():
            time.sleep(0.5)
            try:
                m = os.path.getmtime(path)
            except OSError:
                continue
            if m == w['mtime']:
                continue
            time.sleep(0.3)                     # the writer finishing its save
            w['mtime'] = os.path.getmtime(path)
            t0 = time.time()
            try:
                rec = self.add_path(path, w['body'])
                w['asset'] = rec['id']
                w['changes'] += 1
                out = {'t': 'asset_changed', 'path': os.path.basename(path), 'asset': rec['id'], 'state': rec['state']}
                if rec['fits'] and self.viewer_linked() and w['body'].get('send', True):
                    code, v = self.view(rec['id'], dict(w['body'], **(self.last_body or {})), 'watch')
                    out.update(view=v.get('id'), code=code, diff_over=(v.get('diff') or {}).get('over'))
                out['seconds'] = round(time.time() - t0, 3)
                w['last'] = out
                self.b.record(out)
                self.b.log('asset watch: %s changed -> %s (%s) %.2f s' % (path, rec['id'], rec['state'], out['seconds']))
            except Exception as e:
                self.b.log('asset watch %s: %r' % (path, e))
                w['last'] = {'error': str(e)}

    last_body = None


def get(bridge):
    a = getattr(bridge, 'assets', None)
    if a is None:
        a = bridge.assets = Assets(bridge)
    return a


def _decode_body(h):
    """(name, bytes, body) from JSON {name, base64 | path} or multipart (an `asset` file field)."""
    ctype = h.headers.get('Content-Type') or ''
    if ctype.startswith('multipart/form-data'):
        from .api import multipart
        f = multipart(h.body(), ctype)
        if 'asset' not in f:
            raise ValueError('multipart needs an "asset" file')
        fname, raw = f['asset']
        body = {k: v[1].decode('utf-8') for k, v in f.items() if k != 'asset'}
        for k in ('send', 'mips'):
            if k in body:
                body[k] = body[k] not in ('0', 'false', '')
        return body.get('name') or fname, raw, body, None
    body = h.jbody()
    if body.get('path'):
        return os.path.basename(body['path']), None, body, body['path']
    if not body.get('base64') or not body.get('name'):
        raise ValueError('POST /v0/assets: {name, base64} or {path}, or multipart with an "asset" file')
    return body['name'], qlsafe.b64_capped(body['base64'], ASSET_MAX), body, None


def handle(h, bridge, method, p, q):
    """Routes under /v0/assets for api.py's handler `h`. Returns True when it answered."""
    a = get(bridge)
    if a.missing and not (method == 'GET' and p == '/v0/assets'):
        h.err(501, a.missing)
        return True
    if method == 'GET':
        if p == '/v0/assets':
            dq = None if a.missing else _lib()[0]
            h.reply(200, {'assets': [a.recs[i] for i in reversed(a.order)], 'viewer': a.viewer_status(),
                          'budget': None if dq is None else {'ram': dq.BLOCK_BYTES, 'vram': dq.VRAM_BUDGET,
                                                             'vbuf': dq.VBUF_BYTES},
                          'watching': [{'path': k, 'asset': w['asset'], 'changes': w['changes'], 'last': w.get('last')}
                                       for k, w in a.watches.items()], 'error': a.missing})
            return True
        if p == '/v0/assets/views':
            h.reply(200, {'views': [{k: x for k, x in a.views[v].items() if k != 'console_still_png'}
                                    for v in reversed(a.view_order)]})
            return True
        m = re.match(r'^/v0/assets/views/([0-9a-f]{12}-\d+)/(pc|diff|side|console)\.png$', p)
        if m:
            v = a.views.get(m[1])
            if v is None:
                h.err(404, 'no view %s' % m[1])
                return True
            path = v['console_still_png'] if m[2] == 'console' else os.path.join(a.views_dir, '%s-%s.png' % (m[1], m[2]))
            h.reply(200, open(path, 'rb').read(), 'image/png')
            return True
        m = re.match(r'^/v0/assets/([0-9a-f]{12})(/file|/render\.png)?$', p)
        if m:
            rec = a.recs.get(m[1])
            if rec is None:
                h.err(404, 'no asset %s' % m[1])
                return True
            if m[2] == '/file':
                h.reply(200, open(os.path.join(a.dir, rec['file']), 'rb').read(), 'application/octet-stream')
                return True
            if m[2] == '/render.png':
                dq, image, render = _lib()
                f = lambda k, d: float(q[k][0]) if k in q else d
                cam = {'yaw': f('yaw', render.DEFAULT_CAMERA['yaw']), 'pitch': f('pitch', render.DEFAULT_CAMERA['pitch']),
                       'dist': f('dist', render.DEFAULT_CAMERA['dist']), 'fov': f('fov', render.DEFAULT_CAMERA['fov'])}
                parsed = dq.parse(open(os.path.join(a.dir, rec['file']), 'rb').read())
                bg = q.get('bg', ['000000'])[0]
                pic = render.render(parsed, cam, None, (int(bg[0:2], 16), int(bg[2:4], 16), int(bg[4:6], 16)),
                                    q.get('filter', ['nearest'])[0], q.get('view', [None])[0], q.get('cull', ['cw'])[0])
                h.reply(200, image.write_png(None, pic), 'image/png')
                return True
            h.reply(200, rec)
            return True
        return False
    # POST
    who = h.who()
    if p == '/v0/assets':
        if not h.allowed('control'):
            return True
        name, raw, body, path = _decode_body(h)
        if path:
            real, why = a.confined(path)
            if real is None:
                h.err(why[0], why[1])
                return True
            try:
                rec = a.add_path(real, body)
            except qlsafe.Refused as e:
                h.err(413, str(e))
                return True
        else:
            rec = a.add(name, raw, None, body.get('format', 'auto'), body.get('mips', True))
        bridge.audit_call(who, 'control', 'POST /v0/assets %s' % name, '%s %s' % (rec['id'], rec['state']))
        if rec['fits'] and body.get('send') and a.viewer_linked():
            with a.lock:
                st, secs, why = a.send(rec['id'])
        h.reply(200, dict(rec, viewer=a.viewer_status()))
        return True
    if p == '/v0/assets/viewer':
        if not h.allowed('run'):
            return True
        b = h.jbody()
        code, res = a.start_viewer(who, float(b.get('timeout_s') or 90), int(b.get('secs') or 900))
        bridge.audit_call(who, 'run', 'POST /v0/assets/viewer', res.get('error') or 'linked')
        h.reply(code, res)
        return True
    if p == '/v0/assets/watch':
        if not h.allowed('control'):
            return True
        b = h.jbody()
        a.last_body = {k: b[k] for k in ('camera', 'light', 'bg', 'filter', 'view', 'cull') if k in b}
        code, res = a.watch(b.get('path') or '', b)
        bridge.audit_call(who, 'control', 'POST /v0/assets/watch %s' % b.get('path'), res.get('error') or res.get('asset'))
        h.reply(code, res)
        return True
    m = re.match(r'^/v0/assets/([0-9a-f]{12})/view$', p)
    if m:
        b = h.jbody()
        if not h.allowed('run' if b.get('start_viewer') else 'control'):
            return True
        a.last_body = {k: b[k] for k in ('camera', 'light', 'bg', 'filter', 'view', 'cull') if k in b}
        try:
            code, res = a.view(m[1], b, who)
        except (RuntimeError, TimeoutError) as e:
            code, res = 504, {'error': str(e)}
        bridge.audit_call(who, 'control', 'POST /v0/assets/%s/view' % m[1], res.get('error') or res.get('id'))
        h.reply(code, res)
        return True
    return False
