/* loader.c: the control loop's loader (dc-qalink 0.2; first written as the RE4 Dreamcast console loop's loader, lane
   eng-loop, 2026-11-19). MIT licence.

   Boots from its own disc (a GDEMU image), brings up the link (lib/dcql_net.c: the broadband adapter if one is fitted,
   else the modem; /cd/LINK.TXT can force either), beacons HELLO on UDP 52060 and waits for the PC (pc/dcql_loop.py).
   It receives a program in checked chunks (cl_core.c), verifies it whole (stream CRC-32, bounded inflate, SHA-256),
   arms the return stub in the hand-off area, and on RUN writes the hand-off header and jumps to it with arch_exec_at.
   Every way out of the program comes back here through the stub (or the client's older paths); the next boot reads
   the run's report from the hand-off area.

   Self-update: a LOAD flagged CL_LOAD_F_LOADER is a newer loader; after its checks it runs in this one's place (the
   loader on the disc is the first stage: with no PC it simply carries on).

   /cd/LINK.TXT, one setting a line (all optional):
     net auto|bba|modem      ip a.b.c.d (adapter, static; default DHCP)   mask a.b.c.d   gw a.b.c.d
     host a.b.c.d|peer       port 52060   lport 52060   number 1111111 (modem)

   The screen says what it is doing, for a photo. A on the pad restarts the link at once. */
#include <kos.h>
#include <arch/exec.h>
#include <arch/cache.h>
#include <dc/biosfont.h>
#include <dc/video.h>
#include <dc/maple.h>
#include <dc/maple/controller.h>
#include <dc/cdrom.h>
#include <arpa/inet.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include "cl_core.h"
#include "dcql_net.h"
#include "cl_arm.h"

#ifndef LOADER_BUILD
#define LOADER_BUILD "dev"
#endif
#ifndef LOADER_TITLE
#define LOADER_TITLE "DC-QALINK LOADER"
#endif

/* the hand-off area: this program's stack and heap end below it (dcql_loop.h) */
uintptr_t arch_stack_16m = CL_HANDOFF_ADDR;
uintptr_t arch_stack_32m = CL_HANDOFF_ADDR + 0x1000000u;

/* /cd on a GD-ROM image: KOS mounts the low-density TOC; the payload is on track 3 */
int __real_cdrom_read_toc(cd_toc_t *toc, bool hd);
int __wrap_cdrom_read_toc(cd_toc_t *toc, bool hd) {
    int rc = __real_cdrom_read_toc(toc, true);
    return rc && !hd ? __real_cdrom_read_toc(toc, false) : rc;
}

#define H ((cl_handoff_t *)CL_HANDOFF_ADDR)
#define PUSH_MAGIC 0x48535550u   /* "PUSH" in reserved[3]: this loader was pushed by the PC */

/* ---------------------------------------------------------------- log lines (screen + dbglog) */
#define LOG_LINES 6
static char g_log[LOG_LINES][54];
static volatile unsigned g_log_n;
void loader_log(const char *fmt, ...) {
    char b[160];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(b, sizeof(b), fmt, ap);
    va_end(ap);
    dbglog(DBG_INFO, "loader: %s\n", b);
    const irq_mask_t irq = irq_disable();
    unsigned i = g_log_n++ % LOG_LINES;
    strncpy(g_log[i], b, sizeof(g_log[i]) - 1);
    g_log[i][sizeof(g_log[i]) - 1] = 0;
    irq_restore(irq);
}
static void net_log(const char *line) { loader_log("%s", line); }

/* ---------------------------------------------------------------- platform for cl_core */
static int p_send(void *ctx, const uint8_t *p, size_t n) { (void)ctx; return dcql_net_send(p, (unsigned)n); }
static uint32_t p_now(void *ctx) { (void)ctx; return (uint32_t)timer_ms_gettime64(); }
static void *p_alloc(size_t n) {
    uint8_t *m = (uint8_t *)malloc(n);
    if (m && (uintptr_t)m + n > CL_HANDOFF_ADDR - 0x11000u) { free(m); return NULL; }   /* never near the hand-off */
    return m;
}
static void p_free(void *m) { free(m); }
static void p_log(void *ctx, const char *line) { (void)ctx; loader_log("%s", line); }

static cl_core_t g_core;
static cl_platform_t g_plat;
static int g_pushed;

