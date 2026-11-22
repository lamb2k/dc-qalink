/* dcql_menu.c: dc-qalink's in-game menu, design B "Bar" (dcql_menu.h; guide docs/MENU.md). MIT licence.
   Font: Spleen 8x16 (BSD 2-Clause, lib/font/). Builds for KallistiOS and, for the host tests, for a PC (no _arch_dreamcast).

   Layout on a 640x480 grid of 8x16 cells, as the approved mock-ups (ql-design ingame.html, design B): black, a blue
   title bar (y 24..64) and hint bar (y 424..456), white text, dim blue-grey labels; the body is y 80..416. */
#include <stdio.h>
#include <string.h>
#include "dcql_menu.h"
#include "dcql_net.h"
#include "dcql_live.h"
#include "font/spleen8x16.h"
#ifdef _arch_dreamcast
#include <arch/irq.h>
#include <dc/video.h>
#endif

/* Used when the program links them; a program with the menu alone links no network, live-data or loader code. */
#pragma weak dcql_net_state
#pragma weak dcql_net_kind
#pragma weak dcql_net_ip
#pragma weak dcql_net_host
#pragma weak dcql_net_port
#pragma weak dcql_net_attempt
#pragma weak dcql_net_learning
#pragma weak dcql_net_counters
#pragma weak dcql_net_config
#pragma weak dcql_net_start
#pragma weak dcql_net_stop
#pragma weak dcql_live_stats
#pragma weak dcql_live_picture
int cl_active(void);
void cl_exit(int code) __attribute__((noreturn));
void dcql_bios_reboot(void) __attribute__((noreturn));
#pragma weak cl_active
#pragma weak cl_exit
#pragma weak dcql_bios_reboot

const uint32_t dcql_menu_colours[5] = {0x000000, 0x1848a8, 0xf0f0f0, 0x7890a8, 0xf0f0f0};
enum { BODY_Y = 80, BODY_END = 416, SEL_X = 32, SEL_W = 576, VAL_X = 592, QR_MAX = 37, SNAP_MAX = 10 };
enum { P_LINK = 0, P_LOG = 1, P_SNAP = 2 };   /* page kinds; game pages are 3 + their index */

typedef struct { const char *title; const dcql_menu_row_t *rows; int n; } page_t;

static struct {
    uint32_t gate;          /* closed: the combo (one test in dcql_menu_input); open: 0 */
    uint32_t combo, prev;
    int open, page, npages, sel[3 + DCQL_MENU_PAGES_MAX], scroll;
    int repeat;             /* pictures a direction has been held (auto-repeat) */
    uint32_t changes;
    const char *title;
    dcql_menu_config_t cfg;
    page_t pages[3 + DCQL_MENU_PAGES_MAX];
    int order[3 + DCQL_MENU_PAGES_MAX];   /* page kinds in the order L / R walks them */
    dcql_menu_row_t link[16];
    int nlink, transport;
    char connect_label[12];
    /* the Log page's ring */
    char log[DCQL_MENU_LOG_LINES][DCQL_MENU_LOG_COLS + 1];
    uint32_t log_seq[DCQL_MENU_LOG_LINES];
    volatile uint32_t log_n;
    /* the Snapshot page: values as they were when the code was made */
    const char *snap_label[SNAP_MAX];
    char snap_val[SNAP_MAX][24];
    int nsnap, qr_size;
    char snap_text[112];
    uint8_t qr[QR_MAX * QR_MAX];
} g;

/* ---------------------------------------------------------------------------------------------- text and boxes */
static void text(const dcql_menu_target_t *t, int x, int y, const char *s, int c, int maxc, int upper) {
    const uint32_t rgb = dcql_menu_colours[c];
    for (int i = 0; s[i] && i < maxc; i++, x += 8) {
        int ch = (unsigned char)s[i];
        if (upper && ch >= 'a' && ch <= 'z') ch -= 32;
        if (ch == ' ') continue;
        if (ch < DCQL_FONT_FIRST || ch > DCQL_FONT_LAST) ch = '?';
        t->glyph(x, y, dcql_font[ch - DCQL_FONT_FIRST], rgb, t->user);
    }
}
static void text_r(const dcql_menu_target_t *t, int xr, int y, const char *s, int c, int maxc, int upper) {
    int n = (int)strlen(s);
    if (n > maxc) n = maxc;
    text(t, xr - n * 8, y, s, c, n, upper);
}
static void box(const dcql_menu_target_t *t, int x, int y, int w, int h, int c) {
    t->box(x, y, w, h, dcql_menu_colours[c], t->user);
}

