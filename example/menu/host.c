/* host.c: the menu on a PC (lane ql-menu's host test). MIT licence.
   Builds lib/dcql_menu.c and demo.c for the PC, plays demo.c's pad script and writes the pictures the console
   example takes (way (b)'s frame-buffer drawing into memory) as raw RGB565 files, plus checks that need no console:
     host <out dir>        pictures <name>.raw (640x480, little-endian RGB565) and qr-*.txt (the QR test texts);
                           exit 1 if a check fails. The link here is a stand-in that reports "Linked". */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "dcql_menu.h"
#include "dcql_net.h"
#include "demo.h"

/* stand-ins for the link, so the title bar reads as on the console once its link is up */
int dcql_net_state(void) { return DCQL_LINK_UP; }
int dcql_net_learning(void) { return 0; }

static uint16_t fb[640 * 480];
static int fails;
static void check(int ok, const char *what) { printf("%s %s\n", ok ? "ok  " : "FAIL", what); if (!ok) fails++; }
static void save(const char *dir, const char *name) {
    char p[512];
    snprintf(p, sizeof p, "%s/%s.raw", dir, name);
    FILE *f = fopen(p, "wb");
    if (!f) { perror(p); exit(2); }
    fwrite(fb, 2, 640 * 480, f);
    fclose(f);
}

int main(int argc, char **argv) {
    const char *out = argc > 1 ? argv[1] : ".";
    dcql_menu_target_t t;
    dcql_menu_fb_t f;
    dcql_menu_fb_target(&t, &f, fb, 640, 640, 480, 0);
    demo_init(0);
    dcql_menu_log("example started");

    /* closed: nothing opens without the whole combo; the combo opens; START closes; a held combo does not reopen */
    check(dcql_menu_input(DCQL_MENU_LT | DCQL_MENU_RT) == 0, "closed: part of the combo does not open");
    check(dcql_menu_input(DCQL_MENU_LT | DCQL_MENU_RT | DCQL_MENU_START) == 1, "the combo opens");
    check(dcql_menu_input(DCQL_MENU_LT | DCQL_MENU_RT) == 1, "still open with START released");
    check(dcql_menu_input(DCQL_MENU_LT | DCQL_MENU_RT | DCQL_MENU_START) == 0, "START closes");
    check(dcql_menu_input(DCQL_MENU_LT | DCQL_MENU_RT | DCQL_MENU_START) == 0, "a combo held since closing does not reopen");
    check(dcql_menu_input(0) == 0 && dcql_menu_input(DCQL_MENU_LT | DCQL_MENU_RT | DCQL_MENU_START) == 1, "a new press reopens");
    dcql_menu_input(0);
    dcql_menu_close();
    dcql_menu_input(0);

    /* the script, as on the console (the console's link wait is not here: the script's clock starts at 0) */
    for (uint32_t k = 0; k <= DEMO_END; k++) {
        dcql_menu_input(demo_pad(k));
        for (int i = 0; i < DEMO_NSHOTS; i++)
            if (DEMO_SHOTS[i].at == k && dcql_menu_is_open()) {
                dcql_menu_draw(&t);
                save(out, DEMO_SHOTS[i].name);
                if (!strcmp(DEMO_SHOTS[i].name, "example")) {   /* the 480i option's still, for the owner's look check */
                    dcql_menu_interlace(1);
                    dcql_menu_draw(&t);
                    save(out, "example-480i");
                    dcql_menu_interlace(0);
                }
                printf("shot %s snapshot \"%s\"\n", DEMO_SHOTS[i].name, dcql_menu_snapshot_text());
            }
    }
    check(demo_scene == 1, "the script's warp chose the checker board");
    check(demo_speed == 4 && demo_spin == 0 && demo_bg == 1, "the script's options: speed 4, spin off, grey");
    check(!dcql_menu_is_open(), "the warp closed the menu");

    /* QR texts: sizes at each version's limit, and one too long */
    static const int LEN[6] = {17, 32, 53, 78, 106, 107};
    for (int i = 0; i < 6; i++) {
        char s[120];
        uint8_t m[37 * 37];
        for (int j = 0; j < LEN[i]; j++) s[j] = (char)('A' + (j * 7 + i) % 26);
        s[LEN[i]] = 0;
        const int n = dcql_menu_qr(s, m, 37);
        char p[512];
        snprintf(p, sizeof p, "%s/qr-%d.txt", out, LEN[i]);
        FILE *q = fopen(p, "w");
        fprintf(q, "%s\n%d\n", s, n);
        for (int r = 0; r < n; r++) { for (int c = 0; c < n; c++) fputc(m[r * n + c] ? '1' : '0', q); fputc('\n', q); }
        fclose(q);
        check(i < 5 ? n == 21 + 4 * i : n == 0, i < 5 ? "QR version by length" : "QR refuses 107 bytes");
    }
    printf("host: %d failed\n", fails);
    return fails ? 1 : 0;
}
