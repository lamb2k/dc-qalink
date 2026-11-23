# dc-qalink 0.2: what it holds, and what is still open

0.2.0 was tagged on 2026-11-23 from branch `v0.2` after the release criteria at the end of this file held at the
tag head (the gate's record: head, ledger, sizes and link maps) and the maintainer approved. `CHANGELOG.md` has the
short list and the known issues.

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
  loader's GDEMU image and reboots through the BIOS. When the PC has gone away, END's sends and its waits for the
  PC's acknowledgment share a 4 s retry budget (`END_LINK_MS`): no new retry or wait starts after it runs out, but a
  step under way finishes first (a wait for ring room reads the clock after its 1 ms sleep, a receive poll takes up to
  10 ms), so the delay is about 4 s, not a hard wall-clock bound on the whole exit. On a link that is down it gives up
  at once. (ql-fix-dc; code reading, Flycast row f-03 below.)
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
  files, live events as Server-Sent Events. Only-local by default (it binds the loader's UDP port on 127.0.0.1 and
  links only a loader at this PC's own address, that is Flycast, until a person allows the LAN with `--allow-lan`;
  ql-fix-pc). The CLI (`pc/dcql_loop.py --udp <console>`) still binds every address: naming a console on the network
  is its opt-in. File reads stay below named roots (`--files`, `--asset-root`, `QALINK_MCP_ROOT`). Autoconnect keyed by the adapter's MAC from HELLO.
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
   example, the 0.2 work-in-progress PC tool must take its own echo). Since ql-fix-pc each of these is its own gate
   row: the full `test_loop` (with the modem-rate transfer, no `--quick`), `test_assets` with its four
   broken-implementation controls, and the three safety suites, each a positive row and a historical control row
   whose pinned rows must fail with their named discrepancy on the code the audit read:
   `test_safety` (21 pinned rows, 16 controls against 1990fb9), `test_safety2` (34 pinned rows, 27 controls against
   2ff9b9d and 1 against 24cd792), `test_safety3` (24 pinned rows, 22 controls against ac45161: the round-3
   blockers) and `test_safety4` (13 pinned rows, 12 controls against df476a2: the round-4 confirmation items);
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

Measured at `v0.2-fix-dc2` 214a15f (lane ql-fix-dc round 2, 2026-11-23) by `tools/gate/sizes.py` from the candidate's
link maps (exact bytes). The row's objects: `client.o`, `cl_util.o`, `dcql_return.o`, `dcql_net_bba.o`,
`dcql_live.o` in `liveprog.map` (the test program `loader/liveprog.c`). The menu and the network disc are measured
separately, from `example/menu`'s `menu-cl.map` and `example/netdisc`'s `netdisc.map`; they are not in the row.

