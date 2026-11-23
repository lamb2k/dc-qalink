/* cl_still.h: a still's rounds to the PC (dc-qalink 0.2, ql-core6). MIT licence. Header only and freestanding: the
   client library (loader/client.c) sends with it on the console, and tests/test_still.py runs the same code on a PC
   against the PC's own still receivers (pc/dcql_loop.py StillSink, pc/bridge/core.py Stills) over a link that loses
   chosen datagrams.

   The wire (include/dcql_loop.h): CL_STILL with a = CL_STILL_HEAD (the 32-byte head), a byte offset (one block), or
   CL_STILL_END (the head again); the PC answers an END with STILLACK (blocks it still misses, and its bitmap). The PC
   keeps nothing for a still it has no head for: its blocks and ENDs are dropped and never answered.

   Until ql-core6 the head went out once, before the first round. On the console (first-run9 row 4, 2026-11-22) one
   head was lost: the PC dropped all 600 blocks of every round and never answered, and the client sent the whole
   picture 12 times with 10 ENDs of 300 ms each between them: "screen 1: NOT HELD in 43483 ms", END 45 s late.
   Now the head goes out at the start of every round and again before every END after the first that went
   unanswered, so a lost head costs one END wait (300 ms) and one round, not the still. */
#ifndef CL_STILL_H
#define CL_STILL_H
#include <stdint.h>
#include <string.h>
#include "dcql_loop.h"

#define CL_STILL_ROUNDS 12        /* rounds of the missing blocks */
#define CL_STILL_END_TRIES 10     /* ENDs a round waits through for an answer */
#define CL_STILL_END_WAIT_MS 300  /* one END's wait */

typedef struct {
    void *ctx;
    /* one CL_STILL datagram (a = offset, CL_STILL_HEAD or CL_STILL_END; b = the still's index), queued for sending */
    void (*send)(void *ctx, uint32_t a, uint16_t index, const void *p, uint16_t n);
    /* waits up to ms for this still's STILLACK: returns the blocks the PC still misses (its bitmap already copied into
       the caller's bits), or -1 when no answer came */
    int (*wait_ack)(void *ctx, unsigned ms);
} cl_still_io_t;

typedef struct { uint32_t rounds, heads, blocks, ends, acks; } cl_still_count_t;

/* the bytes [off, off + n) of the packed picture (rows of `row` bytes), read from the caller's rows in place */
static inline void cl_still_bytes(const uint8_t *pix, uint32_t row, uint32_t stride, uint32_t off, uint32_t n,
                                  uint8_t *out) {
    while (n) {
        const uint32_t y = off / row, x = off % row, k = row - x < n ? row - x : n;
        memcpy(out, pix + y * stride + x, k);
        out += k; off += k; n -= k;
    }
}

/* one still: 0 when the PC holds it whole, -2 when it did not within CL_STILL_ROUNDS. bits: the PC's bitmap of held
   blocks (cleared here, filled by wait_ack). c: what was sent (added to). */
static inline int cl_still_rounds(const cl_still_io_t *io, uint8_t *bits, unsigned bits_size, const uint8_t *src,
                                  uint32_t row, uint32_t stride, uint32_t bytes, uint16_t index, const void *head,
                                  uint16_t head_len, cl_still_count_t *c) {
    const uint32_t blocks = (bytes + CL_CHUNK - 1) / CL_CHUNK;
    if (blocks > bits_size * 8u) return -1;
    uint8_t blk[CL_CHUNK];
    memset(bits, 0, bits_size);
    for (unsigned round = 0; round < CL_STILL_ROUNDS; ++round) {
        ++c->rounds;
        io->send(io->ctx, CL_STILL_HEAD, index, head, head_len);   /* every round: a lost head costs one round */
        ++c->heads;
        for (uint32_t k = 0; k < blocks; ++k)
            if (!(bits[k >> 3] & (1u << (k & 7)))) {
                const uint32_t off = k * CL_CHUNK, n = bytes - off < CL_CHUNK ? bytes - off : CL_CHUNK;
                cl_still_bytes(src, row, stride, off, n, blk);
                io->send(io->ctx, off, index, blk, (uint16_t)n);
                ++c->blocks;
            }
        int missing = -1;
        for (unsigned t = 0; t < CL_STILL_END_TRIES && missing < 0; ++t) {
            if (t) {   /* an END the PC cannot place (its head lost) is never answered: the head first */
                io->send(io->ctx, CL_STILL_HEAD, index, head, head_len);
                ++c->heads;
            }
            io->send(io->ctx, CL_STILL_END, index, head, head_len);
            ++c->ends;
            missing = io->wait_ack(io->ctx, CL_STILL_END_WAIT_MS);
        }
        if (missing >= 0) ++c->acks;
        if (missing == 0) return 0;
    }
    return -2;
}
#endif
