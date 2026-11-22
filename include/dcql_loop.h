/* dcql_loop.h: dc-qalink's control loop, one versioned header for the wire format and the hand-off area. MIT licence.
 *
 * Shared by the loader (loader/loader.c), the client library (loader/client.c), the return stub (loader/stub.c), the
 * host test build (loader/host.c) and, by value, the PC tool (pc/dcql_loop.py: keep the two in step).
 *
 * WIRE FORMAT (UDP, little-endian). Datagram: 16-byte header, payload, CRC-32 (IEEE, zlib's) of header + payload, at
 * most CL_DGRAM_MAX bytes (the modem path reassembles no IP fragments):
 *
 *   0  'C' 'L'   magic           8  u32 a      (chunk index, offset, request id ...)
 *   2  u8 ver    CL_VERSION     12  u16 len    payload bytes
 *   3  u8 type                  14  u16 b      (count, flags ...)
 *   4  u32 session              16  payload, then u32 CRC-32 of bytes 0..16+len
 *
 * A datagram whose magic is right but whose version is not CL_VERSION is answered with ERR CL_E_VERSION (a = the
 * console's version) and otherwise ignored: the loader refuses a PC tool of another protocol version, and the PC tool
 * refuses a loader of another (it reads the version byte of the console's HELLO before anything else).
 *
 * Version 2 (dc-qalink 0.2): v1 (the RE4 console loop of 2026-11-19) plus the adapter transport, the client's link
 * (lines, stills, the /pc file service, RESET), the return stub and the self-update flag on LOAD.
 *
 * HAND-OFF AREA: the top 128 KiB of main RAM. KallistiOS puts the main stack at the weak symbol arch_stack_16m and keeps
 * the heap below it; the loader and every client program define arch_stack_16m = CL_HANDOFF_ADDR (and arch_stack_32m =
 * CL_HANDOFF_ADDR + 16 MiB), so the area survives the jump to the program, its run and the way back.
 *
 *   CL_HANDOFF_ADDR   +0x0000  header (cl_handoff_hdr_t, sealed by a CRC)
 *   CL_STUB_ADDR      +0x1000  the return stub (code + its stack), armed by the loader before every run
 *   CL_REPORT_ADDR    +0x3000  the run's report (CL_REPORT_MAX bytes; a launcher may cap it, report_cap)
 */
#ifndef DCQL_LOOP_H
#define DCQL_LOOP_H
#include <stdint.h>
#include <stddef.h>

/* ------------------------------------------------------------------------------------------------ wire format */
#define CL_MAGIC0 'C'
#define CL_MAGIC1 'L'
#define CL_VERSION 2
#define CL_HDR 16
#define CL_DGRAM_MAX 1200
#define CL_PAYLOAD_MAX (CL_DGRAM_MAX - CL_HDR - 4)
#define CL_CHUNK 1024
#define CL_PORT 52060
#define CL_LOAD_ADDR 0x8c010000u
#define CL_ARGS_MAX 512
#define CL_ACK_BITS 2048 /* chunks covered by an ACK's bitmap after the cumulative point */

enum {
    /* console -> PC */
    CL_HELLO = 0x01, CL_ACK = 0x02, CL_VERDICT = 0x03, CL_REPORT = 0x04, CL_EXEC = 0x05, CL_ERR = 0x06,
    CL_LINE = 0x07,      /* v2: a = line sequence, payload = text (the program's serial lines, live) */
    CL_STILL = 0x08,     /* v2: a = byte offset (or CL_STILL_HEAD / CL_STILL_END), b = still index, payload = bytes */
    CL_FOPEN = 0x09,     /* v2: a = request id, payload = path below /pc/ */
    CL_FREAD = 0x0a,     /* v2: a = request id, payload = cl_fread_t */
    CL_FCLOSE = 0x0b,    /* v2: a = handle (no answer) */
    CL_RESETACK = 0x0c,  /* v2: a = run id: the program is going back to the loader */
    /* PC -> console */
    CL_LOAD = 0x10, CL_DATA = 0x11, CL_RUN = 0x12, CL_REPQ = 0x13, CL_REPDONE = 0x14, CL_PING = 0x15,
    CL_ABORT = 0x16, CL_VERIFY = 0x17,
    CL_RESET = 0x18,     /* v2: a = run id (0 = whatever runs): back to the loader through the return stub */
    CL_FINFO = 0x19,     /* v2: answer to FOPEN: a = request id, payload = cl_finfo_t */
    CL_FDATA = 0x1a,     /* v2: answer to FREAD: a = request id, b = block index, payload = up to CL_CHUNK bytes */
    CL_STILLACK = 0x1b,  /* v2: a = still index, b = blocks still missing, payload = bitmap of blocks held */
    /* v2 live data (dc-qalink 0.2 bridge; lib/dcql_live.h, pc/bridge/wire.py; additive: old ends ignore them).
       console -> PC: REC records, LIVEDEF the program's declaration, TRACE numbered events, CMDACK; LINE with b = 1..4
       is a game log line (payload u32 picture + text). PC -> console: SUB channels, GRAB the displayed picture as a
       STILL (its head's reserved word = the request), CMD pause / resume / step / warp / option / pad. */
    CL_REC = 0x0d, CL_LIVEDEF = 0x0e, CL_TRACE = 0x0f, CL_CMDACK = 0x20,
    CL_SUB = 0x1c, CL_GRAB = 0x1d, CL_CMD = 0x1e
};

