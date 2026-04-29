"""Daemon-side channel coordinator.

Composes the OS primitives — `holo.windows.list_windows()` for the
page → daemon direction, `holo.clipboard.paste()` for daemon → page,
and `holo.framing` for the wire format — into a single `Channel`
class with a small, blocking, request/response surface:

    ch = Channel()
    sid = ch.wait_for_calibration()       # picks up the bookmarklet beacon
    result = ch.send_command({"op": "ping"})

The Channel locks onto a specific browser window at calibration and
polls only that window's title for replies. Multi-window / multi-tab
addressing is a Phase 2 concern.

Single-frame replies only in this layer. Multi-frame reassembly is
implemented in `holo.framing.Reassembler` and will be wired into the
channel when a Phase 1 caller actually needs it; today's commands
(`ping`, `read_global` for short values) all fit in one title.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from holo import clipboard, framing
from holo import title as title_mod
from holo.windows import WindowInfo, list_windows

# Common browser owner-name strings on macOS. Users with other browsers
# can extend this via the `browsers` constructor arg.
DEFAULT_BROWSERS: frozenset[str] = frozenset(
    {
        "Google Chrome",
        "Google Chrome Canary",
        "Firefox",
        "Firefox Developer Edition",
        "Safari",
        "Brave Browser",
        "Microsoft Edge",
        "Arc",
        "Vivaldi",
        "Chromium",
    }
)

DEFAULT_POLL_INTERVAL_S: float = 0.05
DEFAULT_TIMEOUT_S: float = 5.0

# Sentinel for `_poll_reply_*`: the locked window has disappeared.
_WINDOW_GONE: object = object()
# Time to wait after activating the target app before clicking, so
# the OS has time to make it the key window. Empirically ~80–120 ms
# is enough on a warm app; 200 ms gives generous headroom.
ACTIVATE_SETTLE_S: float = 0.2
# Time after the synthetic click before sending Cmd+V, so the
# contenteditable has time to receive focus from the click event.
CLICK_SETTLE_S: float = 0.1


class CalibrationError(RuntimeError):
    """Raised when no calibration beacon arrives within the timeout."""


class CommandError(RuntimeError):
    """Raised when a command's reply doesn't arrive within the timeout."""


@dataclass(slots=True)
class Channel:
    browsers: frozenset[str] = field(default=DEFAULT_BROWSERS)
    poll_interval: float = DEFAULT_POLL_INTERVAL_S
    default_timeout: float = DEFAULT_TIMEOUT_S
    session: str | None = None
    _window_id: int | None = None
    _window_pid: int = 0
    _window_owner: str = ""

    def wait_for_calibration(self, *, timeout: float | None = None) -> str:
        """Poll browser windows for a `[holo:cal:<sid>]` beacon.

        Returns the session id and locks the channel to the window
        that emitted it. Raises `CalibrationError` on timeout.
        """
        budget = timeout if timeout is not None else self.default_timeout
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            for win in self._list_browser_windows():
                marker = title_mod.decode_plain(win.title)
                if marker and marker.startswith("cal:"):
                    self.session = marker[len("cal:") :]
                    self._window_id = win.id
                    self._window_pid = win.pid
                    self._window_owner = win.owner
                    return self.session
            time.sleep(self.poll_interval)
        raise CalibrationError(f"no calibration beacon within {budget}s")

    def send_command(self, cmd: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        """Send a JSON command, return the result dict.

        Blocks until a reply with the matching frame id arrives, or
        until `timeout` elapses (default `self.default_timeout`).
        """
        if self.session is None or self._window_id is None:
            raise RuntimeError(
                "Channel has not been calibrated; call wait_for_calibration() first"
            )
        data = json.dumps(cmd).encode("utf-8")
        frame = framing.Frame(session=self.session, type="cmd", data=data)
        text = frame.encode()

        # On macOS we use the osascript / System Events pipeline for
        # both activation and the Cmd+V keystroke. pyautogui's
        # synthetic keystrokes work for the terminal but have been
        # observed to never reach a Chrome popup's contenteditable
        # paste handler — System Events shares the same Automation
        # pipeline that successfully activates the popup, so the
        # keystroke and the activation can't race against each other.
        # We don't restore the clipboard here: a fast restore can
        # win the race against the OS-level paste, and the page's
        # handler reads event.clipboardData (a snapshot) anyway.
        if sys.platform == "darwin" and self._window_owner:
            self._activate_target()
            clipboard.write(text)
            time.sleep(0.05)
            from holo._macos import keystroke_paste

            keystroke_paste(self._window_owner)
        else:
            self._activate_target()
            clipboard.paste(text)
        budget = timeout if timeout is not None else self.default_timeout
        deadline = time.monotonic() + budget
        # The reply channel is a QR code rendered into the popup's
        # canvas. We capture the window's pixels and run Vision QR
        # detection on each poll. QR-decode round-trip is ~50–100 ms
        # vs ~1 ms for a title read, so we use a slower poll interval.
        qr_poll_interval = max(self.poll_interval, 0.15)
        reply_poller = self._poll_reply_qr if sys.platform == "darwin" else self._poll_reply_title
        while time.monotonic() < deadline:
            reply = reply_poller()
            if reply == _WINDOW_GONE:
                raise CommandError("locked window is no longer present")
            if (
                reply is not None
                and reply.id == frame.id
                and reply.session == self.session
                and reply.type == "result"
            ):
                return json.loads(reply.data.decode("utf-8"))
            time.sleep(qr_poll_interval)
        raise CommandError(f"no reply for cmd within {budget}s")

    def _poll_reply_qr(self) -> framing.Frame | object | None:
        """Capture the locked window and decode any QR code present."""
        from holo._macos import capture_window_qr

        # Confirm the window still exists; capture_window_qr returns
        # None for both 'window gone' and 'no QR yet', so we
        # disambiguate by listing windows separately.
        if self._read_window_title() is None:
            return _WINDOW_GONE
        payload = capture_window_qr(self._window_id) if self._window_id else None
        if not payload:
            return None
        try:
            return framing.decode(payload)
        except framing.FrameError:
            return None

    def _poll_reply_title(self) -> framing.Frame | object | None:
        """Legacy title-channel poll, kept for non-darwin platforms."""
        current = self._read_window_title()
        if current is None:
            return _WINDOW_GONE
        framed_json = title_mod.decode_framed(current)
        if not framed_json:
            return None
        try:
            return framing.decode(framed_json)
        except framing.FrameError:
            return None

    def _list_browser_windows(self) -> list[WindowInfo]:
        return [w for w in list_windows() if w.owner in self.browsers]

    def _read_window_title(self) -> str | None:
        for w in list_windows():
            if w.id == self._window_id:
                return w.title
        return None

    def _activate_target(self) -> None:
        """Bring the locked popup to the foreground and put OS keyboard
        focus inside its contenteditable body before pasting.

        Without activation, the synthesized Cmd+V lands in whatever app
        currently has keyboard focus (the terminal we're running from,
        a different browser window, …). Without the click, Chrome opens
        new popups with OS focus on the address bar — JS `.focus()`
        cannot move OS focus out of browser chrome, so we have to do it
        with a synthetic mouse click. On non-darwin platforms or when
        pid/bounds are unknown this degrades gracefully — callers must
        keep the target window focused themselves.
        """
        if sys.platform != "darwin" or self._window_pid <= 0:
            return
        from holo._macos import activate_pid, click_at

        if not activate_pid(self._window_pid):
            return
        time.sleep(ACTIVATE_SETTLE_S)

        click_point = self._popup_body_click_point()
        if click_point is not None:
            click_at(*click_point)
            time.sleep(CLICK_SETTLE_S)

    def _popup_body_click_point(self) -> tuple[float, float] | None:
        """Pick a point inside the popup body that is safely below the
        URL bar and away from window controls.

        Re-reads the locked window's current bounds — the user may
        have moved or resized the popup since calibration.

        The popup's vertical layout in Chrome:
            0–28 px   titlebar (traffic-light buttons)
            28–78 px  URL bar
            78– px    body / content area

        Targeting the horizontal center at 75 % of window height puts
        the click well inside the body even on a tiny popup, with
        plenty of margin from the URL bar above and the resize handle
        below.
        """
        for w in list_windows():
            if w.id == self._window_id and w.bounds is not None:
                x, y, width, height = w.bounds
                return (x + width / 2.0, y + height * 0.75)
        return None
