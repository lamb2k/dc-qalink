/* dcql_live.c: live data from a running program (dc-qalink 0.2). API: dcql_live.h. Wire: pc/bridge/wire.py. MIT.

   Three single-producer rings (records, trace events, log lines) written by the game's thread and drained by the link
   thread; a small command queue the other way. A full ring drops the newest item and counts it: the PC sees a gap in
   the picture numbers or the trace sequence, never wrong data. Fixed memory only (sizes in the hand-back). */
#include <kos.h>
#include <arch/timer.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include "dcql_loop.h"
#include "dcql_live.h"

#define CH_REC 1u
#define CH_TRACE 2u
#define CH_LOG 4u
#define CMD_PAUSE 1
#define CMD_RESUME 2
#define CMD_STEP 3

#define BARRIER() __asm__ __volatile__("" ::: "memory")
#define REC_N 128u      /* about 2 s at 60 pictures a second */
#define TR_N 64u
#define TR_LABEL 40u
#define LOG_N 16u
#define LOG_LEN 120u
#define CMDQ_N 4u
#define ACK_N 8u

static dcql_frame_t g_rec[REC_N];
static volatile uint32_t g_rec_w, g_rec_r;
typedef struct { uint32_t n; uint16_t d; uint8_t lane, len; char label[TR_LABEL]; } tr_t;
static tr_t g_tr[TR_N];
static volatile uint32_t g_tr_w, g_tr_r, g_tr_seq;
typedef struct { uint32_t n; uint8_t level, len; char text[LOG_LEN]; } lg_t;
static lg_t g_lg[LOG_N];
static volatile uint32_t g_lg_w, g_lg_r;
typedef struct { uint32_t id; uint8_t code, len; char arg[64]; } cmd_t;
static cmd_t g_cmdq[CMDQ_N];
static volatile uint32_t g_cmd_w, g_cmd_r;
typedef struct { uint32_t id, n; uint8_t status, len; char why[60]; } ack_t;
static ack_t g_ack[ACK_N];
static volatile uint32_t g_ack_w, g_ack_r;

static const char *g_def;
static volatile uint32_t g_def_ver = 1;
static dcql_live_cmd_fn g_cb;
static void *g_cb_user;
static volatile uint32_t g_sub, g_every = 1;
static volatile int g_up, g_paused;
static volatile uint32_t g_step, g_n, g_line_seq_base = 0x40000000u;
static dcql_live_stats_t g_st;

void dcql_live_declare(const char *text) { g_def = text; g_def_ver++; }
void dcql_live_on_command(dcql_live_cmd_fn fn, void *user) { g_cb_user = user; g_cb = fn; }
void dcql_live_link(int up) { g_up = up; if (!up) { g_sub = 0; g_paused = 0; g_step = 0; } }
uint32_t dcql_live_picture(void) { return g_n; }
int dcql_live_paused(void) { return g_paused; }
void dcql_live_stats(dcql_live_stats_t *s) { *s = g_st; }

static void queue_ack(uint32_t id, uint8_t status, const char *why) {
    if (g_ack_w - g_ack_r >= ACK_N) return;
    ack_t *a = &g_ack[g_ack_w % ACK_N];
    a->id = id; a->status = status; a->n = g_n;
    a->len = why ? (uint8_t)strnlen(why, sizeof(a->why)) : 0;
    if (a->len) memcpy(a->why, why, a->len);
    BARRIER();
    g_ack_w++;
}

/* the commands the program's callback answers, on the game's thread */
static void run_commands(void) {
    while (g_cmd_r != g_cmd_w) {
        cmd_t *c = &g_cmdq[g_cmd_r % CMDQ_N];
        char why[60] = "";
        char arg[65];
        memcpy(arg, c->arg, c->len);
        arg[c->len] = 0;
        const int rc = g_cb ? g_cb(c->code, arg, c->len, why, g_cb_user) : 1;
        queue_ack(c->id, (uint8_t)(rc == 0 ? 0 : rc == 1 ? 1 : 2), why[0] ? why : NULL);
        BARRIER();
        g_cmd_r++;
        g_st.cmds++;
    }
}

