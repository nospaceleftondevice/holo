"""CLI surface for holo.

Subcommands:

    holo --version         print version
    holo windows           print visible windows (smoke for windows reader)
    holo doctor            check macOS permissions / runtime environment
    holo demo              end-to-end smoke test against the in-page agent
    holo mcp               run the MCP server over stdio
    holo mcp --listen PORT run the MCP server over TCP (single connection)
    holo connect HOST:PORT stdio↔TCP bridge to a listening `holo mcp`
    holo screen <verb>     smoke-test the SikuliX-backed screen tools directly
    holo install-screen    pre-download the SikuliX jar into the user cache
    holo install-bookmarklet  download the bookmarklet page and open it
"""

from __future__ import annotations

import sys
from typing import Any

from holo import __version__


def _cmd_windows() -> int:
    from holo.windows import list_windows

    try:
        windows = list_windows()
    except NotImplementedError as e:
        print(f"holo windows: {e}", file=sys.stderr)
        return 2
    if not windows:
        print("(no visible windows reported)")
        return 0
    for w in windows:
        title = w.title if w.title else "<unreadable>"
        print(f"{w.id:>8}  L{w.layer}  {w.owner!r:>24}  {title}")
    return 0


def _cmd_doctor() -> int:
    """Check the daemon's environment: platform, permissions, deps."""
    print(f"Python:    {sys.executable}")
    print(f"Platform:  {sys.platform}")
    print(f"Version:   holo {__version__}")
    print()

    if sys.platform != "darwin":
        print(f"⚠ holo currently supports macOS only; running on {sys.platform}.")
        return 1

    try:
        from holo.windows import list_windows
    except Exception as e:  # noqa: BLE001 — surface anything that breaks the import
        print(f"❌ holo.windows failed to import: {e}")
        return 1

    try:
        windows = list_windows()
    except Exception as e:  # noqa: BLE001
        print(f"❌ list_windows() raised: {e}")
        return 1

    from holo.channel import DEFAULT_BROWSERS

    total = len(windows)
    browser_wins = [w for w in windows if w.owner in DEFAULT_BROWSERS]
    browser_titled = sum(1 for w in browser_wins if w.title)
    print(f"Windows:   {total} visible total, {len(browser_wins)} from a browser")

    if total == 0:
        print()
        print("⚠ No visible windows reported. Is anything open?")
        return 1

    if not browser_wins:
        print()
        print("⚠ No browser windows visible. Open Chrome/Firefox/Safari/etc.")
        print("  before running `holo demo`.")
        return 1

    if browser_titled == 0:
        # System windows (e.g. WindowServer/StatusIndicator) have readable
        # titles even without Screen Recording permission, so we check
        # specifically that *browser* windows are readable.
        print()
        print("❌ Screen Recording permission appears to be missing.")
        print(f"   Grant access for: {sys.executable}")
        print("   System Settings → Privacy & Security → Screen Recording")
        print("   You may need to restart the daemon after granting.")
        return 1

    print(f"✓ Screen Recording permission granted ({browser_titled} browser titles readable).")
    print()

    try:
        import pyautogui  # noqa: F401
    except Exception as e:  # noqa: BLE001
        print(f"❌ pyautogui import failed: {e}")
        return 1
    try:
        import pyperclip  # noqa: F401
    except Exception as e:  # noqa: BLE001
        print(f"❌ pyperclip import failed: {e}")
        return 1
    print("✓ pyautogui and pyperclip importable.")
    print()
    print("Accessibility permission (for keyboard simulation) cannot be")
    print("detected without firing a keystroke. If `holo demo` fails to")
    print("get a reply, grant Accessibility for the same Python binary at:")
    print("  System Settings → Privacy & Security → Accessibility")
    return 0


_MANUAL_COUNTDOWN_S: int = 5


