/* testprog.c: the control loop's test program (dc-qalink 0.2; first written for the RE4 Dreamcast console loop, lane
   eng-loop, 2026-11-19). MIT licence. Sent by the PC tool to the loader; each word of the PC's --args picks a path:

     report     (default) a few lines and DONE through cl_exit
     ret        back to the loader at once, the link never started (exec and the way back alone; ql-core2)
     gdreboot   hand in DONE through cl_exit (END to the PC first), then the disc switch alone: the loader's GDEMU image
                selected, a BIOS reboot (the full restart even under "return stub")
     stubfail   control: the stub's copy of LOADER.BIN's SHA-256 spoilt, so its check refuses and its fallback (GDEMU
                image, reboot) must bring the loader back; the loader then shows "STUB: STEP 7 ERR 400"
     diag=1     every step of the client and of the stub held on screen about a second (large text / screen colour)
     return     return from main (the atexit hand-in: DONE, exit 0)
     big:<n>    n more bytes of report (the report path under load)
     lines:<n>  n numbered lines on the serial line (forwarded live to the PC on the adapter)
     file:<p>   open /pc/<p> from the PC's file service, read it whole, report its length, time and SHA-256
                (with the heap in use before the open and after the close: the read-ahead block freed at last close)
     fileopen:<p> open /pc/<p>, read 100 bytes through the C library (the read-ahead block) and leave it open: the
                way out frees the block ("cl-files: ... read-ahead freed at exit"; lane ql-fix-dc)
     endnolink  stop the link just before cl_exit: END cannot be sent, so the way out must not wait for ring room
                (the exit-time bound; the report comes back from the copy kept in RAM; lane ql-fix-dc)
     cdfile:<p> the same from the loader's disc (/cd/<p>), for the load-time comparison
     still      one 640x480 test still (colour bars) to the PC; stills:<n> n of them
     screen:<n> n grabs of the screen itself (the frame buffer, bars drawn on its lower part) to the PC
     crash      a misaligned write (then an illegal instruction, then abort, as Flycast raises none of the first):
                CRASHED with the registers, back through the return stub
     heapwreck  overwrite the whole heap (and KOS's own records in it), then crash: the stub still brings the loader
     waitreset  loop with interrupts on until the PC's RESET (RESET), or the deadline (TIMEOUT)
     hang       loop with interrupts on: the --deadline hands in TIMEOUT
     hangoff    interrupts off for ever: the link thread cannot run, RESET goes unanswered; with wdt=1 the watchdog
                resets the CPU; otherwise only a power cycle returns
     reboot     BIOS reboot without a hand-in: shows what the BIOS boots after a soft reboot (loader -> LOST)
     preload0   no loader preload when no stub is armed
     wdt=1      arm the SH-4 watchdog (manual reset after 5.25 ms, fed every 1 ms by TMU1; off by default)

   It shows its state on screen so a photo tells which path ran. */
#include <kos.h>
#include <dc/biosfont.h>
#include <dc/video.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <malloc.h>
#include "cl_client.h"
#include "dcql_loop.h"
#include "dcql_hw.h"
#include "dcql_net.h"

void dcql_bios_reboot(void) __attribute__((noreturn));   /* lib/dcql_return.c: the GDEMU image, then a reboot */
int __real_cdrom_read_toc(cd_toc_t *toc, bool hd);
int __wrap_cdrom_read_toc(cd_toc_t *toc, bool hd) {
    int rc = __real_cdrom_read_toc(toc, true);
    return rc && !hd ? __real_cdrom_read_toc(toc, false) : rc;
}

static void say(int row, const char *s) {
    bfont_draw_str_ex(vram_s + (40 + row * 24) * 640 + 20, 640, 0xffe0, 0, 16, true, s);
}

static int has(const char *args, const char *w) { return strstr(args, w) != NULL; }
/* a whole word of the args ("ret" is not "return") */
static int word(const char *a, const char *w) {
    const size_t n = strlen(w);
    for (const char *p = strstr(a, w); p; p = strstr(p + 1, w))
        if ((p == a || p[-1] == ' ') && (p[n] == 0 || p[n] == ' ')) return 1;
    return 0;
}

