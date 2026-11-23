# Changelog

## 0.2.0 (2026-11-23)

The control loop and the broadband adapter. A program built with KallistiOS can now be pushed from the PC, run, and
brought back to the loader, with its report, serial lines and screen grabs on the PC. Details, tiers and evidence:
`docs/RELEASE_0.2.md`.

**New**
- Broadband adapter (HIT-0400 and the LAN adapter) beside the 56k modem, one link layer, DHCP or a static address.
  The 0.1 API is unchanged.
- The loader disc: takes a program from the PC (deflated, CRC per chunk, SHA-256 checked before it runs), runs it and
  takes it back. The full BIOS restart is the default way back; the return stub is optional and experimental.
- The client library for pushed programs: report and exit code, serial lines, screen grabs, the PC's files as
  `/pc/...`, RESET from the PC, an opt-in SH-4 watchdog fed every 1 ms by TMU1.
- The network disc: `/cd/...` served from the PC (a folder, `.gdi` or `.iso`), whole or over the GDEMU disc.
- The in-game menu (`DCQL_MENU=1`): link state, counters, log lines, a QR snapshot, the game's own pages.
- Live data: a record per picture (phases, counters, room, position), a trace ring, channel subscriptions.
- The bridge (`pc/qalink_bridge.py`): one local service with an HTTP API, live events and a local MCP server (23
  tools), only-local by default.

**Safety (from four independent review rounds before the tag)**
- The bridge listens only on this PC unless `--allow-lan`; files are served only from the folders you name, checked
  on the opened file; size and count limits on every upload, still and report; a valid ending is kept through reset
  and report failures; screen grabs are checked end to end.

**Measured**
- Console (2026-11-22, the owner's Dreamcast, loader from an earlier 0.2 build): screen grabs, the armed watchdog
  (5.22 ms limit, longest feed gap 2.76 ms) and recovery from a hang with interrupts off.
- Link map at the tag: fixed memory 70,211 B of 73,728 B; code 22,279 B of 49,152 B; stub 6,144 B.
- Everything else: host tests and Flycast function checks at the tag head.

**Known issues** (fixed in 0.2.1 or checked on a console)
- Flycast only: start the bridge (or the app) before Flycast; Flycast stops receiving after Windows answers a beacon
  with "port unreachable". A real console is not affected (to be confirmed on the console checklist).
- A screen grab of a moving picture can mix rows of neighbouring frames; pause the program first for a clean frame.
- Rarely, a test program went silent after linking during screen grabs and its deadline did not fire: seen twice in
  Flycast before the tag (once in 8 runs of one grab check, once in the menu grab check), not in 10 repeat runs at
  the tag head. Cause unknown; on the console checklist.
- The audit follow-up list in `docs/RELEASE_0.2.md` (stricter test assertions, disc ownership across queued runs,
  ZIP accounting for growing files, the bridge's discovery before the version check, Windows directory-swap races).

## 0.1.0

The in-game library over the modem, the DreamPi relay, the PC tool and three agent skills.
