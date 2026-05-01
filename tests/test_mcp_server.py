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

    def scroll(self, x, y, *, direction="down", steps=3):
        self.calls.append(
            ("scroll", {"x": x, "y": y, "direction": direction, "steps": steps})
        )
        return {
            "scrolled": True,
            "x": x,
            "y": y,
            "direction": direction,
            "steps": steps,
        }

    def screenshot(self, *, region=None, timeout=15.0):
        self.calls.append(("screenshot", {"region": region}))
        return self.next_screenshot

    def find_image(self, needle, *, region=None, score=0.7, timeout=15.0):
        self.calls.append(("find_image", {"needle": needle, "region": region, "score": score}))
        return self.next_find

    def find_image_path(self, path, *, region=None, score=0.7, timeout=15.0):
        self.calls.append(
            ("find_image_path", {"path": str(path), "region": region, "score": score})
        )
        return self.next_find

    def user_capture(self, *, prompt="", timeout=60.0):
        self.calls.append(("user_capture", {"prompt": prompt, "timeout": timeout}))
        # Default: return cancelled. Tests override `next_capture` for success.
        return getattr(self, "next_capture", {"cancelled": True, "reason": "stub"})


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
                "screen_scroll",
                "screen_shot",
                "screen_find_image",
                "browser_navigate",
                "browser_new_tab",
                "browser_close_active_tab",
                "browser_activate_tab",
                "browser_list_tabs",
                "browser_read_active_url",
                "browser_read_active_title",
                "browser_reload",
                "browser_back",
                "browser_forward",
                "browser_execute_js",
                "bookmarklet_query",
                "ui_template_capture",
                "ui_template_list",
                "ui_template_find",
                "ui_template_click",
                "ui_template_delete",
            }
        finally:
            holo.shutdown()

    def test_no_bookmarklet_omits_channel_tools(self):
        """`--no-bookmarklet` mode drops the seven channel-dependent tools
        (calibrate / list_channels / drop_channel / ping / read_global /
        send_command / bookmarklet_query) but keeps screen, template, and
        AppleScript browser ops. Used by agents that never touch the
        bookmarklet — Slack / desktop orchestrators."""
        import asyncio

        mcp, holo = build_server(no_bookmarklet=True)
        try:
            tools = asyncio.run(mcp.list_tools())
            names = {t.name for t in tools}
            channel_tools = {
                "calibrate",
                "list_channels",
                "drop_channel",
                "ping",
                "read_global",
                "send_command",
                "bookmarklet_query",
            }
            assert names.isdisjoint(channel_tools), (
                f"channel tools should be omitted: {names & channel_tools}"
            )
            # Sanity: surfaces that don't depend on the channel still load.
            assert {
                "app_activate",
                "screen_click",
                "screen_scroll",
                "screen_shot",
                "browser_navigate",
                "browser_execute_js",
                "ui_template_capture",
                "ui_template_click",
            }.issubset(names)
        finally:
            holo.shutdown()

    def test_no_bookmarklet_skips_ws_server(self):
        """The Daemon should never spin up its WS server when the flag is
        on — the flag's whole point is to avoid binding a port that
        nothing will use."""
        from holo.daemon import Daemon

        d = Daemon(no_bookmarklet=True)
        try:
            assert d.ws_server is None
        finally:
            d.shutdown()  # must not raise even with ws_server=None

    def test_no_bookmarklet_calibrate_raises(self):
        """`Daemon.calibrate()` must raise rather than silently hanging
        in no-bookmarklet mode — there's no WS server to receive the
        beacon and no popup serving infrastructure."""
        from holo.daemon import Daemon

        d = Daemon(no_bookmarklet=True)
        try:
            with pytest.raises(RuntimeError, match="no-bookmarklet"):
                d.calibrate(timeout=0.1)
        finally:
            d.shutdown()


