/* dcql_disc.c: the network disc, console side (dc-qalink 0.2, lane ql-disc, 2026-11-22). API, memory and wire:
   dcql_disc.h. MIT licence.

   One KallistiOS VFS handler at the mount name ("/cd"). KallistiOS picks the newest handler of the longest matching
   name, so ours shadows the ISO 9660 one, whose handler we keep and call for every file the PC does not serve.
   Requests: up to `window` in flight, each `block` bytes in 1 KiB pieces; the link thread copies each piece straight
   into its destination (a cache block, or the caller's buffer for whole blocks) and a resend asks only for the
   missing pieces. */
#include <kos.h>
#include <kos/fs.h>
#include <kos/nmmgr.h>
#include <kos/mutex.h>
#include <kos/sem.h>
#include <arch/irq.h>
#include <arch/timer.h>
#include <errno.h>
#include <fcntl.h>
#include <string.h>
#include <sys/stat.h>
#include "dcql_loop.h"
#include "dcql_net.h"
#include "cl_client.h"
#include "dcql_disc.h"

#define NREQ DCQL_DISC_REQS
#define NFILE DCQL_DISC_FILES
#define MAXSLOT (DCQL_DISC_CACHE / 1024u)

typedef struct {
    volatile int state;          /* 0 free, 1 in flight, 2 done, 3 cancelled (late pieces are dropped) */
    uint32_t id, handle, off, len, want, sent_ms;
    volatile uint32_t got;
    uint8_t *dst;
    int slot;                    /* the cache block it fills, -1 = the caller's buffer */
    uint64_t t0_us;
    volatile uint64_t done_us;
} req_t;
typedef struct { int valid, req; uint32_t handle, off, len, used; } slot_t;
typedef struct {
    int used, side, dir;
    void *bh;                    /* the real disc's handle (side DISC) */
    uint32_t handle, size, pos;  /* the PC's handle (side PC) */
    dirent_t de;
} df_t;
typedef struct { uint32_t hash, size; } tent_t;

static dcql_disc_config_t g_cfg;
static vfs_handler_t *g_base;    /* the real disc's handler, kept from before the mount */
static int g_mounted, g_whole;
static uint32_t g_block, g_slots, g_tflags, g_tcount;
static tent_t g_table[DCQL_DISC_TABLE_N];
static uint8_t g_cache[DCQL_DISC_CACHE] __attribute__((aligned(32)));
static slot_t g_slot[MAXSLOT];
static req_t g_req[NREQ];
static df_t g_f[NFILE];
static uint32_t g_next_id = 0x40000000u, g_use;
static uint32_t g_prev_id, g_prev_us;
static mutex_t g_mx = MUTEX_INITIALIZER;
static semaphore_t g_sem = SEM_INITIALIZER(0);
static volatile uint32_t g_open_id, g_open_handle, g_open_size, g_open_flags;
static volatile int32_t g_open_status;
static volatile int g_open_done;
static dcql_disc_stats_t g_st;

static uint64_t now_ms(void) { return timer_ms_gettime64(); }
static uint64_t now_us(void) { return timer_us_gettime64(); }

uint32_t dcql_disc_hash(const char *p) {
    while (*p == '/' || *p == '\\') ++p;
    uint32_t h = 2166136261u;
    for (; *p && *p != ';'; ++p) {
        unsigned char c = (unsigned char)*p;
        if (c == '\\') c = '/';
        if (c >= 'a' && c <= 'z') c -= 32;
        h = (h ^ c) * 16777619u;
    }
    return h;
}

/* ------------------------------------------------------------------------------------------------ sending */
static int send_dg(uint8_t type, uint32_t a, uint16_t b, const void *pl, uint16_t n) {
    uint8_t d[CL_DGRAM_MAX];
    const size_t len = cl_frame(d, type, cl_run_id(), a, b, pl, n);
    for (int i = 0; len && i < 200; ++i) {   /* the ring full: 200 ms at most */
        if (dcql_net_send(d, (unsigned)len)) return 1;
        thd_sleep(1);
    }
    return 0;
}