/* ---------------------------------------------------------------- settings: /cd/LINK.TXT */
typedef struct { char number[24], host[32], net[8], ip[20], mask[20], gw[20]; unsigned port, lport; } settings_t;
static void read_settings(settings_t *s) {
    FILE *f = fopen("/cd/LINK.TXT", "r");
    if (!f) f = fopen("/cd/link.txt", "r");
    if (!f) { loader_log("no LINK.TXT: defaults"); return; }
    char line[96], k[24], v[64];
    while (fgets(line, sizeof(line), f)) {
        if (sscanf(line, "%23s %63s", k, v) != 2 || k[0] == '#') continue;
        if (!strcmp(k, "number")) snprintf(s->number, sizeof(s->number), "%s", v);
        else if (!strcmp(k, "host")) snprintf(s->host, sizeof(s->host), "%s", v);
        else if (!strcmp(k, "net")) snprintf(s->net, sizeof(s->net), "%s", v);
        else if (!strcmp(k, "ip")) snprintf(s->ip, sizeof(s->ip), "%s", v);
        else if (!strcmp(k, "mask")) snprintf(s->mask, sizeof(s->mask), "%s", v);
        else if (!strcmp(k, "gw")) snprintf(s->gw, sizeof(s->gw), "%s", v);
        else if (!strcmp(k, "port")) s->port = (unsigned)atoi(v);
        else if (!strcmp(k, "lport")) s->lport = (unsigned)atoi(v);
    }
    fclose(f);
}
static unsigned parse_ip(const char *s) {
    unsigned a[4];
    if (!s[0] || sscanf(s, "%u.%u.%u.%u", &a[0], &a[1], &a[2], &a[3]) != 4) return 0;
    unsigned char b[4] = {(unsigned char)a[0], (unsigned char)a[1], (unsigned char)a[2], (unsigned char)a[3]};
    unsigned v;
    memcpy(&v, b, 4);
    return v;
}

/* ---------------------------------------------------------------- screen */
static void line_at(int row, uint32_t colour, const char *fmt, ...) {
    char b[64];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(b, sizeof(b), fmt, ap);
    va_end(ap);
    if (n < 0) n = 0;
    for (int i = n; i < 53; ++i) b[i] = ' ';   /* pad: the old text goes */
    b[53] = 0;
    bfont_draw_str_ex(vram_s + (8 + row * 24) * 640 + 4, 640, colour, 0, 16, true, b);
}
static const char *run_state_name(uint32_t s) {
    static const char *n[] = {"NONE", "RUNNING", "DONE", "CRASHED", "TIMEOUT", "LOST (NO HAND-IN)", "RESET BY THE PC"};
    return s < 7 ? n[s] : "?";
}
static void draw(unsigned frame, uint32_t rate_Bps) {
    enum { WHITE = 0xffff, GREY = 0x8410, GREEN = 0x07e0, YELLOW = 0xffe0, RED = 0xf800, CYAN = 0x07ff };
    char t[64];
    line_at(0, CYAN, "%s  %s%s", LOADER_TITLE, LOADER_BUILD, g_pushed ? " (PUSHED)" : "");
    line_at(1, GREY, "BOOT %u  PORT %u  STUB %u B  PROTO %u", (unsigned)g_core.boot_count, dcql_net_port(),
            (unsigned)H->h.stub_bytes, (unsigned)CL_VERSION);
    dcql_net_text(t, sizeof(t));
    line_at(3, dcql_net_state() == DCQL_LINK_UP ? GREEN : YELLOW, "LINK: %s", t);
    static const char spin[4] = {'|', '/', '-', '\\'};
    const cl_core_t *c = &g_core;
    if (dcql_net_state() != DCQL_LINK_UP) {
        line_at(5, YELLOW, "NOT CONNECTED: A = START THE LINK NOW");
        line_at(6, GREY, "");
    } else if (c->state == CL_ST_RECEIVING) {
        line_at(5, WHITE, "RECEIVING %08X: %u / %u CHUNKS", (unsigned)c->session, (unsigned)c->have, (unsigned)c->nchunks);
        line_at(6, WHITE, "%u OF %u KB   %u.%02u KB/S   BAD %u DUP %u", (unsigned)(c->have * CL_CHUNK / 1024),
                (unsigned)(c->comp_len / 1024), (unsigned)(rate_Bps / 1000), (unsigned)(rate_Bps % 1000 / 10),
                (unsigned)c->rx_bad, (unsigned)c->rx_dup);
    } else if (c->state == CL_ST_VERIFIED) {
        line_at(5, GREEN, "VERIFIED %08X: %u BYTES, SHA-256 OK", (unsigned)c->session, (unsigned)c->raw_len);
        line_at(6, GREEN, "WAITING FOR RUN %c", spin[frame & 3]);
    } else if (c->state == CL_ST_FAILED) {
        line_at(5, RED, "REFUSED %08X: %s", (unsigned)c->session, cl_err_name(c->verdict));
        line_at(6, YELLOW, "NOTHING RAN. WAITING FOR THE PC %c", spin[frame & 3]);
    } else {
        line_at(5, GREEN, "WAITING FOR THE PC %c", spin[frame & 3]);
        line_at(6, GREY, "BEACONS SENT %u   RECEIVED %u", (unsigned)c->tx_pkts, (unsigned)c->rx_pkts);
    }
    const cl_handoff_hdr_t *h = &g_plat.handoff->h;
    line_at(8, GREY, "LAST RUN %08X: %s%s", (unsigned)h->run_id, run_state_name(h->state),
            (h->flags & CL_F_STUB_USED) ? " (STUB)" : "");
    if (h->state == CL_RUN_CRASHED)
        line_at(9, RED, "EXC %03X PC %08X PR %08X", (unsigned)h->crash_evt, (unsigned)h->crash_pc, (unsigned)h->crash_pr);
    else
        line_at(9, GREY, "EXIT %d   REPORT %u BYTES", (int)h->exit_code, (unsigned)h->report_len);
    unsigned tx, rx, txd, rxd;
    dcql_net_counters(&tx, &rx, &txd, &rxd);
    line_at(11, GREY, "LINK BYTES OUT %u IN %u DROPS %u/%u", tx, rx, txd, rxd);
    unsigned n = g_log_n;
    for (unsigned i = 0; i < LOG_LINES; ++i) {
        unsigned k = n >= LOG_LINES ? n - LOG_LINES + i : i;
        line_at(13 + (int)i, GREY, "%s", k < n ? g_log[k % LOG_LINES] : "");
    }
}