def _cmd_demo(*, manual: bool = False, hide_qr: bool = False, enable_screen: bool = False) -> int:
    """End-to-end smoke test: read R2D2_VERSION through the channel.

    Pass `manual=True` (or run `holo demo --manual`) to skip the
    automatic activate-and-click step and instead use a fixed
    countdown: you click into the popup body and don't touch the
    keyboard until the paste fires. Useful when cross-app activation
    is being denied by the OS.

    Pass `hide_qr=True` (or `holo demo --hide-qr`) to render the QR
    reply channel in two near-identical greens that humans / external
    cameras can't decode; the daemon amplifies the subtle red-channel
    delta in software just before running Vision QR detection.
    """
    import time

    from holo.channel import CalibrationError, CommandError
    from holo.daemon import Daemon

    print("holo demo — Phase 0 walking-skeleton" + (" (manual)" if manual else ""))
    print()
    print("Setup:")
    print("  1. (one-time) Build & install the bookmarklet:")
    print("       cd bookmarklet && npm install && npm run build")
    print("       open bookmarklet/dist/install.html")
    print("       drag the 🔧 holo link to your bookmarks bar")
    print("  2. (one-time) Allow popups for the host page in your browser.")
    print("  3. Open https://tai.sh (or any page exposing R2D2_VERSION).")
    print("  4. After this command starts polling, click the 🔧 holo bookmark.")
    print("     A small dark green 'holo console' popup will open — leave it.")
    if manual:
        print(
            f"     In manual mode, you'll have {_MANUAL_COUNTDOWN_S} s after"
            " calibration to click"
        )
        print(
            "     into the popup body. The paste fires automatically — don't"
        )
        print("     touch the keyboard once you've clicked.")
    else:
        print("     The daemon will raise it before each command, so you don't")
        print("     have to babysit focus.")
    print()
    print("Run `holo doctor` first if you suspect a permissions issue.")
    print()

    daemon = Daemon(hide_qr=hide_qr, enable_screen=enable_screen)
    if hide_qr:
        print("QR reply channel: stealth (camera-resistant)")
    if enable_screen:
        print("Screen tools: SikuliX bridge enabled")
    print(f"WS listener: {daemon.ws_server.url}")
    print("Polling for calibration beacon (60s timeout)…")
    try:
        ch = daemon.calibrate(timeout=60.0)
    except CalibrationError as e:
        print(f"❌ {e}", file=sys.stderr)
        print(
            "   Is the bookmarklet installed and clicked on a normal http(s) page?",
            file=sys.stderr,
        )
        print("   Run `holo doctor` to check Screen Recording permission.", file=sys.stderr)
        return 1

    print(f"✓ calibrated · session={ch.session} window={ch._window_id}")

    if manual:
        # Disable the auto activate+click so the user can drive focus
        # by hand. We countdown without reading stdin — pressing Enter
        # would steal focus back from the popup.
        ch._window_pid = 0
        print()
        print("Manual mode: click anywhere in the GREEN popup body NOW.")
        print("Don't touch the keyboard or any other window.")
        print("The paste will fire automatically after the countdown.")
        for i in range(_MANUAL_COUNTDOWN_S, 0, -1):
            print(f"  {i}…", flush=True)
            time.sleep(1.0)

    print()
    print("Sending read_global(document.title)…")
    try:
        title_result = ch.send_command(
            {"op": "read_global", "path": "document.title"}, timeout=10.0
        )
    except CommandError as e:
        print(f"❌ {e}", file=sys.stderr)
        print("   Possible causes:", file=sys.stderr)
        print("   - The popup didn't have OS keyboard focus when Cmd+V fired", file=sys.stderr)
        print("     (try `holo demo --manual` and click the popup body yourself)", file=sys.stderr)
        print(
            "   - Accessibility permission missing (System Settings → Privacy & Security)",
            file=sys.stderr,
        )
        print("   - osascript Automation prompt was denied — re-allow in", file=sys.stderr)
        print("     System Settings → Privacy & Security → Automation", file=sys.stderr)
        return 1

    transport = "ws" if ch._ws_ready else "qr"
    print(f"✓ title: {title_result}  (transport: {transport})")

    print("Sending read_global(R2D2_VERSION)…")
    try:
        result = ch.send_command(
            {"op": "read_global", "path": "R2D2_VERSION"}, timeout=10.0
        )
    except CommandError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1

    print(f"✓ result: {result}  (transport: {transport})")

    # If WS came up, send a second command — should be near-instant and
    # not steal focus. Confirms the post-handshake hot path works.
    if ch._ws_ready:
        print("Re-sending over WS to confirm hot path…")
        try:
            result2 = ch.send_command(
                {"op": "read_global", "path": "R2D2_VERSION"}, timeout=5.0
            )
            print(f"✓ result: {result2}  (transport: ws)")
        except CommandError as e:
            print(f"❌ second send failed: {e}", file=sys.stderr)
            return 1
    return 0


