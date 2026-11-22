/* client.c: the control loop's client library (dc-qalink 0.2; first written for the RE4 Dreamcast console loop, lane
   eng-loop, 2026-11-19). API and use: cl_client.h. Wire format and hand-off area: include/dcql_loop.h. MIT licence.

   The way back to the loader, in order: (1) the resident return stub the loader armed in the hand-off area
   (lib/dcql_return.c jumps to it; it reads LOADER.BIN from the mounted disc with the BIOS's disc calls and checks its
   SHA-256); (2) without one, the loader image preloaded at cl_init, copied down with KallistiOS's exec trampoline
   from the hand-off area's scratch words; (3) a BIOS reboot. All work from an exception handler: no disc access, no
   allocation, interrupts off. */
#include <kos.h>
#include <arch/arch.h>
#include <arch/irq.h>
#include <arch/timer.h>
#include <kos/cache.h>
#include <kos/fs.h>
#include <kos/nmmgr.h>
#include <dc/pvr.h>
#include <dc/maple.h>
#include <dc/spu.h>
#include <dc/wdt.h>
#include <dc/vblank.h>
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "cl_client.h"
#include "cl_core.h"
#include "dcql_net.h"
#include "dcql_live.h"
#include "dcql_hw.h"
#include <dc/video.h>
#include <dc/biosfont.h>

/* the program's stack and heap end below the hand-off area (dcql_loop.h) */
uintptr_t arch_stack_16m = CL_HANDOFF_ADDR;
uintptr_t arch_stack_32m = CL_HANDOFF_ADDR + 0x1000000u;

extern uint32_t _arch_exec_template[], _arch_exec_template_values[], _arch_exec_template_end[];
extern uint32_t _arch_old_sr, _arch_old_vbr, _arch_old_fpscr;
void dcql_jump_stub(void) __attribute__((noreturn));      /* lib/dcql_return.c */
void dcql_bios_reboot(void) __attribute__((noreturn));

#define H ((cl_handoff_t *)CL_HANDOFF_ADDR)
static int g_active;
static uint8_t *g_loader;
static uint32_t g_loader_len;
static uint64_t g_deadline_ms;
static volatile int g_leaving;

int cl_active(void) { return g_active; }
const char *cl_args(void) { return g_active ? H->h.args : ""; }
uint32_t cl_run_id(void) { return g_active ? H->h.run_id : 0; }
static int has_arg(const char *w) {
    const char *a = cl_args();
    const size_t n = strlen(w);
    for (const char *p = strstr(a, w); p; p = strstr(p + 1, w))
        if ((p == a || p[-1] == ' ') && (p[n] == 0 || p[n] == ' ')) return 1;
    return 0;
}

void cl_report_bytes(const void *p, size_t n) {
    if (!g_active) return;
    const uint32_t cap = CL_REPORT_CAP(&H->h);
    uint32_t len = H->h.report_len;
    if (len > cap) len = cap;
    if (n > cap - len) n = cap - len;
    memcpy(H->report + len, p, n);
    H->h.report_len = len + (uint32_t)n;
    cl_handoff_seal(H);   /* the header stays valid at every moment: a reboot or power-off mid-run reads as LOST */
}
void cl_report(const char *fmt, ...) {
    if (!g_active) return;
    char b[512];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(b, sizeof(b), fmt, ap);
    va_end(ap);
    if (n > 0) cl_report_bytes(b, (size_t)n < sizeof(b) ? (size_t)n : sizeof(b) - 1);
}

/* ---------------------------------------------------------------- the SH-4 watchdog, fed from the vertical blank */
#define WTCNT (*(volatile uint16_t *)0xffc00008)
#define WTCSR (*(volatile uint16_t *)0xffc00004)
static volatile uint32_t g_wdt_feeds, g_wdt_gap_us;
static volatile uint64_t g_wdt_last_us;
static volatile int g_wdt_armed;
static int g_vbl_handle = -1;
static void vbl_feed(uint32_t code, void *data) {
    (void)code; (void)data;
    if (g_wdt_armed) WTCNT = 0x5a00;
    const uint64_t now = timer_us_gettime64();
    if (g_wdt_last_us && g_wdt_feeds > 1) {
        const uint64_t gap = now - g_wdt_last_us;
        if (gap > g_wdt_gap_us) g_wdt_gap_us = (uint32_t)gap;
    }
    g_wdt_last_us = now;
    ++g_wdt_feeds;
}
static void wdt_start(int arm) {
    g_vbl_handle = vblank_handler_add(vbl_feed, NULL);
    if (!arm || g_vbl_handle < 0) return;
    WTCSR = 0xa500 | 0x40 | 7;   /* watchdog mode, power-on reset, Pck/4096 (TME off while CKS is set) */
    WTCNT = 0x5a00;
    WTCSR = 0xa500 | 0x80 | 0x40 | 7;   /* TME: 256 counts of Pck/4096, about 21 ms at 50 MHz */
    g_wdt_armed = 1;
    H->h.flags |= CL_F_WDT_ARMED;
    cl_handoff_seal(H);
}
static void wdt_stop(void) {
    if (g_wdt_armed) { WTCSR = 0xa500; g_wdt_armed = 0; }
    H->h.wdt_feeds = g_wdt_feeds;
    H->h.wdt_gap_us = g_wdt_gap_us;
}