/* ---------------------------------------------------------------------------------------------- link page */
static const char *state_word(void) {
    if (!dcql_net_state) return "No link";
    switch (dcql_net_state()) {
    case DCQL_LINK_DIAL: return "Dialling";
    case DCQL_LINK_PPP: return "Connecting";
    case DCQL_LINK_UP: return dcql_net_learning && dcql_net_learning() ? "Waiting for PC" : "Linked";
    case DCQL_LINK_FAILED: return "Failed";
    case DCQL_LINK_DROPPED: return "Link lost";
    case DCQL_LINK_STOPPING: return "Hanging up";
    default: return "Unlinked";
    }
}
static void ip_text(char *o, int n, unsigned a, unsigned port) {   /* a: network order */
    const uint8_t *b = (const uint8_t *)&a;
    if (!a || a == 0xffffffffu) snprintf(o, n, "not known yet");
    else if (port) snprintf(o, n, "%u.%u.%u.%u:%u", b[0], b[1], b[2], b[3], port);
    else snprintf(o, n, "%u.%u.%u.%u", b[0], b[1], b[2], b[3]);
}
static void i_state(char *o, int n, void *u) {
    (void)u;
    if (dcql_net_state && dcql_net_state() == DCQL_LINK_DIAL && dcql_net_attempt)
        snprintf(o, n, "Dialling, attempt %d of 3", dcql_net_attempt());
    else snprintf(o, n, "%s", state_word());
}
static void i_transport(char *o, int n, void *u) {
    (void)u;
    const int k = dcql_net_kind ? dcql_net_kind() : 0;
    snprintf(o, n, "%s", k == DCQL_NET_MODEM ? "Modem" : k == DCQL_NET_BBA ? "Broadband adapter" : "Not started");
}
static void i_pc(char *o, int n, void *u) { (void)u; ip_text(o, n, dcql_net_host ? dcql_net_host() : 0, dcql_net_port ? dcql_net_port() : 0); }
static void i_console(char *o, int n, void *u) { (void)u; ip_text(o, n, dcql_net_ip ? dcql_net_ip() : 0, 0); }
static void i_data(char *o, int n, void *u) {
    (void)u;
    unsigned tx = 0, rx = 0, txd = 0, rxd = 0;
    if (dcql_net_counters) dcql_net_counters(&tx, &rx, &txd, &rxd);
    snprintf(o, n, "%u sent, %u received, %u dropped", tx, rx, txd + rxd);
}
static void i_records(char *o, int n, void *u) {
    (void)u;
    dcql_live_stats_t s = {0};
    if (dcql_live_stats) dcql_live_stats(&s);
    snprintf(o, n, "%u sent, %u dropped", (unsigned)s.recs_sent, (unsigned)s.recs_dropped);
}
static void i_log(char *o, int n, void *u) {
    (void)u;
    dcql_live_stats_t s = {0};
    if (dcql_live_stats) dcql_live_stats(&s);
    snprintf(o, n, "%u lines, %u dropped", (unsigned)s.logs_sent, (unsigned)s.logs_dropped);
}
static int a_connect(const dcql_menu_row_t *r, int v, void *u) {
    (void)r; (void)v; (void)u;
    const int st = dcql_net_state ? dcql_net_state() : 0;
    if (st == DCQL_LINK_OFF || st == DCQL_LINK_FAILED || st == DCQL_LINK_DROPPED) {
        if (g.cfg.net_config && dcql_net_config) {
            ((dcql_net_config_t *)g.cfg.net_config)->net = g.transport;
            dcql_net_config((const dcql_net_config_t *)g.cfg.net_config);
        }
        if (dcql_net_start) dcql_net_start();
        dcql_menu_log("menu: connect");
    } else {
        if (dcql_net_stop) dcql_net_stop();
        dcql_menu_log("menu: hang up");
    }
    return 0;
}
static int a_loader(const dcql_menu_row_t *r, int v, void *u) {
    (void)r; (void)v; (void)u;
    if (cl_active && cl_exit && cl_active()) cl_exit(0);
    if (dcql_bios_reboot) dcql_bios_reboot();
    return 0;
}
static const char *const TRANSPORTS[3] = {"Automatic", "Modem", "Broadband adapter"};

static void build_link_page(void) {
    dcql_menu_row_t *L = g.link;
    int n = 0;
    L[n++] = (dcql_menu_row_t)DCQL_MENU_INFO("State", i_state, 0);
    L[n++] = (dcql_menu_row_t)DCQL_MENU_INFO("Transport", i_transport, 0);
    L[n++] = (dcql_menu_row_t)DCQL_MENU_INFO("PC link", i_pc, 0);
    L[n++] = (dcql_menu_row_t)DCQL_MENU_INFO("Console", i_console, 0);
    L[n++] = (dcql_menu_row_t)DCQL_MENU_INFO("Datagrams", i_data, 0);
    if (dcql_live_stats) {
        L[n++] = (dcql_menu_row_t)DCQL_MENU_INFO("Records", i_records, 0);
        L[n++] = (dcql_menu_row_t)DCQL_MENU_INFO("Log", i_log, 0);
    }
    for (int i = 0; i < g.cfg.nlink_rows && i < 4; i++) L[n++] = g.cfg.link_rows[i];
    L[n++] = (dcql_menu_row_t)DCQL_MENU_GAP;
    if (dcql_net_start) L[n++] = (dcql_menu_row_t)DCQL_MENU_ACTION(g.connect_label, 0, a_connect, 0);
    if (g.cfg.net_config && dcql_net_start) {
        g.transport = ((dcql_net_config_t *)g.cfg.net_config)->net;
        L[n++] = (dcql_menu_row_t)DCQL_MENU_CHOICE("Transport on connect", &g.transport, TRANSPORTS, 3, 0, 0);
    }
    if (dcql_bios_reboot || cl_exit) L[n++] = (dcql_menu_row_t)DCQL_MENU_ACTION("Back to the loader", 0, a_loader, 0);
    g.nlink = n;
}

