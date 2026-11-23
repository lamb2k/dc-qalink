# The bridge's API, contract 1 (dc-qalink 0.2)

The **bridge** (`pc/qalink_bridge.py`, package `pc/bridge/`) is the PC program that owns the link to one Dreamcast or
one Flycast and serves the same data to QA Link Cloud, to scripts and to agents (the local MCP, `docs/MCP.md`). This
page is the contract; QA Link Cloud's `src/data/bridge-api.ts` matches it. Contract 0 (lane ql-core) was the first cut;
contract 1 takes the integrator's rulings on ql-app's 15-row table (2026-11-22).

Python 3.10 or newer, standard library only (`http.server`'s threading server; no web framework). The MCP part needs
one package, the Python MCP SDK (`pip install mcp`, 2.x).

```
python pc/qalink_bridge.py [--port 52080] [--data DIR] [--udp-port 52060] [--console auto|IP] [--device flycast|console]
                           [--files ROOT] [--asset-root DIR] [--selftest testprog.elf] [--loader-disc DIR]
                           [--level read|control|run] [--udp-host ADDR] [--allow-lan] [--mcp-http PORT]
python pc/qalink_bridge.py --mcp stdio [--bridge http://127.0.0.1:52080] [--mcp-level read|control|run]
```

## Shape
- HTTP/1.1 with JSON bodies on **127.0.0.1:52080** only (the bridge refuses any other address). The hosted relay
  (QA Link Cloud, L3b) carries the same paths with a token.
- A request body's `Content-Length` must be digits only and at most 16 MiB; anything else is answered **413** before
  the body is read.
- Paths under **`/v0/`**. Every JSON answer carries **`contract: 1`**. A client sends `X-Contract: 1` (or
  `?contract=1`); another major is answered **409** with `theirs` and `ours`.
- The bridge speaks the **loader's words** (link `searching` / `up` / `lost` / `refused`; run states below); the app maps
  them to its plain words. Ids are opaque strings (runs: 8 hex digits, the console's session; stills `<run>-<n>`).
- **Tier on every number:** `console`, `flycast` or `mock` (the simulated console of the host tests). It is on the
  status, every run, every summary, every phase and guard, every headline number and every still.
- Times are seconds (float); pictures are `n`, counted from 1; wall-clock fields are ISO; sizes are bytes; hashes are
  lowercase hex SHA-256.
- **Levels:** `read` (state, sessions, stills, summary, trace, log), `control` (commands, reset, grab, link), `run`
  (push and run code). A bridge started at a level answers 403 above it. Every control and run call is written to
  `calls.log` in the data folder with who made it (`X-By`, else the client address) and its level.
- The UDP port of the loader (52060) stays bound while the bridge runs. **It is bound on 127.0.0.1 by default**
  (ql-fix-pc, after the 2026-11-23 audit); on the address `--console` names when that is this PC's own (Flycast); on
  every address (0.0.0.0) only with `--allow-lan`; a typed `--udp-host` is used as typed. Whatever is bound, without
  `--allow-lan` only datagrams from this PC's own addresses are read. `status.udp_bind` says what is bound
  (`address:port`). The console is learnt only from a loader's HELLO; a PC-to-console datagram (our own broadcast
  looped back) is dropped.
- **Only local by default.** The bridge talks only to a loader at this PC's own addresses (Flycast, the simulated
  console): a console on the network is not listed in discovery, never linked or sent anything, `POST /v0/link` to it
  is 403, and `--console <LAN address>` refuses to start. `--allow-lan` lifts it; QA Link Cloud passes it when a person
  turns on "Dreamcast on the network". Tests, scripts and agents leave it off (ruling 2026-11-22: no lane run sends
  anything to a real console).
- **Autoconnect** (on unless `--no-autoconnect` or `POST /v0/settings {"autoconnect": false}`): when a loader beacons,
  the bridge links to it without a call if it is a console linked before, or, when none is remembered, the only loader
  heard; two unknown loaders wait for a click. When the linked console falls silent for 4 s (a run that reboots, a
  power cycle) and comes back, on a new address too, it is relinked. Consoles are remembered in `consoles.json` by
  identity, not address: the adapter's MAC from HELLO (offset 128, loaders from 456ac57 on; `mac:<mac>`), so one
  console stays one across address changes and loader updates; a loader without it (older, or the modem) by its build
  and the first bytes of its SHA-256 (`loader:<sha8>:<build>`; two consoles on the same loader then look alike: a click
  picks). A console remembered by its loader is found that way once and then kept by its MAC.
