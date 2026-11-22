/* viewer.c: dc-qalink's console asset viewer (0.2, plan L5b). MIT licence.

   The loader runs it; the bridge (pc/bridge/assets.py) sends it live assets and a camera, grabs its screen and reads
   its cost. It draws one asset (a texture or a still model, docs/ASSETS.md) with the camera the PC sends, the same
   camera and lighting as QA Link Cloud's PC preview, so the two pictures can be set side by side and differenced.

   - Assets arrive through the link's /pc file service: the bridge serves its asset store to this run, and a command
     `load=<seq>:<file>` names the file. The file is read into one fixed block (a quarter of main memory, the live-asset
     budget), checked (magic, size, CRC-32, budget), and its textures copied into graphics memory as they are (the
     converter wrote the chip's own form). Loading a new asset frees the old one first.
   - Everything else is a live-data option `name=value`: cam, light, bg, filter, view, cull, seq (below).
   - Cost: every picture is a live-data record (CPU time to transform and submit, the graphics chip's registration and
     render time, the wait, the picture), and the counters say what is loaded: vertices, strips, texture bytes in
     graphics memory, the vertex buffer the picture used, graphics memory still free.
   - Fixed buffers only after start (the asset block, one strip of vertices); every wait is time-limited (the file
     service's own limits, the run's deadline).

   Arguments: secs=<n> (run time, default 900), diag=1 (the client's steps on screen). */
#include <kos.h>
#include <dc/pvr.h>
#include <arch/timer.h>
#include <math.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "cl_client.h"
#include "dcql_live.h"
#include "dqa.h"

int __real_cdrom_read_toc(cd_toc_t *toc, bool hd);
int __wrap_cdrom_read_toc(cd_toc_t *toc, bool hd) {
    int rc = __real_cdrom_read_toc(toc, true);
    return rc && !hd ? __real_cdrom_read_toc(toc, false) : rc;
}

/* the live-asset budget (QA Link Cloud's Assets workspace, the same rule): a quarter of main memory for the asset
   block, half of graphics memory for its textures */
#define BLOCK_BYTES (4u << 20)
#define VRAM_BUDGET (4u << 20)
#define VBUF_BYTES (1024u * 1024u)     /* the chip's vertex buffer for the opaque list (and as much again for blending) */
#define TEX_MAX 16u

static uint8_t g_block[BLOCK_BYTES] __attribute__((aligned(32)));
static pvr_vertex_t g_strip[DQA_STRIP_MAX] __attribute__((aligned(32)));

static const char DEF[] =
    "program viewer.elf\n"
    "rate 60\n"
    "phase submit 14000 - Transform and submit\n"
    "phase ta - - Chip registration\n"
    "phase render - - Chip render\n"
    "phase wait - - Wait for the chip\n"
    "phase frame 16667 total Picture\n"
    "ctr seq 0 Viewer Applied change\n"
    "ctr asset 0 Viewer Loaded asset\n"
    "ctr state 0 Viewer State\n"
    "ctr verts 0 Cost Vertices\n"
    "ctr strips 0 Cost Strips\n"
    "ctr txbytes 0 Cost Texture bytes\n"
    "ctr vtxbuf 0 Cost Bytes sent to the chip a picture\n"
    "ctr vfree 0 Cost Graphics memory free\n"
    "lane 1 Viewer\n"
    "option filter int 0 1 0 Bilinear filter\n"
    "option cull int 0 2 2 Culling (0 none, 1 CCW, 2 CW)\n";

enum { ST_NONE = 0, ST_LOADING = 1, ST_READY = 2, ST_REFUSED = 3, ST_ERROR = 4 };
enum { VIEW_MODEL = 0, VIEW_FLAT = 1, VIEW_MIPS = 2 };

/* what the PC set */
static float g_yaw = 0.6108652f, g_pitch = 0.4886922f, g_dist = 3.1f, g_fov = 0.8726646f;   /* 35, 28 deg; 50 deg */
static float g_L[3], g_amb = 0.25f, g_dif = 0.75f;
static float g_bg[3];
static int g_filter, g_cull = 2, g_view = -1;
static int32_t g_seq;                  /* the PC's change number, echoed once drawn */
static char g_load_name[48];
static int32_t g_load_seq, g_load_pending;