/* VERDICT / ERR codes */
enum {
    CL_OK = 0, CL_E_COMP_CRC = 1, CL_E_INFLATE = 2, CL_E_RAW_LEN = 3, CL_E_SHA = 4, CL_E_SIZE = 5, CL_E_ADDR = 6,
    CL_E_NOMEM = 7, CL_E_NOT_VERIFIED = 8, CL_E_SESSION = 9, CL_E_BAD_LOAD = 10, CL_E_CODEC = 11, CL_E_NO_REPORT = 12,
    CL_E_BUSY = 13, CL_E_VERSION = 14
};

/* loader states (HELLO) */
enum { CL_ST_IDLE = 0, CL_ST_RECEIVING = 1, CL_ST_VERIFIED = 2, CL_ST_FAILED = 3, CL_ST_EXEC = 4 };
/* who beacons (HELLO role) */
enum { CL_ROLE_LOADER = 1, CL_ROLE_CLIENT = 2 };
/* transports (HELLO net, hand-off net_kind) */
enum { CL_NET_NONE = 0, CL_NET_MODEM = 1, CL_NET_BBA = 2 };

/* codecs */
enum { CL_CODEC_NONE = 0, CL_CODEC_DEFLATE = 1 };
/* LOAD flags */
#define CL_LOAD_F_LOADER 1u   /* v2: the image is a newer loader: run it as the loader (self-update), not as a program */

/* LOAD payload */
typedef struct {
    uint32_t comp_len, raw_len, chunk_size, nchunks;
    uint8_t codec, flags;
    uint16_t deadline_s;
    uint32_t load_addr;
    uint8_t sha256[32];
    uint32_t comp_crc;
    uint16_t args_len;
    uint16_t reserved;
    /* args follow (args_len bytes, no terminator) */
} cl_load_t;
#define CL_LOAD_FIXED 64

