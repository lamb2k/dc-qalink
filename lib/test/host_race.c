/* host_race.c: host test of who may touch the link's rings and the /pc read-ahead block at once (lane ql-fix-dc round
 * 2, 2026-11-23; audit item 2), built with ThreadSanitizer by lib/test/run.sh. MIT licence.
 *   rings  lib/dcql_ring.h as lib/dcql_net.c uses it: a receive thread puts and the program takes, the program sends
 *          and a send thread takes, while a controller runs stop / reconfigure / start 300 times, each time handing the
 *          link new rings of the program's own, freeing its old ones as soon as dcql_net_config would return, and
 *          now and then built-in (owned) rings that the swap frees. Every datagram is checked byte for byte.
 *   ra     loader/cl_ra.h as loader/client.c uses it: three threads read three files through the one read-ahead block
 *          (the PC's fetch yields half way through filling it), then the way out releases the block while they read.
 *          Every byte read is checked against its file.
 * The console's exclusions are interrupts off (rings) and a KallistiOS mutex (read-ahead); here both are pthread
 * mutexes. --control runs copies of the code at 2ff9b9d (receive and take without the exclusion, the swap after a
 * 2 ms sleep; the read-ahead held by a count with its bookkeeping and copy open), which ThreadSanitizer must catch. */
#define _GNU_SOURCE
#include <pthread.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static pthread_mutex_t g_ring_mx = PTHREAD_MUTEX_INITIALIZER;   /* the console's interrupts-off */
#define DCQL_RING_LOCK_T int
#define DCQL_RING_LOCK(m) ((void)pthread_mutex_lock(&g_ring_mx), (m) = 0)
#define DCQL_RING_UNLOCK(m) ((void)(m), (void)pthread_mutex_unlock(&g_ring_mx))
static int g_live;   /* owned ring blocks alive */
static unsigned char *t_alloc(unsigned n) { __atomic_add_fetch(&g_live, 1, __ATOMIC_RELAXED); return malloc(n); }
static void t_free(void *p) { if (p) { __atomic_sub_fetch(&g_live, 1, __ATOMIC_RELAXED); free(p); } }
#define DCQL_RING_ALLOC(n) t_alloc(n)
#define DCQL_RING_FREE(p) t_free(p)
#include "../dcql_ring.h"

static int lock_ms(pthread_mutex_t *l, unsigned ms) {
    struct timespec t;
    clock_gettime(CLOCK_REALTIME, &t);
    t.tv_nsec += (long)(ms % 1000) * 1000000L; t.tv_sec += ms / 1000 + t.tv_nsec / 1000000000L; t.tv_nsec %= 1000000000L;
    return pthread_mutex_timedlock(l, &t) ? -1 : 0;
}
#define CL_RA_LOCK_T pthread_mutex_t
#define CL_RA_LOCK_INIT PTHREAD_MUTEX_INITIALIZER
#define CL_RA_LOCK(l) pthread_mutex_lock(l)
#define CL_RA_LOCK_MS(l, ms) lock_ms((l), (ms))
#define CL_RA_UNLOCK(l) pthread_mutex_unlock(l)
static int g_ra_live;
static uint8_t *ra_alloc(unsigned n) { __atomic_add_fetch(&g_ra_live, 1, __ATOMIC_RELAXED); return malloc(n); }
static void ra_free_(void *p) { __atomic_sub_fetch(&g_ra_live, 1, __ATOMIC_RELAXED); free(p); }
#define CL_RA_ALLOC(n) ra_alloc(n)
#define CL_RA_FREE(p) ra_free_(p)
#include "../../loader/cl_ra.h"

static int g_control, g_bad_ring, g_bad_ra;
static int g_done;
#define DONE() __atomic_load_n(&g_done, __ATOMIC_ACQUIRE)
static int g_up;   /* the link's state as the threads see it (atomic loads/stores) */
#define UP() __atomic_load_n(&g_up, __ATOMIC_ACQUIRE)
static void bad(int *n, const char *what) { if (__atomic_fetch_add(n, 1, __ATOMIC_RELAXED) < 3) printf("BAD %s\n", what); }