/* what is loaded */
static int g_state = ST_NONE;
static int32_t g_asset_seq;
static const dqa_head_t *g_h;
static const dqa_tex_t *g_tex;
static const dqa_strip_t *g_strips;
static const dqa_vert_t *g_verts;
static pvr_ptr_t g_vtex[TEX_MAX];
static uint32_t g_txbytes;
static pvr_poly_hdr_t g_hdr[TEX_MAX][2], g_hdr_col[2];   /* [tex][0 opaque, 1 blended] */
static uint32_t g_ta;                   /* bytes sent to the chip this picture (headers and vertices) */

static void set_light(float x, float y, float z) {
    const float n = sqrtf(x * x + y * y + z * z);
    g_L[0] = x / n; g_L[1] = y / n; g_L[2] = z / n;
}

/* ---------------------------------------------------------------- CRC-32 (zlib's) */
static uint32_t g_crc_tab[256];
static void crc_init(void) {
    for (uint32_t i = 0; i < 256; ++i) {
        uint32_t c = i;
        for (int k = 0; k < 8; ++k) c = c & 1 ? 0xedb88320u ^ (c >> 1) : c >> 1;
        g_crc_tab[i] = c;
    }
}
static uint32_t crc32_of(const uint8_t *p, uint32_t n) {
    uint32_t c = 0xffffffffu;
    while (n--) c = g_crc_tab[(c ^ *p++) & 0xff] ^ (c >> 8);
    return c ^ 0xffffffffu;
}

/* ---------------------------------------------------------------- loading */
static void unload(void) {
    for (unsigned i = 0; i < TEX_MAX; ++i)
        if (g_vtex[i]) { pvr_mem_free(g_vtex[i]); g_vtex[i] = NULL; }
    g_h = NULL; g_tex = NULL; g_strips = NULL; g_verts = NULL; g_txbytes = 0;
}

static void compile_headers(void) {
    pvr_poly_cxt_t c;
    for (int b = 0; b < 2; ++b) {
        const pvr_list_t list = b ? PVR_LIST_TR_POLY : PVR_LIST_OP_POLY;
        pvr_poly_cxt_col(&c, list);
        c.gen.culling = (pvr_cull_mode_t)(g_cull == 1 ? PVR_CULLING_CCW : g_cull == 2 ? PVR_CULLING_CW : PVR_CULLING_NONE);
        pvr_poly_compile(&g_hdr_col[b], &c);
        if (!g_h) continue;
        for (uint32_t i = 0; i < g_h->ntex && i < TEX_MAX; ++i) {
            const dqa_tex_t *t = &g_tex[i];
            pvr_poly_cxt_txr(&c, list, (int)t->txrfmt, t->w, t->h, g_vtex[i],
                             g_filter ? PVR_FILTER_BILINEAR : PVR_FILTER_NEAREST);
            c.txr.mipmap = t->mip != 0;
            c.txr.env = g_view == VIEW_MODEL ? PVR_TXRENV_MODULATE : PVR_TXRENV_REPLACE;
            c.gen.culling = g_view == VIEW_MODEL ? (pvr_cull_mode_t)(g_cull == 1 ? PVR_CULLING_CCW : g_cull == 2 ?
                                                                     PVR_CULLING_CW : PVR_CULLING_NONE) : PVR_CULLING_NONE;
            pvr_poly_compile(&g_hdr[i][b], &c);
        }
    }
}

static int refuse(int state, const char *fmt, ...) __attribute__((format(printf, 2, 3)));
static int refuse(int state, const char *fmt, ...) {
    char why[120];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(why, sizeof(why), fmt, ap);
    va_end(ap);
    unload();
    g_state = state;
    dcql_live_log(state == ST_REFUSED ? DCQL_LOG_WARN : DCQL_LOG_ERR, "asset %d %s: %s", (int)g_load_seq,
                  state == ST_REFUSED ? "refused" : "failed", why);
    printf("viewer: asset %d %s\n", (int)g_load_seq, why);
    return -1;
}