/* ------------------------------------------------------------------------------------------------ the log (DLOG) */
static uint8_t g_log[1024];
static unsigned g_log_n;
static uint32_t g_log_seq;
static uint64_t g_log_at;
static struct { int fd, side; uint32_t off, end, us, calls; } g_run = {-1, 0, 0, 0, 0, 0};
static void log_send(void) {
    if (!g_log_n) return;
    if (send_dg(CL_DLOG, g_log_seq++, 0, g_log, (uint16_t)g_log_n)) ++g_st.log_dgrams;
    g_log_n = 0;
    g_log_at = now_ms();
}
static void log_put(const uint8_t *r, unsigned n) {
    if (g_cfg.no_log) return;
    if (g_log_n + n > sizeof(g_log)) log_send();
    memcpy(g_log + g_log_n, r, n);
    g_log_n += n;
    ++g_st.log_records;
}
static void run_flush(void) {
    if (g_run.fd < 0) return;
    uint8_t r[20];
    r[0] = DCQL_DL_READ; r[1] = (uint8_t)g_run.side; cl_wr16(r + 2, (uint16_t)g_run.fd);
    cl_wr32(r + 4, g_run.off); cl_wr32(r + 8, g_run.end - g_run.off); cl_wr32(r + 12, g_run.us);
    cl_wr32(r + 16, g_run.calls);
    log_put(r, sizeof(r));
    g_run.fd = -1;
}
static void log_read(int fd, int side, uint32_t off, uint32_t n, uint32_t us) {
    if (g_cfg.no_log) return;
    if (g_run.fd == fd && g_run.end == off) { g_run.end += n; g_run.us += us; g_run.calls++; }
    else {
        run_flush();
        g_run.fd = fd; g_run.side = side; g_run.off = off; g_run.end = off + n; g_run.us = us; g_run.calls = 1;
    }
    if (now_ms() - g_log_at > 500) { run_flush(); log_send(); }
}
static void log_open(int fd, int side, const char *fn, uint32_t size) {
    if (g_cfg.no_log) return;
    run_flush();
    uint8_t r[8 + 200];
    size_t n = strlen(fn);
    if (n > 200) n = 200;
    r[0] = DCQL_DL_OPEN; r[1] = (uint8_t)side; cl_wr16(r + 2, (uint16_t)fd); cl_wr32(r + 4, size);
    memcpy(r + 8, fn, n);
    r[8 + n] = 0;
    log_put(r, (unsigned)(9 + n));
}
static void log_close(int fd) {
    if (g_cfg.no_log) return;
    run_flush();
    uint8_t r[4] = {DCQL_DL_CLOSE, 0, 0, 0};
    cl_wr16(r + 2, (uint16_t)fd);
    log_put(r, 4);
    log_send();
}

/* ------------------------------------------------------------------------------------------------ requests */
int dcql_disc_datagram(uint8_t type, uint32_t a, uint16_t b, const uint8_t *pl, int len) {
    if (type == CL_DINFO) {
        if (a == g_open_id && len >= 16 && !g_open_done) {
            g_open_status = (int32_t)cl_rd32(pl);
            g_open_handle = cl_rd32(pl + 4);
            g_open_size = cl_rd32(pl + 8);
            g_open_flags = cl_rd32(pl + 12);
            g_open_done = 1;
            sem_signal(&g_sem);
        }
        return 1;
    }
    if (type != CL_DDATA) return 0;
    const irq_mask_t m = irq_disable();
    for (int k = 0; k < NREQ; ++k) {
        req_t *r = &g_req[k];
        if (r->state != 1 || r->id != a) continue;
        const uint32_t off = (uint32_t)b * CL_CHUNK;
        if (b < 32 && off < r->len && !(r->got & (1u << b))) {
            const uint32_t want = r->len - off < CL_CHUNK ? r->len - off : CL_CHUNK;
            if ((uint32_t)len == want) {
                memcpy(r->dst + off, pl, want);
                r->got |= 1u << b;
                if (r->got == r->want) { r->done_us = now_us(); r->state = 2; sem_signal(&g_sem); }
            }
        }
        break;
    }
    irq_restore(m);
    return 1;
}

