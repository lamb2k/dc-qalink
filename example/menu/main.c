/* main.c: dc-qalink's in-game menu example (0.2, design B). MIT licence.

   A small "game" (moving colour bars, a checker board or spinning squares on the PVR) with the menu on top:
     way (a): while the game runs, the menu draws through two calls this program supplies, here untextured PVR
              sprites (a box is one sprite; a glyph is a few sprites, one a block of set pixels): no texture memory;
     way (b): with the game stopped, dcql_menu_stopped() draws straight into the displayed frame buffer.
   The menu opens on L + R + START. The link (dcql.h) comes up on the broadband adapter when the program is built with
   EX_LINK=1 (the default): its state and counters show on the Link page and its log lines on the Log page.

   EX_SCRIPT=1 (the lane's Flycast runs): the pad is demo.c's script, not the controller; stills and a clip are sent
   over the serial line (QLSHOT, rows of the displayed frame buffer, run-length coded, only rows that changed), and the
   limits are measured and printed (QLMEAS): the menu closed, the menu open both ways, a planted 2 ms delay (the timing
   control), heap use over 100 opens and closes (and a planted allocation, the heap control). */
#include <kos.h>
#include <malloc.h>
#include <dc/perfctr.h>
#include <stdio.h>
#include <string.h>
#include "dcql_menu.h"
#include "demo.h"
#if EX_LINK
#include "dcql.h"
#endif
#if EX_CL   /* the build with the control loop's client library: the in-game link and live data (menu-cl.elf) */
#include "cl_client.h"
#include "dcql_live.h"
static const char LIVE_DEF[] = "program menu-cl.elf\nrate 60\nphase draw 8000 - Drawing\nphase menu 2000 - Menu\n"
                                "ctr late 1 Guards Late pictures\n";
#endif
#ifndef EX_SCRIPT
#define EX_SCRIPT 0
#endif

static uint64_t now_ns(void) { return perf_cntr_timer_ns(); }

/* ------------------------------------------------------------------ way (a): untextured PVR sprites */
static pvr_sprite_hdr_t g_hdr;
static uint32_t g_rgb = 0xffffffffu, g_sprites;
static float g_z;
static void spin_ns(int ns) { const uint64_t t = now_ns() + (uint64_t)ns; while (now_ns() < t) {} }
static void spr_colour(uint32_t rgb) {
    if (rgb == g_rgb) return;
    g_rgb = rgb;
    ((uint32_t *)&g_hdr)[4] = 0xff000000u | rgb;   /* a sprite header's colour word */
    pvr_prim(&g_hdr, sizeof g_hdr);
}
static void spr(float x, float y, float w, float h) {
    pvr_sprite_col_t v;
    g_z += 0.0001f;
    v.flags = PVR_CMD_VERTEX_EOL;
    v.ax = x; v.ay = y; v.az = g_z;
    v.bx = x + w; v.by = y; v.bz = g_z;
    v.cx = x + w; v.cy = y + h; v.cz = g_z;
    v.dx = x; v.dy = y + h;
    v.d1 = v.d2 = v.d3 = v.d4 = 0;
    pvr_prim(&v, sizeof v);
    g_sprites++;
}
static void spr_box(int x, int y, int w, int h, uint32_t rgb, void *u) {
    (void)u;
    spr_colour(rgb);
    spr((float)x, (float)y, (float)w, (float)h);
}
static void spr_glyph(int x, int y, const uint8_t rows[16], uint32_t rgb, void *u) {
    (void)u;
    uint8_t done[16] = {0};
    spr_colour(rgb);
    for (int r = 0; r < 16; r++) {
        unsigned bits = rows[r] & ~done[r] & 0xffu;
        while (bits) {
            int c0 = 0;
            while (!(bits & (0x80u >> c0))) c0++;
            int c1 = c0;
            while (c1 < 8 && (rows[r] & (0x80u >> c1))) c1++;   /* the run in the glyph row */
            const unsigned mask = (0xffu >> c0) & ~(0xffu >> c1) & 0xffu;
            int h = 1;
            while (r + h < 16 && (rows[r + h] & mask) == mask) { done[r + h] |= (uint8_t)mask; h++; }
            spr((float)(x + c0), (float)(y + r), (float)(c1 - c0), (float)h);
            bits &= ~mask;
        }
    }
}
static const dcql_menu_target_t SPRITES = {spr_box, spr_glyph, 0};