static int pad_a(void) {
    maple_device_t *d = maple_enum_type(0, MAPLE_FUNC_CONTROLLER);
    if (!d) return 0;
    cont_state_t *s = (cont_state_t *)maple_dev_status(d);
    return s && (s->buttons & CONT_A);
}

/* ---------------------------------------------------------------- the jump */
static void do_exec(void) {
    cl_core_t *c = &g_core;
    loader_log("exec %08x: %u bytes%s", (unsigned)c->session, (unsigned)c->raw_len, c->exec_loader ? " (loader)" : "");
    line_at(5, 0xffe0, "RUNNING %08X: %u BYTES", (unsigned)c->session, (unsigned)c->raw_len);
    line_at(6, 0x8410, c->exec_loader ? "A NEWER LOADER" : "LINK DOWN, THEN JUMPING");
    /* let EXEC leave: the ring, then the line */
    uint64_t t0 = timer_ms_gettime64();
    while (dcql_net_tx_queued() && timer_ms_gettime64() - t0 < 2000) thd_sleep(20);
    if (dcql_net_kind() == DCQL_NET_BBA) {
        thd_sleep(200);
        dcql_net_shutdown();   /* the adapter stopped; the program brings it up again on the same address */
    } else {
        thd_sleep(600);
        dcql_net_stop();
        t0 = timer_ms_gettime64();
        while ((dcql_net_busy() || dcql_net_state() != DCQL_LINK_OFF) && timer_ms_gettime64() - t0 < 8000) thd_sleep(50);
    }
    if (c->exec_loader) {
        H->h.reserved[3] = PUSH_MAGIC;
        memcpy(&H->h.reserved[4], c->sha, 8);
    }
    cl_arm_stub(H);
    arch_exec_at(c->raw, (c->raw_len + 3) & ~3u, CL_LOAD_ADDR);
}