/* ================================================================== rings */
static ring_t g_rx, g_tx;
static unsigned g_rnd_state = 1;
static void fill(unsigned char *b, unsigned n, unsigned seq) { for (unsigned i = 0; i < n; ++i) b[i] = (unsigned char)(seq + i); }
static int whole(const unsigned char *b, int n) { for (int i = 1; i < n; ++i) if (b[i] != (unsigned char)(b[0] + i)) return 0; return 1; }
/* 2ff9b9d: the receive thread put and the program took with no exclusion; dcql_net_send put with interrupts off */
static int r_put(ring_t *r, const void *p, unsigned n) { return g_control ? ring_put(r, p, n) : ringx_put(r, p, n); }
static int r_get(ring_t *r, void *p, unsigned max) { return g_control ? ring_get(r, p, max) : ringx_get(r, p, max); }
static long g_moved;
static void *producer(void *arg) {   /* rx: the adapter's receive thread; tx: dcql_net_send from the program */
    ring_t *r = arg;
    unsigned char b[1200];
    unsigned seq = r == &g_rx ? 7 : 91, rs = seq;
    while (!DONE()) {
        if (!UP()) { sched_yield(); continue; }
        rs = rs * 1103515245u + 12345u;
        const unsigned n = 1 + (rs >> 8) % 1200u;
        fill(b, n, seq++);
        if (r == &g_tx) (void)ringx_put(r, b, n);   /* dcql_net_send: inside the exclusion in both */
        else (void)r_put(r, b, n);
    }
    return NULL;
}
static void *consumer(void *arg) {   /* rx: dcql_net_recv in the program; tx: the adapter's send thread */
    ring_t *r = arg;
    unsigned char b[1200];
    while (!DONE()) {
        const int n = r_get(r, b, sizeof(b));
        if (n > 0) { if (!whole(b, n)) bad(&g_bad_ring, "ring: a datagram came out torn"); __atomic_add_fetch(&g_moved, 1, __ATOMIC_RELAXED); }
        else sched_yield();
    }
    return NULL;
}
static void reconfigure(unsigned char *rxm, unsigned char *txm, unsigned n) {
    unsigned char *old[2] = {NULL, NULL};
    if (g_control) {   /* 2ff9b9d rings_swap: the state checked, a 2 ms sleep, the pointers changed with interrupts off */
        usleep(2000);
        int m; DCQL_RING_LOCK(m);
        old[0] = ring_take(&g_rx, rxm, n, 0);
        old[1] = ring_take(&g_tx, txm, n, 0);
        DCQL_RING_UNLOCK(m);
    } else rings_replace(&g_rx, rxm, n, &g_tx, txm, n, old);   /* this head */
    t_free(old[0]); t_free(old[1]);
}
static void test_rings(void) {
    __atomic_store_n(&g_done, 0, __ATOMIC_RELEASE);
    __atomic_store_n(&g_up, 0, __ATOMIC_RELEASE);
    if (!rings_alloc(&g_rx, &g_tx, 4096)) { printf("FAIL rings: no memory\n"); return; }
    pthread_t t[4];
    pthread_create(&t[0], NULL, producer, &g_rx); pthread_create(&t[1], NULL, consumer, &g_rx);
    pthread_create(&t[2], NULL, producer, &g_tx); pthread_create(&t[3], NULL, consumer, &g_tx);
    unsigned char *mine[2] = {NULL, NULL};
    for (int i = 0; i < 300; ++i) {
        __atomic_store_n(&g_up, 1, __ATOMIC_RELEASE);   /* start */
        ringx_clear(&g_rx); ringx_clear(&g_tx);
        usleep(300);   /* traffic */
        __atomic_store_n(&g_up, 0, __ATOMIC_RELEASE);   /* stop: the link off, its threads still running */
        const unsigned n = 2048u + 512u * (unsigned)(i % 5);
        if (i % 3 == 2) {   /* built-in rings again (the test's stand-in for a link that allocated its own) */
            unsigned char *a = t_alloc(n), *b = t_alloc(n);
            int m; DCQL_RING_LOCK(m);
            unsigned char *o0 = ring_take(&g_rx, a, n, 1), *o1 = ring_take(&g_tx, b, n, 1);
            DCQL_RING_UNLOCK(m);
            t_free(o0); t_free(o1);
        } else {
            unsigned char *rxm = malloc(n), *txm = malloc(n);
            reconfigure(rxm, txm, n);
        }
        free(mine[0]); free(mine[1]);   /* the program reuses its old rings once dcql_net_config has returned */
        mine[0] = mine[1] = NULL;
        if (i % 3 != 2) { mine[0] = g_rx.buf; mine[1] = g_tx.buf; }
    }
    __atomic_store_n(&g_done, 1, __ATOMIC_RELEASE);
    for (int i = 0; i < 4; ++i) pthread_join(t[i], NULL);
    unsigned char *old[2];
    rings_replace(&g_rx, NULL, 0, &g_tx, NULL, 0, old);
    if (g_rx.owned) { t_free(g_rx.buf); t_free(g_tx.buf); }
    free(mine[0]); free(mine[1]);
    printf("rings: %ld datagrams moved across 300 stop/reconfigure/start rounds, owned blocks alive %d\n", g_moved, g_live);
    if (g_moved < 300) printf("FAIL rings: too little traffic to judge (%ld)\n", g_moved);
    if (g_live) printf("FAIL rings: %d owned blocks leaked\n", g_live);
    if (g_bad_ring) printf("FAIL rings: %d datagrams came out torn\n", g_bad_ring);
    else printf("ok   rings: every datagram whole through stop/reconfigure/start with traffic\n");
}

