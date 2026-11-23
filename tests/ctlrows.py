"""ctlrows.py: pinned rows and historical controls for the host tests (dc-qalink 0.2, lane ql-fix-pc round 2, after the
audit of 2026-11-23). Standard library only.

A test names every row it must produce (PINNED: row id -> the discrepancy the old tree must show, or None for a row
that only runs on the new tree, because the old tree has no such helper). Then:

  positive run   every pinned row must be present and PASS; a pinned row that is missing is a FAIL of its own
  control run    (--controls --pc <old pc/>) every row with an expected discrepancy must FAIL on the old tree with
                 exactly that discrepancy. A row that passes, raises (a missing helper, an import or setup error) or
                 fails another way is a control FAIL; so is a missing row. Rows with no expected discrepancy are not
                 run on the old tree.

A row's function returns (ok, found, details): `found` is a short tag of the behaviour seen (on the old tree it is the
discrepancy, e.g. "bound 0.0.0.0")."""
import json, os, time


class Rows:
    def __init__(self, pinned, controls=False):
        self.pinned = pinned
        self.controls = controls
        self.results = {}
        self.t0 = time.time()

    def row(self, rid, name, f):
        if rid not in self.pinned:
            raise KeyError('row %s is not pinned' % rid)
        if self.controls and self.pinned[rid] is None:
            return   # a new-tree-only row (the old tree has no such helper): not run as a control
        try:
            ok, found, kw = f()
            status = 'pass' if ok else 'fail'
        except Exception as e:   # recorded as an error: never a control's pass
            ok, found, kw, status = False, 'error', {'error': '%s: %s' % (type(e).__name__, e)}, 'error'
        self.results[rid] = {'id': rid, 'name': name, 'status': status, 'found': found, **kw}
        print('%-24s %-72s %s found=%s %s' % (rid, name[:72], status.upper(), found,
                                              json.dumps(kw, default=str)[:160]), flush=True)

    def verdict(self, label, out=None):
        """Prints the summary rows; returns the exit code (0 only when every pinned check holds)."""
        bad = []
        if self.controls:
            exp = {k: v for k, v in self.pinned.items() if v is not None}
            for rid, want in sorted(exp.items()):
                r = self.results.get(rid)
                if r is None:
                    why = 'missing row'
                elif r['status'] == 'error':
                    why = 'raised (%s): an error is not the expected failure' % r.get('error', '')[:100]
                elif r['status'] == 'pass':
                    why = 'passed on the old tree'
                elif r['found'] != want:
                    why = 'failed with %r, not the expected %r' % (r['found'], want)
                else:
                    why = None
                print('control %s (on %s: must fail with "%s") %s%s' % (rid, label, want, 'FAIL' if why else 'PASS',
                                                                       ': ' + why if why else ''))
                if why:
                    bad.append(rid)
            print('controls: %d of %d pinned rows failed as they must on %s' % (len(exp) - len(bad), len(exp), label))
        else:
            for rid in sorted(self.pinned):
                r = self.results.get(rid)
                if r is None or r['status'] != 'pass':
                    bad.append(rid)
                    if r is None:
                        print('pinned %s MISSING: the row did not run (a FAIL)' % rid)
            print('pinned: %d of %d rows passed in %.1f s%s' % (len(self.pinned) - len(bad), len(self.pinned),
                                                               time.time() - self.t0,
                                                               '' if not bad else '; FAILED: ' + ', '.join(bad)))
        self.bad = bad
        if out:
            os.makedirs(out, exist_ok=True)
            json.dump({'label': label, 'controls': self.controls, 'pinned': self.pinned,
                       'results': list(self.results.values()), 'failed': bad},
                      open(os.path.join(out, 'rows-%s.json' % ('controls' if self.controls else 'positive')), 'w'),
                      indent=1, default=str)
        return 1 if bad else 0


def self_check():
    """The harness's own control: an error, a pass, a wrong discrepancy and a missing row are each a control FAIL;
    the expected discrepancy is a PASS; a missing pinned row fails a positive run. True when all hold."""
    import contextlib, io
    pinned = {'a': 'x', 'b': 'x', 'c': 'x', 'd': 'x', 'e': 'x', 'f': None}
    r = Rows(pinned, controls=True)
    with contextlib.redirect_stdout(io.StringIO()):
        r.row('a', 'expected', lambda: (False, 'x', {}))
        r.row('b', 'raises', lambda: (_ for _ in ()).throw(AttributeError('no helper')))
        r.row('c', 'passes', lambda: (True, 'ok', {}))
        r.row('d', 'other', lambda: (False, 'y', {}))
        r.row('f', 'new only', lambda: 1 / 0)
        rc = r.verdict('self')
    ctl_ok = rc == 1 and 'f' not in r.results and sorted(r.bad) == ['b', 'c', 'd', 'e']
    p = Rows({'a': None, 'b': None})
    with contextlib.redirect_stdout(io.StringIO()):
        p.row('a', 'pass', lambda: (True, 'ok', {}))
        prc = p.verdict('self')
    return ctl_ok and prc == 1 and p.bad == ['b']
