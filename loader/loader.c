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
     net auto|bba|modem      ip a.b.c.d mask a.b.c.d gw a.b.c.d (adapter: the address asked of DHCP, and used as a
                             static address when no DHCP server answers; default DHCP alone)   dhcp no (ip without DHCP)
     host a.b.c.d|peer       (peer: beacons to the whole network, the first PC to answer is kept)
     port 52060   lport 52060   number 1111111 (modem)
     slot 158                (the GDEMU image this loader boots from: every way back selects it, so a reboot lands here,
                             not in openMenu; GDEMU cannot be asked which image it serves)
     return reboot|stub      (ql-core4; default reboot, owner 2026-11-22: every ending of a program sends its END to the
                             PC and restarts the console into this loader. stub: the quick in-RAM return stub, armed
                             only when asked)
     dhcp always             (ql-core4: DHCP even when the last run's saved record carries the address; by default a
                             loader that finds the record starts at once on that address, the fast way back)
     guard 1                 (ql-core4: the SH-4 watchdog over this loader's own start, until its link is up or
                             90 s: a start that hangs restarts the console. Off by default: no emulator shows the
                             watchdog's reset, and a false one here would be a boot loop)

   Autoconnect: once picked in openMenu it needs nobody. It beacons once a second until a PC answers and shows LINKED;
   when no PC has answered for 10 s (none yet, or the PC went away or changed address) it beacons to the whole network
   as well and takes the first PC that answers. After every way back (stub, disc switch, reboot) it starts the same.

   The screen says what it is doing, for a photo: three large lines at the top (the console's address, the PC it looks
   for, beacons sent and answers heard) can be read off a TV. A on the pad restarts the link at once. */
#include <kos.h>
#include <arch/exec.h>
#include <arch/cache.h>
#include <dc/biosfont.h>
#include <dc/video.h>
#include <dc/maple.h>
#include <dc/maple/controller.h>
#include <dc/cdrom.h>
#include <dc/vblank.h>
#include <arpa/inet.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include "cl_core.h"
#include "dcql_net.h"
#include "cl_arm.h"
#include "dcql_hw.h"

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
typedef struct { char number[24], host[32], net[8], ip[20], mask[20], gw[20], dhcp[8], ret[8]; unsigned port, lport, slot, guard; } settings_t;
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
        else if (!strcmp(k, "dhcp")) snprintf(s->dhcp, sizeof(s->dhcp), "%s", v);
        else if (!strcmp(k, "port")) s->port = (unsigned)atoi(v);
        else if (!strcmp(k, "lport")) s->lport = (unsigned)atoi(v);
        else if (!strcmp(k, "slot")) s->slot = (unsigned)atoi(v);
        else if (!strcmp(k, "return")) snprintf(s->ret, sizeof(s->ret), "%s", v);
        else if (!strcmp(k, "guard")) s->guard = (unsigned)atoi(v);
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
/* three large lines at the top (the BIOS font at twice its size, 20 characters a line) for reading off a TV across a
   room: the console's address, the PC it looks for, the beacons; the small rows below them */
#define BIG_ROWS_PX 152
static void line_at(int row, uint32_t colour, const char *fmt, ...) {
    char b[64];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(b, sizeof(b), fmt, ap);
    va_end(ap);
    if (n < 0) n = 0;
    for (int i = n; i < 53; ++i) b[i] = ' ';   /* pad: the old text goes */
    b[53] = 0;
    bfont_draw_str_ex(vram_s + (BIG_ROWS_PX + row * 24) * 640 + 4, 640, colour, 0, 16, true, b);
}
static void big_at(int line, uint32_t colour, const char *fmt, ...) {
    char b[32];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(b, sizeof(b), fmt, ap);
    va_end(ap);
    if (n < 0) n = 0;
    if (n > 26) n = 26;
    for (int i = n; i < 26; ++i) b[i] = ' ';
    b[26] = 0;
    static char last[3][27];
    static uint32_t last_colour[3];
    if (line < 0 || line > 2 || (!strcmp(last[line], b) && last_colour[line] == colour)) return;   /* drawn only on change */
    memcpy(last[line], b, 27);
    last_colour[line] = colour;
    static uint16_t g[BFONT_HEIGHT * BFONT_THIN_WIDTH];
    for (int k = 0; k < 26; ++k) {
        bfont_draw_ex(g, BFONT_THIN_WIDTH, colour, 0, 16, true, (uint8_t)b[k], false, false);
        uint16_t *dst = vram_s + (4 + line * 48) * 640 + 8 + k * 24;
        for (int y = 0; y < BFONT_HEIGHT; ++y)
            for (int x = 0; x < BFONT_THIN_WIDTH; ++x) {
                const uint16_t p = g[y * BFONT_THIN_WIDTH + x];
                dst[(2 * y) * 640 + 2 * x] = dst[(2 * y) * 640 + 2 * x + 1] = p;
                dst[(2 * y + 1) * 640 + 2 * x] = dst[(2 * y + 1) * 640 + 2 * x + 1] = p;
            }
    }
}
static const char *g_host_text = "ANY (BROADCAST)";
static int g_linked, g_ever_heard;   /* a PC answered in the last 6 s / ever since this boot */
static const char *run_state_name(uint32_t s) {
    static const char *n[] = {"NONE", "RUNNING", "DONE", "CRASHED", "TIMEOUT", "LOST (NO HAND-IN)", "RESET BY THE PC"};
    return s < 7 ? n[s] : "?";
}
/* the return stub's own record of the last way back (hand-off tramp[], read at boot before anything changes it) */
static int g_stub_seen;
static uint32_t g_stub_step, g_stub_err, g_stub_gdemu, g_stub_entries, g_stub_aux, g_stub_rung, g_stub_run, g_stub_w0,
                g_stub_sense;
/* the same record in three more places (dcql_hw.h dcql_crumb_*), read before anything of KallistiOS starts (before the
   BSS is cleared, and before KallistiOS clears sound and video RAM), so the loader can say which of them kept it across
   a BIOS reboot: the console run of 2026-11-22 lost the hand-off area on every reboot (ql-core3) */
static dcql_crumb_t g_crumb[DCQL_CRUMB_PLACES] __attribute__((section(".data")));
static uint32_t g_crumb_ok __attribute__((section(".data")));
/* the saved copies of the hand-off area (dcql_hw.h dcql_save_handoff): the one in sound RAM moved to the one in main
   RAM now, before KallistiOS clears sound RAM; main() checks it and puts it back when the area itself was lost */
static uint32_t g_save_from __attribute__((section(".data")));   /* 1 main RAM copy, 2 sound RAM copy */
static int save_looks_whole(uint32_t base) {
    const volatile cl_handoff_hdr_t *s = (const volatile cl_handoff_hdr_t *)base;
    return s->magic == CL_HANDOFF_MAGIC && s->version == CL_HANDOFF_VERSION && s->report_len <= CL_REPORT_MAX;
}
static void early_read_crumbs(void) {
    for (int k = 0; k < DCQL_CRUMB_PLACES; ++k)
        if (dcql_crumb_get(k, &g_crumb[k])) g_crumb_ok |= 1u << k;
    dcql_crumb_clear();   /* said once: a later boot by another way does not repeat it */
    if (save_looks_whole(DCQL_SAVE_RAM)) g_save_from = 1;
    else if (save_looks_whole(DCQL_SAVE_SND)) {
        const uint32_t rep = ((const volatile cl_handoff_hdr_t *)DCQL_SAVE_SND)->report_len;
        dcql_copy_words(DCQL_SAVE_RAM + CL_REPORT_OFF, DCQL_SAVE_SND + CL_REPORT_OFF, (rep + 3) & ~3u, 0);
        dcql_copy_words(DCQL_SAVE_RAM, DCQL_SAVE_SND, CL_STUB_OFF, 0);
        g_save_from = 2;
    }
    /* the adapter's own memory (ql-core4): only while its bridge is there and still set up (dcql_hw.h), before
       KallistiOS's adapter driver clears it */
    const int gaps = dcql_gaps_ready();
    if (!g_save_from && gaps && save_looks_whole(DCQL_SAVE_BBA)) {
        uint32_t rep = ((const volatile cl_handoff_hdr_t *)DCQL_SAVE_BBA)->report_len;
        const uint32_t room = DCQL_SAVE_BBA_SIZE - DCQL_SAVE_BBA_REPORT;
        if (rep > room) rep = room;   /* the report's head only: its check then fails, the header stands */
        dcql_copy_words(DCQL_SAVE_RAM + CL_REPORT_OFF, DCQL_SAVE_BBA + DCQL_SAVE_BBA_REPORT, (rep + 3) & ~3u, 0);
        dcql_copy_words(DCQL_SAVE_RAM, DCQL_SAVE_BBA, CL_STUB_OFF, 0);
        g_save_from = 3;
    }
    dcql_g2_fifo_wait();
    ((volatile cl_handoff_hdr_t *)DCQL_SAVE_SND)->magic = 0;
    if (gaps) { dcql_g2_fifo_wait(); ((volatile cl_handoff_hdr_t *)DCQL_SAVE_BBA)->magic = 0; }
}
KOS_INIT_EARLY(early_read_crumbs);
/* CL_HELLO_WB bits: 0 hand-off, 1 main RAM, 2 sound RAM, 3 video RAM (the record); 4 / 5: the area itself was lost and
   put back from the copy in main / sound RAM */
static uint32_t g_wb_where;
static uint32_t restore_handoff(void) {
    cl_handoff_t *s = (cl_handoff_t *)DCQL_SAVE_RAM;
    uint32_t r = 0;
    if (g_save_from && !cl_handoff_valid(H) && cl_handoff_valid(s)) {
        memcpy((uint8_t *)H + CL_REPORT_OFF, (const uint8_t *)s + CL_REPORT_OFF, (s->h.report_len + 3) & ~3u);
        memcpy(H, s, CL_STUB_OFF);
        r = g_save_from == 1 ? 0x10u : g_save_from == 2 ? 0x20u : 0x40u;
    }
    if (g_save_from) s->h.magic = 0;
    return r;
}
static void draw(unsigned frame, uint32_t rate_Bps) {
    enum { WHITE = 0xffff, GREY = 0x8410, GREEN = 0x07e0, YELLOW = 0xffe0, RED = 0xf800, CYAN = 0x07ff };
    char t[64];
    static const char spin[4] = {'|', '/', '-', '\\'};
    const cl_core_t *c = &g_core;
    const int up = dcql_net_state() == DCQL_LINK_UP;
    const unsigned char *ip = (const unsigned char *)&(unsigned){dcql_net_ip()};
    if (up) big_at(0, GREEN, "IP %u.%u.%u.%u", ip[0], ip[1], ip[2], ip[3]);
    else if (dcql_net_kind() == DCQL_NET_MODEM) big_at(0, YELLOW, "IP: DIALLING %c", spin[frame & 3]);
    else big_at(0, YELLOW, "IP: ASKING DHCP %c", spin[frame & 3]);
    const unsigned hst = dcql_net_host();
    const unsigned char *hp = (const unsigned char *)&hst;
    if (hst && hst != 0xFFFFFFFFu) big_at(1, WHITE, "PC %u.%u.%u.%u", hp[0], hp[1], hp[2], hp[3]);
    else big_at(1, WHITE, "PC %s", g_host_text);
    if (up && g_linked) big_at(2, GREEN, "LINKED (HEARD %u)", (unsigned)c->rx_pkts);
    else if (up && g_ever_heard) big_at(2, YELLOW, "PC LOST: BEACONS %u", (unsigned)c->tx_pkts);
    else big_at(2, YELLOW, "BEACONS %u NO PC YET", (unsigned)c->tx_pkts);
    line_at(0, CYAN, "%s  %s%s", LOADER_TITLE, LOADER_BUILD, g_pushed ? " (PUSHED)" : "");
    const uint32_t gv = H->h.reserved[CL_RSV_GDEMU_VER], slot = H->h.reserved[CL_RSV_GDEMU_SLOT];
    if (gv) line_at(1, GREY, "BOOT %u PORT %u STUB %u B GDEMU %u.%u.%u SLOT %u", (unsigned)g_core.boot_count,
                    dcql_net_port(), (unsigned)H->h.stub_bytes, (unsigned)(gv >> 16), (unsigned)((gv >> 8) & 255),
                    (unsigned)(gv & 255), (unsigned)slot);
    else line_at(1, GREY, "BOOT %u PORT %u STUB %u B NO GDEMU SLOT %u", (unsigned)g_core.boot_count, dcql_net_port(),
                 (unsigned)H->h.stub_bytes, (unsigned)slot);
    dcql_net_text(t, sizeof(t));
    line_at(2, up ? GREEN : YELLOW, "LINK: %s", t);
    if (!up) {
        line_at(3, YELLOW, "NOT CONNECTED: A = START THE LINK NOW");
        line_at(4, GREY, "");
    } else if (c->state == CL_ST_RECEIVING) {
        line_at(3, WHITE, "RECEIVING %08X: %u / %u CHUNKS", (unsigned)c->session, (unsigned)c->have, (unsigned)c->nchunks);
        line_at(4, WHITE, "%u OF %u KB   %u.%02u KB/S   BAD %u DUP %u", (unsigned)(c->have * CL_CHUNK / 1024),
                (unsigned)(c->comp_len / 1024), (unsigned)(rate_Bps / 1000), (unsigned)(rate_Bps % 1000 / 10),
                (unsigned)c->rx_bad, (unsigned)c->rx_dup);
    } else if (c->state == CL_ST_VERIFIED) {
        line_at(3, GREEN, "VERIFIED %08X: %u BYTES, SHA-256 OK", (unsigned)c->session, (unsigned)c->raw_len);
        line_at(4, GREEN, "WAITING FOR RUN %c", spin[frame & 3]);
    } else if (c->state == CL_ST_FAILED) {
        line_at(3, RED, "REFUSED %08X: %s", (unsigned)c->session, cl_err_name(c->verdict));
        line_at(4, YELLOW, "NOTHING RAN. WAITING FOR THE PC %c", spin[frame & 3]);
    } else {
        line_at(3, GREEN, "WAITING FOR THE PC %c", spin[frame & 3]);
        line_at(4, GREY, "");
    }
    const cl_handoff_hdr_t *h = &g_plat.handoff->h;
    line_at(5, GREY, "LAST RUN %08X: %s%s", (unsigned)h->run_id, run_state_name(h->state),
            (h->flags & CL_F_STUB_USED) ? " (STUB)" : "");
    if (h->state == CL_RUN_CRASHED)
        line_at(6, RED, "EXC %03X PC %08X PR %08X", (unsigned)h->crash_evt, (unsigned)h->crash_pc, (unsigned)h->crash_pr);
    else
        line_at(6, GREY, "EXIT %d   REPORT %u BYTES", (int)h->exit_code, (unsigned)h->report_len);
    unsigned tx, rx, txd, rxd;
    dcql_net_counters(&tx, &rx, &txd, &rxd);
    if (g_stub_seen && g_stub_step >= CL_STUB_STEP_RUNNING)   /* the full restart's crumb (ql-core4) */
        line_at(7, g_stub_step == CL_STUB_STEP_RESTART ? GREEN : RED, "RESTART %s RUN %08X %s %c%c%c%c%c",
                g_stub_step == CL_STUB_STEP_RESTART ? "TAKEN" : "NO ENDING", (unsigned)g_stub_run,
                g_stub_step == CL_STUB_STEP_RESTART ? run_state_name(g_stub_err < 7 ? g_stub_err : 7) : "",
                g_wb_where & 0x70 ? 'P' : '-', g_wb_where & 2 ? 'R' : '-', g_wb_where & 4 ? 'S' : '-',
                g_wb_where & 8 ? 'V' : '-', g_wb_where & 0x40 ? 'B' : '-');
    else if (g_stub_seen) line_at(7, g_stub_step == CL_STUB_STEP_JUMP ? GREEN : RED, "BACK S%u R%u ERR %08X AUX %08X %c%c%c%c",
                             (unsigned)g_stub_step, (unsigned)g_stub_rung, (unsigned)g_stub_err, (unsigned)g_stub_aux,
                             g_wb_where & 1 ? 'H' : g_wb_where & 0x70 ? 'P' : '-', g_wb_where & 2 ? 'R' : '-', g_wb_where & 4 ? 'S' : '-',
                             g_wb_where & 8 ? 'V' : '-');
    else line_at(7, GREY, "LINK BYTES OUT %u IN %u DROPS %u/%u", tx, rx, txd, rxd);
    /* the last five log lines (rows 8 to 12: the screen's bottom) */
    unsigned n = g_log_n;
    for (unsigned i = 0; i < 5; ++i) {
        unsigned k = n >= 5 ? n - 5 + i : i;
        line_at(8 + (int)i, GREY, "%s", k < n ? g_log[k % LOG_LINES] : "");
    }
}

static int pad_a(void) {
    maple_device_t *d = maple_enum_type(0, MAPLE_FUNC_CONTROLLER);
    if (!d) return 0;
    cont_state_t *s = (cont_state_t *)maple_dev_status(d);
    return s && (s->buttons & CONT_A);
}

/* ---------------------------------------------------------------- the start guard (LINK.TXT "guard 1", ql-core4) */
/* The SH-4 watchdog in reset mode (as the client's: 256 counts of Pck/4096, about 21 ms), fed from the vertical blank
   only while the guard's time is not spent: a start that stops with interrupts off (no vertical blank) or that never
   gets its link up (the time runs out) restarts the console through the BIOS, like any way back. */
/* ql-core5: the registers from dcql_hw.h (WTCSR is 0xffc0000c; this guard wrote 0xffc00004, STBCR, until then) */
static volatile uint64_t g_guard_until;
static volatile int g_guard_on;
static void guard_feed(uint32_t code, void *data) {
    (void)code; (void)data;
    if (g_guard_on && timer_ms_gettime64() < g_guard_until) dcql_wdt_feed();
}
static void guard_start(unsigned seconds) {
    if (vblank_handler_add(guard_feed, NULL) < 0) return;
    g_guard_until = timer_ms_gettime64() + seconds * 1000ull;
    g_guard_on = 1;
    dcql_wdt_arm();
    loader_log("start guard: the watchdog for %u s (WTCSR %02x)", seconds, dcql_wdt_csr());
}
static void guard_stop(const char *why) {
    if (!g_guard_on) return;
    dcql_wdt_off();
    g_guard_on = 0;
    loader_log("start guard off: %s", why);
}

/* ---------------------------------------------------------------- the jump */
static int g_ret_stub;   /* LINK.TXT "return stub": the quick way back armed; else the full restart (the default) */
int net_arp_query(netif_t *nif, const uint8_t ip[4]);   /* kernel/net/net_arp.c */
static void do_exec(void) {
    guard_stop("jumping");
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
    if (g_ret_stub) cl_arm_stub(H);
    else {   /* the full restart: no stub armed, so nothing can take it by mistake */
        H->h.stub_bytes = 0;
        H->h.flags |= CL_F_RET_REBOOT;
    }
    cl_handoff_seal(H);
    arch_exec_at(c->raw, (c->raw_len + 3) & ~3u, CL_LOAD_ADDR);
}

/* the start's steps on the TV (ql-core4): until the main loop's first picture the screen was black, so a start that
   stopped (the console after the stub's jump, 2026-11-22) showed nothing; now the last step reached stays on screen */
static void start_step(const char *what) {
    big_at(0, 0xffe0, "START: %s", what);
    dbglog(DBG_INFO, "loader: start: %s\n", what);
}
int main(int argc, char **argv) {
    (void)argc; (void)argv;
    vid_set_mode(DM_640x480, PM_RGB565);
    vid_clear(0, 0, 0);
    start_step("1 SETTINGS (DISC)");
    settings_t s;
    memset(&s, 0, sizeof(s));
    strcpy(s.number, "1111111");
    strcpy(s.host, "peer");
    strcpy(s.net, "auto");
    s.port = s.lport = CL_PORT;
    read_settings(&s);
    start_step("2 GDEMU");
    /* GDEMU's version (nothing answers on a GD-ROM drive or in Flycast: bounded, harmless), before any disc work */
    irq_mask_t im = irq_disable();
    const uint32_t gver = dcql_gdemu_version();
    if (!gver) (void)dcql_gd_req_error();   /* refused: the drive's "illegal request" read, so no later command sees it */
    irq_restore(im);
    g_ret_stub = !strcmp(s.ret, "stub");
    if (s.guard) guard_start(90);   /* after the probe: its bounded waits run with interrupts off */
    start_step("3 LAST RUN");
    memset(&g_plat, 0, sizeof(g_plat));
    g_plat.send = p_send; g_plat.now_ms = p_now; g_plat.alloc = p_alloc; g_plat.free = p_free; g_plat.log = p_log;
    g_plat.handoff = H;
    g_plat.max_raw = CL_HANDOFF_ADDR - 0x11000u - CL_LOAD_ADDR;
    g_plat.build = "loader " LOADER_BUILD;
    /* the area lost on a BIOS reboot: put back from its saved copy (header and report) */
    const int top_ok = cl_handoff_valid(H);
    const uint32_t restored = restore_handoff();
    if (restored) loader_log("hand-off lost on the reboot: put back from the copy in %s (run %08x, report %u B)",
                             restored == 0x10u ? "main RAM" : restored == 0x20u ? "sound RAM" : "the adapter's memory",
                             (unsigned)H->h.run_id, (unsigned)H->h.report_len);
    else if (g_save_from && !top_ok) loader_log("hand-off lost and its saved copy not whole: fresh");
    /* the last way back: the hand-off header's words first, else the first place that kept the record */
    const int in_hdr = cl_handoff_valid(H) && H->h.tramp[CL_STUBW_MAGIC] == CL_STUB_MAGIC;
    g_wb_where = (top_ok && in_hdr ? 1u : 0u) | (g_crumb_ok << 1) | restored;
    if (in_hdr) {
        g_stub_seen = 1;
        g_stub_step = H->h.tramp[CL_STUBW_STEP]; g_stub_err = H->h.tramp[CL_STUBW_ERR];
        g_stub_gdemu = H->h.tramp[CL_STUBW_GDEMU]; g_stub_entries = H->h.tramp[CL_STUBW_ENTRIES];
        g_stub_aux = H->h.tramp[CL_STUBW_GDST]; g_stub_rung = H->h.tramp[CL_STUBW_RUNG];
        g_stub_w0 = H->h.tramp[CL_STUBW_W0]; g_stub_run = H->h.run_id; g_stub_sense = H->h.tramp[CL_STUBW_SENSE];
        H->h.tramp[CL_STUBW_MAGIC] = 0;   /* said once: a later boot by another way does not repeat it */
    } else {
        for (int k = 0; k < DCQL_CRUMB_PLACES && !g_stub_seen; ++k)
            if (g_crumb_ok & (1u << k)) {
                const dcql_crumb_t *c = &g_crumb[k];
                g_stub_seen = 1;
                g_stub_step = c->step; g_stub_err = c->err; g_stub_aux = c->aux; g_stub_entries = c->entries;
                g_stub_w0 = c->w0; g_stub_run = c->run_id;
            }
    }
    if (g_stub_seen) {   /* and in every HELLO of this boot (cl_core_init copies g_plat) */
        g_plat.stub_rec[0] = g_stub_step; g_plat.stub_rec[1] = g_stub_err;
        g_plat.stub_rec[2] = g_stub_gdemu; g_plat.stub_rec[3] = g_stub_entries;
    }
    if (g_stub_seen) {
        loader_log("way back of run %08x: step %u rung %u err %08x aux %08x", (unsigned)g_stub_run,
                   (unsigned)g_stub_step, (unsigned)g_stub_rung, (unsigned)g_stub_err, (unsigned)g_stub_aux);
        loader_log("drive sense %08x (rung 2), w0 %08x", (unsigned)g_stub_sense, (unsigned)g_stub_w0);
        loader_log("kept by: hand-off %s, RAM 8M %s, sound %s, video %s; w0 %08x gdemu %x",
                   top_ok && in_hdr ? "yes" : restored ? "restored" : "no",
                   g_crumb_ok & 1 ? "yes" : "no", g_crumb_ok & 2 ? "yes" : "no", g_crumb_ok & 4 ? "yes" : "no",
                   (unsigned)g_stub_w0, (unsigned)g_stub_gdemu);
    }
    const uint32_t wb[4] = {g_wb_where, g_stub_aux, g_stub_run, g_stub_sense};
    memcpy(g_plat.wb, wb, sizeof(wb));
    cl_core_init(&g_core, &g_plat);   /* reads the last run's hand-off before anything grows */
    /* the full restart lost the area and every saved copy, but the run's crumb names it (ql-core4): the run is handed
       in as the crumb says (step 17: the ending it took, without its report; step 16: it ended without a word, the
       watchdog, a reset or the power), so the PC tool learns which run came back and how */
    if (!top_ok && !restored && !in_hdr && g_stub_seen && g_stub_run &&
        (g_stub_step == CL_STUB_STEP_RUNNING || g_stub_step == CL_STUB_STEP_RESTART)) {
        H->h.run_id = g_stub_run;
        H->h.state = g_stub_step == CL_STUB_STEP_RUNNING || g_stub_err < CL_RUN_DONE || g_stub_err > CL_RUN_RESET
                     ? CL_RUN_LOST : g_stub_err;
        H->h.exit_code = g_stub_step == CL_STUB_STEP_RESTART ? (int32_t)g_stub_aux : 0;
        H->h.report_len = 0;
        H->h.report_crc = 0;
        cl_handoff_seal(H);
        if (g_stub_step == CL_STUB_STEP_RUNNING) g_wb_where |= 0x80u;
        g_core.p.wb[0] = g_wb_where;
        loader_log("run %08x named by its crumb only: %s", (unsigned)g_stub_run,
                   g_stub_step == CL_STUB_STEP_RUNNING ? "ended without a word (watchdog, reset, power)" : run_state_name(H->h.state));
    }
    g_pushed = cl_handoff_valid(H) && H->h.reserved[3] == PUSH_MAGIC;
    uint8_t pushed_sha8[8];
    memcpy(pushed_sha8, &H->h.reserved[4], 8);
    H->h.reserved[3] = 0;
    H->h.reserved[CL_RSV_GDEMU_SLOT] = s.slot <= 999 ? s.slot : 0;
    H->h.reserved[CL_RSV_GDEMU_VER] = gver;
    cl_handoff_seal(H);
    if (gver) loader_log("GDEMU %u.%u.%u, loader image %u", (unsigned)(gver >> 16), (unsigned)((gver >> 8) & 255),
                         (unsigned)(gver & 255), s.slot);
    else loader_log("no GDEMU answered; image %u%s", s.slot, s.slot ? " (selected anyway on the way back)" : "");
    start_step("4 LOADER.BIN (DISC)");
    const int hr = cl_arm_find();
    if (hr) loader_log("no way back by the stub: LOADER.BIN %d", hr);
    else loader_log("LOADER.BIN at sector %u, %u bytes", (unsigned)cl_arm_lba(), (unsigned)cl_arm_len());
    if (g_ret_stub) cl_arm_stub(H);
    else { H->h.stub_bytes = 0; cl_handoff_seal(H); }
    loader_log("way back: %s", g_ret_stub ? "the return stub (LINK.TXT return stub)" : "full restart (BIOS reboot)");
    /* the fast start (ql-core4): the address, mask and gateway the last run's saved record carries (this loader held
       them seconds before the restart, and the program used them since), set at once without DHCP. No DHCP fallback:
       asking DHCP on the live adapter afterwards lost the link in Flycast (ev\f1); the address was proven seconds ago,
       and autoconnect still finds a PC that moved. Without a record: DHCP as before ("dhcp no" + "ip" is static) */
    unsigned fast_ip = 0, fast_mask = 0, fast_gw = 0;
    if (restored && H->h.net_kind == CL_NET_BBA && H->h.console_ip && strcmp(s.dhcp, "always")) {
        fast_ip = H->h.console_ip; fast_mask = H->h.net_mask; fast_gw = H->h.net_gw;
    }
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
    nc.dhcp_first = strcmp(s.dhcp, "no") ? 2 : 0;   /* "ip" in LINK.TXT: the fallback when no DHCP server answers */
    if (fast_ip) {
        nc.ip = fast_ip; nc.mask = fast_mask; nc.gw = fast_gw;
        nc.dhcp_first = 0;
        g_wb_where |= 0x100u;
        g_core.p.wb[0] = g_wb_where;
        const unsigned char *fp = (const unsigned char *)&fast_ip;
        loader_log("fast start on %u.%u.%u.%u (the saved record), no DHCP", fp[0], fp[1], fp[2], fp[3]);
    }
    nc.rx_mem = rx_mem; nc.rx_size = sizeof(rx_mem);
    nc.tx_mem = tx_mem; nc.tx_size = sizeof(tx_mem);
    nc.ppp_wait_ms = 45000;
    nc.log = net_log;
    dcql_net_config(&nc);
    loader_log("net %s host %s port %u lport %u", s.net, s.host, s.port, s.lport);
    start_step("5 LINK");
    dcql_net_start();
    g_core.p.net_kind = (uint32_t)dcql_net_kind();
    g_core.p.number = dcql_net_number();
    g_core.p.hello_flags = g_pushed ? CL_HF_PUSHED_LOADER : 0;
    memcpy(g_core.p.loader_sha8, g_pushed ? pushed_sha8 : cl_arm_sha(), 8);
    uint64_t next_try = 0, last_draw = 0, rate_t = 0;
    uint32_t rate_have = 0, rate = 0;
    unsigned frame = 0;
    int was_up = 0, a_was = 0;
    uint64_t heard_ms = 0, up_ms = 0;
    uint32_t rx_seen = 0;
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
                dcql_net_mac(g_core.p.mac);
                was_up = 1;
                up_ms = now;
                guard_stop("link up");
                loader_log("link up %u ms after start", (unsigned)now);
                const unsigned hst = dcql_net_host();   /* the PC's ARP asked at once: the first beacon is not lost */
                if (net_default_dev && hst && hst != 0xffffffffu) net_arp_query(net_default_dev, (const uint8_t *)&hst);
            }
            g_core.p.link_host = dcql_net_host();   /* learnt from the PC's first datagram */
            int n;
            while ((n = dcql_net_recv(buf, sizeof(buf))) >= 0) {
                if (n > 0) cl_core_input(&g_core, buf, (size_t)n);
                if (g_core.exec_pending) break;
            }
            cl_core_tick(&g_core);
            if (g_core.rx_pkts != rx_seen) { rx_seen = g_core.rx_pkts; heard_ms = now; g_ever_heard = 1; }
            g_linked = g_ever_heard && now - heard_ms < 6000;
            const uint64_t quiet_since = heard_ms > up_ms ? heard_ms : up_ms;
            if (now - quiet_since >= 10000 && !dcql_net_learning()) {   /* autoconnect: any PC that answers */
                loader_log(g_ever_heard ? "PC lost 10 s: looking for any PC" : "no PC in 10 s: looking for any PC");
                dcql_net_relearn();
            }
        } else {
            if (st == DCQL_LINK_FAILED) guard_stop("link failed: the loader waits");
            g_linked = 0;
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
