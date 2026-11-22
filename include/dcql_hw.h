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

/* ---------------------------------------------------------------- the clock: whole seconds, for multi-second limits */
/* the AICA's real-time clock (seconds, two 16-bit halves on the G2 bus); runs without timers or interrupts */
static inline uint32_t dcql_rtc_s(void) {
    return ((DCQL_HW_R32(0xa0710000u) & 0xffffu) << 16) | (DCQL_HW_R32(0xa0710004u) & 0xffffu);
}

/* ---------------------------------------------------------------- the drive read by its own command, no BIOS */
#define DCQL_ATA_BCL DCQL_ATA_LBA_MID   /* byte count (ATAPI) */
#define DCQL_ATA_BCH DCQL_ATA_LBA_HIGH
/* the GD-ROM's DMA stopped: SB_GDEN = 0 ends a transfer in flight (KallistiOS reads by DMA) */
static inline void dcql_g1_dma_stop(void) { DCQL_HW_R32(0xa05f7414u) = 0; }
/* the drive busy or holding data from a command the program (or the BIOS) left: ATA DEVICE RESET, bounded */
static inline void dcql_gd_unstick(void) {
    if (!(DCQL_HW_R8(DCQL_ATA_ALTSTATUS) & (DCQL_ATA_BSY | DCQL_ATA_DRQ))) return;
    DCQL_HW_R8(DCQL_ATA_COMMAND) = 0x08;
    for (int i = 0; i < 4; ++i) (void)DCQL_HW_R8(DCQL_ATA_ALTSTATUS);
    (void)dcql_gd_wait(DCQL_ATA_BSY, 0);
    (void)DCQL_HW_R8(DCQL_ATA_STATUS);
}
/* the drive's pending error read and cleared (SPI REQ_ERROR 0x13): until it is, every command reports CHECK (the ERR
   bit) for a sense left by an earlier command (a refused vendor command, the unit attention after an image change;
   Flycast's drive works the same way). Returns sense key | ASC << 8 | ASCQ << 16, or 0xff when it did not answer */
static inline uint32_t dcql_gd_req_error(void) {
    const uint8_t cmd[12] = {0x13, 0, 0, 0, 10, 0, 0, 0, 0, 0, 0, 0};
    uint16_t b[5] = {0, 0, 0, 0, 0};
    uint32_t n = sizeof(b);
    const int rc = dcql_gd_packet(cmd, b, &n);
    if ((rc && rc != -4) || n < 10) return 0xffu;
    const uint8_t *r = (const uint8_t *)b;
    return (uint32_t)(r[2] & 15) | ((uint32_t)r[8] << 8) | ((uint32_t)r[9] << 16);
}
/* n 2048-byte sectors from frame address fad to dst (2-byte aligned; pass an uncached address) with the drive's own
   read command (SPI CD_READ 0x30, data area, PIO, as Linux's gdrome driver sends it), without the BIOS: nothing of
   the BIOS's state, vectors or disc code is needed. 0 = read; -1 busy before, -2 no packet phase, -3 no data in
   time, -6 the drive set ERR (no disc, not ready, unit attention after an image change: ask again) */