/* HELLO payload: v1's 96 bytes, then v2's 32 */
typedef struct {
    uint32_t boot_count, uptime_ms, state, session;
    uint32_t have, nchunks, rx_bytes, rx_bad;
    uint32_t report_run, report_state, report_len, report_crc;
    int32_t report_exit;
    uint32_t console_ip, max_raw, verdict;
    char build[32];
    /* v2 */
    uint32_t proto, role, net, stub_bytes;
    uint8_t loader_sha8[8];   /* first 8 bytes of the SHA-256 of the loader this console returns to */
    uint32_t run_id, flags;   /* role CLIENT: the run; flags: CL_HF_* */
    /* v2, additive (ql-core2, 2026-11-22): a PC that reads only 128 bytes ignores these */
    uint8_t mac[6];           /* offset 128: the adapter's MAC address (all zero on the modem): one console across
                                 DHCP address changes */
    uint16_t gdemu_slot;      /* offset 134: the GDEMU image the loader returns to (0 = none known) */
    /* v2, additive (ql-data, 2026-11-22): the stub's record of the last way back as the loader read it at boot, so
       the PC can say where a way back stopped without the TV; all zero from a client or when there was no record
       (ql-core3: also from a copy of the record that outlived a BIOS reboot; stub_gdemu is then 0) */
    uint8_t stub_step;        /* offset 136: CL_STUB_STEP_* the last way back reached (0 = no record this boot) */
    uint8_t stub_entries;     /* offset 137: times the stub was entered (low 8 bits) */
    uint16_t stub_gdemu;      /* offset 138: tramp[CL_STUBW_GDEMU]: the GDEMU select's result + 0x100 (0 = none) */
    uint32_t stub_err;        /* offset 140: tramp[CL_STUBW_ERR] (one byte per rung, ql-core3) */
    uint32_t gdemu_ver;       /* offset 144: GDEMU's version, major << 16 | minor << 8 | patch (0 = none answered) */
    /* v2, additive (ql-core3, 2026-11-22): where the record came through and the rest of it */
    uint32_t wb_where;        /* offset 148: bit 0 the hand-off header, 1 main RAM at 8 MiB, 2 sound RAM, 3 video RAM
                                 held the record; 4 / 5: the hand-off area was lost on a reboot and put back from its
                                 copy in main / sound RAM (dcql_hw.h); 0 = no record anywhere */
    uint32_t wb_aux;          /* offset 152: tramp[CL_STUBW_GDST]: the BIOS's last answer */
    uint32_t wb_run;          /* offset 156: the run the record belongs to */
    uint32_t wb_sense;        /* offset 160: tramp[CL_STUBW_SENSE]: the drive's last sense (0 from a copy) */
} cl_hello_t;
#define CL_HELLO_LEN 164
#define CL_HELLO_MAC 128
#define CL_HELLO_SLOT 134
#define CL_HELLO_STUB 136
#define CL_HELLO_GDEMU_VER 144
#define CL_HELLO_WB 148
#define CL_HF_PUSHED_LOADER 1u   /* this loader was pushed by the PC (self-update), not read from the disc */
#define CL_HF_STUB_ARMED 2u

/* the client's file service (/pc) */
typedef struct { uint32_t handle, offset, len; } cl_fread_t;              /* len <= CL_FREAD_MAX */
typedef struct { int32_t status; uint32_t handle, size, reserved; } cl_finfo_t;   /* status 0 = open, <0 = refused */
#define CL_FREAD_MAX (8u * 1024u)   /* one answer must fit the adapter's 16 KiB receive ring with room to spare */
/* stills: a header block, the data blocks, an end block (the PC answers the end with STILLACK) */
#define CL_STILL_HEAD 0xffffffffu
#define CL_STILL_END 0xfffffffeu
typedef struct { uint32_t index, picture, width, height, format, bytes, crc, reserved; } cl_still_head_t;
enum { CL_PIX_RGB565 = 1 };

/* little-endian helpers (the SH-4 and x86 are both little-endian; these keep the code honest elsewhere) */
static inline uint32_t cl_rd32(const uint8_t *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}
static inline uint16_t cl_rd16(const uint8_t *p) { return (uint16_t)(p[0] | (p[1] << 8)); }
static inline void cl_wr32(uint8_t *p, uint32_t v) {
    p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); p[2] = (uint8_t)(v >> 16); p[3] = (uint8_t)(v >> 24);
}
static inline void cl_wr16(uint8_t *p, uint16_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }

/* ------------------------------------------------------------------------------------------------ hand-off area */
#define CL_HANDOFF_ADDR 0x8cfe0000u
#define CL_HANDOFF_SIZE 0x20000u
#define CL_HANDOFF_MAGIC 0x31484c43u   /* "CLH1" */
#define CL_HANDOFF_VERSION 2
#define CL_STUB_OFF 0x1000u
#define CL_STUB_MAX 0x2000u            /* code + stack */
#define CL_STUB_ADDR (CL_HANDOFF_ADDR + CL_STUB_OFF)
#define CL_REPORT_OFF (CL_STUB_OFF + CL_STUB_MAX)
#define CL_REPORT_MAX (CL_HANDOFF_SIZE - CL_REPORT_OFF)

enum { CL_RUN_NONE = 0, CL_RUN_RUNNING = 1, CL_RUN_DONE = 2, CL_RUN_CRASHED = 3, CL_RUN_TIMEOUT = 4, CL_RUN_LOST = 5,
       CL_RUN_RESET = 6 /* v2: the PC sent RESET */ };

