# Fixed memory of the link (dc-qalink 0.2)

The row (plan section 3): the fixed memory (`.data` + `.bss`) of the link and live data that a program carries, at
most **73,728 B** (72 KB). It is measured by the gate (`tools/gate/sizes.py`) from `liveprog.map`, the link map of the
test program `loader/liveprog.c`, over the objects `client.o`, `cl_util.o`, `dcql_return.o`, `dcql_net_bba.o` and
`dcql_live.o`. Tier: the link map, exact bytes.

## At this head (lane ql-fix-dc, 2026-11-23)

| | base v0.2 1990fb9 | head of `v0.2-fix-dc` |
|---|---|---|
| fixed memory, link + live | 70,167 B | **70,187 B: inside** (3,541 B to spare) |
| code and constant data | 20,421 B | 21,313 B (row 49,152) |
| per object, code / fixed | client 11,497 / 26,922; adapter 4,324 / 24,837 | client 12,073 / 26,934; adapter 4,640 / 24,845 (live, utilities, way back unchanged) |
| the in-game menu, `dcql_menu.o` (menu-cl.map, reported) | | 10,038 B code and constants (font 2,002 B of it), 3,988 B fixed |
| a program with the network disc (netdisc.map, reported) | 114,591 B (ql-mem) | 114,627 B fixed, 28,207 B code; `dcql_disc.o` 6,874 / 44,440 |

The 20 B: the read-ahead's hold count, its closed flag and its exit result (client), an owned flag per ring
(adapter). Both columns: `tools/gate/sizes.py` on maps built the same way (`make -C loader`, `make -C example/...`).

## Heap: what is allocated, when, and when it is freed

| block | size | allocated | freed |
|---|---|---|---|
| the client's link thread stack | 16 KiB | `cl_init` starts the link (`thd_create_ex` without a stack pointer: KallistiOS's heap) | with the program image (the full restart or a jump) |
| the client's grab thread stack | 8 KiB | with the link thread | with the program image |
| the deadline thread's stack (and `diag=1`'s thread) | KallistiOS's default thread stack size | `cl_init` with a deadline (or `diag=1`) | with the program image |
| the /pc read-ahead block (`g_ra`) | 8 KiB (`CL_FREAD_MAX`) | the first /pc open (none: small reads go to the PC unbuffered) | the last close; or on the way out of a program that leaves files open (`summary()` and `finish()`, threads still running), when no read holds it (it waits up to 200 ms; a read still waiting for the PC keeps it, and the line says "kept"). After the way out begins, reads are unbuffered. An exception's way out frees nothing (the heap may be what failed); the restart ends the image. |
| the built-in rings (`dcql_net.c`) | 2 x 4 KiB | the first `dcql_net_start()`, only for a ring the program did not supply; both or neither (a failed second allocation frees the first; start fails with err -44) | kept across `dcql_net_stop()` and a later start (the adapter's threads stay on); freed when `dcql_net_config()` supplies the program's own rings in their place; otherwise with the program image |
| the program's own rings | its own | the program's (static) | never by the link: borrowed |

Ring changes (`dcql_net_config()` with `rx_mem` / `tx_mem`) happen only while the link is off, failed or dropped with no
dial or hang-up thread; otherwise the current rings are kept and a log line says so. The pointers change with
interrupts off, after a 2 ms sleep that lets a datagram copy already under way on a link thread finish. Checks: the
host test `lib/test/run.sh` (both-or-neither, the swap frees owned and never borrowed memory; its control, the code at
1990fb9, must fail) and the Flycast rows f-01 and f-02 (`docs/RELEASE_0.2.md`).

Close, stop and restart: `close` of the last /pc file frees the read-ahead; `dcql_net_stop()` frees nothing (the
adapter keeps its socket and threads for a quick restart); `dcql_net_shutdown()` before a jump stops the adapter and
frees nothing; the full restart (BIOS reboot) and the stub's jump end the program image, heap and all.

## Before and after (lane ql-mem)

| | before (v0.2 2ac5297) | after (lane ql-mem) |
|---|---|---|
| fixed memory, link + live | 124,327 B: OVER | **70,151 B: inside** (3,577 B to spare) |
| code and constant data | 19,333 B | 19,961 B (row 49,152) |
| per object, fixed | client 53,906; adapter 52,013; live 17,380; utilities 1,028 | client 26,922; adapter 24,821; live 17,380; utilities 1,028 |
| a program with the network disc (own row, reported) | 168,767 B | 114,591 B |

Both columns are the gate's own size rows (lane ql-mem's gate runs `gates\ql-mem-base-2ac5297c5` and
`gates\ql-mem-77e8c38d0`, same options). With the next-hop fix below (011a5a0): fixed **70,167 B** (+16 B: the next
hop's address, a flag and a start time), code 20,421 B (`gates\m-fix2-011a5a00f`).

## The next hop asked again (011a5a0)

ql-int's gate on fd4b45d found end to beacon after a kept record at 10.23 s against 5.25 s. A timestamped Flycast
serial showed the console on time and the loader's first HELLO reaching the PC 5.0 s late. KallistiOS queues a
datagram whose next hop is not in its ARP cache, asks once, and asks again only after 5 s (`net_arp.c`); in Flycast the
first question right after the adapter comes up usually goes unanswered. The link now asks again from its send thread
every 100 ms (at most 3 s) while the MAC is missing, and still counts as up at once. Flycast, rows 01 to c4-05, one run
each (function tier):

| | fd4b45d (m-rep) | 011a5a0 (m-fix2) |
|---|---|---|
| end to beacon, copy kept (c4-05) | 10.24 s | 5.24 s |
| program's link up after EXEC | 1.30 s | 0.66 s (the client's ARP wait at link up shortens with it) |
| END after EXEC (lines:5) | 2.76 s | 2.16 s |
| next hop answered | not logged | 124 to 214 ms in 20 boots, once 609 ms |