static int do_load(void) {
    char path[64];
    unload();                                   /* the old asset's graphics memory first */
    g_state = ST_LOADING;
    g_asset_seq = g_load_seq;
    snprintf(path, sizeof(path), "/pc/%s", g_load_name);
    const uint64_t t0 = timer_ms_gettime64();
    file_t fd = fs_open(path, O_RDONLY);
    if (fd < 0) return refuse(ST_ERROR, "cannot open %s", path);
    const size_t n = fs_total(fd);
    if (n < sizeof(dqa_head_t) || n > BLOCK_BYTES) {
        fs_close(fd);
        return refuse(ST_REFUSED, "%u bytes, the live-asset block holds %u", (unsigned)n, BLOCK_BYTES);
    }
    const ssize_t got = fs_read(fd, g_block, n);
    fs_close(fd);
    const uint32_t read_ms = (uint32_t)(timer_ms_gettime64() - t0);
    if (got != (ssize_t)n) return refuse(ST_ERROR, "read %d of %u bytes", (int)got, (unsigned)n);
    const dqa_head_t *h = (const dqa_head_t *)g_block;
    if (h->magic != DQA_MAGIC || h->version != DQA_VERSION || h->bytes != n)
        return refuse(ST_ERROR, "not a DQA1 file (magic %08x version %u bytes %u)", (unsigned)h->magic,
                      (unsigned)h->version, (unsigned)h->bytes);
    const uint32_t crc = crc32_of(g_block + 16, (uint32_t)n - 16);
    if (crc != h->crc32) return refuse(ST_ERROR, "CRC %08x, the file says %08x", (unsigned)crc, (unsigned)h->crc32);
    if (h->ntex > TEX_MAX) return refuse(ST_REFUSED, "%u textures, the viewer takes %u", (unsigned)h->ntex, TEX_MAX);
    if ((uint64_t)h->tex_off + h->ntex * sizeof(dqa_tex_t) > n ||
        (uint64_t)h->strip_off + h->nstrips * sizeof(dqa_strip_t) > n ||
        (uint64_t)h->vert_off + (uint64_t)h->nverts * sizeof(dqa_vert_t) > n)
        return refuse(ST_ERROR, "tables outside the file");
    if ((uint64_t)h->nverts * sizeof(pvr_vertex_t) > VBUF_BYTES)
        return refuse(ST_REFUSED, "%u strip vertices need %u bytes of the chip's vertex buffer; it has %u",
                      (unsigned)h->nverts, (unsigned)(h->nverts * sizeof(pvr_vertex_t)), VBUF_BYTES);
    const dqa_tex_t *tx = (const dqa_tex_t *)(g_block + h->tex_off);
    uint32_t tb = 0;
    for (uint32_t i = 0; i < h->ntex; ++i) {
        /* the copy into graphics memory goes in 32-byte pieces: the file pads every block to 32 bytes (Flycast run f1:
           a 174,768-byte texture lost its last 16 bytes, the bottom-right texels, when the length was not rounded) */
        if ((uint64_t)tx[i].data_off + ((tx[i].data_bytes + 31u) & ~31u) > n || (uint64_t)tx[i].pal_off + tx[i].pal_n * 4u > n ||
            tx[i].pal_base + tx[i].pal_n > 1024u)
            return refuse(ST_ERROR, "texture %u outside the file", (unsigned)i);
        tb += (tx[i].data_bytes + 31u) & ~31u;
    }
    if (tb > VRAM_BUDGET)
        return refuse(ST_REFUSED, "textures need %u bytes of graphics memory; live assets get %u", (unsigned)tb, VRAM_BUDGET);
    const dqa_strip_t *st = (const dqa_strip_t *)(g_block + h->strip_off);
    for (uint32_t i = 0; i < h->nstrips; ++i)
        if (st[i].n < 3 || st[i].n > DQA_STRIP_MAX || (uint64_t)st[i].first + st[i].n > h->nverts ||
            (st[i].tex != DQA_NO_TEX && st[i].tex >= h->ntex))
            return refuse(ST_ERROR, "strip %u is bad", (unsigned)i);
    const uint64_t t1 = timer_us_gettime64();
    if (h->pal_fmt != 0xffffffffu) pvr_set_pal_format((pvr_palfmt_t)h->pal_fmt);
    for (uint32_t i = 0; i < h->ntex; ++i) {
        const uint32_t nb = (tx[i].data_bytes + 31u) & ~31u;
        g_vtex[i] = pvr_mem_malloc(nb);
        if (!g_vtex[i]) return refuse(ST_REFUSED, "graphics memory: no block of %u bytes", (unsigned)nb);
        pvr_txr_load(g_block + tx[i].data_off, g_vtex[i], nb);
        const uint32_t *pal = (const uint32_t *)(g_block + tx[i].pal_off);
        for (uint32_t k = 0; k < tx[i].pal_n; ++k) pvr_set_pal_entry(tx[i].pal_base + k, pal[k]);
    }
    const uint32_t up_us = (uint32_t)(timer_us_gettime64() - t1);
    g_h = h; g_tex = tx; g_strips = st; g_verts = (const dqa_vert_t *)(g_block + h->vert_off); g_txbytes = tb;
    if (g_view < 0 || (h->kind == DQA_KIND_TEXTURE && g_view == VIEW_MODEL)) g_view = h->kind == DQA_KIND_TEXTURE ? VIEW_FLAT : VIEW_MODEL;
    compile_headers();
    g_state = ST_READY;
    dcql_live_log(DCQL_LOG_INFO, "asset %d loaded: %s, %u bytes read in %u ms, %u textures (%u bytes) up in %u us, "
                  "%u strips, %u vertices", (int)g_load_seq, g_load_name, (unsigned)n, (unsigned)read_ms,
                  (unsigned)h->ntex, (unsigned)tb, (unsigned)up_us, (unsigned)h->nstrips, (unsigned)h->nverts);
    dcql_live_trace(1, 0, "asset loaded");
    return 0;
}

