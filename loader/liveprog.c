/* liveprog.c: the live-data example (dc-qalink 0.2, lane ql-data). MIT licence. Pushed by the bridge to the loader; it
   draws moving colour bars straight into the frame buffer and sends a record a picture (its phase times, counters, a
   room and position), a trace event a second, log lines, and answers warp, option, pause and step. 0.1.0's example
   (example/main.c) stays as it was; this one shows the live additions.

   Arguments (the bridge's args): secs=<n> run time (default 60); slow=<us> a planted busy wait inside drawing (the
   measuring control: slow=2000 must read +2 ms); nolive no dcql_live calls (the cost base). */
#include <kos.h>
#include <dc/biosfont.h>
#include <dc/video.h>
#include <arch/timer.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "cl_client.h"
#include "dcql_live.h"

int __real_cdrom_read_toc(cd_toc_t *toc, bool hd);
int __wrap_cdrom_read_toc(cd_toc_t *toc, bool hd) {
    int rc = __real_cdrom_read_toc(toc, true);
    return rc && !hd ? __real_cdrom_read_toc(toc, false) : rc;
}

static const char DEF[] =
    "program liveprog.elf\n"
    "rate 60\n"
    "phase draw 8000 - Drawing\n"
    "phase logic 2000 - Logic\n"
    "phase live 250 - Live data\n"
    "phase wait - - Pacer wait\n"
    "phase frame 16667 total Picture\n"
    "ctr bars 0 Scene Bars\n"
    "ctr speed 0 Scene Speed\n"
    "ctr late 1 Guards Late pictures\n"
    "ctr dropped 1 Guards Records dropped\n"
    "lane 1 Scene\n"
    "lane 2 Commands\n"
    "preset bars Scene Colour bars\n"
    "preset checker Scene Checker board\n"
    "option speed int 1 8 2 Bar speed\n"
    "option bars int 2 16 8 Bars across\n";

static const uint16_t COL[8] = {0xffff, 0xffe0, 0x07ff, 0x07e0, 0xf81f, 0xf800, 0x001f, 0x0000};
static int g_scene, g_speed = 2, g_bars = 8;
static uint32_t g_late;

static int on_cmd(int code, const char *arg, unsigned len, char *why, void *user) {
    (void)user; (void)len;
    if (code == DCQL_CMD_WARP) {
        if (!strcmp(arg, "bars")) g_scene = 0;
        else if (!strcmp(arg, "checker")) g_scene = 1;
        else { snprintf(why, 60, "no preset %s", arg); return 2; }
        dcql_live_trace(2, 0, arg);
        return 0;
    }
    if (code == DCQL_CMD_OPTION) {
        const char *eq = strchr(arg, '=');
        if (!eq) return 2;
        const int v = atoi(eq + 1);
        if (!strncmp(arg, "speed=", 6) && v >= 1 && v <= 8) g_speed = v;
        else if (!strncmp(arg, "bars=", 5) && v >= 2 && v <= 16) g_bars = v;
        else { snprintf(why, 60, "bad option %s", arg); return 2; }
        return 0;
    }
    if (code == DCQL_CMD_PAD) return 0;
    return 1;
}

static void draw(uint32_t n) {
    /* a 640 x 200 band of moving bars (or a checker board), the top rows keep the text. Each distinct row is built
       once and copied (the first cut divided per pixel: 37 ms a picture on Flycast, every picture late) */
    static uint16_t rows[2][640] __attribute__((aligned(32)));
    const int w = 640 / g_bars;
    for (int x = 0; x < 640; ++x) {
        const int k = ((x + (int)n * g_speed) / w) & 7;
        rows[0][x] = g_scene == 0 ? COL[k] : COL[k & 1 ? 0 : 7];
        rows[1][x] = g_scene == 0 ? COL[k] : COL[(k + 1) & 1 ? 0 : 7];
    }
    for (int y = 120; y < 320; ++y)
        memcpy(vram_s + y * 640, rows[g_scene == 0 ? 0 : (y / 25) & 1], sizeof(rows[0]));
}

static void spin_us(uint32_t us) {
    const uint64_t t = timer_us_gettime64() + us;
    while (timer_us_gettime64() < t) {}
}