/* ---------------------------------------------------------------------------------------------- QR code */
static uint8_t gf_mul(uint8_t a, uint8_t b) {
    uint8_t r = 0;
    while (b) {
        if (b & 1) r ^= a;
        a = (uint8_t)((a << 1) ^ ((a & 0x80) ? 0x1d : 0));
        b >>= 1;
    }
    return r;
}
/* modules: bit 0 dark, bit 1 function pattern (not data, not masked) */
static void qr_set(uint8_t *m, int n, int r, int c, int dark) { m[r * n + c] = (uint8_t)(2 | (dark ? 1 : 0)); }
static void qr_finder(uint8_t *m, int n, int r0, int c0) {
    for (int r = -1; r <= 7; r++)
        for (int c = -1; c <= 7; c++) {
            const int rr = r0 + r, cc = c0 + c;
            if (rr < 0 || cc < 0 || rr >= n || cc >= n) continue;
            const int in = r >= 0 && r <= 6 && c >= 0 && c <= 6;
            const int ring = r == 0 || r == 6 || c == 0 || c == 6, core = r >= 2 && r <= 4 && c >= 2 && c <= 4;
            qr_set(m, n, rr, cc, in && (ring || core));
        }
}
static int qr_mask_bit(int k, int r, int c) {
    switch (k) {
    case 0: return (r + c) % 2 == 0;
    case 1: return r % 2 == 0;
    case 2: return c % 3 == 0;
    case 3: return (r + c) % 3 == 0;
    case 4: return (r / 2 + c / 3) % 2 == 0;
    case 5: return (r * c) % 2 + (r * c) % 3 == 0;
    case 6: return ((r * c) % 2 + (r * c) % 3) % 2 == 0;
    default: return ((r + c) % 2 + (r * c) % 3) % 2 == 0;
    }
}
static void qr_format(uint8_t *m, int n, int mask) {
    int d = (1 << 3) | mask, b = d << 10;   /* ECC level L = 01 */
    for (int i = 14; i >= 10; i--) if (b & (1 << i)) b ^= 0x537 << (i - 10);
    const int f = ((d << 10) | b) ^ 0x5412;
    for (int i = 0; i < 15; i++) {
        const int bit = (f >> i) & 1;
        /* around the top-left finder */
        if (i < 6) qr_set(m, n, i, 8, bit);
        else if (i < 8) qr_set(m, n, i + 1, 8, bit);
        else if (i == 8) qr_set(m, n, 8, 7, bit);
        else qr_set(m, n, 8, 14 - i, bit);
        /* the copy beside the other two */
        if (i < 8) qr_set(m, n, 8, n - 1 - i, bit);
        else qr_set(m, n, n - 15 + i, 8, bit);
    }
    qr_set(m, n, n - 8, 8, 1);   /* the dark module */
}
static int qr_penalty(const uint8_t *m, int n) {
    int p = 0, dark = 0;
    for (int pass = 0; pass < 2; pass++)
        for (int a = 0; a < n; a++) {
            int run = 1, prev = -1;
            unsigned win = 0;
            for (int b = 0; b < n; b++) {
                const int v = (pass ? m[b * n + a] : m[a * n + b]) & 1;
                if (!pass) dark += v;
                if (v == prev) { if (++run == 5) p += 3; else if (run > 5) p++; }
                else { run = 1; prev = v; }
                win = ((win << 1) | (unsigned)v) & 0x7ff;
                if (b >= 10 && (win == 0x05d || win == 0x5d0)) p += 40;
            }
        }
    for (int r = 0; r + 1 < n; r++)
        for (int c = 0; c + 1 < n; c++) {
            const int v = m[r * n + c] & 1;
            if (v == (m[r * n + c + 1] & 1) && v == (m[(r + 1) * n + c] & 1) && v == (m[(r + 1) * n + c + 1] & 1)) p += 3;
        }
    int k = (dark * 20) / (n * n) - 10;
    return p + (k < 0 ? -k : k) * 10;
}
int dcql_menu_qr(const char *s, uint8_t *m, int max_size) {
    static const uint8_t DATA[5] = {19, 34, 55, 80, 108}, ECC[5] = {7, 10, 15, 20, 26};
    const int len = (int)strlen(s);
    int v = 0;
    while (v < 5 && len + 2 > DATA[v]) v++;   /* 4 + 8 bits of header, then the bytes */
    if (v == 5) return 0;
    const int n = 17 + 4 * (v + 1), nd = DATA[v], ne = ECC[v];
    if (n > max_size) return 0;
    uint8_t cw[134];
    memset(cw, 0, sizeof cw);
    int bit = 0;
#define PUT(val, nb) for (int _i = (nb) - 1; _i >= 0; _i--, bit++) if (((val) >> _i) & 1) cw[bit >> 3] |= (uint8_t)(0x80 >> (bit & 7))
    PUT(4, 4);
    PUT(len, 8);
    for (int i = 0; i < len; i++) PUT((uint8_t)s[i], 8);
    bit += 4;   /* terminator (zeros), capped by the data size below */
#undef PUT
    for (int i = (bit + 7) >> 3, k = 0; i < nd; i++, k++) cw[i] = k & 1 ? 0x11 : 0xec;
    /* Reed-Solomon: one block for versions 1 to 5 at level L. Generator: prod (x - a^i), i < ne. */
    uint8_t gen[27] = {1}, a = 1;
    for (int i = 0; i < ne; i++) {
        for (int j = i + 1; j > 0; j--) gen[j] = (uint8_t)(gen[j - 1] ^ gf_mul(gen[j], a));
        gen[0] = gf_mul(gen[0], a);
        a = gf_mul(a, 2);
    }
    uint8_t *ec = cw + nd;
    for (int i = 0; i < nd; i++) {
        const uint8_t f = cw[i] ^ ec[0];
        memmove(ec, ec + 1, (size_t)(ne - 1));
        ec[ne - 1] = 0;
        for (int j = 0; j < ne; j++) ec[j] ^= gf_mul(gen[ne - 1 - j], f);
    }
    /* function patterns */
    memset(m, 0, (size_t)(n * n));
    qr_finder(m, n, 0, 0);
    qr_finder(m, n, 0, n - 7);
    qr_finder(m, n, n - 7, 0);
    for (int i = 8; i < n - 8; i++) { qr_set(m, n, 6, i, !(i & 1)); qr_set(m, n, i, 6, !(i & 1)); }
    if (v > 0)
        for (int r = -2; r <= 2; r++)
            for (int c = -2; c <= 2; c++) {
                const int d = r * r > c * c ? (r < 0 ? -r : r) : (c < 0 ? -c : c);
                qr_set(m, n, n - 7 + r, n - 7 + c, d != 1);
            }
    qr_format(m, n, 0);   /* reserves the format areas; rewritten with the chosen mask */
    /* data, in the zig-zag */
    int k = 0;
    const int total = (nd + ne) * 8;
    for (int right = n - 1; right >= 1; right -= 2) {
        if (right == 6) right = 5;
        for (int vert = 0; vert < n; vert++)
            for (int j = 0; j < 2; j++) {
                const int c = right - j, up = ((right + 1) & 2) == 0;
                const int r = up ? n - 1 - vert : vert;
                if (m[r * n + c] & 2) continue;
                const int d = k < total ? (cw[k >> 3] >> (7 - (k & 7))) & 1 : 0;
                m[r * n + c] = (uint8_t)d;
                k++;
            }
    }
    /* the mask with the lowest penalty */
    int best = 0, best_p = 1 << 30;
    for (int mask = 0; mask < 8; mask++) {
        for (int r = 0; r < n; r++)
            for (int c = 0; c < n; c++)
                if (!(m[r * n + c] & 2) && qr_mask_bit(mask, r, c)) m[r * n + c] ^= 1;
        qr_format(m, n, mask);
        const int p = qr_penalty(m, n);
        if (p < best_p) { best_p = p; best = mask; }
        for (int r = 0; r < n; r++)
            for (int c = 0; c < n; c++)
                if (!(m[r * n + c] & 2) && qr_mask_bit(mask, r, c)) m[r * n + c] ^= 1;
    }
    for (int r = 0; r < n; r++)
        for (int c = 0; c < n; c++)
            if (!(m[r * n + c] & 2) && qr_mask_bit(best, r, c)) m[r * n + c] ^= 1;
    qr_format(m, n, best);
    for (int i = 0; i < n * n; i++) m[i] &= 1;
    return n;
}

