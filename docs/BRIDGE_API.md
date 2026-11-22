# The bridge's API, first cut (dc-qalink 0.2, contract version 0)

The **bridge** is the small PC program (Python, standard library, part of dc-qalink) that owns the link to one
Dreamcast or one Flycast and serves the same data to Studio (the web app), to scripts and to agents. This page is what
it will serve. It is a first cut for the app's side to match: nothing here is built yet (the server is milestone L2),
and the version stays 0 until both sides agree. The wire to the console underneath is `include/dcql_loop.h`
(protocol 2); the bridge never changes it.

## Shape

- HTTP/1.1 with JSON bodies on `127.0.0.1:52080` (local mode, no sign-in), and the same paths through the hosted relay
  (QA Link Cloud) with a bearer token per person, agent or bridge.
- One live stream per client: `GET /v0/live` as Server-Sent Events (`text/event-stream`); each event is one JSON
  object with `type`, `t` (seconds since the session started, a float) and its fields. A WebSocket on the same path is
  allowed later; the event objects stay the same.
- Every response carries `contract: 0`. A client of another contract major version is refused with 409 and both
  versions in the body (as the loader refuses a PC tool of another protocol version).
- Times are seconds (float) on the session clock; sizes are bytes; addresses are dotted strings; hashes are lowercase
  hex SHA-256. Every number says where it came from: `"tier": "console" | "flycast"`.
- Permission levels: `read` (state, sessions, stills, logs), `control` (reset, pause/step later, warp later),
  `run` (push and run code). A token carries one level; a call above it is 403.

## Resources

| Method and path | Level | What |
|---|---|---|
| `GET /v0/device` | read | The device this bridge owns: `kind` (`console` / `flycast`), `name`, `transport` (`adapter` / `modem`), `console_ip`, `link` (`searching` / `up` / `lost`), `loader` {`build`, `sha8`, `protocol`, `stub_bytes`, `boot_count`}, `state` (`idle` / `receiving` / `verified` / `running` / `returning`), `last_run` {`id`, `state`, `report_bytes`} |
| `GET /v0/runs` | read | Runs of this session, newest first: `id`, `program` {`name`, `sha256`, `bytes`}, `args`, `state` (`queued` / `sending` / `running` / `done` / `crashed` / `timeout` / `reset` / `lost` / `refused`), `times` {`push_s`, `verify_s`, `run_s`, `back_s`}, `way_back` (`stub` / `preloaded` / `watchdog` / `power`) |
| `POST /v0/runs` | run | Push and run: multipart `program` (ELF or bin), fields `args`, `deadline_s`, `files` (a served folder name the bridge knows, never a path from the client), `reset_after_s`. Answers 202 with the run's `id`; progress on the live stream |
| `GET /v0/runs/{id}` | read | One run: the fields above plus `report` (text), `crash` {`event`, `pc`, `pr`, `sp`} when crashed, `files` (the file service's totals), `stills` (their ids), `serial_lines` (count) |
| `GET /v0/runs/{id}/report` | read | The report as `text/plain` (CRC checked; `X-Report-CRC-OK: 1`) |
| `GET /v0/runs/{id}/serial` | read | The program's serial lines as `text/plain`, in order, gaps marked |
| `GET /v0/stills/{id}` | read | One still as PNG; `GET /v0/stills/{id}.json`: `run`, `picture`, `width`, `height`, `format` (`rgb565`), `crc_ok`, `t` |
| `POST /v0/reset` | control | RESET the running program; the restart ladder follows. Answers with the rung that worked: `stub` / `watchdog` / `power` / `none` and seconds |
| `POST /v0/power` | control | The last rung (the plug adapter, or `{"done": false, "say": "power cycle needed"}`) |
| `GET /v0/sessions` | read | Recorded sessions: `id`, `device`, `started`, `ended`, `runs`, `bytes` |
| `GET /v0/sessions/{id}` | read | One session file (the recording the live stream was made of), `application/x-ndjson` |

## Live events (`GET /v0/live`)

| `type` | Fields |
|---|---|
| `link` | `state` (`searching` / `up` / `lost`), `console_ip`, `transport`, `beacons`, `heard` |
| `hello` | the loader's beacon as `GET /v0/device` has it |
| `push` | `run`, `chunks_have`, `chunks`, `bytes_per_s`, `resent` |
| `verdict` | `run`, `ok`, `why` (`stream crc` / `inflate` / `raw length` / `sha256` / `size` / ...) |
| `exec` | `run` |
| `line` | `run`, `seq`, `text` (one serial line; a jump in `seq` is a gap, never a wrong line) |
| `file` | `run`, `path`, `op` (`open` / `read` / `refused`), `bytes` |
| `still` | `run`, `id`, `picture` |
| `back` | `run`, `state`, `way_back`, `seconds` |
| `reset` | `run`, `rung`, `seconds` |

Per-picture records, the trace ring and memory reads (milestone L2) add event types; none of the above change.

## Differences from the app's side (`studio/src/data/bridge-api.ts`)

That file did not exist in `E:\RE4DC-Lanes\ql-ui\studio\src\data\` when this page was written (2026-11-22, lane
ql-core), so there is nothing to compare yet. The integrator's list to settle once it exists:
1. path and version prefix (`/v0/...`) and the `contract` field;
2. the run and link state names above (the loader's own words, lower case);
3. SSE now, WebSocket later, same event objects;
4. ids as 8 hex digits (the run id the console uses) and still ids as `<run>-<n>`;
5. `tier` on every number;
6. permission levels and where the app shows them.
