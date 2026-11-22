#!/usr/bin/env python3
"""sizes.py: the console-side limits of plan section 3 (dc-qalink 0.2), measured from a GNU ld link map.

  sizes.py --map liveprog.map --objs client.o,cl_util.o,dcql_return.o,dcql_net_bba.o,dcql_live.o
           [--nm sh-elf-nm --objdir <dir of those objects>] [--stub stub.bin] [--baseline sizes.json]
           --out <dir>

The link, live-data (and, once built, menu) objects a program links, by output section:
  code and constant data = .text + .init + .fini + .rodata + .eh_frame + .gcc_except_table   (row: at most 48 KB)
  fixed memory           = .data + .sdata + .bss + .sbss + COMMON                          (row: at most 72 KB)
Heap: the objects' undefined references to the allocator (malloc, calloc, realloc, free, memalign and their _r
forms), from nm; the plan's row is "no heap use after start", so a reference is listed for reading, not judged here.
The rows are never loosened: a fixed-memory figure over 72 KB is listed as OVER (open, plan section 3) with its change
against the baseline; a code figure over 48 KB fails. Exit 0, or 1 when a row fails.
"""
import argparse, collections, json, os, re, subprocess, sys

CODE = ('.text', '.init', '.fini', '.rodata', '.eh_frame', '.gcc_except_table')
FIXED = ('.data', '.sdata', '.bss', '.sbss')
ROWS = {'code': 48 * 1024, 'fixed': 72 * 1024}
ALLOC = re.compile(r'^\s*U\s+(_?(malloc|calloc|realloc|free|memalign|aligned_alloc|posix_memalign|_malloc_r|_calloc_r|'
                   r'_realloc_r|_free_r|_memalign_r))$')


def parse_map(path, want):
    per = collections.defaultdict(collections.Counter)
    out_sec, pend = None, None
    in_memmap = False
    line_re = re.compile(r'^ (\S+)?\s+0x([0-9a-fA-F]+)\s+0x([0-9a-fA-F]+)\s+(\S+)\s*$')
    for line in open(path, encoding='latin-1'):
        if line.startswith('Linker script and memory map'):
            in_memmap = True
            continue
        if not in_memmap:
            continue
        if line.startswith('.') or (line[:1].isalpha() and not line.startswith(' ')):
            out_sec = line.split()[0]
            pend = None
            continue
        m = re.match(r'^ (\S+)\s*$', line)
        if m and not m.group(1).startswith('*') and not m.group(1).startswith('0x'):
            pend = m.group(1)
            continue
        m = line_re.match(line.rstrip('\n'))
        if not m:
            continue
        sec = m.group(1) or pend
        pend = None
        obj = os.path.basename(m.group(4))
        n = int(m.group(3), 16)
        if obj in want and n and out_sec:
            per[obj][out_sec if sec != 'COMMON' else '.bss'] += n
    return per


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--map', required=True)
    ap.add_argument('--objs', required=True)
    ap.add_argument('--nm', default='sh-elf-nm')
    ap.add_argument('--objdir')
    ap.add_argument('--stub')
    ap.add_argument('--baseline')
    ap.add_argument('--out', required=True)
    ap.add_argument('--report-only', action='store_true',
                    help='a separate program (not the link a game carries): its sizes are listed, not judged against the rows')
    a = ap.parse_args()
    want = a.objs.split(',')
    per = parse_map(a.map, set(want))
    os.makedirs(a.out, exist_ok=True)
    res = {'map': a.map, 'objects': {}, 'rows': []}
    tc = tf = 0
    for o in want:
        c = per.get(o, collections.Counter())
        code = sum(v for k, v in c.items() if k in CODE)
        fixed = sum(v for k, v in c.items() if k in FIXED)
        other = {k: v for k, v in c.items() if k not in CODE + FIXED}
        res['objects'][o] = {'sections': dict(c), 'code': code, 'fixed': fixed, 'other': other}
        tc += code
        tf += fixed
    heap = {}
    if a.objdir:
        for o in want:
            p = os.path.join(a.objdir, o)
            if os.path.isfile(p):
                txt = subprocess.run([a.nm, '-u', p], capture_output=True, text=True).stdout
                heap[o] = [m.group(1) for m in map(ALLOC.match, txt.splitlines()) if m]
    base = json.load(open(a.baseline)) if a.baseline and os.path.isfile(a.baseline) else None
    bt = {r['row']: r['measured'] for r in base['rows']} if base else {}
    fail = False
    for key, val, limit in (('code', tc, ROWS['code']), ('fixed', tf, ROWS['fixed'])):
        over = val > limit
        if a.report_only:
            verdict = 'reported (its own program, not the link row)'
        elif key == 'code':
            verdict = 'FAIL (over the row)' if over else 'PASS'
            fail |= over
        else:
            verdict = 'OVER (open, plan section 3; the row is not loosened)' if over else 'PASS'
        d = val - bt[key] if key in bt else None
        res['rows'].append({'row': key, 'limit': limit, 'measured': val, 'verdict': verdict, 'delta_vs_baseline': d})
    if a.stub and os.path.isfile(a.stub):
        n = os.path.getsize(a.stub)
        res['rows'].append({'row': 'stub', 'limit': 7168, 'measured': n, 'verdict': 'PASS' if n <= 7168 else 'FAIL',
                            'delta_vs_baseline': n - bt['stub'] if 'stub' in bt else None})
        fail |= n > 7168
    res['heap_references'] = heap
    json.dump(res, open(os.path.join(a.out, 'sizes.json'), 'w'), indent=1)
    L = ['# Size rows (plan section 3), from the link map', '', 'Map: `%s` (tier: link map, exact bytes).' % os.path.basename(a.map), '',
         '| row | limit | measured | change against the last v0.2 gate | verdict |', '|---|---|---|---|---|']
    names = {'code': 'Code and constant data, link + live (+ menu when built)',
             'fixed': 'Fixed memory (data + bss: rings, stacks, state)', 'stub': 'Return stub (stub.bin)'}
    for r in res['rows']:
        d = r['delta_vs_baseline']
        L.append('| %s | %s B | %s B | %s | %s |' % (names[r['row']], format(r['limit'], ','), format(r['measured'], ','),
                                                    'no baseline' if d is None else '%+d B' % d, r['verdict']))
    L += ['', '| object | code + const | fixed (data + bss) | sections |', '|---|---|---|---|']
    for o, v in res['objects'].items():
        L.append('| %s | %s | %s | %s |' % (o, format(v['code'], ','), format(v['fixed'], ','),
                                            ' '.join('%s=%d' % kv for kv in sorted(v['sections'].items())
                                                     if kv[0] in CODE + FIXED)))
    L += ['', 'Heap: allocator references (undefined symbols, nm): ' +
          ('; '.join('%s: %s' % (o, ', '.join(h) or 'none') for o, h in heap.items()) if heap else 'not read'),
          '(the row is "no heap use after start": a reference is for reading, the gate does not judge it)']
    open(os.path.join(a.out, 'sizes.md'), 'w').write('\n'.join(L) + '\n')
    print('\n'.join(L))
    return 1 if fail else 0


if __name__ == '__main__':
    sys.exit(main())
