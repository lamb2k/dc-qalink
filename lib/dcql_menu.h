/* dcql_menu.h: dc-qalink's in-game menu (0.2, design B "Bar"), for QA and test builds of any KallistiOS game.
   MIT licence; the font is Spleen 8x16 (BSD 2-Clause, lib/font/LICENSE-spleen.txt). Guide: docs/MENU.md.

     static int warp(const dcql_menu_row_t *r, int v, void *u) { go_to(r->note); return 1; }   // 1 = close the menu
     static int cap = 4, timing = 1;
     static const dcql_menu_row_t GAME[] = {
         DCQL_MENU_HEAD("Chapter 1"),
         DCQL_MENU_ACTION("Village cross", "r101", warp, 0),
         DCQL_MENU_GAP,
         DCQL_MENU_HEAD("Test options"),
         DCQL_MENU_NUMBER("Enemy cap", &cap, 1, 8, 1, 0, 0),
         DCQL_MENU_TOGGLE("Show timing rows", &timing, 0, 0),
     };
     dcql_menu_config_t mc = {.combo = DCQL_MENU_LT | DCQL_MENU_RT | DCQL_MENU_START};
     dcql_menu_init(&mc);
     dcql_menu_add_page("Game", GAME, 6);
     for (;;) {                                          // once a picture:
         uint32_t b = dcql_menu_pad(st->buttons, st->ltrig, st->rtrig);   // the game's own pad read (KOS cont_state_t)
         if (!dcql_menu_input(b)) game_input(st);        // closed: one test, the game plays on
         ... the game's drawing ...
         if (dcql_menu_is_open()) dcql_menu_draw(&my_target);   // way (a): the game's box and glyph calls
     }
   or, with the game stopped between pictures, way (b): dcql_menu_stopped(read_pad, 0) draws straight into the
   displayed frame buffer until the menu closes.

   Limits (dc-qalink plan section 3): no heap (all state is fixed, in .bss), no texture or graphics memory, no network
   or disc access in a picture (the Link page reads the link's counters; connect / hang up only ask the link thread),
   closed cost one test. The link, live data and the way back to the loader are used when the program links them
   (weak references): a program with the menu alone links no network code. */
#ifndef DCQL_MENU_H
#define DCQL_MENU_H
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* buttons: the values of KOS's cont_state_t.buttons, plus the two triggers folded in by dcql_menu_pad() */
#define DCQL_MENU_B      (1u << 1)
#define DCQL_MENU_A      (1u << 2)
#define DCQL_MENU_START  (1u << 3)
#define DCQL_MENU_UP     (1u << 4)
#define DCQL_MENU_DOWN   (1u << 5)
#define DCQL_MENU_LEFT   (1u << 6)
#define DCQL_MENU_RIGHT  (1u << 7)
#define DCQL_MENU_Y      (1u << 9)
#define DCQL_MENU_X      (1u << 10)
#define DCQL_MENU_LT     (1u << 30)
#define DCQL_MENU_RT     (1u << 31)

#define DCQL_MENU_PAGES_MAX 6       /* pages the game registers (plus Link, Log and Snapshot) */
#define DCQL_MENU_LOG_LINES 16      /* the Log page's ring: last lines kept */
#define DCQL_MENU_LOG_COLS 72

enum { DCQL_ROW_GAP = 0, DCQL_ROW_HEAD, DCQL_ROW_ACTION, DCQL_ROW_TOGGLE, DCQL_ROW_CHOICE, DCQL_ROW_NUMBER, DCQL_ROW_INFO };

typedef struct dcql_menu_row dcql_menu_row_t;
/* ACTION: run (v = 0). TOGGLE / CHOICE / NUMBER: called after *value changed to v. Return 1 to close the menu. Runs on
   the thread that calls dcql_menu_input(), between pictures. */
typedef int (*dcql_menu_fn)(const dcql_menu_row_t *row, int v, void *user);
typedef void (*dcql_menu_info_fn)(char *out, int n, void *user);   /* INFO: the value text, at most n-1 chars */
struct dcql_menu_row {
    uint8_t kind;
    const char *label;
    const char *note;               /* ACTION: dim text on the right (e.g. the room) */
    int *value;                     /* TOGGLE 0/1, CHOICE index, NUMBER value */
    int min, max, step;             /* NUMBER; CHOICE: max = names - 1 */
    const char *const *names;       /* CHOICE */
    dcql_menu_fn fn;
    dcql_menu_info_fn info;
    void *user;
};
#define DCQL_MENU_GAP {DCQL_ROW_GAP, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0}
#define DCQL_MENU_HEAD(label) {DCQL_ROW_HEAD, label, 0, 0, 0, 0, 0, 0, 0, 0, 0}
#define DCQL_MENU_ACTION(label, note, fn, user) {DCQL_ROW_ACTION, label, note, 0, 0, 0, 0, 0, fn, 0, user}
#define DCQL_MENU_TOGGLE(label, ptr, fn, user) {DCQL_ROW_TOGGLE, label, 0, ptr, 0, 1, 1, 0, fn, 0, user}
#define DCQL_MENU_CHOICE(label, ptr, names, count, fn, user) {DCQL_ROW_CHOICE, label, 0, ptr, 0, (count) - 1, 1, names, fn, 0, user}
#define DCQL_MENU_NUMBER(label, ptr, lo, hi, step, fn, user) {DCQL_ROW_NUMBER, label, 0, ptr, lo, hi, step, 0, fn, 0, user}
#define DCQL_MENU_INFO(label, info, user) {DCQL_ROW_INFO, label, 0, 0, 0, 0, 0, 0, 0, info, user}

