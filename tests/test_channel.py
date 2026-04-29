"""Tests for holo.channel — orchestration of clipboard + window-title primitives.

The channel itself is platform-neutral (it composes the OS primitives),
so these tests mock both `holo.windows.list_windows` and
`holo.clipboard.paste`. The orchestration tests verify the request/
response loop end-to-end without needing a real browser.
"""

from __future__ import annotations

import base64
import json
from unittest.mock import patch

import pytest

from holo import framing
from holo.channel import CalibrationError, Channel, CommandError
from holo.windows import WindowInfo


def _w(id, title, owner="Google Chrome", pid=0, bounds=None):
    return WindowInfo(id=id, title=title, owner=owner, layer=0, pid=pid, bounds=bounds)


def _make_reply_title(*, frame_id, session, result, original_title="Page"):
    """Build a [holo:1:...] title that core.js would produce in response to a cmd."""
    reply_frame = framing.Frame(
        session=session,
        type="result",
        data=json.dumps(result).encode("utf-8"),
        id=frame_id,
    )
    encoded = base64.b64encode(reply_frame.encode().encode("utf-8")).decode("ascii")
    return f"{original_title} [holo:1:{encoded}]"


@pytest.fixture
def fake_list_windows():
    with patch("holo.channel.list_windows") as m:
        yield m


@pytest.fixture
def fake_paste():
    with patch("holo.clipboard.paste") as m:
        yield m


class TestWaitForCalibration:
    def test_returns_session_from_beacon_and_locks_window(self, fake_list_windows):
        fake_list_windows.return_value = [
            _w(1, "Other - Chrome"),
            _w(42, "GitHub [holo:cal:abc-123]", pid=9876),
        ]
        ch = Channel(poll_interval=0.001, default_timeout=0.5)
        assert ch.wait_for_calibration() == "abc-123"
        assert ch._window_id == 42
        assert ch._window_pid == 9876

    def test_ignores_non_browser_windows(self, fake_list_windows):
        fake_list_windows.return_value = [
            _w(42, "X [holo:cal:abc]", owner="Some App"),
        ]
        ch = Channel(poll_interval=0.001, default_timeout=0.05)
        with pytest.raises(CalibrationError):
            ch.wait_for_calibration()

    def test_ignores_non_calibration_markers(self, fake_list_windows):
        fake_list_windows.return_value = [
            _w(42, "X [holo:bye:abc]"),
            _w(43, "X [holo:err:something]"),
        ]
        ch = Channel(poll_interval=0.001, default_timeout=0.05)
        with pytest.raises(CalibrationError):
            ch.wait_for_calibration()

    def test_raises_on_timeout_with_no_beacon(self, fake_list_windows):
        fake_list_windows.return_value = [_w(1, "Just a page")]
        ch = Channel(poll_interval=0.001, default_timeout=0.05)
        with pytest.raises(CalibrationError, match="0.05"):
            ch.wait_for_calibration()

    def test_explicit_timeout_overrides_default(self, fake_list_windows):
        fake_list_windows.return_value = [_w(1, "Just a page")]
        ch = Channel(poll_interval=0.001, default_timeout=99)
        with pytest.raises(CalibrationError, match="0.02"):
            ch.wait_for_calibration(timeout=0.02)

    def test_uses_custom_browsers_set(self, fake_list_windows):
        fake_list_windows.return_value = [
            _w(42, "X [holo:cal:custom]", owner="WeirdBrowser"),
        ]
        ch = Channel(
            browsers=frozenset({"WeirdBrowser"}),
            poll_interval=0.001,
            default_timeout=0.5,
        )
        assert ch.wait_for_calibration() == "custom"


class TestSendCommand:
    def test_round_trip_returns_result(self, fake_list_windows, fake_paste):
        ch = Channel(poll_interval=0.001, default_timeout=2.0)
        ch.session = "sess-1"
        ch._window_id = 42

        # Initially the window shows just the calibration beacon.
        current_title = {"value": "Page [holo:cal:sess-1]"}
        fake_list_windows.side_effect = lambda: [_w(42, current_title["value"])]

        def respond(text):
            sent = framing.decode(text)
            current_title["value"] = _make_reply_title(
                frame_id=sent.id,
                session="sess-1",
                result={"pong": True},
            )

        fake_paste.side_effect = respond

        assert ch.send_command({"op": "ping"}) == {"pong": True}
        fake_paste.assert_called_once()

    def test_ignores_replies_with_mismatched_id(self, fake_list_windows, fake_paste):
        ch = Channel(poll_interval=0.001, default_timeout=0.1)
        ch.session = "sess"
        ch._window_id = 42
        # Pre-populate a stale reply for some other frame id; daemon must ignore.
        stale_title = _make_reply_title(
            frame_id="stale-id", session="sess", result={"pong": True}
        )
        fake_list_windows.return_value = [_w(42, stale_title)]
        with pytest.raises(CommandError):
            ch.send_command({"op": "ping"})

    def test_ignores_replies_with_mismatched_session(self, fake_list_windows, fake_paste):
        ch = Channel(poll_interval=0.001, default_timeout=0.1)
        ch.session = "sess-A"
        ch._window_id = 42
        # A reply from a different session — ignored.
        cur_title = {"value": "Page"}
        fake_list_windows.side_effect = lambda: [_w(42, cur_title["value"])]

        def respond(text):
            sent = framing.decode(text)
            cur_title["value"] = _make_reply_title(
                frame_id=sent.id,
                session="sess-B",  # wrong session
                result={"pong": True},
            )

        fake_paste.side_effect = respond
        with pytest.raises(CommandError):
            ch.send_command({"op": "ping"})

    def test_raises_if_not_calibrated(self):
        ch = Channel()
        with pytest.raises(RuntimeError, match="calibrated"):
            ch.send_command({"op": "ping"})

    def test_raises_if_window_disappears(self, fake_list_windows, fake_paste):
        ch = Channel(poll_interval=0.001, default_timeout=0.5)
        ch.session = "sess"
        ch._window_id = 42
        # Window list never contains id 42 → daemon should detect and raise.
        fake_list_windows.return_value = [_w(99, "Other")]
        with pytest.raises(CommandError, match="no longer present"):
            ch.send_command({"op": "ping"})

    def test_serializes_command_as_json_in_frame(self, fake_list_windows, fake_paste):
        ch = Channel(poll_interval=0.001, default_timeout=2.0)
        ch.session = "sess"
        ch._window_id = 42
        cur_title = {"value": "Page"}
        fake_list_windows.side_effect = lambda: [_w(42, cur_title["value"])]
        captured = {}

        def respond(text):
            sent = framing.decode(text)
            captured["frame"] = sent
            cur_title["value"] = _make_reply_title(
                frame_id=sent.id, session="sess", result={"ok": True}
            )

        fake_paste.side_effect = respond

        ch.send_command({"op": "read_global", "path": "R2D2_VERSION"})

        sent = captured["frame"]
        assert sent.session == "sess"
        assert sent.type == "cmd"
        assert json.loads(sent.data.decode("utf-8")) == {
            "op": "read_global",
            "path": "R2D2_VERSION",
        }


