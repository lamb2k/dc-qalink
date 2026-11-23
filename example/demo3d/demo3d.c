/* demo3d.c: dc-qalink's sample game (0.3). MIT licence. Original: every shape and colour here is made in code.

   A low-poly craft glides over a tiled floor collecting floating cubes; the score counts them. One texture
   (DEMO3D/FLOOR.BMP, a 24-bit BMP any paint program saves) and one model (DEMO3D/CRAFT.OBJ, a text OBJ any 3D tool
   saves) load from /cd at start, never inside a picture. Under the dc-qalink loader on the broadband adapter, /cd is the
   network disc first (lib/dcql_disc.c), so a texture changed on the PC shows on the next push with no disc change. If a
   file is missing, built-in art stands in, so the program also runs from its own disc or in a link-off build.

   With the link (DEMO_LINK=1, the default build): live data every picture (time to update, draw and the whole
   picture; score; the craft's position), warp presets and options from the PC, pad input from the PC, screen grabs,
   serial lines, and the in-game menu (L + R + START) with a "Demo" page: crash now (the report and registers come
   back), back to the loader, autopilot, speed. DEMO_LINK=0 builds the game alone (no network code at all).

   Controls: stick or D-pad left/right to steer, A faster, B slower. With no input for 3 s the autopilot flies (option). */
#include <kos.h>
#include <dc/perfctr.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "font/spleen8x16.h"
#if DEMO_LINK
#include "cl_client.h"
#include "dcql_live.h"
#include "dcql_menu.h"
#include "dcql_disc.h"
int __real_cdrom_read_toc(cd_toc_t *toc, bool hd);
int __wrap_cdrom_read_toc(cd_toc_t *toc, bool hd) {   /* as example/netdisc: the TOC read Flycast and GDEMU both answer */
    int rc = __real_cdrom_read_toc(toc, true);
    return rc && !hd ? __real_cdrom_read_toc(toc, false) : rc;
}
#endif

#define ARENA 32.0f          /* the floor runs from -ARENA to +ARENA on x and z */
#define TILE 2.0f
#define NCUBES 8
#define NEAR 0.25f
#define FOCAL 417.0f         /* 75 degrees across 640 pixels */
#define MAXV 256
#define MAXT 512

