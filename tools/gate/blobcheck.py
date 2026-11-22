#!/usr/bin/env python3
"""blobcheck.py: what a branch would add to the public repository (dc-qalink 0.2 gate).

  blobcheck.py --repo <worktree> --range <base>..<head> --out <dir>

Every blob reachable from <head> and not from <base> (git rev-list --objects), and every path the range adds or
changes, against four rules; any hit fails:
  1. no blob over 300 KB;
  2. no build outputs (ELF, binaries, objects, archives, maps, disc images: .elf .bin .o .a .map .gdi .cdi .iso .chd
     .raw .img .exe .dll; a file named 1ST_READ.BIN or LOADER.BIN; a build/ folder);
  3. no game data (archives, movies and sound banks a game ships: .afs .pss .sfd .adx .aix .pak .dat .xpk, and any
     path component naming a game's own data, such as re4, bio4 or capcom);
  4. no dot-folder in a path but .github (dot-files such as .gitignore are allowed).
Writes <out>/blob.md and blob.json; exit 0 clean, 1 a rule was broken.
"""
import argparse, json, os, re, subprocess, sys

LIMIT = 300 * 1024
BUILD = re.compile(r'\.(elf|bin|o|a|so|map|gdi|cdi|iso|chd|raw|img|exe|dll|obj|lib)$', re.I)
BUILD_NAMES = re.compile(r'(^|/)(1ST_READ\.BIN|LOADER\.BIN|build/)', re.I)
GAME = re.compile(r'\.(afs|pss|sfd|adx|aix|pak|dat|xpk)$', re.I)
GAME_NAMES = re.compile(r'(^|[/_.\-])(re4|re4dc|bio4|biohazard|capcom)([/_.\-]|$)', re.I)


def git(repo, *a):
    return subprocess.run(['git', '-C', repo] + list(a), capture_output=True, text=True, check=True).stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--repo', required=True)
    ap.add_argument('--range', required=True)
    ap.add_argument('--out', required=True)
    a = ap.parse_args()
    base, head = a.range.split('..')
    objs = git(a.repo, 'rev-list', '--objects', head, '^' + base).splitlines()
    inp = '\n'.join(o.split(' ', 1)[0] for o in objs) + '\n'
    meta = subprocess.run(['git', '-C', a.repo, 'cat-file', '--batch-check=%(objectname) %(objecttype) %(objectsize)'],
                          input=inp, capture_output=True, text=True, check=True).stdout.splitlines()
    names = {o.split(' ', 1)[0]: (o.split(' ', 1)[1] if ' ' in o else '') for o in objs}
    blobs = []
    for line in meta:
        h, t, s = line.split()
        if t == 'blob':
            blobs.append({'sha': h, 'size': int(s), 'path': names.get(h, '')})
    paths = [p for p in git(a.repo, 'diff', '--name-only', '--diff-filter=AMR', base, head).splitlines() if p]
    hits = []
    for b in blobs:
        if b['size'] > LIMIT:
            hits.append(('blob over 300 KB', b['path'], b['size']))
    for p in set(paths) | {b['path'] for b in blobs if b['path']}:
        parts = p.split('/')
        if BUILD.search(p) or BUILD_NAMES.search(p):
            hits.append(('build output', p, None))
        if GAME.search(p) or GAME_NAMES.search(p):
            hits.append(('game data or game name in a path', p, None))
        if any(x.startswith('.') and x != '.github' for x in parts[:-1]):
            hits.append(('dot-folder path', p, None))
    largest = sorted(blobs, key=lambda b: -b['size'])[:5]
    res = {'range': a.range, 'blobs': len(blobs), 'paths_changed': len(paths), 'largest': largest,
           'hits': [{'rule': r, 'path': p, 'size': s} for r, p, s in sorted(set(hits), key=str)]}
    os.makedirs(a.out, exist_ok=True)
    json.dump(res, open(os.path.join(a.out, 'blob.json'), 'w'), indent=1)
    L = ['# Blob check `%s`' % a.range, '', '%d new blobs, %d paths added or changed.' % (len(blobs), len(paths)), '',
         'Largest new blobs: ' + ', '.join('%s %s B' % (b['path'] or b['sha'][:9], format(b['size'], ',')) for b in largest), '',
         '| rule | path | size |', '|---|---|---|']
    L += ['| %s | %s | %s |' % (h['rule'], h['path'], h['size'] or '') for h in res['hits']] or ['| (none) | | |']
    L += ['', 'Verdict: %s' % ('PASS' if not res['hits'] else 'FAIL')]
    open(os.path.join(a.out, 'blob.md'), 'w').write('\n'.join(L) + '\n')
    print('\n'.join(L))
    return 1 if res['hits'] else 0


if __name__ == '__main__':
    sys.exit(main())
