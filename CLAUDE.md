# CLAUDE.md — holo project guide

`holo` is a browser + screen automation primitive for AI agents. A CLI agent (Claude Code, Codex, etc.) drives the user's already-signed-in browser via a same-origin **bookmarklet** that talks to a local **daemon**. No extension, no headless browser, no credential storage.

Repo layout:
- `src/holo/` — Python daemon (macOS-first; Windows shim exists but is partial)
- `bookmarklet/` — JS payload built with esbuild, run as `javascript:` URL on any tab
- `tests/` — pytest for daemon, `bookmarklet/test/` for JS

## Architecture: two transports, one channel

The daemon ↔ bookmarklet channel has **two transports**, chosen at runtime:

1. **WebSocket (preferred)** — bookmarklet opens a cross-origin popup pointing at `http://127.0.0.1:<port>/popup.html` (served by the daemon's `WSServer`). The popup connects back via WS, handshakes with `{sid, token}`, then relays commands/results between daemon and host page via `postMessage`. Low latency, no focus stealing.
2. **QR / clipboard fallback** — daemon writes the command to the clipboard, focuses the bookmarklet's `about:blank` popup, sends Cmd+V; the page replies by rendering a QR code in a `<canvas>` and the daemon decodes it via macOS Vision framework.

Both run side-by-side; the daemon prefers WS but the QR poller stays live so a failed/refused WS attach silently falls back. On strict sites (`tai.sh` has `default-src 'none'` + `Cross-Origin-Opener-Policy: same-origin`) the WS popup detects a severed `window.opener` and refuses to attach, and we stay on QR.

### Two-popup design (important, non-obvious)
The bookmarklet keeps its **original `about:blank` popup** (for QR rendering / paste target) and opens a **second cross-origin popup** for the WS client. The WS popup reaches the host page via `window.opener.opener` (WS popup → about:blank popup → host). Don't replace the original popup with `location.replace` — that breaks QR fallback.

### Stealth QR (`--hide-qr`)
The popup paints reply QRs in two near-identical greens (`rgb(120,200,120)` light, `rgb(124,200,120)` dark — only 4-unit delta on the red channel). Humans and external phone cameras can't decode them. The daemon thresholds the captured framebuffer's red channel against `STEALTH_PIVOT_R = 122` to reconstruct a real B/W QR before handing it to Vision. Done via raw pixel access through `CGBitmapContext` + `bytearray` — **not** Core Image, because CI filters operate in linear-light by default and an sRGB pivot value gives wrong results without explicit color-space handling.

## Key files

| File | Role |
|---|---|
| `src/holo/daemon.py` | Single-process owner of `WSServer` + `ChannelRegistry`. `daemon.calibrate()` returns one `Channel` per tab. |
| `src/holo/channel.py` | Per-tab command channel. Owns title-poll calibration, QR poller, optional WS attachment. `send_command` picks transport. |
| `src/holo/registry.py` | Thread-safe `dict[sid, Channel]`. |
| `src/holo/ws_server.py` | websockets sync server on `127.0.0.1:<random>`. Serves `popup.html` + `framing.js` (HTTP) and routes WS handshakes to channels. |
| `src/holo/_macos.py` | AppKit/Quartz/Vision helpers — process activation (osascript fallback for Sonoma+), Cmd+V via System Events, window-pixel capture, QR decode, stealth-QR amplification. |
| `src/holo/cli.py` | `holo demo`, `holo doctor`. `--manual` and `--hide-qr` flags. |
| `src/holo/static/popup.html` | Daemon-served WS-client popup body. Reads `sid/token/parentOrigin` from URL fragment. Detects COOP severance and refuses on no-opener. |
| `bookmarklet/core.js` | Bookmarklet entrypoint — creates `about:blank` popup, registers host listener, opens WS popup on handshake, renders QR replies. |
| `bookmarklet/framing.js` | Title/QR framing protocol (chunk + ack). Mirrored in `src/holo/static/framing.js` for the popup's ES-module import. |

## Build / test commands

Python:
- `uv sync` — install deps incl. dev extras
- `uv run pytest` — run all daemon tests
- `uv run ruff check src tests` — lint
- `uv run holo demo` / `uv run holo demo --manual` / `uv run holo demo --hide-qr` — end-to-end smoke

Bookmarklet:
- `cd bookmarklet && npm install && npm run build` — produces `bookmarklet/dist/install.html` and bundled JS
- `npm test` — JS unit tests

After editing `bookmarklet/core.js` you must rebuild **and** re-drag the bookmarklet from `dist/install.html`. The bookmarklet's `install()` short-circuits when its popup already exists, so close the popup before re-installing.

## Conventions / gotchas

- **macOS Sonoma cross-app activation is restricted.** `NSRunningApplication.activateWithOptions_` silently fails from non-foreground processes. We always belt-and-suspenders with `osascript -e 'tell application "X" to activate'`. See `_macos.activate_pid`.
- **`pyautogui.hotkey('command','v')` doesn't reach Chrome popups reliably.** Use `keystroke_paste` (System Events via osascript) instead.
- **WindowServer truncates window titles >~70 chars.** That's why replies use the QR/canvas channel, not titles.
- **CSP varies a lot.** `tai.sh` is strict (`default-src 'none'`, only `ws://localhost:7080` allowed in `connect-src`); github.com / example.com are permissive. Don't assume connect-src or frame-src.
- **`websockets` sync `Headers` is multi-dict.** `__setitem__` appends; use `del response.headers["Content-Type"]` before setting if you need to overwrite.
- **The QR poller is opportunistic, not load-bearing.** Channels for which WS never lands stay on QR indefinitely — that's by design.
- **Per-process token + per-channel sid** — WS handshake validates `{sid, token}` against the registry. Don't leak the token.

## Branching / PR flow

- Feature branches off `main`. PRs squash-merged. Branch deleted on merge.
- Recent merged work:
  - **#19** — Phase 0 QR pivot (replies via canvas + Vision decode, escapes the title length limit)
  - **#20** — Phase 1 WebSocket transport + stealth-QR fallback (this is what `main` is now at: `8c5b6d5`)

## Phase status (from README)

- [x] Phase 0 — primitive layer (channel, framing, bookmarklet)
- [x] Phase 1 — agent surface (MCP server `holo mcp` over stdio; WS transport + stealth-QR fallback)
- [x] Phase 2 — cross-host bridge (`holo mcp-remote` spawn-per-connection stdio proxy + `holo mcp --listen PORT` / `holo connect HOST:PORT` persistent-daemon TCP transport — see `docs/cross-host.md`)
- [ ] Phase 3 — opt-in CDP adapter

## MCP surface (Phase 1)

`src/holo/mcp_server.py` exposes a `FastMCP` server with the channel + screen tools (`calibrate`, `list_channels`, `drop_channel`, `ping`, `read_global`, `send_command`, `bookmarklet_query`, plus `app_activate`, `screen_*` when `--bridge` is on, plus `browser_*` AppleScript ops). `HoloMCPServer` owns one lazily-constructed `Daemon` and translates `CalibrationError` / `CommandError` into MCP-style runtime errors.

Two transports:
- `holo mcp` — stdio (default; for `claude mcp add` spawning per session)
- `holo mcp --listen PORT` — TCP on 127.0.0.1:PORT, single concurrent connection, magic-prefix handshake (`HOLO/1\n`) required before MCP traffic. Daemon state persists across reconnects. Used with `holo connect HOST:PORT` (a stdio↔TCP bridge that injects the prefix) on the remote side of an SSH-tunnelled `mcp-remote`. Solves the macOS-TCC-on-SSH problem: the listener runs in a tmux session that has Screen Recording permission; SSH-spawned processes wouldn't.

When the registry is non-empty, `calibrate` fast-paths to the most recent channel — cross-host setups depend on this so the human can calibrate locally on the daemon's machine and the remote agent inherits the channel.

Both transports must keep stdout clean — diagnostics go to stderr only.

## Browser ops (AppleScript adapter)

`src/holo/browser_chrome.py` wraps Chrome's AppleScript dictionary as `browser_*` MCP tools (`browser_navigate`, `browser_new_tab`, `browser_list_tabs`, `browser_activate_tab`, `browser_close_active_tab`, `browser_read_active_*`, `browser_reload`, `browser_back`, `browser_forward`, `browser_execute_js`). These bypass the SikuliX keystroke layer entirely — no `app_activate` race, no focus beeps, no Accessibility permission needed for keyboard injection. Only Automation permission for the launching Terminal → Google Chrome.

**Use `browser_*` for any Chrome navigation.** SikuliX's `screen_key`/`screen_type` should be reserved for non-browser apps (terminals, native dialogs, canvas content).

For arbitrary DOM reads, prefer `browser_execute_js` (AppleScript) — it has no CSP constraint because it runs on Chrome's main world via Apple Events, not via a script the page can block. It needs Chrome → View → Developer → **Allow JavaScript from Apple Events** turned on; when that toggle is off, `JavaScriptNotAuthorized` surfaces a message naming the menu item and points the agent at `bookmarklet_query` (`query_selector` / `query_selector_all` ops in `bookmarklet/dispatch.js`) as the CSP-safe fallback. The bookmarklet path stays useful even when the AppleScript path is available — it doesn't need the toggle and works on any tab where the bookmarklet is calibrated.

macOS-only. Linux/Windows browser ops will land via the Phase 3 CDP adapter — Chrome 136+ blocks `--remote-debugging-port` on the default profile, so CDP requires a profile-switching dance that defeats holo's auth-piggyback pitch on macOS, but it's the right path for non-macOS.

## UI template cache (desktop DOM analog)

For UI elements outside any browser tab — Chrome's kebab/bookmarks bar, the dock, menu-bar items, native app buttons — `src/holo/templates.py` + the `ui_template_*` MCP tools provide a persistent name → image cache. Capture an element once (`ui_template_capture` blocks for a SikuliX `Region.userCapture()` rect drag, or accepts an explicit `region` for programmatic stash), then `ui_template_click("kebab", app="chrome")` in future sessions does template matching via `find_image_path` and clicks the center. Avoids re-doing vision each session for stable on-screen elements.

Storage: `~/Library/Caches/holo/templates/<app>/<label>.png` plus `index.json` with metadata (similarity, dimensions, last_used, match_count). `app=None` routes to `_global/`. Override root with `HOLO_TEMPLATE_DIR`. Variants per label support hover/idle/dark-mode states — `_find` walks them in order and returns the first hit; `_click` raises if nothing matches (silent miss is worse than a clear error). New JVM-side handlers `screen.user_capture` and `screen.find_image_path` mirror the existing `screen.shot` / `screen.find_image` but optimize for the cache's "PNGs already on disk" path.