static uint64_t now_ns(void) { return perf_cntr_timer_ns(); }
static void say(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void say(const char *fmt, ...) {   /* one line: the serial console, the PC's serial lines, the menu's Log page */
    char b[160];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(b, sizeof b, fmt, ap);
    va_end(ap);
    dbgio_printf("demo3d: %s\n", b);
#if DEMO_LINK
    char l[170];
    const int n = snprintf(l, sizeof l, "demo3d: %s\n", b);
    cl_line(l, (size_t)n);
    dcql_menu_log(b);
#endif
}

/* ------------------------------------------------------------------ assets: read once at start */
static pvr_ptr_t g_tex;
static int g_tw, g_th;
static char g_tex_from[96] = "built-in", g_mdl_from[96] = "built-in";
static float g_mv[MAXV][3];
static int g_mt[MAXT][3], g_nv, g_nt;
static float g_mn[MAXT][3];   /* each triangle's normal (model space) */

static uint8_t *read_all(const char *path, int *size) {
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    fseek(f, 0, SEEK_END);
    const long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    uint8_t *p = n > 0 && n <= 1024 * 1024 ? malloc((size_t)n) : NULL;
    if (p && fread(p, 1, (size_t)n, f) != (size_t)n) { free(p); p = NULL; }
    fclose(f);
    *size = p ? (int)n : 0;
    return p;
}
static uint32_t le32(const uint8_t *p) { return p[0] | p[1] << 8 | p[2] << 16 | (uint32_t)p[3] << 24; }
static int pow2(int v) { return v >= 8 && v <= 512 && !(v & (v - 1)); }

/* FLOOR.BMP: uncompressed 24- or 32-bit, width and height powers of two from 8 to 512; RGB565 in graphics memory */
static int load_texture(const char *path) {
    int n;
    uint8_t *b = read_all(path, &n);
    if (!b) return -1;
    int ok = n > 54 && b[0] == 'B' && b[1] == 'M';
    const int w = ok ? (int)le32(b + 18) : 0, hs = ok ? (int)le32(b + 22) : 0, h = hs < 0 ? -hs : hs;
    const int bpp = ok ? b[28] : 0, comp = ok ? (int)le32(b + 30) : -1;
    const uint32_t off = ok ? le32(b + 10) : 0, stride = (uint32_t)((w * bpp / 8 + 3) & ~3);
    ok = ok && pow2(w) && pow2(h) && (bpp == 24 || bpp == 32) && (comp == 0 || (comp == 3 && bpp == 32)) &&
         off + stride * (uint32_t)h <= (uint32_t)n;
    if (!ok) { free(b); say("%s: not a 24/32-bit BMP with power-of-two sides (8..512)", path); return -2; }
    uint16_t *px = malloc((size_t)(w * h * 2));
    for (int y = 0; y < h; y++) {
        const uint8_t *row = b + off + stride * (uint32_t)(hs > 0 ? h - 1 - y : y);   /* bottom-up unless height < 0 */
        for (int x = 0; x < w; x++, row += bpp / 8)
            px[y * w + x] = (uint16_t)((row[2] >> 3) << 11 | (row[1] >> 2) << 5 | row[0] >> 3);
    }
    free(b);
    if (g_tex) pvr_mem_free(g_tex);
    g_tex = pvr_mem_malloc((size_t)(w * h * 2));
    pvr_txr_load(px, g_tex, (size_t)(w * h * 2));
    free(px);
    g_tw = w; g_th = h;
    return 0;
}
static void builtin_texture(void) {   /* a tile with a bevelled edge and a ring: our own pattern */
    static uint16_t px[64 * 64];
    for (int y = 0; y < 64; y++)
        for (int x = 0; x < 64; x++) {
            const int e = x < y ? (x < 63 - y ? x : 63 - y) : (y < 63 - x ? y : 63 - x);   /* distance to the edge */
            const float r = sqrtf((float)((x - 32) * (x - 32) + (y - 32) * (y - 32)));
            int v = e < 2 ? 40 : e < 4 ? 150 : 90 + ((x ^ y) & 3) * 4;
            if (fabsf(r - 18.0f) < 1.5f) v = 180;
            px[y * 64 + x] = (uint16_t)((v / 3 >> 3) << 11 | ((v * 3 / 4) >> 2) << 5 | v >> 3);   /* teal-grey */
        }
    g_tex = pvr_mem_malloc(sizeof px);
    pvr_txr_load(px, g_tex, sizeof px);
    g_tw = g_th = 64;
}

/* CRAFT.OBJ: "v x y z" and "f a b c ..." lines (a/b/c forms too; polygons become fans); x right, y up, z forward */
static float g_ctr[3];
static void tri_normals(void) {
    g_ctr[0] = g_ctr[1] = g_ctr[2] = 0;
    for (int i = 0; i < g_nv; i++)
        for (int k = 0; k < 3; k++) g_ctr[k] += g_mv[i][k] / (float)g_nv;
    for (int i = 0; i < g_nt; i++) {
        const float *a = g_mv[g_mt[i][0]], *b = g_mv[g_mt[i][1]], *c = g_mv[g_mt[i][2]];
        const float u[3] = {b[0] - a[0], b[1] - a[1], b[2] - a[2]}, v[3] = {c[0] - a[0], c[1] - a[1], c[2] - a[2]};
        float n[3] = {u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]};
        const float l = sqrtf(n[0] * n[0] + n[1] * n[1] + n[2] * n[2]);
        for (int k = 0; k < 3; k++) n[k] = l > 0 ? n[k] / l : 0;
        /* outward: away from the model's centre (either winding in the file draws right) */
        float o = 0;
        for (int k = 0; k < 3; k++) o += n[k] * ((a[k] + b[k] + c[k]) / 3.0f - g_ctr[k]);
        if (o < 0) { const int t = g_mt[i][1]; g_mt[i][1] = g_mt[i][2]; g_mt[i][2] = t; for (int k = 0; k < 3; k++) n[k] = -n[k]; }
        for (int k = 0; k < 3; k++) g_mn[i][k] = n[k];
    }
}
static int load_model(const char *path) {
    FILE *f = fopen(path, "r");
    if (!f) return -1;
    char line[160];
    int nv = 0, nt = 0;
    while (fgets(line, sizeof line, f)) {
        if (line[0] == 'v' && line[1] == ' ' && nv < MAXV) {
            if (sscanf(line + 2, "%f %f %f", &g_mv[nv][0], &g_mv[nv][1], &g_mv[nv][2]) == 3) nv++;
        } else if (line[0] == 'f' && line[1] == ' ') {
            int idx[16], k = 0;
            for (char *p = strtok(line + 2, " \t\r\n"); p && k < 16; p = strtok(NULL, " \t\r\n")) idx[k++] = atoi(p) - 1;
            for (int i = 1; i + 1 < k && nt < MAXT; i++) {
                if (idx[0] < 0 || idx[i] < 0 || idx[i + 1] < 0) continue;
                g_mt[nt][0] = idx[0]; g_mt[nt][1] = idx[i]; g_mt[nt][2] = idx[i + 1]; nt++;
            }
        }
    }
    fclose(f);
    for (int i = 0; i < nt; i++)
        for (int k = 0; k < 3; k++)
            if (g_mt[i][k] >= nv) { say("%s: a face names vertex %d of %d", path, g_mt[i][k] + 1, nv); return -2; }
    if (nt == 0) { say("%s: no faces", path); return -2; }
    g_nv = nv; g_nt = nt;
    tri_normals();
    return 0;
}
static void builtin_model(void) {   /* a dart: nose, two wing tips, a fin, a tail */
    static const float V[6][3] = {{0, 0.2f, 1.2f}, {-0.9f, 0, -0.6f}, {0.9f, 0, -0.6f}, {0, 0.55f, -0.5f}, {0, -0.15f, -0.4f}, {0, 0.2f, -0.7f}};
    static const int T[8][3] = {{0, 1, 3}, {0, 3, 2}, {0, 4, 1}, {0, 2, 4}, {1, 5, 3}, {2, 3, 5}, {1, 4, 5}, {2, 5, 4}};
    memcpy(g_mv, V, sizeof V); memcpy(g_mt, T, sizeof T);
    g_nv = 6; g_nt = 8;
    tri_normals();
}

