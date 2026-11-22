/* dcql_hw.h: the few Dreamcast hardware steps dc-qalink's way back needs with nothing else running (dc-qalink 0.2).
 * MIT licence. Header only and freestanding (stdint.h alone): the return stub (loader/stub.c, no C library, no
 * KallistiOS), the client library (loader/client.c, lib/dcql_return.c: from an exception handler) and the loader use
 * the same code. Every wait is bounded: a missing device or an emulator that does not answer costs at most a fraction
 * of a second and changes nothing.
 *
 * GDEMU image select (the "disc switch"). openMenu chooses the image GDEMU serves with a vendor packet command on the
 * GD-ROM's ATA interface (openMenu backend/gdemu_sdk.c, by megavolt85: packet 0x52 0x82 <image low> <image high>;
 * 0x52 0x00 answers 8 bytes of firmware version), then starts the disc. On a real GD-ROM drive, an optical drive
 * emulator other than GDEMU, or Flycast, 0x52 is not a command: the drive answers with an error (or not at all) and
 * nothing changes.
 *
 * Screen colour (the stub has no text): the border colour register, and the video output's blank bit, which shows the
 * border colour over the whole picture (KallistiOS vid_set_enabled). Readable from across a room or on a photo.
 */
#ifndef DCQL_HW_H
#define DCQL_HW_H
#include <stdint.h>

#define DCQL_HW_R32(a) (*(volatile uint32_t *)(a))
#define DCQL_HW_R16(a) (*(volatile uint16_t *)(a))
#define DCQL_HW_R8(a) (*(volatile uint8_t *)(a))

/* ---------------------------------------------------------------- the GD-ROM's ATA registers (G1 bus, P2) */
#define DCQL_ATA_ALTSTATUS 0xa05f7018u
#define DCQL_ATA_DATA 0xa05f7080u
#define DCQL_ATA_FEATURES 0xa05f7084u
#define DCQL_ATA_LBA_MID 0xa05f7090u
#define DCQL_ATA_LBA_HIGH 0xa05f7094u
#define DCQL_ATA_STATUS 0xa05f709cu
#define DCQL_ATA_COMMAND 0xa05f709cu
#define DCQL_EXT_INT_STAT 0xa05f6904u   /* bit 0: the GD-ROM's interrupt line (seen even when masked) */
#define DCQL_ATA_BSY 0x80u
#define DCQL_ATA_DRQ 0x08u
#define DCQL_ATA_ERR 0x01u
#define DCQL_GD_SPINS 3000000u        /* about 0.3 to 1 s of G1 register reads */

/* 0 when (ALTSTATUS & mask) == want within the bound */
static inline int dcql_gd_wait(uint8_t mask, uint8_t want) {
    for (uint32_t i = 0; i < DCQL_GD_SPINS; ++i)
        if ((DCQL_HW_R8(DCQL_ATA_ALTSTATUS) & mask) == want) return 0;
    return -1;
}
static inline int dcql_gd_wait_irq(void) {
    for (uint32_t i = 0; i < DCQL_GD_SPINS; ++i)
        if (DCQL_HW_R32(DCQL_EXT_INT_STAT) & 1u) return 0;
    return -1;
}
/* one 12-byte packet command; with buf, its data phase (at most *len bytes kept). 0 = done without error;
   -1 busy before, -2 no data request (not a packet device), -3 no completion, -4 the drive set ERR */
