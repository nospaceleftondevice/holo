"""CLI surface for the Phase 0 walking skeleton.

Subcommands:

    holo --version         print version
    holo windows           print visible windows (smoke for windows reader)
    holo doctor            check macOS permissions / runtime environment
    holo demo              end-to-end smoke test against the in-page agent
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


def _cmd_demo(*, manual: bool = False, hide_qr: bool = False) -> int:
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

    daemon = Daemon(hide_qr=hide_qr)
    if hide_qr:
        print("QR reply channel: stealth (camera-resistant)")
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


COMMANDS = {
    "windows": _cmd_windows,
    "doctor": _cmd_doctor,
    "demo": _cmd_demo,
    "focus": _cmd_focus,
}


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print(
            f"holo {__version__} — try `holo --version`, `holo windows`, "
            "`holo doctor`, `holo demo`, or `holo focus`"
        )
        return 0
    cmd = args[0]
    rest = args[1:]
    if cmd in {"-V", "--version"}:
        print(__version__)
        return 0
    if cmd == "demo":
        return _cmd_demo(manual="--manual" in rest, hide_qr="--hide-qr" in rest)
    if cmd in COMMANDS:
        return COMMANDS[cmd]()
    print(f"holo: unknown command {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