- **What HELLO says beyond the address** (additive, protocol 2): `mac`, `gdemu_slot` (offset 134), and from 148 bytes
  the stub's record of the last way back (`stub` {`step`, `step_name`, `err`, `gdemu`, `entries`}, null when the loader
  read none this boot; offsets 136 to 143) and `gdemu_version` (offset 144, null when GDEMU did not answer). The device
  and every discovery row carry them, with `stub_text`, one line naming where the last way back stopped.

## Resources
| Method and path | Level | What |
|---|---|---|
| `GET /v0/status` | read | `bridge`, `link`, `device` (below), `session` (being recorded, or null), `rate_bps`, `mode` (`full` adapter / `summary` modem), `paused`, `n` (newest picture), `level`, `tier`, `queue`, `active_run`, `autoconnect`, `remembered`, `only_local`, `remote_refused`, `udp_bind` |
| `POST /v0/settings` | control | `{autoconnect: true|false}`; answers the status |
| `GET /v0/device` | read | `id`, `kind` (`flycast`/`console`), `tier`, `name`, `transport` (`adapter`/`modem`), `address`, `console_ip`, `link`, `state` (the loader's: `idle`/`receiving`/`verified`/`failed`/`exec`, or the run's), `loader` {`build`, `sha8`, `protocol`, `stub_bytes`, `boot_count`, `pushed`}, `ident`, `mac`, `gdemu_slot`, `gdemu_version`, `stub`, `stub_text`, `program` {`run`, `linked`}, `last_run` |
| `GET /v0/devices` | read | `{devices: [device]}` (one device per bridge) |
| `GET /v0/discovery` | read | `listening`, `port`, `seen[]`: `id`, `console_ip`, `address`, `port`, `kind` (flycast when the beacon comes from this PC), `transport`, `loader`, `protocol`, `beacons`, `last_s_ago`, `chosen`, `ident`, `remembered`, `at`, `mac`, `gdemu_slot`, `gdemu_version`, `stub`, `stub_text` |
| `POST /v0/link` | control | `{console_ip}` (or `device` id): talk to that loader; answers the status. 404 if none was heard there |
| `POST /v0/runs` | run | Push and run. **multipart/form-data**: `program` (file: ELF or raw image), `sha256` (required; a mismatch is 400 and nothing is queued), `name`, `args`, `deadline_s`, `files` (a folder name below the bridge's `--files`, never a path), `reset_after_s`. Scripts may send `application/octet-stream` with `X-Build-Sha256`, `X-Build-Name`, `X-Args`. Answers **202** with the run |
| `GET /v0/runs` | read | `{runs: [run]}`, newest first |
| `GET /v0/runs/{id}` | read | The run with `report` (text) |
| `GET /v0/runs/{id}/report` | read | The report, `text/plain`; `X-Report-CRC-OK: 1` |
| `GET /v0/runs/{id}/serial` | read | The serial lines in order, a gap marked `[gap: n lines lost]` |
| `POST /v0/selftest` | run | Push and run the bridge's test program (`--selftest`, dc-qalink's `testprog.elf`) with `report screen:1`; 202 with the run; 501 if none is set up |
| `POST /v0/stills` | control | **Grab now**: `{timeout_s?}`. The linked program sends its displayed picture (a program with live data is paused for the read and resumed). Answers the still's JSON; 409 no program linked, 504 no answer (a program without the live additions), 410 the program left, 502 CRC mismatch |
| `GET /v0/stills` | read | `{stills: [still]}` |
| `GET /v0/stills/{id}` | read | The PNG. `GET /v0/stills/{id}.json`: `id`, `run`, `index`, `picture`, `width`, `height`, `format` (`rgb565`), `crc_ok`, `bytes`, `seconds`, `request`, `tier`, `at` |
| `POST /v0/command` | control | One endpoint: `{cmd: pause}`, `{cmd: resume}`, `{cmd: step, count}`, `{cmd: warp, preset}`, `{cmd: option, name, value}`, `{cmd: pad, state | steps}`, `{cmd: reset, level: return|watchdog|power}`. Answers `{ok, id, n, error}` (`n` the picture it took effect at); reset answers the rung. **409** no program linked; **501** what the loader or the program cannot do yet (a program without the live additions; `memory`) |
| `POST /v0/reset`, `POST /v0/power` | control | Aliases: the ladder (`rung`: `stub` / `watchdog` / `none`, `seconds`); the last rung (`done`, `say`) |
| `GET /v0/summary?slowest=k` | read | The current session: `session`, `label`, `device`, `tier`, `pictures`, `measured`, `missing` (pictures never received), `window`, `phases[]` {`key`, `label`, `budget`, `mean`, `p95`, `p99`, `worst`, `worstAt`, `over`, `tier`}, `guards[]` {`key`, `name`, `value`, `tier`}, `slowest[]` {`n`, `total` (busy: the phases that are not the total), `phases`}, `trace_lost` |
| `GET /v0/trace?from&to&lane&limit` | read | `from`, `to`, `events[]` {`lane`, `n`, `d`, `label`}, `truncated`, `lost` |
| `GET /v0/log?from&to&level&q&limit` | read | `from`, `to`, `lines[]` {`n`, `lv`, `msg`}, `truncated` (the newest when over the limit; `warn` includes `err`) |
| `GET /v0/presets` | read | What the running program declared: `program`, `presets[]` {`id`, `group`, `label`}, `options[]` {`name`, `type` bool/int/enum, `min`, `max`, `values`, `value`, `label`}, `scripts` (none yet), `linked` |
| `GET /v0/sessions` | read | `{sessions: [{id, label, device, started, recorded, ended, pictures, tier, bytes}]}` |
| `GET /v0/sessions/{id}` | read | The session file, `application/x-ndjson` (below) |
| `GET /v0/loader` | read | The loader disc folder (`--loader-disc`) as one zip (`Content-Disposition` names it); 501 if none |
| `POST /v0/memory/read`, `POST /v0/assets` | - | **501** in this loader (memory reads: plan L5; live assets: plan L5b) |

