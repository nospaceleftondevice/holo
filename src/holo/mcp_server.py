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

import signal
import socket
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.stdio import stdio_server

from holo.channel import CalibrationError, Channel, CommandError
from holo.daemon import Daemon
from holo.mcp_wire import WIRE_MAGIC, is_valid_handshake, read_handshake
from holo.templates import TemplateNotFound, TemplateStore


class HoloMCPServer:
    """Holds the lazy Daemon and the tool implementations.

    Splitting the tool bodies off the FastMCP decorator lets tests
    drive them directly without spinning up a stdio loop, and gives
    us one place to attach a fake daemon in tests.
    """

    def __init__(
        self,
        *,
        hide_qr: bool = False,
        enable_screen: bool = False,
        no_bookmarklet: bool = False,
        templates: TemplateStore | None = None,
        announce: bool = False,
        announce_session: str | None = None,
        announce_user: str | None = None,
        announce_ssh_user: str | None = None,
        announce_ips: list[str] | None = None,
        announce_port: int = 0,
    ) -> None:
        self.hide_qr = hide_qr
        self.enable_screen = enable_screen
        self.no_bookmarklet = no_bookmarklet
        self._daemon: Daemon | None = None
        self._daemon_lock = threading.Lock()
        # Template cache lives across daemon restarts — it's a pure
        # filesystem store, not bound to any JVM/browser session.
        self.templates = templates if templates is not None else TemplateStore()

        self._announcer: Any | None = None
        if announce:
            try:
                from holo.announce import HoloAnnouncer

                self._announcer = HoloAnnouncer(
                    session=announce_session,
                    user=announce_user,
                    ssh_user=announce_ssh_user,
                    port=announce_port,
                    ips=announce_ips,
                )
                self._announcer.start()
            except Exception as e:  # noqa: BLE001 — surface and continue
                print(
                    f"holo mcp: mDNS announce failed ({e}); continuing "
                    "without broadcast",
                    file=sys.stderr,
                    flush=True,
                )
                self._announcer = None

    @property
    def daemon(self) -> Daemon:
        with self._daemon_lock:
            if self._daemon is None:
                self._daemon = Daemon(
                    hide_qr=self.hide_qr,
                    enable_screen=self.enable_screen,
                    no_bookmarklet=self.no_bookmarklet,
                )
            return self._daemon

    def shutdown(self) -> None:
        if self._announcer is not None:
            try:
                self._announcer.stop()
            except Exception:  # noqa: BLE001 — shutdown must not raise
                pass
            self._announcer = None
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
                "Screen tools unavailable. Start the daemon with "
                "`enable_screen=True` (CLI: `holo mcp --screen`) and "
                "ensure OpenJDK 11+ + sikulix*.jar are installed."
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

    def screen_scroll(
        self,
        x: int,
        y: int,
        direction: str = "down",
        steps: int = 3,
    ) -> dict[str, Any]:
        """Move to (x, y) and emit `steps` mouse-wheel events."""
        return self._require_bridge().scroll(
            x, y, direction=direction, steps=steps
        )

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

    # ---- UI template cache ----------------------------------------------
    #
    # Maps natural-language `(app, label)` keys to stored PNG variants
    # so the agent doesn't need to re-discover the same on-screen
    # element via Vision every session. `_capture` blocks for a user
    # rectangle drag (or accepts a `region` for programmatic stash);
    # `_find` runs SikuliX template matching against the saved PNG;
    # `_click` is the find-and-click convenience over the existing
    # screen.click. Storage layout / index format documented in
    # `holo.templates`.

    def ui_template_capture(
        self,
        label: str,
        app: str | None = None,
        region: dict[str, int] | None = None,
        replace: bool = False,
        similarity: float = 0.85,
        timeout: float = 60.0,
        prompt: str = "",
    ) -> dict[str, Any]:
        """Save a template for `(app, label)` from a user-drawn rect or a programmatic region.

        - region=None → blocks for a user `drag-rectangle` capture (Esc cancels).
        - region={x,y,w,h} → captures that rect via screen.shot (no UI prompt).

        `replace=True` discards prior variants for the entry; otherwise the
        new image is appended as another variant (idle / hover / etc.).
        Returns the saved index entry.
        """
        bridge = self._require_bridge()
        if region is not None:
            png = bridge.screenshot(region=region)
        else:
            result = bridge.user_capture(prompt=prompt, timeout=timeout)
            if result.get("cancelled"):
                # Surface as a non-error response — agent re-prompts user.
                return {
                    "cancelled": True,
                    "reason": result.get("reason", "user cancelled"),
                }
            import base64 as _b64

            png = _b64.b64decode(result["image"])
        entry = self.templates.add_variant(
            label, app, png, replace=replace, similarity=similarity
        )
        return {"saved": True, "entry": entry}

    def ui_template_list(self, app: str | None = None) -> dict[str, Any]:
        """List stored templates. `app=None` lists all; pass `'_global'` for the catch-all."""
        return {"templates": self.templates.list(app=app)}

    def ui_template_find(
        self,
        label: str,
        app: str | None = None,
        region: dict[str, int] | None = None,
    ) -> dict[str, Any] | None:
        """Locate a saved template on the current screen.

        Walks each variant in order and returns the first hit. Bumps
        `last_used` / `match_count` on the entry. Returns None if no
        variant matches anywhere on the (optionally constrained) screen.

        Raises `LookupError` if there's no entry for `(app, label)` —
        distinct from "entry exists but doesn't match right now".
        """
        try:
            paths = self.templates.variant_paths(label, app)
        except TemplateNotFound as e:
            raise LookupError(str(e)) from e
        entry = self.templates.get(label, app)
        # `get` is checked again because `variant_paths` raises before
        # we reach this; we know it exists.
        score = float(entry["similarity"]) if entry else 0.85
        bridge = self._require_bridge()
        for p in paths:
            match = bridge.find_image_path(str(p), region=region, score=score)
            if match is not None:
                self.templates.touch(label, app)
                return {**match, "variant": p.name}
        return None

    def ui_template_click(
        self,
        label: str,
        app: str | None = None,
        region: dict[str, int] | None = None,
        button: str = "left",
        clicks: int = 1,
    ) -> dict[str, Any]:
        """Find a saved template and click its center. Raises if nothing matches.

        Convenience over `ui_template_find` + `screen_click`. The agent
        gets one tool call for the 80% case (open the bookmarks menu,
        click a saved icon, etc.). On miss this raises rather than
        silently doing nothing — clicking the wrong place is worse than
        a clear error.
        """
        del button, clicks  # not yet wired through screen.click — left/single only
        match = self.ui_template_find(label, app, region=region)
        if match is None:
            app_norm = app or "_global"
            raise RuntimeError(
                f"template {app_norm}/{label} matched nothing on screen "
                "(use ui_template_capture to refresh, or check whether the "
                "target app is in front)"
            )
        cx = int(match["x"] + match["width"] / 2)
        cy = int(match["y"] + match["height"] / 2)
        self._require_bridge().click(cx, cy)
        return {
            "clicked": True,
            "x": cx,
            "y": cy,
            "score": match["score"],
            "variant": match["variant"],
        }

    def ui_template_delete(
        self,
        label: str,
        app: str | None = None,
        variant: str | None = None,
    ) -> dict[str, Any]:
        """Remove a stored template entry, or just one of its variants."""
        removed = self.templates.delete(label, app, variant=variant)
        return {"removed": removed}

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
    *,
    hide_qr: bool = False,
    enable_screen: bool = False,
    no_bookmarklet: bool = False,
    announce: bool = False,
    announce_session: str | None = None,
    announce_user: str | None = None,
    announce_ssh_user: str | None = None,
    announce_ips: list[str] | None = None,
    announce_port: int = 0,
) -> tuple[FastMCP, HoloMCPServer]:
    """Build a FastMCP instance with the holo tools registered.

    Returns the FastMCP server and the underlying `HoloMCPServer` so
    the caller can shut down the daemon after `mcp.run()` returns.

    With `no_bookmarklet=True`, the channel-dependent tools
    (calibrate, list_channels, drop_channel, ping, read_global,
    send_command, bookmarklet_query) are not registered and the
    daemon never spins up its WS server. Suits agents that only
    drive screen + AppleScript surfaces.

    With `announce=True`, broadcasts an mDNS service record
    (`_holo-session._tcp.local.`) carrying the optional session /
    user / ssh_user metadata for a companion desktop app to
    discover.
    """
    holo = HoloMCPServer(
        hide_qr=hide_qr,
        enable_screen=enable_screen,
        no_bookmarklet=no_bookmarklet,
        announce=announce,
        announce_session=announce_session,
        announce_user=announce_user,
        announce_ssh_user=announce_ssh_user,
        announce_ips=announce_ips,
        announce_port=announce_port,
    )
    mcp = FastMCP("holo")

    if not no_bookmarklet:
        @mcp.tool(
            description=(
                "Wait for the bookmarklet's calibration beacon and "
                "register a channel."
            )
        )
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
            "Move to screen coordinates (x, y) and emit `steps` "
            "mouse-wheel events in `direction` ('up' or 'down', "
            "default 'down'). Use this when keyboard scroll won't "
            "work — e.g. a sidebar that doesn't have keyboard focus."
        )
    )
    def screen_scroll(
        x: int, y: int, direction: str = "down", steps: int = 3
    ) -> dict[str, Any]:
        return holo.screen_scroll(x, y, direction=direction, steps=steps)

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

    # ---- UI template cache --------------------------------------------------
    #
    # Persistent cache mapping (app, label) → stored PNG variants. Used to
    # turn natural-language UI references ("the kebab menu", "the bookmarks
    # bar work folder") into screen coordinates without redoing vision each
    # session. Templates are captured once (interactively or from a region
    # the agent already has) and reused.

    @mcp.tool(
        description=(
            "Save a template image for `(app, label)`. If `region` is "
            "provided, captures that screen rect; otherwise blocks for the "
            "user to drag-select a rectangle (Esc cancels). `app` defaults "
            "to '_global'. Pass `replace=True` to discard existing variants; "
            "otherwise the new image is added as another variant (idle, "
            "hover, dark mode, etc.). Returns the saved index entry, or "
            "{cancelled: true} if the user pressed Esc."
        )
    )
    def ui_template_capture(
        label: str,
        app: str | None = None,
        region: dict[str, int] | None = None,
        replace: bool = False,
        similarity: float = 0.85,
        timeout: float = 60.0,
        prompt: str = "",
    ) -> dict[str, Any]:
        return holo.ui_template_capture(
            label,
            app=app,
            region=region,
            replace=replace,
            similarity=similarity,
            timeout=timeout,
            prompt=prompt,
        )

    @mcp.tool(
        description=(
            "List stored UI templates. `app=None` lists everything; pass an "
            "app name (or '_global') to filter."
        )
    )
    def ui_template_list(app: str | None = None) -> dict[str, Any]:
        return holo.ui_template_list(app=app)

    @mcp.tool(
        description=(
            "Locate a saved template on the current screen. Walks variants "
            "in order, returns the first hit as {x, y, width, height, score, "
            "variant} or null if none match. Raises if the label has no "
            "registered template — call `ui_template_capture` first."
        )
    )
    def ui_template_find(
        label: str,
        app: str | None = None,
        region: dict[str, int] | None = None,
    ) -> dict[str, Any] | None:
        return holo.ui_template_find(label, app=app, region=region)

    @mcp.tool(
        description=(
            "Find a saved template and click its center. Raises if nothing "
            "matches — clicking the wrong place is worse than a clear error."
        )
    )
    def ui_template_click(
        label: str,
        app: str | None = None,
        region: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        return holo.ui_template_click(label, app=app, region=region)

    @mcp.tool(
        description=(
            "Remove a stored template entry, or pass `variant` to delete just "
            "one variant (the entry stays if other variants remain)."
        )
    )
    def ui_template_delete(
        label: str,
        app: str | None = None,
        variant: str | None = None,
    ) -> dict[str, Any]:
        return holo.ui_template_delete(label, app=app, variant=variant)

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

    if not no_bookmarklet:
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


@contextmanager
def _sigterm_as_keyboard_interrupt() -> Iterator[None]:
    """Convert SIGTERM into KeyboardInterrupt while inside the block.

    Existing teardown paths already handle KeyboardInterrupt — they
    fall through to the `finally` clauses that call `holo.shutdown()`,
    which gives the announcer a chance to send mDNS Goodbye packets
    (TTL=0 records) before the process exits. Without this, `kill
    <pid>` leaves stale entries in every cache on the LAN until they
    age out (~75 s).

    The handler is restored on exit so test-suite re-entry doesn't
    poison subsequent runs. Only the main thread can call
    `signal.signal`; that's fine — `run`/`run_tcp` are CLI entrypoints.
    """

    def _handler(signum: int, frame: Any) -> None:  # noqa: ARG001
        raise KeyboardInterrupt()

    try:
        previous = signal.signal(signal.SIGTERM, _handler)
    except ValueError:
        # Not on the main thread (e.g. inside a test harness that
        # called this from a worker). Skip — graceful shutdown still
        # works for SIGINT via Python's default handler.
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def run(
    *,
    hide_qr: bool = False,
    enable_screen: bool = False,
    no_bookmarklet: bool = False,
    announce: bool = False,
    announce_session: str | None = None,
    announce_user: str | None = None,
    announce_ssh_user: str | None = None,
    announce_ips: list[str] | None = None,
) -> None:
    """Entrypoint used by `holo mcp` — runs the server over stdio."""
    mcp, holo = build_server(
        hide_qr=hide_qr,
        enable_screen=enable_screen,
        no_bookmarklet=no_bookmarklet,
        announce=announce,
        announce_session=announce_session,
        announce_user=announce_user,
        announce_ssh_user=announce_ssh_user,
        announce_ips=announce_ips,
    )
    try:
        with _sigterm_as_keyboard_interrupt():
            mcp.run()
    except KeyboardInterrupt:
        # Normal shutdown path — fall through to finally for cleanup.
        pass
    finally:
        holo.shutdown()


def run_tcp(
    port: int,
    *,
    hide_qr: bool = False,
    enable_screen: bool = False,
    no_bookmarklet: bool = False,
    announce: bool = False,
    announce_session: str | None = None,
    announce_user: str | None = None,
    announce_ssh_user: str | None = None,
    announce_ips: list[str] | None = None,
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
    mcp, holo = build_server(
        hide_qr=hide_qr,
        enable_screen=enable_screen,
        no_bookmarklet=no_bookmarklet,
        announce=announce,
        announce_session=announce_session,
        announce_user=announce_user,
        announce_ssh_user=announce_ssh_user,
        announce_ips=announce_ips,
        announce_port=port,
    )
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
        with _sigterm_as_keyboard_interrupt():
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
                except KeyboardInterrupt:
                    break
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