class TestScreenTools:
    """SikuliX-backed tools that don't take a sid — they drive whatever's
    in the foreground. Each delegates straight to `daemon.bridge`; the
    server is responsible only for the bridge-availability check and
    base64 marshalling."""

    @pytest.fixture
    def server_with_bridge(self):
        bridge = _StubBridge()
        server = HoloMCPServer(enable_screen=True)
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

    def test_screen_scroll_defaults_to_down_three_steps(self, server_with_bridge):
        server, bridge = server_with_bridge
        out = server.screen_scroll(100, 200)
        assert out["scrolled"] is True
        assert bridge.calls[-1] == (
            "scroll",
            {"x": 100, "y": 200, "direction": "down", "steps": 3},
        )

    def test_screen_scroll_passes_explicit_args(self, server_with_bridge):
        server, bridge = server_with_bridge
        server.screen_scroll(50, 60, direction="up", steps=10)
        assert bridge.calls[-1] == (
            "scroll",
            {"x": 50, "y": 60, "direction": "up", "steps": 10},
        )

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
        server = HoloMCPServer(enable_screen=False)
        fake = _FakeDaemon(bridge=None)
        server._daemon = fake
        with pytest.raises(RuntimeError, match="Screen tools unavailable"):
            server.screen_click(0, 0)


class TestBrowserTools:
    """The browser_* MCP tools wrap `holo.browser_chrome`. They don't
    touch the daemon or the bridge — they shell out to osascript. Here
    we verify the MCP layer's error translation; the AppleScript
    snippets and parsing are covered in `test_browser_chrome.py`.
    """

    def _server_no_daemon(self) -> HoloMCPServer:
        # Browser tools don't touch the daemon, but we don't want lazy
        # construction to start a real Daemon mid-test if something
        # accidentally pokes it.
        server = HoloMCPServer()
        server._daemon = _FakeDaemon()
        return server

    def test_browser_navigate_delegates(self):
        from unittest.mock import patch

        server = self._server_no_daemon()
        with patch("holo.browser_chrome.navigate") as nav:
            nav.return_value = {"url": "https://x/"}
            out = server.browser_navigate("https://x/")

        assert out == {"url": "https://x/"}
        nav.assert_called_once_with("https://x/")

    def test_browser_navigate_translates_browser_error_to_runtime_error(self):
        from unittest.mock import patch

        from holo.browser_chrome import BrowserError

        server = self._server_no_daemon()
        with patch("holo.browser_chrome.navigate", side_effect=BrowserError("boom")):
            with pytest.raises(RuntimeError, match="boom"):
                server.browser_navigate("https://x/")

    def test_browser_navigate_translates_not_available(self):
        from unittest.mock import patch

        from holo.browser_chrome import BrowserNotAvailable

        server = self._server_no_daemon()
        with patch(
            "holo.browser_chrome.navigate",
            side_effect=BrowserNotAvailable("linux"),
        ):
            with pytest.raises(RuntimeError, match="linux"):
                server.browser_navigate("https://x/")

    def test_browser_list_tabs_passthrough(self):
        from unittest.mock import patch

        server = self._server_no_daemon()
        payload = {
            "tabs": [{"id": 1, "title": "x", "url": "https://x/", "index": 1}],
            "active": 1,
        }
        with patch("holo.browser_chrome.list_tabs", return_value=payload):
            assert server.browser_list_tabs() == payload

    def test_browser_new_tab_with_and_without_url(self):
        from unittest.mock import patch

        server = self._server_no_daemon()
        with patch("holo.browser_chrome.new_tab") as new_tab:
            new_tab.return_value = {"url": "https://y/"}
            server.browser_new_tab("https://y/")
            new_tab.assert_called_once_with("https://y/")

            new_tab.reset_mock()
            new_tab.return_value = {"url": "chrome://newtab/"}
            server.browser_new_tab()
            new_tab.assert_called_once_with(None)

    def test_browser_activate_tab_passes_index(self):
        from unittest.mock import patch

        server = self._server_no_daemon()
        with patch("holo.browser_chrome.activate_tab") as act:
            act.return_value = {"index": 4}
            assert server.browser_activate_tab(4) == {"index": 4}
            act.assert_called_once_with(4)

    def test_browser_execute_js_delegates(self):
        from unittest.mock import patch

        server = self._server_no_daemon()
        with patch("holo.browser_chrome.execute_js") as exec_js:
            exec_js.return_value = {"result": "Click me"}
            out = server.browser_execute_js(
                "document.querySelector('button')?.innerText"
            )
        assert out == {"result": "Click me"}
        exec_js.assert_called_once_with(
            "document.querySelector('button')?.innerText"
        )

    def test_browser_execute_js_surfaces_authorization_message(self):
        """When Chrome's JS-from-AppleEvents is off, the agent should
        see a message that names the menu item AND points at
        bookmarklet_query as the fallback."""
        from unittest.mock import patch

        from holo.browser_chrome import JavaScriptNotAuthorized

        server = self._server_no_daemon()
        with patch(
            "holo.browser_chrome.execute_js",
            side_effect=JavaScriptNotAuthorized(
                "Chrome's 'Allow JavaScript from Apple Events' is off..."
            ),
        ):
            with pytest.raises(RuntimeError, match="Allow JavaScript from Apple Events"):
                server.browser_execute_js("document.title")