## Every symbol of 256 B or more (before), and what happened to it

| symbol | object | bytes | what it is, why that size | after |
|---|---|---|---|---|
| `g_rx_mem` | client.o | 24,576 | the program's receive ring: one 8 KiB file-service answer with room | 16,384: sized to need (one answer is 8,368 B with the ring's length words; the network disc keeps at most 8 KiB in flight) |
| `g_dial_stack` | dcql_net_bba.o | 16,384 | the dial thread's stack (net_init, DHCP) | kept; on the adapter the dial thread now stays on as the receive thread |
| `g_brx_stack` | dcql_net_bba.o | 16,384 | the adapter's receive thread's stack (a loop around recvfrom) | **gone**: the dial thread receives |
| `g_tx_mem` | client.o | 16,384 | the program's send ring (lines, still blocks, END) | 8,192: every sender waits for room (`link_out_wait`), so a smaller ring loses nothing |
| `g_rec` | dcql_live.o | 11,264 | live records, 128 x 88 B (about 2 s at 60 pictures a second) | kept (a smaller ring drops records) |
| `g_ra` | client.o | 8,192 | the /pc read-ahead block (CL_FREAD_MAX) | **heap, behind the feature**: allocated when the first /pc file opens, freed when the last closes; without memory small reads go to the PC unbuffered |
| `g_btx_stack` | dcql_net_bba.o | 8,192 | the adapter's send thread's stack | kept (high-water mark reported, below) |
| `g_rx_def`, `g_tx_def` | dcql_net_bba.o | 4,096 each | the built-in rings, for a program that supplies none | **heap at start**: allocated by the first `dcql_net_start()` only when the program supplied no rings (ql-fix-dc: both or neither, and freed when the program's own rings replace them; "Heap" above) |
| `g_tr` | dcql_live.o | 3,072 | trace events, 64 x 48 B | kept |
| `g_lg` | dcql_live.o | 2,048 | log lines, 16 x 128 B | kept |
| `g_bpkt_rx` | dcql_net_bba.o | 1,408 | the receive thread's datagram buffer | on the receive thread's static stack |
| `g_raw` | client.o | 1,274 | the exception path's raw frame (END from an exception) | kept; also END's answer buffer |
| `d` (link_thread) | client.o | 1,208 | the link thread's datagram buffer | on the link thread's stack |
| `d` (end_send_link) | client.o | 1,208 | END's answer buffer | **shared** with `g_raw` (the two ways of sending END never run together) |
| `g_bpkt_tx` | dcql_net_bba.o | 1,208 | the send thread's datagram buffer | on the send thread's static stack |
| `crc_table` | cl_util.o | 1,024 | CRC-32 table, built at first use | kept (cl_util.c is outside this lane; a constant table would move it to code) |
| `g_ack` | dcql_live.o | 576 | command answers, 8 x 72 B | kept |
| `g_pc_vfs` | client.o | 388 | the /pc file system handler | kept |
| `g_cmdq` | dcql_live.o | 288 | commands, 4 x 72 B | kept |
| `g_pcf` | client.o | 256 | 16 open /pc files | kept |

Not in the row, for reading: the client's link thread (16 KiB) and grab thread (8 KiB) stacks come from KallistiOS's
heap (`thd_create_ex` without a stack pointer), as before ("Heap" above).

## Stacks and rings as used (Flycast, function tier: the `cl-mem:` line of each run)

| run | dial + receive stack (16,384) | send stack (8,192) | ring drops rx / tx refusals |
|---|---|---|---|
| push + run, 5 lines | 1,740 | 2,264 | 0 / 0 |
| three screen grabs | 1,740 | 3,984 | 0 / 1,037 (base with the 16 KiB ring: 1,029; senders wait and send again) |
| /pc file 1 MiB | 1,740 | 2,264 | 0 / 0 |
| network disc, default arm | 1,780 | 2,824 | 0 / 0 |

These runs use a static address (no DHCP). A DHCP start goes deeper into net_init on the dial stack, so the stacks
stay as they are until a DHCP run (and the console) has been measured.

## No slower (Flycast, function tier; one run each side, same build options, same slot; no speed claim)

| measure | before | after |
|---|---|---|
| push 290 KB (loader) | 0.848 s, 222.3 KB/s goodput | 0.890 s, 212.4 KB/s |
| program's link up / end to beacon | 1.28 s / 5.23 s | 1.25 s / 5.24 s |
| three screen grabs, held in | 639 / 661 / 659 ms | 643 / 661 / 659 ms |
| live data 20 s: pictures, live copy mean, sends per picture | 1197, 2.84 us, 37.81 | 1197, 2.83 us, 37.76 |
| planted 2 ms control | passes | passes |
| network disc small read mean / worst | 16.49 / 21.51 ms | 17.06 / 26.67 ms |
| network disc whole file, 2K sequential | 0.343, 0.345 MB/s | 0.348, 0.344 MB/s |
| /pc file 1 MiB through the C library | 3,374 ms | 3,365 ms |

The small-read mean moved by 0.57 ms. Earlier gates of the same code read 16.7, 17.04 and 17.15 ms, so this is within
the run-to-run spread. The receive ring dropped nothing in any run.

## How to re-measure

1. Build the loader folder (`make -C loader`, objects in /dev/shm) or run the gate (`tools/gate/gate.sh`): the map is
   `liveprog.map`.
2. `python3 tools/gate/sizes.py --map liveprog.map --objs client.o,cl_util.o,dcql_return.o,dcql_net_bba.o,dcql_live.o --out <dir>`
   gives the row.
3. Per symbol: `sh-elf-nm -S --size-sort liveprog.elf`, keeping the `b`/`B`/`d`/`D` symbols whose addresses fall in
   those objects' `.bss.*` / `.data.*` input sections in the map (the objects are built with `-fdata-sections`).
4. Stacks: a run's report carries `cl-mem: stack dial+rx U of 16384, tx U of 8192; rings rx R tx T; drops rx D tx D`
   (the most each static stack has used, by a fill pattern).