static void take_snapshot(void) {
    int n = 0, len;
    char st[24];
    if (dcql_live_picture) {
        g.snap_label[n] = "Picture";
        snprintf(g.snap_val[n++], 24, "%u", (unsigned)dcql_live_picture());
    }
    g.snap_label[n] = "Link";
    i_state(st, sizeof st, 0);
    snprintf(g.snap_val[n++], 24, "%s", st);
    for (int i = 0; i < g.cfg.nsnapshot && n < SNAP_MAX; i++) {
        const dcql_menu_row_t *r = &g.cfg.snapshot[i];
        g.snap_label[n] = r->label;
        g.snap_val[n][0] = 0;
        if (r->info) r->info(g.snap_val[n], 24, r->user);
        n++;
    }
    g.nsnap = n;
    len = snprintf(g.snap_text, sizeof g.snap_text, "QL1");
    for (int i = 0; i < n; i++) {
        char item[48];
        int k = snprintf(item, sizeof item, ";%s=%s", g.snap_label[i], g.snap_val[i]);
        for (int j = 1; j < k && item[j] != '='; j++) item[j] = item[j] == ' ' ? '_' : (char)(item[j] | (item[j] >= 'A' && item[j] <= 'Z' ? 32 : 0));
        if (len + k > 106) break;   /* what does not fit in a version 5 code is left out (the page still shows it) */
        memcpy(g.snap_text + len, item, (size_t)k + 1);
        len += k;
    }
    g.qr_size = dcql_menu_qr(g.snap_text, g.qr, QR_MAX);
    g.changes++;
}

