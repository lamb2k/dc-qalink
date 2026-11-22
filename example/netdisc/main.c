/* netdisc: the network disc's example and console test (dc-qalink 0.2, lane ql-disc). MIT licence.
 *
 * Pushed by the PC to the loader. It reads a set of files of known SHA-256 through /cd twice: first from the real disc
 * (no network disc), then with lib/dcql_disc mounted over /cd (the PC's overlay, or the whole disc from the PC), and
 * prints each file's bytes, time and MB/s per read pattern, the per-read latency of small reads, and whether every hash
 * matched. The list is /cd/NETDISC/LIST.TXT, one line a file: "<name> <size> <sha256> <side>" (side: disc or pc, where
 * the file must come from with the overlay on); the overlay's own LIST.TXT names the changed files' hashes, so the same
 * code checks both passes. Test data: tools/netdisc_testdata.py (generated, nothing from any game).
 *
 * Arguments: mode=auto|overlay|pc  window=1..4  block=<KiB>  passes=disc,net (default both)  lat=<n> small reads
 * (default 64)  nodisc (skip the real-disc pass: a disc that lacks the files).
 * Report lines "@ <label> = <value> <unit>" are the bridge's headline numbers; tier is the device's (Flycast: function
 * and protocol cost only, never a console speed). */
#include <kos.h>
#include <arch/timer.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "cl_client.h"
#include "dcql_loop.h"
#include "dcql_disc.h"

int __real_cdrom_read_toc(cd_toc_t *toc, bool hd);
int __wrap_cdrom_read_toc(cd_toc_t *toc, bool hd) {
    int rc = __real_cdrom_read_toc(toc, true);
    return rc && !hd ? __real_cdrom_read_toc(toc, false) : rc;
}

#define MAXF 32
typedef struct { char name[64]; unsigned size; char sha[65]; char side[8]; } ent_t;
static ent_t g_e[MAXF];
static int g_n;
static uint8_t *g_buf;
static unsigned g_cap;
static int g_fail;