/* ---------------------------------------------------------------- the PC's options */
static int parse_floats(const char *s, float *out, int n) {
    int k = 0;
    while (k < n && *s) {
        char *e;
        out[k++] = strtof(s, &e);
        if (e == s) return -1;
        s = *e == ',' ? e + 1 : e;
    }
    return k;
}

static int on_cmd(int code, const char *arg, unsigned len, char *why, void *user) {
    (void)user; (void)len;
    if (code == DCQL_CMD_PAD) return 0;
    if (code != DCQL_CMD_OPTION) { snprintf(why, 60, "the viewer has no presets"); return 2; }
    const char *v = strchr(arg, '=');
    if (!v) return 2;
    ++v;
    float f[6];
    if (!strncmp(arg, "load=", 5)) {
        const char *c = strchr(v, ':');
        if (!c || c - v > 10 || strlen(c + 1) >= sizeof(g_load_name) || strchr(c + 1, '/') || strstr(c + 1, "..")) {
            snprintf(why, 60, "load=<seq>:<file name>");
            return 2;
        }
        g_load_seq = atoi(v);
        strcpy(g_load_name, c + 1);
        g_load_pending = 1;
        return 0;
    }
    if (!strncmp(arg, "cam=", 4)) {
        if (parse_floats(v, f, 4) != 4 || f[2] < 1.1f || f[2] > 100.f || f[3] < 0.05f || f[3] > 2.8f) {
            snprintf(why, 60, "cam=<yaw>,<pitch>,<dist 1.1..100>,<fov>");
            return 2;
        }
        g_yaw = f[0]; g_pitch = f[1]; g_dist = f[2]; g_fov = f[3];
        return 0;
    }
    if (!strncmp(arg, "light=", 6)) {
        if (parse_floats(v, f, 5) != 5) { snprintf(why, 60, "light=<x>,<y>,<z>,<ambient>,<diffuse>"); return 2; }
        set_light(f[0], f[1], f[2]); g_amb = f[3]; g_dif = f[4];
        return 0;
    }
    if (!strncmp(arg, "bg=", 3)) {
        const unsigned long c = strtoul(v, NULL, 16);
        g_bg[0] = (float)((c >> 16) & 255) / 255.f; g_bg[1] = (float)((c >> 8) & 255) / 255.f; g_bg[2] = (float)(c & 255) / 255.f;
        return 0;
    }
    if (!strncmp(arg, "filter=", 7)) { g_filter = atoi(v) != 0; compile_headers(); return 0; }
    if (!strncmp(arg, "cull=", 5)) { g_cull = atoi(v) & 3; compile_headers(); return 0; }
    if (!strncmp(arg, "view=", 5)) {
        g_view = !strcmp(v, "flat") ? VIEW_FLAT : !strcmp(v, "mips") ? VIEW_MIPS : VIEW_MODEL;
        if (g_view == VIEW_MODEL && g_h && g_h->kind == DQA_KIND_TEXTURE) g_view = VIEW_FLAT;
        compile_headers();
        return 0;
    }
    if (!strncmp(arg, "seq=", 4)) { g_seq = atoi(v); return 0; }
    /* dither=1: the 16-bit screen dithered as games usually draw it; 0 (the default) keeps the exact values, so the
       grab can be compared with the PC render pixel for pixel */
    if (!strncmp(arg, "dither=", 7)) { vid_set_dithering(atoi(v) != 0); return 0; }
    snprintf(why, 60, "no option %.40s", arg);
    return 2;
}