/* ---------------------------------------------------------------- the in-game link (adapter) */
static int g_link;                  /* the link was started */
static volatile int g_link_up, g_link_failed;
static volatile uint32_t g_link_up_ms;   /* ms from cl_init to the link up (adapter, address) */
/* rx: one file-service answer (CL_FREAD_MAX in 1 KiB datagrams) with room to spare; tx: lines and still blocks */
static unsigned char g_rx_mem[24 * 1024], g_tx_mem[16 * 1024];
static mutex_t g_req_mx = MUTEX_INITIALIZER;
static semaphore_t g_req_sem = SEM_INITIALIZER(0), g_up_sem = SEM_INITIALIZER(0);
static volatile uint32_t g_req_id, g_req_blocks, g_req_got, g_req_len;
static uint8_t *volatile g_req_buf;
static volatile int32_t g_req_status;
static volatile int g_req_type;
static volatile uint32_t g_req_handle, g_req_size;
static uint8_t g_req_bits[CL_FREAD_MAX / CL_CHUNK / 8 + 1];
static volatile uint32_t g_still_index, g_still_missing;
static uint8_t g_still_bits[(640 * 480 * 2 / CL_CHUNK) / 8 + 16];
static volatile int g_still_acked;
static volatile uint32_t g_line_seq;
static cl_link_stats_t g_st;

static void link_out(uint8_t type, uint32_t a, uint16_t b, const void *p, uint16_t n) {
    uint8_t d[CL_DGRAM_MAX];
    const size_t len = cl_frame(d, type, H->h.run_id, a, b, p, n);
    if (len) dcql_net_send(d, (unsigned)len);
}
static void link_out_wait(uint8_t type, uint32_t a, uint16_t b, const void *p, uint16_t n) {
    uint8_t d[CL_DGRAM_MAX];
    const size_t len = cl_frame(d, type, H->h.run_id, a, b, p, n);
    for (int i = 0; len && i < 2000; ++i) {
        if (dcql_net_send(d, (unsigned)len)) return;
        thd_sleep(1);
    }
}
static int live_send(uint8_t type, uint32_t a, uint16_t b, const void *p, uint16_t n) {
    uint8_t d[CL_DGRAM_MAX];
    const size_t len = cl_frame(d, type, H->h.run_id, a, b, p, n);
    return len && dcql_net_send(d, (unsigned)len);
}
/* GRAB from the PC: the displayed picture, read from video memory in pieces on the grab thread (cl_still blocks until
   the PC holds it, and its answers arrive on the link thread) */
static semaphore_t g_grab_sem = SEM_INITIALIZER(0);
static volatile uint32_t g_grab_req;
static int still_send(const void *pix, unsigned w, unsigned hgt, unsigned stride, unsigned picture, uint32_t req);
static void *grab_thread(void *arg) {
    (void)arg;
    for (;;) {
        sem_wait(&g_grab_sem);
        const uint32_t req = g_grab_req;
        const uint32_t sof = *(volatile uint32_t *)0xa05f8050 & 0x7fffffu;   /* FB_R_SOF1: the picture on screen */
        if (!vid_mode || vid_mode->pm != PM_RGB565) { dbgio_printf("cl: grab: only RGB565 screens\n"); continue; }
        still_send((const void *)(0xa5000000u + sof), vid_mode->width, vid_mode->height, vid_mode->width * 2,
                   dcql_live_picture(), req);
    }
    return NULL;
}
static void client_hello(void) {
    uint8_t pl[CL_HELLO_LEN];
    memset(pl, 0, sizeof(pl));
    cl_wr32(pl + 0, H->h.boot_count);
    cl_wr32(pl + 4, (uint32_t)timer_ms_gettime64());
    cl_wr32(pl + 52, dcql_net_ip());
    strncpy((char *)pl + 64, "client", 31);
    cl_wr32(pl + 96, CL_VERSION);
    cl_wr32(pl + 100, CL_ROLE_CLIENT);
    cl_wr32(pl + 104, CL_NET_BBA);
    cl_wr32(pl + 120, H->h.run_id);   /* run_id's offset (was 116, inside loader_sha8: the PC read run 0) */
    dcql_net_mac(pl + CL_HELLO_MAC);
    cl_wr16(pl + CL_HELLO_SLOT, (uint16_t)H->h.reserved[CL_RSV_GDEMU_SLOT]);
    link_out(CL_HELLO, 0, 0, pl, sizeof(pl));
}
static void __attribute__((noreturn)) finish(uint32_t state, int code);
static void summary(void);

static int g_diag;                  /* args "diag=1": the link's first datagrams and a heartbeat on the serial line */
/* args "diag=1" (ql-core2): every step of the client in large text on the lower part of the screen (the BIOS font at
   twice its size, 26 characters, 5 rows that wrap), held about a second so a TV or a photo can read it; and on the
   serial line ("cl-step"). Only on a 640-wide RGB565 screen; never from an exception handler. */
