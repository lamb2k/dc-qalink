/* dcql_net.c: dc-qalink's link layer (dcql_net.h). MIT licence.
 *
 * MODEM (DCQL_NET_HAVE_MODEM): the console-proven transport of dc-qalink 0.1 and of the RE4 Dreamcast console loop's
 * loader (lanes qalink27 / link28 / qalink26 / build28 / eng-loop), unchanged in behaviour:
 *   dial thread (8 KiB static stack): dials up to 3 times, negotiates PPP, opens the socket, leaves; also hangs up.
 *   PPP thread (8 KiB static stack): libppp's receive loop. dcql.mk compiles libppp from $(KOS_BASE)/addons/libppp
 *     with thd_create / thd_pass / modem_write_data / modem_read_data / sem_wait_timed renamed to dcql_ppp_* below:
 *       - thd_pass sleeps 4 ms and, once the link is up, moves datagrams: all received ones into the rx ring, then ONE
 *         queued datagram out, so libppp reads the line between sends (a 1 KiB modem buffer);
 *       - a modem write that makes no progress for 2 s, or any write while hanging up, discards instead of spinning;
 *       - sem_wait_timed bounds ppp_connect's wait when ppp_wait_ms is set (the loader: 45 s, or a lost line).
 *   The modem driver's chain buffers and its timeout worker's stack come from BSS (--wrap, see dcql.mk).
 *   The KOS network stack runs without net_init(): fs_socket_init() + net_udp_init() only, so no IPv4 fragment
 *   reassembly: every datagram stays <= DCQL_NET_DGRAM both ways.
 * ADAPTER (DCQL_NET_HAVE_BBA): the broadband adapter (or the LAN adapter) through KOS's own driver and network stack:
 *   net_init() (DHCP, or a static address set on the device first), one UDP socket, a receive thread blocked in
 *   recvfrom (16 KiB static stack: it wakes only when a datagram arrives) and a send thread blocked on a semaphore the
 *   ring's producer signals (8 KiB). net_shutdown() before a jump stops the adapter (its DMA goes to the adapter's own
 *   memory on the G2 bus, never to main RAM).
 */
#include "dcql_net.h"

#include <kos.h>
#include <kos/thread.h>
#include <kos/worker_thread.h>
#include <kos/net.h>
#include <kos/fs_socket.h>
#include <kos/sem.h>
#include <arch/timer.h>
#include <arch/irq.h>
#include <sys/socket.h>
#include <sys/queue.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <fcntl.h>
#include <unistd.h>
#include <string.h>
#include <stdio.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdbool.h>
#if DCQL_NET_HAVE_MODEM
#include <dc/modem/modem.h>
#include <ppp/ppp.h>
int _ppp_enter_phase(int phase);   /* libppp ppp_internal.h (compiled in) */
#endif
int net_udp_init(void);            /* kernel/net/net_udp.c */
int net_dev_init(void);            /* kernel/net/net_core.c */
int net_dhcp_request(uint32_t required_address);   /* kernel/net/net_dhcp.c */

#define WRITE_NO_PROGRESS_MS 2000

static volatile int g_state = DCQL_LINK_OFF, g_attempt, g_err, g_stop, g_kind, g_cfg_net;
static volatile unsigned g_rate, g_tx_bytes, g_rx_bytes, g_tx_drops, g_rx_drops, g_tx_stalls;
static unsigned char g_ip[4], g_peer[4], g_mask[4], g_gw[4];
static char g_number[24] = "1111111", g_user[24] = "dream", g_pass[24] = "dreamcast";
static unsigned g_host = 0xFFFFFFFFu, g_configured_host = 0xFFFFFFFFu;   /* network order; all-ones = learn it */
static unsigned g_cfg_ip, g_cfg_mask, g_cfg_gw;
static int g_dhcp_first;
#if DCQL_NET_HAVE_BBA
static int g_static_used;
#endif
static unsigned short g_port = 52040, g_lport = 52040;
static unsigned g_ppp_wait_ms;
static void (*g_logfn)(const char *);
static volatile int g_sock = -1;
static bool g_net_ready;
static volatile int g_dial_running;
static volatile tid_t g_dial_tid;
static unsigned char g_dial_stack[16384] __attribute__((aligned(32)));

static void logf_(const char *fmt, ...) {
    if (!g_logfn) return;
    char b[128];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(b, sizeof(b), fmt, ap);
    va_end(ap);
    g_logfn(b);
}

static bool thread_busy(tid_t id) {
    if (!id) return false;
    /* never dereference a detached thread after it may be reaped: look it up by id with IRQs masked */
    const irq_mask_t irq = irq_disable();
    const kthread_t *t = thd_by_tid(id);
    const bool busy = t && t->state != STATE_ZOMBIE && t->state != STATE_FINISHED;
    irq_restore(irq);
    return busy;
}

