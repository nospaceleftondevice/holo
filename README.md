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

- [ ] Phase 0 — primitive layer (channel, framing protocol, bookmarklet payload)
- [ ] Phase 1 — agent surface (MCP server)
- [ ] Phase 2 — per-origin assertion plugins
- [ ] Phase 3 — cross-host registry + bridge
- [ ] Phase 4 — opt-in CDP adapter

## License

Apache-2.0. See [LICENSE](LICENSE).