static inline int dcql_gd_read_pio(uint32_t fad, uint32_t n, uint16_t *dst) {
    const uint8_t cmd[12] = {0x30, 0x20, (uint8_t)(fad >> 16), (uint8_t)(fad >> 8), (uint8_t)fad, 0, 0, 0,
                             (uint8_t)(n >> 16), (uint8_t)(n >> 8), (uint8_t)n, 0};
    if (dcql_gd_wait(DCQL_ATA_BSY | DCQL_ATA_DRQ, 0)) return -1;
    (void)DCQL_HW_R8(DCQL_ATA_STATUS);
    DCQL_HW_R8(DCQL_ATA_FEATURES) = 0;     /* PIO */
    DCQL_HW_R8(DCQL_ATA_BCL) = 0;          /* at most 2048 bytes a data block */
    DCQL_HW_R8(DCQL_ATA_BCH) = 8;
    DCQL_HW_R8(DCQL_ATA_COMMAND) = 0xa0;   /* PACKET */
    if (dcql_gd_wait(DCQL_ATA_BSY | DCQL_ATA_DRQ, DCQL_ATA_DRQ)) return -2;
    for (int i = 0; i < 6; ++i) DCQL_HW_R16(DCQL_ATA_DATA) = (uint16_t)(cmd[2 * i] | (cmd[2 * i + 1] << 8));
    const uint32_t total = n * 2048u;
    uint32_t got = 0;
    while (got < total) {
        uint32_t i = 0;
        uint8_t s;
        for (int k = 0; k < 4; ++k) (void)DCQL_HW_R8(DCQL_ATA_ALTSTATUS);   /* the drive's 400 ns to raise BSY */
        while (((s = DCQL_HW_R8(DCQL_ATA_ALTSTATUS)) & DCQL_ATA_BSY) || !(s & (DCQL_ATA_DRQ | DCQL_ATA_ERR)))
            if (++i > 4u * DCQL_GD_SPINS) return -3;   /* a seek and spin-up: a few seconds at most */
        s = DCQL_HW_R8(DCQL_ATA_STATUS);   /* the drive's interrupt cleared */
        if (!(s & DCQL_ATA_DRQ)) return -6;
        uint32_t cnt = (uint32_t)(DCQL_HW_R8(DCQL_ATA_BCL) | (DCQL_HW_R8(DCQL_ATA_BCH) << 8));
        if (!cnt || cnt > total - got) cnt = total - got < 2048u ? total - got : 2048u;
        for (uint32_t w = 0; w < cnt / 2; ++w) dst[got / 2 + w] = DCQL_HW_R16(DCQL_ATA_DATA);
        got += cnt & ~1u;
    }
    for (int k = 0; k < 4; ++k) (void)DCQL_HW_R8(DCQL_ATA_ALTSTATUS);
    if (dcql_gd_wait(DCQL_ATA_BSY, 0)) return -3;
    return (DCQL_HW_R8(DCQL_ATA_STATUS) & DCQL_ATA_ERR) ? -6 : 0;
}

/* ---------------------------------------------------------------- the way-back record that outlives a reboot */
/* The stub's record (step, error word, run) in three more places than the hand-off header, each 8 words with a check
   word, so the next loader can say how the last way back ended even when a BIOS reboot came between (the console run
   of 2026-11-22 lost the hand-off area at the top of main RAM on every reboot). The loader reads all of them before
   anything of KallistiOS starts (KOS_INIT_EARLY) and says which held: that answers where memory survives the BIOS. */
typedef struct { uint32_t magic, run_id, step, err, aux, entries, w0, check; } dcql_crumb_t;
#define DCQL_CRUMB_MAGIC 0x424d5243u   /* "CRMB" */
#define DCQL_CRUMB_PLACES 3
/* 0: main RAM at 8 MiB (below every program's own top), 1: the top of sound RAM, 2: the top of video RAM */
static inline volatile uint32_t *dcql_crumb_at(int k) {
    return (volatile uint32_t *)(k == 0 ? 0xac7fffe0u : k == 1 ? 0xa09fffe0u : 0xa57fffe0u);
}
static inline uint32_t dcql_crumb_sum(const uint32_t *w) {
    uint32_t c = 0x811c9dc5u;
    for (int i = 0; i < 7; ++i) c = (c ^ w[i]) * 0x01000193u;
    return c;
}
static inline void dcql_g2_fifo_wait(void) {
    for (uint32_t i = 0; i < 100000u && (DCQL_HW_R32(0xa05f688cu) & 0x11u); ++i) {}
}
static inline void dcql_crumb_put(dcql_crumb_t *c) {
    c->magic = DCQL_CRUMB_MAGIC;
    c->check = dcql_crumb_sum((const uint32_t *)c);
    const uint32_t *w = (const uint32_t *)c;
    for (int k = 0; k < DCQL_CRUMB_PLACES; ++k) {
        volatile uint32_t *p = dcql_crumb_at(k);
        for (int i = 0; i < 8; ++i) {
            if (k == 1) dcql_g2_fifo_wait();   /* sound RAM sits on the G2 bus */
            p[i] = w[i];
        }
    }
}
/* 1 when place k holds a whole record (copied to *c) */
static inline int dcql_crumb_get(int k, dcql_crumb_t *c) {
    volatile uint32_t *p = dcql_crumb_at(k);
    uint32_t *w = (uint32_t *)c;
    for (int i = 0; i < 8; ++i) w[i] = p[i];
    return c->magic == DCQL_CRUMB_MAGIC && c->check == dcql_crumb_sum(w);
}
static inline void dcql_crumb_clear(void) {
    for (int k = 0; k < DCQL_CRUMB_PLACES; ++k) {
        if (k == 1) dcql_g2_fifo_wait();
        dcql_crumb_at(k)[0] = 0;
    }
}