/* ------------------------------------------------------------------ the "game" */
static void quad(float x, float y, float w, float h, float z, uint32_t argb) {
    pvr_vertex_t v[4];
    const float xs[4] = {x, x + w, x, x + w}, ys[4] = {y, y, y + h, y + h};
    for (int i = 0; i < 4; i++) {
        v[i].flags = i == 3 ? PVR_CMD_VERTEX_EOL : PVR_CMD_VERTEX;
        v[i].x = xs[i]; v[i].y = ys[i]; v[i].z = z; v[i].u = v[i].v = 0; v[i].argb = argb; v[i].oargb = 0;
    }
    pvr_prim(v, sizeof v);
}
static void game_draw(uint32_t pic) {
    static pvr_poly_hdr_t hdr;
    static int ready;
    if (!ready) {
        pvr_poly_cxt_t cxt;
        pvr_poly_cxt_col(&cxt, PVR_LIST_OP_POLY);
        pvr_poly_compile(&hdr, &cxt);
        ready = 1;
    }
    static const uint32_t COL[8] = {0xffffffff, 0xffffff00, 0xff00ffff, 0xff00ff00, 0xffff00ff, 0xffff0000, 0xff0000ff, 0xff202020};
    const float bg[3][3] = {{0, 0, 0}, {0.3f, 0.3f, 0.3f}, {0, 0, 0.35f}};
    pvr_set_bg_color(bg[demo_bg][0], bg[demo_bg][1], bg[demo_bg][2]);
    pvr_prim(&hdr, sizeof hdr);
    const int t = (int)(pic * (uint32_t)demo_speed);
    if (demo_scene == 0) {
        for (int i = 0; i < 8; i++) quad((float)((i * 80 + t) % 640), 60, 80, 360, 1.0f, COL[i]);
    } else if (demo_scene == 1) {
        for (int r = 0; r < 6; r++)
            for (int c = 0; c < 8; c++)
                if ((r + c + (t / 60)) & 1) quad((float)(c * 80), (float)(r * 80), 80, 80, 1.0f, 0xffc0c0c0);
    } else {
        for (int i = 0; i < 4; i++) {
            const float s = 60 + 20 * i, ph = demo_spin ? (float)t * 0.02f + (float)i : (float)i;
            const float cx = 160 + 110 * i, cy = 240, c = fcos(ph), sn = fsin(ph);
            pvr_vertex_t v[4];
            const float px[4] = {-s, s, -s, s}, py[4] = {-s, -s, s, s};
            for (int k = 0; k < 4; k++) {
                v[k].flags = k == 3 ? PVR_CMD_VERTEX_EOL : PVR_CMD_VERTEX;
                v[k].x = cx + px[k] * c - py[k] * sn; v[k].y = cy + px[k] * sn + py[k] * c; v[k].z = 1.0f + (float)i;
                v[k].u = v[k].v = 0; v[k].argb = COL[i + 1]; v[k].oargb = 0;
            }
            pvr_prim(v, sizeof v);
        }
    }
}

