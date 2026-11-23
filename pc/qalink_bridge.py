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
    ap.add_argument('--viewer', help='the console asset viewer (example/viewer\'s viewer.elf) for /v0/assets')
    # the network disc (lane ql-disc): a program that mounts lib/dcql_disc reads /cd from these
    ap.add_argument('--disc', help='the network disc\'s base: the built disc folder, or an image (.gdi with its tracks, .iso)')
    ap.add_argument('--overlay', help='the network disc\'s overlay folder: its changed files are served, the rest read '
                                      'from the console\'s own disc (GDEMU)')
    ap.add_argument('--whole', action='store_true', help='serve the whole disc (base + overlay) from the PC')
    ap.add_argument('--asset-plugins', help='a folder of asset decoder plugins (ES modules) for QA Link Cloud\'s Assets '
                                            'workspace: a game\'s own formats, kept outside any public repository')
    ap.add_argument('--level', choices=('read', 'control', 'run'), default='run', help='the most this bridge accepts')
    ap.add_argument('--allow-lan', action='store_true',
                    help='talk to a real console on the network. Off by default: the bridge then talks only to a loader '
                         'at this PC\'s own addresses (Flycast, the simulated console). A person turns it on in QA Link '
                         'Cloud; tests, scripts and agents leave it off')
    ap.add_argument('--no-autoconnect', action='store_true',
                    help='link only on POST /v0/link (default: link by itself to a console linked before, or the only '
                         'one heard, and relink when it comes back; POST /v0/settings {"autoconnect": false} at run time)')
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
    b = Bridge(a.data, (a.udp_host, a.udp_port), a.console, a.device, a.files, a.selftest, a.loader_disc, a.level, log,
               autoconnect=not a.no_autoconnect, only_local=not a.allow_lan, disc=a.disc, overlay=a.overlay,
               whole=a.whole)
    b.asset_plugins = a.asset_plugins
    b.viewer_elf = a.viewer
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