static void req_send(req_t *r) {
    uint8_t pl[24];
    cl_wr32(pl, r->handle); cl_wr32(pl + 4, r->off); cl_wr32(pl + 8, r->len);
    cl_wr32(pl + 12, r->want & ~r->got); cl_wr32(pl + 16, g_prev_id); cl_wr32(pl + 20, g_prev_us);
    r->sent_ms = (uint32_t)now_ms();
    send_dg(CL_DREAD, r->id, 0, pl, sizeof(pl));
}
static int inflight(void) {
    int n = 0;
    for (int k = 0; k < NREQ; ++k) n += g_req[k].state == 1;
    return n;
}
static void req_cancel(req_t *r) {
    const irq_mask_t m = irq_disable();
    r->state = 3;
    irq_restore(m);
    if (r->slot >= 0 && g_slot[r->slot].req == (int)(r - g_req)) { g_slot[r->slot].req = -1; g_slot[r->slot].valid = 0; }
    r->state = 0;
}
/* a free request (a demand read takes one from a read-ahead when none is free) */
static req_t *req_get(int demand) {
    for (int k = 0; k < NREQ; ++k) if (g_req[k].state == 0) return &g_req[k];
    if (!demand) return NULL;
    for (int k = 0; k < NREQ; ++k)
        if (g_req[k].slot >= 0) { req_cancel(&g_req[k]); return &g_req[k]; }
    return NULL;
}
static req_t *req_start(uint32_t handle, uint32_t off, uint32_t len, uint8_t *dst, int slot, int demand) {
    req_t *r = req_get(demand);
    if (!r) return NULL;
    r->id = ++g_next_id;
    r->handle = handle; r->off = off; r->len = len; r->dst = dst; r->slot = slot;
    r->want = len >= 32 * CL_CHUNK ? 0xffffffffu : (1u << ((len + CL_CHUNK - 1) / CL_CHUNK)) - 1;
    r->got = 0;
    r->t0_us = now_us();
    r->done_us = 0;
    r->state = 1;
    ++g_st.requests;
    req_send(r);
    return r;
}
/* waits for one request: resends the missing pieces after 250 ms, then every 500 ms; fails at timeout_ms */
static int req_wait(req_t *r) {
    const uint64_t start = now_ms();
    int resends = 0;
    while (r->state == 1) {
        const uint64_t t = now_ms();
        if (t - start >= g_cfg.timeout_ms) { ++g_st.timeouts; req_cancel(r); return -1; }
        if ((uint32_t)t - r->sent_ms >= (resends ? 500u : 250u)) { ++resends; ++g_st.resends; req_send(r); }
        sem_wait_timed(&g_sem, 20);
    }
    if (r->state != 2) return -1;
    const uint32_t us = (uint32_t)(r->done_us - r->t0_us);
    g_prev_id = r->id; g_prev_us = us;
    g_st.req_us_total += us;
    if (us > g_st.req_us_worst) g_st.req_us_worst = us;
    return 0;
}
static void req_free(req_t *r) { r->state = 0; r->slot = -1; }

