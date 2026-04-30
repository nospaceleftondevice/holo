"""Unit tests for holo.mcp_server.

The tool handlers are exercised against a fake `Daemon` that owns a
real `ChannelRegistry` plus stub Channel objects — no WS server, no
clipboard, no browser. Goal is to lock the MCP-side surface (arg
shapes, error mapping, transport reporting) while reusing the real
Channel/Daemon paths in their own test files.
"""

from __future__ import annotations

from typing import Any

import pytest

from holo.channel import CalibrationError, CommandError
from holo.mcp_server import HoloMCPServer, build_server
from holo.registry import ChannelRegistry


class _StubChannel:
    """Stand-in for `holo.channel.Channel` with just the bits MCP reads."""

    def __init__(
        self,
        sid: str,
        *,
        window_id: int = 42,
        window_owner: str = "Google Chrome",
        ws_ready: bool = False,
        replies: list[Any] | None = None,
    ) -> None:
        self.session = sid
        self._window_id = window_id
        self._window_owner = window_owner
        self._ws_ready = ws_ready
        # Either a list of pre-baked replies or exceptions (popped FIFO),
        # or None to default to {"pong": True} for every call.
        self._replies = replies
        self.calls: list[tuple[dict[str, Any], float | None]] = []

    def send_command(
        self, cmd: dict[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        self.calls.append((cmd, timeout))
        if self._replies is None:
            return {"pong": True}
        item = self._replies.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class _StubBridge:
    """Stand-in for the SikuliX bridge — records calls, returns canned data."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.next_screenshot: bytes = b"\x89PNG-stub"
        self.next_find: dict | None = {
            "x": 50,
            "y": 60,
            "width": 30,
            "height": 30,
            "score": 0.95,
        }

    def activate(self, name):
        self.calls.append(("activate", {"name": name}))
        return {"focused": True, "name": name}

    def click(self, x, y, *, modifiers=None):
        self.calls.append(("click", {"x": x, "y": y, "modifiers": modifiers or []}))
        return {"clicked": True, "x": x, "y": y}

    def key(self, combo):
        self.calls.append(("key", {"combo": combo}))
        return {"sent": combo}

    def type_text(self, text):
        self.calls.append(("type_text", {"text": text}))
        return {"typed_chars": len(text)}

    def screenshot(self, *, region=None, timeout=15.0):
        self.calls.append(("screenshot", {"region": region}))
        return self.next_screenshot

    def find_image(self, needle, *, region=None, score=0.7, timeout=15.0):
        self.calls.append(("find_image", {"needle": needle, "region": region, "score": score}))
        return self.next_find


class _FakeDaemon:
    def __init__(self, *, bridge: _StubBridge | None = None) -> None:
        self.registry = ChannelRegistry()
        self.shutdown_called = False
        self.next_calibrations: list[Any] = []
        self.bridge = bridge

    def calibrate(self, *, timeout: float | None = None) -> _StubChannel:
        if not self.next_calibrations:
            raise AssertionError("test did not queue a calibration result")
        item = self.next_calibrations.pop(0)
        if isinstance(item, BaseException):
            raise item
        self.registry.register(item.session, item)
        return item

    def shutdown(self) -> None:
        self.shutdown_called = True


@pytest.fixture
def server_with_fake() -> tuple[HoloMCPServer, _FakeDaemon]:
    server = HoloMCPServer()
    fake = _FakeDaemon()
    server._daemon = fake  # bypass lazy init — never construct a real Daemon in tests
    return server, fake


class TestCalibrate:
    def test_returns_channel_descriptor(self, server_with_fake):
        server, fake = server_with_fake
        fake.next_calibrations.append(_StubChannel("sid-A", window_id=7))

        result = server.calibrate(timeout=1.0)

        assert result == {
            "sid": "sid-A",
            "window_id": 7,
            "window_owner": "Google Chrome",
            "transport": "qr",
        }

    def test_timeout_becomes_runtime_error(self, server_with_fake):
        server, fake = server_with_fake
        fake.next_calibrations.append(CalibrationError("no beacon within 1s"))

        with pytest.raises(RuntimeError, match="calibration timeout"):
            server.calibrate(timeout=1.0)

    def test_fast_path_returns_existing_channel_without_blocking(
        self, server_with_fake
    ):
        """When the registry is non-empty, calibrate returns the most
        recent channel immediately. Cross-host setups depend on this:
        the human calibrates locally on the daemon's machine, then a
        remote agent that connects in shouldn't have to re-trigger the
        bookmarklet — list/use the existing channel.
        """
        server, fake = server_with_fake
        existing = _StubChannel("sid-existing", window_id=99, ws_ready=True)
        fake.registry.register(existing.session, existing)

        # No queued calibrations: if the fast path were missing, the
        # fake's `calibrate()` would assert.
        result = server.calibrate(timeout=1.0)

        assert result == {
            "sid": "sid-existing",
            "window_id": 99,
            "window_owner": "Google Chrome",
            "transport": "ws",
        }

    def test_fast_path_picks_most_recent_channel(self, server_with_fake):
        server, fake = server_with_fake
        fake.registry.register("sid-old", _StubChannel("sid-old"))
        fake.registry.register(
            "sid-new", _StubChannel("sid-new", window_id=42, ws_ready=True)
        )

        result = server.calibrate()

        assert result["sid"] == "sid-new"


class TestListAndDrop:
    def test_list_channels_snapshots_registry(self, server_with_fake):
        server, fake = server_with_fake
        fake.registry.register("sid-A", _StubChannel("sid-A", window_id=1))
        fake.registry.register(
            "sid-B", _StubChannel("sid-B", window_id=2, ws_ready=True)
        )

        listed = server.list_channels()

        sids = sorted(ch["sid"] for ch in listed["channels"])
        assert sids == ["sid-A", "sid-B"]
        by_sid = {ch["sid"]: ch for ch in listed["channels"]}
        assert by_sid["sid-A"]["transport"] == "qr"
        assert by_sid["sid-B"]["transport"] == "ws"

    def test_drop_channel_removes_from_registry(self, server_with_fake):
        server, fake = server_with_fake
        fake.next_calibrations.append(_StubChannel("sid-A"))
        server.calibrate()

        result = server.drop_channel("sid-A")

        assert result == {"ok": True, "sid": "sid-A"}
        assert fake.registry.lookup("sid-A") is None

    def test_drop_unknown_sid_raises(self, server_with_fake):
        server, _ = server_with_fake
        with pytest.raises(ValueError, match="no channel for sid"):
            server.drop_channel("nope")


class TestSendCommand:
    def test_round_trip_returns_result_and_transport(self, server_with_fake):
        server, fake = server_with_fake
        ch = _StubChannel("sid-A", ws_ready=True, replies=[{"value": "Hello"}])
        fake.next_calibrations.append(ch)
        server.calibrate()

        out = server.send_command(
            "sid-A", {"op": "read_global", "path": "document.title"}, timeout=2.0
        )

        assert out == {
            "sid": "sid-A",
            "transport": "ws",
            "result": {"value": "Hello"},
        }
        assert ch.calls == [
            ({"op": "read_global", "path": "document.title"}, 2.0)
        ]

    def test_unknown_sid_raises_value_error(self, server_with_fake):
        server, _ = server_with_fake
        with pytest.raises(ValueError, match="no channel for sid"):
            server.send_command("missing", {"op": "ping"})

    def test_command_must_be_dict(self, server_with_fake):
        server, fake = server_with_fake
        fake.next_calibrations.append(_StubChannel("sid-A"))
        server.calibrate()
        with pytest.raises(ValueError, match="must be an object"):
            server.send_command("sid-A", "ping")  # type: ignore[arg-type]

    def test_command_requires_op_string(self, server_with_fake):
        server, fake = server_with_fake
        fake.next_calibrations.append(_StubChannel("sid-A"))
        server.calibrate()
        with pytest.raises(ValueError, match="op"):
            server.send_command("sid-A", {"path": "x"})
        with pytest.raises(ValueError, match="op"):
            server.send_command("sid-A", {"op": ""})

    def test_command_error_becomes_runtime_error(self, server_with_fake):
        server, fake = server_with_fake
        ch = _StubChannel(
            "sid-A", replies=[CommandError("no reply for cmd within 1s")]
        )
        fake.next_calibrations.append(ch)
        server.calibrate()

        with pytest.raises(RuntimeError, match="command failed"):
            server.send_command("sid-A", {"op": "ping"}, timeout=1.0)


class TestPingAndReadGlobal:
    def test_ping_delegates_to_send_command(self, server_with_fake):
        server, fake = server_with_fake
        ch = _StubChannel("sid-A", replies=[{"pong": True}])
        fake.next_calibrations.append(ch)
        server.calibrate()

        out = server.ping("sid-A", timeout=3.0)

        assert out["result"] == {"pong": True}
        assert ch.calls == [({"op": "ping"}, 3.0)]

    def test_read_global_passes_path(self, server_with_fake):
        server, fake = server_with_fake
        ch = _StubChannel("sid-A", replies=[{"value": 7}])
        fake.next_calibrations.append(ch)
        server.calibrate()

        out = server.read_global("sid-A", "R2D2_VERSION", timeout=4.0)

        assert out["result"] == {"value": 7}
        assert ch.calls == [
            ({"op": "read_global", "path": "R2D2_VERSION"}, 4.0)
        ]

    def test_read_global_rejects_empty_path(self, server_with_fake):
        server, fake = server_with_fake
        fake.next_calibrations.append(_StubChannel("sid-A"))
        server.calibrate()
        with pytest.raises(ValueError, match="path"):
            server.read_global("sid-A", "")


class TestShutdownAndBuild:
    def test_shutdown_propagates_to_daemon_and_clears(self, server_with_fake):
        server, fake = server_with_fake
        # Touch the daemon so it is materialised, then shut down.
        _ = server.daemon
        server.shutdown()
        assert fake.shutdown_called is True
        assert server._daemon is None

    def test_shutdown_no_op_when_daemon_never_created(self):
        server = HoloMCPServer()
        server.shutdown()  # must not raise

    def test_build_server_registers_expected_tools(self):
        mcp, holo = build_server()
        try:
            # FastMCP exposes registered tools via the async list_tools() API.
            import asyncio

            tools = asyncio.run(mcp.list_tools())
            names = {t.name for t in tools}
            assert names == {
                "calibrate",
                "list_channels",
                "drop_channel",
                "ping",
                "read_global",
                "send_command",
                "app_activate",
                "screen_click",
                "screen_type",
                "screen_key",
                "screen_shot",
                "screen_find_image",
            }
        finally:
            holo.shutdown()


class TestScreenTools:
    """SikuliX-backed tools that don't take a sid — they drive whatever's
    in the foreground. Each delegates straight to `daemon.bridge`; the
    server is responsible only for the bridge-availability check and
    base64 marshalling."""

    @pytest.fixture
    def server_with_bridge(self):
        bridge = _StubBridge()
        server = HoloMCPServer(use_bridge=True)
        fake = _FakeDaemon(bridge=bridge)
        server._daemon = fake  # bypass lazy init
        return server, bridge

    def test_app_activate_delegates(self, server_with_bridge):
        server, bridge = server_with_bridge
        out = server.app_activate("Google Chrome")
        assert out == {"focused": True, "name": "Google Chrome"}
        assert bridge.calls == [("activate", {"name": "Google Chrome"})]

    def test_screen_click_passes_modifiers(self, server_with_bridge):
        server, bridge = server_with_bridge
        server.screen_click(100, 200, modifiers=["cmd"])
        assert bridge.calls == [
            ("click", {"x": 100, "y": 200, "modifiers": ["cmd"]})
        ]

    def test_screen_type_and_key(self, server_with_bridge):
        server, bridge = server_with_bridge
        server.screen_type("hello")
        server.screen_key("cmd+v")
        assert bridge.calls == [
            ("type_text", {"text": "hello"}),
            ("key", {"combo": "cmd+v"}),
        ]

    def test_screen_shot_returns_base64_payload(self, server_with_bridge):
        import base64

        server, bridge = server_with_bridge
        bridge.next_screenshot = b"PNG-bytes-here"
        out = server.screen_shot()
        assert out["format"] == "png"
        assert out["byte_count"] == len(b"PNG-bytes-here")
        assert base64.b64decode(out["image"]) == b"PNG-bytes-here"
        assert bridge.calls == [("screenshot", {"region": None})]

    def test_screen_shot_passes_region(self, server_with_bridge):
        server, bridge = server_with_bridge
        region = {"x": 10, "y": 20, "width": 30, "height": 40}
        server.screen_shot(region=region)
        assert bridge.calls[-1] == ("screenshot", {"region": region})

    def test_screen_find_image_decodes_needle(self, server_with_bridge):
        import base64

        server, bridge = server_with_bridge
        needle_bytes = b"\x89PNG-needle-bytes"
        out = server.screen_find_image(
            base64.b64encode(needle_bytes).decode("ascii"), score=0.9
        )
        assert out == {"x": 50, "y": 60, "width": 30, "height": 30, "score": 0.95}
        assert bridge.calls[-1][0] == "find_image"
        params = bridge.calls[-1][1]
        assert params["needle"] == needle_bytes
        assert params["score"] == 0.9
        assert params["region"] is None

    def test_screen_find_image_returns_none_for_no_match(self, server_with_bridge):
        import base64

        server, bridge = server_with_bridge
        bridge.next_find = None
        out = server.screen_find_image(base64.b64encode(b"x").decode())
        assert out is None

    def test_screen_find_image_rejects_bad_base64(self, server_with_bridge):
        server, _ = server_with_bridge
        with pytest.raises(ValueError, match="base64"):
            server.screen_find_image("!!!not-base64!!!")

    def test_no_bridge_raises_clean_error(self):
        server = HoloMCPServer(use_bridge=False)
        fake = _FakeDaemon(bridge=None)
        server._daemon = fake
        with pytest.raises(RuntimeError, match="SikuliX bridge unavailable"):
            server.screen_click(0, 0)