static unsigned g_diag_row;
static uint64_t g_t0_ms;
static void diag_text(uint16_t colour, const char *b) {
    if (!vid_mode || vid_mode->pm != PM_RGB565 || vid_mode->width != 640 || vid_mode->height < 480) return;
    static uint16_t g[BFONT_HEIGHT * BFONT_THIN_WIDTH];
    const unsigned row = g_diag_row++ % 5;
    uint16_t *base = vram_s + (250 + row * 46) * 640;
    for (int y = 0; y < 2 * BFONT_HEIGHT + 2; ++y)
        for (int x = 0; x < 640; ++x) base[y * 640 + x] = 0x0008;   /* the row cleared (dark blue) */
    for (int k = 0; k < 26 && b[k]; ++k) {
        bfont_draw_ex(g, BFONT_THIN_WIDTH, colour, 0x0008, 16, true, (uint8_t)b[k], false, false);
        uint16_t *dst = base + 8 + k * 24;
        for (int y = 0; y < BFONT_HEIGHT; ++y)
            for (int x = 0; x < BFONT_THIN_WIDTH; ++x) {
                const uint16_t p = g[y * BFONT_THIN_WIDTH + x];
                dst[(2 * y) * 640 + 2 * x] = dst[(2 * y) * 640 + 2 * x + 1] = p;
                dst[(2 * y + 1) * 640 + 2 * x] = dst[(2 * y + 1) * 640 + 2 * x + 1] = p;
            }
    }
}
static void diag_step(uint16_t colour, const char *fmt, ...) {
    if (!g_diag) return;
    char b[64];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(b, sizeof(b), fmt, ap);
    va_end(ap);
    dbgio_printf("cl-step %u ms: %s\n", (unsigned)(timer_ms_gettime64() - g_t0_ms), b);
    diag_text(colour, b);
    if (irq_inside_int()) return;
    thd_sleep(1000);
}
static volatile uint32_t g_rx_seen, g_rx_bad;
static void on_datagram(const uint8_t *d, int n) {
    const int len = cl_unframe(d, (size_t)n);
    ++g_rx_seen;
    if (g_diag && g_rx_seen <= 6)
        dbgio_printf("cl: from the PC: %d bytes type %02x frame %d\n", n, n > 3 ? d[3] : 0, len);
    if (g_rx_seen == 1) diag_step(0x07e0, "CL 5 PC ANSWERED %u MS", (unsigned)(timer_ms_gettime64() - g_t0_ms));
    if (len < 0) { ++g_rx_bad; return; }
    const uint8_t type = d[3];
    const uint32_t session = cl_rd32(d + 4), a = cl_rd32(d + 8);
    const uint16_t b = cl_rd16(d + 14);
    const uint8_t *pl = d + CL_HDR;
    switch (type) {
    case CL_PING: client_hello(); break;
    case CL_RESET:
        if (session && session != H->h.run_id) break;
        link_out(CL_RESETACK, H->h.run_id, 0, NULL, 0);
        for (int i = 0; i < 30 && dcql_net_tx_queued(); ++i) thd_sleep(10);
        thd_sleep(50);
        cl_report("\nRESET by the PC\n");
        summary();
        finish(CL_RUN_RESET, 0);
    /* the request's words are set and cleared by the asking thread with interrupts off: an answer to an older request
       never lands in the next request's buffer */
    case CL_FINFO: {
        const irq_mask_t m = irq_disable();
        if (a == g_req_id && g_req_type == CL_FOPEN && len >= (int)sizeof(cl_finfo_t)) {
            g_req_status = (int32_t)cl_rd32(pl);
            g_req_handle = cl_rd32(pl + 4);
            g_req_size = cl_rd32(pl + 8);
            sem_signal(&g_req_sem);
        }
        irq_restore(m);
        break;
    }
    case CL_FDATA: {
        const irq_mask_t m = irq_disable();
        uint8_t *const dst = g_req_buf;
        if (a == g_req_id && g_req_type == CL_FREAD && dst && b < g_req_blocks) {
            const uint32_t off = (uint32_t)b * CL_CHUNK;
            const uint32_t want = g_req_len - off < CL_CHUNK ? g_req_len - off : CL_CHUNK;
            if ((uint32_t)len == want && !(g_req_bits[b >> 3] & (1u << (b & 7)))) {
                memcpy(dst + off, pl, want);
                g_req_bits[b >> 3] |= (uint8_t)(1u << (b & 7));
                if (++g_req_got == g_req_blocks) sem_signal(&g_req_sem);
            }
        }
        irq_restore(m);
        break;
    }
    case CL_GRAB:
        g_grab_req = a;
        sem_signal(&g_grab_sem);
        break;
    case CL_STILLACK:
        if (a == g_still_index) {
            const unsigned nb = (unsigned)len < sizeof(g_still_bits) ? (unsigned)len : sizeof(g_still_bits);
            memcpy(g_still_bits, pl, nb);
            g_still_missing = b;
            g_still_acked = 1;
            sem_signal(&g_req_sem);
        }
        break;
    default:
        dcql_live_datagram(type, a, b, pl, len, live_send);
        break;
    }
}
/* the path to the PC proven before the link counts as up (ql-core3): an ARP answer from the PC (or the gateway, for a PC
   on another network), asked again every second, 30 s at most. A switch port that has just seen the adapter's link
   come up can forward nothing for seconds (spanning tree's listening and learning); with DHCP that hides in DHCP's
   own resends, but a static address sent its only HELLO into it (the console run of 2026-11-22: `static=1`, no link
   line, no report). Returns the ms it took, or -1 when nothing answered (the link is then used anyway). */