/* ---- single-producer single-consumer rings of datagrams: [u16 length][bytes] */
typedef struct { unsigned char *buf; unsigned size; volatile unsigned head, tail; } ring_t;
static unsigned char g_rx_def[4096], g_tx_def[4096];
static ring_t g_rx = {g_rx_def, sizeof(g_rx_def), 0, 0}, g_tx = {g_tx_def, sizeof(g_tx_def), 0, 0};
static unsigned ring_used(const ring_t *r) { return r->head - r->tail; }
static bool ring_put(ring_t *r, const void *p, unsigned n) {
    if (!p || !n || n > DCQL_NET_DGRAM || ring_used(r) + n + 2 > r->size) return false;
    unsigned h = r->head;
    r->buf[h++ % r->size] = (unsigned char)n;
    r->buf[h++ % r->size] = (unsigned char)(n >> 8);
    for (unsigned i = 0; i < n; ++i) r->buf[h++ % r->size] = ((const unsigned char *)p)[i];
    __asm__ __volatile__("" ::: "memory");
    r->head = h;
    return true;
}
/* -1 empty; -2 retired whole because it did not fit */
static int ring_get(ring_t *r, void *p, unsigned max) {
    if (r->head == r->tail) return -1;
    unsigned t = r->tail;
    unsigned n = r->buf[t++ % r->size];
    n |= (unsigned)r->buf[t++ % r->size] << 8;
    if (n > max || !p) { __asm__ __volatile__("" ::: "memory"); r->tail = t + n; return -2; }
    for (unsigned i = 0; i < n; ++i, ++t) ((unsigned char *)p)[i] = r->buf[t % r->size];
    __asm__ __volatile__("" ::: "memory");
    r->tail = t;
    return (int)n;
}
static void ring_clear(ring_t *r) { r->tail = r->head; }

/* a received datagram: the peer's port, and its address once known (learnt from the first one) */
static volatile unsigned g_rx_refused;
/* dcql_net_relearn(): the next PC to send to our port becomes the peer (autoconnect: a PC that went away, or one at a
   new address); until then every datagram also goes to the whole network */
static volatile int g_learn;
static int accept_from(const struct sockaddr_in *from, socklen_t fl, int n) {
    if (g_learn && n <= DCQL_NET_DGRAM && fl >= sizeof(*from) && from->sin_family == AF_INET &&
        from->sin_port == htons(g_port) && from->sin_addr.s_addr && from->sin_addr.s_addr != 0xFFFFFFFFu) {
        g_learn = 0;
        if (from->sin_addr.s_addr != g_host) {
            g_host = from->sin_addr.s_addr;
            memcpy(g_peer, &g_host, 4);
            logf_("dcql: peer %u.%u.%u.%u (learnt again)", g_peer[0], g_peer[1], g_peer[2], g_peer[3]);
        }
        return 1;
    }
    if (n > DCQL_NET_DGRAM || fl < sizeof(*from) || from->sin_family != AF_INET || from->sin_port != htons(g_port) ||
        (g_host != 0xFFFFFFFFu && g_host != 0 && from->sin_addr.s_addr != g_host)) {
        if (g_rx_refused++ < 4) {
            const unsigned char *a = (const unsigned char *)&from->sin_addr.s_addr;
            logf_("dcql: refused %d bytes from %u.%u.%u.%u:%u", n, a[0], a[1], a[2], a[3], ntohs(from->sin_port));
        }
        return 0;
    }
    if (g_host == 0xFFFFFFFFu || g_host == 0) {
        g_host = from->sin_addr.s_addr;
        memcpy(g_peer, &g_host, 4);
        logf_("dcql: peer %u.%u.%u.%u", g_peer[0], g_peer[1], g_peer[2], g_peer[3]);
    }
    return 1;
}
static void send_to_peer(const unsigned char *p, int n) {
    struct sockaddr_in to;
    memset(&to, 0, sizeof(to));
    to.sin_family = AF_INET;
    to.sin_port = htons(g_port);
    to.sin_addr.s_addr = g_host;
    /* an unknown peer is 255.255.255.255: the modem's point-to-point link carries it to the Pi, as in 0.1 */
    const int sent = sendto(g_sock, p, n, 0, (struct sockaddr *)&to, sizeof(to));
    if (sent == n) g_tx_bytes += n;
    else ++g_tx_drops;
    if (g_learn && g_host != 0xFFFFFFFFu && g_kind == DCQL_NET_BBA) {   /* relearning: the whole network hears it too */
        to.sin_addr.s_addr = 0xFFFFFFFFu;
        sendto(g_sock, p, n, 0, (struct sockaddr *)&to, sizeof(to));
    }
}

/* =============================================================================================== modem transport */
#if DCQL_NET_HAVE_MODEM
static volatile int g_ppp_running;
static kthread_t *volatile g_ppp_thd;
static volatile tid_t g_ppp_tid;
static volatile bool g_retiring, g_tx_fault, g_abort, g_ppp_init;
static volatile int g_why;
static bool g_write_waiting;
static uint64_t g_write_since;
static volatile unsigned g_modem_tx, g_modem_rx;
static unsigned char g_ppp_stack[8192] __attribute__((aligned(32)));
static void *(*g_ppp_fn)(void *);
static unsigned char g_pkt[DCQL_NET_DGRAM + 208];   /* the PPP thread's datagram buffer */