def _cmd_focus() -> int:
    """Diagnostic: activate + click into the locked window's body, no paste.

    Calibrates against the live bookmarklet, then runs the same
    activate-and-click sequence the demo uses before pasting — without
    sending any keystroke. Lets you see *visually* whether the popup
    comes to the foreground and whether the click hits the body.
    """
    import time

    from holo.channel import CalibrationError, Channel

    print("holo focus — diagnostic activate-and-click (no paste)")
    print()
    print("Open the holo console popup first (run holo demo, click the bookmark)")
    print("and leave it visible. This command will calibrate against it, then")
    print("activate + click. Watch whether the popup comes to the front and")
    print("whether the click lands inside the green body.")
    print()

    ch = Channel(default_timeout=15.0)
    print("Polling for calibration beacon (15s timeout)…")
    try:
        sid = ch.wait_for_calibration()
    except CalibrationError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1
    print(f"✓ calibrated · session={sid} window={ch._window_id} pid={ch._window_pid}")
    print()
    print("Activating + clicking in 3s — watch the popup…")
    time.sleep(3.0)
    ch._activate_target()
    print("✓ done. Did the popup come to the front and receive the click?")
    return 0


def _cmd_mcp(
    *,
    hide_qr: bool = False,
    enable_screen: bool = False,
    no_bookmarklet: bool = False,
    listen_port: int | None = None,
    announce: bool = False,
    announce_session: str | None = None,
    announce_user: str | None = None,
    announce_ssh_user: str | None = None,
    announce_ips: list[str] | None = None,
    announce_capabilities: bool = False,
    probe_software: list[str] | None = None,
    probe_packages: list[str] | None = None,
) -> int:
    """Run the MCP server.

    Default mode is stdio — intended to be launched by an MCP client
    (Claude Code, Codex, Cursor) rather than from a terminal.

    With `--listen PORT`, instead binds 127.0.0.1:PORT and accepts a
    single concurrent TCP client. Each new connection must send the
    magic handshake prefix before any MCP traffic, so a drive-by
    browser can't reach the server (browsers can't control the
    first bytes of a TCP connection — fetch always sends an HTTP
    request line first). Daemon state persists across reconnects.

    With `--no-bookmarklet`, the channel-dependent tools are
    omitted from the surface and the WS server isn't started. Use
    this for agents that only drive screen / template / AppleScript
    tools — e.g. a Slack-only orchestrator.

    With `--announce`, broadcasts an mDNS service record so a
    companion desktop app on the same LAN can discover this session.
    Optional metadata: `--announce-session NAME` (logical session
    id), `--announce-user NAME` (display label, defaults to $USER),
    `--announce-ssh-user NAME` (SSH login user, omitted if not set),
    `--announce-ip A,B,C` (comma-separated IPv4 list; each entry is
    either a literal IP that's advertised verbatim or a trailing-dot
    prefix like `192.168.1.` that filters the enumerated interfaces.
    Default is to enumerate every non-loopback interface).

    Either way we print only to stderr — stdout carries protocol.
    """
    from holo import mcp_server

    def _banner_lines() -> list[str]:
        lines: list[str] = []
        if hide_qr:
            lines.append("QR reply channel: stealth (camera-resistant)")
        if enable_screen:
            lines.append("Screen tools: SikuliX bridge enabled")
        if no_bookmarklet:
            lines.append("Bookmarklet channel: disabled (--no-bookmarklet)")
        if announce:
            label_bits = []
            if announce_session:
                label_bits.append(f"session={announce_session}")
            if announce_user:
                label_bits.append(f"user={announce_user}")
            if announce_ips:
                label_bits.append(f"ips={','.join(announce_ips)}")
            label = " ".join(label_bits) if label_bits else "(defaults)"
            lines.append(f"mDNS announce: enabled — {label}")
        if announce_capabilities:
            sw = ",".join(probe_software) if probe_software else "default"
            pkg = ",".join(probe_packages) if probe_packages else "(none)"
            lines.append(
                f"Capabilities endpoint: enabled — software={sw} pkg={pkg}"
            )
        return lines

    announce_kwargs: dict[str, Any] = {
        "announce": announce,
        "announce_session": announce_session,
        "announce_user": announce_user,
        "announce_ssh_user": announce_ssh_user,
        "announce_ips": announce_ips,
        "announce_capabilities": announce_capabilities,
        "probe_software": probe_software,
        "probe_packages": probe_packages,
    }

    if listen_port is not None:
        print(
            f"holo mcp — listening on 127.0.0.1:{listen_port} "
            "(magic prefix required)",
            file=sys.stderr,
        )
        for line in _banner_lines():
            print(line, file=sys.stderr)
        mcp_server.run_tcp(
            listen_port,
            hide_qr=hide_qr,
            enable_screen=enable_screen,
            no_bookmarklet=no_bookmarklet,
            **announce_kwargs,
        )
        return 0

    print("holo mcp — starting MCP server over stdio", file=sys.stderr)
    for line in _banner_lines():
        print(line, file=sys.stderr)
    mcp_server.run(
        hide_qr=hide_qr,
        enable_screen=enable_screen,
        no_bookmarklet=no_bookmarklet,
        **announce_kwargs,
    )
    return 0


