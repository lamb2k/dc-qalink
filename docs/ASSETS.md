# Live assets (dc-qalink 0.2, plan L5b)

Drop a texture or a still model on QA Link Cloud (or POST it to the bridge), switch on "view on console", and the
console's own picture of it sits beside the PC render on one camera, with a difference view and what it costs.

```
QA Link Cloud Assets workspace          bridge (pc/bridge/assets.py)                   Dreamcast (example/viewer)
  drop file / pick from the build  -->  convert (pc/assets) + budget check
  camera {yaw, pitch, dist, fov}   -->  store <data>/assets/<id>.dqa  -- /pc file service -->  load=<seq>:<id>.dqa
                                        options cam / light / bg / view  -- live commands -->  draw every picture
  side by side, difference, cost   <--  PC render + diff + cost           <-- grab, records --  60 pictures measured
```

## Pieces
| Piece | Where | What |
|---|---|---|
| Console viewer | `example/viewer/` (`viewer.c`, `dqa.h`, `Makefile`) | A program the loader runs. Reads a DQA1 file into one fixed 4 MiB block, copies its textures into graphics memory as they are, draws the asset with the PC's camera, sends a live-data record a picture (its costs) and answers the bridge's grabs |
| Converters | `pc/assets/` (`image.py`, `pvrtex.py`, `model.py`, `dqa.py`) | PNG/JPEG to Dreamcast textures (RGB565, ARGB1555, ARGB4444 twiddled; VQ; PAL4/PAL8; mip maps); OBJ (with .mtl and its texture) and glTF 2.0 (.gltf with .bin or data URIs, .glb) to triangle strips; each says what it lost |
| Reference render | `pc/assets/render.py` | The same view drawn on the PC at 640x480 from the bytes that were sent, written as the 16-bit screen holds it; the difference view |
| Bridge endpoints | `pc/bridge/assets.py` (routed from `api.py`) | Convert, store, send, set the camera, grab, compare, measure, watch files, log every round trip |
| Test assets | `pc/assets/gen.py` | A test card (with and without alpha), a checker torus OBJ, a one-sided flag, a glTF cube. No game data |
| Host tests | `tests/test_assets.py` | 13 checks; `--controls` breaks four pieces and each check must fail |

Python: the converters need **numpy**; Pillow is optional (JPEG and better resizing; PNG is read without it). Without
numpy the bridge runs and `/v0/assets` answers 501 with the reason. Install them into the interpreter's own
site-packages: a bridge started with `python -I` (as the desktop app starts it) does not see `pip install --user`.

Game formats never enter this repository: a game's own decoders are private plugins (QA Link Cloud loads them from a
folder outside the tree) that hand the bridge PNG and OBJ/glTF, or a ready DQA1 file.

## Contract 2a: the camera and lighting (one definition for the PC preview and the console)
Held by QA Link Cloud's Assets editor as `Cam {yaw, pitch, dist, fov}` (radians; `src/editors/pictures.ts`).

- **Model space:** the converter centres the model on its bounding box and scales it to a bounding sphere of radius 1
  (the centre and scale it applied are in the file and the record). Right-handed, y up.
- **Rotation** (cy = cos yaw, sy = sin yaw, cp = cos pitch, sp = sin pitch), for a position (x, y, z):
  `x1 = x cy - z sy;  z1 = x sy + z cy;  y1 = y cp - z1 sp;  z2 = y sp + z1 cp`
- **Projection** at W x H = 640 x 480: `depth = dist - z2` (the camera sits on +z at yaw 0 looking towards -z),
  `f = (H / 2) / tan(fov / 2)`, `sx = W/2 + x1 f / depth`, `sy = H/2 - y1 f / depth`. `dist` is 1.1 to 100 (the camera
  stays outside the bounding sphere, so nothing needs clipping), `fov` 0.05 to 2.8.
  The app's mock today uses `depth = z2 + dist`, which shows the model mirrored (seen from behind with x kept); the
  app takes this formula when it is wired (item 6).
- **Lighting**, per vertex: the normal through the same rotation gives (nx1, ny1, nz2); `shade = ambient + diffuse
  * max(0, n . L)` with `L` a unit vector in view space (+z towards the viewer); default `L = normalize(0.4, 0.75, 0.5)`,
  ambient 0.25, diffuse 0.75 (the app's mock's numbers). The vertex colour is multiplied by `min(shade, 1)` as
  `c * int(s * 256) >> 8`; a texture is modulated by it.
- **Front faces** are counter-clockwise on the screen (with y down: signed area < 0); back faces are culled (`cull`
  `cw`, the default; `none` and `ccw` exist for checks).
- **Background** black by default; **filter** nearest by default (bilinear on request); **dithering** off by default
  on the console so the grab holds exact 16-bit values (`dither=1` shows the game-like dithered screen; then the
  difference view is not exact).
- **Views:** `model` (the default for models), `flat` (the default for textures: the texture at one texel a pixel,
  centred, cropped to the screen), `mips` (each mip level side by side, the level the chip picks at that size).

JSON, as the bridge takes it:
```json
{"camera": {"yaw": 0.6109, "pitch": 0.4887, "dist": 3.1, "fov": 0.8727},
 "light": {"dir": [0.4, 0.75, 0.5], "ambient": 0.25, "diffuse": 0.75},
 "bg": [0, 0, 0], "filter": "nearest", "view": "model", "cull": "cw"}
```

