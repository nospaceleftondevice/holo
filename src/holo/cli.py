"""CLI surface for holo.

Subcommands:

    holo --version         print version
    holo windows           print visible windows (smoke for windows reader)
    holo doctor            check macOS permissions / runtime environment
    holo demo              end-to-end smoke test against the in-page agent
    holo mcp               run the MCP server over stdio
    holo bridge <verb>     smoke-test the SikuliX bridge directly
    holo install-bridge    pre-download the SikuliX jar into the user cache
    holo install-bookmarklet  download the bookmarklet page and open it
"""

from __future__ import annotations

import sys

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


def _cmd_demo(*, manual: bool = False, hide_qr: bool = False, use_bridge: bool = False) -> int:
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

    daemon = Daemon(hide_qr=hide_qr, use_bridge=use_bridge)
    if hide_qr:
        print("QR reply channel: stealth (camera-resistant)")
    if use_bridge:
        print("Input pipeline: SikuliX bridge (cross-platform)")
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


def _cmd_mcp(*, hide_qr: bool = False, use_bridge: bool = False) -> int:
    """Run the MCP server over stdio.

    Intended to be launched by an MCP client (Claude Code, Codex, Cursor)
    rather than from a terminal. The client exchanges JSON-RPC over the
    process's stdin/stdout, so anything we print to stdout corrupts the
    protocol — keep output on stderr only.
    """
    from holo import mcp_server

    print("holo mcp — starting MCP server over stdio", file=sys.stderr)
    if hide_qr:
        print("QR reply channel: stealth (camera-resistant)", file=sys.stderr)
    if use_bridge:
        print("Input pipeline: SikuliX bridge (cross-platform)", file=sys.stderr)
    mcp_server.run(hide_qr=hide_qr, use_bridge=use_bridge)
    return 0


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


_BRIDGE_USAGE = (
    "usage: holo bridge <verb> [args]\n"
    "  ping                          start the JVM bridge and ping it\n"
    "  activate <app>                bring an app to the foreground\n"
    "  click <x> <y>                 click at screen coordinates\n"
    "  key <combo>                   send a key combo (e.g. 'cmd+v', 'enter')\n"
    "  type <text>                   type a literal string"
)


def _cmd_bridge(rest: list[str]) -> int:
    """Drive the SikuliX bridge directly — no calibration, no channel.

    This is for sanity-checking the JVM + jar setup end-to-end. Each
    invocation spawns a fresh JVM, runs one verb, exits. Slow for
    everyday use; that's fine, this is a smoke tool.
    """
    from holo.bridge import BridgeClient, BridgeError, BridgeMissingError

    if not rest:
        print(_BRIDGE_USAGE, file=sys.stderr)
        return 2
    verb = rest[0]
    args = rest[1:]

    client = BridgeClient()
    try:
        client.start()
    except BridgeMissingError as e:
        print(f"holo bridge: {e}", file=sys.stderr)
        print(
            "Hint: install OpenJDK 11+ and drop sikulixapi.jar in vendor/ "
            "or set HOLO_SIKULI_JAR.",
            file=sys.stderr,
        )
        return 1
    except (BridgeError, OSError) as e:
        print(f"holo bridge: failed to start JVM: {e}", file=sys.stderr)
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
            print(_BRIDGE_USAGE, file=sys.stderr)
            return 2
    except BridgeError as e:
        print(f"holo bridge: error {e.code}: {e.message}", file=sys.stderr)
        if e.trace:
            print(e.trace, file=sys.stderr)
        return 1
    finally:
        client.stop()

    print(result)
    return 0


def _cmd_install_bridge() -> int:
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

    print(f"holo install-bridge — fetching {SIKULI_JAR_NAME}")
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
        print(f"holo install-bridge: {e}", file=sys.stderr)
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


COMMANDS = {
    "windows": _cmd_windows,
    "doctor": _cmd_doctor,
    "demo": _cmd_demo,
    "focus": _cmd_focus,
    "mcp": _cmd_mcp,
    "install-bridge": _cmd_install_bridge,
}


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print(
            f"holo {__version__} — try `holo --version`, `holo windows`, "
            "`holo doctor`, `holo demo`, `holo focus`, `holo mcp`, "
            "`holo mcp-remote`, `holo bridge`, `holo install-bridge`, "
            "or `holo install-bookmarklet`"
        )
        return 0
    cmd = args[0]
    rest = args[1:]
    if cmd in {"-V", "--version"}:
        print(__version__)
        return 0
    if cmd == "demo":
        return _cmd_demo(
            manual="--manual" in rest,
            hide_qr="--hide-qr" in rest,
            use_bridge="--bridge" in rest,
        )
    if cmd == "mcp":
        return _cmd_mcp(
            hide_qr="--hide-qr" in rest,
            use_bridge="--bridge" in rest,
        )
    if cmd == "mcp-remote":
        return _cmd_mcp_remote(rest)
    if cmd == "bridge":
        return _cmd_bridge(rest)
    if cmd == "install-bookmarklet":
        return _cmd_install_bookmarklet(rest)
    if cmd in COMMANDS:
        return COMMANDS[cmd]()
    print(f"holo: unknown command {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