/* ---------------------------------------------------------------------------------------------- pages and input */
void dcql_menu_init(const dcql_menu_config_t *c) {
    memset(&g, 0, sizeof g);
    if (c) g.cfg = *c;
    g.combo = g.cfg.combo ? g.cfg.combo : (DCQL_MENU_LT | DCQL_MENU_RT | DCQL_MENU_START);
    g.gate = g.combo;
    g.title = g.cfg.title ? g.cfg.title : "QA LINK";
    strcpy(g.connect_label, "Connect");
    build_link_page();
    g.pages[P_LINK] = (page_t){"Link", g.link, g.nlink};
    g.pages[P_LOG] = (page_t){"Log", 0, 0};
    g.pages[P_SNAP] = (page_t){"Snapshot", 0, 0};
    g.order[0] = P_LINK;
    g.order[1] = P_LOG;
    g.order[2] = P_SNAP;
    g.npages = 3;
    for (int i = 0; i < 3 + DCQL_MENU_PAGES_MAX; i++) g.sel[i] = -1;
}
int dcql_menu_add_page(const char *title, const dcql_menu_row_t *rows, int n) {
    if (g.npages >= 3 + DCQL_MENU_PAGES_MAX) return -1;
    const int kind = g.npages;
    g.pages[kind] = (page_t){title, rows, n};
    /* the game's pages come after Link, before Log and Snapshot */
    const int at = g.npages - 2;
    memmove(&g.order[at + 1], &g.order[at], (size_t)(g.npages - at) * sizeof g.order[0]);
    g.order[at] = kind;
    g.npages++;
    return kind;
}
uint32_t dcql_menu_pad(uint32_t b, int lt, int rt) {
    return (b & ~(DCQL_MENU_LT | DCQL_MENU_RT)) | (lt >= 128 ? DCQL_MENU_LT : 0) | (rt >= 128 ? DCQL_MENU_RT : 0);
}
static int selectable(const dcql_menu_row_t *r) { return r->kind >= DCQL_ROW_ACTION && r->kind <= DCQL_ROW_NUMBER; }
static void fix_sel(void) {
    const int k = g.order[g.page];
    const page_t *p = &g.pages[k];
    if (g.sel[k] >= 0 && g.sel[k] < p->n && selectable(&p->rows[g.sel[k]])) return;
    g.sel[k] = -1;
    for (int i = 0; i < p->n; i++) if (selectable(&p->rows[i])) { g.sel[k] = i; break; }
}
static void enter_page(int idx) {
    g.page = (idx + g.npages) % g.npages;
    g.scroll = 0;
    fix_sel();
    if (g.order[g.page] == P_SNAP) take_snapshot();
    g.changes++;
}
void dcql_menu_open(void) {
    if (g.open) return;
    g.open = 1;
    g.gate = 0;
    if (!g.npages) dcql_menu_init(0);
    enter_page(g.page);
}
void dcql_menu_close(void) {
    g.open = 0;
    g.gate = g.combo;
    g.changes++;
}
int dcql_menu_is_open(void) { return g.open; }
uint32_t dcql_menu_changes(void) { return g.changes; }