class TestActivation:
    """The daemon must activate the locked window's app before pasting,
    otherwise the synthesized Cmd+V lands in whatever app currently has
    keyboard focus (usually the terminal we're running from).
    """

    def test_activates_then_clicks_then_pastes(self, fake_list_windows, fake_paste):
        import sys

        if sys.platform != "darwin":
            pytest.skip("activation helper is darwin-only")
        ch = Channel(poll_interval=0.001, default_timeout=2.0)
        ch.session = "sess"
        ch._window_id = 42
        ch._window_pid = 1234
        cur_title = {"value": "Page"}
        # Bounds so _popup_body_click_point computes a click point.
        fake_list_windows.side_effect = lambda: [
            _w(42, cur_title["value"], pid=1234, bounds=(100.0, 50.0, 320.0, 160.0)),
        ]
        order = []

        def respond(text):
            order.append(("paste", text[:8]))
            sent = framing.decode(text)
            cur_title["value"] = _make_reply_title(
                frame_id=sent.id, session="sess", result={"pong": True}
            )

        fake_paste.side_effect = respond

        with (
            patch(
                "holo._macos.activate_pid",
                side_effect=lambda pid: order.append(("activate", pid)) or True,
            ),
            patch(
                "holo._macos.click_at",
                side_effect=lambda x, y: order.append(("click", x, y)),
            ),
            patch("holo.channel.ACTIVATE_SETTLE_S", 0.0),
            patch("holo.channel.CLICK_SETTLE_S", 0.0),
        ):
            ch.send_command({"op": "ping"})

        # Strict ordering: activate, then click into the body, then paste.
        assert order[0] == ("activate", 1234)
        assert order[1][0] == "click"
        # Click should be inside the popup body — left side, near bottom.
        # bounds = (100, 50, 320, 160) → expect (130, 180).
        assert order[1] == ("click", 130.0, 180.0)
        assert order[2][0] == "paste"

    def test_skips_activation_when_pid_unknown(self, fake_list_windows, fake_paste):
        ch = Channel(poll_interval=0.001, default_timeout=2.0)
        ch.session = "sess"
        ch._window_id = 42
        ch._window_pid = 0  # unknown (e.g. older calibration path)
        cur_title = {"value": "Page"}
        fake_list_windows.side_effect = lambda: [_w(42, cur_title["value"])]
        called = []

        def respond(text):
            sent = framing.decode(text)
            cur_title["value"] = _make_reply_title(
                frame_id=sent.id, session="sess", result={"pong": True}
            )

        fake_paste.side_effect = respond

        with patch("holo._macos.activate_pid", side_effect=lambda p: called.append(p)):
            ch.send_command({"op": "ping"})

        assert called == []

    def test_skips_click_when_bounds_unknown(self, fake_list_windows, fake_paste):
        import sys

        if sys.platform != "darwin":
            pytest.skip("activation helper is darwin-only")
        ch = Channel(poll_interval=0.001, default_timeout=2.0)
        ch.session = "sess"
        ch._window_id = 42
        ch._window_pid = 1234
        cur_title = {"value": "Page"}
        # No bounds — _popup_body_click_point should return None.
        fake_list_windows.side_effect = lambda: [
            _w(42, cur_title["value"], pid=1234, bounds=None),
        ]
        clicks = []

        def respond(text):
            sent = framing.decode(text)
            cur_title["value"] = _make_reply_title(
                frame_id=sent.id, session="sess", result={"pong": True}
            )

        fake_paste.side_effect = respond

        with (
            patch("holo._macos.activate_pid", return_value=True),
            patch("holo._macos.click_at", side_effect=lambda x, y: clicks.append((x, y))),
            patch("holo.channel.ACTIVATE_SETTLE_S", 0.0),
            patch("holo.channel.CLICK_SETTLE_S", 0.0),
        ):
            ch.send_command({"op": "ping"})

        # Activate fired, but no click because bounds were unavailable.
        assert clicks == []