| Limit | Row | Measured (link map) | Verdict |
|---|---|---|---|
| Code and constant data: link + live data | at most 48 KB (49,152 B) | 21,605 B (client 11,997, adapter 5,008, live 2,932, way back 1,040, utilities 628) | inside, 27,547 B to spare |
| Fixed memory (rings, stacks, state) | at most 72 KB (73,728 B) | 70,211 B (client 26,946, adapter 24,857, live 17,380, utilities 1,028) | inside, 3,517 B to spare |
| Return stub | at most 7,168 B | 6,144 B | inside |
| The in-game menu (`dcql_menu.o`, with its font) | reported | +10,038 B code and constants, +3,988 B fixed (measured at c9b8468) | not in the row: a program that links the menu adds it |
| A program with the network disc (`netdisc.elf`: the row's objects plus `dcql_disc.o`) | reported | 28,207 B code, 114,627 B fixed (`dcql_disc.o`: 6,874 B code, 44,440 B fixed: its cache, mount table and requests; measured at c9b8468, before round 2's +292 B code and +24 B fixed in the row's objects) | its own program, not the row |
| Heap use | none in a picture | thread stacks from KallistiOS's heap at link start (link 16 KiB, grab 8 KiB; the deadline thread at the default size); the /pc read-ahead (8 KiB) while a /pc file is open; the built-in rings (2 x 4 KiB) only for a program that supplies none. Lifetimes: `docs/SIZES.md` "Heap" | read, not judged by the gate |
| Cost each picture, live data on | 0.10 ms mean, 0.25 ms worst, calibrated tier | 2.85 us mean, 3.0 us worst (Flycast, plain) | inside on Flycast; the calibrated tier needs a console-like Flycast with the adapter |
| Cost each picture, link off | one test, under 0.01 ms | 0.62 us mean, 1.0 us worst (Flycast) | inside |
| Network or disc access inside a picture | none | copies into rings only (code reading) | inside |
| Screen grab | in pieces by the link thread | the program pauses, the link thread sends in pieces (Flycast) | inside |
| Graphics memory | none | none | inside |

On the base `1990fb9` the same maps gave 20,421 B and 70,167 B; ql-fix-dc round 1 (c9b8468) gave 21,313 B and
70,187 B (the END budget, ring ownership, the read-ahead hold); round 2 adds 292 B of code (the ring exclusion and
wrapped copies, the API wrappers) and 24 B of fixed memory
(two mutexes).

## Console-side fixes after the 2026-11-23 audits (lane ql-fix-dc, rounds 1 and 2)

| audit finding | fix | check (tier) | its control |
|---|---|---|---|
| a /cd directory entry advanced past the listing | `lib/dcql_dirent.h`: a truncated entry is rejected and ends the listing | `lib/test/run.sh` (host, ASan) | the 1990fb9 rule accepts the truncated entry and leaves the position past the size |
| built-in rings: one kept when its partner failed; owned rings stranded by a later `dcql_net_config` | `lib/dcql_ring.h`: owned vs borrowed, both or neither, a swap frees the owned rings it replaces | `lib/test/run.sh` (host, ASan leaks) | the 1990fb9 code leaks in both cases |
| the /pc read-ahead not freed when a program ends with files open | held by each read, freed at the last close or on the way out | Flycast f-01 (read and closed: "none held at exit"), f-02 (left open: "freed at exit") | the lines do not exist at 1990fb9 |
| round 2: a ring swap could free memory a reader was still copying (2 ms sleep, no exclusion on the receive copy, no lock against a start) | every ring access and the swap inside one exclusion (interrupts off), `lib/dcql_ring.h` `ringx_*` / `rings_replace`; owned memory freed at once; config/start/stop/shutdown under one mutex | `lib/test/run.sh` `host_race` (host, ThreadSanitizer): 300 stop/reconfigure/start rounds with traffic both ways, 7,664 datagrams, none torn, no report | copies of the 2ff9b9d code in the same test: 6,851 datagrams torn, ThreadSanitizer data races in `ring_put` / `ring_get` |
| round 2: the read-ahead hold protected the block's life, not its bookkeeping or copy | `loader/cl_ra.h`: one mutex from the check to the copy, over fetch, close, allocation and free; the way out waits at most 200 ms for it | `host_race`: three readers and the way out, every byte its own file's, no report; Flycast f-01 / f-02 again on 214a15f ("none held at exit", "freed at exit") | the 2ff9b9d read in the same test: ThreadSanitizer races in it every run (44 wrong reads in the gate's run; wrong bytes on some runs only) |
| END enqueue retries had no total time bound | `END_LINK_MS`: a 4 s retry budget for the whole END path (round 2 wording: a step under way finishes first); at once on a link that is down | Flycast f-03 (30 KB report, link stopped before exit): loader back 7.42 s after EXEC (7.38 s on 214a15f), report DONE from the kept copy | the same row on c0bc0db with the END sender as at 1990fb9 (local branch ql-fix-dc-control2 46c7dbc, `gates\ql-fix-dc-control-46c7dbc1e`): the loader not back within 75 s, FAIL as it must |

Round 1 measured on the code commit c0bc0db (`gates\ql-fix-dc-c0bc0db23`), round 2 on 214a15f (`gates2\ql-fix-dc2-code-214a15f2a-run2`, f-01 to f-03 PASS; f-01 the same heap figures); Flycast function tier, one run each. The f-01 heap
line reads 309,724 B before the open and 310,244 B after the close: the 8 KiB block is gone; the 520 B left is most
likely the C library's file records, allocated once at the first `fopen` (not read further).

## Release criteria for 0.2.0 (integrator's ruling, 2026-11-23)

0.2.0 is tagged on **host tests and Flycast at the exact tag head**:
1. The gate passes at the tag head: every build, every host test with its controls (including `lib/test/run.sh`:
   `host_lib` and `host_race`), the size rows, the repository checks, and the Flycast suite with the lanes' rows.
2. The gate's log and the candidate's size outputs (link maps, `sizes.py`) for that head are kept with the release
   evidence.
3. The pre-tag audit's open items are closed or listed below as deferred.

A console run is **not** a tag criterion. The console evidence on record is cited as console, with its date and build:

| What (console) | When and build | Result |
|---|---|---|
| `first-run10` (the first-console steps on the owner's console) | 2026-11-22, loader built from `a54c944` (`v0.2-console-fix`), not this head | steps 1, 4a, 4b, 9, 10, 11 all passed: beacon; screen grabs in two runs of three, 3 of 3 held each; the watchdog armed (resets 5.22 ms after the last feed, measured; fed every 1 ms, 15,091 feeds, longest gap 2.76 ms); a program hung with interrupts off came back to the loader by itself; the next run after it |
| earlier sessions the same day | 2026-11-22, earlier heads | the loader, push (0.57 to 0.65 s), the program's link (15 to 18 s with DHCP), reports and screen grabs (0.64 s each), the full restart; the optional stub froze after its jump |

These runs used earlier heads; they do not establish recovery or timing at the tag head.

### Exact-head console checklist (after the tag, run with the owner)

From the pre-tag audit (2026-11-23, "What a console run should still check"), with the tag's loader and PC tool:
- Normal exit, RESET, deadline and exception, each through the CLI and through the bridge. Record the run ID, END
  state, tail coverage, whole-report CRC when there is one, ENDACK, the saved copy that survives, the GDEMU slot, the
  next beacon and that the next run is accepted.
- The PC disconnected before the end, and during a congested long report. END's retry time measured apart from the
  BIOS-to-beacon time. A run with a received END and no loader beacon recorded apart from a lost ending.
- The watchdog off and armed: counter movement, the longest TMU1 feeding gap, a normal shutdown, recovery from a hang
  with interrupts off.
- Static and DHCP starts; a next hop that does not answer; repeated `/pc` and network-disc use; files open at
  shutdown; stop / reconfigure / start with traffic in flight; repeated grabs after an interrupted grab; stack
  high-water marks with DHCP.
- The optional stub's results recorded apart.
- Boot the loader, wait 30 s, then start the bridge and push (on Flycast this order fails: Flycast on Windows stops
  receiving on a port that met an ICMP "port unreachable"; a console's KallistiOS ignores it).
- Grabs of a moving game, a held game and a game with its menu open: each held whole (the CRC covers the bytes sent; a
  moving picture may tear).

### Deferred: can follow the tag

- Throughput and stack tuning.
- The modem's summary mode in the desktop app.
- The smart-plug power rung of the reset ladder.
- Calibrated performance figures (every cost row on the calibrated tier).
- The optional return stub stays **experimental** until a console run shows it working.
- Windows directory windows of the /pc grant: Windows has no directory descriptors, so a served folder is
  re-checked by identity before each open and a run's subfolder is checked by name before it is anchored; a folder
  swapped in between those two steps by someone who can write in the served root is not caught there (Linux opens
  both from the anchored descriptor).
- The bridge auto-selects a HELLO before the version check (discovery only; the session decoder refuses an
  incompatible peer).

Closed since the first version of this list: fixed memory under its row (ql-mem, 70,151 B, then 70,167 B, 70,187 B
and the figure above); the in-game menu (ql-menu) and the network disc (ql-disc) are built and measured above.