## The budget (the Assets editor's rule; the viewer enforces it too)
| Resource | Live assets get | Checked by |
|---|---|---|
| Main memory (the asset block: the whole DQA1 file) | a quarter of 16 MiB = 4,194,304 B | `dqa.budget()` before sending; the viewer refuses a larger file before reading it |
| Graphics memory (textures, each rounded to 32 B) | half of 8 MiB = 4,194,304 B | both; the viewer also refuses when `pvr_mem_malloc` finds no block |
| The viewer's vertex buffer for one picture | 1 MiB (32 B a strip vertex: 32,768) | both |
An asset over budget is `refused` with the reason and never sent. The viewer's own size: text 301 KB (mostly
KallistiOS and the link), bss 4.37 MB (the 4 MiB block, a 32 KiB strip buffer, the link's rings); graphics memory free
after `pvr_init` with a 1 MiB vertex buffer: 4,971,264 B (Flycast), so the 4 MiB budget fits.

## The DQA1 file (`example/viewer/dqa.h`, `pc/assets/dqa.py`)
Little-endian, every block aligned to 32 bytes. Head (80 B): magic `DQA1`, kind (1 texture, 2 model), version 1, file
bytes, CRC-32 of bytes 16 to the end, the source's SHA-256 (first 16 bytes), counts and offsets of the texture records
(32 B: format word with palette bank, width, height, data offset and bytes, palette offset, count and first entry,
mip, alpha), the strips (16 B: first vertex, count, texture) and the vertices (32 B: position, normal x127, uv, ARGB),
the normalisation (centre, scale), flags and the palette format. Texture data is in graphics memory's own layout
(twiddled; mip chain from 1x1 with the chip's padding; VQ codebook first), so the console copies it as it is.

## Bridge endpoints (contract 1, additive)
| Method and path | Level | What |
|---|---|---|
| `POST /v0/assets` | control | `{name, base64}` or `{path}` (a file on the bridge's PC below `--asset-root`, or `--files` when that is not given; with neither, a path import is **403** and uploads still work; an OBJ's .mtl and textures are read beside it, below the same root, at most 16 MiB of dependencies in all) or multipart with an `asset` file; options `format` (`auto`, `rgb565`, `argb1555`, `argb4444`, `vq`, `pal4`, `pal8`), `mips` (true), `send` (false). Answers the **asset record** |
| `GET /v0/assets` | read | `{assets: [record], viewer: {linked, run, loaded, state, vfree, tier}, budget: {ram, vram, vbuf}, watching: [...]}` |
| `GET /v0/assets/{id}` | read | The record; `/file` the DQA1 bytes; `/render.png?yaw&pitch&dist&fov&view&filter&bg&cull` the PC render alone |
| `POST /v0/assets/viewer` | run | Push the viewer through the loader (`--viewer <viewer.elf>`) and wait for its link |
| `POST /v0/assets/{id}/view` | control (run with `start_viewer: true`) | The round trip with the camera JSON above: send if not loaded, set the view, measure 60 pictures, grab, render, compare. Answers the **view** |
| `GET /v0/assets/views` | read | Every view, newest first; `/v0/assets/views/{view}/(pc|console|diff|side).png` |
| `POST /v0/assets/watch` | control | `{path, send, camera...}` (the path below the same root as a path import): a saved file is converted again and, with the viewer linked, sent and viewed with the last camera |

**Asset record:** `id` (first 12 hex of the DQA1's SHA-256), `name`, `kind` (`texture` / `model`), `state` (the Assets
editor's: `checked`, `refused`, `live`; `sent` while the console loads), `tris`, `strips`, `stripVerts`, `texW`, `texH`,
`note` (the editor's row fields), `bytes`, `sha256` (the source), `dqa_sha256`, `ram`, `vram`, `fits`, `reason`,
`lost` (plain sentences: colour depth, size, alpha, codewords, normalisation), `psnr` (level 0 against the input),
`textures[]` {`name`, `w`, `h`, `format`, `mip`, `bytes`, `psnr`, `src`}, `console` {`state`, `send_s`, `why`, `tier`},
`cost` (from the last view), `build` (the bridge's build record id once it keeps one), `tier` `pc`.

**View:** `id`, `asset`, `camera`, `light`, `bg`, `filter`, `view`, `cull`, `tier`, `times` {`send_s`, `set_s`,
`cost_s`, `grab_s`, `render_s`, `diff_s`, `round_trip_s` (send + set + grab), `total_s`}, `console` {`still`, `url`},
`pc` {`url`}, `diff` {`pixels`, `differ`, `over` (more than 8 levels), `max`, `mean_abs`, `psnr`, `url`}, `side`,
`cost` {`pictures`, `window`, `verts`, `strips`, `texture_bytes`, `vertex_buffer_bytes` (bytes sent to the chip a
picture), `graphics_free`, `submit_ms`, `ta_ms`, `render_ms`, `frame_ms` (each mean and worst), `tier`}, `build`,
`run`. Every view is also a line in `<data>/assets/roundtrips.jsonl` and an `asset_view` record in the session.

## The viewer's options (live-data `option` commands, at most 64 bytes)
`load=<seq>:<file>` (a file the bridge serves this run), `cam=<yaw>,<pitch>,<dist>,<fov>`,
`light=<x>,<y>,<z>,<ambient>,<diffuse>`, `bg=<rrggbb>`, `filter=0|1`, `cull=0|1|2`, `view=model|flat|mips`,
`dither=0|1`, `seq=<n>` (echoed in the `seq` counter once applied: the bridge then knows the next picture is the new
view). Counters: `seq`, `asset` (the loaded load's seq), `state` (0 none, 1 loading, 2 ready, 3 refused, 4 error),
`verts`, `strips`, `txbytes`, `vtxbuf` (bytes sent to the chip a picture), `vfree`. Phases: submit (CPU transform and
submit), ta and render (the chip's registration and render time, from KallistiOS's `pvr_get_stats`), wait, frame.
A refusal or failure is a warning or error log line naming the load's seq and the reason.
