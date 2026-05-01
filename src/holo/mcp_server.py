"""MCP server surface for the holo daemon.

Exposes the `Daemon` / `Channel` primitives as MCP tools so an AI
agent (Claude Code, Codex, Cursor, …) can drive the user's already-
signed-in browser tab through the bookmarklet channel.

This module is the thin in-process variant: each MCP server instance
owns one `Daemon`, started lazily on the first tool call that needs
it. The agent calibrates one or more tabs (`calibrate`), then issues
commands against them by sid (`ping`, `read_global`, `send_command`).
The set of available bookmarklet ops is whatever `bookmarklet/
dispatch.js` knows about today; `send_command` is the forward-compat
escape hatch for ops we add later.

Run via `holo mcp` (stdio transport) or import `build_server()` to
embed in another runner.
"""

from __future__ import annotations

import socket
import sys
import threading
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.stdio import stdio_server

from holo.channel import CalibrationError, Channel, CommandError
from holo.daemon import Daemon
from holo.mcp_wire import WIRE_MAGIC, is_valid_handshake, read_handshake


class HoloMCPServer:
    """Holds the lazy Daemon and the tool implementations.

    Splitting the tool bodies off the FastMCP decorator lets tests
    drive them directly without spinning up a stdio loop, and gives
    us one place to attach a fake daemon in tests.
    """

    def __init__(self, *, hide_qr: bool = False, use_bridge: bool = False) -> None:
        self.hide_qr = hide_qr
        self.use_bridge = use_bridge
        self._daemon: Daemon | None = None
        self._daemon_lock = threading.Lock()

    @property
    def daemon(self) -> Daemon:
        with self._daemon_lock:
            if self._daemon is None:
                self._daemon = Daemon(hide_qr=self.hide_qr, use_bridge=self.use_bridge)
            return self._daemon

    def shutdown(self) -> None:
        with self._daemon_lock:
            if self._daemon is not None:
                self._daemon.shutdown()
                self._daemon = None

    # ---- tool implementations ---------------------------------------

    def calibrate(self, timeout: float = 30.0) -> dict[str, Any]:
        """Return the most recently registered channel if one exists,
        otherwise wait for a fresh calibration beacon.

        The fast-path matters for cross-host setups: the human
        calibrates locally on the daemon's machine (where the browser
        is), then a remote agent connecting in shouldn't have to
        re-trigger the bookmarklet — it can just keep working with
        whatever's already registered.
        """
        existing = self.daemon.registry.items()
        if existing:
            _, ch = existing[-1]
            return _describe(ch)
        try:
            ch = self.daemon.calibrate(timeout=timeout)
        except CalibrationError as e:
            raise RuntimeError(f"calibration timeout: {e}") from e
        return _describe(ch)

    def list_channels(self) -> dict[str, Any]:
        """Snapshot of currently registered channels."""
        return {"channels": [_describe(ch) for _, ch in self.daemon.registry.items()]}

    def drop_channel(self, sid: str) -> dict[str, Any]:
        """Forget the channel for `sid`. Does not close the browser popup."""
        ch = self.daemon.registry.unregister(sid)
        if ch is None:
            raise ValueError(f"no channel for sid {sid!r}")
        return {"ok": True, "sid": sid}

    def ping(self, sid: str, timeout: float = 5.0) -> dict[str, Any]:
        """Round-trip a ping to confirm the channel is live."""
        return self.send_command(sid, {"op": "ping"}, timeout=timeout)

    def read_global(
        self, sid: str, path: str, timeout: float = 5.0
    ) -> dict[str, Any]:
        """Read a dotted path off the page's global object."""
        if not path:
            raise ValueError("path must be non-empty")
        return self.send_command(
            sid, {"op": "read_global", "path": path}, timeout=timeout
        )

    def send_command(
        self, sid: str, command: dict[str, Any], timeout: float = 5.0
    ) -> dict[str, Any]:
        """Send an arbitrary command to the bookmarklet for `sid`.

        `command` must be a JSON-serialisable dict with an `op` field.
        """
        if not isinstance(command, dict):
            raise ValueError("command must be an object")
        if not isinstance(command.get("op"), str) or not command["op"]:
            raise ValueError("command must have a non-empty string `op` field")
        ch = self._require_channel(sid)
        try:
            result = ch.send_command(command, timeout=timeout)
        except CommandError as e:
            raise RuntimeError(f"command failed: {e}") from e
        return {
            "sid": sid,
            "transport": _transport(ch),
            "result": result,
        }

    def _require_channel(self, sid: str) -> Channel:
        ch = self.daemon.registry.lookup(sid)
        if ch is None:
            raise ValueError(f"no channel for sid {sid!r}")
        return ch

    # ---- screen / SikuliX tools (no sid; drives whatever's foreground) ----

    def _require_bridge(self) -> Any:
        """Return the daemon's SikuliX bridge or raise a clean error."""
        bridge = self.daemon.bridge
        if bridge is None:
            raise RuntimeError(
                "SikuliX bridge unavailable. Start the daemon with "
                "`use_bridge=True` (CLI: `holo mcp --bridge`) and ensure "
                "OpenJDK 11+ + sikulix*.jar are installed."
            )
        return bridge

    def app_activate(self, name: str) -> dict[str, Any]:
        """Bring an application to the foreground by name."""
        return self._require_bridge().activate(name)

    def screen_click(
        self, x: int, y: int, modifiers: list[str] | None = None
    ) -> dict[str, Any]:
        """Click at screen coordinates, optionally holding modifiers."""
        return self._require_bridge().click(x, y, modifiers=modifiers or [])

    def screen_type(self, text: str) -> dict[str, Any]:
        """Type a literal string into whatever has keyboard focus."""
        return self._require_bridge().type_text(text)

    def screen_key(self, combo: str) -> dict[str, Any]:
        """Send a key combo, e.g. 'cmd+v', 'enter', 'shift+tab'."""
        return self._require_bridge().key(combo)

    def screen_shot(
        self, region: dict[str, int] | None = None
    ) -> dict[str, Any]:
        """Capture the screen (or a region) and return base64 PNG + size."""
        import base64 as _b64

        png = self._require_bridge().screenshot(region=region)
        return {
            "image": _b64.b64encode(png).decode("ascii"),
            "format": "png",
            "byte_count": len(png),
        }

    def screen_find_image(
        self,
        needle: str,
        region: dict[str, int] | None = None,
        score: float = 0.7,
    ) -> dict[str, Any] | None:
        """Find `needle` (base64-encoded PNG) on screen. Returns coords or null."""
        import base64 as _b64

        try:
            needle_bytes = _b64.b64decode(needle, validate=True)
        except Exception as e:
            raise ValueError("needle must be base64-encoded PNG") from e
        return self._require_bridge().find_image(
            needle_bytes, region=region, score=score
        )

    # ---- Chrome browser tools (AppleScript; macOS-only) -----------------
    #
    # These bypass the SikuliX keystroke layer entirely — Chrome's
    # AppleScript dictionary is synchronous and reliable, no focus
    # races, no beeps. Use these instead of `app_activate` +
    # `screen_key cmd+l` + `screen_type` + `screen_key enter` for any
    # browser navigation. The bookmarklet channel is still the right
    # tool for in-page DOM reads.

    def browser_navigate(self, url: str) -> dict[str, Any]:
        """Set the active tab's URL (Chrome, front window)."""
        from holo import browser_chrome

        try:
            return browser_chrome.navigate(url)
        except (browser_chrome.BrowserError, browser_chrome.BrowserNotAvailable) as e:
            raise RuntimeError(str(e)) from e

    def browser_new_tab(self, url: str | None = None) -> dict[str, Any]:
        """Open a new tab in Chrome's front window. URL is optional."""
        from holo import browser_chrome

        try:
            return browser_chrome.new_tab(url)
        except (browser_chrome.BrowserError, browser_chrome.BrowserNotAvailable) as e:
            raise RuntimeError(str(e)) from e

    def browser_close_active_tab(self) -> dict[str, Any]:
        """Close the active tab of Chrome's front window."""
        from holo import browser_chrome

        try:
            return browser_chrome.close_active_tab()
        except (browser_chrome.BrowserError, browser_chrome.BrowserNotAvailable) as e:
            raise RuntimeError(str(e)) from e

    def browser_activate_tab(self, index: int) -> dict[str, Any]:
        """Make tab `index` (1-based) the active tab of the front window."""
        from holo import browser_chrome

        try:
            return browser_chrome.activate_tab(index)
        except (browser_chrome.BrowserError, browser_chrome.BrowserNotAvailable) as e:
            raise RuntimeError(str(e)) from e

    def browser_list_tabs(self) -> dict[str, Any]:
        """List tabs in the front window: `{tabs: [{id,title,url,index}], active: index}`."""
        from holo import browser_chrome

        try:
            return browser_chrome.list_tabs()
        except (browser_chrome.BrowserError, browser_chrome.BrowserNotAvailable) as e:
            raise RuntimeError(str(e)) from e

    def browser_read_active_url(self) -> dict[str, Any]:
        from holo import browser_chrome

        try:
            return browser_chrome.read_active_url()
        except (browser_chrome.BrowserError, browser_chrome.BrowserNotAvailable) as e:
            raise RuntimeError(str(e)) from e

    def browser_read_active_title(self) -> dict[str, Any]:
        from holo import browser_chrome

        try:
            return browser_chrome.read_active_title()
        except (browser_chrome.BrowserError, browser_chrome.BrowserNotAvailable) as e:
            raise RuntimeError(str(e)) from e

    def browser_reload(self) -> dict[str, Any]:
        from holo import browser_chrome

        try:
            return browser_chrome.reload()
        except (browser_chrome.BrowserError, browser_chrome.BrowserNotAvailable) as e:
            raise RuntimeError(str(e)) from e

    def browser_back(self) -> dict[str, Any]:
        from holo import browser_chrome

        try:
            return browser_chrome.go_back()
        except (browser_chrome.BrowserError, browser_chrome.BrowserNotAvailable) as e:
            raise RuntimeError(str(e)) from e

    def browser_forward(self) -> dict[str, Any]:
        from holo import browser_chrome

        try:
            return browser_chrome.go_forward()
        except (browser_chrome.BrowserError, browser_chrome.BrowserNotAvailable) as e:
            raise RuntimeError(str(e)) from e

    def browser_execute_js(self, js: str) -> dict[str, Any]:
        """Run a JS expression in Chrome's active tab via AppleScript.

        Requires Chrome's 'Allow JavaScript from Apple Events' toggle
        (View → Developer). When that's off, raises a runtime error
        whose message tells the caller exactly how to enable it OR to
        fall back to `bookmarklet_query` against a calibrated channel.
        """
        from holo import browser_chrome

        try:
            return browser_chrome.execute_js(js)
        except browser_chrome.JavaScriptNotAuthorized as e:
            # Specific message; the agent can read this and route to
            # `bookmarklet_query` if a channel is available.
            raise RuntimeError(str(e)) from e
        except (browser_chrome.BrowserError, browser_chrome.BrowserNotAvailable) as e:
            raise RuntimeError(str(e)) from e

    def bookmarklet_query(
        self,
        sid: str,
        selector: str,
        prop: str = "innerText",
        attr: str | None = None,
        all: bool = False,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        """DOM query through the bookmarklet channel — CSP-safe
        fallback when `browser_execute_js` is unavailable.

        - `selector` is a CSS selector
        - `prop` is the JS property to read (default 'innerText'); ignored when `attr` is set
        - `attr` is an HTML attribute name; takes precedence over `prop`
        - `all=True` returns a list of matches; default returns the first match
        """
        if not selector:
            raise ValueError("selector must be non-empty")
        cmd: dict[str, Any] = {
            "op": "query_selector_all" if all else "query_selector",
            "selector": selector,
        }
        if attr is not None:
            cmd["attr"] = attr
        else:
            cmd["prop"] = prop
        return self.send_command(sid, cmd, timeout=timeout)


def _transport(ch: Channel) -> str:
    return "ws" if ch._ws_ready else "qr"


def _describe(ch: Channel) -> dict[str, Any]:
    return {
        "sid": ch.session,
        "window_id": ch._window_id,
        "window_owner": ch._window_owner,
        "transport": _transport(ch),
    }


def build_server(
    *, hide_qr: bool = False, use_bridge: bool = False
) -> tuple[FastMCP, HoloMCPServer]:
    """Build a FastMCP instance with the holo tools registered.

    Returns the FastMCP server and the underlying `HoloMCPServer` so
    the caller can shut down the daemon after `mcp.run()` returns.
    """
    holo = HoloMCPServer(hide_qr=hide_qr, use_bridge=use_bridge)
    mcp = FastMCP("holo")

    @mcp.tool(description="Wait for the bookmarklet's calibration beacon and register a channel.")
    def calibrate(timeout: float = 30.0) -> dict[str, Any]:
        return holo.calibrate(timeout=timeout)

    @mcp.tool(description="List currently registered channels (one per calibrated tab).")
    def list_channels() -> dict[str, Any]:
        return holo.list_channels()

    @mcp.tool(description="Forget a channel by sid. Does not close the browser popup.")
    def drop_channel(sid: str) -> dict[str, Any]:
        return holo.drop_channel(sid)

    @mcp.tool(description="Round-trip a ping through the channel for `sid`.")
    def ping(sid: str, timeout: float = 5.0) -> dict[str, Any]:
        return holo.ping(sid, timeout=timeout)

    @mcp.tool(
        description=(
            "Read a dotted path off the page's global object "
            "(e.g. 'document.title' or 'R2D2_VERSION')."
        )
    )
    def read_global(sid: str, path: str, timeout: float = 5.0) -> dict[str, Any]:
        return holo.read_global(sid, path, timeout=timeout)

    @mcp.tool(
        description=(
            "Send an arbitrary command to the bookmarklet. "
            "Must be a dict with a string `op` field; see bookmarklet/dispatch.js."
        )
    )
    def send_command(
        sid: str, command: dict[str, Any], timeout: float = 5.0
    ) -> dict[str, Any]:
        return holo.send_command(sid, command, timeout=timeout)

    @mcp.tool(description="Bring an application to the foreground by name (e.g. 'Google Chrome').")
    def app_activate(name: str) -> dict[str, Any]:
        return holo.app_activate(name)

    @mcp.tool(
        description=(
            "Click at screen coordinates (top-left origin). "
            "`modifiers` is an optional list like ['cmd'] or ['shift', 'ctrl']."
        )
    )
    def screen_click(
        x: int, y: int, modifiers: list[str] | None = None
    ) -> dict[str, Any]:
        return holo.screen_click(x, y, modifiers=modifiers)

    @mcp.tool(description="Type a literal string into whatever has keyboard focus.")
    def screen_type(text: str) -> dict[str, Any]:
        return holo.screen_type(text)

    @mcp.tool(
        description=(
            "Send a key combo (e.g. 'cmd+v', 'enter', 'shift+tab'). "
            "Sikuli's Key constants are recognised (ENTER, TAB, ESC, F1-F12, …)."
        )
    )
    def screen_key(combo: str) -> dict[str, Any]:
        return holo.screen_key(combo)

    @mcp.tool(
        description=(
            "Capture the screen (or a region) as a PNG. Returns "
            "{image: base64, format: 'png', byte_count}. Pass `region` "
            "as {x, y, width, height} to crop."
        )
    )
    def screen_shot(region: dict[str, int] | None = None) -> dict[str, Any]:
        return holo.screen_shot(region=region)

    @mcp.tool(
        description=(
            "Find a base64-encoded PNG `needle` on screen. Returns "
            "{x, y, width, height, score} or null if no match. "
            "`score` is the minimum similarity threshold (0..1, default 0.7)."
        )
    )
    def screen_find_image(
        needle: str,
        region: dict[str, int] | None = None,
        score: float = 0.7,
    ) -> dict[str, Any] | None:
        return holo.screen_find_image(needle, region=region, score=score)

    # ---- Chrome browser tools (AppleScript; macOS-only) ---------------------
    #
    # Prefer these over keystroke automation (`app_activate` + `screen_key`)
    # for any browser navigation — they're synchronous and don't fight
    # macOS focus.

    @mcp.tool(
        description=(
            "Set the URL of Chrome's active tab in the front window. "
            "Reliable navigation without keystroke simulation (macOS only)."
        )
    )
    def browser_navigate(url: str) -> dict[str, Any]:
        return holo.browser_navigate(url)

    @mcp.tool(
        description=(
            "Open a new tab in Chrome's front window. "
            "If `url` is omitted, the tab opens to the New Tab page."
        )
    )
    def browser_new_tab(url: str | None = None) -> dict[str, Any]:
        return holo.browser_new_tab(url)

    @mcp.tool(description="Close the active tab of Chrome's front window.")
    def browser_close_active_tab() -> dict[str, Any]:
        return holo.browser_close_active_tab()

    @mcp.tool(
        description=(
            "Make tab `index` (1-based) the active tab of Chrome's front "
            "window and bring Chrome to the foreground."
        )
    )
    def browser_activate_tab(index: int) -> dict[str, Any]:
        return holo.browser_activate_tab(index)

    @mcp.tool(
        description=(
            "List tabs in Chrome's front window. Returns "
            "{tabs: [{id, title, url, index}], active: index}."
        )
    )
    def browser_list_tabs() -> dict[str, Any]:
        return holo.browser_list_tabs()

    @mcp.tool(description="Read the URL of Chrome's active tab.")
    def browser_read_active_url() -> dict[str, Any]:
        return holo.browser_read_active_url()

    @mcp.tool(description="Read the title of Chrome's active tab.")
    def browser_read_active_title() -> dict[str, Any]:
        return holo.browser_read_active_title()

    @mcp.tool(description="Reload Chrome's active tab.")
    def browser_reload() -> dict[str, Any]:
        return holo.browser_reload()

    @mcp.tool(description="Navigate back in Chrome's active tab history.")
    def browser_back() -> dict[str, Any]:
        return holo.browser_back()

    @mcp.tool(description="Navigate forward in Chrome's active tab history.")
    def browser_forward() -> dict[str, Any]:
        return holo.browser_forward()

    @mcp.tool(
        description=(
            "Run a JS expression in Chrome's active tab via AppleScript "
            "and return its stringified result. Use this for arbitrary "
            "DOM queries (`document.querySelector('button')?.innerText`, "
            "`JSON.stringify(...)`, etc). "
            "Requires Chrome's 'Allow JavaScript from Apple Events' "
            "toggle (View → Developer); if disabled, the error message "
            "will say so — fall back to `bookmarklet_query` against a "
            "calibrated channel for CSP-safe DOM access."
        )
    )
    def browser_execute_js(js: str) -> dict[str, Any]:
        return holo.browser_execute_js(js)

    @mcp.tool(
        description=(
            "DOM query through the bookmarklet channel — CSP-safe "
            "fallback for `browser_execute_js`. Reads `selector` and "
            "returns the named property (default 'innerText') or "
            "attribute. Pass `all=true` for a list of matches. "
            "Requires a calibrated `sid`."
        )
    )
    def bookmarklet_query(
        sid: str,
        selector: str,
        prop: str = "innerText",
        attr: str | None = None,
        all: bool = False,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        return holo.bookmarklet_query(
            sid, selector, prop=prop, attr=attr, all=all, timeout=timeout
        )

    return mcp, holo


def run(*, hide_qr: bool = False, use_bridge: bool = False) -> None:
    """Entrypoint used by `holo mcp` — runs the server over stdio."""
    mcp, holo = build_server(hide_qr=hide_qr, use_bridge=use_bridge)
    try:
        mcp.run()
    finally:
        holo.shutdown()


def run_tcp(
    port: int,
    *,
    hide_qr: bool = False,
    use_bridge: bool = False,
    stop_event: threading.Event | None = None,
) -> None:
    """Entrypoint used by `holo mcp --listen PORT`.

    Binds 127.0.0.1:PORT, accepts one client at a time. Each
    connection must send the magic prefix line before any MCP
    traffic; mismatched connections are dropped. Daemon state
    (calibrated channels, WS server, bridge) lives across
    connection lifetimes — clients can disconnect and reconnect
    without losing the registered tabs.

    `stop_event` is for tests — production callers leave it None
    and stop the loop with KeyboardInterrupt.
    """
    mcp, holo = build_server(hide_qr=hide_qr, use_bridge=use_bridge)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind(("127.0.0.1", port))
    except OSError as e:
        print(f"holo mcp: bind 127.0.0.1:{port} failed: {e}", file=sys.stderr)
        listener.close()
        holo.shutdown()
        raise SystemExit(1) from e
    listener.listen(1)
    # Polling timeout so the accept loop notices stop_event.
    listener.settimeout(0.5)
    print(
        f"holo mcp: listening on 127.0.0.1:{port} (magic prefix required)",
        file=sys.stderr,
        flush=True,
    )

    try:
        while stop_event is None or not stop_event.is_set():
            try:
                conn, addr = listener.accept()
            except TimeoutError:
                continue
            except KeyboardInterrupt:
                break
            try:
                handshake = read_handshake(conn)
                if not is_valid_handshake(handshake):
                    print(
                        f"holo mcp: rejecting {addr}: "
                        f"bad handshake {handshake[:32]!r}",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue
                print(
                    f"holo mcp: accepted {addr}",
                    file=sys.stderr,
                    flush=True,
                )
                _serve_one_connection(mcp, conn)
                print(
                    f"holo mcp: closed {addr}",
                    file=sys.stderr,
                    flush=True,
                )
            finally:
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                conn.close()
    finally:
        listener.close()
        holo.shutdown()


def _serve_one_connection(mcp: FastMCP, conn: socket.socket) -> None:
    """Run the FastMCP server loop on a single TCP connection.

    Mirrors `FastMCP.run_stdio_async` but supplies socket-backed
    streams instead of sys.stdin/sys.stdout. Reaches for the
    underlying `_mcp_server` because FastMCP doesn't expose a
    stream-injecting public runner.
    """
    fin = conn.makefile("r", encoding="utf-8", errors="replace", newline="\n")
    fout = conn.makefile("w", encoding="utf-8", newline="\n")

    async def go() -> None:
        try:
            stdin = anyio.wrap_file(fin)
            stdout = anyio.wrap_file(fout)
            async with stdio_server(stdin=stdin, stdout=stdout) as (rs, ws):
                await mcp._mcp_server.run(  # noqa: SLF001 — no public stream-injecting runner
                    rs,
                    ws,
                    mcp._mcp_server.create_initialization_options(),  # noqa: SLF001
                )
        finally:
            try:
                fin.close()
            except OSError:
                pass
            try:
                fout.close()
            except OSError:
                pass

    try:
        anyio.run(go)
    except (anyio.EndOfStream, ConnectionResetError, BrokenPipeError):
        pass


# Re-export so callers can write the magic prefix without importing the
# wire helper module separately.
__all__ = [
    "HoloMCPServer",
    "build_server",
    "run",
    "run_tcp",
    "WIRE_MAGIC",
]
