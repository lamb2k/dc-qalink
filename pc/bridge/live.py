"""live.py: one session's records in the session format (version 1, docs/SESSION_FORMAT.md), the recorder that writes
them to a file as they arrive, and the queries the API answers from them (summary, slowest pictures, trace, log).

Every record carries `t` (its type) and `s` (seconds on the session clock). Run events (push, verdict, exec, line, file,
still, back, reset, link) are record types a session-format-1 reader may ignore.

Version 2 (lane ql-disc, 2026-11-22): the build is the anchor. `meta.build` is the build record (program sha256, name,
commit, disc manifest, loader, args, tier; docs/BRIDGE_API.md "Builds") and every record carries `build` (its id) when
the session has one; `disc` records (mount, open, read: path, off, bytes, ms, side) are the network disc's. A version-1
file is still read as before (no build)."""
import json, os, threading, time

FORMAT = 'dc-qalink.session'
VERSION = 2


class Session:
    KEEP = 200000   # records held in memory per list (round 3, blocker 3); the session's file keeps every record

    def __init__(self, folder, sid, device, tier, label, program=None, build=None):
        self.id = sid
        self.build_id = build['id'] if build else None
        self.t0 = time.time()
        self.lock = threading.RLock()
        self.records = []          # every record but meta, in arrival order
        self.rows = {}             # n -> row record
        self.ctr = {}              # latest counter values
        self.ctr_recs = []
        self.trace = []
        self.logs = []
        self.lines = []            # printed lines {n, text} (n: the latest row's picture when the line arrived)
        self.grabs = []
        self.max_n = 0
        self.trace_seq = None
        self.trace_lost = 0
        self.rec_bytes = 0
        self.ended = None
        self.device = device
        self.meta = {'t': 'meta', 'format': FORMAT, 'version': VERSION, 'id': sid, 'label': label,
                     'program': program or {'name': ''}, 'device': device, 'tier': tier,
                     'tierDetail': 'bridge %s; numbers measured by the program on the %s' % (sid, tier),
                     'source': 'dc-qalink bridge', 'recorded': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                     'rate': 60, 'units': 'ms per picture', 'phases': [], 'counters': [], 'lanes': []}
        if build:
            self.meta['build'] = dict(build)
        self.path = os.path.join(folder, '%s.jsonl' % sid) if folder else None
        self.f = open(self.path, 'w', encoding='utf-8', newline='\n') if self.path else None
        self._write(self.meta)
        self.listeners = []

    # ------------------------------------------------------------------ writing
    def _write(self, r):
        if self.f:
            self.f.write(json.dumps(r, separators=(',', ':')) + '\n')

    def s(self):
        return round(time.time() - self.t0, 3)

    def add(self, r):
        """Adds a record (t set by the caller, s added here), writes it down and tells the live listeners."""
        with self.lock:
            r.setdefault('s', self.s())
            if self.build_id:
                r.setdefault('build', self.build_id)
            self.records.append(r)
            t = r['t']
            if t == 'row':
                self.rows[r['n']] = r
                self.max_n = max(self.max_n, r['n'])
            elif t == 'ctr':
                self.ctr.update(r['v'])
                self.ctr_recs.append(r)
            elif t == 'trace':
                self.trace.append(r)
            elif t == 'log':
                self.logs.append(r)
            elif t == 'line':   # 0.2.1: kept for query_log, at the latest row's picture
                self.lines.append({'n': self.max_n, 'text': r.get('text', '')})
            elif t == 'grab':
                self.grabs.append(r)
            elif t == 'end':
                self.ended = r
            self._trim()
            self._write(r)
            for cb in list(self.listeners):
                cb(r)
        return r

    def _trim(self):
        """Each in-memory list and the row table held at KEEP, the oldest half dropped past it (amortised O(1))."""
        k = self.KEEP
        for lst in (self.records, self.ctr_recs, self.trace, self.logs, self.grabs, self.lines):
            if len(lst) > k:
                del lst[:len(lst) - k // 2]
                self.dropped = getattr(self, 'dropped', 0) + 1
        if len(self.rows) > k:
            for n in sorted(self.rows)[:len(self.rows) - k // 2]:
                del self.rows[n]

    def set_declaration(self, d):
        """The program's LIVEDEF: phases, counters, lanes into meta. A changed declaration is written as a new meta line
        (a reader takes the last meta for names; the first one stays the file's head)."""
        with self.lock:
            m = self.meta
            m['phases'] = [dict(p) for p in d['phases']]
            m['counters'] = [dict(c) for c in d['counters']]
            m['lanes'] = [d['lanes'][k] for k in sorted(d['lanes'])]
            m['rate'] = d['rate']
            if d['program']:
                m['program'] = dict(m['program'], name=d['program'])
            m['label'] = '%s on %s' % (m['program']['name'] or 'program', self.device.get('name', 'device'))
            self._write(dict(m, s=self.s()))
            for cb in list(self.listeners):
                cb(dict(m, s=self.s()))

    def flush(self):
        with self.lock:
            if self.f:
                self.f.flush()

    def close(self, reason=None):
        with self.lock:
            if self.ended is None and self.max_n:
                self.add({'t': 'end', 'n': self.max_n, 'reason': reason or 'program left'})
            if self.f:
                self.f.close()
                self.f = None

    # ------------------------------------------------------------------ queries
    def window(self):
        n = self.max_n
        w0 = min(31, n) if n > 60 else 1   # the first half second is warm-up when there is enough
        return w0, n

    def summary(self, slowest=5):
        with self.lock:
            m = self.meta
            w0, w1 = self.window()
            rows = [self.rows[k] for k in sorted(self.rows) if w0 <= k <= w1]
            missing = sum(1 for k in range(1, self.max_n + 1) if k not in self.rows)
            phases = []
            for p in m['phases']:
                v = [r['ph'][p['key']] for r in rows if p['key'] in r['ph']]
                s = sorted(v)
                worst, at = (max((r['ph'].get(p['key'], -1), r['n']) for r in rows) if rows else (0, 0))
                b = p.get('budget')
                phases.append({'key': p['key'], 'label': p['label'], 'budget': b,
                               'mean': round(sum(v) / len(v), 3) if v else 0,
                               'p95': round(s[int(0.95 * (len(s) - 1))], 3) if s else 0,
                               'p99': round(s[int(0.99 * (len(s) - 1))], 3) if s else 0,
                               'worst': round(worst, 3) if v else 0, 'worstAt': at if v else 0,
                               'over': sum(1 for x in v if b is not None and x > b), 'tier': m['tier']})
            guards = [{'key': c['key'], 'name': c['name'], 'value': self.ctr.get(c['key'], 0), 'tier': m['tier']}
                      for c in m['counters'] if c.get('guard')]
            busy_keys = [p['key'] for p in m['phases'] if not p.get('total')]
            busy = lambda r: sum(r['ph'].get(k, 0) for k in busy_keys)
            slow = sorted(rows, key=lambda r: -busy(r))[:max(1, min(50, slowest))]
            return {'session': self.id, 'label': m['label'], 'device': self.device.get('name', ''), 'tier': m['tier'],
                    'pictures': self.max_n, 'measured': len(rows), 'missing': missing, 'window': [w0, w1],
                    'phases': phases, 'guards': guards, 'trace_lost': self.trace_lost,
                    'slowest': [{'n': r['n'], 'total': round(busy(r), 3), 'phases': r['ph']} for r in slow]}

    def query_trace(self, frm=0, to=None, lane=None, limit=500):
        with self.lock:
            to = self.max_n if to is None else to
            ev = [{'lane': r['lane'], 'n': r['n'], 'd': r.get('d', 0), 'label': r['label']} for r in self.trace
                  if frm <= r['n'] <= to and (not lane or r['lane'] == lane)]
            return {'from': frm, 'to': to, 'events': ev[:limit], 'truncated': len(ev) > limit, 'lost': self.trace_lost}

    def query_log(self, frm=0, to=None, level=None, q=None, limit=500):
        with self.lock:
            to = self.max_n if to is None else to
            q = (q or '').lower()
            ls = [{'n': r['n'], 'lv': r['lv'], 'msg': r['msg']} for r in self.logs
                  if frm <= (r['n'] or 0) <= to and (not level or r['lv'] == level or (level == 'warn' and r['lv'] == 'err'))
                  and (not q or q in r['msg'].lower())]
            # 0.2.1: the program's printed lines too, as level "serial" at the picture they arrived at
            ls = sorted(ls + [{'n': r['n'], 'lv': 'serial', 'msg': r['text']} for r in self.lines
                              if frm <= r['n'] <= to and level in (None, '', 'serial')
                              and (not q or q in r['text'].lower())], key=lambda x: x['n'] or 0)
            return {'from': frm, 'to': to, 'lines': ls[-limit:], 'truncated': len(ls) > limit}

    def info(self):
        with self.lock:
            return {'id': self.id, 'label': self.meta['label'], 'device': self.device.get('name', ''),
                    'started': self.meta['recorded'], 'recorded': self.meta['recorded'],
                    'ended': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()) if self.f is None else None,
                    'pictures': self.max_n, 'tier': self.meta['tier'], 'runs': 0, 'build': self.build_id,
                    'bytes': os.path.getsize(self.path) if self.path and os.path.exists(self.path) else 0}
