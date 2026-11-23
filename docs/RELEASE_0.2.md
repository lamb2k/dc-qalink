# dc-qalink 0.2: what it holds, and what is still open

Branch `v0.2` is the integration branch for 0.2.0. Work reaches it only through the gate (`tools/gate/gate.sh`,
below). 0.2.0 is tagged from it once the open items are closed and the maintainer approves; until then nothing here is a
release.

Every number below carries its tier: **console** (a real Dreamcast), **Flycast** (the emulator: a function check,
never a speed claim), **host** (tests on a PC), **link map** (exact bytes).

## What 0.2 holds today

**Link core** (from `v0.2-bba-loader`)
- One link layer, two transports (`lib/dcql_net.h`): the 56k modem as in 0.1, or the broadband adapter (HIT-0400 or
  the LAN adapter; DHCP, or a static address when no DHCP server answers). The 0.1 API is unchanged and `example/` is
  0.1's program, built in four forms by the gate.
- The loader disc (`loader/`): takes a program from the PC (deflated, CRC per chunk, SHA-256 checked before it runs),
  runs it and takes it back. `LINK.TXT` sets the transport, the PC, ports, the GDEMU slot and a fallback address.
- The client library (`loader/client.mk`) in each pushed program: report and exit code, serial lines, stills and
  screen grabs, the PC's files as `/pc/...`, RESET from the PC, the watchdog.
- The PC tool `pc/dcql_loop.py` (`dc_qalink.py loop ...`) and `tools/first_console.py` for a first session.

**The way back** (from `v0.2-console-fix`, ql-core4 to ql-core6)
- **The full restart is the default** (`LINK.TXT` `return reboot`, owner 2026-11-22): every ending sends the run's END
  (state, registers on a crash, the last 32 KiB of the report; 4 KiB from an exception) to the PC, then selects the
  loader's GDEMU image and reboots through the BIOS. The END path ends within 4 s when the PC has gone away, and at
  once when the link is down (ql-fix-dc; code reading, Flycast row f-03 below).
- The run's hand-off is copied before every BIOS reboot and put back by the loader, so a run that ends in a reboot
  still reports when no END arrived (Flycast; whether a copy survives a real BIOS reboot is a console question).
