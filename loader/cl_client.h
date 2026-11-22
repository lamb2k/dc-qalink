/* cl_client.h: the control loop's client library, for programs the loader runs (dc-qalink 0.2; first written for the
   RE4 Dreamcast console loop, lane eng-loop, 2026-11-19).

   Link client.c, cl_util.c, lib/dcql_return.c and lib/dcql_net.c into the program (loader/client.mk does it). Linking
   client.c also lowers the program's RAM top to the hand-off area (arch_stack_16m), so the report survives it.

     int main() {
         cl_init(0);                         // 1 = started by the loader; 0 = booted any other way (all calls no-op)
         const char *args = cl_args();       // the PC's --args
         ... cl_report("cross mean %u us\n", mean); ...
         cl_exit(0);                         // DONE, back to the loader with the report
     }

   Every way out goes back to the loader through the resident return stub the loader armed (dcql_loop.h): returning
   from main (DONE, 0), cl_exit, a crash (unhandled exception, double fault: CRASHED with the registers), KOS's abort
   and failed asserts (build with -DCL_WRAP_ABORT, link -Wl,--wrap=arch_abort), the PC's --deadline (TIMEOUT, from a
   thread that sleeps once to it) and the PC's RESET (RESET, from the link thread). Without an armed stub (a launcher
   that arms none), the loader image preloaded from /cd/LOADER.BIN at cl_init; without that, a BIOS reboot.

   On the broadband adapter (the loader's transport, unless the arguments say net=0) cl_init also brings up the
   in-game link (lib/dcql_net.c) on the loader's address: RESET from the PC, the /pc file service (files the PC serves,
   opened and read at load time), stills and the program's serial lines live. On the modem the program does not dial.

   The SH-4 watchdog: wdt=1 in the arguments arms it in reset mode, fed from the vertical blank interrupt, so a hang
   with interrupts off resets the CPU (the BIOS boots the mounted disc, the loader). Armed or not, the gaps between
   vertical blanks are measured and reported (the cl-wdt line), so an image can be checked before it is armed. */
#ifndef CL_CLIENT_H
#define CL_CLIENT_H
#include <stddef.h>
#include <stdint.h>

#define CL_NO_PRELOAD 1u     /* cl_init flag: no /cd/LOADER.BIN preload when no stub is armed (reboot instead) */
#define CL_NO_LINK 2u        /* cl_init flag: no in-game link even on the adapter */

int cl_init(unsigned flags);
int cl_active(void);
const char *cl_args(void);
uint32_t cl_run_id(void);
void cl_report(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
void cl_report_bytes(const void *p, size_t n);
void cl_exit(int code) __attribute__((noreturn));

/* the in-game link (adapter only; every call is a no-op without it) */
int cl_link_up(void);                                   /* 1 once the link is up to the PC */
int cl_link_wait(int timeout_ms);                       /* waits until up; 1 = up */
void cl_line(const char *text, size_t n);               /* one serial line to the PC (copied; never blocks) */
/* a still to the PC: RGB565 pixels, row-major, `stride` bytes a row. Blocks until the PC holds it whole (or 10 s).
   Returns 0 = held, <0 = not (no link, or the PC never confirmed). Never call it inside a timed picture. */
int cl_still(const void *pix, unsigned width, unsigned height, unsigned stride, unsigned picture);
typedef struct { unsigned files_opened, files_refused, reads, read_bytes, read_ms, lines, lines_dropped, stills; } cl_link_stats_t;
void cl_link_stats(cl_link_stats_t *s);
#endif