A **run**: `id`, `program` {`name`, `sha256`, `bytes`}, `args`, `state` (`queued` / `sending` / `running` / `done` /
`crashed` / `timeout` / `reset` / `lost` / `refused`), `times` {`push_s`, `run_s`, `report_s`, `total_s`}, `way_back`
(`stub` / `watchdog` / `power`), `by`, `queued_at`, `ended_at`, `device`, `tier`, `deadline_s`, `files` (the file
service's totals), `stills`, `serial_lines`, `error`, `transfer` (the push's numbers), `crash` {`event`, `pc`, `pr`, `sp`},
**`ending`** when the result came by END before the console's restart (ql-fix-pc: `bytes`, `tail`, `complete`,
`whole`, `crc_ok`; the run is kept even when the loader's saved report is empty afterwards) and the parsed
**`summary`**: `exit`, `state`, `seconds`, `pictures`, `numbers[]` {`label`, `value`, `unit`, `tier`}
(report lines of the form `@ label = value unit`, and each phase's mean and worst when the program sent records),
`notes`, `crash`, `tier`. Crash symbols and source lines (from an ELF with symbols) are for later.

## Builds (the workspace's anchor; v0.2-netdisc, additive to contract 1)
A **build record** is made at every push (`POST /v0/runs`, `POST /v0/selftest`): `id` (`b` + 12 hex: a hash of the
program's SHA-256, the disc manifest's SHA-256, the loader's SHA prefix, the arguments and the tier, so the same push
again is the same build and its runs gather under it), `program` {`name`, `sha256`, `bytes`, `commit`}, `disc`
{`manifest`, `mode` overlay/whole, `files`, `served`, `base`, `overlay`} (null without a network disc), `loader`
{`build`, `sha8`, `protocol`}, `args`, `tier`, `created`, `last`, `by`, `runs` (run ids). Kept in `<data>/builds.json`.
A push may name its git commit: multipart field `commit`, or `X-Build-Commit` (7 to 40 hex digits, else 400).

The bridge stamps the build's id as **`build`** on every record it writes: each session record (rows, counters, logs,
trace, grabs, disc reads, run events), every run (`build`), every still's JSON, every line of `calls.log` (control and
run calls, MCP included); `status.build` names the current build. A program started on the console (not pushed) has
no build.

| Method and path | Level | What |
|---|---|---|
| `GET /v0/builds` | read | `{builds: [build], current}`, newest first |
| `GET /v0/builds/{id}` | read | One build |
| `GET /v0/disc` | read | The network disc: `mode` (`overlay` / `whole`, or null with a `note` when none is served), `files[]` {`path`, `size`, `sha256`, `side` (`pc` served / `disc` read from GDEMU), `source` (`base` / `overlay`), `changed`, `new`, `base_sha256`}, `sha256` (the manifest's), `count`, `served`, `base`, `overlay`, `stats` (requests, resent requests, pieces, bytes, the console's round trip `request_ms` {`n`, `mean`, `p50`, `p99`, `worst`}, `program_reads` per side {`records`, `bytes`, `ms`, `MBps`}) |
| `GET /v0/disc/reads?limit` | read | `{reads: [disc read record], total}` |
| `GET /v0/disc/file?path=` | read | A disc file's bytes as the console sees it with the network disc on (overlay first), for the Assets workspace |
| `GET /v0/plugins`, `GET /v0/plugins/{name}.js` | read | Asset decoder plugins (ES modules) from `--asset-plugins DIR`, a folder outside any repository: a game's own formats |

