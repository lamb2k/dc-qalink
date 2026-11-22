/* dcql.c: dc-qalink's text rows (dcql.h) on the link layer (dcql_net.h). MIT licence.
 *
 * 0.2: the transport moved to dcql_net.c (modem as in 0.1; the broadband adapter when dcql.mk builds it with
 * DCQL_NET=bba or both). The API, the rows and their limits are those of 0.1. New built-in command: RESET, which goes
 * back to the control loop's loader through its return stub when the program was started by the loader (dcql_return.c);
 * otherwise it answers ERR.
 */
#undef DCQL_ENABLE
#define DCQL_ENABLE 1 /* this file is the implementation; dcql.mk only compiles it when DCQL_ENABLE=1 */
#include "dcql.h"
#include "dcql_net.h"

#include <kos.h>
#include <arch/timer.h>
#include <arpa/inet.h>
#include <string.h>
#include <stdio.h>
#include <stdarg.h>
#include <ctype.h>
#include <stdint.h>
#include <stdbool.h>

enum { kBulkCap = 2400 /* ~0.7 s of line at 33.6 kbps */ };

static int (*g_bulk_ok)(void);
static void (*g_logfn)(const char *);
static dcql_command_fn g_cmd;
static void *g_cmd_user;
static unsigned g_tseq, g_lseq, g_bulk_skips;

static unsigned parse_ip(const char *s) {
    unsigned a[4];
    if (!s || sscanf(s, "%u.%u.%u.%u", &a[0], &a[1], &a[2], &a[3]) != 4) return 0;
    unsigned char b[4] = {(unsigned char)a[0], (unsigned char)a[1], (unsigned char)a[2], (unsigned char)a[3]};
    unsigned v;
    memcpy(&v, b, 4);
    return v;
}

void dcql_config(const dcql_config_t *c) {
    if (!c) return;
    dcql_net_config_t n;
    memset(&n, 0, sizeof(n));
    n.net = c->net;
    n.number = c->number;
    n.user = c->user;
    n.pass = c->pass;
    n.host = (!c->host || !c->host[0] || !strcmp(c->host, "peer")) ? 0xFFFFFFFFu : parse_ip(c->host);
    if (!n.host) n.host = 0xFFFFFFFFu;
    n.port = c->port ? c->port : 52040;
    n.lport = c->lport ? c->lport : 52040;
    n.ip = parse_ip(c->ip);
    n.mask = parse_ip(c->mask);
    n.gw = parse_ip(c->gw);
    n.log = c->log;
    dcql_net_config(&n);
    g_bulk_ok = c->bulk_ok;
    g_logfn = c->log;
}
void dcql_set_command(dcql_command_fn fn, void *user) { g_cmd = fn; g_cmd_user = user; }

int dcql_start(void) {
    const int r = dcql_net_start();
    if (g_logfn && r == 0) {
        char b[96];
        snprintf(b, sizeof(b), "dcql: start number=%s port=%u lport=%u", dcql_net_number(), dcql_net_port(),
                 dcql_net_lport());
        g_logfn(b);
    }
    return r;
}
void dcql_stop(void) { dcql_net_stop(); }
dcql_state_t dcql_state(void) { return (dcql_state_t)dcql_net_state(); }
void dcql_state_text(char *out, size_t n) {
    char t[64];
    dcql_net_text(t, sizeof(t));
    /* 0.1's texts: "DIALLING 1/3", "UP a TO b", "FAILED (e)" */
    if (dcql_net_state() == DCQL_LINK_DIAL && dcql_net_kind() != DCQL_NET_BBA) snprintf(out, n, "DIALLING %d/3", dcql_net_attempt());
    else if (dcql_net_state() == DCQL_LINK_FAILED) snprintf(out, n, "FAILED (%d)", dcql_net_err());
    else if (dcql_net_state() == DCQL_LINK_UP && dcql_net_kind() != DCQL_NET_BBA) {
        const unsigned ip = dcql_net_ip(), h = dcql_net_host();
        const unsigned char *a = (const unsigned char *)&ip, *b = (const unsigned char *)&h;
        snprintf(out, n, "UP %u.%u.%u.%u TO %u.%u.%u.%u", a[0], a[1], a[2], a[3], b[0], b[1], b[2], b[3]);
    } else snprintf(out, n, "%s", t);
}
void dcql_counters(dcql_counters_t *c) {
    unsigned tx, rx, txd, rxd;
    dcql_net_counters(&tx, &rx, &txd, &rxd);
    c->tx_bytes = tx; c->rx_bytes = rx; c->tx_drops = txd; c->bulk_skips = g_bulk_skips; c->rx_drops = rxd;
    c->tx_stalls = dcql_net_stalls(); c->queued = dcql_net_tx_queued(); c->rate = dcql_net_rate();
    c->attempt = dcql_net_attempt(); c->err = dcql_net_err();
}

int dcql_send_raw(const void *p, size_t n) {
    if (dcql_net_state() != DCQL_LINK_UP) return 0;
    if (n > DCQL_DATAGRAM_MAX) return 0;
    return dcql_net_send(p, (unsigned)n);
}
/* Bulk rows (T / L) are admitted only while the backlog is short and bulk_ok() agrees, so a slow line never grows
 * the backlog without bound; answers and marks keep the rest of the ring. */