int net_arp_query(netif_t *nif, const uint8_t ip[4]);   /* kernel/net/net_arp.c */
static int g_path_ms = -2;
static int path_wait(void) {
    netif_t *nif = net_default_dev;
    uint32_t host = H->h.link_host, me, mask, gw;
    if (!nif || !host || host == 0xffffffffu) return -2;
    memcpy(&me, nif->ip_addr, 4);
    memcpy(&mask, nif->netmask, 4);
    memcpy(&gw, nif->gateway, 4);
    if (mask && (host & mask) != (me & mask) && gw) host = gw;
    uint8_t ip[4], mac[6];
    memcpy(ip, &host, 4);
    const uint64_t t0 = timer_ms_gettime64();
    for (int i = 0; i < 120; ++i) {
        if (net_arp_lookup(nif, ip, mac, NULL, NULL, 0) == 0) return (int)(timer_ms_gettime64() - t0);
        if (i % 4 == 3) net_arp_query(nif, ip);
        thd_sleep(250);
    }
    return -1;
}
static void *link_thread(void *arg) {
    (void)arg;
    static uint8_t d[CL_DGRAM_MAX + 8];
    while (dcql_net_state() != DCQL_LINK_UP) {
        if (dcql_net_state() == DCQL_LINK_FAILED) {
            cl_report("cl-link: adapter failed %d after %u ms\n", dcql_net_err(), (unsigned)(timer_ms_gettime64() - g_t0_ms));
            g_link_failed = 1;
            sem_signal(&g_up_sem);   /* a waiter learns at once, not at its deadline */
            diag_step(0xf800, "CL LINK FAILED %d", dcql_net_err());
            return NULL;
        }
        thd_sleep(20);
    }
    g_path_ms = path_wait();
    if (g_path_ms >= 0) diag_step(0x07e0, "CL 3 PATH TO PC %d MS", g_path_ms);
    else diag_step(0xffe0, "CL 3 NO ARP ANSWER");
    g_link_up = 1;
    g_link_up_ms = (uint32_t)(timer_ms_gettime64() - g_t0_ms);
    dcql_live_link(1);
    sem_signal(&g_up_sem);
    {
        const unsigned char *ip = (const unsigned char *)&(unsigned){dcql_net_ip()};
        diag_step(0x07e0, "CL 3 UP %u.%u.%u.%u %u MS", ip[0], ip[1], ip[2], ip[3], (unsigned)g_link_up_ms);
    }
    client_hello();
    diag_step(0xffff, "CL 4 HELLO SENT");
    uint64_t last = 0, hello_at = timer_ms_gettime64();
    int hellos = 1;
    for (;;) {   /* answers at once; live data out every 20 ms (a batch of about one picture) */
        const int n = dcql_net_recv_wait(d, sizeof(d), 20);
        if (n > 0) on_datagram(d, n);
        const uint64_t now = timer_ms_gettime64();
        if (now - last >= 20) { last = now; dcql_live_pump(live_send); }
        /* HELLO again each second until the PC has sent anything, 10 times at most: one lost HELLO is not a lost run */
        if (!g_rx_seen && hellos < 10 && now - hello_at >= 1000) { client_hello(); hello_at = now; ++hellos; }
    }
    return NULL;
}
int cl_link_up(void) { return g_link_up; }
int cl_link_wait(int timeout_ms) {
    if (!g_link || g_link_failed) return 0;
    if (g_link_up) return 1;
    sem_wait_timed(&g_up_sem, timeout_ms > 0 ? timeout_ms : 1);
    if (g_link_up) sem_signal(&g_up_sem);
    return g_link_up;
}
void cl_line(const char *text, size_t n) {
    if (!g_link_up || !text) return;
    if (n > 1000) n = 1000;
    uint8_t d[CL_DGRAM_MAX];
    const size_t len = cl_frame(d, CL_LINE, H->h.run_id, g_line_seq++, 0, text, (uint16_t)n);
    if (len && dcql_net_send(d, (unsigned)len)) ++g_st.lines;
    else ++g_st.lines_dropped;
}
void cl_link_stats(cl_link_stats_t *s) { *s = g_st; }

/* one request to the PC's file service: FOPEN (path) or FREAD (handle, offset, len) */
static int pc_request(int type, const void *pl, uint16_t n, uint8_t *buf, uint32_t len) {
    if (!cl_link_wait(8000)) return -EIO;
    mutex_lock(&g_req_mx);
    int rc = -ETIMEDOUT;
    irq_mask_t m = irq_disable();
    g_req_id++;
    g_req_type = type;
    g_req_buf = buf;
    g_req_len = len;
    g_req_blocks = (len + CL_CHUNK - 1) / CL_CHUNK;
    g_req_got = 0;
    g_req_status = 1;
    memset(g_req_bits, 0, sizeof(g_req_bits));
    irq_restore(m);
    while (sem_trywait(&g_req_sem) == 0) {}
    for (int tries = 0; tries < 12; ++tries) {
        link_out_wait((uint8_t)type, g_req_id, 0, pl, n);
        sem_wait_timed(&g_req_sem, tries < 3 ? 400 : 1000);
        if (tries == 3) dbgio_printf("cl: the PC's file service has not answered request %u (type %d) in 1.2 s\n",
                                     (unsigned)g_req_id, type);
        if (type == CL_FOPEN && g_req_status != 1) { rc = 0; break; }
        if (type == CL_FREAD && g_req_got == g_req_blocks) { rc = 0; break; }
    }
    m = irq_disable();
    g_req_buf = NULL;
    g_req_type = 0;
    irq_restore(m);
    mutex_unlock(&g_req_mx);
    return rc;
}