static void read_pc_file(const char *path, int cd) {
    char p[160];
    snprintf(p, sizeof(p), "/%s/%s", cd ? "cd" : "pc", path);
    for (char *q = p; *q; ++q) if (*q == ' ') { *q = 0; break; }
    printf("file %s: opening\n", p);
    const unsigned heap0 = (unsigned)mallinfo().uordblks;
    const uint64_t t0 = timer_ms_gettime64();
    FILE *f = fopen(p, "rb");
    if (!f) { cl_report("file %s: open refused\n", p); printf("file %s: open refused\n", p); return; }
    cl_sha256_t s;
    cl_sha256_init(&s);
    static uint8_t b[64 * 1024];
    size_t n, total = 0;
    while ((n = fread(b, 1, sizeof(b), f)) > 0) { cl_sha256_update(&s, b, n); total += n; }
    fclose(f);
    uint8_t d[32];
    cl_sha256_final(&s, d);
    char hex[65];
    for (int i = 0; i < 32; ++i) snprintf(hex + 2 * i, 3, "%02x", d[i]);
    const unsigned ms = (unsigned)(timer_ms_gettime64() - t0);
    cl_report("file %s: %u bytes in %u ms sha256 %s\n", p, (unsigned)total, ms, hex);
    cl_report("file %s: heap in use %u B before the open, %u B after the close\n", p, heap0,
              (unsigned)mallinfo().uordblks);
    printf("file %s: %u bytes in %u ms\n", p, (unsigned)total, ms);
}
/* fileopen:<p>: a /pc file left open at exit, its read-ahead block in use */
static void open_pc_file(const char *path) {
    char p[160];
    snprintf(p, sizeof(p), "/pc/%s", path);
    for (char *q = p; *q; ++q) if (*q == ' ') { *q = 0; break; }
    FILE *f = fopen(p, "rb");
    if (!f) { cl_report("fileopen %s: open refused\n", p); return; }
    static uint8_t b[100];
    const size_t n = fread(b, 1, sizeof(b), f);
    cl_report("fileopen %s: %u bytes read, left open\n", p, (unsigned)n);
}

