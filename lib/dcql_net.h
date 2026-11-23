/* dcql_net.h: dc-qalink's link layer: one datagram transport over the modem (PPP, as dc-qalink 0.1) or the broadband
 * adapter (or the LAN adapter), picked at start. MIT licence.
 *
 * Both transports present the same thing: datagrams of at most DCQL_NET_DGRAM bytes to and from one peer (host:port)
 * through two single-producer single-consumer rings, so the caller's thread only copies. The modem transport is the
 * console-proven one (KOS libppp from source with four redirections, no net_init()); the adapter transport runs the KOS
 * network stack (net_init: DHCP, or a static address) with one receive thread blocked in recvfrom (no wake-ups while
 * the line is quiet) and one send thread woken by the ring.
 *
 * Used by the text-row API (dcql.h), the loader (loader/loader.c) and the client library (loader/client.c).
 */
#ifndef DCQL_NET_H
#define DCQL_NET_H
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

#define DCQL_NET_DGRAM 1200

enum { DCQL_NET_AUTO = 0, DCQL_NET_MODEM = 1, DCQL_NET_BBA = 2 };   /* also the hand-off area's CL_NET_* values */
/* states: the same numbers as dcql_state_t */
enum { DCQL_LINK_OFF = 0, DCQL_LINK_DIAL, DCQL_LINK_PPP, DCQL_LINK_UP, DCQL_LINK_FAILED, DCQL_LINK_DROPPED,
       DCQL_LINK_STOPPING };

typedef struct {
    int net;                    /* DCQL_NET_AUTO (adapter if one is fitted, else modem), _MODEM or _BBA */
    const char *number;         /* modem: dial string (default "1111111") */
    const char *user, *pass;    /* modem: PAP login (default dream / dreamcast) */
    unsigned host;              /* the peer, network order; 0xFFFFFFFF or 0 = learn it from its first datagram */
    unsigned short port, lport; /* the peer's UDP port and ours */
    unsigned ip, mask, gw;      /* adapter: static address (network order); ip 0 = DHCP */
    int dhcp_first;             /* adapter with ip set: 1 ask DHCP for that address first (as KOS's net_init(ip) does
                                   for dcload-ip), 2 ask DHCP for any address first; the static address only if no DHCP
                                   server answers. 0: the static address alone */
    unsigned char *rx_mem, *tx_mem;   /* ring memory (the program's, at least 2,048 B each, never freed by the link);
                                         NULL = keep the current rings, or the built-in 4 KiB rings that the first
                                         dcql_net_start() allocates (both or neither: start fails with err -44 without
                                         memory). Rings change only while the link is off, failed or dropped; a change
                                         frees the built-in rings it replaces. Lifetimes: docs/SIZES.md "Heap" */
    unsigned rx_size, tx_size;
    unsigned ppp_wait_ms;       /* modem: bound on PPP negotiation (0 = unbounded, as dc-qalink 0.1) */
    void (*log)(const char *line);
} dcql_net_config_t;

void dcql_net_config(const dcql_net_config_t *c);   /* before dcql_net_start(); copies the strings */
/* the most bytes the dial thread (on the adapter: also the receive thread) and the adapter's send thread have used of
   their static stacks so far (a pattern check), with the stacks' sizes; tx 0 before the adapter's threads start */
void dcql_net_stack_use(unsigned *dial, unsigned *dial_size, unsigned *tx, unsigned *tx_size);
int dcql_net_bba_present(void);                     /* an adapter answers its detect (no side effects) */
int dcql_net_start(void);         /* bring the link up in the background (or hang up + redial); 0 = started/already */
void dcql_net_stop(void);         /* modem: hang up in the background. adapter: close the socket */
void dcql_net_shutdown(void);     /* before a jump to another program: the adapter stopped (net_shutdown) at once */
int dcql_net_state(void);
int dcql_net_kind(void);          /* DCQL_NET_MODEM or DCQL_NET_BBA once started, else 0 */
int dcql_net_busy(void);          /* a dial / bring-up / hang-up thread still runs */
int dcql_net_send(const void *p, unsigned n);      /* 1 queued, 0 refused (not up, ring full) */
int dcql_net_recv(void *p, unsigned max);          /* bytes, -1 empty, -2 a datagram too large (dropped) */
int dcql_net_recv_wait(void *p, unsigned max, int timeout_ms);   /* as recv, sleeping until one comes (adapter) */
unsigned dcql_net_tx_queued(void);
/* autoconnect (ql-core2): accept the next PC that sends to our port as the peer, and until one does send every
   datagram to the whole network as well as to the last peer (adapter). The loader calls it when no PC has answered
   for a while. */
void dcql_net_relearn(void);
int dcql_net_learning(void);
/* the adapter link's own UDP port moved (round 3): the loader calls it while no PC answers, because Flycast on Windows
   stops receiving on a port that once sent to a PC port nobody listened on. 1 when moved; 0 on the modem, a link not
   up, or a port that cannot be bound. The PC answers the port the console speaks from. */
int dcql_net_move_lport(unsigned port);
int dcql_net_mac(unsigned char out[6]);            /* the adapter's MAC address; -1 (all zero) on the modem */
void dcql_net_text(char *out, unsigned n);          /* "UP 192.0.2.5 TO 192.0.2.10 ADAPTER", "DIALLING 1/3" */
void dcql_net_counters(unsigned *tx, unsigned *rx, unsigned *txd, unsigned *rxd);
unsigned dcql_net_ip(void), dcql_net_host(void), dcql_net_mask(void), dcql_net_gw(void);
unsigned dcql_net_port(void), dcql_net_lport(void), dcql_net_rate(void), dcql_net_stalls(void);
int dcql_net_attempt(void), dcql_net_err(void);
const char *dcql_net_number(void);

#ifdef __cplusplus
}
#endif
#endif
