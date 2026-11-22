/* stub.c: the control loop's resident return stub (dc-qalink 0.2). MIT licence.
 *
 * The loader copies this program (linked at CL_STUB_ADDR in P2, stub.ld) into the hand-off area before every run and
 * seals its CRC in the header. A program goes back to the loader by jumping here (dcql_return.c) with interrupts off,
 * from any state: a normal exit, a deadline, a crash handler, KOS's abort, a RESET from the PC. The stub needs nothing
 * of the program or of KallistiOS:
 *   1. it stops what could still write memory or raise interrupts: the graphics chip (reset), controller DMA, the four
 *      G2 DMA channels, the sound CPU, the SH-4 DMA controller, timers and watchdog, the ASIC's interrupt masks, and the
 *      broadband adapter's chip when the loader used it;
 *   2. it reads LOADER.BIN from the mounted disc (the GD sector and length the loader found and wrote in the header)
 *      with the BIOS's own disc calls (the vector at 0x8C0000BC, as KallistiOS's cdrom.c calls it), uncached, to
 *      0x8C010000;
 *   3. it checks the SHA-256 of what it read against the header's; a mismatch is a BIOS reboot (the last rungs);
 *   4. it turns the caches off and invalidates them (as KallistiOS's exec trampoline) and starts the loader.
 * Progress is written to the header's scratch words (tramp[], not covered by the header CRC), so a loader that comes
 * back by another way can say where the stub stopped, and shown as the colour of the whole screen (CL_STUB_COLOURS;
 * held about a second a step when the client's args had diag=1).
 * Runs in P2 (uncached) with SR.BL set: an exception inside it resets the CPU (the BIOS boots the mounted disc).
 *
 * Console fixes (lane ql-core2, 2026-11-22; Flycast has no operand cache and answers the BIOS's disc calls itself, so
 * neither showed there):
 *   - the operand cache is written back and invalidated first: a dirty line of the program's, evicted while the stub
 *     reads LOADER.BIN to 0x8C010000 through P2, would be written back over the bytes just read (the SHA-256 check then
 *     fails and the stub reboots);
 *   - VBR is the BIOS's while the BIOS's disc code runs (the program's handlers are dead by then);
 *   - with a GDEMU image known (hand-off reserved[CL_RSV_GDEMU_SLOT]) the image is selected before the disc is read,
 *     and again before a reboot, so a reboot boots the loader's image like openMenu's disc switch, not openMenu. */
#include <stdint.h>
#include "dcql_loop.h"
#include "dcql_hw.h"

typedef volatile uint32_t vu32;
typedef volatile uint16_t vu16;
typedef volatile uint8_t vu8;
#define P2(a) (((uint32_t)(a) & 0x1fffffffu) | 0xa0000000u)
#define HD ((volatile cl_handoff_hdr_t *)P2(CL_HANDOFF_ADDR))
#define REG32(a) (*(vu32 *)(a))

void stub_go(uint32_t sr, uint32_t vbr, uint32_t fpscr) __attribute__((noreturn));   /* stub_entry.S */

