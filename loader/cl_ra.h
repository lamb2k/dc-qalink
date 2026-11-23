/* cl_ra.h: the /pc read-ahead block (loader/client.c; lane ql-fix-dc round 2, 2026-11-23, audit item 2). MIT licence.
 * Plain C with no KallistiOS calls of its own, so the host test (lib/test/host_race.c) builds the code the console runs.
 *
 * One block of CL_FREAD_MAX bytes serves a C library's small buffered reads: one round trip to the PC per block, not
 * per call. Every use of it is serialized by one lock (CL_RA_LOCK; a KallistiOS mutex on the console): the check of
 * what the block holds (file, handle, offset, length), the fetch into it, the copy out of it, a close forgetting it,
 * and its allocation and free. A read holds the lock from its check to its copy, so no other read can refill the block
 * under it, and the block is freed only by whoever holds the lock: the last close, or the way out (cl_ra_release),
 * which waits at most wait_ms for a read under way and otherwise leaves the block to the program image's end. After
 * the way out begins (closed), reads go to the PC unbuffered. */
#ifndef CL_RA_H
#define CL_RA_H
#include <stdint.h>
#include <string.h>

#ifndef CL_RA_LOCK
#include <kos/mutex.h>
#define CL_RA_LOCK_T mutex_t
#define CL_RA_LOCK_INIT MUTEX_INITIALIZER
#define CL_RA_LOCK(l) mutex_lock(l)
#define CL_RA_LOCK_MS(l, ms) mutex_lock_timed((l), (ms))   /* 0 when taken */
#define CL_RA_UNLOCK(l) mutex_unlock(l)
#endif
#ifndef CL_RA_ALLOC
#include <malloc.h>
#define CL_RA_ALLOC(n) ((uint8_t *)memalign(32, (n)))
#define CL_RA_FREE(p) free(p)
#endif

typedef struct {
    CL_RA_LOCK_T lock;
    uint8_t *buf;                     /* NULL: none (no file open, no memory, or freed on the way out) */
    unsigned size;
    const void *file;                 /* what the block holds: NULL nothing */
    uint32_t handle, off, len;
    int closed;                       /* the way out has begun: no new block, reads unbuffered */
} cl_ra_t;
#define CL_RA_INIT(sz) {CL_RA_LOCK_INIT, NULL, (sz), NULL, 0, 0, 0, 0}

/* the PC's bytes [pos, pos + n) of the file with this handle into dst; 0 when they came */
typedef int (*cl_ra_fetch_t)(void *ctx, uint32_t handle, uint32_t pos, uint8_t *dst, uint32_t n);

/* a file opened: the block, when there is none and the way out has not begun (no memory: reads go unbuffered) */
static inline void cl_ra_open(cl_ra_t *ra) {
    CL_RA_LOCK(&ra->lock);
    if (!ra->buf && !__atomic_load_n(&ra->closed, __ATOMIC_RELAXED)) ra->buf = CL_RA_ALLOC(ra->size);
    CL_RA_UNLOCK(&ra->lock);
}
/* a file closed: the block no longer holds its bytes */
static inline void cl_ra_forget(cl_ra_t *ra, const void *file) {
    CL_RA_LOCK(&ra->lock);
    if (ra->file == file) ra->file = NULL;
    CL_RA_UNLOCK(&ra->lock);
}
/* cnt bytes (cnt < size) at pos of a file of fsize bytes through the block: cnt; -1 the fetch failed; -2 no block (the
   caller reads unbuffered). *fetched: the bytes asked of the PC (0 on a hit). */
static inline int cl_ra_read(cl_ra_t *ra, const void *file, uint32_t handle, uint32_t pos, uint32_t fsize, void *dst,
                             uint32_t cnt, cl_ra_fetch_t fetch, void *ctx, uint32_t *fetched) {
    *fetched = 0;
    CL_RA_LOCK(&ra->lock);
    if (!ra->buf || __atomic_load_n(&ra->closed, __ATOMIC_RELAXED)) { CL_RA_UNLOCK(&ra->lock); return -2; }
    if (!(ra->file == file && ra->handle == handle && pos >= ra->off && pos + cnt <= ra->off + ra->len)) {
        const uint32_t n = fsize - pos < ra->size ? fsize - pos : ra->size;
        ra->file = NULL;
        if (fetch(ctx, handle, pos, ra->buf, n)) { CL_RA_UNLOCK(&ra->lock); return -1; }
        ra->file = file; ra->handle = handle; ra->off = pos; ra->len = n;
        *fetched = n;
    }
    memcpy(dst, ra->buf + (pos - ra->off), cnt);
    CL_RA_UNLOCK(&ra->lock);
    return (int)cnt;
}
/* the last file closed: the block freed (1), or there was none (0) */
static inline int cl_ra_free(cl_ra_t *ra) {
    CL_RA_LOCK(&ra->lock);
    uint8_t *const p = ra->buf;
    ra->buf = NULL; ra->file = NULL;
    CL_RA_UNLOCK(&ra->lock);
    if (p) CL_RA_FREE(p);
    return p ? 1 : 0;
}
/* the way out: no new block from now on; the block freed (1), none held (0), or kept because a read still held it
   after wait_ms (-1: the program image's end reclaims it) */
static inline int cl_ra_release(cl_ra_t *ra, unsigned wait_ms) {
    __atomic_store_n(&ra->closed, 1, __ATOMIC_RELAXED);   /* reads that start now go unbuffered */
    if (CL_RA_LOCK_MS(&ra->lock, wait_ms)) return -1;   /* wait_ms > 0: KallistiOS reads 0 as no limit */
    uint8_t *const p = ra->buf;
    ra->buf = NULL; ra->file = NULL;
    CL_RA_UNLOCK(&ra->lock);
    if (p) CL_RA_FREE(p);
    return p ? 1 : 0;
}
#endif