/* ------------------------------------------------------------------------------------------------ the cache */
static slot_t *slot_find(uint32_t handle, uint32_t off) {
    for (uint32_t i = 0; i < g_slots; ++i)
        if (g_slot[i].valid && g_slot[i].handle == handle && g_slot[i].off == off) return &g_slot[i];
    return NULL;
}
static int slot_take(void) {   /* an empty block, else the least recently used one not being filled */
    int best = -1;
    for (uint32_t i = 0; i < g_slots; ++i) {
        if (!g_slot[i].valid) return (int)i;
        if (g_slot[i].req < 0 && (best < 0 || g_slot[i].used < g_slot[best].used)) best = (int)i;
    }
    if (best < 0)   /* every block is being filled by a read-ahead nobody used (seeks): take the oldest one back */
        for (uint32_t i = 0; i < g_slots; ++i)
            if (best < 0 || g_slot[i].used < g_slot[best].used) best = (int)i;
    if (best >= 0) {
        if (g_slot[best].req >= 0) req_cancel(&g_req[g_slot[best].req]);
        g_slot[best].valid = 0;
        g_slot[best].req = -1;
    }
    return best;
}
/* starts filling a cache block of the file at `off` (a block boundary); NULL when no block or request is free */
static slot_t *slot_fill(df_t *f, uint32_t off, int demand) {
    const int i = slot_take();
    if (i < 0) return NULL;
    const uint32_t len = f->size - off < g_block ? f->size - off : g_block;
    req_t *r = req_start(f->handle, off, len, g_cache + (uint32_t)i * g_block, i, demand);
    if (!r) return NULL;
    g_slot[i] = (slot_t){1, (int)(r - g_req), f->handle, off, len, ++g_use};
    return &g_slot[i];
}
static int slot_ready(slot_t *s) {
    if (s->req < 0) return 0;
    req_t *r = &g_req[s->req];
    const int rc = req_wait(r);
    if (rc == 0) req_free(r);
    s->req = -1;
    if (rc) s->valid = 0;
    return rc;
}
static void readahead(df_t *f, uint32_t off) {
    if (off >= f->size || slot_find(f->handle, off) || inflight() >= (int)g_cfg.window) return;
    slot_fill(f, off, 0);
}
static void forget(uint32_t handle) {
    for (uint32_t i = 0; i < g_slots; ++i)
        if (g_slot[i].valid && g_slot[i].handle == handle) {
            if (g_slot[i].req >= 0) req_cancel(&g_req[g_slot[i].req]);
            g_slot[i].valid = 0;
        }
}

/* whole blocks straight into the caller's buffer, `window` in flight */
static int direct(df_t *f, uint8_t *dst, uint32_t nblk) {
    req_t *q[4];
    uint32_t issued = 0, waited = 0;
    const uint32_t w = g_cfg.window;
    while (waited < nblk) {
        while (issued < nblk && issued - waited < w) {
            req_t *r = req_start(f->handle, f->pos + issued * g_block, g_block, dst + issued * g_block, -1, 1);
            if (!r) break;
            q[issued % w] = r;
            ++issued;
        }
        if (issued == waited) return -1;
        if (req_wait(q[waited % w])) {
            for (uint32_t k = waited + 1; k < issued; ++k) req_cancel(q[k % w]);
            return -1;
        }
        req_free(q[waited % w]);
        ++waited;
        ++g_st.direct_blocks;
    }
    return 0;
}

static ssize_t pc_read_locked(df_t *f, uint8_t *buf, size_t cnt) {
    if (f->pos >= f->size) return 0;
    if (cnt > f->size - f->pos) cnt = f->size - f->pos;
    size_t done = 0;
    while (done < cnt) {
        const uint32_t boff = f->pos - f->pos % g_block;
        slot_t *s = slot_find(f->handle, boff);
        if (!s && f->pos == boff && cnt - done >= g_block) {
            const uint32_t nblk = (uint32_t)((cnt - done) / g_block);
            if (direct(f, buf + done, nblk)) { errno = EIO; return done ? (ssize_t)done : -1; }
            f->pos += nblk * g_block;
            done += nblk * g_block;
            readahead(f, f->pos - f->pos % g_block);
            continue;
        }
        if (!s) {
            s = slot_fill(f, boff, 1);
            if (!s) { errno = EIO; return done ? (ssize_t)done : -1; }
        } else if (s->req < 0) ++g_st.cache_hits;
        else ++g_st.readahead_hits;
        if (s->req >= 0 && slot_ready(s)) { errno = EIO; return done ? (ssize_t)done : -1; }
        s->used = ++g_use;
        uint32_t n = s->off + s->len - f->pos;
        if (n > cnt - done) n = (uint32_t)(cnt - done);
        memcpy(buf + done, g_cache + (uint32_t)(s - g_slot) * g_block + (f->pos - s->off), n);
        f->pos += n;
        done += n;
        for (uint32_t k = 1; k <= g_cfg.window; ++k) readahead(f, boff + k * g_block);   /* `window` blocks ahead */
    }
    return (ssize_t)done;
}