int main(int argc, char **argv) {
    (void)argc; (void)argv;
    cl_init(0);
    const char *args = cl_args();
    const char *p;
    int secs = (p = strstr(args, "secs=")) ? atoi(p + 5) : 60;
    const uint32_t slow = (p = strstr(args, "slow=")) ? (uint32_t)atoi(p + 5) : 0;
    const int live = !strstr(args, "nolive");
    for (int i = 0; i < 640 * 120; ++i) vram_s[i] = 0x0841;
    bfont_draw_str_ex(vram_s + 20 * 640 + 20, 640, 0xffe0, 0x0841, 16, true, "dc-qalink live example");
    char line[96];
    snprintf(line, sizeof(line), "args: %.60s", args);
    bfont_draw_str_ex(vram_s + 50 * 640 + 20, 640, 0xffff, 0x0841, 16, true, line);
    const int up = cl_link_wait(10000);
    if (live) {
        dcql_live_declare(DEF);
        dcql_live_on_command(on_cmd, NULL);
    }
    printf("liveprog: link %s, %d s, slow %u us, live %d\n", up ? "up" : "down", secs, (unsigned)slow, live);
    uint64_t sum_draw = 0, sum_live = 0, sum_frame = 0, max_live = 0;
    uint32_t n = 0, measured = 0;
    const uint64_t t_end = timer_ms_gettime64() + (uint64_t)secs * 1000u;
    uint64_t t_prev = timer_us_gettime64();
    uint32_t last_live = 0;
    while (timer_ms_gettime64() < t_end) {
        vid_waitvbl();
        const uint64_t t0 = timer_us_gettime64();
        const uint32_t wait_us = (uint32_t)(t0 - t_prev - 0);
        ++n;
        draw(n);
        if (slow) spin_us(slow);
        const uint64_t t1 = timer_us_gettime64();
        /* "logic": a little arithmetic */
        volatile uint32_t acc = 0;
        for (int i = 0; i < 2000; ++i) acc += (uint32_t)i * n;
        const uint64_t t2 = timer_us_gettime64();
        const uint32_t frame_us = (uint32_t)(t2 - t_prev);
        if (frame_us > 17500 && n > 2) ++g_late;
        if (live) {
            dcql_live_stats_t ls;
            dcql_live_stats(&ls);
            dcql_frame_t f = {.n = n, .nph = 5, .nctr = 4, .has_pos = 1, .room = 0x101,
                              .phase_us = {(uint32_t)(t1 - t0), (uint32_t)(t2 - t1), last_live, wait_us > frame_us ? 0 : wait_us, frame_us},
                              .ctr = {g_bars, g_speed, (int32_t)g_late, (int32_t)ls.recs_dropped},
                              .pos = {(float)(n % 640), 0.0f, (float)g_scene}};
            const uint64_t tl = timer_us_gettime64();
            dcql_live_frame(&f);
            last_live = (uint32_t)(timer_us_gettime64() - tl);
            if (!dcql_live_paused() && last_live > max_live && n > 2) max_live = last_live;
            if (n % 60 == 1) dcql_live_trace(1, 30, g_scene ? "checker second" : "bars second");
            if (n % 120 == 0) dcql_live_log(DCQL_LOG_INFO, "picture %u, speed %d, bars %d", (unsigned)n, g_speed, g_bars);
            if (n % 600 == 0) dcql_live_log(DCQL_LOG_WARN, "late pictures so far: %u", (unsigned)g_late);
        }
        if (n > 30) {
            sum_draw += t1 - t0;
            sum_frame += frame_us;
            if (live && !dcql_live_paused()) { sum_live += last_live; ++measured; }
        }
        t_prev = t2;
        if (n % 60 == 0) {
            snprintf(line, sizeof(line), "picture %6u  speed %d  bars %2d  %s   ", (unsigned)n, g_speed, g_bars,
                     g_scene ? "checker" : "bars   ");
            bfont_draw_str_ex(vram_s + 80 * 640 + 20, 640, 0x07ff, 0x0841, 16, true, line);
        }
    }
    const uint32_t m = n > 30 ? n - 30 : 1;
    cl_report("liveprog: %u pictures, slow %u us, live %d\n", (unsigned)n, (unsigned)slow, live);
    cl_report("@ Pictures = %u n\n", (unsigned)n);
    cl_report("@ Drawing mean = %.3f ms\n", (double)sum_draw / m / 1000.0);
    cl_report("@ Picture mean = %.3f ms\n", (double)sum_frame / m / 1000.0);
    if (live) {
        dcql_live_stats_t ls;
        dcql_live_stats(&ls);
        cl_report("@ Live copy mean = %.2f us\n", measured ? (double)sum_live / measured : 0.0);
        cl_report("@ Live copy worst = %u us\n", (unsigned)max_live);
        cl_report("@ Link sends per picture = %.2f us\n", ls.frames ? (double)ls.pump_us_total / ls.frames : 0.0);
    }
    cl_report("@ Late pictures = %u n\n", (unsigned)g_late);
    cl_exit(0);
}