/* the PPP thread, link up: every received datagram in, then at most ONE queued datagram out */
static void pump(void) {
    if (!modem_is_connected()) { g_state = DCQL_LINK_DROPPED; logf_("dcql: line dropped"); return; }
    int n;
    for (;;) {
        struct sockaddr_in from;
        socklen_t fl = sizeof(from);
        n = recvfrom(g_sock, g_pkt, sizeof(g_pkt) - 1, 0, (struct sockaddr *)&from, &fl);
        if (n == 0) { ++g_rx_drops; continue; }
        if (n < 0) break;
        g_rx_bytes += n;
        if (!accept_from(&from, fl, n)) { ++g_rx_drops; continue; }
        if (!ring_put(&g_rx, g_pkt, (unsigned)n)) ++g_rx_drops;
    }
    if ((n = ring_get(&g_tx, g_pkt, sizeof(g_pkt))) > 0) {
        if (!g_tx_fault && !g_retiring && !g_stop) send_to_peer(g_pkt, n);
        else ++g_tx_drops;
    }
}

static void *ppp_trampoline(void *arg) {
    g_ppp_thd = thd_get_current();
    g_ppp_tid = thd_get_id(g_ppp_thd);
    void *r = g_ppp_fn(arg);
    g_ppp_thd = NULL;
    g_ppp_running = 0;
    return r;
}

/* ---- libppp redirections (dcql.mk compiles libppp with -Dthd_create=dcql_ppp_thd_create etc.) */
kthread_t *dcql_ppp_thd_create(bool detach, void *(*routine)(void *), void *param) {
    (void)detach;   /* libppp never joins it */
    if (thread_busy(g_ppp_tid) || g_ppp_running) return NULL;   /* one PPP thread: one static stack */
    kthread_attr_t a;
    memset(&a, 0, sizeof(a));
    a.create_detached = true;
    a.stack_size = sizeof(g_ppp_stack);
    a.stack_ptr = g_ppp_stack;
    a.prio = thd_get_prio(thd_get_current());
    a.label = "dcql-ppp";
    a.disable_tls = true;
    g_ppp_fn = routine;
    g_ppp_running = 1;
    kthread_t *t = thd_create_ex(&a, ppp_trampoline, param);
    if (!t) g_ppp_running = 0;
    return t;
}
static bool on_ppp_thread_retiring(void) {
    return g_ppp_thd && thd_get_current() == g_ppp_thd && (g_stop || g_retiring || g_tx_fault);
}
void dcql_ppp_thd_pass(void) {
    /* libppp calls this with its mutex released. Entering DEAD also wakes ppp_connect. */
    if (on_ppp_thread_retiring()) {
        if (g_tx_fault && !g_stop && !g_retiring) g_state = DCQL_LINK_DROPPED;
        _ppp_enter_phase(PPP_PHASE_DEAD);
        thd_sleep(4);
        return;
    }
    if (g_state == DCQL_LINK_UP && g_sock >= 0 && g_ppp_thd && thd_get_current() == g_ppp_thd) pump();
    if (on_ppp_thread_retiring()) {
        if (g_tx_fault && !g_stop && !g_retiring) g_state = DCQL_LINK_DROPPED;
        _ppp_enter_phase(PPP_PHASE_DEAD);
    }
    thd_sleep(4);
}
int dcql_ppp_sem_wait_timed(semaphore_t *s, unsigned int timeout) {
    if (timeout || !g_ppp_wait_ms) return sem_wait_timed(s, timeout);
    const uint64_t t0 = timer_ms_gettime64();
    for (;;) {
        if (sem_wait_timed(s, 250) == 0) return 0;
        const unsigned ms = (unsigned)(timer_ms_gettime64() - t0);
        const int lost = !modem_is_connected();
        if (g_stop || lost || ms >= g_ppp_wait_ms) {
            g_why = g_stop ? 0 : lost ? -16 : -17;
            logf_("dcql: PPP %s after %u ms (modem tx %u rx %u)", g_stop ? "stopped" : lost ? "line lost" : "timed out",
                  ms, (unsigned)g_modem_tx, (unsigned)g_modem_rx);
            g_abort = true;
            _ppp_enter_phase(PPP_PHASE_DEAD);
            return sem_wait_timed(s, 0);
        }
    }
}
int dcql_ppp_modem_write(unsigned char *data, int size) {
    /* modem_shutdown does not clear CONNECTED on every KOS; never write once hang-up has started */
    if (g_stop || g_retiring || g_tx_fault || g_abort) return size;
    if (!modem_is_connected()) return size;   /* dropped line: discard (libppp would spin forever) */
    const int n = modem_write_data(data, size);
    if (n <= 0) {
        const uint64_t now = timer_ms_gettime64();
        if (!g_write_waiting) { g_write_waiting = true; g_write_since = now; }
        else if (now - g_write_since >= WRITE_NO_PROGRESS_MS) {
            g_tx_fault = true;
            ++g_tx_stalls;
            g_err = -14;
            return size;   /* discard; thd_pass enters DEAD */
        }
        thd_sleep(2);
        return 0;
    }
    g_write_waiting = false;
    g_modem_tx += (unsigned)n;
    return n;
}
int dcql_ppp_modem_read(unsigned char *data, int size) {
    const int n = modem_read_data(data, size);
    if (n > 0) g_modem_rx += (unsigned)n;
    return n;
}