/* ---------------------------------------------------------------- drawing */
static inline uint32_t shade_argb(uint32_t c, float s) {
    if (s > 1.f) s = 1.f;
    const uint32_t k = (uint32_t)(s * 256.f);
    const uint32_t r = (((c >> 16) & 255) * k) >> 8, g = (((c >> 8) & 255) * k) >> 8, b = ((c & 255) * k) >> 8;
    return (c & 0xff000000u) | (r << 16) | (g << 8) | b;
}

/* the camera and lighting contract (docs/ASSETS.md, the same formulas as the PC preview) */
static void draw_model(int blended) {
    const float cy = cosf(g_yaw), sy = sinf(g_yaw), cp = cosf(g_pitch), sp = sinf(g_pitch);
    const float f = 240.f / tanf(g_fov * 0.5f);
    int cur = -2;
    for (uint32_t s = 0; s < g_h->nstrips; ++s) {
        const dqa_strip_t *st = &g_strips[s];
        const int t = st->tex == DQA_NO_TEX ? -1 : st->tex;
        const int tb = t >= 0 && g_tex[t].alpha;
        if (tb != blended) continue;
        if (t != cur) {
            pvr_prim(t >= 0 ? &g_hdr[t][blended] : &g_hdr_col[blended], sizeof(pvr_poly_hdr_t));
            g_ta += sizeof(pvr_poly_hdr_t);
            cur = t;
        }
        const dqa_vert_t *v = g_verts + st->first;
        for (unsigned i = 0; i < st->n; ++i, ++v) {
            const float x1 = v->x * cy - v->z * sy, z1 = v->x * sy + v->z * cy;
            const float y1 = v->y * cp - z1 * sp, z2 = v->y * sp + z1 * cp;
            const float iw = 1.f / (g_dist - z2);
            const float nx = v->nx * (1.f / 127.f), ny = v->ny * (1.f / 127.f), nz = v->nz * (1.f / 127.f);
            const float nx1 = nx * cy - nz * sy, nz1 = nx * sy + nz * cy;
            const float ny1 = ny * cp - nz1 * sp, nz2 = ny * sp + nz1 * cp;
            float d = nx1 * g_L[0] + ny1 * g_L[1] + nz2 * g_L[2];
            if (d < 0.f) d = 0.f;
            pvr_vertex_t *o = &g_strip[i];
            o->flags = i + 1 == st->n ? PVR_CMD_VERTEX_EOL : PVR_CMD_VERTEX;
            o->x = 320.f + x1 * f * iw;
            o->y = 240.f - y1 * f * iw;
            o->z = iw;
            o->u = v->u; o->v = v->v;
            o->argb = shade_argb(v->argb, g_amb + g_dif * d);
            o->oargb = 0;
        }
        pvr_prim(g_strip, st->n * sizeof(pvr_vertex_t));
        g_ta += st->n * sizeof(pvr_vertex_t);
    }
}

static void quad(float x0, float y0, float x1, float y1, float u1, float v1) {
    pvr_vertex_t *o = g_strip;
    const float xs[4] = {x0, x0, x1, x1}, ys[4] = {y0, y1, y0, y1}, us[4] = {0, 0, u1, u1}, vs[4] = {0, v1, 0, v1};
    for (int i = 0; i < 4; ++i) {
        o[i].flags = i == 3 ? PVR_CMD_VERTEX_EOL : PVR_CMD_VERTEX;
        o[i].x = xs[i]; o[i].y = ys[i]; o[i].z = 1.f; o[i].u = us[i]; o[i].v = vs[i];
        o[i].argb = 0xffffffffu; o[i].oargb = 0;
    }
    pvr_prim(o, 4 * sizeof(pvr_vertex_t));
    g_ta += 4 * sizeof(pvr_vertex_t);
}

/* a texture at one texel a pixel, centred (cropped to the screen); "mips": each level beside the next, the level the
   chip picks by itself at that size */
static void draw_flat(int blended) {
    const dqa_tex_t *t = &g_tex[0];
    if ((t->alpha != 0) != blended) return;
    pvr_prim(&g_hdr[0][blended], sizeof(pvr_poly_hdr_t));
    g_ta += sizeof(pvr_poly_hdr_t);
    if (g_view == VIEW_MIPS && t->mip) {
        float x = 8.f;
        for (unsigned w = t->w; w >= 1 && x + (float)w <= 640.f; w >>= 1) {
            const float y = (float)((480 - (int)w) / 2);
            quad(x, y, x + (float)w, y + (float)w, 1.f, 1.f);
            x += (float)w + 8.f;
            if (w == 1) break;
        }
        return;
    }
    const int w = t->w < 640 ? t->w : 640, h = t->h < 480 ? t->h : 480;
    const float x0 = (float)((640 - w) / 2), y0 = (float)((480 - h) / 2);
    quad(x0, y0, x0 + (float)w, y0 + (float)h, (float)w / (float)t->w, (float)h / (float)t->h);
}