/* DOPEN: returns the status; handle, size and flags set on 0 */
static int pc_open_req(const char *path, uint32_t flags, uint32_t *handle, uint32_t *size, uint32_t *oflags) {
    uint8_t pl[4 + 400];
    size_t n = strlen(path);
    if (n > 400) return DCQL_DS_NOENT;
    cl_wr32(pl, flags);
    memcpy(pl + 4, path, n);
    const uint32_t id = ++g_next_id;
    irq_mask_t m = irq_disable();
    g_open_id = id; g_open_done = 0;
    irq_restore(m);
    ++g_st.asked_pc;
    const uint64_t start = now_ms();
    uint64_t sent = 0;
    int resends = 0;
    while (!g_open_done) {
        const uint64_t t = now_ms();
        if (t - start >= g_cfg.timeout_ms) { ++g_st.timeouts; g_open_id = 0; return -1000; }
        if (!sent || t - sent >= (resends ? 500u : 250u)) {
            if (sent) { ++resends; ++g_st.resends; }
            send_dg(CL_DOPEN, id, 0, pl, (uint16_t)(4 + n));
            sent = t;
        }
        sem_wait_timed(&g_sem, 20);
    }
    g_open_id = 0;
    if (g_open_status == 0) { *handle = g_open_handle; *size = g_open_size; *oflags = g_open_flags; }
    return g_open_status;
}

static int table_has(uint32_t h) {
    int lo = 0, hi = (int)g_tcount - 1;
    while (lo <= hi) {
        const int mid = (lo + hi) / 2;
        if (g_table[mid].hash == h) return 1;
        if (g_table[mid].hash < h) lo = mid + 1; else hi = mid - 1;
    }
    return 0;
}

/* ------------------------------------------------------------------------------------------------ the VFS */
static df_t *fget(void *h) {
    const intptr_t i = (intptr_t)h - 1;
    return i >= 0 && i < NFILE && g_f[i].used ? &g_f[i] : NULL;
}
static int fslot(void) {
    for (int i = 0; i < NFILE; ++i) if (!g_f[i].used) return i;
    return -1;
}

static void *d_open(vfs_handler_t *vfs, const char *fn, int mode) {
    (void)vfs;
    if ((mode & O_MODE_MASK) != O_RDONLY) { errno = EROFS; return NULL; }
    mutex_lock(&g_mx);
    const int i = fslot();
    if (i < 0) { mutex_unlock(&g_mx); errno = EMFILE; return NULL; }
    df_t *f = &g_f[i];
    memset(f, 0, sizeof(*f));
    const char *rel = fn;
    while (*rel == '/') ++rel;
    const int dir = (mode & O_DIR) != 0;
    const int ask = g_whole || (g_tflags & DCQL_DT_OVERFLOW) || (!dir && table_has(dcql_disc_hash(rel)));
    if (ask) {
        uint32_t h, size, fl;
        const int st = pc_open_req(rel, (dir ? DCQL_DO_DIR : 0) | (g_whole ? 0 : DCQL_DO_OVERLAY), &h, &size, &fl);
        if (st == 0) {
            *f = (df_t){1, DCQL_SIDE_PC, dir, NULL, h, size, 0, {0}};
            ++g_st.opens_pc;
            log_open(i, DCQL_SIDE_PC, rel, size);
            mutex_unlock(&g_mx);
            return (void *)(intptr_t)(i + 1);
        }
        if (g_whole || st != DCQL_DS_BASE) {
            ++g_st.opens_refused;
            mutex_unlock(&g_mx);
            errno = st == -1000 ? EIO : ENOENT;
            return NULL;
        }
    }
    if (!g_base || !g_base->open) { ++g_st.opens_refused; mutex_unlock(&g_mx); errno = ENOENT; return NULL; }
    void *bh = g_base->open(g_base, fn, mode);
    if (!bh) { ++g_st.opens_refused; mutex_unlock(&g_mx); return NULL; }
    *f = (df_t){1, DCQL_SIDE_DISC, dir, bh, 0, 0, 0, {0}};
    ++g_st.opens_disc;
    if (!dir) log_open(i, DCQL_SIDE_DISC, rel, g_base->total ? (uint32_t)g_base->total(bh) : 0);
    mutex_unlock(&g_mx);
    return (void *)(intptr_t)(i + 1);
}