class TestBookmarkletQuery:
    """`bookmarklet_query` rides on the existing channel send_command
    path — we just verify it builds the right command shape."""

    @pytest.fixture
    def server_with_channel(self):
        server = HoloMCPServer()
        fake = _FakeDaemon()
        server._daemon = fake
        ch = _StubChannel("sid-A", replies=[{"value": "Click me"}])
        fake.registry.register("sid-A", ch)
        return server, ch

    def test_default_uses_query_selector_and_innerText(self, server_with_channel):
        server, ch = server_with_channel
        out = server.bookmarklet_query("sid-A", "button")
        assert out["result"] == {"value": "Click me"}
        cmd, _timeout = ch.calls[-1]
        assert cmd == {"op": "query_selector", "selector": "button", "prop": "innerText"}

    def test_all_flag_switches_to_query_selector_all(self, server_with_channel):
        server, ch = server_with_channel
        # The stub returns the canned reply regardless of op.
        server.bookmarklet_query("sid-A", "button", all=True)
        cmd, _ = ch.calls[-1]
        assert cmd["op"] == "query_selector_all"

    def test_attr_takes_precedence_over_prop(self, server_with_channel):
        server, ch = server_with_channel
        server.bookmarklet_query("sid-A", "a", prop="innerText", attr="href")
        cmd, _ = ch.calls[-1]
        assert "attr" in cmd and cmd["attr"] == "href"
        assert "prop" not in cmd

    def test_custom_prop(self, server_with_channel):
        server, ch = server_with_channel
        server.bookmarklet_query("sid-A", "h1", prop="innerHTML")
        cmd, _ = ch.calls[-1]
        assert cmd["prop"] == "innerHTML"

    def test_rejects_empty_selector(self, server_with_channel):
        server, _ = server_with_channel
        with pytest.raises(ValueError, match="selector"):
            server.bookmarklet_query("sid-A", "")

    def test_unknown_sid_raises(self, server_with_channel):
        server, _ = server_with_channel
        with pytest.raises(ValueError, match="no channel"):
            server.bookmarklet_query("nope", "button")