static void draw(int blended) {
    if (g_state != ST_READY || !g_h) return;
    if (g_view == VIEW_MODEL && g_h->nstrips) draw_model(blended);
    else if (g_h->ntex) draw_flat(blended);
}

int main(int argc, char **argv) {
    (void)argc; (void)argv;
    cl_init(0);
    const char *args = cl_args();
    const char *p;
    const int secs = (p = strstr(args, "secs=")) ? atoi(p + 5) : 900;
    crc_init();
    set_light(0.4f, 0.75f, 0.5f);
    pvr_init_params_t pp = {.opb_sizes = {PVR_BINSIZE_16, PVR_BINSIZE_0, PVR_BINSIZE_16, PVR_BINSIZE_0, PVR_BINSIZE_0},
                            .vertex_buf_size = VBUF_BYTES, .dma_enabled = 0, .fsaa_enabled = 0,
                            .autosort_disabled = 0, .opb_overflow_count = 0};
    pvr_init(&pp);
    vid_set_dithering(false);
    const uint32_t vfree0 = (uint32_t)pvr_mem_available();
    const int up = cl_link_wait(20000);
    dcql_live_declare(DEF);
    dcql_live_on_command(on_cmd, NULL);
    compile_headers();
    printf("viewer: link %s, graphics memory free %u (live assets get %u), block %u, %d s\n", up ? "up" : "down",
           (unsigned)vfree0, VRAM_BUDGET, BLOCK_BYTES, secs);
    if (vfree0 < VRAM_BUDGET) dcql_live_log(DCQL_LOG_WARN, "graphics memory free %u is under the live-asset budget %u",
                                            (unsigned)vfree0, VRAM_BUDGET);
    uint64_t t_prev = timer_us_gettime64(), sum_sub = 0;
    uint32_t n = 0, worst_sub = 0, drawn = 0;
    const uint64_t t_end = timer_ms_gettime64() + (uint64_t)secs * 1000u;
    while (timer_ms_gettime64() < t_end) {
        if (g_load_pending) { g_load_pending = 0; do_load(); }
        const uint64_t tw = timer_us_gettime64();
        pvr_wait_ready();
        const uint64_t t0 = timer_us_gettime64();
        pvr_set_bg_color(g_bg[0], g_bg[1], g_bg[2]);
        g_ta = 0;
        pvr_scene_begin();
        pvr_list_begin(PVR_LIST_OP_POLY);
        draw(0);
        pvr_list_finish();
        pvr_list_begin(PVR_LIST_TR_POLY);
        draw(1);
        pvr_list_finish();
        pvr_scene_finish();
        const uint64_t t1 = timer_us_gettime64();
        ++n;
        pvr_stats_t ps;
        pvr_get_stats(&ps);
        const uint32_t sub = (uint32_t)(t1 - t0);
        if (g_state == ST_READY) { sum_sub += sub; ++drawn; if (sub > worst_sub) worst_sub = sub; }
        dcql_frame_t fr = {.n = n, .nph = 5, .nctr = 8,
                           .phase_us = {sub, (uint32_t)(ps.reg_last_time / 1000u), (uint32_t)(ps.rnd_last_time / 1000u),
                                        (uint32_t)(t0 - tw), (uint32_t)(t1 - t_prev)},
                           .ctr = {g_seq, g_asset_seq, g_state, g_h ? (int32_t)g_h->nverts : 0,
                                   g_h ? (int32_t)g_h->nstrips : 0, (int32_t)g_txbytes, (int32_t)g_ta,
                                   (int32_t)pvr_mem_available()}};
        t_prev = t1;
        dcql_live_frame(&fr);
    }
    unload();
    cl_report("viewer: %u pictures, %u drawn with an asset\n", (unsigned)n, (unsigned)drawn);
    cl_report("@ Submit mean = %.3f ms\n", drawn ? (double)sum_sub / drawn / 1000.0 : 0.0);
    cl_report("@ Submit worst = %.3f ms\n", worst_sub / 1000.0);
    cl_report("@ Graphics memory free at start = %u B\n", (unsigned)vfree0);
    cl_report("@ Graphics memory free at end = %u B\n", (unsigned)pvr_mem_available());
    cl_exit(0);
}
