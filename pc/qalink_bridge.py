#!/usr/bin/env python3
"""qalink_bridge.py: start dc-qalink's bridge (Python 3.10+, standard library; the MCP part needs the `mcp` package).

  python qalink_bridge.py [--port 52080] [--data DIR] [--udp-port 52060] [--console auto|IP[:PORT]]
                          [--device flycast|console] [--files ROOT] [--selftest testprog.elf] [--loader-disc DIR]
                          [--level read|control|run] [--mcp-http PORT]
  python qalink_bridge.py --mcp stdio [--bridge http://127.0.0.1:52080] [--mcp-level run]   the local MCP over stdio
                                                                     (for Claude Code / Codex; a client of a running bridge)

Runs with `python -I` (the desktop app starts it that way): it puts its own folder on the path itself.
"""
import argparse, os, signal, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--port', type=int, default=52080, help='HTTP port on 127.0.0.1 (default 52080)')
    ap.add_argument('--data', default=os.path.join(os.path.expanduser('~'), '.dc-qalink', 'bridge'),
                    help='sessions, stills, reports, bridge.log, calls.log')
    ap.add_argument('--udp-host', default='0.0.0.0')
    ap.add_argument('--udp-port', type=int, default=52060, help='the loader\'s port, bound while the bridge runs')
    ap.add_argument('--console', default='auto', help='auto (the first loader heard) or the console\'s address')
    ap.add_argument('--device', choices=('flycast', 'console', 'mock'), help='the tier of numbers (default: guessed)')
    ap.add_argument('--files', help='folder served to programs as /pc/ (a run may name a subfolder)')
    ap.add_argument('--selftest', help='the self-test program (dc-qalink\'s testprog.elf)')
    ap.add_argument('--loader-disc', help='folder of the loader disc image served by /v0/loader')
    ap.add_argument('--level', choices=('read', 'control', 'run'), default='run', help='the most this bridge accepts')
    ap.add_argument('--mcp', choices=('stdio',), help='serve the MCP tools over stdio as a client of --bridge')
    ap.add_argument('--mcp-http', type=int, help='also serve the MCP tools over HTTP on 127.0.0.1:<port>')
    ap.add_argument('--mcp-level', choices=('read', 'control', 'run'), default='run')
    ap.add_argument('--bridge', default='http://127.0.0.1:52080', help='the bridge the stdio MCP talks to')
    ap.add_argument('--quiet', action='store_true')
    a = ap.parse_args(argv)
    if a.mcp == 'stdio':
        from bridge import mcp_server
        return mcp_server.run_stdio(a.bridge, a.mcp_level)
    from bridge.core import Bridge
    from bridge.api import serve
    log = None if a.quiet else (lambda s: print(s, flush=True))
    b = Bridge(a.data, (a.udp_host, a.udp_port), a.console, a.device, a.files, a.selftest, a.loader_disc, a.level, log)
    serve(b, '127.0.0.1', a.port, a.level)
    if a.mcp_http:
        from bridge import mcp_server
        mcp_server.start_http('http://127.0.0.1:%d' % a.port, a.mcp_http, a.mcp_level, b.log)
    stop = []
    signal.signal(signal.SIGINT, lambda *x: stop.append(1))
    if hasattr(signal, 'SIGTERM'):
        signal.signal(signal.SIGTERM, lambda *x: stop.append(1))
    try:
        while not stop:
            time.sleep(0.5)
    finally:
        b.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
