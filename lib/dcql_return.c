/* dcql_return.c: back to the control loop's loader from a running program (include/dcql_loop.h). MIT licence.
 *
 * Shared by the client library (loader/client.c: normal exit, deadline, crash, abort, RESET) and the text rows
 * (dcql.c: the RESET command of a program that links dc-qalink but not the client). Works from an exception handler:
 * no disc access, no allocation, interrupts off.
 *
 * Two ways back, chosen by the loader (LINK.TXT "return"; hand-off flag CL_F_RET_REBOOT):
 *   reboot (the default, owner 2026-11-22): the full restart. The run's header and report are saved where a reboot may
 *          leave them (main RAM at 7.75 MiB, sound RAM at 1.75 MiB, the adapter's own memory), the adapter is
 *          stopped, the loader's GDEMU image selected and the BIOS restarts the console into the loader. The client
 *          has already sent the run's ending to the PC over the link (CL_END): RAM is a bonus, never relied on (the
 *          console lost all of it on the reboot of 2026-11-22).
 *   stub:  the resident return stub the loader armed (loader/stub.c), kept for later work; nothing uses it unless asked.
 */
#include <kos.h>
#include <arch/irq.h>
#include <arch/cache.h>
#include <dc/wdt.h>
#include <string.h>
#include "dcql_loop.h"
#include "dcql_hw.h"

#define H ((cl_handoff_t *)CL_HANDOFF_ADDR)

/* the full restart. With the loader's GDEMU image in the hand-off area it is selected first, as openMenu switches
   discs, so the BIOS boots the loader's image (in dev mode the loader is image 01 and the select changes nothing). */
void __attribute__((noreturn)) dcql_bios_reboot(void) {
    irq_disable();   /* as arch_reboot */
    dcql_wdt_off();   /* no reset in the middle of the saves (ql-core5: WTCSR at 0xffc0000c; this wrote STBCR) */
    dcql_tmu1_stop();   /* the watchdog's feed (ql-core6): no timer interrupt left armed for the BIOS */
    dcql_bba_quiet();   /* the adapter stopped: nothing lands in its memory or raises an interrupt from here on */
    if (cl_handoff_valid(H) && (H->h.flags & CL_F_NOSAVE)) {   /* lab arm: as if nothing survived the reboot */
        const uint32_t slot = H->h.reserved[CL_RSV_GDEMU_SLOT];
        if (slot && slot <= 999) dcql_gdemu_select((uint16_t)slot);
        dcql_crumb_clear();
        H->h.magic = 0;
        dcache_wback_range(CL_HANDOFF_ADDR, CL_HANDOFF_SIZE);
    } else if (cl_handoff_valid(H)) {
        const uint32_t slot = H->h.reserved[CL_RSV_GDEMU_SLOT];
        if (slot && slot <= 999) dcql_gdemu_select((uint16_t)slot);
        /* the run's header and report saved where a reboot may leave them; the next loader puts one back (ql-core3) */
        dcache_wback_range(CL_HANDOFF_ADDR, CL_HANDOFF_SIZE);
        *(volatile uint32_t *)0xa0702c00u |= 1;   /* the sound CPU held while its RAM is written */
        const uint32_t rep = H->h.report_len <= CL_REPORT_MAX ? H->h.report_len : CL_REPORT_MAX;
        const uint32_t area = (CL_HANDOFF_ADDR & 0x1fffffffu) | 0xa0000000u;
        dcql_save_handoff(area, CL_STUB_OFF, CL_REPORT_OFF, rep);
        if (dcql_gaps_ready()) {   /* the adapter's memory (ql-core4): the header, then as much report as fits */
            const uint32_t room = DCQL_SAVE_BBA_SIZE - DCQL_SAVE_BBA_REPORT;
            dcql_copy_words(DCQL_SAVE_BBA + DCQL_SAVE_BBA_REPORT, area + CL_REPORT_OFF, ((rep < room ? rep : room) + 3) & ~3u, 1);
            dcql_copy_words(DCQL_SAVE_BBA, area, CL_STUB_OFF, 1);
        }
        dcql_crumb_t c = {0, H->h.run_id, CL_STUB_STEP_RESTART, H->h.state, (uint32_t)H->h.exit_code, H->h.boot_count,
                          H->h.report_len, 0};
        dcql_crumb_put(&c);
    }
    ((void (*)(void))0xa0000000u)();
    for (;;) {}
}

/* the hand-off area must reach RAM before the stub turns the caches off and invalidates them */
static void wback_handoff(void) { dcache_wback_range(CL_HANDOFF_ADDR, CL_HANDOFF_SIZE); }

void __attribute__((noreturn)) dcql_jump_stub(void) {
    irq_disable();
    wdt_disable();
    if ((H->h.flags & CL_F_RET_REBOOT) || !cl_stub_armed(H)) dcql_bios_reboot();
    H->h.flags |= CL_F_STUB_USED;
    cl_handoff_seal(H);
    wback_handoff();
    /* the stub runs uncached (P2): its own code and the image it reads never pass through the caches */
    ((void (*)(void))((CL_STUB_ADDR & 0x1fffffffu) | 0xa0000000u))();
    for (;;) {}
}

/* state: CL_RUN_* to hand in (for a RESET from a program without the client). Returns only if no loader started it. */
int dcql_return_to_loader(int state) {
    if (!cl_handoff_valid(H) || H->h.state != CL_RUN_RUNNING) return -1;
    if (!(H->h.flags & CL_F_RET_REBOOT) && !cl_stub_armed(H)) return -1;
    irq_disable();
    H->h.state = (uint32_t)state;
    H->h.exit_code = 0;
    H->h.report_crc = cl_crc32(0, H->report, H->h.report_len <= CL_REPORT_MAX ? H->h.report_len : 0);
    cl_handoff_seal(H);
    dcql_jump_stub();
}