def _cmd_connect(rest: list[str]) -> int:
    """Bridge process stdio to a listening `holo mcp --listen PORT`.

    Used as the remote side of an SSH-tunnelled MCP setup:

        holo mcp-remote -- ssh user@host /usr/local/bin/holo connect localhost:7777

    The magic handshake prefix is sent automatically — users never
    type it.
    """
    from holo import mcp_connect

    if len(rest) != 1 or rest[0] in {"-h", "--help"}:
        sys.stderr.write(
            "usage: holo connect HOST:PORT\n"
            "example: holo connect localhost:7777\n"
        )
        return 2
    return mcp_connect.run(rest[0])


def _cmd_mcp_remote(rest: list[str]) -> int:
    """Bridge a local MCP client's stdio to a remote `holo mcp`.

    Spawns the user-supplied command (everything after `--`), strips
    stdout banner content until a JSON envelope arrives, then becomes a
    transparent stdio proxy. See `holo.mcp_remote` for the proxy
    semantics. Typical usage:

        holo mcp-remote -- ssh -A hostA holo mcp
        holo mcp-remote -- kubectl exec -i pod-x -- holo mcp
    """
    from holo import mcp_remote

    if "--" not in rest:
        sys.stderr.write(
            "usage: holo mcp-remote [--startup-timeout SECS] -- <command>\n"
            "example: holo mcp-remote -- ssh -A hostA holo mcp\n"
        )
        return 2
    sep = rest.index("--")
    flags = rest[:sep]
    child_argv = rest[sep + 1:]

    timeout = mcp_remote.DEFAULT_STARTUP_TIMEOUT_S
    i = 0
    while i < len(flags):
        flag = flags[i]
        if flag == "--startup-timeout" and i + 1 < len(flags):
            try:
                timeout = float(flags[i + 1])
            except ValueError:
                sys.stderr.write(
                    f"holo mcp-remote: invalid --startup-timeout: {flags[i + 1]!r}\n"
                )
                return 2
            i += 2
        else:
            sys.stderr.write(f"holo mcp-remote: unknown flag: {flag!r}\n")
            return 2

    return mcp_remote.run(child_argv, startup_timeout_s=timeout)


_SCREEN_USAGE = (
    "usage: holo screen <verb> [args]\n"
    "  ping                          start the JVM bridge and ping it\n"
    "  activate <app>                bring an app to the foreground\n"
    "  click <x> <y>                 click at screen coordinates\n"
    "  key <combo>                   send a key combo (e.g. 'cmd+v', 'enter')\n"
    "  type <text>                   type a literal string"
)