static int arg_int(const char *k, int d) {
    const char *a = cl_args(), *p = strstr(a, k);
    return p ? atoi(p + strlen(k)) : d;
}
static const char *arg_str(const char *k, char *out, size_t n, const char *d) {
    const char *a = cl_args(), *p = strstr(a, k);
    if (!p) return d;
    p += strlen(k);
    size_t i = 0;
    while (p[i] && p[i] != ' ' && i + 1 < n) { out[i] = p[i]; ++i; }
    out[i] = 0;
    return out;
}
static void say(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void say(const char *fmt, ...) {
    char b[400];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(b, sizeof(b), fmt, ap);
    va_end(ap);
    dbgio_printf("%s", b);
    cl_line(b, strlen(b));
    cl_report("%s", b);
}

static int read_list(void) {
    FILE *f = fopen("/cd/NETDISC/LIST.TXT", "r");   /* stdio, as a game would */
    if (!f) return -1;
    char line[200];
    g_n = 0;
    while (g_n < MAXF && fgets(line, sizeof(line), f)) {
        ent_t *e = &g_e[g_n];
        if (sscanf(line, "%63s %u %64s %7s", e->name, &e->size, e->sha, e->side) == 4) ++g_n;
    }
    fclose(f);
    return g_n;
}
static void hex(const uint8_t *d, char *out) {
    static const char h[] = "0123456789abcdef";
    for (int i = 0; i < 32; ++i) { out[2 * i] = h[d[i] >> 4]; out[2 * i + 1] = h[d[i] & 15]; }
    out[64] = 0;
}
static int sha_ok(const ent_t *e, const uint8_t *p, unsigned n) {
    cl_sha256_t s;
    uint8_t d[32];
    char hx[65];
    cl_sha256_init(&s);
    cl_sha256_update(&s, p, n);
    cl_sha256_final(&s, d);
    hex(d, hx);
    return n == e->size && !strcmp(hx, e->sha);
}

/* one file three ways: one whole read, 2 KiB reads in order, 2 KiB reads at scattered offsets */
typedef struct { double bytes, us; } acc_t;
static acc_t g_whole, g_seq, g_scat;
static void one_file(const ent_t *e, const char *pass, int net) {
    char path[96];
    snprintf(path, sizeof(path), "/cd/NETDISC/%s", e->name);
    if (e->size > g_cap) { say("netdisc %s %s: %u B over the buffer\n", pass, e->name, e->size); ++g_fail; return; }
    dcql_disc_stats_t s0, s1;
    dcql_disc_stats(&s0);
    int fd = open(path, O_RDONLY);
    if (fd < 0) { say("netdisc %s %s: open failed\n", pass, e->name); ++g_fail; return; }
    dcql_disc_stats(&s1);
    const char *side = !net ? "disc" : s1.opens_pc > s0.opens_pc ? "pc" : "disc";
    uint64_t t0 = timer_us_gettime64();
    ssize_t got = read(fd, g_buf, e->size);
    const uint64_t t_whole = timer_us_gettime64() - t0;
    const int ok1 = got == (ssize_t)e->size && sha_ok(e, g_buf, e->size);
    memset(g_buf, 0, e->size);
    lseek(fd, 0, SEEK_SET);
    t0 = timer_us_gettime64();
    unsigned done = 0;
    while (done < e->size) {
        const unsigned n = e->size - done < 2048 ? e->size - done : 2048;
        if (read(fd, g_buf + done, n) != (ssize_t)n) break;
        done += n;
    }
    const uint64_t t_seq = timer_us_gettime64() - t0;
    const int ok2 = done == e->size && sha_ok(e, g_buf, e->size);
    /* scattered: 2 KiB at offsets k * 37 KiB (beyond the cache's reach), as many as fit */
    unsigned nsc = 0, bsc = 0;
    t0 = timer_us_gettime64();
    for (unsigned off = 0; off + 2048 <= e->size && nsc < 32; off += 37 * 1024, ++nsc) {
        lseek(fd, off, SEEK_SET);
        if (read(fd, g_buf, 2048) == 2048) bsc += 2048;
    }
    const uint64_t t_scat = timer_us_gettime64() - t0;
    close(fd);
    dcql_disc_stats_t sm;
    dcql_disc_stats(&sm);
    const int side_ok = !net || !strcmp(side, sm.mode == DCQL_DISC_PC ? "pc" : e->side);   /* whole disc: all from the PC */
    if (!ok1 || !ok2 || !side_ok) ++g_fail;
    say("netdisc %s %-12s %8u B from %-4s whole %7.2f ms (%5.2f MB/s) 2K-seq %7.2f ms (%5.2f MB/s) 2K-scattered %u in "
        "%6.2f ms, sha %s %s%s\n", pass, e->name, e->size, side, t_whole / 1000.0,
        t_whole ? e->size / (double)t_whole : 0, t_seq / 1000.0, t_seq ? e->size / (double)t_seq : 0, nsc,
        t_scat / 1000.0, ok1 ? "ok" : "BAD", ok2 ? "ok" : "BAD", side_ok ? "" : " WRONG SIDE");
    if (net && !strcmp(side, "pc")) {
        g_whole.bytes += e->size; g_whole.us += t_whole;
        g_seq.bytes += e->size; g_seq.us += t_seq;
        g_scat.bytes += bsc; g_scat.us += t_scat;
    }
}

/* latency: n single 2 KiB reads of the biggest PC file, each at a fresh offset 37 KiB on (no cache hit) */
static void latency(int n) {
    const ent_t *big = NULL;
    for (int i = 0; i < g_n; ++i)
        if (!strcmp(g_e[i].side, "pc") && (!big || g_e[i].size > big->size)) big = &g_e[i];
    if (!big || n <= 0) return;
    char path[96];
    snprintf(path, sizeof(path), "/cd/NETDISC/%s", big->name);
    int fd = open(path, O_RDONLY);
    if (fd < 0) return;
    uint64_t sum = 0, worst = 0;
    int k = 0;
    for (; k < n; ++k) {
        const unsigned off = (unsigned)((k * 37u * 1024u) % (big->size > 4096 ? big->size - 2048 : 1)) & ~2047u;
        lseek(fd, off, SEEK_SET);
        const uint64_t t0 = timer_us_gettime64();
        if (read(fd, g_buf, 2048) != 2048) break;
        const uint64_t d = timer_us_gettime64() - t0;
        sum += d;
        if (d > worst) worst = d;
        thd_sleep(2);   /* the read-ahead lands in between, as in a game: the next read still misses (37 KiB on) */
    }
    close(fd);
    if (k) {
        say("netdisc latency: %d single 2 KiB reads of %s: mean %.3f ms worst %.3f ms\n", k, big->name,
            sum / 1000.0 / k, worst / 1000.0);
        cl_report("@ small read latency mean = %.3f ms\n@ small read latency worst = %.3f ms\n", sum / 1000.0 / k,
                  worst / 1000.0);
    }
}

int main(int argc, char **argv) {
    (void)argc; (void)argv;
    if (!cl_init(0)) { printf("netdisc: not started by the dc-qalink loader\n"); return 0; }
    char mb[16], pb[32];
    const char *m = arg_str("mode=", mb, sizeof(mb), "auto");
    const char *passes = arg_str("passes=", pb, sizeof(pb), "disc,net");
    dcql_disc_config_t c = {0};
    c.mode = !strcmp(m, "overlay") ? DCQL_DISC_OVERLAY : !strcmp(m, "pc") ? DCQL_DISC_PC : DCQL_DISC_AUTO;
    c.window = (unsigned)arg_int("window=", 0);           /* 0: the library's default (2) */
    c.block = (unsigned)arg_int("block=", 0) * 1024u;     /* 0: the library's default (4 KiB) */
    g_cap = 2u * 1024u * 1024u;
    g_buf = memalign(32, g_cap);
    if (!g_buf) { say("netdisc: no buffer\n"); cl_exit(3); }
    say("netdisc: mode %s window %u block %u, passes %s\n", m, c.window, c.block, passes);
    if (strstr(passes, "disc") && !strstr(cl_args(), "nodisc")) {
        if (read_list() <= 0) { say("netdisc disc: no /cd/NETDISC/LIST.TXT on the real disc\n"); ++g_fail; }
        for (int i = 0; i < g_n; ++i) {
            ent_t e = g_e[i];
            if (!strcmp(e.side, "pc")) continue;   /* the real disc's list names only its own files' hashes */
            one_file(&e, "disc", 0);
        }
    }
    if (strstr(passes, "net")) {
        const uint64_t t0 = timer_us_gettime64();
        const int rc = dcql_disc_mount(&c);
        const uint64_t tm = timer_us_gettime64() - t0;
        dcql_disc_stats_t st;
        dcql_disc_stats(&st);
        say("netdisc: mount %d in %.2f ms: %s, table %u entries, flags %u, disc files %u\n", rc, tm / 1000.0,
            st.mode == DCQL_DISC_PC ? "whole disc from the PC" : "overlay", (unsigned)st.table_count,
            (unsigned)st.table_flags, (unsigned)st.files);
        if (rc) { ++g_fail; }
        else {
            if (read_list() <= 0) { say("netdisc net: no LIST.TXT\n"); ++g_fail; }
            for (int i = 0; i < g_n; ++i) one_file(&g_e[i], "net ", 1);
            latency(arg_int("lat=", 64));
            if (g_whole.us) cl_report("@ pc whole-file read = %.3f MB/s\n", g_whole.bytes / g_whole.us);
            if (g_seq.us) cl_report("@ pc 2K sequential read = %.3f MB/s\n", g_seq.bytes / g_seq.us);
            if (g_scat.us) cl_report("@ pc 2K scattered read = %.3f MB/s\n", g_scat.bytes / g_scat.us);
            dcql_disc_report();
            dcql_disc_unmount();
        }
    }
    say("netdisc: %s (%d failures)\n", g_fail ? "FAILED" : "all hashes matched", g_fail);
    cl_report("@ netdisc failures = %d\n", g_fail);
    cl_exit(g_fail ? 1 : 0);
}
