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

/* ---- the picture's CRC over exactly the bytes sent (lane ql-fix-dc round 3)
   A grab reads the frame buffer on screen while the program keeps drawing (a moving game; a held game that still
   renders; the in-game menu over it), so a CRC taken before sending did not match what was sent: every copy failed it.
   Now each block's CRC is taken from the copy as it is sent and kept (one word a block, the caller's table), and the
   END carries the CRC of the picture the PC holds: every block as last sent (the PC keeps the latest copy of a block),
   joined with the zlib crc32_combine method. The heads carry CRC 0, which tells the PC to take the END's (bridge and
   CLI). A picture that changes during the grab comes back whole and with a matching CRC; it may mix rows of
   neighbouring frames. No frame copy (a full frame is about 600 KB). */
static inline uint32_t cl_still_crc(uint32_t crc, const uint8_t *p, uint32_t n) {   /* zlib's crc32, a nibble table */
    static const uint32_t t[16] = {0x00000000u, 0x1db71064u, 0x3b6e20c8u, 0x26d930acu, 0x76dc4190u, 0x6b6b51f4u,
                                   0x4db26158u, 0x5005713cu, 0xedb88320u, 0xf00f9344u, 0xd6d6a3e8u, 0xcb61b38cu,
                                   0x9b64c2b0u, 0x86d3d2d4u, 0xa00ae278u, 0xbdbdf21cu};
    crc = ~crc;
    while (n--) {
        crc ^= *p++;
        crc = (crc >> 4) ^ t[crc & 15];
        crc = (crc >> 4) ^ t[crc & 15];
    }
    return ~crc;
}
static inline uint32_t cl_gf2_times(const uint32_t *m, uint32_t v) {
    uint32_t s = 0;
    for (; v; v >>= 1, ++m)
        if (v & 1) s ^= *m;
    return s;
}
static inline void cl_gf2_square(uint32_t *sq, const uint32_t *m) {
    for (int n = 0; n < 32; ++n) sq[n] = cl_gf2_times(m, m[n]);
}
/* op: the operator that moves a CRC past len zero bytes, so crc(A B) = cl_gf2_times(op(len B), crc(A)) ^ crc(B) */
static inline void cl_crc_zeros_op(uint32_t op[32], uint32_t len) {
    uint32_t odd[32], even[32], tmp[32];
    odd[0] = 0xedb88320u;   /* one zero bit */
    for (int n = 1; n < 32; ++n) odd[n] = 1u << (n - 1);
    cl_gf2_square(even, odd);   /* two zero bits */
    cl_gf2_square(odd, even);   /* four */
    for (int n = 0; n < 32; ++n) op[n] = 1u << n;
    while (len) {
        cl_gf2_square(even, odd);   /* one byte, then 4, 16, ... */
        if (len & 1) { for (int n = 0; n < 32; ++n) tmp[n] = cl_gf2_times(even, op[n]); memcpy(op, tmp, sizeof(tmp)); }
        if (!(len >>= 1)) break;
        cl_gf2_square(odd, even);   /* two bytes, then 8, 32, ... */
        if (len & 1) { for (int n = 0; n < 32; ++n) tmp[n] = cl_gf2_times(odd, op[n]); memcpy(op, tmp, sizeof(tmp)); }
        len >>= 1;
    }
}
/* the CRC of the whole picture from its blocks' CRCs (each CL_CHUNK bytes, the last `bytes` - its offset) */
static inline uint32_t cl_still_crc_join(const uint32_t *crcs, uint32_t blocks, uint32_t bytes) {
    uint32_t op[32], crc = 0;
    cl_crc_zeros_op(op, CL_CHUNK);
    for (uint32_t k = 0; k < blocks; ++k) {
        const uint32_t n = bytes - k * CL_CHUNK < CL_CHUNK ? bytes - k * CL_CHUNK : CL_CHUNK;
        if (n != CL_CHUNK) cl_crc_zeros_op(op, n);
        crc = cl_gf2_times(op, crc) ^ crcs[k];
    }
    return crc;
}

/* one still: 0 when the PC holds it whole, -2 when it did not within CL_STILL_ROUNDS. bits: the PC's bitmap of held
   blocks (cleared here, filled by wait_ack). crcs: one word a block, the CRC of each block as last sent (round 3); NULL
   keeps the older way, the CRC in the head as the caller computed it before sending (a picture that cannot change).
   head: the 32-byte cl_still_head_t, its CRC at byte 24. c: what was sent (added to). */
static inline int cl_still_rounds(const cl_still_io_t *io, uint8_t *bits, unsigned bits_size, uint32_t *crcs,
                                  const uint8_t *src, uint32_t row, uint32_t stride, uint32_t bytes, uint16_t index,
                                  const void *head, uint16_t head_len, cl_still_count_t *c) {
    const uint32_t blocks = (bytes + CL_CHUNK - 1) / CL_CHUNK;
    if (blocks > bits_size * 8u || head_len > 64 || (crcs && head_len < 28)) return -1;
    uint8_t blk[CL_CHUNK], hd[64], he[64];
    memcpy(hd, head, head_len);
    memcpy(he, head, head_len);
    if (crcs) memset(hd + 24, 0, 4);   /* CRC 0 in the heads: the PC takes the END's */
    memset(bits, 0, bits_size);
    for (unsigned round = 0; round < CL_STILL_ROUNDS; ++round) {
        ++c->rounds;
        io->send(io->ctx, CL_STILL_HEAD, index, hd, head_len);   /* every round: a lost head costs one round */
        ++c->heads;
        for (uint32_t k = 0; k < blocks; ++k)
            if (!(bits[k >> 3] & (1u << (k & 7)))) {
                const uint32_t off = k * CL_CHUNK, n = bytes - off < CL_CHUNK ? bytes - off : CL_CHUNK;
                cl_still_bytes(src, row, stride, off, n, blk);
                if (crcs) crcs[k] = cl_still_crc(0, blk, n);
                io->send(io->ctx, off, index, blk, (uint16_t)n);
                ++c->blocks;
            }
        if (crcs) {
            const uint32_t crc = cl_still_crc_join(crcs, blocks, bytes);
            memcpy(he + 24, &crc, 4);   /* little-endian, as the head */
        }
        int missing = -1;
        for (unsigned t = 0; t < CL_STILL_END_TRIES && missing < 0; ++t) {
            if (t) {   /* an END the PC cannot place (its head lost) is never answered: the head first */
                io->send(io->ctx, CL_STILL_HEAD, index, hd, head_len);
                ++c->heads;
            }
            io->send(io->ctx, CL_STILL_END, index, he, head_len);
            ++c->ends;
            missing = io->wait_ack(io->ctx, CL_STILL_END_WAIT_MS);
        }
        if (missing >= 0) ++c->acks;
        if (missing == 0) return 0;
    }
    return -2;
}
#endif
