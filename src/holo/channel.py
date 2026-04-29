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
# Time to wait after activating the target app before sending Cmd+V,
# so the OS has time to make it the key window. Empirically ~80–120 ms
# is enough on a warm app; 200 ms gives generous headroom.
ACTIVATE_SETTLE_S: float = 0.2


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
        self._activate_target()
        clipboard.paste(frame.encode())
        budget = timeout if timeout is not None else self.default_timeout
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            current = self._read_window_title()
            if current is None:
                # Window has been closed or moved; abort with a clear error.
                raise CommandError("locked window is no longer present")
            framed_json = title_mod.decode_framed(current)
            if framed_json:
                try:
                    reply = framing.decode(framed_json)
                except framing.FrameError:
                    time.sleep(self.poll_interval)
                    continue
                if (
                    reply.id == frame.id
                    and reply.session == self.session
                    and reply.type == "result"
                ):
                    return json.loads(reply.data.decode("utf-8"))
            time.sleep(self.poll_interval)
        raise CommandError(f"no reply for cmd within {budget}s")

    def _list_browser_windows(self) -> list[WindowInfo]:
        return [w for w in list_windows() if w.owner in self.browsers]

    def _read_window_title(self) -> str | None:
        for w in list_windows():
            if w.id == self._window_id:
                return w.title
        return None

    def _activate_target(self) -> None:
        """Bring the locked window's app to the foreground before pasting.

        Without this step, the synthesized Cmd+V lands in whatever app
        currently has keyboard focus (the terminal we're running from,
        a different browser window, …). On non-darwin platforms or when
        the pid is unknown this is a no-op — callers must keep the
        target window focused themselves.
        """
        if sys.platform != "darwin" or self._window_pid <= 0:
            return
        from holo._macos import activate_pid

        if activate_pid(self._window_pid):
            time.sleep(ACTIVATE_SETTLE_S)