Bridge options: `--disc <folder | .gdi | .iso>` the base, `--overlay <folder>` the changed files, `--whole` serve the
whole disc, `--asset-plugins <folder>`. `pc/dcql_loop.py run|serve` take `--disc`, `--overlay`, `--whole` too and write
`disc-manifest.json` and `disc-reads.jsonl` in `--out`. docs/NETDISC.md has the console side and the wire.

## The live stream: `GET /v0/live` (Server-Sent Events)
SSE, not WebSocket (one of them, as ruled; SSE needs no library on either side and resumes by itself). Each event's
`data:` is one JSON object with **`t`** (its type) and **`s`** (seconds on the session clock).
1. `hello`: `contract`, `bridge`, `s`, `session` (null before any program linked), `device`, `meta` (the session's meta
   record, or null), `resume_from`, `link`.
2. The current session's records after picture `from` (`?from=<n>&session=<id>`, or the `Last-Event-ID` header): no
   repeat on a reconnect, and a hole stays a gap.
3. Then everything as it arrives, and `status` about once a second (`link`, `rate_bps`, `paused`, `n`, `mode`,
   `session`, `active_run`), and `: keep-alive` comments.

A `row` event's SSE id is its picture `n`. The records are the session format's (QA Link Cloud `docs/SESSION_FORMAT.md`,
version 1): `meta` (again whenever the program declares its phases; a new session's meta when another program links),
`row` {`n`, `ph` {key: ms}, `x` {`room`, `pos`}}, `ctr` {`n`, `v`} (only when a value changes), `log` {`n`, `lv`, `ph`,
`msg`}, `trace` {`lane`, `n`, `d`, `label`}, `grab` {`n`, `w`, `h`, `fmt: png`, `url` (`/v0/stills/<id>`), `label`,
`kind`}, `end` {`n`, `reason`}. Run events are further record types a format-1 reader may ignore (so the format stays
version 1): `link` {`state`, `role` loader/program, `console_ip`, `transport`, `beacons`}, `hello` (the device),
`push` {`run`, `chunks_have`, `chunks`, `bytes_per_s`, `resent`}, `verdict` {`run`, `ok`, `why`}, `exec`, `line`
{`run`, `seq`, `text`}, `file` {`run`, `path`, `op`}, `still` {`run`, `id`, `picture`, `crc_ok`}, `back` {`run`,
`state`, `way_back`, `seconds`, `error`}, `reset` {`run`, `rung`, `seconds`}.

**Session format version 2** (v0.2-netdisc): `meta.build` is the build record and every record carries `build` (its id)
when the session has one; `disc` records are the network disc's: `mount` {`mode`, `served`, `files`, `manifest`},
`open` {`path`, `size`, `side`}, `read` {`path`, `off`, `bytes`, `ms` (the console's time inside its read calls),
`calls` (sequential reads merged), `side`}. A version-1 file opens as before (no build); a reader of version 1 refuses
version 2 by its version check, so QA Link Cloud reads both from its `netdisc` branch on.

## Sessions
Every program that links (pushed by the bridge or started on the console) is one session, written as it arrives to
`<data>/sessions/<yyyymmdd-hhmmss>-<run>.jsonl` in the session format: the stream written down. Stills go to
`<data>/stills`, reports to `<data>/reports`. Game pictures and run data are private: keep them out of repositories.

## The wire underneath
`include/dcql_loop.h` (protocol 2) and, for live data, `pc/bridge/wire.py` / `lib/dcql_live.h`: additive message types
(REC 0x0d, LIVEDEF 0x0e, TRACE 0x0f, CMDACK 0x20 from the console; SUB 0x1c, GRAB 0x1d, CMD 0x1e from the PC; LINE with
`b` = 1..4 for game log lines). A loader or client that does not know them ignores them; 0.1.0's modem API is unchanged.
The network disc adds DOPEN 0x27, DREAD 0x28, DCLOSE 0x29, DLOG 0x2c (console to PC) and DINFO 0x2a, DDATA 0x2b (PC to
console); END 0x21 / ENDACK 0x1f are the run's ending (ql-core4).

## Not yet
Memory reads, live assets, pad scripts the program registers, crash symbols and source lines, the modem's summary mode
(the in-game link is adapter only), tokens (local mode has none; the hosted relay brings them, L3b).
