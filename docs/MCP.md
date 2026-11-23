# The local MCP (dc-qalink 0.2)

The bridge serves the console as MCP tools, so Claude Code, Codex or any MCP client on this PC can link, push, run,
grab the screen and read live data. The tool list is QA Link Cloud's (`src/data/mcp-tools.ts`), kept here as its JSON
export `pc/bridge/mcp-tools.json` (`node tools/gen-mcp-doc.mjs --json`); the bridge lists exactly those tools, with
their schemas, and checks arguments the same way. Each tool is one or two calls of contract 1 (`docs/BRIDGE_API.md`).

Needs the Python MCP SDK (`pip install mcp`, 2.x) beside a running bridge.

- **stdio** (the client starts it): `python pc/qalink_bridge.py --mcp stdio --bridge http://127.0.0.1:52080 --mcp-level control`
  - Claude Code: `claude mcp add qalink -- python <dc-qalink>/pc/qalink_bridge.py --mcp stdio --mcp-level control`
  - Codex (`~/.codex/config.toml`): `[mcp_servers.qalink]` with `command = "python"` and the same `args`.
- **HTTP** (started with the bridge): `python pc/qalink_bridge.py --mcp-http 52081` serves streamable HTTP on
  `http://127.0.0.1:52081/mcp`.

Levels: `read` (11 tools), `control` (20), `run` (23: push_build, run_and_wait, self_test). A server lists and accepts
only the tools at or below its level. Every call is logged with who made it (the client's name and version) and its
level in `mcp-calls.log` (`~/.dc-qalink/bridge`), and reaches the bridge's `calls.log` as `X-By: mcp <client>`.
`grab_screen` answers with a PNG image block and the picture number; every number carries its tier.

Files: `push_build` and `run_and_wait` read a `path` only below `QALINK_MCP_ROOT` (default: the MCP server's working
folder); any other path is refused (403) before it is read. A pushed program is at most 16 MiB (the bridge's body
limit), checked before it is read or decoded. `upload_asset`'s `base64` is refused when it is over 16 MiB less 64 KiB
(16,711,680 characters), checked before the request's JSON is built (ql-fix-pc round 2).

Test: `tests/test_mcp.py` (a real MCP client: list, link_status, bad arguments refused, push_build, grab_screen,
live_summary, list_presets, level read refuses push_build), against the simulated console or a running bridge.