/* ------------------------------------------------------------------ the camera and drawing */
typedef struct { float x, y, z, u, v; uint32_t c; } vtx_t;
static float g_eye[3], g_r[3], g_u[3], g_f[3];
static unsigned g_verts;

static void view(const float p[3], vtx_t *o) {
    const float d[3] = {p[0] - g_eye[0], p[1] - g_eye[1], p[2] - g_eye[2]};
    o->x = d[0] * g_r[0] + d[1] * g_r[1] + d[2] * g_r[2];
    o->y = d[0] * g_u[0] + d[1] * g_u[1] + d[2] * g_u[2];
    o->z = d[0] * g_f[0] + d[1] * g_f[1] + d[2] * g_f[2];
}
/* a convex polygon in view space: clipped at the near plane, projected, sent as one strip */
static void poly(const vtx_t *in, int n) {
    vtx_t c[8];
    int m = 0;
    for (int i = 0; i < n; i++) {
        const vtx_t *a = &in[i], *b = &in[(i + 1) % n];
        if (a->z >= NEAR) c[m++] = *a;
        if ((a->z >= NEAR) != (b->z >= NEAR) && m < 8) {
            const float t = (NEAR - a->z) / (b->z - a->z);
            vtx_t *o = &c[m++];
            o->x = a->x + (b->x - a->x) * t; o->y = a->y + (b->y - a->y) * t; o->z = NEAR;
            o->u = a->u + (b->u - a->u) * t; o->v = a->v + (b->v - a->v) * t; o->c = a->c;
        }
    }
    if (m < 3) return;
    int order[8], k = 0;   /* a fan as a strip: 0, 1, m-1, 2, m-2, ... */
    for (int lo = 1, hi = m - 1, i = 0; k < m; i++) order[k++] = i == 0 ? 0 : (i & 1) ? lo++ : hi--;
    for (int i = 0; i < m; i++) {
        const vtx_t *s = &c[order[i]];
        pvr_vertex_t v;
        v.flags = i == m - 1 ? PVR_CMD_VERTEX_EOL : PVR_CMD_VERTEX;
        v.x = 320.0f + FOCAL * s->x / s->z; v.y = 240.0f - FOCAL * s->y / s->z; v.z = 1.0f / s->z;
        v.u = s->u; v.v = s->v; v.argb = s->c; v.oargb = 0;
        pvr_prim(&v, sizeof v);
    }
    g_verts += (unsigned)m;
}
static uint32_t shade(uint32_t rgb, float k) {
    if (k < 0) k = 0;
    if (k > 1) k = 1;
    const uint32_t r = (uint32_t)(((rgb >> 16) & 255) * k), g = (uint32_t)(((rgb >> 8) & 255) * k), b = (uint32_t)((rgb & 255) * k);
    return 0xff000000u | r << 16 | g << 8 | b;
}

/* ------------------------------------------------------------------ the game */
static float g_px, g_pz, g_yaw, g_turn, g_t;
static int g_score, g_taken, g_speed = 3, g_auto = 1, g_idle;
static float g_cube[NCUBES][3];
static uint32_t g_seed = 12345u;
static const uint32_t CUBE_RGB[4] = {0xffd23c, 0x3cc8ff, 0xff5ab4, 0x8cff5a};
static float rnd(void) { g_seed = g_seed * 1664525u + 1013904223u; return (float)(g_seed >> 8) / 16777216.0f; }
static void place(int i) {
    do { g_cube[i][0] = (rnd() * 2 - 1) * (ARENA - 3); g_cube[i][2] = (rnd() * 2 - 1) * (ARENA - 3); }
    while ((g_cube[i][0] - g_px) * (g_cube[i][0] - g_px) + (g_cube[i][2] - g_pz) * (g_cube[i][2] - g_pz) < 100.0f);
    g_cube[i][1] = rnd() * 6.28f;   /* its bob and spin phase */
}
static void new_round(void) {
    g_px = g_pz = g_yaw = 0; g_score = 0; g_seed = 12345u;
    for (int i = 0; i < NCUBES; i++) place(i);
}