/* each step also as two characters on the serial port ("S1".."S7", polled; a console without a cable loses nothing) */
static void serial_put(char c) {
    volatile uint16_t *fsr = (volatile uint16_t *)0xffe80010u;
    for (int i = 0; i < 100000 && !(*fsr & 0x20u); ++i) {}   /* TDFE */
    *(volatile uint8_t *)0xffe8000cu = (uint8_t)c;
    *fsr &= (uint16_t)~0x60u;
}
static void step(uint32_t s) {
    static const uint32_t colours[] = CL_STUB_COLOURS;
    HD->tramp[CL_STUBW_STEP] = s;
    dcql_screen_colour(colours[s < sizeof(colours) / sizeof(colours[0]) ? s : 0]);
    serial_put('S');
    serial_put((char)('0' + s));
    serial_put(s == CL_STUB_STEP_JUMP || s == CL_STUB_STEP_REBOOT ? '\n' : ' ');
    if (HD->flags & CL_F_DIAG) dcql_wait_vbl(s == CL_STUB_STEP_REBOOT ? 180 : 60);   /* about 1 s (3 s for a reboot) */
}
/* the GDEMU image the loader came from, selected (0 = done or none known); the result kept for the next loader */
static int gdemu_select(void) {
    const uint32_t slot = HD->reserved[CL_RSV_GDEMU_SLOT];
    if (!slot || slot > 999) return 0;
    step(CL_STUB_STEP_GDEMU);
    const int r = dcql_gdemu_select((uint16_t)slot);
    HD->tramp[CL_STUBW_GDEMU] = 0x100u | (uint32_t)(-r);
    return r;
}
static void serial_hex(uint32_t v) {
    for (int i = 28; i >= 0; i -= 4) serial_put("0123456789abcdef"[(v >> i) & 15]);
    serial_put(' ');
}

/* the compiler's own calls (array initialisers); no C library here */
void *memcpy(void *d, const void *s, unsigned n) {
    uint8_t *a = (uint8_t *)d;
    const uint8_t *b = (const uint8_t *)s;
    while (n--) *a++ = *b++;
    return d;
}
void *memset(void *d, int v, unsigned n) {
    uint8_t *a = (uint8_t *)d;
    while (n--) *a++ = (uint8_t)v;
    return d;
}

/* ---------------------------------------------------------------- the BIOS's disc calls (syscalls.c) */
typedef int (*sysc_t)(uint32_t, uint32_t, uint32_t, uint32_t);
static int gd(uint32_t func, uint32_t r4, uint32_t r5) {
    sysc_t f = (sysc_t)(*(vu32 *)0x8c0000bcu);
    return f(r4, r5, 0 /* SUPER_FUNC_GDROM */, func);
}
enum { G_SEND = 0, G_CHECK = 1, G_EXEC = 2, G_INIT = 3, G_DRIVE = 4, G_RESET = 9, G_MODE = 10 };
enum { CMD_PIOREAD = 16, CMD_INIT = 24 };

static int gd_cmd(uint32_t cmd, void *params) {
    int st[4];
    int h = gd(G_SEND, cmd, (uint32_t)params);
    if (h <= 0) return -1;
    for (uint32_t i = 0; i < 20000000u; ++i) {
        gd(G_EXEC, 0, 0);
        const int r = gd(G_CHECK, (uint32_t)h, (uint32_t)st);
        if (r == 2 || r == 3) return 0;      /* completed / streaming */
        if (r != 1 && r != 4) return -2;    /* failed / not found */
    }
    return -3;
}

/* ---------------------------------------------------------------- SHA-256 (FIPS 180-4) */
static const uint32_t K[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5, 0xd807aa98,
    0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
    0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8,
    0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
    0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819,
    0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
    0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7,
    0xc67178f2};