static int d_close(void *h) {
    mutex_lock(&g_mx);
    df_t *f = fget(h);
    if (!f) { mutex_unlock(&g_mx); return -1; }
    const int i = (int)(f - g_f);
    if (f->side == DCQL_SIDE_PC) {
        forget(f->handle);
        uint8_t pl[8];
        cl_wr32(pl, g_prev_id); cl_wr32(pl + 4, g_prev_us);
        send_dg(CL_DCLOSE, f->handle, 0, pl, sizeof(pl));
    } else if (g_base && g_base->close) g_base->close(f->bh);
    if (!f->dir) log_close(i);
    f->used = 0;
    mutex_unlock(&g_mx);
    return 0;
}

static ssize_t d_read(void *h, void *buf, size_t cnt) {
    mutex_lock(&g_mx);
    df_t *f = fget(h);
    if (!f || f->dir) { mutex_unlock(&g_mx); errno = EBADF; return -1; }
    const uint64_t t0 = now_us();
    ssize_t rc;
    uint32_t off;
    if (f->side == DCQL_SIDE_PC) {
        off = f->pos;
        rc = pc_read_locked(f, (uint8_t *)buf, cnt);
    } else {
        off = g_base->tell ? (uint32_t)g_base->tell(f->bh) : 0;
        rc = g_base->read ? g_base->read(f->bh, buf, cnt) : -1;
    }
    const uint32_t us = (uint32_t)(now_us() - t0);
    if (rc > 0) {
        if (f->side == DCQL_SIDE_PC) { ++g_st.reads_pc; g_st.bytes_pc += (uint32_t)rc; g_st.us_pc += us; }
        else { ++g_st.reads_disc; g_st.bytes_disc += (uint32_t)rc; g_st.us_disc += us; }
        log_read((int)(f - g_f), f->side, off, (uint32_t)rc, us);
    }
    mutex_unlock(&g_mx);
    return rc;
}

static off_t d_seek(void *h, off_t off, int whence) {
    mutex_lock(&g_mx);
    df_t *f = fget(h);
    off_t p = -1;
    if (f && f->side == DCQL_SIDE_PC) {
        p = whence == SEEK_SET ? off : whence == SEEK_CUR ? (off_t)f->pos + off : (off_t)f->size + off;
        if (p < 0) p = 0;
        if (p > (off_t)f->size) p = f->size;
        f->pos = (uint32_t)p;
    } else if (f && g_base->seek) p = g_base->seek(f->bh, off, whence);
    mutex_unlock(&g_mx);
    return p;
}
static off_t d_tell(void *h) {
    df_t *f = fget(h);
    if (!f) return -1;
    return f->side == DCQL_SIDE_PC ? (off_t)f->pos : g_base->tell ? g_base->tell(f->bh) : -1;
}
static size_t d_total(void *h) {
    df_t *f = fget(h);
    if (!f) return (size_t)-1;
    return f->side == DCQL_SIDE_PC ? f->size : g_base->total ? g_base->total(f->bh) : (size_t)-1;
}
/* a PC directory listing: entries {u32 size (0xffffffff = directory), u16 name length, name} */
static const dirent_t *d_readdir(void *h) {
    mutex_lock(&g_mx);
    df_t *f = fget(h);
    const dirent_t *out = NULL;
    if (f && f->side == DCQL_SIDE_PC && f->dir) {
        uint8_t hd[6];
        if (pc_read_locked(f, hd, 6) == 6) {
            const uint32_t size = cl_rd32(hd);
            uint16_t n = cl_rd16(hd + 4);
            char name[NAME_MAX];
            const uint16_t keep = n < NAME_MAX - 1 ? n : NAME_MAX - 1;
            if (pc_read_locked(f, (uint8_t *)name, keep) == keep) {
                if (n > keep) f->pos += n - keep;
                name[keep] = 0;
                memset(&f->de, 0, sizeof(f->de));
                memcpy(f->de.name, name, keep + 1u);
                f->de.size = size == 0xffffffffu ? -1 : (int)size;
                f->de.attr = size == 0xffffffffu ? O_DIR : 0;
                out = &f->de;
            }
        }
    } else if (f && f->side == DCQL_SIDE_DISC && g_base->readdir) out = g_base->readdir(f->bh);
    mutex_unlock(&g_mx);
    return out;
}
static int d_fstat(void *h, struct stat *st) {
    df_t *f = fget(h);
    if (!f) return -1;
    if (f->side == DCQL_SIDE_DISC && g_base->fstat) return g_base->fstat(f->bh, st);
    memset(st, 0, sizeof(*st));
    st->st_mode = (f->dir ? S_IFDIR | 0555 : S_IFREG | 0444);
    st->st_size = f->dir ? 0 : f->size;
    st->st_blksize = 2048;
    return 0;
}
static int d_stat(vfs_handler_t *vfs, const char *path, struct stat *st, int flag) {
    (void)flag;
    void *h = d_open(vfs, path, O_RDONLY);
    if (!h) {
        h = d_open(vfs, path, O_RDONLY | O_DIR);
        if (!h) return -1;
    }
    const int rc = d_fstat(h, st);
    d_close(h);
    return rc;
}