typedef struct { uint32_t buttons; int x; } pad_t;
static void update(pad_t p, float dt) {
    int steer = p.x < -40 ? -1 : p.x > 40 ? 1 : 0;
    if (p.buttons & CONT_DPAD_LEFT) steer = -1;
    if (p.buttons & CONT_DPAD_RIGHT) steer = 1;
    g_idle = steer || (p.buttons & (CONT_A | CONT_B)) ? 0 : g_idle + 1;
    float want = (float)steer;
    if (g_auto && g_idle > 180) {   /* autopilot: towards the nearest cube */
        int best = 0;
        float bd = 1e9f;
        for (int i = 0; i < NCUBES; i++) {
            const float dx = g_cube[i][0] - g_px, dz = g_cube[i][2] - g_pz, d = dx * dx + dz * dz;
            if (d < bd) { bd = d; best = i; }
        }
        float a = atan2f(g_cube[best][0] - g_px, g_cube[best][2] - g_pz) - g_yaw;
        while (a > 3.14159f) a -= 6.28318f;
        while (a < -3.14159f) a += 6.28318f;
        want = a > 0.05f ? 1.0f : a < -0.05f ? -1.0f : a * 20.0f;
    }
    g_turn += (want - g_turn) * 0.15f;
    g_yaw += g_turn * 2.2f * dt;
    float v = 4.0f + 2.0f * (float)g_speed;
    if (p.buttons & CONT_A) v *= 1.6f;
    if (p.buttons & CONT_B) v *= 0.4f;
    g_px += sinf(g_yaw) * v * dt; g_pz += cosf(g_yaw) * v * dt;
    const float lim = ARENA - 1.0f;
    if (g_px < -lim) g_px = -lim;
    if (g_px > lim) g_px = lim;
    if (g_pz < -lim) g_pz = -lim;
    if (g_pz > lim) g_pz = lim;
    for (int i = 0; i < NCUBES; i++) {
        const float dx = g_cube[i][0] - g_px, dz = g_cube[i][2] - g_pz;
        if (dx * dx + dz * dz < 1.6f) {
            g_score++; g_taken++;
            say("cube taken at %.1f %.1f, score %d", (double)g_cube[i][0], (double)g_cube[i][2], g_score);
#if DEMO_LINK
            dcql_live_trace(1, 1, "cube taken");
#endif
            place(i);
        }
    }
    g_t += dt;
}

static void camera(void) {
    const float fx = sinf(g_yaw), fz = cosf(g_yaw);
    g_eye[0] = g_px - fx * 7.0f; g_eye[1] = 3.4f; g_eye[2] = g_pz - fz * 7.0f;
    const float tgt[3] = {g_px + fx * 4.0f, 0.4f, g_pz + fz * 4.0f};
    float f[3] = {tgt[0] - g_eye[0], tgt[1] - g_eye[1], tgt[2] - g_eye[2]};
    const float l = sqrtf(f[0] * f[0] + f[1] * f[1] + f[2] * f[2]);
    for (int k = 0; k < 3; k++) g_f[k] = f[k] / l;
    const float rl = sqrtf(g_f[2] * g_f[2] + g_f[0] * g_f[0]);
    g_r[0] = g_f[2] / rl; g_r[1] = 0; g_r[2] = -g_f[0] / rl;                    /* up x forward */
    g_u[0] = g_f[1] * g_r[2] - g_f[2] * g_r[1];                                /* forward x right */
    g_u[1] = g_f[2] * g_r[0] - g_f[0] * g_r[2];
    g_u[2] = g_f[0] * g_r[1] - g_f[1] * g_r[0];
}