/* header flags */
#define CL_F_WDT_ARMED 1u      /* the client armed the SH-4 watchdog (reset mode) */
#define CL_F_STUB_USED 2u      /* the way back went through the return stub */
#define CL_F_DIAG 4u           /* args diag=1: the client and the stub hold each step on screen about a second */
#define CL_F_STUB_NOBIOS 8u    /* lab arm (testprog "stubata"): the stub skips its BIOS rung (the drive's own read) */

#define CL_TRAMP_WORDS 32
typedef struct {
    uint32_t magic, version;
    uint32_t header_crc;          /* CRC-32 of the header from boot_count to tramp */
    uint32_t boot_count;          /* loader boots since the area was made (power-on resets it) */
    uint32_t run_id;              /* the PC's session of the program that runs / ran */
    uint32_t state;               /* CL_RUN_* */
    int32_t exit_code;
    uint32_t deadline_s;          /* 0 = none */
    uint32_t flags;               /* CL_F_* */
    uint32_t link_host, link_port, link_lport, console_ip;   /* the loader's link (network order host / ip) */
    char number[24];
    char args[512];               /* the PC's arguments, NUL-terminated */
    uint32_t crash_evt, crash_pc, crash_pr, crash_sp;
    uint32_t loader_len;          /* v1: bytes of LOADER.BIN preloaded; v2: bytes the stub reads back */
    uint32_t report_len, report_crc;
    uint32_t report_cap;          /* 0 = CL_REPORT_MAX; else the client writes at most this many report bytes */
    /* v2 */
    uint32_t net_kind;            /* CL_NET_* the loader used */
    uint32_t net_mask, net_gw;    /* the adapter's netmask and gateway (network order) */
    uint32_t ret_lba;             /* the GD sector of LOADER.BIN on the mounted disc (0 = unknown: no stub way back) */
    uint8_t ret_sha[32];          /* SHA-256 of LOADER.BIN: the stub runs it only if it matches */
    uint32_t stub_bytes, stub_crc;   /* the armed stub (CRC-32 of its bytes at CL_STUB_ADDR) */
    uint32_t wdt_feeds, wdt_gap_us;  /* the watchdog's feeds and the longest gap between two (measured) */
    uint32_t lines_sent, stills_sent;
    uint32_t reserved[8];         /* [CL_RSV_BIOS_*]: the SR, VBR and FPSCR the BIOS started the loader with */
    uint32_t tramp[CL_TRAMP_WORDS];   /* scratch, not covered by the CRC (the stub's progress words) */
    uint32_t tramp_stack[8];
} cl_handoff_hdr_t;

enum { CL_RSV_BIOS_SR = 0, CL_RSV_BIOS_VBR = 1, CL_RSV_BIOS_FPSCR = 2,
       CL_RSV_GDEMU_SLOT = 6,  /* the loader's GDEMU image (LINK.TXT "slot"; 0 = none): every way back selects it */
       CL_RSV_GDEMU_VER = 7 }; /* GDEMU's firmware version as the loader read it (dcql_hw.h), 0 = no GDEMU answered */
#define CL_HANDOFF_HDR_CRC_FROM 12u
#define CL_HANDOFF_HDR_CRC_TO ((uint32_t)__builtin_offsetof(cl_handoff_hdr_t, tramp))
/* the report bytes a client may write: the launcher's cap when it set one */
#define CL_REPORT_CAP(hdr) ((hdr)->report_cap && (hdr)->report_cap < CL_REPORT_MAX ? (hdr)->report_cap : CL_REPORT_MAX)

typedef struct {
    cl_handoff_hdr_t h;
    uint8_t pad[CL_STUB_OFF - sizeof(cl_handoff_hdr_t)];
    uint8_t stub[CL_STUB_MAX];
    uint8_t report[CL_REPORT_MAX];
} cl_handoff_t;

