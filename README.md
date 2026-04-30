# holo

Browser + screen automation primitive for AI agents.

`holo` is a small daemon that lets a CLI agent (Claude Code, Codex, etc.) drive
the browser you're already signed into — no extension to install, no headless
browser to log in fresh, no Chrome-only constraints. The agent gets a thin
MCP-shaped tool surface (`browser.open`, `browser.click`, `browser.read`,
`browser.eval`, `browser.os.*`); under the hood, an OS-layer driver and a
same-origin bookmarklet do the work and talk to each other through a small
local channel.

**Status:** early — Phase 0, walking-skeleton stage. Not yet usable as a
general tool.

## What's different about this approach

- **Auth piggyback.** The bookmarklet runs in your real browser session, so
  every site you're already logged into is reachable. No credential storage,
  no MFA dance, no fresh-login-per-test.
- **Cross-origin from day one.** Bookmarklet runs on whatever origin the
  active tab is — `tai.sh`, `github.com`, AWS Console, anywhere. Not pinned to
  a single host.
- **OS layer is first-class.** Native dialogs, terminal apps, canvas/WebGL
  content (xterm.js, video, anything pixel-rendered) — all reachable, because
  the same daemon that drives the in-page bookmarklet also drives the screen
  via OpenCV-backed image matching.
- **Cross-host optional.** Local mode (CLI and browser on the same machine)
  needs no infrastructure. Cross-host mode (CLI on a remote dev box, browser
  on your laptop) adds a registry + bridge service when you need it.

## Status

- [x] Phase 0 — primitive layer (channel, framing protocol, bookmarklet payload)
- [x] Phase 1 — agent surface (MCP server, with WebSocket transport + stealth-QR fallback)
- [ ] Phase 2 — per-origin assertion plugins
- [ ] Phase 3 — cross-host registry + bridge
- [ ] Phase 4 — opt-in CDP adapter

## Build a single-file binary

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/pyinstaller --clean holo.spec
./dist/holo --version
```

Produces a self-contained `dist/holo` (`dist/holo.exe` on Windows) that
bundles the Python interpreter + dependencies + the Jython bridge script
+ static assets. End users only need OpenJDK 11+ installed; the SikuliX
jar is fetched on first run from a pinned GitHub Release (or pre-warmed
with `holo install-bridge`).

## Use as an MCP server

`holo mcp` runs a stdio MCP server that exposes the channel as a small
set of tools. Wire it into Claude Code, Codex, or another MCP client by
pointing at the entrypoint:

```jsonc
{
  "mcpServers": {
    "holo": { "command": "holo", "args": ["mcp"] }
  }
}
```

The agent calibrates a tab once (the user clicks the holo bookmarklet
on whichever page should be driven), then issues commands by sid. Tools:

| Tool            | What it does                                             |
| ---             | ---                                                      |
| `calibrate`     | Block until the bookmarklet beacon arrives, return its sid |
| `list_channels` | Snapshot of currently calibrated tabs                    |
| `drop_channel`  | Forget a channel (does not close the browser popup)      |
| `ping`          | Round-trip ping over the channel                         |
| `read_global`   | Read a dotted path off the page's global object          |
| `send_command`  | Escape hatch — send any bookmarklet op (see `bookmarklet/dispatch.js`) |

## License

Apache-2.0. See [LICENSE](LICENSE).