static pvr_poly_hdr_t g_hdr_tex, g_hdr_col;
static void draw_floor(void) {
    pvr_prim(&g_hdr_tex, sizeof g_hdr_tex);
    const int n = (int)(ARENA / TILE);
    for (int j = -n; j < n; j++)
        for (int i = -n; i < n; i++) {
            const float x0 = (float)i * TILE, z0 = (float)j * TILE, cx = x0 + TILE / 2 - g_eye[0], cz = z0 + TILE / 2 - g_eye[2];
            const float d2 = cx * cx + cz * cz;
            if (d2 > 34.0f * 34.0f || cx * g_f[0] + cz * g_f[2] < -TILE * 1.5f) continue;   /* far, or behind */
            const float x[4] = {x0, x0 + TILE, x0 + TILE, x0}, z[4] = {z0, z0, z0 + TILE, z0 + TILE};
            const float uu[4] = {0, 1, 1, 0}, vv[4] = {0, 0, 1, 1};
            vtx_t q[4];
            for (int k = 0; k < 4; k++) {
                const float p[3] = {x[k], 0, z[k]};
                view(p, &q[k]);
                q[k].u = uu[k]; q[k].v = vv[k];
                const float dx = x[k] - g_eye[0], dz = z[k] - g_eye[2];
                q[k].c = shade(0xffffff, 1.15f - sqrtf(dx * dx + dz * dz) / 30.0f);   /* fades into the dark */
            }
            poly(q, 4);
        }
}
static void draw_cube(const float c[3], float s, float spin, uint32_t rgb) {
    static const int F[6][4] = {{0, 1, 3, 2}, {4, 6, 7, 5}, {0, 4, 5, 1}, {2, 3, 7, 6}, {0, 2, 6, 4}, {1, 5, 7, 3}};
    static const float N[6][3] = {{0, 0, -1}, {0, 0, 1}, {0, -1, 0}, {0, 1, 0}, {-1, 0, 0}, {1, 0, 0}};
    const float cs = cosf(spin), sn = sinf(spin);
    vtx_t v[8];
    for (int i = 0; i < 8; i++) {
        const float lx = (i & 1 ? s : -s), ly = (i & 2 ? s : -s), lz = (i & 4 ? s : -s);
        const float p[3] = {c[0] + lx * cs + lz * sn, c[1] + ly, c[2] - lx * sn + lz * cs};
        view(p, &v[i]);
        v[i].u = v[i].v = 0;
    }
    for (int f = 0; f < 6; f++) {
        const float nx = N[f][0] * cs + N[f][2] * sn, ny = N[f][1], nz = -N[f][0] * sn + N[f][2] * cs;
        const float vx = c[0] - g_eye[0], vy = c[1] - g_eye[1], vz = c[2] - g_eye[2];
        if (nx * vx + ny * vy + nz * vz > 0) continue;   /* faces away */
        vtx_t q[4];
        const uint32_t col = shade(rgb, 0.62f + 0.38f * (0.4f * nx + 0.8f * ny - 0.45f * nz));
        for (int k = 0; k < 4; k++) { q[k] = v[F[f][k]]; q[k].c = col; }
        poly(q, 4);
    }
}
static void draw_craft(void) {
    const float bank = -g_turn * 0.5f, cb = cosf(bank), sb = sinf(bank), cy = cosf(g_yaw), sy = sinf(g_yaw);
    const float hy = 0.7f + 0.08f * sinf(g_t * 3.0f);
    vtx_t v[MAXV];
    float w[MAXV][3];
    for (int i = 0; i < g_nv; i++) {
        const float *m = g_mv[i];
        const float bx = m[0] * cb - m[1] * sb, by = m[0] * sb + m[1] * cb;   /* bank about the nose axis */
        w[i][0] = g_px + bx * cy + m[2] * sy; w[i][1] = hy + by; w[i][2] = g_pz - bx * sy + m[2] * cy;
        view(w[i], &v[i]);
        v[i].u = v[i].v = 0;
    }
    for (int t = 0; t < g_nt; t++) {
        const float *n = g_mn[t];
        const float bx = n[0] * cb - n[1] * sb, by = n[0] * sb + n[1] * cb;
        const float nx = bx * cy + n[2] * sy, ny = by, nz = -bx * sy + n[2] * cy;
        const float *a = w[g_mt[t][0]];
        if (nx * (a[0] - g_eye[0]) + ny * (a[1] - g_eye[1]) + nz * (a[2] - g_eye[2]) > 0) continue;
        vtx_t q[3];
        const uint32_t col = shade(ny > 0.6f ? 0xff8a3c : 0xe0e4ea, 0.4f + 0.6f * (0.4f * nx + 0.8f * ny - 0.45f * nz));
        for (int k = 0; k < 3; k++) { q[k] = v[g_mt[t][k]]; q[k].c = col; }
        poly(q, 3);
    }
}