void dcql_live_frame(const dcql_frame_t *f) {
    if (!g_up) { ++g_n; return; }                    /* link off: one test */
    const uint64_t t0 = timer_us_gettime64();
    const uint32_t n = f->n ? f->n : g_n + 1;
    g_n = n;
    g_st.frames++;
    if ((g_sub & CH_REC) && (n % g_every) == 0) {
        if (g_rec_w - g_rec_r < REC_N) {
            dcql_frame_t *d = &g_rec[g_rec_w % REC_N];
            *d = *f;
            d->n = n;
            if (d->nph > DCQL_LIVE_PH_MAX) d->nph = DCQL_LIVE_PH_MAX;
            if (d->nctr > DCQL_LIVE_CTR_MAX) d->nctr = DCQL_LIVE_CTR_MAX;
            BARRIER();
            g_rec_w++;
        } else g_st.recs_dropped++;
    }
    if (g_cmd_r != g_cmd_w) run_commands();
    g_st.frame_us_total += (uint32_t)(timer_us_gettime64() - t0);
    /* pause and step: at the end of a picture, so the screen holds a whole picture */
    while (g_paused && g_up) {
        if (g_step) { g_step--; break; }
        if (g_cmd_r != g_cmd_w) run_commands();
        thd_sleep(5);
    }
}

void dcql_live_trace(unsigned lane, unsigned pictures, const char *label) {
    if (!g_up || !(g_sub & CH_TRACE)) { if (g_up) g_tr_seq++; return; }
    if (g_tr_w - g_tr_r >= TR_N) { g_st.trace_dropped++; g_tr_seq++; return; }
    tr_t *t = &g_tr[g_tr_w % TR_N];
    t->n = g_n; t->d = (uint16_t)(pictures > 0xffff ? 0xffff : pictures); t->lane = (uint8_t)lane;
    t->len = (uint8_t)strnlen(label, TR_LABEL);
    memcpy(t->label, label, t->len);
    BARRIER();
    g_tr_w++;
}

void dcql_live_log(int level, const char *fmt, ...) {
    if (!g_up || !(g_sub & CH_LOG)) return;
    if (g_lg_w - g_lg_r >= LOG_N) { g_st.logs_dropped++; return; }
    lg_t *l = &g_lg[g_lg_w % LOG_N];
    va_list ap;
    va_start(ap, fmt);
    int k = vsnprintf(l->text, LOG_LEN, fmt, ap);
    va_end(ap);
    l->len = (uint8_t)(k < 0 ? 0 : k >= (int)LOG_LEN ? LOG_LEN - 1 : k);
    l->n = g_n; l->level = (uint8_t)level;
    BARRIER();
    g_lg_w++;
}

/* ---------------------------------------------------------------- the link thread's side */
static void send_def(dcql_live_send_fn send) {
    const char *t = g_def ? g_def : "";
    const size_t len = strlen(t), part = 1000;
    const unsigned parts = (unsigned)((len + part - 1) / part ? (len + part - 1) / part : 1);
    for (unsigned i = 0; i < parts; ++i) {
        const size_t off = i * part, n = len - off < part ? len - off : part;
        for (int k = 0; k < 50 && !send(CL_LIVEDEF, g_def_ver, (uint16_t)(i | (parts << 8)), t + off, (uint16_t)n); ++k)
            thd_sleep(2);
    }
}

int dcql_live_datagram(uint8_t type, uint32_t a, uint16_t b, const uint8_t *pl, int len, dcql_live_send_fn send) {
    if (type == CL_SUB) {
        g_every = b ? b : 1;
        g_sub = a;
        send_def(send);
        return 1;
    }
    if (type != CL_CMD) return 0;
    uint8_t ack[4 + 60];
    cl_wr32(ack, g_n);
    if (b == CMD_PAUSE) { g_step = 0; g_paused = 1; send(CL_CMDACK, a, 0, ack, 4); return 1; }
    if (b == CMD_RESUME) { g_step = 0; g_paused = 0; send(CL_CMDACK, a, 0, ack, 4); return 1; }
    if (b == CMD_STEP) {
        g_step = len >= 4 ? cl_rd32(pl) : 1;
        g_paused = 1;
        cl_wr32(ack, g_n + g_step);
        send(CL_CMDACK, a, 0, ack, 4);
        return 1;
    }
    if (!g_cb) { memcpy(ack + 4, "no handler", 10); send(CL_CMDACK, a, 1, ack, 14); return 1; }
    for (uint32_t i = g_cmd_r; i != g_cmd_w; ++i)   /* a repeated CMD (lost answer) is queued once */
        if (g_cmdq[i % CMDQ_N].id == a) return 1;
    if (g_cmd_w - g_cmd_r >= CMDQ_N) { memcpy(ack + 4, "busy", 4); send(CL_CMDACK, a, 2, ack, 8); return 1; }
    cmd_t *c = &g_cmdq[g_cmd_w % CMDQ_N];
    c->id = a; c->code = (uint8_t)b;
    c->len = (uint8_t)(len < 0 ? 0 : len > 64 ? 64 : len);
    memcpy(c->arg, pl, c->len);
    BARRIER();
    g_cmd_w++;
    return 1;
}

