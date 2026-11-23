# The network disc (dc-qalink 0.2)

A KallistiOS program run by the dc-qalink loader reads its disc from the PC over the broadband adapter, with no change
to its code: it still opens `/cd/...`.

- **Overlay** (the default): the console's own disc (GDEMU) is the base; the files the developer changed are read from
  the PC. Changing one texture means copying one file into a folder, not building and copying a disc image.
- **Whole disc**: every file from the PC: a built disc folder, a `.gdi` (with its tracks) or an `.iso`, with the overlay
  on top. `.cdi` is not read yet (convert it, or serve its folder).
- The modem is far too slow for it (about 1.4 KB a second); it needs the adapter.

## Using it

Console side (`lib/dcql_disc.c`, `lib/dcql_disc.h`; build rule in `example/netdisc/Makefile`):
```c
cl_init(0);                 /* the client library: the link on the adapter */
dcql_disc_mount(NULL);      /* "/cd", the PC says overlay or whole disc */
FILE *f = fopen("/cd/DATA/LEVEL1.BIN", "rb");   /* unchanged code */
dcql_disc_report();         /* one cl-disc line of counters in the run's report */
```
PC side, either tool:
```
python pc/qalink_bridge.py --disc build/disc-tree --overlay changed/           (QA Link Cloud, agents)
python pc/dcql_loop.py --udp <console> run game.elf --disc disc.gdi --overlay changed/ [--whole] --out results/
```
At mount the console asks for the mount table: the hashes and sizes of the files the PC serves (overlay mode: the
overlay's files whose SHA-256 differs from the base's; a byte-identical copy is still read from GDEMU), or a flag
"whole disc". Opening any other file costs no network at all. The PC's manifest (every file: path, size, SHA-256, side)
is in `GET /v0/disc` and in the build record (docs/BRIDGE_API.md, "Builds").

## Console side
- One VFS handler at the mount name. KallistiOS picks the newest handler of the longest matching name, so it shadows
  the ISO 9660 one, which it keeps and calls for every file the PC does not serve (open, read, seek, readdir, stat).
- Requests ask for one block (`block`, default 4 KiB) in 1 KiB pieces; `window` (default 2) requests are in flight; the
  link thread copies each piece straight into its place (a cache block, or the caller's buffer for whole blocks). A
  resend asks only for the pieces still missing (after 250 ms, then every 500 ms); a request fails after `timeout_ms`
  (3 s) and the read returns EIO: nothing waits without a time limit. Read-ahead fills `window` blocks after the one
  read and never blocks the caller; the game thread waits only inside its own read call.
- `window * block` is held to 8 KiB in flight (`inflight_max`, default `DCQL_DISC_INFLIGHT`; at most 32 KiB for lab
  arms): the PC sends a request's pieces back to back and KallistiOS's adapter receive ring is 16 KiB, frame headers
  included (see "Speed").
- Fixed memory, no heap: the cache `DCQL_DISC_CACHE` (32 KiB), the table `DCQL_DISC_TABLE_N` x 8 B (512: 4 KiB; more
  served files set an overflow flag and every open asks the PC, still correct), 16 open files, 6 requests, a 1 KiB log
  buffer. Measured (`netdisc.map`, sh-elf-size of `dcql_disc.o`): code 7,152 B + data 424 B, fixed memory 44,012 B.
- Interrupts are off only while a piece (at most 1 KiB) is copied into place and while a request's words change: a few
  microseconds, far inside the watchdog's 21 ms.
- The program's reads are sent to the PC as `DLOG` records (file, offset, bytes, microseconds, side; sequential reads
  of one file merged), batched in a 1 KiB buffer, at most every 500 ms and at each close: the session's timeline
  shows them (`disc` records, session format 2).

## Wire (protocol 2, additive; `include/dcql_loop.h`, `pc/dcql_disc.py`)
| Type | Way | a | Payload |
|---|---|---|---|
| DOPEN 0x27 | console to PC | request id | u32 flags (1 mount table, 2 directory, 4 overlay mode: answer -3 when not served), path |
| DINFO 0x2a | PC to console | request id | i32 status (0, -2 no file, -3 read it from the disc), u32 handle, u32 size, u32 flags (1 overlay, 2 directory) |
| DREAD 0x28 | console to PC | request id | u32 handle, offset, length (at most 32 KiB), mask of pieces wanted, previous request id, its microseconds |
| DDATA 0x2b | PC to console | request id, b = piece | up to 1024 bytes |
| DCLOSE 0x29 | console to PC | handle | previous request id, its microseconds |
| DLOG 0x2c | console to PC | sequence | records: open (kind 1, side, fd, size, name), read (kind 2, side, fd, offset, bytes, us, calls), close (kind 3) |

The mount table and a directory listing are virtual files read with DREAD like any other. The console's own round-trip
time of each request rides on its next DREAD, so the PC records it without extra datagrams.

## Speed
**Flycast (plain tier: function and protocol cost only, never a console speed).** Rounds f1 and f2 of lane ql-disc
(`E:\RE4DC-Lanes\ql-disc\ev`), 1 MiB file from the PC, 8 small files, 64 single 2 KiB reads:

| Arm (window x block, in flight) | Whole 1 MiB | 2 KiB sequential | 2 KiB scattered | Small read mean / worst | Resends | Hashes |
|---|---|---|---|---|---|---|
| 2 x 4 KiB, 8 KiB (default, f2) | 0.34 MB/s | 0.35 MB/s | 0.08 MB/s | 16.7 / 22.6 ms | 0 | all ok |
| 2 x 4 KiB, whole disc from a .gdi (f2) | 0.34 MB/s | 0.35 MB/s | 0.09 MB/s | 17.2 / 26.7 ms | 0 | all ok |
| 1 x 8 KiB, 8 KiB (f2) | 0.35 MB/s | 0.35 MB/s | 0.05 MB/s | 25.1 / 299.7 ms | 5 | all ok |
| 2 x 8 KiB, 16 KiB (f2) | 0.016 MB/s | 0.017 MB/s | 0.05 MB/s | 29.8 / 292 ms | 510 | all ok |
| 4 x 4 KiB, 16 KiB (f2) | 0.09 MB/s | 0.09 MB/s | 0.06 MB/s | 16.5 / 26.8 ms | 293 | all ok |
| 2 x 16 KiB, 32 KiB (f1) | 0.03 MB/s | 0.03 MB/s | 0.004 MB/s | 792 / 798 ms | 215 | all ok |
| Real disc in Flycast (f2, the disc pass) | 3,000 MB/s | 117 MB/s | | | | all ok |

Controls (f1): a wrong byte in an overlay file gives `sha BAD` and exit 1 (`badctl`); no network disc service on the
PC gives mount -2 after 3.8 s and exit 1 (`noserve`); f3: 2 x 8 KiB without `inflight=16` is refused at mount (-5).

What Flycast shows about the protocol: a request costs about 20 ms there whatever its size (its emulated adapter, so
MB/s is block / 20 ms and says nothing of the console), and 16 KiB or more arriving at once loses pieces, each loss a
250 ms resend that the window then repeats; 8 KiB lost nothing as two requests and a few pieces as one. Hence the
defaults 2 x 4 KiB and the in-flight cap of 8 KiB (`DCQL_DISC_INFLIGHT`; `inflight=` raises it for a lab arm). Reads
from Flycast's emulated GD-ROM are instant there and say nothing about GDEMU.

**Expected on a console (estimate from the code and the wire, not measured):**
- At 10 Mbit/s a 1 KiB piece is a 1,110-byte frame on the wire (headers, CRC, preamble, gap): the data ceiling is about
  **1.15 MB/s**. A 4 KiB request takes about 3.7 ms of wire plus about 0.5 ms of round trip and PC time; with two in
  flight the wire stays busy, so **0.9 to 1.1 MB/s** for whole-file and sequential reads, and **about 4 to 5 ms** for a
  single small read that misses the cache (estimate). At 10 Mbit/s the ring drains faster than the wire fills it, so
  the console may well take 16 KiB in flight without loss: the run sheet's `inflight=16` arms say whether to raise
  the default.
- CPU per piece on the SH-4 (estimate): the adapter's frame read over the G2 bus (about 85 us), the stack and the
  ring copies (about 30 to 50 us), the CRC-32 in software (about 40 us): about 0.15 to 0.2 ms, so at 1.1 MB/s the
  network disc takes about 20% of the CPU while it reads (loading time, not play).
- If the adapter's link comes up at 100 Mbit/s, the wire stops being the limit and the console's CPU per piece is: an
  estimated 3 to 5 MB/s, with the receive ring the constraint (hence the in-flight cap).
- What would raise it: larger datagrams on the adapter (1,400-byte pieces: 25% fewer frames; the modem path's
  1,200-byte limit does not apply to the adapter), a table CRC on the SH-4 with sh4zam-style word loads, and PC-side
  pacing so more can be in flight without overrunning the ring.

## The console run sheet (the next owner session)
The disc: the loader disc with the generated `NETDISC/` base files on it (`tools/netdisc_testdata.py`; lane ql-disc's
`s\disc.sh` stages it); on the PC the same test data's `overlay\`. Nothing else is needed on the card.
1. Power on; the loader comes up (as for every run).
2. Overlay, both passes (real disc first, then the network disc):
   `python pc\dcql_loop.py --udp <console> run pc\netdisc.elf --args "passes=disc,net" --disc data\base --overlay data\overlay --deadline 150 --wait 240 --out nd-overlay`
3. Whole disc from the image: `... run pc\netdisc.elf --args "passes=net" --disc disc.gdi --overlay data\overlay --whole ... --out nd-whole`
4. Window and block arms (one variable each): `--args "passes=net window=1 block=8"`, `"passes=net window=1 block=4"`,
   then the lab arms with more in flight: `"passes=net window=2 block=8 inflight=16"`,
   `"passes=net window=4 block=4 inflight=16"` (lost pieces show as resends in `cl-disc:`; none means the default
   may be raised to 16 KiB).
5. Read from each report: `@ pc whole-file read`, `@ pc 2K sequential read`, `@ pc 2K scattered read` (MB/s),
   `@ small read latency mean / worst` (ms), `@ netdisc failures = 0`, the `cl-disc:` line (resends and timeouts: 0
   expected at the defaults), and the disc pass's lines (GDEMU's own speed beside it). The PC's `disc-reads.jsonl` and
   `run-*.json` (`program_side.disc.request_ms`) hold the per-read times.
6. Pass: every hash matches in every arm; the defaults resend nothing; MB/s against the estimate above.