def _cmd_screen(rest: list[str]) -> int:
    """Drive the SikuliX-backed screen tools directly — no channel.

    This is for sanity-checking the JVM + jar setup end-to-end. Each
    invocation spawns a fresh JVM, runs one verb, exits. Slow for
    everyday use; that's fine, this is a smoke tool.
    """
    from holo.bridge import BridgeClient, BridgeError, BridgeMissingError

    if not rest:
        print(_SCREEN_USAGE, file=sys.stderr)
        return 2
    verb = rest[0]
    args = rest[1:]

    client = BridgeClient()
    try:
        client.start()
    except BridgeMissingError as e:
        print(f"holo screen: {e}", file=sys.stderr)
        print(
            "Hint: install OpenJDK 11+ and drop sikulixapi.jar in vendor/ "
            "or set HOLO_SIKULI_JAR.",
            file=sys.stderr,
        )
        return 1
    except (BridgeError, OSError) as e:
        print(f"holo screen: failed to start JVM: {e}", file=sys.stderr)
        return 1

    try:
        if verb == "ping":
            result = client.ping()
        elif verb == "activate" and len(args) == 1:
            result = client.activate(args[0])
        elif verb == "click" and len(args) == 2:
            result = client.click(int(args[0]), int(args[1]))
        elif verb == "key" and len(args) == 1:
            result = client.key(args[0])
        elif verb == "type" and len(args) >= 1:
            result = client.type_text(" ".join(args))
        else:
            print(_SCREEN_USAGE, file=sys.stderr)
            return 2
    except BridgeError as e:
        print(f"holo screen: error {e.code}: {e.message}", file=sys.stderr)
        if e.trace:
            print(e.trace, file=sys.stderr)
        return 1
    finally:
        client.stop()

    print(result)
    return 0


def _cmd_install_screen() -> int:
    """Pre-download the SikuliX jar into the user cache.

    Useful in air-gapped / metered-bandwidth environments where you want
    to stage the jar up front rather than letting the daemon fetch it on
    first run. Idempotent: if the cached file is already valid, exits
    immediately.
    """
    from holo.bridge import (
        SIKULI_JAR_BYTES,
        SIKULI_JAR_NAME,
        SIKULI_JAR_URL,
        BridgeMissingError,
        ensure_jar,
    )

    print(f"holo install-screen — fetching {SIKULI_JAR_NAME}")
    print(f"  source: {SIKULI_JAR_URL}")

    last_pct = {"value": -1}

    def on_progress(read: int, total: int) -> None:
        if not total:
            return
        pct = int(100 * read / total)
        if pct != last_pct["value"]:
            last_pct["value"] = pct
            sys.stdout.write(
                f"\r  progress: {pct:3d}%  ({read / 1_048_576:.1f} / "
                f"{total / 1_048_576:.1f} MiB)"
            )
            sys.stdout.flush()

    try:
        path = ensure_jar(on_progress=on_progress)
    except BridgeMissingError as e:
        print()
        print(f"holo install-screen: {e}", file=sys.stderr)
        return 1
    finally:
        if last_pct["value"] >= 0:
            print()
    print(f"✓ cached at {path}")
    print(f"  size:   {path.stat().st_size} bytes (pinned: {SIKULI_JAR_BYTES})")
    return 0


def _cmd_install_bookmarklet(rest: list[str]) -> int:
    """Download `holo-bookmarklet.html` from the matching release and
    open it in the default browser."""
    from holo import install_bookmarklet

    url: str | None = None
    i = 0
    while i < len(rest):
        flag = rest[i]
        if flag == "--url" and i + 1 < len(rest):
            url = rest[i + 1]
            i += 2
        else:
            sys.stderr.write(
                f"holo install-bookmarklet: unknown flag: {flag!r}\n"
                "usage: holo install-bookmarklet [--url URL]\n"
            )
            return 2
    return install_bookmarklet.run(url=url)