/* the stub's progress words (tramp[]): where a way back stopped, for the next loader to say */
enum { CL_STUBW_MAGIC = 0, CL_STUBW_STEP = 1, CL_STUBW_ERR = 2, CL_STUBW_ENTRIES = 3 };
#define CL_STUB_MAGIC 0x42545353u   /* "SSTB" */
enum { CL_STUB_STEP_ENTERED = 1, CL_STUB_STEP_QUIET = 2, CL_STUB_STEP_GDINIT = 3, CL_STUB_STEP_READ = 4,
       CL_STUB_STEP_SHA = 5, CL_STUB_STEP_JUMP = 6, CL_STUB_STEP_REBOOT = 7,
       CL_STUB_STEP_GDEMU = 8 /* v2 (ql-core2): the loader's GDEMU image selected */,
       CL_STUB_STEP_ATA = 9 /* ql-core3: rung 2, the drive's own read */ };
/* tramp[CL_STUBW_ERR] (ql-core3): one byte per rung of the stub, 0 = not tried, else (stage << 4) | code:
     byte 0  rung 1, the BIOS's disc calls          stage 1 drive not ready (CMD_INIT), 2 no way back recorded,
     byte 1  rung 2, the drive's own read (no BIOS)       3 read, 4 SHA-256 mismatch; code = the failing call's
     byte 2  rung 3, after the GDEMU image select         -return (1..15)
     byte 3  the GDEMU select: 0 not tried, 0x10 | -code (0x10 = taken, 0x14 = refused: no GDEMU)
   tramp[CL_STUBW_GDST]: the BIOS's last answer: err1 | err2 << 8 | -(last call) << 16 | CMD_INIT tries << 24
   (err1 2 = not ready, 6 = unit attention / disc changed). tramp[CL_STUBW_RUNG]: the rung that came back (1..3).
   tramp[CL_STUBW_GDEMU]: the select's result + 0x100 (before the reboot: | (0x200 + code) << 12) */
enum { CL_STUBW_GDEMU = 4, CL_STUBW_GDST = 5, CL_STUBW_RUNG = 6, CL_STUBW_W0 = 7,
       CL_STUBW_SENSE = 8 /* the drive's last sense the stub read and cleared before a rung (key | ASC << 8 | ASCQ << 16;
                             0xff no answer; bit 24: rung 2 had a read refused) */ };
/* the screen colour of each stub step (dcql_hw.h dcql_screen_colour): the stub has no text */
#define CL_STUB_COLOURS {0x000000, 0x0000ff /* 1 entered: blue */, 0x00ffff /* 2 quiet: cyan */,     0xffff00 /* 3 GD init: yellow */, 0xff8000 /* 4 read: orange */, 0x00ff00 /* 5 SHA ok: green */,     0xffffff /* 6 jump: white */, 0xff0000 /* 7 reboot: red */, 0xff00ff /* 8 GDEMU select: magenta */,     0x8000ff /* 9 the drive's own read: violet */}

/* ------------------------------------------------------------------------------------------------ helpers */
uint32_t cl_crc32(uint32_t crc, const void *buf, size_t n);  /* zlib-compatible: start with 0 */
typedef struct { uint32_t h[8]; uint64_t len; uint8_t buf[64]; uint32_t n; } cl_sha256_t;
void cl_sha256_init(cl_sha256_t *s);
void cl_sha256_update(cl_sha256_t *s, const void *p, size_t n);
void cl_sha256_final(cl_sha256_t *s, uint8_t out[32]);
/* raw deflate (RFC 1951, no zlib header). 0 = ok and *out_len set; <0 = malformed input or the output would not fit.
   Never reads past src + src_len or writes past dst + dst_cap. */
int cl_inflate(const uint8_t *src, size_t src_len, uint8_t *dst, size_t dst_cap, size_t *out_len);
/* datagram framing: returns the total length, or 0 if it would not fit */
size_t cl_frame(uint8_t *out, uint8_t type, uint32_t session, uint32_t a, uint16_t b, const void *payload, uint16_t len);
/* checks magic, version, length and CRC; returns the payload length, -1 if bad, -2 if only the version differs */
int cl_unframe(const uint8_t *in, size_t n);

uint32_t cl_handoff_hdr_crc(const cl_handoff_hdr_t *h);
int cl_handoff_valid(const cl_handoff_t *a);     /* magic, version, header CRC */
void cl_handoff_seal(cl_handoff_t *a);           /* recompute the header CRC */
int cl_handoff_report_ok(const cl_handoff_t *a); /* report length in range and its CRC matches */
int cl_stub_armed(const cl_handoff_t *a);        /* a stub is there, its CRC matches and a way back is known */
#endif