/* ------------------------------------------------------------------ stills over the serial line */
static uint32_t g_rowhash[480];
static uint8_t g_rle[640 * 3];
static char g_b64[640 * 4 + 8];
static void b64(const uint8_t *p, int n, char *o) {
    static const char A[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    int k = 0;
    for (int i = 0; i < n; i += 3) {
        const uint32_t v = (uint32_t)p[i] << 16 | (i + 1 < n ? (uint32_t)p[i + 1] << 8 : 0) | (i + 2 < n ? p[i + 2] : 0);
        o[k++] = A[v >> 18]; o[k++] = A[(v >> 12) & 63];
        o[k++] = i + 1 < n ? A[(v >> 6) & 63] : '='; o[k++] = i + 2 < n ? A[v & 63] : '=';
    }
    o[k] = 0;
}
static void shot(const char *name) {
    dcql_menu_fb_t f;
    dcql_menu_target_t t;
    if (dcql_menu_fb_displayed(&f, &t) < 0) { printf("QLSHOT %s none\n", name); return; }
    printf("QLSHOT %s 640 480 565\n", name);
    int changed = 0;
    for (int y = 0; y < 480; y++) {
        const uint16_t *row = f.pix + y * f.stride;
        uint32_t h = 2166136261u;
        for (int x = 0; x < 640; x++) h = (h ^ row[x]) * 16777619u;
        if (h == g_rowhash[y]) continue;
        g_rowhash[y] = h;
        int n = 0;
        for (int x = 0; x < 640;) {
            int e = x + 1;
            while (e < 640 && e - x < 256 && row[e] == row[x]) e++;
            g_rle[n++] = (uint8_t)(e - x - 1); g_rle[n++] = (uint8_t)row[x]; g_rle[n++] = (uint8_t)(row[x] >> 8);
            x = e;
        }
        b64(g_rle, n, g_b64);
        printf("R%d %s\n", y, g_b64);
        changed++;
    }
    printf("QLEND %s %d\n", name, changed);
}
static void after_render(void) {   /* the last submitted picture is on screen */
    pvr_wait_ready();
    vid_waitvbl();
    vid_waitvbl();
}

/* ------------------------------------------------------------------ pictures */
static uint32_t g_pic;
static int g_menu_way_a_ns;
static void picture(uint32_t pad) {
    dcql_menu_input(pad);
    pvr_wait_ready();
    pvr_scene_begin();
    pvr_list_begin(PVR_LIST_OP_POLY);
    const uint64_t t0 = now_ns();
    game_draw(g_pic);
    const uint64_t t1 = now_ns();
    demo_draw_us = (unsigned)((t1 - t0) / 1000);
    g_menu_way_a_ns = 0;
    if (dcql_menu_is_open()) {
        pvr_sprite_cxt_t cxt;
        pvr_sprite_cxt_col(&cxt, PVR_LIST_OP_POLY);
        pvr_sprite_compile(&g_hdr, &cxt);
        g_rgb = 0xffffffffu; g_z = 100.0f; g_sprites = 0;
        dcql_menu_draw(&SPRITES);
        g_menu_way_a_ns = (int)(now_ns() - t1);
    }
    pvr_list_finish();
    pvr_scene_finish();
#if EX_CL
    dcql_frame_t lf = {0};
    lf.nph = 2; lf.phase_us[0] = demo_draw_us; lf.phase_us[1] = (uint32_t)g_menu_way_a_ns / 1000;
    lf.nctr = 1; lf.ctr[0] = (int32_t)demo_late;
    dcql_live_frame(&lf);
#endif
    g_pic++;
}

#if EX_SCRIPT
static uint32_t g_stop_tick;
static uint32_t stopped_pad(void *u) {   /* way (b)'s script; stills from inside the stopped loop */
    (void)u;
    const uint32_t k = g_stop_tick++;
    if (k == 30) shot("stopped-example");
    if (k == 60) shot("stopped-log");
    if (k == 40 || k == 41) return DCQL_MENU_RT;
    if (k == 70 || k == 71) return DCQL_MENU_START;
    return 0;
}
static void stats(const char *what, const int *ns, int n) {
    long long sum = 0; int worst = 0;
    for (int i = 0; i < n; i++) { sum += ns[i]; if (ns[i] > worst) worst = ns[i]; }
    printf("QLMEAS %s mean_us %lld worst_us %d n %d\n", what, sum / n / 1000, worst / 1000, n);
}
static void measure(void) {
    static int ns[64];
    /* closed: one test. The loop's own cost is measured the same way and taken off. */
    dcql_menu_close();
    const int N = 1000000;
    uint64_t t0 = now_ns();
    for (int i = 0; i < N; i++) dcql_menu_input(0);
    uint64_t t1 = now_ns();
    for (int i = 0; i < N; i++) __asm__ volatile("" ::: "memory");
    uint64_t t2 = now_ns();
    printf("QLMEAS closed_ns_per_call %llu loop_ns %llu calls %d\n", (unsigned long long)((t1 - t0) * 1000 / N),
           (unsigned long long)((t2 - t1) * 1000 / N), N);
    printf("QLMEAS closed_unit ns_x1000\n");
    /* open, way (a), each page: CPU time to submit the menu's sprites (20 pictures a page) */
    dcql_menu_open();
    static const char *const PAGES[4] = {"link", "example", "log", "snapshot"};
    for (int p = 0; p < 4; p++) {
        unsigned spr = 0;
        for (int i = 0; i < 20; i++) { picture(0); ns[i] = g_menu_way_a_ns; spr = g_sprites; }
        char w[48];
        snprintf(w, sizeof w, "open_a_%s", PAGES[p]);
        stats(w, ns, 20);
        printf("QLMEAS open_a_%s sprites %u vertex_bytes %u\n", PAGES[p], spr, spr * 64u);
        picture(DCQL_MENU_RT); picture(0);
    }
    /* back to Link; way (b) into the displayed frame buffer, each page (20 draws a page) */
    after_render();
    dcql_menu_fb_t f;
    dcql_menu_target_t t;
    dcql_menu_fb_displayed(&f, &t);
    for (int p = 0; p < 4; p++) {
        for (int i = 0; i < 20; i++) { const uint64_t a = now_ns(); dcql_menu_draw(&t); ns[i] = (int)(now_ns() - a); }
        char w[48];
        snprintf(w, sizeof w, "open_b_%s", PAGES[p]);
        stats(w, ns, 20);
        dcql_menu_input(DCQL_MENU_RT); dcql_menu_input(0);
    }
    /* the timing control: a planted 2 ms busy wait in each draw must read 2 ms more (way b, Link page) */
    {
        int base[20], plant[20];
        for (int i = 0; i < 20; i++) { const uint64_t a = now_ns(); dcql_menu_draw(&t); base[i] = (int)(now_ns() - a); }
        for (int i = 0; i < 20; i++) {
            const uint64_t a = now_ns();
            spin_ns(2000000);
            dcql_menu_draw(&t);
            plant[i] = (int)(now_ns() - a);
        }
        long long sb = 0, sp = 0;
        for (int i = 0; i < 20; i++) { sb += base[i]; sp += plant[i]; }
        printf("QLMEAS control_planted_2000us reads_us %lld\n", (sp - sb) / 20 / 1000);
    }
    /* heap: 100 opens, draws and closes, no change; the control plants a 64-byte allocation each time */
    struct mallinfo m0 = mallinfo();
    for (int i = 0; i < 100; i++) { dcql_menu_open(); dcql_menu_draw(&t); dcql_menu_input(DCQL_MENU_RT); dcql_menu_input(0); dcql_menu_close(); }
    struct mallinfo m1 = mallinfo();
    for (int i = 0; i < 100; i++) { dcql_menu_open(); dcql_menu_draw(&t); if (!malloc(64)) break; dcql_menu_close(); }
    struct mallinfo m2 = mallinfo();
    printf("QLMEAS heap_100_open_close_delta %d control_planted_alloc_delta %d heap_in_use %d\n", m1.uordblks - m0.uordblks,
           m2.uordblks - m1.uordblks, m1.uordblks);
    dcql_menu_close();
}
#endif

#if EX_LINK
static void link_log(const char *line) { dcql_menu_log(line); }
#endif

static __attribute__((unused)) uint32_t maple_pad(void *u) {
    (void)u;
    maple_device_t *d = maple_enum_type(0, MAPLE_FUNC_CONTROLLER);
    cont_state_t *s = d ? (cont_state_t *)maple_dev_status(d) : 0;
    return s ? dcql_menu_pad(s->buttons, s->ltrig, s->rtrig) : 0;
}

int main(int argc, char **argv) {
    (void)argc; (void)argv;
    pvr_init_params_t params = {{PVR_BINSIZE_16, PVR_BINSIZE_0, PVR_BINSIZE_0, PVR_BINSIZE_0, PVR_BINSIZE_0}, 512 * 1024, 0, 0, 0, 3};
    pvr_init(&params);
    perf_cntr_timer_enable();
#if EX_CL
    cl_init(0);
    dcql_live_declare(LIVE_DEF);
#endif
    demo_init(0);
    struct mallinfo heap0 = mallinfo();
#if EX_LINK
    dcql_config_t lc = {0};
    lc.net = 2;
    lc.host = EX_PC;
    lc.port = EX_PORT;
    lc.lport = EX_LPORT;
    lc.ip = EX_IP;
    lc.mask = "255.255.255.0";
    lc.gw = "192.168.1.1";
    lc.log = link_log;
    dcql_config(&lc);
    dcql_start();
#endif
    dcql_menu_log("example started");
#if EX_SCRIPT
    printf("QLSTART menu example\n");
    /* the script's clock starts once the link is up (10 s at most), so the stills show it */
    for (int i = 0; i < 600; i++) {
#if EX_LINK
        if (dcql_state() == DCQL_UP) break;
#endif
        picture(0);
    }
    printf("QLMEAS link_wait_pictures %u\n", (unsigned)g_pic);
    const uint32_t base = g_pic;
    for (uint32_t k = 0; k <= DEMO_END; k++) {
        picture(demo_pad(k));
        int s = -1;
        for (int i = 0; i < DEMO_NSHOTS; i++) if (DEMO_SHOTS[i].at == k) s = i;
        if (s >= 0 || (k >= 56 && k < 440 && k % 3 == 0)) {
            after_render();
            char name[24];
            snprintf(name, sizeof name, "clip-%04u", (unsigned)k);
            shot(s >= 0 ? DEMO_SHOTS[s].name : name);
            if (s >= 0) printf("QLSNAP %s\n", dcql_menu_snapshot_text());
            if (s >= 0 && dcql_menu_is_open()) {   /* the same state through way (b), for the (a) / (b) comparison */
                dcql_menu_fb_t f;
                dcql_menu_target_t t;
                if (dcql_menu_fb_displayed(&f, &t) == 0) {
                    dcql_menu_draw(&t);
                    snprintf(name, sizeof name, "%s-b", DEMO_SHOTS[s].name);
                    shot(name);
                }
            }
        }
    }
    (void)base;
    measure();
    /* way (b): the game stopped, the menu straight into the displayed frame buffer */
    after_render();
    memset(g_rowhash, 0, sizeof g_rowhash);
    const int rc = dcql_menu_stopped(stopped_pad, 0);
    struct mallinfo heap1 = mallinfo();
    printf("QLMEAS stopped_rc %d heap_after_start_delta %d\n", rc, heap1.uordblks - heap0.uordblks);
    printf("QLDONE\n");
    for (;;) picture(0);
#else
    (void)heap0;
    for (;;) {
        const uint32_t pad = maple_pad(0);
        if (!dcql_menu_is_open() && (pad & DCQL_MENU_Y) && (pad & DCQL_MENU_X)) dcql_menu_stopped(maple_pad, 0);   /* X + Y: way (b) */
        picture(pad);
    }
#endif
    return 0;
}