typedef struct { int used; uint32_t handle, size, pos; } pcf_t;
static pcf_t g_pcf[16];
/* one read-ahead block: a C library's small buffered reads (1 KiB each through newlib's FILE) cost one round trip per
   CL_FREAD_MAX, not per call */
static uint8_t g_ra[CL_FREAD_MAX] __attribute__((aligned(32)));
static const pcf_t *g_ra_file;
static uint32_t g_ra_handle, g_ra_off, g_ra_len;
static void *pc_open(vfs_handler_t *vfs, const char *fn, int mode) {
    (void)vfs;
    if ((mode & O_MODE_MASK) != O_RDONLY || (mode & O_DIR)) { errno = EROFS; return NULL; }
    while (*fn == '/') ++fn;
    const size_t n = strlen(fn);
    if (!n || n > 400) { errno = ENOENT; return NULL; }
    const uint64_t t0 = timer_ms_gettime64();
    if (pc_request(CL_FOPEN, fn, (uint16_t)n, NULL, 0) || g_req_status < 0) {
        ++g_st.files_refused;
        errno = ENOENT;
        return NULL;
    }
    g_st.read_ms += (unsigned)(timer_ms_gettime64() - t0);
    for (int i = 0; i < 16; ++i)
        if (!g_pcf[i].used) {
            g_pcf[i] = (pcf_t){1, g_req_handle, g_req_size, 0};
            ++g_st.files_opened;
            return (void *)(intptr_t)(i + 1);
        }
    errno = EMFILE;
    return NULL;
}
static pcf_t *pcf(void *h) {
    const intptr_t i = (intptr_t)h - 1;
    return i >= 0 && i < 16 && g_pcf[i].used ? &g_pcf[i] : NULL;
}
static int pc_close(void *h) {
    pcf_t *f = pcf(h);
    if (!f) return -1;
    link_out(CL_FCLOSE, f->handle, 0, NULL, 0);
    if (g_ra_file == f) g_ra_file = NULL;
    f->used = 0;
    return 0;
}
static ssize_t pc_read(void *h, void *buf, size_t cnt) {
    pcf_t *f = pcf(h);
    if (!f) { errno = EBADF; return -1; }
    if (f->pos >= f->size) return 0;
    if (cnt > f->size - f->pos) cnt = f->size - f->pos;
    const uint64_t t0 = timer_ms_gettime64();
    size_t done = 0;
    if (cnt < CL_FREAD_MAX) {
        if (!(g_ra_file == f && g_ra_handle == f->handle && f->pos >= g_ra_off && f->pos + cnt <= g_ra_off + g_ra_len)) {
            const uint32_t n = f->size - f->pos < CL_FREAD_MAX ? f->size - f->pos : CL_FREAD_MAX;
            uint8_t pl[12];
            cl_wr32(pl, f->handle); cl_wr32(pl + 4, f->pos); cl_wr32(pl + 8, n);
            g_ra_file = NULL;
            if (pc_request(CL_FREAD, pl, sizeof(pl), g_ra, n)) { errno = EIO; return -1; }
            g_ra_file = f; g_ra_handle = f->handle; g_ra_off = f->pos; g_ra_len = n;
            ++g_st.reads;
            g_st.read_bytes += n;
        }
        memcpy(buf, g_ra + (f->pos - g_ra_off), cnt);
        f->pos += cnt;
        g_st.read_ms += (unsigned)(timer_ms_gettime64() - t0);
        return (ssize_t)cnt;
    }
    while (done < cnt) {
        const uint32_t n = cnt - done < CL_FREAD_MAX ? (uint32_t)(cnt - done) : CL_FREAD_MAX;
        uint8_t pl[12];
        cl_wr32(pl, f->handle); cl_wr32(pl + 4, f->pos); cl_wr32(pl + 8, n);
        if (pc_request(CL_FREAD, pl, sizeof(pl), (uint8_t *)buf + done, n)) { errno = EIO; return done ? (ssize_t)done : -1; }
        f->pos += n;
        done += n;
        ++g_st.reads;
        g_st.read_bytes += n;
    }
    g_st.read_ms += (unsigned)(timer_ms_gettime64() - t0);
    return (ssize_t)done;
}
static off_t pc_seek(void *h, off_t off, int whence) {
    pcf_t *f = pcf(h);
    if (!f) return -1;
    off_t p = whence == SEEK_SET ? off : whence == SEEK_CUR ? (off_t)f->pos + off : (off_t)f->size + off;
    if (p < 0) p = 0;
    if (p > (off_t)f->size) p = f->size;
    f->pos = (uint32_t)p;
    return p;
}
static off_t pc_tell(void *h) { pcf_t *f = pcf(h); return f ? (off_t)f->pos : -1; }
static size_t pc_total(void *h) { pcf_t *f = pcf(h); return f ? f->size : (size_t)-1; }
static int pc_fstat(void *h, struct stat *st) {
    pcf_t *f = pcf(h);
    if (!f) return -1;
    memset(st, 0, sizeof(*st));
    st->st_mode = S_IFREG | 0444;
    st->st_size = f->size;
    return 0;
}
static vfs_handler_t g_pc_vfs = {
    .nmmgr = {"/pc", 0, 0x00010000, 0, NMMGR_TYPE_VFS, NMMGR_LIST_INIT},
    .open = pc_open, .close = pc_close, .read = pc_read, .seek = pc_seek, .tell = pc_tell, .total = pc_total,
    .fstat = pc_fstat};

