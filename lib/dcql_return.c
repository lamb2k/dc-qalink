/* dcql_return.c: back to the control loop's loader from a running program, through the resident return stub the
 * loader armed in the hand-off area (include/dcql_loop.h, loader/stub.c). MIT licence.
 *
 * Shared by the client library (loader/client.c: normal exit, deadline, crash, abort, RESET) and the text rows
 * (dcql.c: the RESET command of a program that links dc-qalink but not the client). Works from an exception handler:
 * no disc access, no allocation, interrupts off. The stub itself stops the graphics chip, sound, controller and DMA,
 * reads LOADER.BIN from the mounted disc with the BIOS's own disc calls, checks its SHA-256 and runs it.
 */
#include <kos.h>
#include <arch/irq.h>
#include <arch/cache.h>
#include <dc/wdt.h>
#include <string.h>
#include "dcql_loop.h"
#include "dcql_hw.h"

#define H ((cl_handoff_t *)CL_HANDOFF_ADDR)

/* the last rung: a BIOS reboot. With the loader's GDEMU image in the hand-off area it is selected first, as openMenu
   switches discs, so the BIOS boots the loader's image (ql-core2; whether GDEMU's reset_goto overrides it is a console
   question). Harmless without GDEMU (dcql_hw.h). */
void __attribute__((noreturn)) dcql_bios_reboot(void) {
    irq_disable();   /* as arch_reboot */
    if (cl_handoff_valid(H)) {
        const uint32_t slot = H->h.reserved[CL_RSV_GDEMU_SLOT];
        if (slot && slot <= 999) dcql_gdemu_select((uint16_t)slot);
        /* the run's header and report saved where a reboot leaves them; the next loader puts them back (ql-core3) */
        dcache_wback_range(CL_HANDOFF_ADDR, CL_HANDOFF_SIZE);
        *(volatile uint32_t *)0xa0702c00u |= 1;   /* the sound CPU held while its RAM is written */
        dcql_save_handoff((CL_HANDOFF_ADDR & 0x1fffffffu) | 0xa0000000u, CL_STUB_OFF, CL_REPORT_OFF,
                          H->h.report_len <= CL_REPORT_MAX ? H->h.report_len : CL_REPORT_MAX);
    }
    ((void (*)(void))0xa0000000u)();
    for (;;) {}
}

/* the hand-off area must reach RAM before the stub turns the caches off and invalidates them */
static void wback_handoff(void) { dcache_wback_range(CL_HANDOFF_ADDR, CL_HANDOFF_SIZE); }

void __attribute__((noreturn)) dcql_jump_stub(void) {
    irq_disable();
    wdt_disable();
    if (!cl_stub_armed(H)) dcql_bios_reboot();
    H->h.flags |= CL_F_STUB_USED;
    cl_handoff_seal(H);
    wback_handoff();
    /* the stub runs uncached (P2): its own code and the image it reads never pass through the caches */
    ((void (*)(void))((CL_STUB_ADDR & 0x1fffffffu) | 0xa0000000u))();
    for (;;) {}
}

/* state: CL_RUN_* to hand in (for a RESET from a program without the client). Returns only if no loader is armed. */
int dcql_return_to_loader(int state) {
    if (!cl_handoff_valid(H) || H->h.state != CL_RUN_RUNNING || !cl_stub_armed(H)) return -1;
    irq_disable();
    H->h.state = (uint32_t)state;
    H->h.exit_code = 0;
    H->h.report_crc = cl_crc32(0, H->report, H->h.report_len <= CL_REPORT_MAX ? H->h.report_len : 0);
    cl_handoff_seal(H);
    dcql_jump_stub();
}
