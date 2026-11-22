/* dcql_live.h: live data from a running program to dc-qalink's bridge (0.2; wire: pc/bridge/wire.py, dcql_loop.h).
   MIT licence. Linked with the control loop's client library (loader/client.mk); every call is a no-op without the
   in-game link (modem, booted without the loader, or link off).

     static const char DEF[] =
         "program myGame\nrate 60\n"
         "phase draw 14000 - Drawing\nphase frame 16667 total Picture\n"     // key, budget us (or -), total (or -), label
         "ctr drops 1 Guards Dropped pictures\n"                              // key, guard 0/1, group, label
         "lane 1 Loads\npreset start Scene Start\noption speed int 1 8 2 Speed\n";
     dcql_live_declare(DEF);
     dcql_live_on_command(my_cmd, NULL);            // warp / option / pad, called on the game thread (below)
     for (;;) {                                     // once a picture, outside timed work:
         dcql_frame_t f = {.nph = 2, .phase_us = {draw_us, frame_us}, .nctr = 1, .ctr = {drops}};
         dcql_live_frame(&f);                       // copies into the ring; holds here while the PC has paused
     }

   Rules (dc-qalink plan section 3): the game's thread only copies into fixed rings (no allocation, no network, no
   disc); the link thread sends. Pause and step take effect here, at the end of a picture; warp, option and pad
   commands run here too, through the program's callback, so they never land in the middle of a picture. */
#ifndef DCQL_LIVE_H
#define DCQL_LIVE_H
#include <stdint.h>

#define DCQL_LIVE_PH_MAX 8
#define DCQL_LIVE_CTR_MAX 8
typedef struct {
    uint32_t n;                          /* picture number (0: the library counts) */
    uint8_t nph, nctr, has_pos, pad;
    uint32_t phase_us[DCQL_LIVE_PH_MAX]; /* in the declared phase order */
    int32_t ctr[DCQL_LIVE_CTR_MAX];      /* in the declared counter order */
    uint32_t room;                       /* as the game names rooms */
    float pos[3];
} dcql_frame_t;

enum { DCQL_LOG_INFO = 1, DCQL_LOG_WARN = 2, DCQL_LOG_ERR = 3, DCQL_LOG_STUB = 4 };
enum { DCQL_CMD_WARP = 4, DCQL_CMD_OPTION = 5, DCQL_CMD_PAD = 6 };
/* code DCQL_CMD_*; arg: warp the preset id, option "name=value", pad 12 bytes (u32 buttons, i8 x, i8 y, u8 lt, u8 rt,
   u32 pictures). Return 0 = done, 2 = refused (write why into `why`, at most 60 bytes). */
typedef int (*dcql_live_cmd_fn)(int code, const char *arg, unsigned arg_len, char *why, void *user);

void dcql_live_declare(const char *text);           /* static text; sent whenever the PC subscribes */
void dcql_live_on_command(dcql_live_cmd_fn fn, void *user);
void dcql_live_frame(const dcql_frame_t *f);        /* end of each picture, on the game's thread */
void dcql_live_trace(unsigned lane, unsigned pictures, const char *label);
void dcql_live_log(int level, const char *fmt, ...) __attribute__((format(printf, 2, 3)));
uint32_t dcql_live_picture(void);                   /* the picture count the library keeps */
int dcql_live_paused(void);

typedef struct { uint32_t frames, recs_sent, recs_dropped, trace_sent, trace_dropped, logs_sent, logs_dropped, cmds,
                 pump_us_total, pumps, frame_us_total; } dcql_live_stats_t;
void dcql_live_stats(dcql_live_stats_t *s);

/* the client library's side (loader/client.c): datagrams from the PC, and the link thread's sending */
typedef int (*dcql_live_send_fn)(uint8_t type, uint32_t a, uint16_t b, const void *p, uint16_t n);  /* 1 = sent */
int dcql_live_datagram(uint8_t type, uint32_t a, uint16_t b, const uint8_t *pl, int len, dcql_live_send_fn send);
void dcql_live_pump(dcql_live_send_fn send);
void dcql_live_link(int up);
#endif