/* the bytes [off, off + n) of the packed picture (rows of w * 2 bytes), read from the caller's rows in place */
static void still_bytes(const uint8_t *pix, uint32_t row, uint32_t stride, uint32_t off, uint32_t n, uint8_t *out) {
    while (n) {
        const uint32_t y = off / row, x = off % row, k = row - x < n ? row - x : n;
        memcpy(out, pix + y * stride + x, k);
        out += k; off += k; n -= k;
    }
}
int cl_still(const void *pix, unsigned w, unsigned hgt, unsigned stride, unsigned picture) {
    return still_send(pix, w, hgt, stride, picture, 0);
}
static int still_send(const void *pix, unsigned w, unsigned hgt, unsigned stride, unsigned picture, uint32_t req) {
    if (!g_link_up || !pix || w > 640 || hgt > 480 || !w || !hgt) return -1;
    const uint8_t *src = (const uint8_t *)pix;
    const uint32_t row = w * 2, bytes = row * hgt, blocks = (bytes + CL_CHUNK - 1) / CL_CHUNK;
    uint32_t crc = 0;
    for (unsigned y = 0; y < hgt; ++y) crc = cl_crc32(crc, src + y * stride, row);
    mutex_lock(&g_req_mx);
    const uint32_t index = ++g_still_index;
    cl_still_head_t hd = {index, picture, w, hgt, CL_PIX_RGB565, bytes, crc, req};
    uint8_t blk[CL_CHUNK];
    uint8_t hb[sizeof(hd)];
    memcpy(hb, &hd, sizeof(hd));   /* both ends little-endian */
    link_out_wait(CL_STILL, CL_STILL_HEAD, (uint16_t)index, hb, sizeof(hb));
    memset(g_still_bits, 0, sizeof(g_still_bits));
    int rc = -2;
    for (int round = 0; round < 12; ++round) {
        for (uint32_t k = 0; k < blocks; ++k)
            if (!(g_still_bits[k >> 3] & (1u << (k & 7)))) {
                const uint32_t off = k * CL_CHUNK, n = bytes - off < CL_CHUNK ? bytes - off : CL_CHUNK;
                still_bytes(src, row, stride, off, n, blk);
                link_out_wait(CL_STILL, off, (uint16_t)index, blk, (uint16_t)n);
            }
        g_still_acked = 0;
        while (sem_trywait(&g_req_sem) == 0) {}
        for (int t = 0; t < 10 && !g_still_acked; ++t) {
            link_out_wait(CL_STILL, CL_STILL_END, (uint16_t)index, hb, sizeof(hb));
            sem_wait_timed(&g_req_sem, 300);
        }
        if (g_still_acked && g_still_missing == 0) { rc = 0; break; }
    }
    mutex_unlock(&g_req_mx);
    if (!rc) { ++g_st.stills; H->h.stills_sent = g_st.stills; }
    return rc;
}

static void net_log(const char *line) {
    dbgio_printf("cl: %s\n", line);
    if (g_diag && !strncmp(line, "dcql: ", 6)) diag_text(0xffe0, line + 6);   /* the link layer's steps, not held */
}
/* args "diag=1": once a second for a minute, what the link has seen (a stop in these lines is a dead scheduler) */
static void *diag_thread(void *arg) {
    (void)arg;
    for (int i = 0; i < 60; ++i) {
        thd_sleep(1000);
        unsigned tx, rx, txd, rxd;
        dcql_net_counters(&tx, &rx, &txd, &rxd);
        dbgio_printf("cl-diag %d: state %d tx %u rx %u drops %u/%u seen %u bad %u req %u status %d got %u/%u\n", i,
                     dcql_net_state(), tx, rx, txd, rxd, (unsigned)g_rx_seen, (unsigned)g_rx_bad, (unsigned)g_req_id,
                     (int)g_req_status, (unsigned)g_req_got, (unsigned)g_req_blocks);
    }
    return NULL;
}
static void link_start(void) {
    dcql_net_config_t c;
    memset(&c, 0, sizeof(c));
    c.net = DCQL_NET_BBA;
    c.host = H->h.link_host;
    c.port = (unsigned short)H->h.link_port;
    c.lport = (unsigned short)H->h.link_lport;
    /* the loader's address, asked of DHCP first (the static address if no server answers). Lab arms: "static=1" sets
       it without DHCP (in Flycast that console then receives nothing), "dhcp=1" asks DHCP for any address */
    if (!has_arg("dhcp=1")) {
        c.ip = H->h.console_ip; c.mask = H->h.net_mask; c.gw = H->h.net_gw;
        c.dhcp_first = !has_arg("static=1");
    }
    c.rx_mem = g_rx_mem; c.rx_size = sizeof(g_rx_mem);
    c.tx_mem = g_tx_mem; c.tx_size = sizeof(g_tx_mem);
    c.log = net_log;
    dcql_net_config(&c);
    {
        const unsigned char *ip = (const unsigned char *)&c.ip;
        diag_step(0xffff, "CL 2 LINK %s %u.%u.%u.%u", c.dhcp_first ? "DHCP" : c.ip ? "STATIC" : "DHCP ANY", ip[0], ip[1],
                  ip[2], ip[3]);
    }
    if (dcql_net_start() < 0) { diag_step(0xf800, "CL LINK START FAILED"); return; }
    nmmgr_handler_add(&g_pc_vfs.nmmgr);
    if (g_diag) {
        kthread_attr_t d;
        memset(&d, 0, sizeof(d));
        d.create_detached = true;
        d.prio = 2;
        d.label = "cl-diag";
        thd_create_ex(&d, diag_thread, NULL);
    }
    kthread_attr_t a;
    memset(&a, 0, sizeof(a));
    a.create_detached = true;
    a.prio = PRIO_DEFAULT;
    a.label = "cl-link";
    a.stack_size = 16384;
    if (thd_create_ex(&a, link_thread, NULL)) g_link = 1;
    a.label = "cl-grab";
    a.stack_size = 8192;
    thd_create_ex(&a, grab_thread, NULL);
}