static int change(const dcql_menu_row_t *r, int dir) {
    int v = *r->value;
    if (r->kind == DCQL_ROW_TOGGLE) v = !v;
    else if (r->kind == DCQL_ROW_CHOICE) v = v + dir > r->max ? 0 : v + dir < 0 ? r->max : v + dir;
    else {
        v += dir * (r->step ? r->step : 1);
        if (v > r->max) v = r->max;
        if (v < r->min) v = r->min;
    }
    if (v == *r->value) return 0;
    *r->value = v;
    g.changes++;
    return r->fn ? r->fn(r, v, r->user) : 0;
}
int dcql_menu_input(uint32_t b) {
    if ((b & g.gate) != g.gate) {   /* closed and the combo not held: the only work a closed picture does */
        g.prev = b;
        return 0;
    }
    uint32_t down = b & ~g.prev;
    if (!g.open) {
        g.prev = b;
        if ((down & g.combo) == 0) return 0;   /* still held since the menu closed */
        dcql_menu_open();
        return 1;
    }
    g.prev = b;
    if (down & DCQL_MENU_START || down & DCQL_MENU_B) { dcql_menu_close(); return 0; }
    if (down & DCQL_MENU_LT) { enter_page(g.page - 1); return 1; }
    if (down & DCQL_MENU_RT) { enter_page(g.page + 1); return 1; }
    const int k = g.order[g.page];
    const page_t *p = &g.pages[k];
    /* up / down with auto-repeat (20 pictures, then every 4) */
    uint32_t dir = b & (DCQL_MENU_UP | DCQL_MENU_DOWN | DCQL_MENU_LEFT | DCQL_MENU_RIGHT);
    if (!dir) g.repeat = 0;
    else if (down & dir) g.repeat = 1;
    else if (++g.repeat >= 20 && !(g.repeat & 3)) down |= dir;   /* held */
    const uint32_t d2 = down;
    if (d2 & (DCQL_MENU_UP | DCQL_MENU_DOWN) && g.sel[k] >= 0) {
        const int step = d2 & DCQL_MENU_UP ? -1 : 1;
        for (int i = g.sel[k] + step; i >= 0 && i < p->n; i += step)
            if (selectable(&p->rows[i])) { g.sel[k] = i; g.changes++; break; }
    }
    if (k == P_SNAP && (d2 & DCQL_MENU_A)) { take_snapshot(); return 1; }
    if (g.sel[k] < 0) return 1;
    const dcql_menu_row_t *r = &p->rows[g.sel[k]];
    int close = 0;
    if (d2 & DCQL_MENU_A) {
        if (r->kind == DCQL_ROW_ACTION) { g.changes++; close = r->fn ? r->fn(r, 0, r->user) : 0; }
        else close = change(r, 1);
    } else if (d2 & (DCQL_MENU_LEFT | DCQL_MENU_RIGHT) && r->kind != DCQL_ROW_ACTION) {
        close = change(r, d2 & DCQL_MENU_LEFT ? -1 : 1);
    }
    if (close) { dcql_menu_close(); return 0; }
    return 1;
}

void dcql_menu_log(const char *line) {
#ifdef _arch_dreamcast
    const int irq = irq_disable();
#endif
    const uint32_t n = g.log_n;
    char *d = g.log[n % DCQL_MENU_LOG_LINES];
    int i = 0;
    for (; line[i] && line[i] != '\n' && i < DCQL_MENU_LOG_COLS; i++) d[i] = line[i];
    d[i] = 0;
    g.log_seq[n % DCQL_MENU_LOG_LINES] = n + 1;
    g.log_n = n + 1;
    g.changes++;
#ifdef _arch_dreamcast
    irq_restore(irq);
#endif
}