static vfs_handler_t g_vfs = {
    .nmmgr = {"/cd", 0, 0x00010000, 0, NMMGR_TYPE_VFS, NMMGR_LIST_INIT},
    .open = d_open, .close = d_close, .read = d_read, .seek = d_seek, .tell = d_tell, .total = d_total,
    .readdir = d_readdir, .fstat = d_fstat, .stat = d_stat};

/* ------------------------------------------------------------------------------------------------ mount */
int dcql_disc_mount(const dcql_disc_config_t *c) {
    if (g_mounted) return -4;
    dcql_disc_config_t d = {0};
    if (c) d = *c;
    if (!d.mount) d.mount = "/cd";
    /* defaults from the Flycast round f1 (ql-disc): more than about 8 KiB arriving at once overran the receive path
       (KallistiOS's adapter ring is 16 KiB with frame headers; the PC sends a request's pieces back to back), each loss
       costing a 250 ms resend; 2 x 4 KiB kept every piece. window * block is held to 16 KiB, the ring's size. */
    if (!d.window) d.window = 2;
    if (!d.block) d.block = 4 * 1024;
    if (!d.timeout_ms) d.timeout_ms = 3000;
    if (!d.link_wait_ms) d.link_wait_ms = 20000;
    if (d.window > 4 || d.block % 1024 || d.block > 32 * 1024 || d.block > DCQL_DISC_CACHE ||
        d.window * d.block > 16u * 1024u ||
        strlen(d.mount) >= sizeof(g_vfs.nmmgr.pathname))
        return -5;
    g_cfg = d;
    g_block = d.block;
    g_slots = DCQL_DISC_CACHE / d.block;
    if (g_slots > MAXSLOT) g_slots = MAXSLOT;
    memset(g_slot, 0, sizeof(g_slot));
    for (uint32_t i = 0; i < MAXSLOT; ++i) g_slot[i].req = -1;
    memset(g_req, 0, sizeof(g_req));
    for (int k = 0; k < NREQ; ++k) g_req[k].slot = -1;
    memset(&g_st, 0, sizeof(g_st));
    if (!cl_link_wait((int)d.link_wait_ms)) return -1;
    mutex_lock(&g_mx);
    uint32_t h, size, fl;
    const int st = pc_open_req(d.mount, DCQL_DO_MOUNT, &h, &size, &fl);
    if (st != 0) { mutex_unlock(&g_mx); return st == -1000 ? -2 : -3; }
    df_t tf = {1, DCQL_SIDE_PC, 0, NULL, h, size, 0, {0}};
    dcql_disc_table_t th;
    int ok = pc_read_locked(&tf, (uint8_t *)&th, sizeof(th)) == (ssize_t)sizeof(th) && th.magic == DCQL_DISC_MAGIC;
    if (ok) {
        g_tcount = th.count <= DCQL_DISC_TABLE_N ? th.count : 0;
        g_tflags = th.flags | (th.count > DCQL_DISC_TABLE_N ? DCQL_DT_OVERFLOW : 0);
        if (g_tcount)
            ok = pc_read_locked(&tf, (uint8_t *)g_table, g_tcount * sizeof(tent_t)) == (ssize_t)(g_tcount * sizeof(tent_t));
        g_st.files = th.files;
    }
    forget(h);
    uint8_t pl[8] = {0};
    send_dg(CL_DCLOSE, h, 0, pl, sizeof(pl));
    if (!ok) { mutex_unlock(&g_mx); return -2; }
    g_whole = d.mode == DCQL_DISC_PC || (d.mode == DCQL_DISC_AUTO && (g_tflags & DCQL_DT_WHOLE));
    g_base = nmmgr_lookup(d.mount) ? (vfs_handler_t *)nmmgr_lookup(d.mount) : NULL;
    if (g_base == &g_vfs) g_base = NULL;
    strncpy(g_vfs.nmmgr.pathname, d.mount, sizeof(g_vfs.nmmgr.pathname) - 1);
    nmmgr_handler_add(&g_vfs.nmmgr);
    g_mounted = 1;
    g_st.mounted = 1;
    g_st.mode = g_whole ? DCQL_DISC_PC : DCQL_DISC_OVERLAY;
    g_st.table_count = g_tcount;
    g_st.table_flags = g_tflags;
    g_st.requests = g_st.resends = g_st.timeouts = 0;   /* the table's own read is not the program's */
    g_st.req_us_total = g_st.req_us_worst = 0;
    g_st.direct_blocks = g_st.cache_hits = g_st.readahead_hits = 0;
    mutex_unlock(&g_mx);
    return 0;
}

