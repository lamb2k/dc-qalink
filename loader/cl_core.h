/* cl_core.h: the console side of the control loop's protocol, without hardware (dc-qalink 0.2; first written for the
   RE4 Dreamcast console loop, lane eng-loop, 2026-11-19). The loader (loader.c, KallistiOS) and the host test build
   (host.c) drive the same state machine: datagrams in through cl_core_input, time through cl_core_tick, datagrams out
   and the final jump through the platform callbacks. Wire format and hand-off area: include/dcql_loop.h. */
#ifndef CL_CORE_H
#define CL_CORE_H
#include "dcql_loop.h"

typedef struct {
    void *ctx;
    int (*send)(void *ctx, const uint8_t *p, size_t n);   /* queue one datagram; 0 = dropped */
    uint32_t (*now_ms)(void *ctx);
    void *(*alloc)(size_t n);
    void (*free)(void *p);
    void (*log)(void *ctx, const char *line);             /* optional */
    cl_handoff_t *handoff;
    uint32_t max_raw;          /* largest raw image the platform can run */
    uint32_t console_ip;       /* network order, for HELLO */
    uint32_t link_host, link_port, link_lport;
    const char *number;
    const char *build;
    /* v2 */
    uint32_t net_kind;         /* CL_NET_* */
    uint32_t net_mask, net_gw;
    uint32_t hello_flags;      /* CL_HF_* */
    uint8_t loader_sha8[8];
    uint8_t mac[6];            /* the adapter's MAC address (HELLO offset CL_HELLO_MAC), all zero if none */
    uint32_t stub_rec[4];      /* the stub's last way back (HELLO offset CL_HELLO_STUB): step, err, gdemu, entries;
                                  step 0 = no record this boot */
} cl_platform_t;

typedef struct {
    cl_platform_t p;
    int state;                 /* CL_ST_* */
    uint32_t session;
    /* the image being received / held */
    uint32_t comp_len, raw_len, nchunks, load_addr, comp_crc, deadline_s, flags;
    uint8_t codec, sha[32];
    char args[CL_ARGS_MAX + 1];
    uint8_t *comp, *raw, *bitmap;
    uint32_t have, next_needed;
    int verified;
    int verdict;               /* -1 none yet, else CL_OK / CL_E_* */
    int exec_pending;          /* RUN accepted: the platform hangs up and jumps */
    int exec_loader;           /* v2: the accepted image is a newer loader (LOAD flag CL_LOAD_F_LOADER) */
    /* ack timing */
    uint32_t new_since_ack, last_data_ms, last_hello_ms, last_err_ms, boot_ms;
    int ack_due;
    /* counters */
    uint32_t rx_pkts, rx_bytes, rx_bad, rx_dup, tx_pkts, rx_version;
    /* the last run, from the hand-off area at boot */
    uint32_t boot_count;
} cl_core_t;

void cl_core_init(cl_core_t *c, const cl_platform_t *p);
void cl_core_input(cl_core_t *c, const uint8_t *dgram, size_t n);
void cl_core_tick(cl_core_t *c);
void cl_core_hello(cl_core_t *c);
const char *cl_err_name(int code);
#endif