/* ---------------------------------------------------------------------------------------------- drawing */
static int row_h(const dcql_menu_row_t *r) { return r->kind == DCQL_ROW_INFO ? 24 : 16; }
static void draw_rows(const dcql_menu_target_t *t, const page_t *p, int sel, int y_end) {
    /* keep the selected row in view */
    int y = 0, sy = -1, sh = 16;
    for (int i = 0; i < p->n; i++) { if (i == sel) { sy = y; sh = row_h(&p->rows[i]); } y += row_h(&p->rows[i]); }
    const int h = y_end - BODY_Y;
    if (sy >= 0) {
        if (sy < g.scroll) g.scroll = sy;
        if (sy + sh > g.scroll + h) g.scroll = sy + sh - h;
    }
    y = BODY_Y - g.scroll;
    for (int i = 0; i < p->n; y += row_h(&p->rows[i]), i++) {
        const dcql_menu_row_t *r = &p->rows[i];
        if (y < BODY_Y || y + 16 > y_end) continue;
        char v[48];
        v[0] = 0;
        switch (r->kind) {
        case DCQL_ROW_HEAD: text(t, 40, y, r->label, DCQL_MENU_DIM, 70, 1); continue;
        case DCQL_ROW_INFO:
            text(t, 40, y, r->label, DCQL_MENU_DIM, 14, 1);
            if (r->info) r->info(v, sizeof v, r->user);
            text(t, 160, y, v, DCQL_MENU_TEXT, 54, 0);
            continue;
        case DCQL_ROW_TOGGLE: snprintf(v, sizeof v, "%s", *r->value ? "On" : "Off"); break;
        case DCQL_ROW_CHOICE: snprintf(v, sizeof v, "%s", r->names[*r->value]); break;
        case DCQL_ROW_NUMBER: snprintf(v, sizeof v, "%d", *r->value); break;
        case DCQL_ROW_ACTION: if (r->note) snprintf(v, sizeof v, "%s", r->note); break;
        default: continue;
        }
        if (i == sel) box(t, SEL_X, y - 2, SEL_W, 20, DCQL_MENU_BAR);
        const int vn = (int)strlen(v);
        text(t, 48, y, r->label, DCQL_MENU_TEXT, 67 - (vn > 20 ? 20 : vn), 0);
        text_r(t, VAL_X, y, v, r->kind == DCQL_ROW_ACTION && i != sel ? DCQL_MENU_DIM : DCQL_MENU_TEXT, 20, 0);
    }
}
static void draw_log(const dcql_menu_target_t *t, int y, int lines) {
    const uint32_t n = g.log_n;
    const uint32_t first = n > (uint32_t)lines ? n - (uint32_t)lines : 0;
    for (uint32_t i = first; i < n; i++, y += 16) {
        char seq[12];
        snprintf(seq, sizeof seq, "%u", (unsigned)g.log_seq[i % DCQL_MENU_LOG_LINES]);
        text_r(t, 72, y, seq, DCQL_MENU_DIM, 8, 0);
        text(t, 88, y, g.log[i % DCQL_MENU_LOG_LINES], DCQL_MENU_TEXT, DCQL_MENU_LOG_COLS - 8, 0);
    }
}
static void draw_snapshot(const dcql_menu_target_t *t) {
    const int s = 4, n = g.qr_size, q = 4 * s;
    if (n) {
        box(t, 48, BODY_Y, n * s + 2 * q, n * s + 2 * q, DCQL_MENU_PAPER);
        for (int r = 0; r < n; r++)
            for (int c = 0; c < n;) {
                if (!g.qr[r * n + c]) { c++; continue; }
                int e = c;
                while (e < n && g.qr[r * n + e]) e++;   /* a run of dark modules is one box */
                box(t, 48 + q + c * s, BODY_Y + q + r * s, (e - c) * s, s, DCQL_MENU_BG);
                c = e;
            }
    }
    for (int i = 0; i < g.nsnap; i++) {
        text(t, 320, 96 + i * 32, g.snap_label[i], DCQL_MENU_DIM, 11, 1);
        text(t, 416, 96 + i * 32, g.snap_val[i], DCQL_MENU_TEXT, 22, 0);
    }
    text(t, 320, 96 + g.nsnap * 32 + 32, n ? "Scan to open in Studio" : "Too long for a code", DCQL_MENU_DIM, 36, 0);
}
void dcql_menu_draw(const dcql_menu_target_t *t) {
    if (!g.open) return;
    const int k = g.order[g.page];
    const page_t *p = &g.pages[k];
    const char *st = state_word();
    box(t, 0, 0, 640, 480, DCQL_MENU_BG);
    box(t, 0, 24, 640, 40, DCQL_MENU_BAR);
    const int tn = (int)strlen(g.title);
    text(t, 40, 36, g.title, DCQL_MENU_TEXT, 20, 1);
    text(t, 40 + tn * 8 + 8, 36, "/", DCQL_MENU_DIM, 1, 0);
    text(t, 40 + tn * 8 + 24, 36, p->title, DCQL_MENU_TEXT, 30, 1);
    text_r(t, 600, 36, st, DCQL_MENU_TEXT, 20, 1);
    if (k == P_LINK) {
        strcpy(g.connect_label, dcql_net_state && dcql_net_state() != DCQL_LINK_OFF && dcql_net_state() != DCQL_LINK_FAILED &&
                                        dcql_net_state() != DCQL_LINK_DROPPED ? "Hang up" : "Connect");
        draw_rows(t, p, g.sel[k], BODY_END - 80);
        text(t, 40, BODY_END - 64, "Last log", DCQL_MENU_DIM, 20, 1);
        draw_log(t, BODY_END - 48, 3);
    } else if (k == P_LOG) {
        draw_log(t, BODY_Y, DCQL_MENU_LOG_LINES);
        if (!g.log_n) text(t, 40, BODY_Y, "No log lines yet", DCQL_MENU_DIM, 40, 0);
    } else if (k == P_SNAP) {
        draw_snapshot(t);
    } else {
        draw_rows(t, p, g.sel[k], BODY_END);
    }
    box(t, 0, 424, 640, 32, DCQL_MENU_BAR);
    text(t, 40, 432, k == P_SNAP ? "(A) new snapshot   (B) back   (L)(R) page   (START) close"
                                 : "(A) select   (B) back   (L)(R) page   (START) close", DCQL_MENU_TEXT, 70, 0);
}
const char *dcql_menu_snapshot_text(void) { return g.snap_text; }