int main(int argc, char **argv) {
    (void)argc; (void)argv;
    vid_set_mode(DM_640x480, PM_RGB565);
    vid_clear(0, 0, 40);
    const char *args0 = "";
    /* cl_args reads the hand-off area; preload0 must be known before cl_init */
    const cl_handoff_t *h = (const cl_handoff_t *)CL_HANDOFF_ADDR;
    if (h->h.magic == CL_HANDOFF_MAGIC) args0 = h->h.args;
    const int active = cl_init((has(args0, "preload0") ? CL_NO_PRELOAD : 0) | (word(args0, "ret") ? CL_NO_LINK : 0));
    const char *args = cl_args();
    char line[64];
    if (active && has(args, "nosave")) {   /* lab arm (ql-core4): the full restart keeps nothing in RAM; END alone tells */
        cl_handoff_t *w = (cl_handoff_t *)CL_HANDOFF_ADDR;
        w->h.flags |= CL_F_NOSAVE;
        cl_handoff_seal(w);
    }
    say(0, "CONTROL LOOP TEST PROGRAM");
    snprintf(line, sizeof(line), "RUN %08X  ACTIVE %d", (unsigned)cl_run_id(), active);
    say(1, line);
    snprintf(line, sizeof(line), "ARGS %.40s", args);
    say(2, line);
    if (!active) {
        say(4, "NOT STARTED BY THE LOADER. NOTHING TO DO.");
        for (;;) thd_sleep(1000);
    }
    cl_report("testprog run %08x args \"%s\"\n", (unsigned)cl_run_id(), args);
    cl_report("hand-off boot %u deadline %u s stub %u bytes way back %s net %u\n", (unsigned)h->h.boot_count,
              (unsigned)h->h.deadline_s, (unsigned)h->h.stub_bytes,
              cl_stub_armed((const cl_handoff_t *)h) ? "stub" : "preload", (unsigned)h->h.net_kind);
    cl_report("timer %u ms since boot\n", (unsigned)timer_ms_gettime64());
    if (word(args, "ret")) {
        say(4, "RET: STRAIGHT BACK, NO LINK");
        cl_report("ret: back at once\n");
        thd_sleep(1000);
        cl_exit(0);
    }
    if (h->h.net_kind == CL_NET_BBA && !has(args, "hangoff")) {   /* hangoff: interrupts off before any RESET can come */
        const uint64_t t0 = timer_ms_gettime64();
        say(3, "LINK: WAITING (UP TO 60 S)");
        /* 60 s: on the console the adapter's link negotiation (KallistiOS waits up to 10 s) and DHCP (it repeats every
           4 s) come first; the old 10 s could run out first (a likely cause of the owner's console run, 2026-11-22) */
        const int up = cl_link_wait(60000);
        say(3, up ? "LINK: UP                  " : "LINK: NOT UP              ");
        cl_report("link %s after %u ms\n", up ? "up" : "not up", (unsigned)(timer_ms_gettime64() - t0));
    }
    const char *big = strstr(args, "big:");
    if (big) {
        unsigned n = (unsigned)strtoul(big + 4, NULL, 10);
        char b[80];
        for (unsigned i = 0; i < n; i += 64) {
            snprintf(b, sizeof(b), "%08u the console loop report path carries this line...........\n", i);
            cl_report_bytes(b, n - i < 64 ? n - i : 64);
        }
    }
    const char *ln = strstr(args, "lines:");
    if (ln) {
        const unsigned n = (unsigned)strtoul(ln + 6, NULL, 10);
        char b[96];
        for (unsigned i = 0; i < n; ++i) {
            const int k = snprintf(b, sizeof(b), "testprog line %u of %u\n", i + 1, n);
            printf("%s", b);
            cl_line(b, (size_t)k);
        }
        cl_report("lines %u sent\n", n);
    }
    for (const char *fp = strstr(args, "file:"); fp; fp = strstr(fp + 5, "file:"))
        read_pc_file(fp + 5, fp >= args + 2 && fp[-2] == 'c' && fp[-1] == 'd');
    for (const char *fp = strstr(args, "fileopen:"); fp; fp = strstr(fp + 9, "fileopen:")) open_pc_file(fp + 9);
    if (has(args, "still")) {   /* "still" = one, "stills:<n>" = n, each with its own bar offset */
        static uint16_t pix[640 * 480];
        static const uint16_t bars[8] = {0xffff, 0xffe0, 0x07ff, 0x07e0, 0xf81f, 0xf800, 0x001f, 0x0000};
        const char *sn = strstr(args, "stills:");
        const unsigned n = sn ? (unsigned)strtoul(sn + 7, NULL, 10) : 1;
        const uint64_t t0 = timer_ms_gettime64();
        unsigned held = 0;
        for (unsigned k = 0; k < n; ++k) {
            for (int y = 0; y < 480; ++y)
                for (int x = 0; x < 640; ++x)
                    pix[y * 640 + x] = bars[((x / 80) + k) & 7] ^ (uint16_t)((y & 0x1f) == 0 ? 0xffff : 0);
            const uint64_t t1 = timer_ms_gettime64();
            const int r = cl_still(pix, 640, 480, 1280, 40 * k + 1);
            held += r == 0;
            cl_report("still %u (picture %u): %s in %u ms\n", k + 1, 40 * k + 1, r ? "NOT HELD" : "held by the PC",
                      (unsigned)(timer_ms_gettime64() - t1));
        }
        cl_report("stills %u of %u held in %u ms\n", held, n, (unsigned)(timer_ms_gettime64() - t0));
    }
    const char *sc = strstr(args, "screen:");
    if (sc) {   /* "screen:<n>": n grabs of the screen itself (the frame buffer in video memory), each with new bars */
        static const uint16_t bars[8] = {0xffff, 0xffe0, 0x07ff, 0x07e0, 0xf81f, 0xf800, 0x001f, 0x0000};
        const unsigned n = (unsigned)strtoul(sc + 7, NULL, 10);
        unsigned held = 0;
        for (unsigned k = 0; k < n; ++k) {
            for (int y = 200; y < 480; ++y)
                for (int x = 0; x < 640; ++x) vram_s[y * 640 + x] = bars[((x / 80) + k) & 7];
            snprintf(line, sizeof(line), "SCREEN GRAB %u OF %u", k + 1, n);
            say(6, line);
            const uint64_t t1 = timer_ms_gettime64();
            const int r = cl_still(vram_s, 640, 480, 1280, k + 1);
            held += r == 0;
            cl_report("screen %u: %s in %u ms\n", k + 1, r ? "NOT HELD" : "held by the PC",
                      (unsigned)(timer_ms_gettime64() - t1));
        }
        cl_report("screens %u of %u held\n", held, n);
    }
    if (has(args, "heapwreck")) {
        say(4, "HEAP WRECKED, THEN A CRASH");
        thd_sleep(500);
        /* everything from the end of the program's BSS to its stack floor, KOS's own records included */
        volatile uint32_t *p = (volatile uint32_t *)(((uintptr_t)end + 31) & ~31u);
        volatile uint32_t *stop = (volatile uint32_t *)(CL_HANDOFF_ADDR - 0x20000u);
        irq_disable();
        while (p < stop) *p++ = 0xdeadbeefu;
        __asm__ __volatile__(".word 0xfffd");   /* illegal instruction: the crash handler, then the stub */
        for (;;) {}
    }
    if (has(args, "crash")) {
        say(4, "CRASH: MISALIGNED WRITE");
        thd_sleep(1500);
        volatile uint32_t *p = (volatile uint32_t *)0x8c100002u;
        *p = 0xdeadbeefu;
        /* Flycast does not raise the address error a misaligned write raises on the SH-4. Still running: an illegal
           instruction (event 180). Still running after that: abort() (the wrapped arch_abort path). */
        cl_report("misaligned write did not fault: illegal instruction\n");
        say(5, "NO FAULT: ILLEGAL INSTRUCTION");
        __asm__ __volatile__(".word 0xfffd");
        cl_report("illegal instruction did not fault: abort\n");
        say(6, "NO FAULT: ABORT");
        abort();
    }
    if (has(args, "waitreset")) {
        say(4, "WAITING FOR RESET FROM THE PC");
        for (;;) thd_sleep(100);
    }
    if (has(args, "hang") && !has(args, "hangoff")) {
        say(4, "HANG: WAITING FOR THE DEADLINE");
        for (;;) thd_sleep(100);
    }
    if (has(args, "hangoff")) {
        say(4, "HANG WITH INTERRUPTS OFF");
        /* the watchdog as the hang starts, on the TV: armed and counting = CSR E7 (ql-core6: manual reset) and a count
           under 256; it resets the CPU about 5 ms after interrupts go off (the 1 ms feed stops) */
        snprintf(line, sizeof(line), "WDT CSR %02X CNT %3u%s", dcql_wdt_csr(), dcql_wdt_cnt(),
                 (dcql_wdt_csr() & DCQL_WT_TME) ? " ARMED: RESET IN 5 MS" : " NOT ARMED: POWER CYCLE");
        say(5, line);
        cl_report("hangoff: %s\n", line);
        thd_sleep(500);
        irq_disable();
        for (;;) {}
    }
    if (word(args, "reboot")) {   /* a whole word: "gdreboot" took this branch until ql-core3 (the console's row 7 was a
                                     plain reboot, no image select and no hand-in) */
        say(4, "BIOS REBOOT, NO HAND-IN");
        thd_sleep(1500);
        arch_reboot();
    }
    if (word(args, "gdreboot")) {   /* the disc switch alone: the loader's GDEMU image selected, then a BIOS reboot */
        say(4, "DISC SWITCH: IMAGE, THEN REBOOT");
        cl_report("gdreboot: GDEMU image %u, then a BIOS reboot, its END first\n",
                  (unsigned)h->h.reserved[CL_RSV_GDEMU_SLOT]);
        thd_sleep(1500);
        /* ql-core5: through the client's own ending, like every other ending, so the END goes to the PC before the
           restart (the console's first-run8: this arm called dcql_bios_reboot itself and no END came). The full
           restart is forced even under "return stub": the flag makes cl_exit take dcql_bios_reboot (image, reboot). */
        cl_handoff_t *w = (cl_handoff_t *)CL_HANDOFF_ADDR;
        w->h.flags |= CL_F_RET_REBOOT;
        cl_handoff_seal(w);
        cl_exit(0);
    }
    if (word(args, "stubfail")) {   /* control: the stub's SHA-256 check must refuse, and its fallback take over */
        say(4, "STUB FAIL: WRONG SHA, FALLBACK");
        cl_report("stubfail: the stub's LOADER.BIN hash spoilt; its reboot fallback must bring the loader\n");
        thd_sleep(1500);
        cl_handoff_t *w = (cl_handoff_t *)CL_HANDOFF_ADDR;
        w->h.ret_sha[0] ^= 0xff;
        cl_handoff_seal(w);
        cl_exit(0);
    }
    if (word(args, "stubata")) {   /* lab arm: the stub's BIOS rung skipped, so its second rung (the drive's own read) must
                                      bring the loader back alone */
        say(4, "STUB: NO BIOS RUNG, DRIVE READ");
        cl_report("stubata: the stub's BIOS rung skipped; the drive's own read must bring the loader\n");
        thd_sleep(1000);
        cl_handoff_t *w = (cl_handoff_t *)CL_HANDOFF_ADDR;
        w->h.flags |= CL_F_STUB_NOBIOS;
        cl_handoff_seal(w);
        cl_exit(0);
    }
    if (has(args, "return")) {
        say(4, "RETURN FROM MAIN");
        thd_sleep(1500);
        return 0;
    }
    say(4, "DONE: BACK TO THE LOADER");
    thd_sleep(1500);
    if (word(args, "endnolink")) {   /* the exit-time bound (ql-fix-dc): END has no link to go out on */
        cl_report("endnolink: link stopped at %u ms, then cl_exit\n", (unsigned)timer_ms_gettime64());
        dcql_net_stop();
    }
    cl_exit(0);
}
