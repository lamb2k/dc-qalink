# Third-party software

dc-qalink ships one piece of third-party data, the in-game menu's font (below). At build time it uses:

| Component | Where | Licence |
|---|---|---|
| KallistiOS (kernel, modem driver, network stack, sockets) | your `$KOS_BASE`, linked as `libkallisti` | KOS License (BSD 3-clause style), `$KOS_BASE/doc/license/LICENSE.KOS` |
| libppp (Lawrence Sebald) | `$KOS_BASE/addons/libppp`, compiled by `lib/dcql.mk` with four function names redirected | KOS License, as above |
| newlib / libgcc | your KOS toolchain | their own licences (see the toolchain) |

0.2: the broadband adapter path uses KOS's own adapter drivers (`bba_init`, `la_init`) and its network stack
(`net_init`, DHCP, sockets) unchanged. `loader/stub.c` calls the Dreamcast BIOS's disc functions by their documented
syscall vector, as KOS's `cdrom.c` does; it contains no BIOS or KOS code. Its SHA-256 and the loader's inflate are
dc-qalink's own (`loader/cl_util.c`).

`lib/dcql.c` mirrors the layout of KOS's modem `CHAIN_BUFFER` struct and calls three KOS-internal functions
(`_ppp_enter_phase`, `modemIntShutdown`, `net_udp_init`); no KOS code is copied.

A program built with the link carries KOS and libppp under the KOS License: keep its copyright notice in your
distribution's documentation, as KOS itself requires.

The in-game menu (`lib/dcql_menu.c`) draws with **Spleen 8x16** 2.3.0 by Frederic Cambus
(https://github.com/fcambus/spleen), BSD 2-Clause licence, full text in `lib/font/LICENSE-spleen.txt`. Only the printable
ASCII glyphs (32..126) are kept, converted to one bit a pixel by `lib/font/mkfont.py` into `lib/font/spleen8x16.h`
(receipt: `lib/font/RECEIPT.txt`). A program built with the menu carries these glyphs in binary form: keep Spleen's
copyright notice and licence in your distribution's documentation, as its licence requires. The menu's QR encoder is
dc-qalink's own.

The Python tools use the Python standard library (PSF licence) and the system `ssh` / `scp` / `ssh-keygen`, except
these, which need packages you install yourself:
- **numpy** (BSD 3-Clause): the asset converters in `pc/assets/` (`gen.py`, `image.py`, `model.py`, `pvrtex.py`,
  `render.py`, `dqa.py`), so the bridge's asset routes (`pc/bridge/assets.py`, loaded when an asset route is first
  used) and their test `tests/test_assets.py`.
- **Pillow** (HPND): `pc/assets/image.py` for pictures other than PNG (PNG is read without it), and the menu
  example's picture tool `example/menu/shots.py` (not needed to build).
- **the MCP Python SDK** (`mcp`, MIT): the MCP server `pc/bridge/mcp_server.py` (`pc/qalink_bridge.py` loads it only
  when MCP is asked for) and its test `tests/test_mcp.py`, which the gate's Flycast suite runs (so the gate's Windows
  Python needs it).
- **zxing-cpp** (Apache 2.0): the QR checks of `example/menu/shots.py`, as an independent decoder.

The loop tool (`pc/dcql_loop.py`), the bridge without MCP or asset routes, `tools/first_console.py` and the other
tests need none of them.
