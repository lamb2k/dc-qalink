#!/usr/bin/env python3
"""limits.py: the menu's limits from the link maps and its object (dc-qalink plan section 3). MIT licence.

  limits.py <menu.map> <menu-cl.map> <dcql_menu.o> [--nm sh-elf-nm]   the table; exit 1 if a limit fails
  limits.py --control-symbols <object.o> [--nm ...]                     the symbol check on another object (the
                                                                       control: dcql_net.o must fail it)

Code and constant data (.text, .rodata) and fixed memory (.data, .bss, COMMON) are summed per object from the input
sections the linker kept (the map's memory map, after garbage collection). Shared C library code (snprintf, memmove)
is the game's and is not counted. The symbol check: the menu's object may not refer to the heap, the disc, the
network stack or graphics memory (an allocation or a disc or network call inside a picture would show here first).
"""
import re, subprocess, sys
from collections import defaultdict

FORBIDDEN = re.compile(r'^(malloc|calloc|realloc|free|memalign|_malloc_r|_free_r|pvr_mem_malloc|pvr_mem_free|'
                       r'fs_\w+|open|read|write|cdrom_\w+|gdrom\w*|net_\w+|socket|sendto|recvfrom|bind|connect|'
                       r'thd_create|sem_wait\w*|modem_\w+|bba_\w+)$')
GROUPS = {
    'menu': ['dcql_menu.o'],
    'link (dcql.h, adapter)': ['dcql.o', 'dcql_net.o'],
    'link + live (client library)': ['client.o', 'cl_util.o', 'dcql_return.o', 'dcql_net_bba.o', 'dcql_live.o'],
}


def kind(sec):
    if sec.startswith('.text'):
        return 'code'
    if sec.startswith('.rodata'):
        return 'const'
    if sec.startswith('.data'):
        return 'data'
    if sec.startswith('.bss') or sec.startswith('.sbss') or sec == 'COMMON':
        return 'bss'
    return None


def parse_map(path):
    sizes = defaultdict(lambda: defaultdict(int))
    lines = open(path, encoding='latin1').read().split('Linker script and memory map', 1)[1].splitlines()
    pend = None
    for ln in lines:
        m = re.match(r'^ (\S+)\s+0x([0-9a-f]+)\s+0x([0-9a-f]+)\s+(\S+\.o)\)?$', ln)
        if not m and re.match(r'^ (\.\S+|COMMON)$', ln):
            pend = ln.strip()
            continue
        if not m and pend:
            m2 = re.match(r'^\s+0x([0-9a-f]+)\s+0x([0-9a-f]+)\s+(\S+\.o)\)?$', ln)
            if m2:
                m = (pend, m2.group(1), m2.group(2), m2.group(3))
            pend = None
        elif m:
            m = m.groups()
        if not m:
            continue
        sec, addr, size, obj = m
        k = kind(sec)
        if k and int(addr, 16):
            sizes[obj.split('/')[-1]][k] += int(size, 16)
    return sizes


def symbols(obj, nm):
    und = subprocess.run([nm, '-u', obj], capture_output=True, text=True).stdout.split()
    und = [s.lstrip('_') if s.startswith('_') and not s.startswith('__') else s for s in und if s not in ('U', 'w', 'v')]
    return [s for s in und if FORBIDDEN.match(s)], und


def main():
    nm = 'sh-elf-nm'
    if '--nm' in sys.argv:
        nm = sys.argv[sys.argv.index('--nm') + 1]
    if sys.argv[1] == '--control-symbols':
        bad, _ = symbols(sys.argv[2], nm)
        print('symbol check on %s: %s' % (sys.argv[2], ('FAILS as it must: ' + ' '.join(sorted(set(bad))[:8])) if bad else 'passes (control did not fail!)'))
        return 0 if bad else 1
    a, b, obj = parse_map(sys.argv[1]), parse_map(sys.argv[2]), sys.argv[3]
    fails = 0

    def tot(sz, objs):
        r = defaultdict(int)
        for o in objs:
            for k, v in sz.get(o, {}).items():
                r[k] += v
        return r
    rows = []
    menu = tot(a, GROUPS['menu'])
    menu_cl = tot(b, GROUPS['menu'])
    link = tot(a, GROUPS['link (dcql.h, adapter)'])
    cl = tot(b, GROUPS['link + live (client library)'])
    print('| Part (map) | code | const | data | bss | code + const | fixed (data + bss) |')
    print('|---|---|---|---|---|---|---|')
    for name, t in (('menu (menu.map)', menu), ('menu (menu-cl.map)', menu_cl), ('link, dcql.h on the adapter (menu.map)', link),
                    ('link + live, client library (menu-cl.map)', cl)):
        print('| %s | %d | %d | %d | %d | %d | %d |' % (name, t['code'], t['const'], t['data'], t['bss'], t['code'] + t['const'],
                                                      t['data'] + t['bss']))
    together = {k: cl[k] + menu_cl[k] for k in ('code', 'const', 'data', 'bss')}
    cc, fx = together['code'] + together['const'], together['data'] + together['bss']
    print('| **link + live + menu (menu-cl.map)** | %d | %d | %d | %d | **%d** | **%d** |' % (
        together['code'], together['const'], together['data'], together['bss'], cc, fx))
    print()
    mcc, mfx = menu_cl['code'] + menu_cl['const'], menu_cl['data'] + menu_cl['bss']
    print('menu code + const %d B (%.1f KB) of the 48 KB code limit; with link + live %d B (%.1f KB): %s' % (
        mcc, mcc / 1024, cc, cc / 1024, 'within' if cc <= 48 * 1024 else 'OVER'))
    print('menu fixed memory %d B (%.1f KB); with link + live %d B (%.1f KB) against 72 KB: %s' % (
        mfx, mfx / 1024, fx, fx / 1024, 'within' if fx <= 72 * 1024 else 'OVER (the link was already over; the menu adds %.1f KB)' % (mfx / 1024)))
    if cc > 48 * 1024:
        fails += 1
    bad, und = symbols(obj, nm)
    print('symbol check on dcql_menu.o (%d undefined: %s): %s' % (len(und), ' '.join(sorted(und)), 'FAIL ' + ' '.join(bad) if bad else 'no heap, disc, network or graphics memory'))
    fails += bool(bad)
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