/* ------------------------------------------------------------------ text and the menu: untextured sprites */
static pvr_sprite_hdr_t g_spr;
static uint32_t g_rgb = 0;
static float g_sz;
static void spr_colour(uint32_t rgb) {
    if (rgb == g_rgb) return;
    g_rgb = rgb;
    ((uint32_t *)&g_spr)[4] = 0xff000000u | rgb;   /* a sprite header's colour word */
    pvr_prim(&g_spr, sizeof g_spr);
}
static void spr(float x, float y, float w, float h) {
    pvr_sprite_col_t v;
    g_sz += 0.01f;
    v.flags = PVR_CMD_VERTEX_EOL;
    v.ax = x; v.ay = y; v.az = g_sz; v.bx = x + w; v.by = y; v.bz = g_sz;
    v.cx = x + w; v.cy = y + h; v.cz = g_sz; v.dx = x; v.dy = y + h;
    v.d1 = v.d2 = v.d3 = v.d4 = 0;
    pvr_prim(&v, sizeof v);
}
static __attribute__((unused)) void spr_box(int x, int y, int w, int h, uint32_t rgb, void *u) { (void)u; spr_colour(rgb); spr((float)x, (float)y, (float)w, (float)h); }
static void spr_glyph(int x, int y, const uint8_t rows[16], uint32_t rgb, void *u) {
    (void)u;
    spr_colour(rgb);
    for (int r = 0; r < 16; r++)   /* one sprite a run of set pixels in a row */
        for (int c = 0; c < 8;) {
            if (!(rows[r] & (0x80u >> c))) { c++; continue; }
            int e = c;
            while (e < 8 && (rows[r] & (0x80u >> e))) e++;
            spr((float)(x + c), (float)(y + r), (float)(e - c), 1);
            c = e;
        }
}
static void text(int x, int y, uint32_t rgb, const char *s) {
    for (; *s; s++, x += 8)
        if (*s > DCQL_FONT_FIRST && *s <= DCQL_FONT_LAST) spr_glyph(x, y, dcql_font[*s - DCQL_FONT_FIRST], rgb, 0);
}
static void sprites_begin(void) {
    pvr_sprite_cxt_t cxt;
    pvr_sprite_cxt_col(&cxt, PVR_LIST_OP_POLY);
    cxt.depth.comparison = PVR_DEPTHCMP_GEQUAL;
    pvr_sprite_compile(&g_spr, &cxt);
    g_rgb = 0; g_sz = 10.0f;
    spr_colour(0xffffff);
}

/* ------------------------------------------------------------------ the link: live data, commands, the menu */
#if DEMO_LINK
static const char LIVE_DEF[] =
    "program demo3d.elf\nrate 60\n"
    "phase update 2000 - Game update\nphase draw 8000 - Transform and submit\nphase wait - - Wait for the chip\n"
    "phase frame 16667 total Picture\n"
    "ctr score 0 Game Score\nctr taken 0 Game Cubes taken\nctr verts 0 Cost Vertices\n"
    "lane 1 Game events\n"
    "preset start Arena Start\npreset corner Arena Far corner\n"
    "option speed int 1 8 3 Speed\noption autopilot int 0 1 1 Autopilot\n";
