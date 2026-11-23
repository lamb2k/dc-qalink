# Fixed memory of the link (dc-qalink 0.2)

The row (plan section 3): the fixed memory (`.data` + `.bss`) of the link and live data that a program carries, at
most **73,728 B** (72 KB). It is measured by the gate (`tools/gate/sizes.py`) from `liveprog.map`, the link map of the
test program `loader/liveprog.c`, over the objects `client.o`, `cl_util.o`, `dcql_return.o`, `dcql_net_bba.o` and
`dcql_live.o`. Tier: the link map, exact bytes.

## Before and after

| | before (v0.2 2ac5297) | after (lane ql-mem) |
|---|---|---|
| fixed memory, link + live | 124,327 B: OVER | AFTER_TOTAL |
| code and constant data | 19,333 B | AFTER_CODE |

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
| `g_rx_def`, `g_tx_def` | dcql_net_bba.o | 4,096 each | the built-in rings, for a program that supplies none | **heap at start**: allocated by the first `dcql_net_start()` only when the program supplied no rings; never freed |
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
heap when the link starts (`thd_create_ex` without a stack pointer), as before.

STACKS

## How to re-measure

1. Build the loader folder (`make -C loader`, objects in /dev/shm) or run the gate (`tools/gate/gate.sh`): the map is
   `liveprog.map`.
2. `python3 tools/gate/sizes.py --map liveprog.map --objs client.o,cl_util.o,dcql_return.o,dcql_net_bba.o,dcql_live.o --out <dir>`
   gives the row.
3. Per symbol: `sh-elf-nm -S --size-sort liveprog.elf`, keeping the `b`/`B`/`d`/`D` symbols whose addresses fall in
   those objects' `.bss.*` / `.data.*` input sections in the map (the objects are built with `-fdata-sections`).
4. Stacks: a run's report carries `cl-mem: stack dial+rx U of 16384, tx U of 8192; rings rx R tx T; drops rx D tx D`
   (the most each static stack has used, by a fill pattern).