#define ROR(x, n) (((x) >> (n)) | ((x) << (32 - (n))))
static void sha_block(uint32_t *hh, const uint8_t *p) {
    uint32_t w[64];
    for (int i = 0; i < 16; ++i)
        w[i] = ((uint32_t)p[4 * i] << 24) | ((uint32_t)p[4 * i + 1] << 16) | ((uint32_t)p[4 * i + 2] << 8) | p[4 * i + 3];
    for (int i = 16; i < 64; ++i) {
        const uint32_t s0 = ROR(w[i - 15], 7) ^ ROR(w[i - 15], 18) ^ (w[i - 15] >> 3);
        const uint32_t s1 = ROR(w[i - 2], 17) ^ ROR(w[i - 2], 19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    uint32_t a = hh[0], b = hh[1], c = hh[2], d = hh[3], e = hh[4], f = hh[5], g = hh[6], h = hh[7];
    for (int i = 0; i < 64; ++i) {
        const uint32_t t1 = h + (ROR(e, 6) ^ ROR(e, 11) ^ ROR(e, 25)) + ((e & f) ^ (~e & g)) + K[i] + w[i];
        const uint32_t t2 = (ROR(a, 2) ^ ROR(a, 13) ^ ROR(a, 22)) + ((a & b) ^ (a & c) ^ (b & c));
        h = g; g = f; f = e; e = d + t1; d = c; c = b; b = a; a = t1 + t2;
    }
    hh[0] += a; hh[1] += b; hh[2] += c; hh[3] += d; hh[4] += e; hh[5] += f; hh[6] += g; hh[7] += h;
}
static int sha_matches(const uint8_t *p, uint32_t n, const volatile uint8_t *want) {
    uint32_t hh[8] = {0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
    uint32_t i = 0;
    for (; i + 64 <= n; i += 64) sha_block(hh, p + i);
    uint8_t last[128];
    uint32_t k = 0;
    for (; i < n; ++i) last[k++] = p[i];
    last[k++] = 0x80;
    const uint32_t end = k <= 56 ? 64 : 128;
    while (k < end - 8) last[k++] = 0;
    const uint64_t bits = (uint64_t)n * 8;
    for (int j = 7; j >= 0; --j) last[k++] = (uint8_t)(bits >> (8 * j));
    sha_block(hh, last);
    if (end == 128) sha_block(hh, last + 64);
    for (int j = 0; j < 32; ++j)
        if ((uint8_t)(hh[j / 4] >> (24 - 8 * (j % 4))) != want[j]) return 0;
    return 1;
}

/* ---------------------------------------------------------------- the way back */
static void quiet(void) {
    REG32(0xa05f8008) = 0xffffffffu;   /* graphics chip reset (PVR_RESET_ALL), as arch_abort */
    REG32(0xa05f8008) = 0;
    REG32(0xa05f6c18) = 0;             /* controller DMA stopped (maple_dma_stop) */
    REG32(0xa05f6c14) = 0;             /* controller DMA disabled */
    for (uint32_t ch = 0; ch < 4; ++ch) REG32(0xa05f7814 + ch * 0x20) = 0;   /* G2 DMA channels off */
    REG32(0xa0702c00) |= 1;            /* the sound CPU held in reset (spu_disable) */
    REG32(0xffa00040) = 0;             /* SH-4 DMA controller off (DMAOR) */
    *(vu8 *)0xffd80004 = 0;            /* timers stopped (TSTR) */
    *(vu16 *)0xffc00004 = 0xa500;      /* watchdog off (WTCSR, TME = 0) */
    static const uint32_t masks[9] = {0xa05f6910, 0xa05f6914, 0xa05f6918, 0xa05f6920, 0xa05f6924, 0xa05f6928,
                                      0xa05f6930, 0xa05f6934, 0xa05f6938};
    for (int i = 0; i < 9; ++i) REG32(masks[i]) = 0;   /* no ASIC interrupt routed anywhere */
    for (int i = 0; i < 3; ++i) REG32(0xa05f6900 + 4 * i) = REG32(0xa05f6900 + 4 * i);   /* pending events cleared */
    if (HD->net_kind == CL_NET_BBA) *(vu8 *)0xa1001737 = 0x10;   /* the adapter's RTL8139: software reset (CR.RST) */
}

void stub_main(void) {
    volatile cl_handoff_hdr_t *h = HD;
    h->tramp[CL_STUBW_MAGIC] = CL_STUB_MAGIC;
    h->tramp[CL_STUBW_ENTRIES]++;
    h->tramp[CL_STUBW_ERR] = 0;
    h->tramp[CL_STUBW_GDEMU] = 0;
    dcql_ocache_purge_all();   /* first: no line of the program's may be written back over what the stub reads */
    if (h->reserved[CL_RSV_BIOS_VBR]) __asm__ __volatile__("ldc %0, vbr" : : "r"(h->reserved[CL_RSV_BIOS_VBR]));
    step(CL_STUB_STEP_ENTERED);
    quiet();
    step(CL_STUB_STEP_QUIET);
    /* the disc switch: the loader's image served again (a no-op when it still is; nothing without GDEMU) */
    const int sel = gdemu_select();
    if (sel) h->tramp[CL_STUBW_ERR] = 0x8000u | ((uint32_t)(-sel) << 16);
    gd(G_RESET, 0, 0);
    gd(G_INIT, 0, 0);
    int r = 0;
    for (int tries = 0; tries < 8; ++tries)
        if ((r = gd_cmd(CMD_INIT, 0)) == 0) break;
    int drive[2] = {0, 0};
    gd(G_DRIVE, (uint32_t)drive, 0);
    /* KallistiOS's default data mode (cdrom_change_datatype): data area, 2048-byte sectors */
    uint32_t mode[4] = {0 /* set */, drive[1] == 0x20 /* CD-ROM XA */ ? 2048u : 1024u, 0x1000 /* data area */, 2048};
    gd(G_MODE, (uint32_t)mode, 0);
    step(CL_STUB_STEP_GDINIT);
    if (r) { h->tramp[CL_STUBW_ERR] |= 0x100u | (uint32_t)(-r); goto reboot; }
    const uint32_t bytes = h->loader_len, lba = h->ret_lba;
    if (!bytes || !lba || bytes > 4u * 1024u * 1024u) { h->tramp[CL_STUBW_ERR] |= 0x200; goto reboot; }
    uint8_t *dst = (uint8_t *)P2(CL_LOAD_ADDR);
    const uint32_t secs = (bytes + 2047) / 2048;
    for (uint32_t s = 0; s < secs;) {
        const uint32_t n = secs - s < 16 ? secs - s : 16;
        uint32_t params[4] = {lba + s, n, (uint32_t)(dst + s * 2048), 0};
        int e = -1;
        for (int tries = 0; tries < 4 && e; ++tries) e = gd_cmd(CMD_PIOREAD, params);
        if (e) { h->tramp[CL_STUBW_ERR] |= 0x300u | (uint32_t)(-e); goto reboot; }
        s += n;
    }
    step(CL_STUB_STEP_READ);
    if (!sha_matches(dst, bytes, h->ret_sha)) { h->tramp[CL_STUBW_ERR] |= 0x400; goto reboot; }
    step(CL_STUB_STEP_SHA);
    /* the run's state and the start-up registers the loader gets, on the serial line (8 hex digits each) */
    serial_hex(h->state);
    serial_hex(h->reserved[CL_RSV_BIOS_SR]);
    serial_hex(h->reserved[CL_RSV_BIOS_VBR]);
    serial_hex(h->tramp[CL_STUBW_ENTRIES]);
    /* the KallistiOS exec contract: the words after the image are what the jump copies; nothing else to set up */
    step(CL_STUB_STEP_JUMP);
    dcql_ocache_purge_all();
    dcql_screen_picture();
    stub_go(h->reserved[CL_RSV_BIOS_SR], h->reserved[CL_RSV_BIOS_VBR], h->reserved[CL_RSV_BIOS_FPSCR]);
reboot:
    serial_hex(h->tramp[CL_STUBW_ERR]);
    step(CL_STUB_STEP_REBOOT);
    /* the BIOS then boots the loader's image, not openMenu's (the step word stays REBOOT for the next loader) */
    if (h->reserved[CL_RSV_GDEMU_SLOT] && h->reserved[CL_RSV_GDEMU_SLOT] <= 999)
        h->tramp[CL_STUBW_GDEMU] |= (0x200u | (uint32_t)(-dcql_gdemu_select((uint16_t)h->reserved[CL_RSV_GDEMU_SLOT]))) << 12;
    ((void (*)(void))0xa0000000u)();
    for (;;) {}
}