void dcql_disc_unmount(void) {
    if (!g_mounted) return;
    for (int i = 0; i < NFILE; ++i)
        if (g_f[i].used) d_close((void *)(intptr_t)(i + 1));
    mutex_lock(&g_mx);
    run_flush();
    log_send();
    nmmgr_handler_remove(&g_vfs.nmmgr);
    g_mounted = 0;
    mutex_unlock(&g_mx);
}

void dcql_disc_stats(dcql_disc_stats_t *s) { *s = g_st; }

void dcql_disc_report(void) {
    const dcql_disc_stats_t *s = &g_st;
    cl_report("cl-disc: %s, table %u (flags %u, files %u), opens pc %u disc %u refused %u, reads pc %u (%u B, %u us) "
              "disc %u (%u B, %u us), requests %u resends %u timeouts %u, cache hits %u read-ahead hits %u direct %u, "
              "request us mean %u worst %u, window %u block %u, log %u in %u datagrams\n",
              !s->mounted ? "not mounted" : s->mode == DCQL_DISC_PC ? "whole disc from the PC" : "overlay",
              (unsigned)s->table_count, (unsigned)s->table_flags, (unsigned)s->files, (unsigned)s->opens_pc,
              (unsigned)s->opens_disc, (unsigned)s->opens_refused, (unsigned)s->reads_pc, (unsigned)s->bytes_pc,
              (unsigned)s->us_pc, (unsigned)s->reads_disc, (unsigned)s->bytes_disc, (unsigned)s->us_disc,
              (unsigned)s->requests, (unsigned)s->resends, (unsigned)s->timeouts, (unsigned)s->cache_hits,
              (unsigned)s->readahead_hits, (unsigned)s->direct_blocks,
              (unsigned)(s->requests ? s->req_us_total / s->requests : 0), (unsigned)s->req_us_worst, g_cfg.window,
              (unsigned)g_block, (unsigned)s->log_records, (unsigned)s->log_dgrams);
}
