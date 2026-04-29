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

import threading
from typing import Any

from mcp.server.fastmcp import FastMCP

from holo.channel import CalibrationError, Channel, CommandError
from holo.daemon import Daemon


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
        """Wait for a calibration beacon, register a channel, return its sid."""
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

    return mcp, holo


def run(*, hide_qr: bool = False, use_bridge: bool = False) -> None:
    """Entrypoint used by `holo mcp` — runs the server over stdio."""
    mcp, holo = build_server(hide_qr=hide_qr, use_bridge=use_bridge)
    try:
        mcp.run()
    finally:
        holo.shutdown()
