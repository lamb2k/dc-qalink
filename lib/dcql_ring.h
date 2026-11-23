/* dcql_ring.h: the link layer's datagram rings and who owns their memory (lib/dcql_net.c; lane ql-fix-dc, 2026-11-23,
 * audit item 4). MIT licence. Plain C with no KallistiOS calls, so the host test (lib/test/host_lib.c) builds the same
 * code the console runs.
 *
 * A ring is single-producer single-consumer: [u16 length][bytes] per datagram. Its memory is either
 *   owned:    the built-in rings, allocated by the link layer (DCQL_RING_ALLOC) when the program supplied none, or
 *   borrowed: the program's own memory from dcql_net_config(); never freed here.
 * ring_take() puts new memory in a ring and hands back the owned memory it replaced, for the caller to free once no
 * thread can touch it any more (dcql_net.c frees it after the swap, with interrupts on). rings_alloc() gives both rings
 * built-in memory or neither: when the second allocation fails, the first is freed before it returns 0. */
#ifndef DCQL_RING_H
#define DCQL_RING_H
#include <stddef.h>

#ifndef DCQL_RING_ALLOC
#include <malloc.h>
#define DCQL_RING_ALLOC(n) ((unsigned char *)memalign(32, (n)))
#define DCQL_RING_FREE(p) free(p)
#endif
#ifndef DCQL_RING_DGRAM
#define DCQL_RING_DGRAM 1200u   /* DCQL_NET_DGRAM */
#endif

typedef struct { unsigned char *buf; unsigned size; volatile unsigned head, tail; int owned; } ring_t;

/* new memory in a ring, emptied; returns the owned memory it replaced (NULL when there was none, or it was borrowed,
   or it is the same memory again) */
static inline unsigned char *ring_take(ring_t *r, unsigned char *buf, unsigned size, int owned) {
    unsigned char *const old = r->owned && r->buf && r->buf != buf ? r->buf : NULL;
    r->buf = buf; r->size = size; r->head = r->tail = 0; r->owned = owned && buf != NULL;
    return old;
}
/* the built-in rings for each ring with no memory: both or neither (1 = both rings have memory) */
static inline int rings_alloc(ring_t *rx, ring_t *tx, unsigned size) {
    unsigned char *a = NULL, *b = NULL;
    if (!rx->buf && !(a = DCQL_RING_ALLOC(size))) return 0;
    if (!tx->buf && !(b = DCQL_RING_ALLOC(size))) { if (a) DCQL_RING_FREE(a); return 0; }
    if (a) (void)ring_take(rx, a, size, 1);
    if (b) (void)ring_take(tx, b, size, 1);
    return 1;
}
static inline unsigned ring_used(const ring_t *r) { return r->head - r->tail; }
static inline int ring_put(ring_t *r, const void *p, unsigned n) {
    if (!r->buf || !p || !n || n > DCQL_RING_DGRAM || ring_used(r) + n + 2 > r->size) return 0;
    unsigned h = r->head;
    r->buf[h++ % r->size] = (unsigned char)n;
    r->buf[h++ % r->size] = (unsigned char)(n >> 8);
    for (unsigned i = 0; i < n; ++i) r->buf[h++ % r->size] = ((const unsigned char *)p)[i];
    __asm__ __volatile__("" ::: "memory");
    r->head = h;
    return 1;
}
/* bytes; -1 empty; -2 retired whole because it did not fit */
static inline int ring_get(ring_t *r, void *p, unsigned max) {
    if (r->head == r->tail) return -1;
    unsigned t = r->tail;
    unsigned n = r->buf[t++ % r->size];
    n |= (unsigned)r->buf[t++ % r->size] << 8;
    if (n > max || !p) { __asm__ __volatile__("" ::: "memory"); r->tail = t + n; return -2; }
    for (unsigned i = 0; i < n; ++i, ++t) ((unsigned char *)p)[i] = r->buf[t % r->size];
    __asm__ __volatile__("" ::: "memory");
    r->tail = t;
    return (int)n;
}
static inline void ring_clear(ring_t *r) { r->tail = r->head; }
#endif