_DISCOVER_USAGE = (
    "usage: holo discover [--json | --tail | --serve PORT] [options]\n"
    "  --json              one-shot snapshot, JSON array, exit\n"
    "  --tail              long-running JSONL event stream\n"
    "  --serve PORT        HTTP+WebSocket server (default 7082)\n"
    "  --wait SECS         --json browse window (default 3.0)\n"
    "  --stale-after SECS  drop sessions older than this (default 150)\n"
    "  --cors-origin O     comma-separated CORS allow-list "
    "(default: http://localhost:8888,https://app-dev.tai.sh)"
)


def _cmd_discover(rest: list[str]) -> int:
    """Browse the LAN for `_holo-session._tcp.local.` broadcasts.

    Reference consumer of `docs/companion-spec.md`. See `--help` for
    the three output modes (`--json`, `--tail`, `--serve PORT`).
    """
    from holo import discover

    json_mode = "--json" in rest
    tail_mode = "--tail" in rest
    serve_port_raw = _value_flag(rest, "--serve")

    selected = sum([json_mode, tail_mode, serve_port_raw is not None])
    if selected == 0:
        sys.stderr.write(
            "holo discover: pick exactly one mode (--json | --tail | --serve PORT)\n"
            f"{_DISCOVER_USAGE}\n"
        )
        return 2
    if selected > 1:
        sys.stderr.write(
            "holo discover: --json / --tail / --serve are mutually exclusive\n"
        )
        return 2

    if serve_port_raw is _MISSING_ARG:
        sys.stderr.write("holo discover: --serve requires a port number\n")
        return 2

    wait_raw = _value_flag(rest, "--wait")
    if wait_raw is _MISSING_ARG:
        sys.stderr.write("holo discover: --wait requires a value\n")
        return 2
    wait_s = discover.DEFAULT_JSON_WAIT_S
    if isinstance(wait_raw, str):
        try:
            wait_s = float(wait_raw)
        except ValueError:
            sys.stderr.write(
                f"holo discover: invalid --wait value {wait_raw!r}\n"
            )
            return 2

    stale_raw = _value_flag(rest, "--stale-after")
    if stale_raw is _MISSING_ARG:
        sys.stderr.write("holo discover: --stale-after requires a value\n")
        return 2
    stale_after_s = discover.DEFAULT_STALE_AFTER_S
    if isinstance(stale_raw, str):
        try:
            stale_after_s = float(stale_raw)
        except ValueError:
            sys.stderr.write(
                f"holo discover: invalid --stale-after value {stale_raw!r}\n"
            )
            return 2

    cors_raw = _value_flag(rest, "--cors-origin")
    if cors_raw is _MISSING_ARG:
        sys.stderr.write("holo discover: --cors-origin requires a value\n")
        return 2
    cors_origins: list[str] | None = None
    if isinstance(cors_raw, str):
        cors_origins = [
            o.strip() for o in cors_raw.split(",") if o.strip()
        ]
        if not cors_origins:
            sys.stderr.write(
                "holo discover: --cors-origin requires at least one origin\n"
            )
            return 2

    if json_mode:
        return discover.run_oneshot(wait_s=wait_s)
    if tail_mode:
        return discover.run_tail(stale_after_s=stale_after_s)
    # --serve
    assert isinstance(serve_port_raw, str)  # _value_flag already returned a str
    try:
        port = int(serve_port_raw)
    except ValueError:
        sys.stderr.write(
            f"holo discover: invalid --serve port {serve_port_raw!r}\n"
        )
        return 2
    if not (0 < port < 65536):
        sys.stderr.write(f"holo discover: --serve port {port} out of range\n")
        return 2
    return discover.run_serve(
        port=port,
        cors_origins=cors_origins,
        stale_after_s=stale_after_s,
    )


COMMANDS = {
    "windows": _cmd_windows,
    "doctor": _cmd_doctor,
    "demo": _cmd_demo,
    "focus": _cmd_focus,
    "mcp": _cmd_mcp,
    "install-screen": _cmd_install_screen,
}


