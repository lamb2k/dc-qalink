# The in-game menu

A small menu for QA and test builds of any KallistiOS game: the link's state and counters, the last log lines, a QR
code of a state snapshot, and pages the game registers (warps, test options, anything with a row). It looks like
dc-qalink's design B ("Bar": black, a blue title bar and hint bar, white text, dim labels) in Spleen 8x16, the same
words as Studio. It needs no heap, no texture memory and none of the game's art, and a closed menu costs one test a
picture.

Files: `lib/dcql_menu.c`, `lib/dcql_menu.h`, `lib/font/` (font, generator, licence). Example: `example/menu/`.

## Add it

```c
#include "dcql_menu.h"

static int warp(const dcql_menu_row_t *r, int v, void *u) { go_to_room(r->note); return 1; }  /* 1 = close */
static int cap = 4, timing = 1;
static const dcql_menu_row_t PAGE[] = {
    DCQL_MENU_HEAD("Chapter 1"),
    DCQL_MENU_ACTION("Village cross", "r101", warp, 0),
    DCQL_MENU_GAP,
    DCQL_MENU_HEAD("Test options"),
    DCQL_MENU_NUMBER("Enemy cap", &cap, 1, 8, 1, 0, 0),
    DCQL_MENU_TOGGLE("Show timing rows", &timing, 0, 0),
};

dcql_menu_config_t mc = {.combo = DCQL_MENU_LT | DCQL_MENU_RT | DCQL_MENU_START};
dcql_menu_init(&mc);
dcql_menu_add_page("Game", PAGE, 6);

/* once a picture, with the pad state the game already read (KOS cont_state_t) */
uint32_t b = dcql_menu_pad(st->buttons, st->ltrig, st->rtrig);
if (!dcql_menu_input(b)) game_input(st);         /* closed: one test, the game plays on */
...
if (dcql_menu_is_open()) dcql_menu_draw(&target); /* way (a), after the game's own drawing */
```

Build: with `lib/dcql.mk`, set `DCQL_MENU=1` (the menu object joins `DCQL_OBJS`); a program that uses the control
loop's client library instead adds `$(DCQL_MENU_OBJS)` to its objects. The menu reads the pad only through
`dcql_menu_input()`; it never touches the maple bus (the example's `maple_pad()` shows the few lines a game needs).

## Pages

| Page | Shows | Rows |
|---|---|---|
| Link | State (Linked, Unlinked, Dialling, Connecting, Waiting for PC, Link lost, Failed, Hanging up), transport (Modem, Broadband adapter), the PC's address, the console's, datagrams, records and log lines (with live data), the game's guard rows, the last 3 log lines | Connect / Hang up; Transport on connect (when the game gives its `dcql_net_config_t`); Back to the loader (when the program links the way back) |
| the game's | Up to 6 pages | Head, gap, action (with a dim note on the right), toggle (On / Off), choice, number, info |
| Log | The last 16 lines (`dcql_menu_log()`, which also fits `dcql_net`'s and `dcql.h`'s log callback) | - |
| Snapshot | A QR code (versions 1 to 5, level L, up to 106 bytes) and the values in it: Picture (with live data), Link, then the game's snapshot rows | (A) takes a new snapshot |

Order: Link, the game's pages, Log, Snapshot; L and R step through them. A on a row runs or changes it, left and right
change a value, up and down move (held: repeats after 20 pictures, every 4), B or START closes. The combo opens it
again on the page it was on; a combo still held from closing does not reopen it.

The snapshot text is `QL1;key=value;...` (keys in lower case, spaces as `_`), for example
`QL1;link=Linked;scene=Colour bars;drawing=0.01 ms;speed=4`. What does not fit in a version 5 code is left out of the
code (the page still shows it).

## Two ways to draw

- **(a) Through the game's calls**, while the game runs: `dcql_menu_target_t` has `box(x, y, w, h, rgb)` and
  `glyph(x, y, rows[16], rgb)` (8x16, one byte a row, bit 7 the left pixel, draw the set bits). Five colours,
  `dcql_menu_colours`. The example draws them as untextured PVR sprites (one for a box, one for each block of set pixels
  in a glyph): no texture memory; about 750 to 1,170 sprites, 48 to 75 KB of vertex data a picture, so a game's
  vertex buffer must have that room while the menu is open. A game with its own font texture can map `glyph()` to it.
- **(b) Straight into the frame buffer**, with the game stopped: `dcql_menu_stopped(read_pad, user)` finds the
  displayed frame buffer from the video registers (16-bit, 640 wide), draws the menu only when something changes (and
  twice a second for live values), reads the pad once a vertical blank through the game's call, and returns when the
  menu closes. Call it between pictures (after `pvr_wait_ready()` when the game uses the PVR). The game redraws its own
  picture afterwards. `dcql_menu_fb_target()` gives the same drawing for any 16-bit buffer.

## Limits (plan section 3)

Measured by `example/menu/limits.py` (from the link maps, after garbage collection) and the example's `EX_SCRIPT=1`
run (`QLMEAS` lines). Tiers: **map** (exact, from the linker), **Flycast** (plain emulator, function checks only; no
speed claim), **estimate**.

| Limit (plan) | Measured | Tier |
|---|---|---|
| Code and constant data, link + live + menu: at most 48 KB | menu alone 10,018 B (9.8 KB: code 8,016, constants 2,002 of which the font is 1,520); with the client library's link and live data 27,355 B (26.7 KB): within | map |
| Fixed memory: at most 72 KB | menu alone 3,988 B (state, log ring 1.2 KB, QR modules 1.4 KB; the font is constant data); with link + live 128,820 B (125.8 KB): over, because the link and live data alone take 124,832 B | map |
| Heap use after start: none | 0 B change over 100 opens, draws and closes (control: a planted 64-byte allocation in the same loop reads +7,208 B); the menu's object refers to no allocator | Flycast; object |
| Cost each picture, menu closed: one test, under 0.01 ms | 200.9 ns a call (0.0002 ms; the empty loop 5.0 ns), one compare and branch | Flycast |
| Cost each picture, menu open | way (a), the example's PVR sprites: 1.29 to 2.01 ms of CPU a picture (worst 2.01 ms), 748 to 1,167 sprites, 48 to 75 KB of vertex data; way (b), into the displayed frame buffer: 8.3 to 8.9 ms a full redraw (worst 11.2 ms), done only when something changes | Flycast |
| Network or disc access inside a picture: none | none referenced by the menu's object (symbol check; the control, dcql_net.o, fails it) | object |
| Graphics memory: none | none (no texture or `pvr_mem` call); way (a) uses vertex-buffer room only | object |

The timing control: a planted 2,000 us busy wait reads 1,998 us (run f3), 1,995 us (f2) and 2,003 us (f1). Flycast times are
the emulator's, not the console's: the calibrated tier needs a console-like Flycast with the adapter, and the
console's own figures (store-queue writes of the sprites, uncached frame-buffer writes, the chip's sort and fill of
about a thousand sprites) are for the console run.

