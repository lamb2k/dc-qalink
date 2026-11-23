/* dcql_ring.h: the link layer's datagram rings, who owns their memory, and who may touch them when (lib/dcql_net.c;
 * lane ql-fix-dc, 2026-11-23, audit item 4; round 2: one exclusion for every access). MIT licence. Plain C with no
 * KallistiOS calls of its own, so the host tests (lib/test/host_lib.c, lib/test/host_race.c) build the code the console
 * runs.
 *
 * A ring is [u16 length][bytes] per datagram. Its memory is either
 *   owned:    the built-in rings, allocated by the link layer (DCQL_RING_ALLOC) when the program supplied none, or
 *   borrowed: the program's own memory from dcql_net_config(); never freed here.
 *
 * Exclusion (round 2, audit item 2 "safe reconfiguration"): every access a thread makes, ringx_put / ringx_get /
 * ringx_clear / ringx_used, and every change of a ring's memory (rings_alloc, rings_replace), runs inside
 * DCQL_RING_LOCK. On the console that is interrupts off (one CPU: no thread or handler runs inside it); each access
 * is one datagram copy of at most 1,200 B, done as at most two memcpy runs. So when rings_replace() returns, no
 * thread is inside the old memory and none can reach it any more: the caller frees the owned memory it hands back at
 * once, with no wait and no guess about the link's threads. A host test supplies a mutex in its place. */
#ifndef DCQL_RING_H
#define DCQL_RING_H
#include <stddef.h>
#include <string.h>

#ifndef DCQL_RING_ALLOC
#include <malloc.h>
#define DCQL_RING_ALLOC(n) ((unsigned char *)memalign(32, (n)))
#define DCQL_RING_FREE(p) free(p)
#endif
#ifndef DCQL_RING_LOCK
#include <arch/irq.h>
#define DCQL_RING_LOCK_T irq_mask_t
#define DCQL_RING_LOCK(m) ((m) = irq_disable())
#define DCQL_RING_UNLOCK(m) irq_restore(m)
#endif
#ifndef DCQL_RING_DGRAM
#define DCQL_RING_DGRAM 1200u   /* DCQL_NET_DGRAM */
#endif

typedef struct { unsigned char *buf; unsigned size; volatile unsigned head, tail; int owned; } ring_t;

/* ---- the bare operations: callers hold DCQL_RING_LOCK (the x-named wrappers below take it) */
/* new memory in a ring, emptied; returns the owned memory it replaced (NULL when there was none, or it was borrowed,
   or it is the same memory again) */
static inline unsigned char *ring_take(ring_t *r, unsigned char *buf, unsigned size, int owned) {
    unsigned char *const old = r->owned && r->buf && r->buf != buf ? r->buf : NULL;
    r->buf = buf; r->size = size; r->head = r->tail = 0; r->owned = owned && buf != NULL;
    return old;
}
static inline unsigned ring_used(const ring_t *r) { return r->head - r->tail; }
/* n bytes between p and the ring at counter c (positions c % size on, wrapping once at most) */
static inline void ring_copy_in(ring_t *r, unsigned c, const unsigned char *p, unsigned n) {
    const unsigned at = c % r->size, first = n < r->size - at ? n : r->size - at;
    memcpy(r->buf + at, p, first);
    if (n > first) memcpy(r->buf, p + first, n - first);
}
static inline void ring_copy_out(const ring_t *r, unsigned c, unsigned char *p, unsigned n) {
    const unsigned at = c % r->size, first = n < r->size - at ? n : r->size - at;
    memcpy(p, r->buf + at, first);
    if (n > first) memcpy(p + first, r->buf, n - first);
}
static inline int ring_put(ring_t *r, const void *p, unsigned n) {
    if (!r->buf || !p || !n || n > DCQL_RING_DGRAM || ring_used(r) + n + 2 > r->size) return 0;
    const unsigned char len[2] = {(unsigned char)n, (unsigned char)(n >> 8)};
    ring_copy_in(r, r->head, len, 2);
    ring_copy_in(r, r->head + 2, (const unsigned char *)p, n);
    __asm__ __volatile__("" ::: "memory");
    r->head += n + 2;
    return 1;
}
/* bytes; -1 empty; -2 retired whole because it did not fit */
static inline int ring_get(ring_t *r, void *p, unsigned max) {
    if (!r->buf || r->head == r->tail) return -1;
    unsigned char len[2];
    ring_copy_out(r, r->tail, len, 2);
    const unsigned n = len[0] | (unsigned)len[1] << 8;
    if (n <= max && p) ring_copy_out(r, r->tail + 2, (unsigned char *)p, n);
    __asm__ __volatile__("" ::: "memory");
    r->tail += n + 2;
    return n <= max && p ? (int)n : -2;
}
static inline void ring_clear(ring_t *r) { r->tail = r->head; }

/* ---- what threads call: each inside the exclusion */
static inline int ringx_put(ring_t *r, const void *p, unsigned n) {
    DCQL_RING_LOCK_T m; DCQL_RING_LOCK(m);
    const int ok = ring_put(r, p, n);
    DCQL_RING_UNLOCK(m);
    return ok;
}
static inline int ringx_get(ring_t *r, void *p, unsigned max) {
    DCQL_RING_LOCK_T m; DCQL_RING_LOCK(m);
    const int n = ring_get(r, p, max);
    DCQL_RING_UNLOCK(m);
    return n;
}
static inline void ringx_clear(ring_t *r) { DCQL_RING_LOCK_T m; DCQL_RING_LOCK(m); ring_clear(r); DCQL_RING_UNLOCK(m); }
static inline unsigned ringx_used(const ring_t *r) {
    DCQL_RING_LOCK_T m; DCQL_RING_LOCK(m);
    const unsigned u = ring_used(r);
    DCQL_RING_UNLOCK(m);
    return u;
}

/* the built-in rings for each ring with no memory: both or neither (1 = both rings have memory). The allocation runs
   outside the exclusion, the take inside it. */
static inline int rings_alloc(ring_t *rx, ring_t *tx, unsigned size) {
    unsigned char *a = NULL, *b = NULL;
    if (!rx->buf && !(a = DCQL_RING_ALLOC(size))) return 0;
    if (!tx->buf && !(b = DCQL_RING_ALLOC(size))) { if (a) DCQL_RING_FREE(a); return 0; }
    DCQL_RING_LOCK_T m; DCQL_RING_LOCK(m);
    if (a) (void)ring_take(rx, a, size, 1);
    if (b) (void)ring_take(tx, b, size, 1);
    DCQL_RING_UNLOCK(m);
    return 1;
}
/* the program's own memory in place of a ring's (a NULL buf leaves that ring as it is), inside the exclusion; the
   owned memory replaced goes to old[0] (rx) and old[1] (tx), NULL when none. When it returns no thread is inside the
   old memory and none can reach it: the caller frees old[] at once. */
static inline void rings_replace(ring_t *rx, unsigned char *rxm, unsigned rxn, ring_t *tx, unsigned char *txm,
                                 unsigned txn, unsigned char *old[2]) {
    DCQL_RING_LOCK_T m; DCQL_RING_LOCK(m);
    old[0] = rxm ? ring_take(rx, rxm, rxn, 0) : NULL;
    old[1] = txm ? ring_take(tx, txm, txn, 0) : NULL;
    DCQL_RING_UNLOCK(m);
}
#endif
