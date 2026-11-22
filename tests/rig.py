#!/usr/bin/env python3
"""rig.py: a bridge and the simulated console on free ports on this PC, for other programs' tests (QA Link Cloud's
real-bridge test). Prints one line `bridge http://127.0.0.1:<port>` and runs until its stdin closes.

  python tests/rig.py --out <dir>
"""
import argparse, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_bridge import Rig   # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument('--out', default=os.path.join(HERE, '..', '.rig'))
a = ap.parse_args()
r = Rig(a.out, 'rig')
print('bridge %s' % r.base, flush=True)
try:
    sys.stdin.read()
finally:
    r.close()