/* ---------------------------------------------------------------- the hand-off area saved across a BIOS reboot */
/* Before a BIOS reboot the way back copies the hand-off header and the run's report (not the stub) to main RAM at
   7.75 MiB and to sound RAM at 1.75 MiB; the next loader puts a copy back when the area at the top of RAM did not
   survive (Flycast, 2026-11-22: the top 128 KiB is lost on a reboot while main RAM at 8 MiB, sound and video RAM keep
   their words; the console lost it on every reboot the same day). */
#define DCQL_SAVE_RAM 0xac7c0000u
#define DCQL_SAVE_SND 0xa09c0000u
/* n bytes (a multiple of 4) from src to dst, both uncached; g2: dst is sound RAM (the G2 FIFO waited) */
static inline void dcql_copy_words(uint32_t dst, uint32_t src, uint32_t n, int g2) {
    volatile uint32_t *d = (volatile uint32_t *)dst;
    const volatile uint32_t *s = (const volatile uint32_t *)src;
    for (uint32_t i = 0; i < n / 4; ++i) {
        if (g2 && !(i & 7)) dcql_g2_fifo_wait();
        d[i] = s[i];
    }
}
/* the area at area_p2 (header hdr_bytes, report rep_bytes at rep_off) saved to both places */
static inline void dcql_save_handoff(uint32_t area_p2, uint32_t hdr_bytes, uint32_t rep_off, uint32_t rep_bytes) {
    rep_bytes = (rep_bytes + 3) & ~3u;
    for (int k = 0; k < 2; ++k) {
        const uint32_t base = k ? DCQL_SAVE_SND : DCQL_SAVE_RAM;
        dcql_copy_words(base + rep_off, area_p2 + rep_off, rep_bytes, k);
        dcql_copy_words(base, area_p2, hdr_bytes, k);
    }
}

/* ---------------------------------------------------------------- the broadband adapter at the way back (ql-core4) */
/* The adapter's PCI bridge ("GAPS") and its RTL8139, on the G2 bus. A BIOS reboot does not know the adapter, so its
   32 KiB of memory (KallistiOS's receive ring and transmit buffers, 0x01840000) is a fourth place a saved hand-off may
   outlive the reboot in, where main and sound RAM did not (the console, 2026-11-22). Read only when the bridge says it
   is there and still set up (its ID, and the "ready" bit KallistiOS's gaps_init waits for), so a console without an
   adapter, or one whose bridge was reset, reads nothing from it. */
#define DCQL_GAPS_ID 0xa1001400u
#define DCQL_GAPS_READY 0xa1001418u
#define DCQL_BBA_NIC 0xa1001700u
#define DCQL_SAVE_BBA 0xa1840000u      /* header at +0 (CL_STUB_OFF bytes), the report from +0x1000 */
#define DCQL_SAVE_BBA_REPORT 0x1000u
#define DCQL_SAVE_BBA_SIZE 0x8000u
static inline int dcql_gaps_ready(void) {
    static const char id[16] = {'G', 'A', 'P', 'S', 'P', 'C', 'I', '_', 'B', 'R', 'I', 'D', 'G', 'E', '_', '2'};
    for (int i = 0; i < 16; ++i)
        if (DCQL_HW_R8(DCQL_GAPS_ID + i) != (uint8_t)id[i]) return 0;
    return (DCQL_HW_R32(DCQL_GAPS_READY) & 1u) != 0;
}
/* the adapter stopped from anywhere (interrupts off, no KallistiOS): receiver and transmitter off, its interrupts
   masked, so nothing it receives is written into its memory or raises an interrupt across the reboot */
