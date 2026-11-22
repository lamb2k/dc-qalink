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

**The way back** (from `v0.2-console-fix`)
- A return stub (6,144 B) with three rungs: the BIOS's disc calls, the drive's own read, then a GDEMU image select and
  both again. Its refusal is shown on the TV as bars and kept in a record that outlives a reboot.
- The run's hand-off is copied before every BIOS reboot and put back by the loader, so a run that ends in a reboot
  still reports.

**The bridge, live data and MCP** (from `v0.2-bridge`)
- `pc/qalink_bridge.py`: one local service (standard-library Python) on 127.0.0.1, contract 1
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
   restart way back, two controls (a corrupted image refused, a reboot with no hand-in), the bridge linking Flycast,
   live data on with a planted 2 ms delay that must read as 2 ms, and MCP 9 of 9; plus the lane's own controls and two
   numbers its hand-back cites, re-measured;
5. on PASS commits the merge and pushes `v0.2`; on FAIL names the step and the file.
Machine settings (paths, addresses, ports) come from a file outside the repository (`GATE_ENV`).

## Limits (plan section 3), measured by the gate

Console side, in QA and test builds only; a release build with `DCQL_ENABLE=0` contains none of it.

| Limit | Row | Measured (6311e2f, link map) | Verdict |
|---|---|---|---|
| Code and constant data: link + live data (+ the menu, not built yet) | at most 48 KB | 15,653 B (client 8,137, adapter 3,448, live 2,872, utilities 628, way back 568) | inside; the menu's share is to come |
| Fixed memory (rings, stacks, state) | at most 72 KB | 122,326 B (adapter 52,013, client 51,905, live 17,380, utilities 1,028) | **over by 48 KB: open** |
| Return stub | at most 7,168 B | 6,144 B | inside |
| Heap use after start | none | the client references `memalign` and `free` (read: where and when) | to be read |
| Cost each picture, live data on | 0.10 ms mean, 0.25 ms worst, calibrated tier | 2.85 us mean, 3.0 us worst (Flycast, plain) | inside on Flycast; the calibrated tier needs a console-like Flycast with the adapter |
| Cost each picture, link off | one test, under 0.01 ms | 0.62 us mean, 1.0 us worst (Flycast) | inside |
| Network or disc access inside a picture | none | copies into rings only (code reading) | inside |
| Screen grab | in pieces by the link thread | the program pauses, the link thread sends in pieces (Flycast) | inside |
| Graphics memory | none | none | inside |

## Still open before 0.2.0

1. **The way back on a real console.** On the console (first sessions, 2026-11-22) the loader, push (0.57 to 0.65 s),
   the program's link (15 to 18 s with DHCP), reports and screen grabs (0.64 s each) worked, but every way back was a
   full reboot, and the three-rung stub froze after its jump on hardware. The chosen direction: the full restart as
   the standard way back, with each ending sent to the PC before the restart (branch `v0.2-console-fix`, next through
   the gate), the stub kept behind a `LINK.TXT` option. What only the console can show: whether any saved copy
   survives a real BIOS reboot, the SH-4 watchdog's reset, the end-to-beacon time.
2. **Fixed memory over its row** (122,326 B against 72 KB). Listed savings, each to be measured: the modem dial
   thread's 16 KB stack unused on an adapter link; the adapter's default 4 + 4 KB rings when the program supplies its
   own; the adapter's receive and send stacks sized by measured high-water marks; the program's rings (24 + 16 KB)
   checked for loss at smaller sizes; the 8 KB file read-ahead only for programs that read files. The row is not
   loosened.
3. **The in-game menu** (design "Bar"): not built. Its code, font and state count in the rows above when it lands.
4. **The network disc**: a game reads its disc from the PC over the adapter (the GDEMU disc as the base, changed
   files served from the PC, whole images served), with a build record stamped on every run, grab and asset. In
   progress on `v0.2-netdisc`.
5. Also open: the modem's summary mode in the desktop app, the power rung of the reset ladder (a smart plug), and
   every cost row on the calibrated tier.