/* ---------------------------------------------------------------------------------------------- way (b) */
static void fb_box(int x, int y, int w, int h, uint32_t rgb, void *u) {
    dcql_menu_fb_t *f = (dcql_menu_fb_t *)u;
    int c = 0;
    while (c < 4 && dcql_menu_colours[c] != rgb) c++;
    if (x < 0) { w += x; x = 0; }
    if (y < 0) { h += y; y = 0; }
    if (x + w > f->w) w = f->w - x;
    if (y + h > f->h) h = f->h - y;
    if (w <= 0 || h <= 0) return;
    const uint16_t px = f->pal[c];
    const uint32_t two = (uint32_t)px * 0x10001u;
    for (int r = 0; r < h; r++) {
        uint16_t *d = f->pix + (y + r) * f->stride + x;
        int n = w;
        if (((uintptr_t)d & 2) && n) { *d++ = px; n--; }
        uint32_t *d4 = (uint32_t *)d;
        for (; n >= 2; n -= 2) *d4++ = two;
        if (n) *(uint16_t *)d4 = px;
    }
}
static void fb_glyph(int x, int y, const uint8_t rows[16], uint32_t rgb, void *u) {
    dcql_menu_fb_t *f = (dcql_menu_fb_t *)u;
    if (x < 0 || y < 0 || x + 8 > f->w || y + 16 > f->h) return;
    int c = 0;
    while (c < 4 && dcql_menu_colours[c] != rgb) c++;
    const uint16_t px = f->pal[c];
    uint16_t *d = f->pix + y * f->stride + x;
    for (int r = 0; r < 16; r++, d += f->stride) {
        const unsigned b = rows[r];
        if (!b) continue;
        for (int i = 0; i < 8; i++) if (b & (0x80u >> i)) d[i] = px;
    }
}
void dcql_menu_fb_target(dcql_menu_target_t *t, dcql_menu_fb_t *f, uint16_t *pix, int stride, int w, int h, int rgb555) {
    f->pix = pix; f->stride = stride; f->w = w; f->h = h; f->rgb555 = rgb555;
    for (int i = 0; i < 5; i++) {
        const uint32_t c = dcql_menu_colours[i];
        const unsigned r = (c >> 16) & 0xff, gg = (c >> 8) & 0xff, b = c & 0xff;
        f->pal[i] = rgb555 ? (uint16_t)(0x8000 | ((r >> 3) << 10) | ((gg >> 3) << 5) | (b >> 3))
                           : (uint16_t)(((r >> 3) << 11) | ((gg >> 2) << 5) | (b >> 3));
    }
    t->box = fb_box; t->glyph = fb_glyph; t->user = f;
}

#ifdef _arch_dreamcast
int dcql_menu_fb_displayed(dcql_menu_fb_t *f, dcql_menu_target_t *t) {
    volatile uint32_t *R = (volatile uint32_t *)0xa05f8000;
    const uint32_t ctrl = R[0x44 / 4], sof1 = R[0x50 / 4], sof2 = R[0x54 / 4], size = R[0x5c / 4], spg = R[0xd0 / 4];
    const int depth = (ctrl >> 2) & 3;
    if (depth > 1) return -1;
    const int words = (int)(size & 0x3ff) + 1, modulo = (int)((size >> 20) & 0x3ff);
    const int w = words * 2, inter = (spg >> 4) & 1;
    const int stride = inter ? (int)(sof2 - sof1) / 2 : (words + modulo - 1) * 2;
    const int h = ((int)((size >> 10) & 0x3ff) + 1) * (inter ? 2 : 1);
    if (w != 640 || h < 480 || stride < 640) return -1;
    dcql_menu_fb_target(t, f, (uint16_t *)(0xa5000000u + (sof1 & 0x7fffff)), stride, 640, 480, depth == 0);
    return 0;
}
int dcql_menu_stopped(uint32_t (*read_pad)(void *), void *user) {
    dcql_menu_fb_t f;
    dcql_menu_target_t t;
    if (dcql_menu_fb_displayed(&f, &t) < 0) return -1;
    dcql_menu_open();
    uint32_t drawn = g.changes - 1;
    uint16_t *where = 0;
    int tick = 0;
    while (g.open) {
        if (dcql_menu_fb_displayed(&f, &t) < 0) return -1;
        if (drawn != g.changes || where != f.pix || ++tick >= 30) {   /* only when something changed; live values 2 a second */
            drawn = g.changes; where = f.pix; tick = 0;
            dcql_menu_draw(&t);
        }
        vid_waitvbl();
        dcql_menu_input(read_pad ? read_pad(user) : 0);
    }
    return 0;
}
#else
int dcql_menu_fb_displayed(dcql_menu_fb_t *f, dcql_menu_target_t *t) { (void)f; (void)t; return -1; }
int dcql_menu_stopped(uint32_t (*read_pad)(void *), void *user) { (void)read_pad; (void)user; return -1; }
#endif