static inline void dcql_bba_quiet(void) {
    if (!dcql_gaps_ready()) return;
    DCQL_HW_R16(DCQL_BBA_NIC + 0x3c) = 0;        /* RT_INTRMASK */
    DCQL_HW_R8(DCQL_BBA_NIC + 0x37) = 0;         /* RT_CHIPCMD: rx and tx off */
    DCQL_HW_R16(DCQL_BBA_NIC + 0x3e) = 0xffff;   /* RT_INTRSTATUS cleared */
}

/* ---------------------------------------------------------------- three words as bars on the TV */
/* The stub has no font: its words go on the screen as bars a TV or a photo can read. Three rows, read left to right,
   most significant bit first, four bars to a hex digit with a wider gap between digits: a tall white bar is 1, a short
   grey bar is 0. Row 1: a (8 bits), row 2: b (32 bits), row 3: c (32 bits). Drawn into the frame buffer the program
   left on screen, whatever its size and pixel format (white and grey are the same byte in every byte, so any 16, 24
   or 32-bit format shows them); the border shows border_rgb. Nothing is drawn if no picture is set up. */
static inline void dcql_screen_bars(uint32_t a, uint32_t b, uint32_t c, uint32_t border_rgb) {
    const uint32_t size = DCQL_HW_R32(0xa05f805cu), wpl = (size & 0x3ffu) + 1, lines = ((size >> 10) & 0x3ffu) + 1;
    const uint32_t mod = (size >> 20) & 0x3ffu, stride = wpl + (mod ? mod - 1 : 0);
    if (wpl < 88 || lines < 64 || stride * lines * 4u > 0x400000u) return;
    const uint32_t u = wpl / 44, h = lines / 16;
    const uint32_t sof[2] = {DCQL_HW_R32(0xa05f8050u) & 0x7ffffcu, DCQL_HW_R32(0xa05f8054u) & 0x7ffffcu};
    for (int f = 0; f < 2; ++f) {
        if (f && sof[1] == sof[0]) break;
        volatile uint32_t *fb = (volatile uint32_t *)(0xa5000000u + sof[f]);
        for (uint32_t y = 0; y < lines; ++y) {
            /* the band: rows at 2h..5h, 6h..10h, 11h..15h */
            const int r = y >= 2 * h && y < 5 * h ? 0 : y >= 6 * h && y < 10 * h ? 1 : y >= 11 * h && y < 15 * h ? 2 : -1;
            const uint32_t top = r == 0 ? 2 * h : r == 1 ? 6 * h : 11 * h, bot = r == 0 ? 5 * h : r == 1 ? 10 * h : 15 * h;
            const uint32_t v = r == 0 ? a : r == 1 ? b : c, nb = r == 0 ? 8 : 32;
            const int low = y >= bot - (bot - top) / 3;   /* the lower third: where a short bar shows */
            volatile uint32_t *row = fb + y * stride;
            for (uint32_t x = 0; x < wpl; ++x) {
                uint32_t px = 0;
                const uint32_t k = x / u;   /* units: 2 of margin, then 8 digits of 5 (4 bars and a gap) */
                if (r >= 0 && k >= 2 && k < 42 && (x % u) < (2 * u + 2) / 3) {
                    const uint32_t g = (k - 2) / 5, j = (k - 2) % 5, bit = g * 4 + j;
                    if (j < 4 && bit < nb) px = (v >> (nb - 1 - bit)) & 1u ? 0xffffffffu : low ? 0x52525252u : 0;
                }
                row[x] = px;
            }
        }
    }
    DCQL_HW_R32(DCQL_PVR_BORDER) = border_rgb & 0xffffffu;
    DCQL_HW_R32(DCQL_PVR_VO_CTRL) &= ~8u;
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
