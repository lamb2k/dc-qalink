/* demo.h: the menu example's shared part (demo.c). MIT licence. */
#ifndef DEMO_H
#define DEMO_H
#include <stdint.h>
extern int demo_scene, demo_speed, demo_spin, demo_bg, demo_timing;
extern uint32_t demo_late;
extern unsigned demo_draw_us;   /* the last picture's drawing time, shown on the Snapshot page */
typedef struct { uint32_t at; const char *name; } demo_shot_t;
extern const demo_shot_t DEMO_SHOTS[];
extern const int DEMO_NSHOTS;
extern const uint32_t DEMO_END;
void demo_init(void *net_config);
uint32_t demo_pad(uint32_t picture);
#endif
