# holo

Browser + screen automation primitive for AI agents.

`holo` is a small daemon that lets a CLI agent (Claude Code, Codex, etc.) drive
the browser you're already signed into — no extension to install, no headless
browser to log in fresh, no Chrome-only constraints. The agent gets a thin
MCP-shaped tool surface (`browser.open`, `browser.click`, `browser.read`,
`browser.eval`, `browser.os.*`); under the hood, an OS-layer driver and a
same-origin bookmarklet do the work and talk to each other through a small
local channel.

**Status:** alpha — Phases 0–2 shipped (primitive layer, MCP agent
surface, cross-host bridge). Pre-release; expect rough edges and
no API stability.

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
- [x] Phase 2 — cross-host bridge (`holo mcp-remote` stdio proxy; persistent-daemon TCP transport via `holo mcp --listen` + `holo connect` — see [`docs/cross-host.md`](docs/cross-host.md))
- [ ] Phase 3 — opt-in CDP adapter

## Install

Pre-built binaries are attached to each [GitHub Release](https://github.com/nospaceleftondevice/holo/releases).
Three binary targets: `holo-macos-universal2` (arm64 + x86_64 fat binary),
`holo-linux-x86_64`, `holo-windows-x86_64.exe`. Each release also ships
`holo-bookmarklet.html` — a self-contained page for installing the
bookmarklet in your browser.

macOS / Linux:

```bash
# Replace TAG with the latest release tag, e.g. v0.1.0a8
TAG=v0.1.0a8
ASSET=holo-macos-universal2   # or holo-linux-x86_64

curl -L -o /usr/local/bin/holo \
  "https://github.com/nospaceleftondevice/holo/releases/download/${TAG}/${ASSET}"
chmod +x /usr/local/bin/holo
holo --version
```

Windows: download `holo-windows-x86_64.exe` from the release page and
put it on `PATH`.

Install the bookmarklet (any platform — opens in the browser you want
holo to drive):

```bash
holo install-bookmarklet
```

That downloads `holo-bookmarklet.html` from the matching release and
opens it in your default browser. Then drag the 🔧 holo button to your
bookmarks bar.

End users also need OpenJDK 11+ installed (for the SikuliX bridge that
drives screen primitives). The 128 MB SikuliX jar itself isn't bundled
in the binary — it's fetched on first `--bridge` use, or pre-warmed
with `holo install-bridge`.

For cross-host setups (agent on one machine, browser on another), see
[`docs/cross-host.md`](docs/cross-host.md).

## Build from source

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/pyinstaller --clean holo.spec
./dist/holo --version
```

Produces a self-contained `dist/holo` (`dist/holo.exe` on Windows) that
bundles the Python interpreter + dependencies + the Jython bridge script
+ static assets.

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
| `calibrate`     | Return the most recent channel if any exist; otherwise block for a fresh bookmarklet beacon |
| `list_channels` | Snapshot of currently calibrated tabs                    |
| `drop_channel`  | Forget a channel (does not close the browser popup)      |
| `ping`          | Round-trip ping over the channel                         |
| `read_global`   | Read a dotted path off the page's global object          |
| `send_command`  | Escape hatch — send any bookmarklet op (see `bookmarklet/dispatch.js`) |
| `browser_navigate` / `browser_new_tab` / `browser_list_tabs` / `browser_activate_tab` / `browser_close_active_tab` / `browser_read_active_url` / `browser_read_active_title` / `browser_reload` / `browser_back` / `browser_forward` | AppleScript-driven Chrome ops (macOS). Reliable navigation without keystroke simulation; bypasses `app_activate` + `screen_key` entirely. |
| `browser_execute_js` | Run an arbitrary JS expression in the active tab via Chrome's AppleScript dictionary. Requires Chrome → View → Developer → "Allow JavaScript from Apple Events". Raises a clear error pointing at `bookmarklet_query` if that toggle is off. |
| `bookmarklet_query` | CSP-safe DOM query routed through the bookmarklet — `document.querySelector(selector)` reading either a property (default `innerText`) or an attribute. Works on strict-CSP origins where `browser_execute_js` is unavailable. Pass `all=true` for `querySelectorAll`. |
| `ui_template_capture` / `ui_template_list` / `ui_template_find` / `ui_template_click` / `ui_template_delete` | Persistent on-disk cache that maps natural-language `(app, label)` keys to PNG variants and matches them with SikuliX. Capture once (drag-rectangle prompt or programmatic region), then click by name in future sessions. Avoids re-discovering the same desktop UI elements via vision. Default cache: `~/Library/Caches/holo/templates/` (override with `HOLO_TEMPLATE_DIR`). |

## License

Apache-2.0. See [LICENSE](LICENSE).