static pad_t g_pcpad;
static int g_pcpad_left;
static void warp(const char *id) {
    if (!strcmp(id, "start")) { g_px = g_pz = g_yaw = 0; }
    else { g_px = ARENA - 4; g_pz = ARENA - 4; g_yaw = 3.92699f; }
    say("warp %s", id);
}
static int on_cmd(int code, const char *arg, unsigned n, char *why, void *u) {
    (void)u;
    char a[64];
    const unsigned k = n < sizeof a - 1 ? n : sizeof a - 1;
    memcpy(a, arg, k); a[k] = 0;
    if (code == DCQL_CMD_WARP) {
        if (strcmp(a, "start") && strcmp(a, "corner")) { snprintf(why, 60, "no preset %.40s", a); return 2; }
        warp(a);
        return 0;
    }
    if (code == DCQL_CMD_OPTION) {
        char *eq = strchr(a, '=');
        const int v = eq ? atoi(eq + 1) : 0;
        if (eq && !strncmp(a, "speed=", 6) && v >= 1 && v <= 8) { g_speed = v; return 0; }
        if (eq && !strncmp(a, "autopilot=", 10)) { g_auto = v != 0; g_idle = g_auto ? 1000 : 0; return 0; }
        snprintf(why, 60, "no option %.40s", a);
        return 2;
    }
    if (code == DCQL_CMD_PAD && n >= 12) {   /* u32 buttons, i8 x, i8 y, u8 lt, u8 rt, u32 pictures */
        const uint8_t *p = (const uint8_t *)arg;
        /* the wire's buttons (pc/bridge/wire.py PAD_BITS: A 1, B 2, X 4, Y 8, START 16, UP 32, DOWN 64, LEFT 128,
           RIGHT 256) as KOS's controller bits, the triggers folded in as the menu reads them */
        static const uint32_t KOS[9] = {CONT_A, CONT_B, CONT_X, CONT_Y, CONT_START, CONT_DPAD_UP, CONT_DPAD_DOWN,
                                        CONT_DPAD_LEFT, CONT_DPAD_RIGHT};
        const uint32_t w = le32(p);
        uint32_t k = (p[6] >= 128 ? DCQL_MENU_LT : 0) | (p[7] >= 128 ? DCQL_MENU_RT : 0);
        for (int i = 0; i < 9; i++)
            if (w & (1u << i)) k |= KOS[i];
        g_pcpad.buttons = k;
        g_pcpad.x = (int8_t)p[4];
        const uint32_t pics = le32(p + 8);
        g_pcpad_left = pics ? (int)pics : g_pcpad_left ? g_pcpad_left : 30;
        return 0;
    }
    snprintf(why, 60, "not in this program");
    return 2;
}
static int act_crash(const dcql_menu_row_t *r, int v, void *u) {
    (void)r; (void)v; (void)u;
    say("crash requested from the menu: a misaligned write, then an illegal instruction");
    cl_report("demo3d: crash requested from the menu at score %d\n", g_score);
    thd_sleep(200);   /* the serial line leaves first */
    volatile uint32_t *p = (volatile uint32_t *)0x8c100002u;
    *p = 0xdeadbeefu;   /* the SH-4 raises an address error; Flycast does not, so: */
    __asm__ __volatile__(".word 0xfffd");   /* an illegal instruction */
    abort();
}
static int act_exit(const dcql_menu_row_t *r, int v, void *u) {
    (void)r; (void)v; (void)u;
    say("back to the loader from the menu, score %d", g_score);
    cl_report("demo3d: back to the loader from the menu, score %d, cubes %d\n", g_score, g_taken);
    cl_exit(0);
}
static int act_round(const dcql_menu_row_t *r, int v, void *u) { (void)r; (void)v; (void)u; new_round(); say("new round"); return 1; }
static void info_score(char *o, int n, void *u) { (void)u; snprintf(o, (size_t)n, "%d (cubes %d)", g_score, g_taken); }
static void info_tex(char *o, int n, void *u) { (void)u; snprintf(o, (size_t)n, "%dx%d, %s", g_tw, g_th, g_tex_from); }
static void info_mdl(char *o, int n, void *u) { (void)u; snprintf(o, (size_t)n, "%d triangles, %s", g_nt, g_mdl_from); }
static void info_pos(char *o, int n, void *u) { (void)u; snprintf(o, (size_t)n, "%.1f %.1f", (double)g_px, (double)g_pz); }
static const dcql_menu_row_t DEMO_PAGE[] = {
    DCQL_MENU_HEAD("Test the link"),
    DCQL_MENU_ACTION("Crash now", "report + registers", act_crash, 0),
    DCQL_MENU_ACTION("Back to the loader", "DONE", act_exit, 0),
    DCQL_MENU_GAP,
    DCQL_MENU_HEAD("Sample game"),
    DCQL_MENU_ACTION("New round", "score 0", act_round, 0),
    DCQL_MENU_TOGGLE("Autopilot", &g_auto, 0, 0),
    DCQL_MENU_NUMBER("Speed", &g_speed, 1, 8, 1, 0, 0),
    DCQL_MENU_INFO("Score", info_score, 0),
    DCQL_MENU_INFO("Texture", info_tex, 0),
    DCQL_MENU_INFO("Model", info_mdl, 0),
};
static const dcql_menu_row_t SNAP[] = {DCQL_MENU_INFO("Score", info_score, 0), DCQL_MENU_INFO("Position", info_pos, 0)};
static const dcql_menu_target_t SPRITES = {spr_box, spr_glyph, 0};
#endif

static void load_assets(void) {
    const uint64_t t0 = now_ns();
    const char *where = "disc";
#if DEMO_LINK
    if (cl_active()) {
        dcql_disc_config_t c = {0};
        c.link_wait_ms = 8000;
        const int rc = dcql_disc_mount(&c);
        where = rc == 0 ? "network disc" : "disc";
        if (rc) say("network disc not mounted (%d): reading the real disc", rc);
    }
#endif
    if (load_texture("/cd/DEMO3D/FLOOR.BMP") == 0) snprintf(g_tex_from, sizeof g_tex_from, "FLOOR.BMP, %s", where);
    else builtin_texture();
    if (load_model("/cd/DEMO3D/CRAFT.OBJ") == 0) snprintf(g_mdl_from, sizeof g_mdl_from, "CRAFT.OBJ, %s", where);
    else builtin_model();
    const unsigned ms = (unsigned)((now_ns() - t0) / 1000000u);
    say("texture %dx%d from %s; model %d triangles from %s; %u ms", g_tw, g_th, g_tex_from, g_nt, g_mdl_from, ms);
#if DEMO_LINK
    dcql_disc_report();
    cl_report("demo3d: texture %dx%d from %s; model %d triangles from %s\n@ asset load = %u ms\n", g_tw, g_th, g_tex_from,
              g_nt, g_mdl_from, ms);
    if (cl_active()) dcql_disc_unmount();   /* nothing more to read: /cd is the real disc again */
#endif
}