def _print_help() -> None:
    print(f"""holo {__version__} — browser + screen automation for AI agents

Usage: holo <command> [options]

Commands:
  doctor                  check macOS permissions / runtime environment
  demo [--manual] [--hide-qr] [--screen]
                          end-to-end smoke test against the in-page agent
  mcp [--listen PORT] [--hide-qr] [--screen] [--no-bookmarklet]
      [--announce] [--announce-session NAME] [--announce-user NAME]
      [--announce-ssh-user NAME] [--announce-ip A,B,C]
      [--announce-capabilities] [--probe-software a,b,c] [--probe-pkg a,b,c]
                          run the MCP server over stdio (or TCP with --listen)
                          --screen          enable screen / template / app_activate tools
                          --no-bookmarklet  drop channel tools; no WS server
                          --announce        broadcast session via mDNS
                          --announce-session NAME    logical session id
                          --announce-user NAME       display label (default: $USER)
                          --announce-ssh-user NAME   SSH login user
                          --announce-ip A,B,C        IPv4 override; each entry is a
                                                     literal IP or a trailing-dot
                                                     prefix (e.g. `192.168.1.`) that
                                                     filters the enumerated set
                          --announce-capabilities    serve hardware/software inventory
                                                     over a token-auth HTTP endpoint
                          --probe-software A,B,C     extra `which`-lookup names
                          --probe-pkg M,M,M          probe package managers
                                                     (brew,apt,dpkg,dnf,yum,rpm,
                                                      port,pacman,winget,choco)
  connect HOST:PORT       stdio↔TCP bridge to a listening `holo mcp`
  mcp-remote -- CMD ...   spawn-per-connection stdio proxy
  discover [--json | --tail | --serve PORT] [--wait SECS]
           [--stale-after SECS] [--cors-origin O,O,...]
                          discover live `_holo-session._tcp.local.` broadcasts
                          (reference consumer for docs/companion-spec.md)
  windows                 print visible windows (smoke for windows reader)
  screen <verb>           smoke-test the SikuliX-backed screen tools directly
  install-screen          pre-download the SikuliX jar into the user cache
  install-bookmarklet     download the bookmarklet page and open it

Options:
  -h, --help              show this help and exit
  -V, --version           print version and exit

Quick start: `holo install-bookmarklet` to install the bookmarklet, then
`holo mcp` to run the MCP server.""")


_MISSING_ARG = object()


