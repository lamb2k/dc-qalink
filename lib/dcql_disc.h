/* dcql_disc.h: the network disc (dc-qalink 0.2, lane ql-disc, 2026-11-22). MIT licence.
 *
 * A KallistiOS file system that takes the place of /cd (or any mount name) while a program runs under the dc-qalink
 * loader with the broadband adapter linked: files the developer changed are read from the PC (the overlay), every
 * other file falls through to the real disc (GDEMU); or, with a whole disc image served by the PC, every file comes
 * from the PC. The program's code is unchanged: it opens "/cd/..." as before.
 *
 *   cl_init(0);                               the client library (loader/cl_client.h), link on the adapter
 *   dcql_disc_mount(NULL);                    defaults: "/cd", the PC decides overlay or whole disc
 *   ... fopen("/cd/DATA/LEVEL1.BIN", "rb") ... unchanged code
 *   dcql_disc_report();                       one "cl-disc:" line with the counters into the run's report
 *
 * Fixed memory (no heap, ever): the read cache DCQL_DISC_CACHE bytes (default 32 KiB: 4 blocks of 8 KiB, which also
 * hold the read-ahead), the overlay table DCQL_DISC_TABLE_N entries of 8 bytes (default 512: 4 KiB), 16 open files,
 * 6 requests, a 1 KiB log buffer: about 42 KiB in all (the map file is the measure; docs/NETDISC.md).
 *
 * Waits: every network wait has a time limit (config timeout_ms per request, default 3000 ms, with resends of only the
 * missing 1 KiB pieces); a read that cannot finish in it fails with EIO, never hangs. Reads ahead never block the
 * caller: the game thread waits only inside its own read call, for the blocks that call needs.
 *
 * Wire (include/dcql_loop.h, additive to protocol 2; pc/dcql_disc.py is the PC side):
 *   DOPEN  0x27 console->PC  a = request id; payload u32 flags (DCQL_DO_*), path below the disc root
 *   DINFO  0x2a PC->console  a = request id; payload i32 status (0 open, <0 refused), u32 handle, u32 size, u32 flags
 *   DREAD  0x28 console->PC  a = request id; payload u32 handle, offset, len (<= 32 KiB), mask (1 KiB pieces wanted),
 *                            prev_id, prev_us (the console's own time for its previous finished request)
 *   DDATA  0x2b PC->console  a = request id, b = piece index; payload up to 1024 bytes
 *   DCLOSE 0x29 console->PC  a = handle; payload prev_id, prev_us
 *   DLOG   0x2c console->PC  a = sequence; payload: the program's reads as it made them (file, offset, bytes, us,
 *                            side), sequential reads of one file merged into one record
 */
#ifndef DCQL_DISC_H
#define DCQL_DISC_H
#include <stdint.h>

#ifndef DCQL_DISC_CACHE
#define DCQL_DISC_CACHE (32u * 1024u)
#endif
#ifndef DCQL_DISC_TABLE_N
#define DCQL_DISC_TABLE_N 512
#endif
#define DCQL_DISC_FILES 16
#define DCQL_DISC_REQS 6

enum {
    DCQL_DISC_AUTO = 0,     /* the PC says: overlay over the real disc, or the whole disc from the PC */
    DCQL_DISC_OVERLAY = 1,  /* files the PC overrides from the PC, the rest from the real disc */
    DCQL_DISC_PC = 2        /* every file from the PC (a disc image or folder served whole) */
};

/* DOPEN flags */
#define DCQL_DO_MOUNT 1u     /* the mount table (a virtual file: header + overlay entries) */
#define DCQL_DO_DIR 2u       /* a directory listing (a virtual file of entries) */
#define DCQL_DO_OVERLAY 4u   /* overlay mode: open only if the PC serves this file (else status -3: use the disc) */
/* DINFO flags */
#define DCQL_DI_OVERLAY 1u   /* served from the overlay folder (a changed file) */
#define DCQL_DI_DIR 2u
/* DINFO status */
#define DCQL_DS_OK 0
#define DCQL_DS_NOENT -2
#define DCQL_DS_BASE -3      /* not overridden: read it from the real disc */

/* the mount table: this header, then count entries {u32 hash, u32 size}, sorted by hash */
#define DCQL_DISC_MAGIC 0x314d5144u   /* "DQM1" */
#define DCQL_DT_WHOLE 1u     /* the PC serves the whole disc */
#define DCQL_DT_OVERFLOW 2u  /* more overlay files than the console's table: the console asks the PC on every open */
typedef struct {
    uint32_t magic, count, flags, files;   /* files: every file of the disc as the PC sees it (manifest) */
    uint8_t manifest[16];                  /* the first 16 bytes of the manifest's SHA-256 (the build record has it) */
} dcql_disc_table_t;

/* DLOG record kinds */
enum { DCQL_DL_OPEN = 1, DCQL_DL_READ = 2, DCQL_DL_CLOSE = 3 };
enum { DCQL_SIDE_DISC = 0, DCQL_SIDE_PC = 1 };

typedef struct {
    const char *mount;       /* default "/cd" */
    int mode;                /* DCQL_DISC_* (default AUTO) */
    unsigned window;         /* requests in flight at once, 1..4 (default 2) */
    unsigned block;          /* bytes a request asks for, 1..32 KiB in whole KiB (default 8 KiB); the cache holds
                                DCQL_DISC_CACHE / block blocks */
    unsigned timeout_ms;     /* one request's time limit (default 3000) */
    unsigned link_wait_ms;   /* how long mount waits for the link (default 20000) */
    int no_log;              /* 1: no DLOG records */
} dcql_disc_config_t;

typedef struct {
    uint32_t mounted, mode, table_count, table_flags, files;
    uint32_t opens_pc, opens_disc, opens_refused, asked_pc;   /* asked_pc: DOPENs sent (table hits and overflow) */
    uint32_t reads_pc, reads_disc;                            /* the program's read calls by side */
    uint32_t bytes_pc, bytes_disc;
    uint32_t us_pc, us_disc;                                  /* time inside those read calls */
    uint32_t requests, resends, timeouts, cache_hits, readahead_hits, direct_blocks;
    uint32_t req_us_total, req_us_worst;                      /* a request's round trip, as the console saw it */
    uint32_t log_records, log_dgrams;
} dcql_disc_stats_t;

/* 0 = mounted; -1 no link, -2 the PC did not answer, -3 the PC refused (no disc served), -4 already mounted,
   -5 bad config. Without a link the real disc stays as it was. */
int dcql_disc_mount(const dcql_disc_config_t *c);
void dcql_disc_unmount(void);   /* back to the real disc; open files of the network disc are closed */
void dcql_disc_stats(dcql_disc_stats_t *s);
void dcql_disc_report(void);    /* one cl-disc line into the run's report */
uint32_t dcql_disc_hash(const char *path);   /* the table's hash of a path (FNV-1a of the normalised name) */

/* the link thread's hook (lib/dcql_live.c calls it for DINFO and DDATA; 1 = taken) */
int dcql_disc_datagram(uint8_t type, uint32_t a, uint16_t b, const uint8_t *pl, int len);
#endif