void dcql_live_pump(dcql_live_send_fn send) {
    if (!g_up) return;
    const uint64_t t0 = timer_us_gettime64();
    uint8_t b[CL_PAYLOAD_MAX];
    /* records: as many as fit a datagram */
    while (g_rec_r != g_rec_w) {
        size_t o = 0;
        uint32_t first = 0, k = 0, r = g_rec_r;
        while (r != g_rec_w) {
            const dcql_frame_t *f = &g_rec[r % REC_N];
            const size_t need = 8 + 4u * (f->nph + f->nctr) + (f->has_pos ? 16 : 0);
            if (o + need > sizeof(b)) break;
            if (!k) first = f->n;
            cl_wr32(b + o, f->n); cl_wr16(b + o + 4, f->has_pos ? 1 : 0); b[o + 6] = f->nph; b[o + 7] = f->nctr;
            o += 8;
            for (unsigned i = 0; i < f->nph; ++i, o += 4) cl_wr32(b + o, f->phase_us[i]);
            for (unsigned i = 0; i < f->nctr; ++i, o += 4) cl_wr32(b + o, (uint32_t)f->ctr[i]);
            if (f->has_pos) {
                cl_wr32(b + o, f->room);
                memcpy(b + o + 4, f->pos, 12);
                o += 16;
            }
            ++k; ++r;
        }
        if (!k || !send(CL_REC, first, (uint16_t)k, b, (uint16_t)o)) break;
        g_st.recs_sent += k;
        BARRIER();
        g_rec_r = r;
    }
    /* trace events, numbered */
    while (g_tr_r != g_tr_w) {
        size_t o = 0;
        uint32_t k = 0, r = g_tr_r;
        while (r != g_tr_w) {
            const tr_t *t = &g_tr[r % TR_N];
            if (o + 8 + t->len > sizeof(b)) break;
            cl_wr32(b + o, t->n); cl_wr16(b + o + 4, t->d); b[o + 6] = t->lane; b[o + 7] = t->len;
            memcpy(b + o + 8, t->label, t->len);
            o += 8 + t->len;
            ++k; ++r;
        }
        if (!send(CL_TRACE, g_tr_seq - (g_tr_w - g_tr_r), (uint16_t)k, b, (uint16_t)o)) break;
        g_st.trace_sent += k;
        BARRIER();
        g_tr_r = r;
    }
    /* log lines: LINE with b = level (its own sequence, above the serial lines') */
    while (g_lg_r != g_lg_w) {
        const lg_t *l = &g_lg[g_lg_r % LOG_N];
        cl_wr32(b, l->n);
        memcpy(b + 4, l->text, l->len);
        if (!send(CL_LINE, g_line_seq_base++, l->level, b, (uint16_t)(4 + l->len))) break;
        g_st.logs_sent++;
        BARRIER();
        g_lg_r++;
    }
    /* answers to the commands the game ran */
    while (g_ack_r != g_ack_w) {
        const ack_t *a = &g_ack[g_ack_r % ACK_N];
        cl_wr32(b, a->n);
        memcpy(b + 4, a->why, a->len);
        if (!send(CL_CMDACK, a->id, a->status, b, (uint16_t)(4 + a->len))) break;
        BARRIER();
        g_ack_r++;
    }
    g_st.pump_us_total += (uint32_t)(timer_us_gettime64() - t0);
    g_st.pumps++;
}