class TestUiTemplates:
    """Template cache MCP tools — capture / list / find / click / delete.

    The TemplateStore itself is exercised exhaustively in test_templates.py;
    here we just verify the MCP layer routes correctly to the store and
    bridge, and that find/click integrate them properly.
    """

    @pytest.fixture
    def fixtures(self, tmp_path):
        """Server wired to a stubbed bridge + a tmp-dir TemplateStore."""
        from holo.templates import TemplateStore

        store = TemplateStore(root=tmp_path / "templates")
        bridge = _StubBridge()
        # 24x24 PNG that the bridge "captured" and that find_image_path "matches".
        png = self._make_png(24, 24)
        bridge.next_screenshot = png
        bridge.next_capture = {
            "image": __import__("base64").b64encode(png).decode(),
            "x": 100, "y": 200, "width": 24, "height": 24,
        }
        bridge.next_find = {
            "x": 100, "y": 200, "width": 24, "height": 24, "score": 0.91,
        }
        server = HoloMCPServer(templates=store)
        server._daemon = _FakeDaemon(bridge=bridge)
        return server, bridge, store, png

    @staticmethod
    def _make_png(w, h):
        import struct
        import zlib

        sig = b"\x89PNG\r\n\x1a\n"

        def chunk(typ, data):
            crc = zlib.crc32(typ + data) & 0xFFFFFFFF
            return struct.pack(">I", len(data)) + typ + data + struct.pack(">I", crc)

        ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
        raw = b"".join(b"\x00" + b"\x00\x00\x00" * w for _ in range(h))
        idat = zlib.compress(raw)
        return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")

    # ---- capture --------------------------------------------------------

    def test_capture_with_region_uses_screenshot(self, fixtures):
        server, bridge, store, _ = fixtures
        out = server.ui_template_capture(
            "kebab", app="chrome",
            region={"x": 0, "y": 0, "width": 24, "height": 24},
        )
        # The bridge was asked for a screenshot, not a userCapture.
        assert any(c[0] == "screenshot" for c in bridge.calls)
        assert not any(c[0] == "user_capture" for c in bridge.calls)
        assert out["saved"] is True
        assert out["entry"]["app"] == "chrome"
        assert out["entry"]["label"] == "kebab"
        assert store.get("kebab", "chrome") is not None

    def test_capture_without_region_calls_user_capture(self, fixtures):
        server, bridge, store, _ = fixtures
        out = server.ui_template_capture("kebab", app="chrome")
        assert any(c[0] == "user_capture" for c in bridge.calls)
        assert out["saved"] is True
        assert store.get("kebab", "chrome") is not None

    def test_capture_cancelled_returns_cancelled_marker_no_save(self, fixtures):
        server, bridge, store, _ = fixtures
        bridge.next_capture = {"cancelled": True, "reason": "user cancelled"}
        out = server.ui_template_capture("kebab", app="chrome")
        assert out == {"cancelled": True, "reason": "user cancelled"}
        # Nothing was written to the cache.
        assert store.get("kebab", "chrome") is None

    def test_capture_propagates_replace_and_similarity(self, fixtures):
        server, _, store, _ = fixtures
        server.ui_template_capture(
            "kebab", app="chrome",
            region={"x": 0, "y": 0, "width": 24, "height": 24},
        )
        server.ui_template_capture(
            "kebab", app="chrome",
            region={"x": 0, "y": 0, "width": 24, "height": 24},
            replace=True,
            similarity=0.95,
        )
        entry = store.get("kebab", "chrome")
        assert entry["variants"] == ["kebab.png"]
        assert entry["similarity"] == 0.95

    # ---- list -----------------------------------------------------------

    def test_list_filters_by_app(self, fixtures):
        server, _, store, png = fixtures
        store.add_variant("a", "chrome", png)
        store.add_variant("b", "slack", png)
        out = server.ui_template_list(app="chrome")
        assert [e["label"] for e in out["templates"]] == ["a"]

    def test_list_all(self, fixtures):
        server, _, store, png = fixtures
        store.add_variant("a", "chrome", png)
        store.add_variant("b", None, png)
        out = server.ui_template_list()
        assert len(out["templates"]) == 2

    # ---- find -----------------------------------------------------------

    def test_find_returns_match_with_variant_name(self, fixtures):
        server, bridge, store, png = fixtures
        store.add_variant("kebab", "chrome", png, similarity=0.9)
        out = server.ui_template_find("kebab", app="chrome")
        assert out["score"] == 0.91
        assert out["variant"] == "kebab.png"
        assert out["x"] == 100
        # Bridge was asked with the entry's similarity, not the default.
        path_calls = [c for c in bridge.calls if c[0] == "find_image_path"]
        assert path_calls and path_calls[-1][1]["score"] == 0.9

    def test_find_walks_variants_in_order(self, fixtures):
        server, bridge, store, png = fixtures
        store.add_variant("kebab", "chrome", png)
        store.add_variant("kebab", "chrome", png)  # _2
        # First variant misses, second hits.
        responses = [None, {
            "x": 1, "y": 2, "width": 24, "height": 24, "score": 0.88,
        }]

        def stepwise_find(path, *, region=None, score=0.7, timeout=15.0):
            bridge.calls.append(("find_image_path", {"path": str(path)}))
            return responses.pop(0)

        bridge.find_image_path = stepwise_find  # type: ignore[assignment]
        out = server.ui_template_find("kebab", app="chrome")
        assert out["variant"] == "kebab_2.png"

    def test_find_returns_null_when_no_variant_matches(self, fixtures):
        server, bridge, store, png = fixtures
        store.add_variant("kebab", "chrome", png)
        bridge.next_find = None
        assert server.ui_template_find("kebab", app="chrome") is None

    def test_find_raises_lookup_error_for_missing_label(self, fixtures):
        server, _, _, _ = fixtures
        with pytest.raises(LookupError, match="kebab"):
            server.ui_template_find("kebab", app="chrome")

    def test_find_bumps_last_used_and_match_count(self, fixtures):
        server, _, store, png = fixtures
        store.add_variant("kebab", "chrome", png)
        server.ui_template_find("kebab", app="chrome")
        entry = store.get("kebab", "chrome")
        assert entry["match_count"] == 1
        assert entry["last_used"] is not None

    # ---- click ----------------------------------------------------------

    def test_click_finds_and_clicks_center(self, fixtures):
        server, bridge, store, png = fixtures
        store.add_variant("kebab", "chrome", png)
        out = server.ui_template_click("kebab", app="chrome")
        # Center of the 100,200 / 24x24 match is (112, 212).
        assert out == {
            "clicked": True, "x": 112, "y": 212, "score": 0.91, "variant": "kebab.png"
        }
        assert ("click", {"x": 112, "y": 212, "modifiers": []}) in bridge.calls

    def test_click_raises_when_template_doesnt_match(self, fixtures):
        server, bridge, store, png = fixtures
        store.add_variant("kebab", "chrome", png)
        bridge.next_find = None
        with pytest.raises(RuntimeError, match="matched nothing"):
            server.ui_template_click("kebab", app="chrome")
        # No click was issued.
        assert not any(c[0] == "click" for c in bridge.calls)

    def test_click_raises_for_missing_label(self, fixtures):
        server, _, _, _ = fixtures
        with pytest.raises(LookupError, match="kebab"):
            server.ui_template_click("kebab", app="chrome")

    # ---- delete ---------------------------------------------------------

    def test_delete_removes_entry(self, fixtures):
        server, _, store, png = fixtures
        store.add_variant("kebab", "chrome", png)
        out = server.ui_template_delete("kebab", app="chrome")
        assert "kebab.png" in out["removed"]
        assert store.get("kebab", "chrome") is None

    def test_delete_one_variant_keeps_entry(self, fixtures):
        server, _, store, png = fixtures
        store.add_variant("kebab", "chrome", png)
        store.add_variant("kebab", "chrome", png)
        out = server.ui_template_delete("kebab", app="chrome", variant="kebab.png")
        assert out["removed"] == ["kebab.png"]
        entry = store.get("kebab", "chrome")
        assert entry["variants"] == ["kebab_2.png"]