/* ---- the modem driver's chain buffers from BSS (-Wl,--wrap=createChainBuffer,--wrap=destroyChainBuffer) */
typedef struct { int length; int start, end; unsigned char flags; unsigned char *data; } dcql_chain_buffer;
static dcql_chain_buffer g_cb[2];
static unsigned char g_cb_data[2][1025] __attribute__((aligned(4)));
static bool g_cb_used[2];
dcql_chain_buffer *__real_createChainBuffer(int length);
void __real_destroyChainBuffer(dcql_chain_buffer *b);
dcql_chain_buffer *__wrap_createChainBuffer(int length) {
    for (int i = 0; i < 2; ++i)
        if (!g_cb_used[i] && length + 1 <= (int)sizeof(g_cb_data[i])) {
            g_cb_used[i] = true;
            memset(&g_cb[i], 0, sizeof(g_cb[i]));
            g_cb[i].length = length + 1;   /* chainbuf.c keeps one spare byte */
            g_cb[i].data = g_cb_data[i];
            return &g_cb[i];
        }
    return __real_createChainBuffer(length);
}
void __wrap_destroyChainBuffer(dcql_chain_buffer *b) {
    for (int i = 0; i < 2; ++i)
        if (b == &g_cb[i]) { g_cb_used[i] = false; g_cb[i].data = NULL; return; }
    __real_destroyChainBuffer(b);
}

/* ---- the modem's timeout worker on a BSS stack (-Wl,--wrap=thd_worker_create_ex); other callers unchanged */
static unsigned char g_timer_stack[4096] __attribute__((aligned(32)));
static kthread_t *volatile g_dial_thd;
static kthread_t *volatile g_timer_thd;
kthread_worker_t *__real_thd_worker_create_ex(const kthread_attr_t *attr, void (*routine)(void *), void *data);
kthread_worker_t *__wrap_thd_worker_create_ex(const kthread_attr_t *attr, void (*routine)(void *), void *data) {
    if (attr || !g_dial_thd || thd_get_current() != g_dial_thd || g_timer_thd)
        return __real_thd_worker_create_ex(attr, routine, data);
    kthread_attr_t a;
    memset(&a, 0, sizeof(a));
    a.stack_size = sizeof(g_timer_stack);
    a.stack_ptr = g_timer_stack;
    a.prio = PRIO_DEFAULT;
    a.label = "dcql-timer";
    a.disable_tls = true;
    kthread_worker_t *w = __real_thd_worker_create_ex(&a, routine, data);
    if (w) g_timer_thd = thd_worker_get_thread(w);
    return w;
}
void modemIntShutdown(void);   /* kernel/arch/dreamcast/hardware/modem/mintr.c */
static void timer_down(void) {
    /* modem_shutdown leaves the IRQ handler and the oneshot timer; every redial would leak a worker thread */
    if (!g_timer_thd) return;
    modemIntShutdown();
    g_timer_thd = NULL;
}

/* ---- dial / hang up (dial thread only) */
static bool hang_up(void) {
    g_retiring = true;
    const int s = g_sock;
    g_sock = -1;          /* the pump stops */
    ring_clear(&g_tx);
    modem_shutdown();     /* the line drops: a blocked modem write returns */
    if (s >= 0) close(s);
    if (g_ppp_init) _ppp_enter_phase(PPP_PHASE_DEAD);
    for (int i = 0; i < 400 && (g_ppp_running || thread_busy(g_ppp_tid)); ++i) thd_sleep(8);
    if (g_ppp_running || thread_busy(g_ppp_tid)) { g_err = -31; return false; }   /* retried by the next start */
    if (g_ppp_init) {
        ppp_set_login(NULL, NULL);   /* ppp_shutdown leaves these allocated */
        ppp_shutdown();
        g_ppp_init = false;
    }
    timer_down();
    ring_clear(&g_rx);
    return true;
}