static bool bulk_admit(unsigned n) {
    if (dcql_net_state() != DCQL_LINK_UP) return false;
    if (dcql_net_tx_queued() + n + 2 * 8 > kBulkCap || (g_bulk_ok && !g_bulk_ok())) { ++g_bulk_skips; return false; }
    return true;
}
/* kind 'T': "T seq ms p/n\n", kind 'L': "L seq p/n\n"; parts end at a newline where one exists. */
static int send_parts(char kind, const char *text) {
    const size_t len = strlen(text);
    size_t cuts[16];
    int parts = 0;
    for (size_t off = 0; parts < 16;) {
        size_t n = len - off;
        if (n > DCQL_PART_MAX) {
            n = DCQL_PART_MAX;
            for (size_t k = DCQL_PART_MAX; k > DCQL_PART_MAX / 2; --k)
                if (text[off + k - 1] == '\n') { n = k; break; }
        }
        off += n;
        cuts[parts++] = off;
        if (off >= len) break;
    }
    if (!bulk_admit((unsigned)(len + parts * 32))) return 0;
    const unsigned seq = kind == 'T' ? ++g_tseq : ++g_lseq;
    const unsigned ms = (unsigned)(timer_ms_gettime64() & 0xFFFFFFFFu);
    char b[DCQL_PART_MAX + 48];
    size_t from = 0;
    for (int p = 0; p < parts; ++p) {
        const int h = kind == 'T' ? snprintf(b, sizeof(b), "T %u %u %d/%d\n", seq, ms, p + 1, parts)
                                  : snprintf(b, sizeof(b), "L %u %d/%d\n", seq, p + 1, parts);
        const size_t n = cuts[p] - from;
        memcpy(b + h, text + from, n);
        from = cuts[p];
        if (!dcql_send_raw(b, h + n)) return 0;
    }
    return 1;
}
int dcql_telemetry(const char *body) { return body ? send_parts('T', body) : 0; }
int dcql_log(const char *text) { return text ? send_parts('L', text) : 0; }
int dcql_answer(const char *id, int ok, const char *fmt, ...) {
    char b[DCQL_DATAGRAM_MAX + 1];
    int k = snprintf(b, sizeof(b), "A %s %s ", id ? id : "-", ok ? "OK" : "ERR");
    va_list ap;
    va_start(ap, fmt);
    const int m = vsnprintf(b + k, sizeof(b) - k, fmt, ap);
    va_end(ap);
    k += m;
    if (k > DCQL_DATAGRAM_MAX) k = DCQL_DATAGRAM_MAX;
    return dcql_send_raw(b, (size_t)k);
}
int dcql_mark(const char *label) {
    char b[160];
    const int k = snprintf(b, sizeof(b), "M %u %s", (unsigned)(timer_ms_gettime64() & 0xFFFFFFFFu), label);
    const int ok = dcql_send_raw(b, (size_t)(k < (int)sizeof(b) ? k : (int)sizeof(b) - 1));
    char l[160];
    snprintf(l, sizeof(l), "dcql-mark: %s\n", label);
    if (g_logfn) { char e[170]; snprintf(e, sizeof(e), "dcql-mark: %s", label); g_logfn(e); }
    /* The L echo goes through the ring directly: marks must not be skipped by the bulk cap. */
    char e[200];
    const int h = snprintf(e, sizeof(e), "L %u 1/1\n%s", ++g_lseq, l);
    dcql_send_raw(e, (size_t)(h < (int)sizeof(e) ? h : (int)sizeof(e) - 1));
    return ok;
}

/* the control loop's way back (dcql_return.c), when it is linked */
int dcql_return_to_loader(int state) __attribute__((weak));

void dcql_poll(void) {
    char b[DCQL_DATAGRAM_MAX + 1];
    for (int guard = 0; guard < 8; ++guard) {
        const int n = dcql_net_recv(b, sizeof(b) - 1);
        if (n == -1) break;
        if (n < 0) continue;
        b[n] = 0;
        /* "<id> <VERB> [args]" */
        char *id = b;
        char *verb = strchr(b, ' ');
        if (!verb) continue;
        *verb++ = 0;
        char *args = strchr(verb, ' ');
        if (args) *args++ = 0; else args = verb + strlen(verb);
        for (char *p = verb; *p; ++p) *p = (char)toupper((unsigned char)*p);
        if (!strcmp(verb, "HELLO")) continue; /* the relay's greeting: it teaches us its address */
        if (!strcmp(verb, "PING")) { dcql_answer(id, 1, "PONG %s", args); continue; }
        if (!strcmp(verb, "MARK")) { dcql_mark(args); dcql_answer(id, 1, "MARK %s", args); continue; }
        if (!strcmp(verb, "STATUS")) {
            dcql_counters_t c;
            dcql_counters(&c);
            dcql_answer(id, 1, "tx=%u rx=%u drops=%u skips=%u rxdrop=%u stall=%u queued=%u rate=%u", c.tx_bytes,
                        c.rx_bytes, c.tx_drops, c.bulk_skips, c.rx_drops, c.tx_stalls, c.queued, c.rate);
            continue;
        }
        if (!strcmp(verb, "RESET")) {
            if (!dcql_return_to_loader) { dcql_answer(id, 0, "RESET no control loop linked"); continue; }
            dcql_answer(id, 1, "RESET back to the loader");
            for (int i = 0; i < 50 && dcql_net_tx_queued(); ++i) thd_sleep(10);
            dcql_return_to_loader(6 /* CL_RUN_RESET */);   /* returns only if no loader is armed */
            dcql_answer(id, 0, "RESET no loader armed");
            continue;
        }
        if (!g_cmd || !g_cmd(id, verb, args, g_cmd_user)) dcql_answer(id, 0, "unknown %s", verb);
    }
}