typedef struct {
    uint32_t combo;        /* buttons held together to open (and close); 0 = LT + RT + START */
    const char *title;     /* title bar's first word, default "QA LINK" */
    /* Snapshot page: the game's fields (INFO rows: label + value), shown beside the QR code and packed into it after
       the menu's own (picture, link). At most 8. */
    const dcql_menu_row_t *snapshot;
    int nsnapshot;
    /* Link page: the game's guard or counter rows (INFO), shown under the link's own. At most 4. */
    const dcql_menu_row_t *link_rows;
    int nlink_rows;
    /* Link page "Transport" choice: the link's config (lib/dcql_net.h dcql_net_config_t) the menu changes `net` in
       before it reconnects. NULL: the transport is shown, not chosen. */
    void *net_config;
} dcql_menu_config_t;

/* What way (a) draws through: a filled box and an 8x16 glyph (16 bytes, one a row, bit 7 = left pixel; draw the set
   bits only). Colours are 0xRRGGBB; the menu uses five (dcql_menu_colours). Coordinates are 640x480 pixels. */
typedef struct {
    void (*box)(int x, int y, int w, int h, uint32_t rgb, void *user);
    void (*glyph)(int x, int y, const uint8_t rows[16], uint32_t rgb, void *user);
    void *user;
} dcql_menu_target_t;

enum { DCQL_MENU_BG = 0, DCQL_MENU_BAR, DCQL_MENU_TEXT, DCQL_MENU_DIM, DCQL_MENU_PAPER };
extern const uint32_t dcql_menu_colours[5];   /* black, blue bar, text, dim, the QR code's light paper */

void dcql_menu_init(const dcql_menu_config_t *c);
int dcql_menu_add_page(const char *title, const dcql_menu_row_t *rows, int n);  /* page number, -1 if full */
uint32_t dcql_menu_pad(uint32_t kos_buttons, int ltrig, int rtrig);              /* triggers folded in (pressed >= 128) */
int dcql_menu_input(uint32_t buttons);    /* once a picture, the pad's held buttons; 1 while the menu is open */
int dcql_menu_is_open(void);
void dcql_menu_open(void);
void dcql_menu_close(void);
void dcql_menu_draw(const dcql_menu_target_t *t);   /* while open, after the game's own drawing */
void dcql_menu_log(const char *line);              /* a line for the Log page; any thread; also fits dcql_net's log */
uint32_t dcql_menu_changes(void);                   /* counts every change of what the menu shows (redraw hint) */

/* Way (b): the built-in frame-buffer target (16-bit RGB565 or ARGB1555 pixels; `stride` in pixels). */
typedef struct { uint16_t *pix; int stride, w, h, rgb555; uint16_t pal[5]; } dcql_menu_fb_t;
void dcql_menu_fb_target(dcql_menu_target_t *t, dcql_menu_fb_t *fb, uint16_t *pix, int stride, int w, int h, int rgb555);
/* The displayed frame buffer, read from the video registers (KallistiOS or the game's own PVR set-up). 0 = found;
   -1 = not a 16-bit 640-wide picture. */
int dcql_menu_fb_displayed(dcql_menu_fb_t *fb, dcql_menu_target_t *t);
/* Way (b), modal: the game is stopped; draws straight into the displayed frame buffer (redrawn only when something
   changes, and twice a second for the live values), reads the pad through read_pad once a vertical blank, returns
   when the menu closes. 0 = closed normally, -1 = no usable frame buffer. The game redraws its own picture after. */
int dcql_menu_stopped(uint32_t (*read_pad)(void *user), void *user);

/* For tests and the Snapshot page: the QR code of `text` (byte mode, ECC level L, versions 1 to 5). Fills modules
   (size x size, 1 = dark) and returns size, or 0 if the text is longer than 106 bytes. */
int dcql_menu_qr(const char *text, uint8_t *modules, int max_size);
const char *dcql_menu_snapshot_text(void);           /* what the last Snapshot page's code holds */
/* 480i: each glyph row is also drawn one line lower, so a one-line horizontal stroke shows in both fields of an
   interlaced picture (no stroke lost in a field; a bolder look). DCQL_MENU_AUTO (the default) follows the video mode
   at each draw: on for interlaced output (480i, 576i), off for VGA and progressive. 0 / 1 force it off / on. */
#define DCQL_MENU_AUTO (-1)
void dcql_menu_interlace(int mode);
#ifndef _arch_dreamcast
extern int dcql_menu_host_interlaced;   /* host builds (tests): what AUTO reads as the video mode */
#endif

#ifdef __cplusplus
}
#endif
#endif
