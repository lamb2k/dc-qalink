/* dqa.h: the live-asset file the PC sends to the console viewer (dc-qalink 0.2, docs/ASSETS.md). MIT licence.
   Little-endian, every offset from the start of the file, every block 32-byte aligned. Written by pc/assets/dqa.py;
   the texture data is already in the graphics chip's own form (twiddled, VQ, palettes, mip chain), so the console
   copies it into graphics memory as it is: no conversion on the console. */
#ifndef DQA_H
#define DQA_H
#include <stdint.h>

#define DQA_MAGIC 0x31415144u          /* "DQA1" */
#define DQA_VERSION 1
#define DQA_KIND_TEXTURE 1
#define DQA_KIND_MODEL 2
#define DQA_NO_TEX 0xffffu
#define DQA_STRIP_MAX 1024u             /* vertices in one strip (the converter cuts longer ones) */

typedef struct {
    uint32_t magic;                     /* DQA_MAGIC */
    uint16_t kind, version;
    uint32_t bytes;                     /* the whole file */
    uint32_t crc32;                     /* zlib's CRC-32 of bytes [16, bytes) */
    uint8_t src_sha[16];                /* the first 16 bytes of the source file's SHA-256 */
    uint32_t ntex, nstrips, nverts;
    uint32_t tex_off, strip_off, vert_off;
    float centre[3], scale;             /* the normalisation the converter applied (information only) */
    uint32_t flags;                     /* bit 0: some texture has alpha */
    uint32_t pal_fmt;                   /* PVR_PAL_* for every palette in the file, or 0xffffffff for none */
} dqa_head_t;                           /* 80 bytes */

typedef struct {
    uint32_t txrfmt;                    /* PVR_TXRFMT_* bits as the polygon header takes them (palette bank included) */
    uint16_t w, h;
    uint32_t data_off, data_bytes;      /* exactly what goes into graphics memory */
    uint32_t pal_off;                   /* u32 entries already in pal_fmt */
    uint16_t pal_n, pal_base;           /* entries, first palette entry (0..1023) */
    uint8_t mip, alpha;                 /* alpha: 0 none, 1 one bit, 2 full */
    uint16_t reserved0;
    uint32_t reserved1;
} dqa_tex_t;                            /* 32 bytes */

typedef struct {
    uint32_t first;                     /* first vertex */
    uint16_t n, tex;                    /* vertices; texture index or DQA_NO_TEX */
    uint32_t flags, reserved;
} dqa_strip_t;                          /* 16 bytes */

typedef struct {
    float x, y, z;
    int8_t nx, ny, nz, pad;             /* unit normal times 127 */
    float u, v;
    uint32_t argb;
    uint32_t reserved;
} dqa_vert_t;                           /* 32 bytes */
#endif