static inline int dcql_gd_packet(const uint8_t cmd[12], uint16_t *buf, uint32_t *len) {
    if (dcql_gd_wait(DCQL_ATA_BSY | DCQL_ATA_DRQ, 0)) return -1;
    (void)DCQL_HW_R8(DCQL_ATA_STATUS);   /* a stale interrupt cleared */
    DCQL_HW_R8(DCQL_ATA_FEATURES) = 0;   /* PIO */
    DCQL_HW_R8(DCQL_ATA_COMMAND) = 0xa0; /* PACKET */
    if (dcql_gd_wait(DCQL_ATA_BSY | DCQL_ATA_DRQ, DCQL_ATA_DRQ)) return -2;
    (void)DCQL_HW_R8(DCQL_ATA_STATUS);
    for (int i = 0; i < 6; ++i) DCQL_HW_R16(DCQL_ATA_DATA) = (uint16_t)(cmd[2 * i] | (cmd[2 * i + 1] << 8));
    if (dcql_gd_wait_irq()) return -3;
    uint32_t got = 0;
    if (buf && len && (DCQL_HW_R8(DCQL_ATA_STATUS) & DCQL_ATA_DRQ)) {
        const uint32_t n = (uint32_t)(DCQL_HW_R8(DCQL_ATA_LBA_MID) | (DCQL_HW_R8(DCQL_ATA_LBA_HIGH) << 8));
        for (uint32_t i = 0; i < n / 2; ++i) {
            const uint16_t w = DCQL_HW_R16(DCQL_ATA_DATA);
            if (2 * i + 2 <= *len) buf[i] = w;
        }
        got = n < *len ? n : *len;
        if (dcql_gd_wait_irq()) return -3;
    }
    if (len) *len = got;
    (void)DCQL_HW_R8(DCQL_ATA_STATUS);
    if (dcql_gd_wait(DCQL_ATA_BSY, 0)) return -3;
    return (DCQL_HW_R8(DCQL_ATA_ALTSTATUS) & DCQL_ATA_ERR) ? -4 : 0;
}
/* GDEMU's firmware version as 0x00MMmmpp (5.14.0 = 0x00050e00), or 0 when nothing answered as GDEMU does.
   Its 8 bytes: 00 00 09 01 00 00 14 05 (bootloader 1.9, firmware 5.14.0; openMenu gdemu_sdk.h) */
static inline uint32_t dcql_gdemu_version(void) {
    uint8_t cmd[12] = {0x52, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0};
    uint16_t b[4] = {0, 0, 0, 0};
    uint32_t n = sizeof(b);
    if (dcql_gd_packet(cmd, b, &n) || n != 8) return 0;
    const uint8_t *v = (const uint8_t *)b;
    const uint32_t ver = ((uint32_t)v[7] << 16) | ((uint32_t)((v[6] >> 4) * 10 + (v[6] & 15)) << 8) | v[5];
    return ver ? ver : 0x00ffffffu;   /* answered with zeros: GDEMU of an unknown version */
}
/* GDEMU: serve image n (1 to 999) from now on; 0 = its default image. 0 = the drive took the command */
static inline int dcql_gdemu_select(uint16_t n) {
    uint8_t cmd[12] = {0x52, 0x82, (uint8_t)n, (uint8_t)(n >> 8), 0, 0, 0, 0, 0, 0, 0, 0};
    return dcql_gd_packet(cmd, 0, 0);
}

/* ---------------------------------------------------------------- screen colour, picture count */
#define DCQL_PVR_BORDER 0xa05f8040u
#define DCQL_PVR_VO_CTRL 0xa05f80e8u
#define DCQL_PVR_SYNC_STAT 0xa05f810cu
/* the whole screen in one colour (0xRRGGBB): the border colour over a blanked picture */
static inline void dcql_screen_colour(uint32_t rgb) {
    DCQL_HW_R32(DCQL_PVR_BORDER) = rgb & 0xffffffu;
    DCQL_HW_R32(DCQL_PVR_VO_CTRL) |= 8u;
}
static inline void dcql_screen_picture(void) {   /* the picture back, the border black */
    DCQL_HW_R32(DCQL_PVR_BORDER) = 0;
    DCQL_HW_R32(DCQL_PVR_VO_CTRL) &= ~8u;
}
/* wait for n vertical blanks (60 a second, 50 on PAL), bounded; needs no timer or interrupt (KallistiOS vid_waitvbl) */
static inline void dcql_wait_vbl(uint32_t n) {
    for (uint32_t k = 0; k < n; ++k) {
        uint32_t i = 0;
        while (!(DCQL_HW_R32(DCQL_PVR_SYNC_STAT) & 0x1ffu) && ++i < 4000000u) {}
        i = 0;
        while ((DCQL_HW_R32(DCQL_PVR_SYNC_STAT) & 0x1ffu) && ++i < 4000000u) {}
    }
}

/* ---------------------------------------------------------------- the SH-4 operand cache */
/* write back every dirty line and invalidate the whole operand cache: a write to each entry of the operand cache's
   address array (non-associative) writes the line back first when it is valid and dirty (SH7750 hardware manual,
   "OC address array"). Must run from P2. After it no line of the program's can be written back over memory the
   caller fills next (a dirty line evicted later would overwrite it on hardware; Flycast has no cache to show this). */
static inline void dcql_ocache_purge_all(void) {
    for (uint32_t i = 0; i < 512; ++i) DCQL_HW_R32(0xf4000000u | (i << 5)) = 0;
}
#endif