int main(int argc, char **argv) {
    (void)argc; (void)argv;
    vid_set_mode(DM_640x480, PM_RGB565);
    vid_clear(0, 0, 0);
    settings_t s;
    memset(&s, 0, sizeof(s));
    strcpy(s.number, "1111111");
    strcpy(s.host, "peer");
    strcpy(s.net, "auto");
    s.port = s.lport = CL_PORT;
    read_settings(&s);
    memset(&g_plat, 0, sizeof(g_plat));
    g_plat.send = p_send; g_plat.now_ms = p_now; g_plat.alloc = p_alloc; g_plat.free = p_free; g_plat.log = p_log;
    g_plat.handoff = H;
    g_plat.max_raw = CL_HANDOFF_ADDR - 0x11000u - CL_LOAD_ADDR;
    g_plat.build = "loader " LOADER_BUILD;
    cl_core_init(&g_core, &g_plat);   /* reads the last run's hand-off before anything grows */
    g_pushed = cl_handoff_valid(H) && H->h.reserved[3] == PUSH_MAGIC;
    uint8_t pushed_sha8[8];
    memcpy(pushed_sha8, &H->h.reserved[4], 8);
    H->h.reserved[3] = 0;
    cl_handoff_seal(H);
    const int hr = cl_arm_find();
    if (hr) loader_log("no way back by the stub: LOADER.BIN %d", hr);
    else loader_log("LOADER.BIN at sector %u, %u bytes", (unsigned)cl_arm_lba(), (unsigned)cl_arm_len());
    cl_arm_stub(H);
    static uint8_t rx_mem[65536], tx_mem[16384];
    dcql_net_config_t nc;
    memset(&nc, 0, sizeof(nc));
    nc.net = !strcmp(s.net, "bba") ? DCQL_NET_BBA : !strcmp(s.net, "modem") ? DCQL_NET_MODEM : DCQL_NET_AUTO;
    nc.number = s.number;
    nc.host = strcmp(s.host, "peer") ? parse_ip(s.host) : 0xFFFFFFFFu;
    if (!nc.host) nc.host = 0xFFFFFFFFu;
    nc.port = (unsigned short)s.port;
    nc.lport = (unsigned short)s.lport;
    nc.ip = parse_ip(s.ip); nc.mask = parse_ip(s.mask); nc.gw = parse_ip(s.gw);
    nc.rx_mem = rx_mem; nc.rx_size = sizeof(rx_mem);
    nc.tx_mem = tx_mem; nc.tx_size = sizeof(tx_mem);
    nc.ppp_wait_ms = 45000;
    nc.log = net_log;
    dcql_net_config(&nc);
    loader_log("net %s host %s port %u lport %u", s.net, s.host, s.port, s.lport);
    dcql_net_start();
    g_core.p.net_kind = (uint32_t)dcql_net_kind();
    g_core.p.number = dcql_net_number();
    g_core.p.hello_flags = g_pushed ? CL_HF_PUSHED_LOADER : 0;
    memcpy(g_core.p.loader_sha8, g_pushed ? pushed_sha8 : cl_arm_sha(), 8);
    uint64_t next_try = 0, last_draw = 0, rate_t = 0;
    uint32_t rate_have = 0, rate = 0;
    unsigned frame = 0;
    int was_up = 0, a_was = 0;
    static uint8_t buf[1408];
    for (;;) {
        const uint64_t now = timer_ms_gettime64();
        const int st = dcql_net_state();
        if (st == DCQL_LINK_UP) {
            if (!was_up) {
                g_core.p.console_ip = dcql_net_ip();
                g_core.p.link_port = dcql_net_port();
                g_core.p.link_lport = dcql_net_lport();
                g_core.p.net_mask = dcql_net_mask(); g_core.p.net_gw = dcql_net_gw();
                was_up = 1;
            }
            g_core.p.link_host = dcql_net_host();   /* learnt from the PC's first datagram */
            int n;
            while ((n = dcql_net_recv(buf, sizeof(buf))) >= 0) {
                if (n > 0) cl_core_input(&g_core, buf, (size_t)n);
                if (g_core.exec_pending) break;
            }
            cl_core_tick(&g_core);
        } else {
            was_up = 0;
            if (!dcql_net_busy() && (st == DCQL_LINK_OFF || st == DCQL_LINK_FAILED || st == DCQL_LINK_DROPPED)) {
                if (st == DCQL_LINK_DROPPED || now >= next_try) {
                    loader_log("link start (%s)", st == DCQL_LINK_DROPPED ? "line dropped" : st == DCQL_LINK_FAILED ? "retry" : "start");
                    dcql_net_start();
                    next_try = now + (dcql_net_kind() == DCQL_NET_BBA ? 10000 : 30000);
                }
            }
        }
        if (g_core.exec_pending) do_exec();
        const int a = pad_a();
        if (a && !a_was && st != DCQL_LINK_UP) next_try = 0;
        a_was = a;
        if (now - rate_t >= 2000) {
            rate = g_core.have >= rate_have ? (uint32_t)((g_core.have - rate_have) * CL_CHUNK * 1000ull / (now - rate_t)) : 0;
            rate_have = g_core.have;
            rate_t = now;
        }
        if (now - last_draw >= 250) {
            draw(frame++, rate);
            last_draw = now;
        }
        thd_sleep(dcql_net_kind() == DCQL_NET_BBA ? 2 : 10);
    }
}
