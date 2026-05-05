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
# Replace TAG with the latest release tag, e.g. v0.1.0a10
TAG=v0.1.0a10
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
in the binary — it's fetched on first `--screen` use, or pre-warmed
with `holo install-screen`.

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

### Tailoring the tool surface per agent

Two flags shape what `holo mcp` exposes (a third — `--announce` —
adds an orthogonal mDNS broadcast; see below):

- `--screen` — register `screen_*`, `app_activate`, and `ui_template_*`.
  Brings up a SikuliX JVM (~200 MB; needs OpenJDK 11+). Off by default
  so agents that only do browser work don't pay for a JVM they never
  use.
- `--no-bookmarklet` — drop the seven channel-dependent tools
  (`calibrate`, `list_channels`, `drop_channel`, `ping`, `read_global`,
  `send_command`, `bookmarklet_query`) and skip starting the WS server
  / popup-serving infrastructure entirely. AppleScript-based
  `browser_*` tools still work.

Common combinations:

| Agent type | Flags | Why |
| --- | --- | --- |
| Browser-only orchestrator (multi-tab) | _(none)_ | Channel + AppleScript browser ops; no JVM |
| Browser + screen captures of Chrome chrome | `--screen` | Adds template / kebab-menu support |
| Slack / desktop-only orchestrator | `--screen --no-bookmarklet` | All screen ops; no bookmarklet, no WS port |
| AppleScript-only browser nav | `--no-bookmarklet` | No JVM, no WS port; AppleScript browser ops only |

Two independent CLI agents on the same host can run their own daemons
in parallel — each `holo mcp` is a self-contained process. Set
`HOLO_TEMPLATE_DIR` per project if both will capture templates, so
their cache indexes don't race.

### Session announcement (mDNS)

`holo mcp --announce` broadcasts an mDNS service record
(`_holo-session._tcp.local.`) carrying session metadata so a
companion desktop app on the same LAN can discover the session and
build a connection (SSH + tmux attach) to reach it. **No
authentication material is broadcast** — credentials live in your
SSH config / agent.

```bash
holo mcp --announce \
  --announce-session "claude-1" \
  --announce-user "$USER" \
  --announce-ssh-user "balexand"
```

| Flag | Purpose |
| --- | --- |
| `--announce` | Enable broadcast |
| `--announce-session NAME` | Logical session id (omitted if not set) |
| `--announce-user NAME` | Display label (default: current user) |
| `--announce-ssh-user NAME` | SSH login user (omitted if not set) |
| `--announce-ip A,B,C` | IPv4 override (default: enumerate every interface). Each entry is a literal IP or a trailing-dot prefix (e.g. `192.168.1.`) that filters the enumerated set — useful when you want to advertise only the LAN-side address and skip a VPN tunnel. |

If `$TMUX` is set, tmux session and window names are auto-detected
and added to the TXT record so the companion can `tmux attach -t`.
Cross-platform — works on macOS, Linux, and Windows without Avahi
or Bonjour installed (uses pure-Python `python-zeroconf`).

Verify the broadcast on macOS:
```bash
dns-sd -B _holo-session._tcp local
```

### Session discovery

`holo discover` is the in-tree consumer of the announce contract. The
desktop companion app talks to its `--serve` mode; you can also use it
from the CLI for ad-hoc inspection.

```bash
# One-shot JSON snapshot (default browse window: 3s)
holo discover --json

# Long-running JSONL event stream
holo discover --tail

# HTTP + WebSocket server (default :7082, talks to the desktop SPA)
holo discover --serve 7082
```

`--serve` exposes:

| Endpoint | Returns |
| --- | --- |
| `GET /sessions` | JSON array of currently-known sessions |
| `GET /healthz` | `{status, interfaces, zt_present}` |
| `WS /events` | newline-delimited `add`/`update`/`remove` events |

CORS allow-list defaults to `http://localhost:8888,https://app-dev.tai.sh`
for development; override with `--cors-origin A,B,C`. Stale entries
(announcer crashed without sending a Goodbye) are swept after
`--stale-after` seconds, default 150.

The wire contract is documented in
[`docs/companion-spec.md`](docs/companion-spec.md) — `holo discover`
is the reference implementation of that spec.

### Capabilities endpoint (`--announce-capabilities`)

When the user wants an agent to **route tasks by host capability**
("send transcription to the M4, not the M1"; "find a host with Chrome
Canary"), the daemon can also serve a small JSON inventory over a
token-authenticated HTTP endpoint. The bound port + auth token are
broadcast in the same mDNS TXT record as the rest of the session
metadata, so a discoverer reads them and fetches:

```
GET http://<ip>:<caps_port>/capabilities
X-Holo-Caps-Token: <caps_token>
```

```bash
holo mcp --announce --announce-capabilities \
  --probe-software chrome-canary,ffmpeg,ollama \
  --probe-pkg brew,apt
```

| Flag | Purpose |
| --- | --- |
| `--announce-capabilities` | Stand up the HTTP endpoint, advertise via TXT |
| `--probe-software A,B,C` | `which`-lookup names (defaults to a curated list) |
| `--probe-pkg M,M,M` | Package managers to query (`brew,apt,dpkg,dnf,yum,rpm,port,pacman,winget,choco`) |

Cross-platform — hardware probes use `sysctl` on macOS, `/proc` on
Linux, the Win32 API on Windows. Software probes use `shutil.which`
plus a small known-bundle map for macOS `.app`s. Package probes only
run for managers that exist on the host (`brew` skipped on Linux,
`apt` skipped on macOS, etc).

The endpoint is hardened against random web origins via a custom auth
header + zero CORS allow-headers (preflight fails, fetch never fires).
It is **not** hardened against same-LAN attackers — anyone who can read
the mDNS broadcast also gets the token. See
[`docs/companion-spec.md`](docs/companion-spec.md#3a-capabilities-http-endpoint-optional)
for the full wire contract.

**MCP tools.** An agent connected to one `holo mcp` instance can read
other holo hosts' inventories without leaving the agent loop. Both
tools answer instantly — `holo mcp` keeps a long-lived mDNS browser
(`DiscoverHandle`) running for the lifetime of the session, so the
cache is already populated by the time the agent queries it.

- `holo_discover_sessions(wait_s=0)` — lists every active holo session
  on the LAN from the cache. `wait_s` is an optional grace period
  for "I just spawned a daemon, give it time to land in the cache".
- `holo_fetch_capabilities(instance, timeout_s=5)` — reads the
  matching session from the cache and HTTP-fetches its
  `/capabilities` endpoint. `instance` matches the mDNS instance
  label, `session`, or `host` (in that order), so pass whatever the
  user typed. Falls through unreachable IPs to find the first that
  responds.

Both tools are always exposed (no `--bookmarklet` / `--screen`
dependency), so an agent on the receiving side gets capability-aware
routing for free.

## License

Apache-2.0. See [LICENSE](LICENSE).