static bool dial_once(void) {
    g_retiring = false; g_tx_fault = false; g_write_waiting = false; g_abort = false; g_why = 0;
    g_host = g_configured_host;   /* a new call learns the peer anew */
    memcpy(g_peer, &g_host, 4);
    g_modem_tx = g_modem_rx = 0;
    g_state = DCQL_LINK_DIAL;
    if (!g_ppp_init) {
        if (ppp_init() < 0) { g_err = -20; return false; }
        g_ppp_init = true;
    }
    int rate = 0;
    const int r = ppp_modem_init(g_number, 1, &rate);   /* blind dial: a DreamPi line has no dial tone */
    if (r < 0) { g_err = r; logf_("dcql: dial failed %d", r); return false; }
    if (g_stop) return false;
    g_rate = (unsigned)rate;
    g_state = DCQL_LINK_PPP;
    logf_("dcql: modem %u bps, PPP", g_rate);
    if (ppp_set_login(g_user, g_pass) < 0) { g_err = -15; return false; }   /* DreamPi's pppd asks for PAP */
    if (ppp_connect() < 0 || g_abort) {
        g_err = g_why ? g_why : -10;
        logf_("dcql: PPP failed %d (modem tx %u rx %u)", g_err, (unsigned)g_modem_tx, (unsigned)g_modem_rx);
        return false;
    }
    if (!g_net_ready) { fs_socket_init(); net_udp_init(); g_net_ready = true; }
    if (net_default_dev) memcpy(g_ip, net_default_dev->ip_addr, 4);
    const int s = socket(AF_INET, SOCK_DGRAM, 0);
    if (s < 0) { g_err = -11; return false; }
    struct sockaddr_in me;
    memset(&me, 0, sizeof(me));
    me.sin_family = AF_INET;
    me.sin_port = htons(g_lport);
    me.sin_addr.s_addr = INADDR_ANY;
    if (bind(s, (struct sockaddr *)&me, sizeof(me)) < 0) { close(s); g_err = -12; return false; }
    if (fcntl(s, F_SETFL, O_NONBLOCK) < 0) { close(s); g_err = -13; return false; }
    ring_clear(&g_rx);   /* no datagram from an earlier call may be taken */
    ring_clear(&g_tx);
    g_sock = s;
    logf_("dcql: up ip=%u.%u.%u.%u", g_ip[0], g_ip[1], g_ip[2], g_ip[3]);
    return true;
}

/* arg 0 = dial (3 tries), 1 = hang up, 2 = hang up then dial */
static void modem_main(intptr_t mode) {
    if (mode) {
        if (!hang_up()) { g_state = DCQL_LINK_DROPPED; return; }
        g_state = DCQL_LINK_OFF;
        logf_("dcql: hung up");
    }
    if (mode != 1 && !g_stop) {
        bool up = false;
        for (int a = 1; a <= 3 && !g_stop && !up; ++a) {
            g_attempt = a;
            up = dial_once();
            if (!up) {
                if (!hang_up()) { g_state = DCQL_LINK_DROPPED; return; }
                for (int i = 0; i < 40 && !g_stop && a < 3; ++i) thd_sleep(100);   /* 4 s between tries */
            }
        }
        if (up && g_stop) {
            if (!hang_up()) { g_state = DCQL_LINK_DROPPED; return; }
            up = false;
        }
        g_state = up ? DCQL_LINK_UP : g_stop ? DCQL_LINK_OFF : DCQL_LINK_FAILED;
    }
}
#endif /* DCQL_NET_HAVE_MODEM */

/* =============================================================================================== adapter transport */
#if DCQL_NET_HAVE_BBA
#include <dc/net/broadband_adapter.h>
#include <dc/net/lan_adapter.h>
/* KOS registers the adapter drivers only with INIT_NET (eth_init), which would also run net_init and DHCP at start-up
   on every console: the link layer registers them itself, once, when it first looks for an adapter */
static void register_adapters(void) {
    static int done;
    if (done) return;
    done = 1;
    bba_init();
    la_init();
}
static unsigned char g_brx_stack[16384] __attribute__((aligned(32))), g_btx_stack[8192] __attribute__((aligned(32)));
static unsigned char g_bpkt_rx[DCQL_NET_DGRAM + 208], g_bpkt_tx[DCQL_NET_DGRAM + 8];
static semaphore_t g_tx_sem = SEM_INITIALIZER(0), g_rx_sem = SEM_INITIALIZER(0);
static volatile int g_bthreads, g_bdown;

static void *dcql_bba_rx_thd(void *arg) {
    (void)arg;
    while (!g_bdown) {
        struct sockaddr_in from;
        socklen_t fl = sizeof(from);
        const int s = g_sock;
        if (s < 0) break;
        const int n = recvfrom(s, g_bpkt_rx, sizeof(g_bpkt_rx) - 1, 0, (struct sockaddr *)&from, &fl);
        if (g_bdown) break;
        if (n <= 0) { if (n < 0) thd_sleep(10); continue; }
        g_rx_bytes += n;
        if (!accept_from(&from, fl, n) || g_state != DCQL_LINK_UP) { ++g_rx_drops; continue; }
        if (!ring_put(&g_rx, g_bpkt_rx, (unsigned)n)) ++g_rx_drops;
        else sem_signal(&g_rx_sem);
    }
    return NULL;
}
static void *dcql_bba_tx_thd(void *arg) {
    (void)arg;
    while (!g_bdown) {
        sem_wait(&g_tx_sem);
        int n;
        while (!g_bdown && g_sock >= 0 && (n = ring_get(&g_tx, g_bpkt_tx, sizeof(g_bpkt_tx))) >= 0)
            if (n > 0) send_to_peer(g_bpkt_tx, n);
    }
    return NULL;
}
static void set4(unsigned char *d, unsigned v) { memcpy(d, &v, 4); }