def _value_flag(rest: list[str], flag: str) -> str | None | object:
    """Read a `--flag VALUE` pair from `rest`.

    Returns:
        - ``None`` if the flag is absent
        - ``_MISSING_ARG`` if the flag is present but lacks a value
        - the string value otherwise
    """
    if flag not in rest:
        return None
    i = rest.index(flag)
    if i + 1 >= len(rest):
        return _MISSING_ARG
    value = rest[i + 1]
    if value.startswith("--"):
        return _MISSING_ARG
    return value


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        _print_help()
        return 0
    cmd = args[0]
    rest = args[1:]
    if cmd in {"-h", "--help", "help"}:
        _print_help()
        return 0
    if cmd in {"-V", "--version"}:
        print(__version__)
        return 0
    if cmd == "demo":
        return _cmd_demo(
            manual="--manual" in rest,
            hide_qr="--hide-qr" in rest,
            enable_screen="--screen" in rest,
        )
    if cmd == "mcp":
        listen_port: int | None = None
        if "--listen" in rest:
            i = rest.index("--listen")
            if i + 1 >= len(rest):
                sys.stderr.write("holo mcp: --listen requires a port number\n")
                return 2
            try:
                listen_port = int(rest[i + 1])
            except ValueError:
                sys.stderr.write(
                    f"holo mcp: invalid --listen port {rest[i + 1]!r}\n"
                )
                return 2
            if not (0 < listen_port < 65536):
                sys.stderr.write(
                    f"holo mcp: --listen port {listen_port} out of range\n"
                )
                return 2

        announce_session = _value_flag(rest, "--announce-session")
        announce_user = _value_flag(rest, "--announce-user")
        announce_ssh_user = _value_flag(rest, "--announce-ssh-user")
        announce_ip_raw = _value_flag(rest, "--announce-ip")
        probe_software_raw = _value_flag(rest, "--probe-software")
        probe_pkg_raw = _value_flag(rest, "--probe-pkg")
        announce = "--announce" in rest
        announce_capabilities = "--announce-capabilities" in rest
        if not announce and (
            announce_session is not None
            or announce_user is not None
            or announce_ssh_user is not None
            or announce_ip_raw is not None
            or announce_capabilities
            or probe_software_raw is not None
            or probe_pkg_raw is not None
        ):
            sys.stderr.write(
                "holo mcp: --announce-session/--announce-user/"
                "--announce-ssh-user/--announce-ip/--announce-capabilities/"
                "--probe-software/--probe-pkg require --announce\n"
            )
            return 2
        if announce_session is _MISSING_ARG:
            sys.stderr.write("holo mcp: --announce-session requires a value\n")
            return 2
        if announce_user is _MISSING_ARG:
            sys.stderr.write("holo mcp: --announce-user requires a value\n")
            return 2
        if announce_ssh_user is _MISSING_ARG:
            sys.stderr.write("holo mcp: --announce-ssh-user requires a value\n")
            return 2
        if announce_ip_raw is _MISSING_ARG:
            sys.stderr.write("holo mcp: --announce-ip requires a value\n")
            return 2
        if probe_software_raw is _MISSING_ARG:
            sys.stderr.write(
                "holo mcp: --probe-software requires a value\n"
            )
            return 2
        if probe_pkg_raw is _MISSING_ARG:
            sys.stderr.write("holo mcp: --probe-pkg requires a value\n")
            return 2
        if (
            probe_software_raw is not None or probe_pkg_raw is not None
        ) and not announce_capabilities:
            sys.stderr.write(
                "holo mcp: --probe-software / --probe-pkg require "
                "--announce-capabilities\n"
            )
            return 2

        announce_ips: list[str] | None = None
        if isinstance(announce_ip_raw, str):
            announce_ips = [
                ip.strip() for ip in announce_ip_raw.split(",") if ip.strip()
            ]
            if not announce_ips:
                sys.stderr.write(
                    "holo mcp: --announce-ip requires at least one IP\n"
                )
                return 2

        probe_software: list[str] | None = None
        if isinstance(probe_software_raw, str):
            from holo.capabilities import parse_software_list

            probe_software = parse_software_list(probe_software_raw)
            if not probe_software:
                sys.stderr.write(
                    "holo mcp: --probe-software requires at least one name\n"
                )
                return 2

        probe_packages: list[str] | None = None
        if isinstance(probe_pkg_raw, str):
            from holo.capabilities import (
                SUPPORTED_PKG_MANAGERS,
                parse_pkg_managers,
            )

            accepted, unknown = parse_pkg_managers(probe_pkg_raw)
            if unknown:
                sys.stderr.write(
                    f"holo mcp: --probe-pkg unknown manager(s): "
                    f"{','.join(unknown)} "
                    f"(supported: {','.join(SUPPORTED_PKG_MANAGERS)})\n"
                )
                return 2
            if not accepted:
                sys.stderr.write(
                    "holo mcp: --probe-pkg requires at least one manager\n"
                )
                return 2
            probe_packages = accepted

        return _cmd_mcp(
            hide_qr="--hide-qr" in rest,
            enable_screen="--screen" in rest,
            no_bookmarklet="--no-bookmarklet" in rest,
            listen_port=listen_port,
            announce=announce,
            announce_session=announce_session,
            announce_user=announce_user,
            announce_ssh_user=announce_ssh_user,
            announce_ips=announce_ips,
            announce_capabilities=announce_capabilities,
            probe_software=probe_software,
            probe_packages=probe_packages,
        )
    if cmd == "connect":
        return _cmd_connect(rest)
    if cmd == "mcp-remote":
        return _cmd_mcp_remote(rest)
    if cmd == "screen":
        return _cmd_screen(rest)
    if cmd == "install-bookmarklet":
        return _cmd_install_bookmarklet(rest)
    if cmd == "discover":
        return _cmd_discover(rest)
    if cmd in COMMANDS:
        return COMMANDS[cmd]()
    print(
        f"holo: unknown command {cmd!r} (try `holo --help`)", file=sys.stderr
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