/* ---------------------------------------------------------------- the way back */
static void seal(uint32_t state, int code) {
    H->h.state = state;
    H->h.exit_code = code;
    if (H->h.report_len > CL_REPORT_CAP(&H->h)) H->h.report_len = CL_REPORT_CAP(&H->h);
    H->h.lines_sent = g_st.lines;
    H->h.report_crc = cl_crc32(0, H->report, H->h.report_len);
    cl_handoff_seal(H);
}

/* without an armed stub: the preloaded loader, copied down by KallistiOS's exec trampoline from the scratch words */
static void __attribute__((noreturn)) jump_preloaded(void) {
    if (!g_loader || !g_loader_len) dcql_bios_reboot();
    PVR_SET(PVR_RESET, PVR_RESET_ALL);   /* as arch_abort: graphics chip, controller DMA, sound */
    PVR_SET(PVR_RESET, PVR_RESET_NONE);
    maple_dma_stop();
    spu_disable();
    const uintptr_t ts = (uintptr_t)_arch_exec_template, te = (uintptr_t)_arch_exec_template_end;
    const size_t words = (te - ts) / 4;
    if (words > CL_TRAMP_WORDS) dcql_bios_reboot();
    uint32_t *tr = H->h.tramp;
    for (size_t i = 0; i < words; ++i) tr[i] = _arch_exec_template[i];
    uint32_t *v = tr + (((uintptr_t)_arch_exec_template_values - ts) / 4);
    H->h.tramp_stack[6] = 0xa0000000u;   /* the PR the loader's start-up saves: a loader exit reboots */
    v[0] = (uintptr_t)g_loader;
    v[1] = CL_LOAD_ADDR;
    v[2] = (g_loader_len + 3) / 4;
    v[3] = (uintptr_t)&H->h.tramp_stack[6];
    dcache_wback_range((uintptr_t)g_loader, g_loader_len + 4);
    dcache_wback_range(CL_HANDOFF_ADDR, CL_HANDOFF_SIZE);
    icache_sync_range((uintptr_t)tr, words * 4);
    __asm__ __volatile__("ldc %0, sr" : : "z"(_arch_old_sr) : "memory");
    __asm__ __volatile__("ldc %0, vbr" : : "z"(_arch_old_vbr) : "memory");
    __asm__ __volatile__("lds %0, fpscr" : : "z"(_arch_old_fpscr) : "memory");
    ((void (*)(void))tr)();
    for (;;) {}
}

static void __attribute__((noreturn)) finish(uint32_t state, int code) {
    irq_disable();
    if (!g_leaving) {   /* the first way out wins; a fault on the way out does not rewrite it */
        g_leaving = 1;
        wdt_stop();
        seal(state, code);
    }
    wdt_disable();
    dbgio_printf("\ncl: way out: %s, state %u exit %d\n", cl_stub_armed(H) ? "return stub" : g_loader ? "preloaded loader"
                 : "BIOS reboot (GDEMU image selected first)", (unsigned)H->h.state, H->h.exit_code);
    if (g_diag && state != CL_RUN_CRASHED) {   /* no font calls from a crash handler */
        diag_text(0xffe0, cl_stub_armed(H) ? "CL 7 BACK: STUB" : g_loader ? "CL 7 BACK: PRELOADED" : "CL 7 BACK: REBOOT");
        dcql_wait_vbl(60);
    }
    if (cl_stub_armed(H)) dcql_jump_stub();
    jump_preloaded();
}

/* the closing lines (not from an exception handler: they format text) */
static void summary(void) {
    if (g_leaving) return;
    cl_report("cl-wdt: %s, vertical blanks %u, longest gap %u us (the watchdog resets after about 20970 us)\n",
              g_wdt_armed ? "armed" : "measured only", (unsigned)g_wdt_feeds, (unsigned)g_wdt_gap_us);
    if (g_link_up) cl_report("cl-link: up %u ms after start (path to the PC: %s %d ms)\n", (unsigned)g_link_up_ms,
                             g_path_ms >= 0 ? "ARP answered after" : "no ARP answer in", g_path_ms >= 0 ? g_path_ms : 30000);
    if (g_link)
        cl_report("cl-link: %s, files opened %u refused %u, reads %u, %u bytes in %u ms, lines %u dropped %u, stills %u\n",
                  g_link_up ? "up" : "never up", g_st.files_opened, g_st.files_refused, g_st.reads, g_st.read_bytes,
                  g_st.read_ms, g_st.lines, g_st.lines_dropped, g_st.stills);
    if (g_link) {
        dcql_live_stats_t ls;
        dcql_live_stats(&ls);
        cl_report("cl-live: pictures %lu, records sent %lu dropped %lu, trace %lu dropped %lu, log %lu dropped %lu, "
                  "commands %lu, copy %lu us in all, link sends %lu us in %lu pumps\n", (unsigned long)ls.frames,
                  (unsigned long)ls.recs_sent, (unsigned long)ls.recs_dropped, (unsigned long)ls.trace_sent,
                  (unsigned long)ls.trace_dropped, (unsigned long)ls.logs_sent, (unsigned long)ls.logs_dropped,
                  (unsigned long)ls.cmds, (unsigned long)ls.frame_us_total, (unsigned long)ls.pump_us_total,
                  (unsigned long)ls.pumps);
    }
}