static void bba_main(void) {
    register_adapters();
    g_state = DCQL_LINK_DIAL;
    g_attempt = 1;
    g_host = g_configured_host;
    memcpy(g_peer, &g_host, 4);
    if (!g_net_ready && g_cfg_ip && g_dhcp_first) {
        /* DHCP asked for the known address (a quick REQUEST where a server answers), then the static address: in
           Flycast's adapter emulation a console that skips DHCP sends but receives nothing (lane ql-core, 2026-11-22) */
        const int r = net_init(g_dhcp_first == 2 ? 0 : ntohl(g_cfg_ip));   /* 2: any address the server gives */
        if (!net_default_dev) { g_err = -41; g_state = DCQL_LINK_FAILED; logf_("dcql: no adapter (%d)", r); return; }
        g_net_ready = true;
        if (!net_default_dev->netmask[0] || !net_default_dev->ip_addr[0]) {   /* no DHCP answer (KOS set at most the address) */
            const unsigned m = g_cfg_mask ? g_cfg_mask : htonl(0xffffff00u);
            set4(net_default_dev->ip_addr, g_cfg_ip);
            set4(net_default_dev->netmask, m);
            set4(net_default_dev->gateway, g_cfg_gw);
            set4(net_default_dev->broadcast, (g_cfg_ip & m) | ~m);
            g_static_used = 1;
        } else g_static_used = 0;
    }
    if (!g_net_ready) {
        g_static_used = g_cfg_ip != 0;
        if (g_cfg_ip) {   /* a static address: on the device before net_init, so it asks no DHCP server */
            if (net_dev_init() < 0 || !net_default_dev) { g_err = -40; g_state = DCQL_LINK_FAILED; return; }
            set4(net_default_dev->ip_addr, g_cfg_ip);
            set4(net_default_dev->netmask, g_cfg_mask ? g_cfg_mask : htonl(0xffffff00u));
            set4(net_default_dev->gateway, g_cfg_gw);
            const unsigned m = g_cfg_mask ? g_cfg_mask : htonl(0xffffff00u);
            set4(net_default_dev->broadcast, (g_cfg_ip & m) | ~m);
        }
        const int r = net_init(0);
        if (!net_default_dev) { g_err = -41; g_state = DCQL_LINK_FAILED; logf_("dcql: no adapter (%d)", r); return; }
        g_net_ready = true;
    }
    if (!net_default_dev->ip_addr[0] && net_dhcp_request(0) < 0) {}   /* a retry after a DHCP that failed */
    if (!net_default_dev->ip_addr[0]) { g_err = -42; g_state = DCQL_LINK_FAILED; logf_("dcql: no address (DHCP)"); return; }
    memcpy(g_ip, net_default_dev->ip_addr, 4);
    memcpy(g_mask, net_default_dev->netmask, 4);
    memcpy(g_gw, net_default_dev->gateway, 4);
    if (g_sock < 0) {
        const int s = socket(AF_INET, SOCK_DGRAM, 0);
        if (s < 0) { g_err = -11; g_state = DCQL_LINK_FAILED; return; }
        struct sockaddr_in me;
        memset(&me, 0, sizeof(me));
        me.sin_family = AF_INET;
        me.sin_port = htons(g_lport);
        me.sin_addr.s_addr = INADDR_ANY;
        if (bind(s, (struct sockaddr *)&me, sizeof(me)) < 0) { close(s); g_err = -12; g_state = DCQL_LINK_FAILED; return; }
        g_sock = s;
    }
    ring_clear(&g_rx);
    ring_clear(&g_tx);
    if (!g_bthreads) {
        kthread_attr_t a;
        memset(&a, 0, sizeof(a));
        a.create_detached = true;
        a.prio = PRIO_DEFAULT;
        a.disable_tls = true;
        a.stack_size = sizeof(g_brx_stack); a.stack_ptr = g_brx_stack; a.label = "dcql-bba-rx";
        if (!thd_create_ex(&a, dcql_bba_rx_thd, NULL)) { g_err = -43; g_state = DCQL_LINK_FAILED; return; }
        a.stack_size = sizeof(g_btx_stack); a.stack_ptr = g_btx_stack; a.label = "dcql-bba-tx";
        if (!thd_create_ex(&a, dcql_bba_tx_thd, NULL)) { g_err = -43; g_state = DCQL_LINK_FAILED; return; }
        g_bthreads = 1;
    }
    g_state = g_stop ? DCQL_LINK_OFF : DCQL_LINK_UP;
    logf_("dcql: up ip=%u.%u.%u.%u (adapter, %s)", g_ip[0], g_ip[1], g_ip[2], g_ip[3],
          g_static_used ? "static" : g_cfg_ip ? "DHCP, the address asked for" : "DHCP");
}
#endif /* DCQL_NET_HAVE_BBA */

int dcql_net_bba_present(void) {
#if DCQL_NET_HAVE_BBA
    register_adapters();
    netif_t *n;
    LIST_FOREACH(n, net_get_if_list(), if_list)
        if (n->name && strcmp(n->name, "ppp") && n->if_detect && n->if_detect(n) >= 0) return 1;
#endif
    return 0;
}