- **The return stub is optional and experimental** (`return stub`): 6,144 B, three rungs (the BIOS's disc calls, the
  drive's own read, then a GDEMU image select and both again). Flycast brings the loader back through it; on the
  console (2026-11-22) it froze after its jump. It stays off by default until a console run shows it working.
- **The SH-4 watchdog is opt-in** (`wdt=1`): reset mode, fed by TMU1 every 1 ms. Console, 2026-11-22 (ql-core6, the
  owner's console): a measured limit of 5.22 ms, a longest feeding gap of 2.76 ms, and a program hung with interrupts
  off came back to the loader by itself. Flycast has no watchdog reset.

**The in-game menu** (ql-menu): `lib/dcql_menu.c` (`DCQL_MENU=1`, `docs/MENU.md`), design "Bar", Spleen 8x16; link
state and counters, log lines, a QR state snapshot, the game's own pages. Host pictures and Flycast runs.

**The network disc** (ql-disc): `lib/dcql_disc.c` (`docs/NETDISC.md`): `/cd/...` from the PC over the adapter, changed
files over the GDEMU disc or a whole disc (folder, `.gdi`, `.iso`), with a build record. Flycast and host.

**The bridge, live data and MCP** (from `v0.2-bridge`)
- `pc/qalink_bridge.py`: one local service on 127.0.0.1 (Python standard library, except the MCP server, which needs
  the MCP SDK, and the asset routes, which need numpy and, for formats other than PNG, Pillow), contract 1
  (`docs/BRIDGE_API.md`): devices and discovery, push and run, reports, stills, the reset ladder, sessions recorded to
  files, live events as Server-Sent Events. Only-local by default (it links only a loader at this PC's own address,
  that is Flycast, until a person allows the LAN). Autoconnect keyed by the adapter's MAC from HELLO.
- Live data (`lib/dcql_live.c`): a record per picture (phases, counters, guards, room, position), a numbered trace
  ring, the game's log, channel subscriptions; copies on the game's thread, sends on the link thread.
- The local MCP (`docs/MCP.md`): 23 tools over stdio and local HTTP, each with a level (read, control, run), every
  call logged.

## The gate

`tools/gate/gate.sh <branch> <head>` merges a head into a scratch worktree (`--no-ff --no-commit`; a conflict fails the
gate, it is never resolved there), then:
1. builds every image with `make -j2`: loader (both transports, and modem only), stub, test program, liveprog, the host
   build of the protocol core, the example in four forms (0.1's modem build, both, adapter with the way back, link
   off), and any other program folder;
2. runs every host test (`tests/test_*.py`, a new one too), the syntax of every Python file, and the controls (for
   example, the 0.2 work-in-progress PC tool must take its own echo);
3. measures the size rows below from the link map and checks what the head adds to the repository (no blob over
   300 KB, no build outputs, no game data, no dot-folders but `.github`);
4. runs one Flycast suite (`tools/gate/flysuite.py`, `suite.json`): it first listens and refuses to start if a loader
   at any other address is heard, then status, push and run, a long report, three screen grabs, a crash, RESET, the
   restart way back, two controls (a corrupted image refused, a reboot with no ending), the bridge linking Flycast,
   live data on with a planted 2 ms delay that must read as 2 ms, and MCP 9 of 9; plus the lane's own controls and two
   numbers its hand-back cites, re-measured;
5. on PASS commits the merge and pushes `v0.2`; on FAIL names the step and the file.
Machine settings (paths, addresses, ports) come from a file outside the repository (`GATE_ENV`).

Rulings on the gate's rows:
- **2026-11-22, the plain-reboot control.** Since the full restart became the standard way back, a program that
  reboots with no ending comes back with its run named **LOST** (the loader's start-of-run record survives the
  restart), and the PC tool exits 5. The row expects exactly that: exit 5, state LOST. Its earlier form (exit 4,
  "back without this run") stays as the `ret nosave` row: no link, nothing kept, exit 4.
- **2026-11-23, ql-core4's cite "program link up after EXEC" (static address), re-cited from 1.29 s to 0.67 s, ±30%.**
  Cause: ql-mem's next-hop fix (011a5a0). KallistiOS asks again for an unknown MAC only after 5 s, so the adapter now
  asks for the next hop at link up and every 100 ms for up to 3 s. The program no longer waits on that, and its link
  comes up sooner. The new figure was measured by the gate on v0.2 plus ql-mem 6917f03: 0.681 and 0.654 s (Flycast,
  function-check tier, rows c4-01 to c4-05). This re-cites a known cause and does not loosen anything. Every other
  cite stays as it was, including "end to beacon, copy kept" at 5.26 s (5.247 and 5.234 s on the same runs).

## Limits (plan section 3), measured by the gate

Console side, in QA and test builds only; a release build with `DCQL_ENABLE=0` contains none of it.

Measured at the head of `v0.2-fix-dc` (lane ql-fix-dc, 2026-11-23) by `tools/gate/sizes.py` from the candidate's
link maps (exact bytes). The row's objects: `client.o`, `cl_util.o`, `dcql_return.o`, `dcql_net_bba.o`,
`dcql_live.o` in `liveprog.map` (the test program `loader/liveprog.c`). The menu and the network disc are measured
separately, from `example/menu`'s `menu-cl.map` and `example/netdisc`'s `netdisc.map`; they are not in the row.

| Limit | Row | Measured (link map) | Verdict |
|---|---|---|---|
| Code and constant data: link + live data | at most 48 KB (49,152 B) | 21,313 B (client 12,073, adapter 4,640, live 2,932, way back 1,040, utilities 628) | inside, 27,839 B to spare |
| Fixed memory (rings, stacks, state) | at most 72 KB (73,728 B) | 70,187 B (client 26,934, adapter 24,845, live 17,380, utilities 1,028) | inside, 3,541 B to spare |
| Return stub | at most 7,168 B | 6,144 B | inside |
| The in-game menu (`dcql_menu.o`, with its font) | reported | +10,038 B code and constants, +3,988 B fixed | not in the row: a program that links the menu adds it |
| A program with the network disc (`netdisc.elf`: the row's objects plus `dcql_disc.o`) | reported | 28,207 B code, 114,627 B fixed (`dcql_disc.o`: 6,874 B code, 44,440 B fixed: its cache, mount table and requests) | its own program, not the row |
| Heap use | none in a picture | thread stacks from KallistiOS's heap at link start (link 16 KiB, grab 8 KiB; the deadline thread at the default size); the /pc read-ahead (8 KiB) while a /pc file is open; the built-in rings (2 x 4 KiB) only for a program that supplies none. Lifetimes: `docs/SIZES.md` "Heap" | read, not judged by the gate |

On the base `1990fb9` the same maps gave 20,421 B and 70,167 B; ql-fix-dc adds 892 B of code (the END bound, the ring
swap, the read-ahead hold) and 20 B of fixed memory.
| Cost each picture, live data on | 0.10 ms mean, 0.25 ms worst, calibrated tier | 2.85 us mean, 3.0 us worst (Flycast, plain) | inside on Flycast; the calibrated tier needs a console-like Flycast with the adapter |
| Cost each picture, link off | one test, under 0.01 ms | 0.62 us mean, 1.0 us worst (Flycast) | inside |
| Network or disc access inside a picture | none | copies into rings only (code reading) | inside |
| Screen grab | in pieces by the link thread | the program pauses, the link thread sends in pieces (Flycast) | inside |
| Graphics memory | none | none | inside |

## Still open before 0.2.0

1. **The way back on a real console, at this head.** Console sessions of 2026-11-22 showed the loader, push (0.57 to
   0.65 s), the program's link (15 to 18 s with DHCP), reports and screen grabs (0.64 s each), the full restart, and
   the watchdog (above). Those sessions ran earlier heads; they do not establish recovery or timing at this head.
   Still to run on the console with this head's loader and PC tool (audit 2026-11-23, section 7): normal exit,
   RESET, timeout and exception through the CLI and the bridge; the PC disconnected before the end and during a large
   report; which saved copy survives; the end-to-beacon time; watchdog off and armed; static and DHCP starts; repeated
   /pc and network-disc reads under load; stack high-water marks with DHCP. The optional stub's results are recorded
   apart.
2. Also open: the modem's summary mode in the desktop app, the power rung of the reset ladder (a smart plug), and
   every cost row on the calibrated tier.

Closed since the first version of this list: fixed memory under its row (ql-mem, 70,151 B, then 70,167 B and
70,187 B above); the in-game menu (ql-menu) and the network disc (ql-disc) are built and measured above.