void cl_exit(int code) {
    if (!g_active) exit(code);
    diag_step(0xffff, "CL 6 EXIT %d", code);
    summary();
    finish(CL_RUN_DONE, code);
}

static void crash_handler(irq_t code, irq_context_t *ctx, void *data) {
    (void)data;
    if (!g_leaving) {
        H->h.crash_evt = code;
        H->h.crash_pc = ctx ? CONTEXT_PC(*ctx) : 0;
        H->h.crash_pr = ctx ? ctx->pr : 0;
        H->h.crash_sp = ctx ? CONTEXT_SP(*ctx) : 0;
        /* no printf in an exception handler: fixed text, hex by hand */
        char b[] = "\nCRASH event xxx pc xxxxxxxx pr xxxxxxxx sp xxxxxxxx\n";
        static const char hx[] = "0123456789abcdef";
        const uint32_t vals[4] = {(uint32_t)code, H->h.crash_pc, H->h.crash_pr, H->h.crash_sp};
        const int at[4] = {13, 20, 32, 44}, digits[4] = {3, 8, 8, 8};
        for (int k = 0; k < 4; ++k)
            for (int d = 0; d < digits[k]; ++d) b[at[k] + d] = hx[(vals[k] >> (4 * (digits[k] - 1 - d))) & 15];
        cl_report_bytes(b, sizeof(b) - 1);
        dbgio_write_str(b);   /* polled serial: the line also reaches a PC log when the stub cannot come back */
    }
    finish(CL_RUN_CRASHED, -1);
}

#ifdef CL_WRAP_ABORT
void __real_arch_abort(void);
void __wrap_arch_abort(void) {
    if (g_active && !g_leaving) {
        cl_report("\nABORT (panic or failed assert)\n");
        finish(CL_RUN_CRASHED, -3);
    }
    __real_arch_abort();
}
#endif

static void at_exit(void) {
    if (g_active && !g_leaving) { summary(); finish(CL_RUN_DONE, 0); }
}

/* one sleep to the deadline (no wakes inside timed pictures) */
static void *deadline(void *arg) {
    (void)arg;
    for (;;) {
        const uint64_t now = timer_ms_gettime64();
        if (now < g_deadline_ms) thd_sleep((int)(g_deadline_ms - now));
        if (timer_ms_gettime64() >= g_deadline_ms) {
            cl_report("\nDEADLINE %u s\n", (unsigned)H->h.deadline_s);
            summary();
            finish(CL_RUN_TIMEOUT, -2);
        }
    }
    return NULL;
}

static void preload(void) {
    FILE *f = fopen("/cd/LOADER.BIN", "rb");
    if (!f) return;
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    if (n > 0 && n < 4 * 1024 * 1024) {
        g_loader = (uint8_t *)memalign(32, (size_t)n + 4);
        if (g_loader) {
            memset(g_loader + n, 0, 4);
            if (fread(g_loader, 1, (size_t)n, f) == (size_t)n) g_loader_len = (uint32_t)n;
            else { free(g_loader); g_loader = NULL; }
        }
    }
    fclose(f);
}

int cl_init(unsigned flags) {
    if (g_active) return 1;
    g_active = cl_handoff_valid(H) && H->h.state == CL_RUN_RUNNING;
    if (!g_active) return 0;
    g_t0_ms = timer_ms_gettime64();
    g_diag = has_arg("diag=1");
    H->h.report_len = 0;
    if (g_diag) H->h.flags |= CL_F_DIAG;   /* the stub holds its colours too */
    cl_handoff_seal(H);
    diag_step(0xffff, "CL 1 START RUN %08X", (unsigned)H->h.run_id);
    if (!cl_stub_armed(H) && !(flags & CL_NO_PRELOAD)) preload();
    irq_set_handler(EXC_UNHANDLED_EXC, crash_handler, NULL);
    irq_set_handler(EXC_DOUBLE_FAULT, crash_handler, NULL);
    atexit(at_exit);
    if (H->h.deadline_s) {
        g_deadline_ms = timer_ms_gettime64() + (uint64_t)H->h.deadline_s * 1000u;
        kthread_attr_t a;
        memset(&a, 0, sizeof(a));
        a.create_detached = true;
        a.prio = 1;
        a.label = "cl-deadline";
        thd_create_ex(&a, deadline, NULL);
    }
    wdt_start(has_arg("wdt=1"));
    if (H->h.net_kind == CL_NET_BBA && !(flags & CL_NO_LINK) && !has_arg("net=0")) link_start();
    else diag_step(0x8410, "CL 2 NO LINK (NOT ASKED)");
    return 1;
}