## 480i

Spleen draws most horizontal strokes one line thick, so at 480i each such stroke would show in only one of the two
fields (it flickers at 30 Hz on a CRT, and one field alone reads badly). So the menu follows the video mode by default
(`DCQL_MENU_AUTO`, read from the video registers at each draw): on interlaced output (480i, 576i) it draws each glyph
row twice (one line lower too), so both fields carry every stroke; on VGA and progressive output it draws the font as
approved. A game can force either: `dcql_menu_interlace(0)` or `dcql_menu_interlace(1)` (owner, 2026-11-22: design B
approved as drawn, with this default).

## Checks

- `make OUT=<dir> host` in `example/menu`, then `<dir>/menu-host <out>`: the combo (part of it does not open, a held
  one does not reopen), 480i in both video modes (AUTO equals forced on when interlaced and the approved drawing when
  progressive; forced off stays off; forced on differs, the control), the example's pad script (its warp and options land), the QR sizes by length and the refusal
  over 106 bytes. Then `shots.py host <out>`: every QR size decodes with an independent decoder (zxing-cpp) to its
  text, and a damaged copy (the control) does not.
- `limits.py <menu.map> <menu-cl.map> <dcql_menu.o>`: the table above and the symbol check (no heap, disc, network or
  graphics memory referenced by the menu's object); `limits.py --control-symbols dcql_net.o` must fail it.
- The example with `EX_SCRIPT=1` in an emulator: stills of each page through both ways, compared pixel for pixel
  with each other and with the host's drawing; the Snapshot still's QR decodes to the text the console printed; the
  timing control (a planted 2 ms delay reads 2 ms); the heap control (a planted allocation shows).

## Licence

The menu is MIT, like the rest of dc-qalink. The font is Spleen 8x16 2.3.0 by Frederic Cambus, BSD 2-Clause
(`lib/font/LICENSE-spleen.txt`, and THIRD_PARTY.md): keep its notice in your documentation.
