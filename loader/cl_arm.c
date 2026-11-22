/* cl_arm.c: arming the control loop's return stub (dc-qalink 0.2). MIT licence. Shared by the loader (loader.c) and
   any launcher that runs programs the same way (a game's own scene runner): find LOADER.BIN on the mounted disc (its
   GD sector, length and SHA-256, the sector arithmetic checked against KallistiOS's own read of the file), then before
   every jump copy the stub into the hand-off area with the way back and the BIOS's start-up registers. */
#include <kos.h>
#include <arch/cache.h>
#include <dc/cdrom.h>
#include <stdio.h>
#include <string.h>
#include <strings.h>
#include "cl_arm.h"

extern uint32_t _arch_old_sr, _arch_old_vbr, _arch_old_fpscr;
/* the return stub's bytes (stub.bin, placed here by stub_blob.S) */
extern const uint8_t cl_stub_blob[], cl_stub_blob_end[];

static uint32_t g_ret_lba, g_ret_len;
static uint8_t g_ret_sha[32];
static uint8_t g_sec[2048] __attribute__((aligned(32)));
static int find_loader_bin(void) {
    cd_toc_t toc;
    if (cdrom_read_toc(&toc, true) != ERR_OK && cdrom_read_toc(&toc, false) != ERR_OK) return -1;
    const uint32_t base = cdrom_locate_data_track(&toc);   /* the data track's first sector (TOC numbering) */
    if (!base) return -2;
    if (cdrom_read_sectors_ex(g_sec, base + 16, 1, true) != ERR_OK) return -3;   /* the volume descriptor */
    if (g_sec[0] != 1 || memcmp(g_sec + 1, "CD001", 5)) return -4;
    const uint32_t root = cl_rd32(g_sec + 156 + 2), root_len = cl_rd32(g_sec + 156 + 10);
    for (uint32_t s = 0; s < (root_len + 2047) / 2048 && s < 16; ++s) {
        if (cdrom_read_sectors_ex(g_sec, root + 150 + s, 1, true) != ERR_OK) return -5;   /* fs_iso9660: + 150 */
        for (uint32_t o = 0; o < 2048 && g_sec[o];) {
            const uint8_t *r = g_sec + o;
            const unsigned nl = r[32];
            if (nl >= 10 && !strncasecmp((const char *)r + 33, "LOADER.BIN", 10) && (nl == 10 || r[43] == ';')) {
                g_ret_lba = cl_rd32(r + 2) + 150;
                g_ret_len = cl_rd32(r + 10);
                return 0;
            }
            o += r[0];
        }
    }
    return -6;
}
/* the hash of the file as KallistiOS reads it; and the stub's sector read checked against it (its first sector) */
static int hash_loader_bin(void) {
    FILE *f = fopen("/cd/LOADER.BIN", "rb");
    if (!f) return -1;
    cl_sha256_t s;
    cl_sha256_init(&s);
    static uint8_t b[16384];
    size_t n, total = 0;
    uint8_t first[64];
    while ((n = fread(b, 1, sizeof(b), f)) > 0) {
        if (!total) memcpy(first, b, n < 64 ? n : 64);
        cl_sha256_update(&s, b, n);
        total += n;
    }
    fclose(f);
    cl_sha256_final(&s, g_ret_sha);
    if (total != g_ret_len) return -2;
    if (cdrom_read_sectors_ex(g_sec, g_ret_lba, 1, true) != ERR_OK || memcmp(g_sec, first, 64)) return -3;
    return 0;
}
void cl_arm_stub(cl_handoff_t *A) {
    const uint32_t n = (uint32_t)(cl_stub_blob_end - cl_stub_blob);
    cl_handoff_hdr_t *h = &A->h;
    if (!g_ret_lba || n == 0 || n > CL_STUB_MAX - 1024) {
        h->stub_bytes = 0;
        h->ret_lba = 0;
    } else {
        memcpy(A->stub, cl_stub_blob, n);
        h->stub_bytes = n;
        h->stub_crc = cl_crc32(0, A->stub, n);
        h->ret_lba = g_ret_lba;
        h->loader_len = g_ret_len;
        memcpy(h->ret_sha, g_ret_sha, 32);
    }
    h->reserved[CL_RSV_BIOS_SR] = _arch_old_sr;
    h->reserved[CL_RSV_BIOS_VBR] = _arch_old_vbr;
    h->reserved[CL_RSV_BIOS_FPSCR] = _arch_old_fpscr;
    cl_handoff_seal(A);
    dcache_wback_range((uintptr_t)A, CL_REPORT_OFF);
}


int cl_arm_find(void) {
    const int r = find_loader_bin();
    const int h = r ? r : hash_loader_bin();
    if (h) g_ret_lba = 0;
    return h;
}
const uint8_t *cl_arm_sha(void) { return g_ret_sha; }
uint32_t cl_arm_lba(void) { return g_ret_lba; }
uint32_t cl_arm_len(void) { return g_ret_len; }
uint32_t cl_arm_stub_bytes(void) { return (uint32_t)(cl_stub_blob_end - cl_stub_blob); }