static void *dial_main(void *arg) {
#if DCQL_NET_HAVE_MODEM
    g_dial_thd = thd_get_current();
#endif
    g_dial_tid = thd_get_id(thd_get_current());
#if DCQL_NET_HAVE_BBA
    if (g_kind == DCQL_NET_BBA) bba_main();
#endif
#if DCQL_NET_HAVE_MODEM
    if (g_kind == DCQL_NET_MODEM) modem_main((intptr_t)arg);
    g_dial_thd = NULL;
#endif
    (void)arg;
    g_dial_running = 0;
    return NULL;
}

static int spawn_dial(int mode) {
    if (thread_busy(g_dial_tid) || g_dial_running) return 0;
    kthread_attr_t a;
    memset(&a, 0, sizeof(a));
    a.create_detached = true;
    a.stack_size = sizeof(g_dial_stack);
    a.stack_ptr = g_dial_stack;
    a.prio = thd_get_prio(thd_get_current());
    a.label = "dcql-dial";
    a.disable_tls = true;
    g_dial_running = 1;
    if (!thd_create_ex(&a, dial_main, (void *)(intptr_t)mode)) { g_dial_running = 0; return 0; }
    return 1;
}

static void copy_str(char *d, size_t n, const char *s) {
    if (s && s[0]) { strncpy(d, s, n - 1); d[n - 1] = 0; }
}

/* ---- API */
void dcql_net_config(const dcql_net_config_t *c) {
    if (!c) return;
    g_cfg_net = c->net;
    copy_str(g_number, sizeof(g_number), c->number);
    copy_str(g_user, sizeof(g_user), c->user);
    copy_str(g_pass, sizeof(g_pass), c->pass);
    g_configured_host = c->host ? c->host : 0xFFFFFFFFu;
    g_host = g_configured_host;
    memcpy(g_peer, &g_host, 4);
    if (c->port) g_port = c->port;
    if (c->lport) g_lport = c->lport;
    g_cfg_ip = c->ip; g_cfg_mask = c->mask; g_cfg_gw = c->gw;
    g_dhcp_first = c->dhcp_first;
    if (c->rx_mem && c->rx_size >= 2048) { g_rx.buf = c->rx_mem; g_rx.size = c->rx_size; g_rx.head = g_rx.tail = 0; }
    if (c->tx_mem && c->tx_size >= 2048) { g_tx.buf = c->tx_mem; g_tx.size = c->tx_size; g_tx.head = g_tx.tail = 0; }
    g_ppp_wait_ms = c->ppp_wait_ms;
    g_logfn = c->log;
}

