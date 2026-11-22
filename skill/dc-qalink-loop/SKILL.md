---
name: dc-qalink-loop
description: Push KallistiOS programs to a Dreamcast and pull their results back with dc-qalink 0.2's control loop - the loader disc, LINK.TXT for the broadband adapter or the modem, run/report/stills/serve/tail, the /pc file service, the return stub and the restart ladder (RESET, watchdog, power cycle), and Flycast with its emulated adapter. Use when asked to run a program on a Dreamcast (or Flycast) from the PC without burning a disc, collect its report or stills, or recover a console that hangs.
---

# The control loop (dc-qalink 0.2)

Tool: `python pc/dc_qalink.py loop ...` (same as `pc/dcql_loop.py ...`). Header: `include/dcql_loop.h` (protocol 2).

1. **Loader disc:** `make -C loader OUT=build`; put `build/LOADER.BIN` and a `LINK.TXT` on a disc whose
   `1ST_READ.BIN` is the loader (scrambled as usual). `LINK.TXT`: `net bba` (or `modem`), `host <PC address>`,
   `port 52060`, optional `ip/mask/gw` (else DHCP), `lport` (Flycast: not 52060). The screen shows UP and the address.
2. **Check:** `loop --udp <console ip> status` (adapter) or `loop --pi dreampi status` (modem): one HELLO line with
   the protocol, the transport, the loader's build and SHA, and whether the return stub is armed.
3. **Run:** `loop --udp <ip> run prog.elf --args "..." --deadline 120 --out results/ [--files <dir>] [--tail]`.
   Exit codes: 0 handed in DONE or RESET, 3 refused (bad image, other protocol), 4 the loader came back without the
   run, 5 CRASHED/TIMEOUT/LOST, 6 the ladder ended at "power cycle needed". Results: `run-<session>.json`,
   `report-<run>.txt`, `serial-<session>.txt`, `still-<run>-NNN-pPPPPP.png`.
4. **The program side:** link `loader/client.mk` (`$(CL_CLIENT_OBJS)`, `$(CL_CLIENT_LDFLAGS)`), call `cl_init(0)`,
   `cl_report(...)`, `cl_still(pix, w, h, stride, picture)`, `cl_line(...)`, read `/pc/<path>`, end with `cl_exit(n)`.
   `net=0` in the arguments keeps the link off (timing runs); `wdt=1` arms the watchdog.
5. **Hangs:** `loop ... reset` sends RESET every 2 s (a program whose link thread runs answers and returns through the
   stub); then it waits for the watchdog; then `pc/power.py` prints "power cycle needed" or switches your plug
   (`DCQL_PLUG`). Never call a run good without its report's CRC.
6. **New loader without a new disc:** `run ... --loader build/LOADER.BIN` pushes it first when the console runs
   another; it runs until power-off. A changed return stub or a new protocol needs the disc rebuilt.
7. **Flycast:** `[network] EmulateBBA = yes` (DCNet off), start `loop relay-local` before the loader boots, then use
   `--udp 127.0.0.1:52059 --bind 0`. One Flycast at a time on the relay's ports. Flycast numbers are function checks,
   never timings for the console.