static pad_t read_pad(void) {
    pad_t p = {0, 0};
    maple_device_t *d = maple_enum_type(0, MAPLE_FUNC_CONTROLLER);
    cont_state_t *s = d ? (cont_state_t *)maple_dev_status(d) : NULL;
    if (s) {
        p.buttons = s->buttons;
        p.x = s->joyx;
#if DEMO_LINK
        p.buttons = dcql_menu_pad(s->buttons, s->ltrig, s->rtrig);
#endif
    }
#if DEMO_LINK
    if (g_pcpad_left > 0) { g_pcpad_left--; p.buttons |= g_pcpad.buttons; if (g_pcpad.x) p.x = g_pcpad.x; }
#endif
    return p;
}

int main(int argc, char **argv) {
    (void)argc; (void)argv;
    pvr_init_params_t params;
    memset(&params, 0, sizeof params);
    params.opb_sizes[0] = PVR_BINSIZE_16;   /* the opaque list only */
    params.vertex_buf_size = 512 * 1024;
    params.opb_overflow_count = 3;
    pvr_init(&params);
    pvr_set_bg_color(0.02f, 0.03f, 0.08f);
    perf_cntr_timer_enable();
#if DEMO_LINK
    cl_init(0);
    dcql_live_declare(LIVE_DEF);
    dcql_live_on_command(on_cmd, NULL);
    dcql_menu_config_t mc = {0};
    mc.snapshot = SNAP; mc.nsnapshot = 2;
    dcql_menu_init(&mc);
    dcql_menu_add_page("Demo", DEMO_PAGE, (int)(sizeof DEMO_PAGE / sizeof DEMO_PAGE[0]));
#endif
    load_assets();
    pvr_poly_cxt_t cxt;
    pvr_poly_cxt_txr(&cxt, PVR_LIST_OP_POLY, PVR_TXRFMT_RGB565 | PVR_TXRFMT_NONTWIDDLED, g_tw, g_th, g_tex, PVR_FILTER_BILINEAR);
    cxt.gen.culling = PVR_CULLING_NONE;   /* the program culls faces itself */
    pvr_poly_compile(&g_hdr_tex, &cxt);
    pvr_poly_cxt_col(&cxt, PVR_LIST_OP_POLY);
    cxt.gen.culling = PVR_CULLING_NONE;
    pvr_poly_compile(&g_hdr_col, &cxt);
    new_round();
    say("started: steer with the stick, A faster, B slower%s", DEMO_LINK ? ", L+R+START the menu" : "");
    uint64_t last = now_ns();
    for (;;) {
        const uint64_t t0 = now_ns();
        const uint32_t frame_us = (uint32_t)((t0 - last) / 1000u);
        last = t0;
        pad_t p = read_pad();
        int menu = 0;
#if DEMO_LINK
        menu = dcql_menu_input(p.buttons);
#endif
        if (menu) p.buttons = 0, p.x = 0;
        update(p, menu ? 0 : 1.0f / 60.0f);
        const uint64_t tw = now_ns();
        pvr_wait_ready();   /* the chip ready for the next picture: the wait is its own phase */
        const uint64_t t1 = now_ns();
        camera();
        pvr_scene_begin();
        pvr_list_begin(PVR_LIST_OP_POLY);
        g_verts = 0;
        draw_floor();
        pvr_prim(&g_hdr_col, sizeof g_hdr_col);
        for (int i = 0; i < NCUBES; i++) {
            const float c[3] = {g_cube[i][0], 1.1f + 0.3f * sinf(g_t * 2.0f + g_cube[i][1]), g_cube[i][2]};
            draw_cube(c, 0.45f, g_t * 1.5f + g_cube[i][1], CUBE_RGB[i & 3]);
        }
        draw_craft();
        const uint64_t t2 = now_ns();
        sprites_begin();
        char s[48];
        snprintf(s, sizeof s, "SCORE %d", g_score);
        text(24, 20, 0xffffff, s);
        if (g_auto && g_idle > 180) text(24, 40, 0x8fd0ff, "AUTOPILOT");
#if DEMO_LINK
        if (!dcql_menu_is_open()) { spr_box(16, 438, 236, 28, 0x101418, 0); text(24, 444, 0xb4bac2, "L + R + START: QA LINK MENU"); }
        else dcql_menu_draw(&SPRITES);
#endif
        pvr_list_finish();
        pvr_scene_finish();
#if DEMO_LINK
        dcql_frame_t f = {0};
        f.nph = 4; f.phase_us[0] = (uint32_t)((tw - t0) / 1000u); f.phase_us[1] = (uint32_t)((t2 - t1) / 1000u);
        f.phase_us[2] = (uint32_t)((t1 - tw) / 1000u); f.phase_us[3] = frame_us;
        f.nctr = 3; f.ctr[0] = g_score; f.ctr[1] = g_taken; f.ctr[2] = (int32_t)g_verts;
        f.has_pos = 1; f.pos[0] = g_px; f.pos[1] = 0.7f; f.pos[2] = g_pz;
        dcql_live_frame(&f);
#else
        (void)frame_us; (void)tw; (void)t1; (void)t2;
#endif
    }
    return 0;
}
