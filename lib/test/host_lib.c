/* host_lib.c: host test of the console library's pure rules (lane ql-fix-dc, 2026-11-23; audit items 2 and 4):
 *   dirent  lib/dcql_dirent.h: a truncated /cd directory entry is rejected and the read position never passes the
 *           listing's size;
 *   rings   lib/dcql_ring.h: built-in rings both-or-neither (a failed second allocation frees the first), a change to
 *           the program's own rings frees the built-in ones it replaces and never frees borrowed memory, and the
 *           datagram ring itself (put, get, the retire of an entry too big for the reader).
 * The same checks run against copies of the code at 1990fb9 with --control and must fail there (each names what it
 * caught). Built with AddressSanitizer and its leak checker: lib/test/run.sh. MIT licence. */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ---- a counting allocator with a planted failure, for dcql_ring.h */
static int g_live, g_calls, g_fail_at = -1;
static unsigned char *t_alloc(unsigned n) {
    if (g_calls++ == g_fail_at) return NULL;
    unsigned char *p = malloc(n);
    if (p) ++g_live;
    return p;
}
static void t_free(void *p) { if (p) { --g_live; free(p); } }
#define DCQL_RING_ALLOC(n) t_alloc(n)
#define DCQL_RING_FREE(p) t_free(p)
#define DCQL_RING_LOCK_T int   /* single-threaded here; host_race.c tests the exclusion */
#define DCQL_RING_LOCK(m) ((m) = 0)
#define DCQL_RING_UNLOCK(m) ((void)(m))
#include "../dcql_ring.h"
#include "../dcql_dirent.h"

static int g_bad;
#define CHECK(c, what) do { if (!(c)) { printf("FAIL %s\n", what); ++g_bad; } else printf("ok   %s\n", what); } while (0)

/* ================================================================== the code at 1990fb9, for --control */
/* lib/dcql_disc.c d_readdir: keep = min(n, cap); read keep bytes (short reads clamp at the size); pos += n - keep */
static int old_dirent_take(uint32_t pos, uint32_t size, uint32_t n, uint32_t cap, uint32_t *keep, uint32_t *skip) {
    *keep = n < cap ? n : cap;
    if (*keep > (size > pos ? size - pos : 0)) return -1;   /* the name read came back short */
    *skip = n - *keep;
    return 0;
}
/* lib/dcql_net.c rings_ready and dcql_net_config's ring lines */
static int old_rings_alloc(ring_t *rx, ring_t *tx, unsigned size) {
    if (!rx->buf) { rx->buf = DCQL_RING_ALLOC(size); rx->size = size; rx->head = rx->tail = 0; }
    if (!tx->buf) { tx->buf = DCQL_RING_ALLOC(size); tx->size = size; tx->head = tx->tail = 0; }
    return rx->buf && tx->buf;
}
static unsigned char *old_ring_take(ring_t *r, unsigned char *buf, unsigned size, int owned) {
    (void)owned;
    r->buf = buf; r->size = size; r->head = r->tail = 0;
    return NULL;
}

typedef int (*take_fn)(uint32_t, uint32_t, uint32_t, uint32_t, uint32_t *, uint32_t *);
typedef int (*alloc_fn)(ring_t *, ring_t *, unsigned);
typedef unsigned char *(*swap_fn)(ring_t *, unsigned char *, unsigned, int);

/* walk a listing as d_readdir does: head, then the name's keep bytes, then skip; returns the entries accepted */
static int walk(take_fn take, const uint8_t *l, uint32_t size, uint32_t cap, uint32_t *end_pos, char last[64]) {
    uint32_t pos = 0;
    int got = 0;
    for (;;) {
        if (pos > size || size - pos < DCQL_DIRENT_HEAD) break;
        const uint32_t n = (uint32_t)l[pos + 4] | (uint32_t)l[pos + 5] << 8;
        pos += DCQL_DIRENT_HEAD;
        uint32_t keep, skip;
        if (take(pos, size, n, cap, &keep, &skip)) { pos = size; break; }
        memcpy(last, l + pos, keep < 63 ? keep : 63);
        last[keep < 63 ? keep : 63] = 0;
        pos += keep + skip;
        ++got;
    }
    *end_pos = pos;
    return got;
}
static uint32_t put_entry(uint8_t *l, uint32_t at, uint32_t size, const char *name, uint32_t declared) {
    l[at] = (uint8_t)size; l[at + 1] = (uint8_t)(size >> 8); l[at + 2] = (uint8_t)(size >> 16); l[at + 3] = (uint8_t)(size >> 24);
    l[at + 4] = (uint8_t)declared; l[at + 5] = (uint8_t)(declared >> 8);
    memcpy(l + at + 6, name, strlen(name));
    return at + 6 + (uint32_t)strlen(name);
}