int dcql_net_start(void) {
    if (thread_busy(g_dial_tid) || g_dial_running || g_state == DCQL_LINK_UP) return 0;
    if (!g_kind) {
#if DCQL_NET_HAVE_BBA && DCQL_NET_HAVE_MODEM
        g_kind = g_cfg_net == DCQL_NET_MODEM ? DCQL_NET_MODEM
               : g_cfg_net == DCQL_NET_BBA ? DCQL_NET_BBA : (dcql_net_bba_present() ? DCQL_NET_BBA : DCQL_NET_MODEM);
#elif DCQL_NET_HAVE_BBA
        g_kind = DCQL_NET_BBA;
#else
        g_kind = DCQL_NET_MODEM;
#endif
        logf_("dcql: transport %s", g_kind == DCQL_NET_BBA ? "adapter" : "modem");
    }
    int mode = 0;
#if DCQL_NET_HAVE_MODEM
    if (g_kind == DCQL_NET_MODEM)
        mode = (g_state == DCQL_LINK_DROPPED || g_state == DCQL_LINK_FAILED || g_ppp_running || thread_busy(g_ppp_tid) ||
                g_sock >= 0) ? 2 : 0;
#endif
    g_stop = 0; g_err = 0; g_attempt = 0;
    g_state = mode ? DCQL_LINK_STOPPING : DCQL_LINK_DIAL;
    ring_clear(&g_rx);
    if (!spawn_dial(mode)) { g_state = DCQL_LINK_FAILED; g_err = -30; return -1; }
    return 0;
}
void dcql_net_stop(void) {
    g_stop = 1;   /* a dial in progress stops after its current step */
    if (g_dial_running) { g_state = DCQL_LINK_STOPPING; return; }
#if DCQL_NET_HAVE_MODEM
    if (g_kind == DCQL_NET_MODEM && (g_state == DCQL_LINK_UP || g_state == DCQL_LINK_DROPPED || g_ppp_running || g_sock >= 0)) {
        g_state = DCQL_LINK_STOPPING;
        if (!spawn_dial(1)) { g_state = DCQL_LINK_FAILED; g_err = -30; }
        return;
    }
#endif
    g_state = DCQL_LINK_OFF;   /* the adapter keeps its socket: dcql_net_shutdown() takes it down before a jump */
}
void dcql_net_shutdown(void) {
#if DCQL_NET_HAVE_BBA
    if (g_kind == DCQL_NET_BBA && g_net_ready) {
        g_bdown = 1;
        g_state = DCQL_LINK_OFF;
        net_shutdown();
        g_net_ready = false;
        g_sock = -1;
    }
#endif
}
int dcql_net_state(void) { return g_state; }
int dcql_net_kind(void) { return g_kind; }
int dcql_net_busy(void) {
#if DCQL_NET_HAVE_MODEM
    if (g_ppp_running) return 1;
#endif
    return g_dial_running;
}
int dcql_net_send(const void *p, unsigned n) {
    if (g_state != DCQL_LINK_UP) return 0;
    /* any thread may send (a program's main thread, its link thread, a stills thread): the ring has one producer at a
       time with interrupts off for the copy (at most 1.5 KB) */
    const irq_mask_t irq = irq_disable();
    const bool ok = n <= DCQL_NET_DGRAM && ring_put(&g_tx, p, n);
    irq_restore(irq);
    if (!ok) { ++g_tx_drops; return 0; }
#if DCQL_NET_HAVE_BBA
    if (g_kind == DCQL_NET_BBA) sem_signal(&g_tx_sem);
#endif
    return 1;
}
int dcql_net_recv(void *p, unsigned max) {
    const int n = ring_get(&g_rx, p, max);
    if (n == -2) ++g_rx_drops;
    return n;
}
int dcql_net_recv_wait(void *p, unsigned max, int timeout_ms) {
    const uint64_t end = timer_ms_gettime64() + (uint64_t)(timeout_ms > 0 ? timeout_ms : 0);
    for (;;) {
        const int n = dcql_net_recv(p, max);
        if (n != -1) return n;
        const uint64_t now = timer_ms_gettime64();
        if (now >= end) return -1;
#if DCQL_NET_HAVE_BBA
        if (g_kind == DCQL_NET_BBA) { sem_wait_timed(&g_rx_sem, (int)(end - now) < 1 ? 1 : (int)(end - now)); continue; }
#endif
        thd_sleep(4);
    }
}
unsigned dcql_net_tx_queued(void) { return ring_used(&g_tx); }
void dcql_net_relearn(void) { g_learn = 1; }
int dcql_net_learning(void) { return g_learn; }
int dcql_net_mac(unsigned char out[6]) {
    memset(out, 0, 6);
#if DCQL_NET_HAVE_BBA
    if (g_kind == DCQL_NET_BBA && net_default_dev) { memcpy(out, net_default_dev->mac_addr, 6); return 0; }
#endif
    return -1;
}
unsigned dcql_net_ip(void) { unsigned v; memcpy(&v, g_ip, 4); return v; }
unsigned dcql_net_mask(void) { unsigned v; memcpy(&v, g_mask, 4); return v; }
unsigned dcql_net_gw(void) { unsigned v; memcpy(&v, g_gw, 4); return v; }
unsigned dcql_net_host(void) { return g_host; }
unsigned dcql_net_port(void) { return g_port; }
unsigned dcql_net_lport(void) { return g_lport; }
unsigned dcql_net_rate(void) { return g_rate; }
unsigned dcql_net_stalls(void) { return g_tx_stalls; }
int dcql_net_attempt(void) { return g_attempt; }
int dcql_net_err(void) { return g_err; }
const char *dcql_net_number(void) { return g_number; }
void dcql_net_text(char *out, unsigned n) {
    const int bba = g_kind == DCQL_NET_BBA;
    switch (g_state) {
    case DCQL_LINK_OFF: snprintf(out, n, "OFF"); break;
    case DCQL_LINK_DIAL:
        if (bba) snprintf(out, n, "ADAPTER: %s", g_cfg_ip ? "STATIC ADDRESS" : "ASKING DHCP");
        else snprintf(out, n, "DIALLING %d/3 (%s)", g_attempt, g_number);
        break;
    case DCQL_LINK_PPP: snprintf(out, n, "PPP %u BPS %d/3", g_rate, g_attempt); break;
    case DCQL_LINK_UP:
        if (bba)
            snprintf(out, n, "UP %u.%u.%u.%u TO %u.%u.%u.%u ADAPTER", g_ip[0], g_ip[1], g_ip[2], g_ip[3], g_peer[0],
                     g_peer[1], g_peer[2], g_peer[3]);
        else
            snprintf(out, n, "UP %u.%u.%u.%u TO %u.%u.%u.%u %u BPS", g_ip[0], g_ip[1], g_ip[2], g_ip[3], g_peer[0],
                     g_peer[1], g_peer[2], g_peer[3], g_rate);
        break;
    case DCQL_LINK_FAILED: snprintf(out, n, "FAILED %d", g_err); break;
    case DCQL_LINK_DROPPED: snprintf(out, n, "LINE DROPPED"); break;
    default: snprintf(out, n, "HANGING UP"); break;
    }
}
void dcql_net_counters(unsigned *tx, unsigned *rx, unsigned *txd, unsigned *rxd) {
    *tx = g_tx_bytes; *rx = g_rx_bytes; *txd = g_tx_drops; *rxd = g_rx_drops;
}