/* ================================================================== read-ahead */
#define RA_SIZE 4096u
#define FILE_SIZE 65536u
typedef struct { uint32_t handle; } file_t;
static unsigned char at_of(uint32_t h, uint32_t pos) { return (unsigned char)(h * 31u + pos * 7u + (pos >> 8)); }
static int fetch(void *ctx, uint32_t handle, uint32_t pos, uint8_t *dst, uint32_t n) {
    (void)ctx;
    for (uint32_t i = 0; i < n / 2; ++i) dst[i] = at_of(handle, pos + i);
    sched_yield();   /* the round trip to the PC: other threads run */
    for (uint32_t i = n / 2; i < n; ++i) dst[i] = at_of(handle, pos + i);
    return 0;
}
static cl_ra_t g_ra = CL_RA_INIT(RA_SIZE);
/* 2ff9b9d loader/client.c: a read holds the block by a count; the bookkeeping, fetch and copy are outside any lock */
static struct { uint8_t *buf; const void *file; uint32_t handle, off, len; int busy, closed; pthread_mutex_t irq; } o = {
    NULL, NULL, 0, 0, 0, 0, 0, PTHREAD_MUTEX_INITIALIZER};
static int old_read(const void *f, uint32_t h, uint32_t pos, uint32_t fsize, void *dst, uint32_t cnt) {
    pthread_mutex_lock(&o.irq);
    const int ok = o.buf && !o.closed;
    if (ok) ++o.busy;
    pthread_mutex_unlock(&o.irq);
    if (!ok) return -2;
    if (!(o.file == f && o.handle == h && pos >= o.off && pos + cnt <= o.off + o.len)) {
        const uint32_t n = fsize - pos < RA_SIZE ? fsize - pos : RA_SIZE;
        o.file = NULL;
        fetch(NULL, h, pos, o.buf, n);
        o.file = f; o.handle = h; o.off = pos; o.len = n;
    }
    memcpy(dst, o.buf + (pos - o.off), cnt);
    pthread_mutex_lock(&o.irq); --o.busy; pthread_mutex_unlock(&o.irq);
    return (int)cnt;
}
static int old_release(void) {
    o.closed = 1;
    for (int i = 0; i < 20 && o.busy; ++i) usleep(10000);
    pthread_mutex_lock(&o.irq);
    uint8_t *p = o.busy ? NULL : o.buf;
    if (p) { o.buf = NULL; o.file = NULL; }
    pthread_mutex_unlock(&o.irq);
    if (p) ra_free_(p);
    return p ? 1 : -1;
}
static void *reader(void *arg) {
    file_t *f = arg;
    unsigned char b[1024];
    unsigned rs = f->handle;
    for (int pass = 0; pass < 6; ++pass)
        for (uint32_t pos = 0; pos < FILE_SIZE;) {
            rs = rs * 1103515245u + 12345u;
            uint32_t cnt = 1 + (rs >> 8) % 1000u;
            if (cnt > FILE_SIZE - pos) cnt = FILE_SIZE - pos;
            uint32_t fetched;
            int r = g_control ? old_read(f, f->handle, pos, FILE_SIZE, b, cnt)
                              : cl_ra_read(&g_ra, f, f->handle, pos, FILE_SIZE, b, cnt, fetch, NULL, &fetched);
            if (r == -2) { fetch(NULL, f->handle, pos, b, cnt); r = (int)cnt; }   /* unbuffered, as pc_read */
            for (uint32_t i = 0; i < cnt; ++i)
                if (b[i] != at_of(f->handle, pos + i)) { bad(&g_bad_ra, "ra: a read returned another file's or offset's bytes"); break; }
            pos += cnt;
        }
    return NULL;
}
static void test_ra(void) {
    file_t f[3] = {{11}, {12}, {13}};
    if (g_control) o.buf = ra_alloc(RA_SIZE);
    else for (int i = 0; i < 3; ++i) cl_ra_open(&g_ra);   /* three opens: one block */
    pthread_t t[3];
    for (int i = 0; i < 3; ++i) pthread_create(&t[i], NULL, reader, &f[i]);
    usleep(20000);
    const int rel = g_control ? old_release() : cl_ra_release(&g_ra, 200);   /* the way out while they read */
    for (int i = 0; i < 3; ++i) pthread_join(t[i], NULL);
    if (!g_control) { cl_ra_forget(&g_ra, &f[0]); (void)cl_ra_free(&g_ra); cl_ra_open(&g_ra); }   /* closes after the way out */
    printf("ra: way out released the block: %d; blocks alive after the closes %d\n", rel, g_ra_live);
    if (!g_control && g_ra_live) printf("FAIL ra: the block outlived the last close (%d)\n", g_ra_live);
    if (!g_control && g_ra.buf) printf("FAIL ra: a block was allocated after the way out began\n");
    if (g_bad_ra) printf("FAIL ra: %d reads returned another file's or offset's bytes\n", g_bad_ra);
    else printf("ok   ra: every byte read through the shared block was its own file's\n");
}

int main(int argc, char **argv) {
    setvbuf(stdout, NULL, _IONBF, 0);
    g_control = argc > 1 && !strcmp(argv[1], "--control");
    (void)g_rnd_state;
    printf("host_race: %s\n", g_control ? "CONTROL: the code at 2ff9b9d (ThreadSanitizer must report)"
                                        : "this head's lib/dcql_ring.h and loader/cl_ra.h");
    test_rings();
    test_ra();
    printf("host_race: %d torn datagrams, %d wrong reads\n", g_bad_ring, g_bad_ra);
    return g_bad_ring || g_bad_ra ? 1 : 0;
}