static void test_dirent(take_fn take) {
    uint8_t *l = malloc(64);   /* exact size: AddressSanitizer catches a read past the listing */
    uint32_t at = put_entry(l, 0, 100, "A.BIN", 5);
    at = put_entry(l, at, 0xffffffffu, "DIR", 3);
    const uint32_t whole = at;
    uint32_t end;
    char last[64];
    CHECK(walk(take, l, whole, 31, &end, last) == 2 && end == whole, "dirent: two whole entries read, position at the end");
    /* a truncated last entry: it declares 40 name bytes and the listing holds 4 of them */
    at = put_entry(l, whole, 7, "LONG", 40);
    int got = walk(take, l, at, 31, &end, last);
    CHECK(got == 2, "dirent: a truncated entry (40 bytes declared, 4 present) is rejected");
    CHECK(end <= at, "dirent: the read position never passes the listing's size (truncated name)");
    /* a name longer than the kept part, truncated in the skipped part: 2 kept of 30 declared, 10 present */
    uint8_t *m = malloc(6 + 10);
    put_entry(m, 0, 1, "ABCDEFGHIJ", 30);
    got = walk(take, m, 16, 2, &end, last);
    CHECK(got == 0 && end <= 16, "dirent: a truncated entry is rejected even when its kept part is present");
    /* a long name inside the listing: kept part copied, the rest skipped, next entry still read */
    uint8_t *k = malloc(6 + 40 + 6 + 1);
    uint32_t p = put_entry(k, 0, 1, "0123456789012345678901234567890123456789", 40);
    p = put_entry(k, p, 2, "Z", 1);
    got = walk(take, k, p, 31, &end, last);
    CHECK(got == 2 && end == p && !strcmp(last, "Z"), "dirent: a long whole name is cut to the kept part and skipped");
    free(l); free(m); free(k);
}

static void test_rings(alloc_fn alloc, swap_fn swap) {
    ring_t rx = {0}, tx = {0};
    /* partial allocation: the second ring's allocation fails */
    g_live = 0; g_calls = 0; g_fail_at = 1;
    const int ok = alloc(&rx, &tx, 4096);
    CHECK(!ok, "rings: start fails when the second built-in ring cannot be allocated");
    CHECK(g_live == 0, "rings: a failed start keeps no built-in ring (the first is freed)");
    for (ring_t *r = &rx; r; r = r == &rx ? &tx : NULL)   /* tidy a control's leftover so the next check stands alone */
        if (r->buf) { t_free(r->buf); r->buf = NULL; }
    g_live = 0; g_calls = 0; g_fail_at = -1;
    CHECK(alloc(&rx, &tx, 4096) && g_live == 2, "rings: both built-in rings allocated");
    /* reconfiguration with the program's own rings: the built-in ones it replaces are freed */
    static unsigned char mine_rx[4096], mine_tx[4096], other[4096];
    unsigned char *o1 = swap(&rx, mine_rx, sizeof(mine_rx), 0);
    unsigned char *o2 = swap(&tx, mine_tx, sizeof(mine_tx), 0);
    t_free(o1); t_free(o2);
    CHECK(g_live == 0, "rings: the program's own rings free the built-in ones they replace");
    /* borrowed memory replaced by borrowed memory: nothing to free */
    unsigned char *o3 = swap(&rx, other, sizeof(other), 0);
    CHECK(o3 == NULL, "rings: borrowed memory is never handed back to be freed");
    /* a later start with rings supplied allocates nothing */
    g_calls = 0;
    CHECK(alloc(&rx, &tx, 4096) && g_calls == 0 && g_live == 0, "rings: supplied rings: no built-in allocation");
    /* the ring itself */
    unsigned char b[2048];
    memset(b, 7, sizeof(b));
    int put = 0;
    while (ring_put(&rx, b, 1200)) ++put;
    CHECK(put == 3, "ring: three 1,200 B datagrams fit a 4,096 B ring (with their length words)");
    unsigned char o[1200];
    CHECK(ring_get(&rx, o, 100) == -2, "ring: a datagram too big for the reader is retired whole");
    CHECK(ring_get(&rx, o, sizeof(o)) == 1200 && o[0] == 7, "ring: the next datagram reads whole");
    CHECK(!ring_put(&rx, b, 1201), "ring: a datagram over 1,200 B is refused");
}

int main(int argc, char **argv) {
    setvbuf(stdout, NULL, _IONBF, 0);   /* the leak checker ends a control without flushing */
    const int control = argc > 1 && !strcmp(argv[1], "--control");
    printf("host_lib: %s\n", control ? "CONTROL: the code at 1990fb9 (must fail)" : "this head's lib/dcql_ring.h and lib/dcql_dirent.h");
    test_dirent(control ? old_dirent_take : dcql_dirent_take);
    test_rings(control ? old_rings_alloc : rings_alloc, control ? old_ring_take : ring_take);
    printf("host_lib: %d failed\n", g_bad);
    return g_bad ? 1 : 0;
}
