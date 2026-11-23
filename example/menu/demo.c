/* demo.c: the menu example's page, snapshot fields and pad script, shared by the console program (main.c) and the
   host test (host.c), so both draw the same pictures. MIT licence. */
#include <stdio.h>
#include <string.h>
#include "dcql_menu.h"
#include "demo.h"

int demo_scene, demo_speed = 2, demo_spin = 1, demo_bg, demo_timing = 1;
uint32_t demo_late;
unsigned demo_draw_us;
static const char *const SCENES[3] = {"Colour bars", "Checker board", "Spinning squares"};
static const char *const BGS[3] = {"Black", "Grey", "Blue"};

static int warp(const dcql_menu_row_t *r, int v, void *u) {
    (void)v;
    demo_scene = (int)(intptr_t)u;
    char line[64];
    snprintf(line, sizeof line, "warp %s (%s)", r->note, r->label);
    dcql_menu_log(line);
    return 1;   /* close the menu: back to the game in the new scene */
}
static int changed(const dcql_menu_row_t *r, int v, void *u) {
    (void)u;
    char line[64];
    snprintf(line, sizeof line, "option %s = %d", r->label, v);
    dcql_menu_log(line);
    return 0;
}
static const dcql_menu_row_t GAME[] = {
    DCQL_MENU_HEAD("Scenes"),
    DCQL_MENU_ACTION("Colour bars", "bars", warp, (void *)0),
    DCQL_MENU_ACTION("Checker board", "checker", warp, (void *)1),
    DCQL_MENU_ACTION("Spinning squares", "squares", warp, (void *)2),
    DCQL_MENU_GAP,
    DCQL_MENU_HEAD("Test options"),
    DCQL_MENU_NUMBER("Speed", &demo_speed, 1, 8, 1, changed, 0),
    DCQL_MENU_TOGGLE("Spin", &demo_spin, changed, 0),
    DCQL_MENU_CHOICE("Background", &demo_bg, BGS, 3, changed, 0),
    DCQL_MENU_TOGGLE("Show timing rows", &demo_timing, changed, 0),
};
static void i_scene(char *o, int n, void *u) { (void)u; snprintf(o, n, "%s", SCENES[demo_scene]); }
static void i_draw(char *o, int n, void *u) { (void)u; snprintf(o, n, "%u.%02u ms", demo_draw_us / 1000, (demo_draw_us % 1000) / 10); }
static void i_speed(char *o, int n, void *u) { (void)u; snprintf(o, n, "%d", demo_speed); }
static void i_guards(char *o, int n, void *u) { (void)u; snprintf(o, n, "late pictures %u", (unsigned)demo_late); }
static const dcql_menu_row_t SNAP[] = {
    DCQL_MENU_INFO("Scene", i_scene, 0),
    DCQL_MENU_INFO("Drawing", i_draw, 0),
    DCQL_MENU_INFO("Speed", i_speed, 0),
};
static const dcql_menu_row_t LINKROWS[] = {DCQL_MENU_INFO("Guards", i_guards, 0)};

void demo_init(void *net_config) {
    dcql_menu_config_t c = {0};
    c.combo = DCQL_MENU_LT | DCQL_MENU_RT | DCQL_MENU_START;
    c.snapshot = SNAP;
    c.nsnapshot = 3;
    c.link_rows = LINKROWS;
    c.nlink_rows = 1;
    c.net_config = net_config;
    dcql_menu_init(&c);
    dcql_menu_add_page("Example", GAME, (int)(sizeof GAME / sizeof GAME[0]));
}

/* The pad script: at picture `at` the buttons are held for `hold` pictures. Walk: open on the combo (Link page),
   R to the example page, down twice, A on Speed... then R to Log and Snapshot, START to close, the combo again,
   a warp (closes the menu). Stills are taken at the pictures in SHOTS. */
#define LT DCQL_MENU_LT
#define RT DCQL_MENU_RT
static const struct { uint32_t at, hold, b; } SCRIPT[] = {
    {60, 6, LT | RT | DCQL_MENU_START},   /* open: Link page */
    {100, 2, RT},                         /* Example page */
    {120, 2, DCQL_MENU_DOWN},
    {130, 2, DCQL_MENU_DOWN},             /* "Spinning squares" selected */
    {150, 2, DCQL_MENU_DOWN},             /* Speed */
    {160, 2, DCQL_MENU_RIGHT},            /* Speed 3 */
    {170, 2, DCQL_MENU_RIGHT},            /* Speed 4 */
    {180, 2, DCQL_MENU_DOWN},             /* Spin */
    {190, 2, DCQL_MENU_A},                /* Spin off */
    {200, 2, DCQL_MENU_DOWN},             /* Background */
    {210, 2, DCQL_MENU_RIGHT},            /* Grey */
    {240, 2, RT},                         /* Log page */
    {270, 2, RT},                         /* Snapshot page */
    {300, 2, DCQL_MENU_START},            /* close */
    {340, 6, LT | RT | DCQL_MENU_START},  /* open again: back on Snapshot */
    {360, 2, LT},                         /* Log */
    {370, 2, LT},                         /* Example */
    {380, 2, DCQL_MENU_UP},
    {390, 2, DCQL_MENU_UP},
    {400, 2, DCQL_MENU_UP},
    {410, 2, DCQL_MENU_UP},               /* "Checker board" */
    {430, 2, DCQL_MENU_A},                /* warp: closes */
};
const demo_shot_t DEMO_SHOTS[] = {
    {90, "link"}, {222, "example"}, {260, "log"}, {290, "snapshot"}, {420, "example-2"}, {460, "closed"},
};
const int DEMO_NSHOTS = (int)(sizeof DEMO_SHOTS / sizeof DEMO_SHOTS[0]);
const uint32_t DEMO_END = 480;

uint32_t demo_pad(uint32_t picture) {
    for (unsigned i = 0; i < sizeof SCRIPT / sizeof SCRIPT[0]; i++)
        if (picture >= SCRIPT[i].at && picture < SCRIPT[i].at + SCRIPT[i].hold) return SCRIPT[i].b;
    return 0;
}
