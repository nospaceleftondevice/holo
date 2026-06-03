"""Route mouse / keyboard / activation ops to a remote `holo mcp`.

Use case: the local machine (Machine A) can capture its own screen
(SikuliX capture works without Accessibility) but corporate policy
blocks `java.awt.Robot` / `CGEvent` input injection. A second machine
(Machine B) has a macOS Screen Sharing client open against A, so
events fired on B's display are relayed by Screen Sharing.app back to
A's display. Holo on B is unrestricted.

This module owns the A→B side of that channel:

  - Spawns ``holo connect HOST:PORT`` as a child subprocess. That
    subcommand is already the stdio↔TCP bridge for `holo mcp --listen`
    (magic-prefix handshake, single connection).
  - Wraps the subprocess's stdio in an MCP ``ClientSession`` from the
    official `mcp` SDK.
  - Exposes a sync facade whose method signatures and return shapes
    match `BridgeClient`'s input methods so the local `Daemon` can
    use either one interchangeably (``daemon._remote_input or
    daemon.bridge``).

The MCP session runs inside a dedicated asyncio loop on a background
thread; sync methods submit coroutines with
``asyncio.run_coroutine_threadsafe`` and block on the result. The
session and the subprocess stay open for the daemon's lifetime —
each input op is a fresh ``call_tool``, not a fresh connection.

macOS-first. Same-LAN topology — no SSH tunnel; just direct TCP
between A and B.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import threading
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


class RemoteInputError(RuntimeError):
    """Raised when the remote-input channel can't be established or a
    proxied tool call fails on the remote side."""


# How long the synchronous facade waits for the async loop to come up
# and for each individual tool call. Tool calls are inherently
# interactive (mouse moves on a remote machine) so the per-call ceiling
# is generous; the startup ceiling is tighter because a hung connect
# would otherwise wedge the daemon at boot.
STARTUP_TIMEOUT_S = 30.0
CALL_TIMEOUT_S = 30.0


def _resolve_holo_exe() -> str:
    """Find the `holo` binary to invoke for the connect subprocess.

    Order:
    1. ``HOLO_EXE`` env var — explicit override for tests / unusual setups.
    2. PyInstaller frozen build → ``sys.executable`` IS the holo binary.
    3. Dev install → ``shutil.which("holo")``.
    4. Last resort: ``sys.executable`` (may or may not be holo).
    """
    env = os.environ.get("HOLO_EXE")
    if env:
        return env
    if getattr(sys, "frozen", False):
        return sys.executable
    found = shutil.which("holo")
    if found:
        return found
    return sys.executable


class RemoteInputBackend:
    """Sync facade over an MCP client connected to a remote holo daemon.

    Method signatures and return shapes match `BridgeClient` so the
    Daemon can pick `self._remote_input or self.bridge` without any
    other branching.
    """

    def __init__(self, host: str, port: int, *, holo_exe: str | None = None) -> None:
        self.host = host
        self.port = port
        self._holo_exe = holo_exe or _resolve_holo_exe()

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session: ClientSession | None = None

        # Async context managers we entered manually so we can exit
        # them at shutdown. `stdio_client` returns a (read, write)
        # tuple stream pair; `ClientSession` wraps it.
        self._stdio_cm: Any = None
        self._session_cm: Any = None

        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._stopped = False

    # ---- lifecycle ------------------------------------------------------

    def start(self) -> None:
        """Spawn the connect subprocess and open an MCP session.

        Blocks until the session is initialized or fails. Raises
        `RemoteInputError` on any startup failure (connect spawn
        failure, magic-prefix rejection by remote, MCP init timeout).
        """
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._thread_main,
            name=f"holo-remote-input-{self.host}-{self.port}",
            daemon=True,
        )
        self._thread.start()
        ok = self._ready.wait(timeout=STARTUP_TIMEOUT_S)
        if not ok:
            raise RemoteInputError(
                f"timed out connecting to {self.host}:{self.port} after "
                f"{STARTUP_TIMEOUT_S}s — is `holo mcp --listen {self.port}` "
                "running on the remote?"
            )
        if self._startup_error is not None:
            raise self._startup_error

    def stop(self) -> None:
        """Tear down the MCP session and the connect subprocess.

        Idempotent. Safe to call from arbitrary threads; the loop
        runs the cleanup coroutine and then stops itself.
        """
        if self._stopped or self._loop is None:
            self._stopped = True
            return
        self._stopped = True
        loop = self._loop
        try:
            fut = asyncio.run_coroutine_threadsafe(self._close_session(), loop)
            try:
                fut.result(timeout=5.0)
            except Exception:
                pass
        finally:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass
            if self._thread is not None:
                self._thread.join(timeout=5.0)

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._open_session())
            if self._startup_error is None:
                loop.run_forever()
        finally:
            try:
                loop.close()
            except Exception:
                pass

    async def _open_session(self) -> None:
        try:
            params = StdioServerParameters(
                command=self._holo_exe,
                args=["connect", f"{self.host}:{self.port}"],
                env=os.environ.copy(),
            )
            self._stdio_cm = stdio_client(params)
            read, write = await self._stdio_cm.__aenter__()
            self._session_cm = ClientSession(read, write)
            self._session = await self._session_cm.__aenter__()
            await self._session.initialize()
        except Exception as e:  # noqa: BLE001 — surface every startup failure as one error
            self._startup_error = RemoteInputError(
                f"failed to open MCP session to {self.host}:{self.port}: {e}"
            )
        finally:
            self._ready.set()

    async def _close_session(self) -> None:
        # Order matters: exit ClientSession first (stops its
        # background readers), then the stdio_client (which waits for
        # the child subprocess to drain and exit).
        if self._session_cm is not None:
            try:
                await self._session_cm.__aexit__(None, None, None)
            except Exception:
                pass
            self._session_cm = None
            self._session = None
        if self._stdio_cm is not None:
            try:
                await self._stdio_cm.__aexit__(None, None, None)
            except Exception:
                pass
            self._stdio_cm = None

    # ---- call ------------------------------------------------------------

    def _call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._stopped:
            raise RemoteInputError("backend already stopped")
        if self._session is None or self._loop is None:
            raise RemoteInputError("backend not started — call start() first")

        coro = self._session.call_tool(tool, arguments)
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            result = fut.result(timeout=CALL_TIMEOUT_S)
        except TimeoutError as e:
            raise RemoteInputError(
                f"remote tool {tool!r} timed out after {CALL_TIMEOUT_S}s"
            ) from e
        except Exception as e:
            raise RemoteInputError(f"remote tool {tool!r} raised: {e}") from e

        if getattr(result, "isError", False):
            # FastMCP serializes raised exceptions as a TextContent block
            # with the error message. Surface it verbatim — the agent /
            # caller is the right place to decide retry semantics.
            msg = _first_text(result.content) or f"remote {tool!r} reported error"
            raise RemoteInputError(f"remote tool {tool!r}: {msg}")

        # FastMCP returns dict-shaped tool results in `structuredContent`.
        sc = getattr(result, "structuredContent", None)
        if isinstance(sc, dict):
            return sc

        # Fallback: parse the first text block as JSON, else wrap.
        text = _first_text(result.content)
        if text:
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return parsed
            except (json.JSONDecodeError, ValueError):
                pass
            return {"text": text}
        return {}

    # ---- input surface (matches BridgeClient) ---------------------------

    def click(
        self, x: int, y: int, *, modifiers: list[str] | None = None
    ) -> dict[str, Any]:
        return self._call(
            "screen_click",
            {"x": x, "y": y, "modifiers": modifiers or []},
        )

    def key(self, combo: str) -> dict[str, Any]:
        return self._call("screen_key", {"combo": combo})

    def type_text(self, text: str) -> dict[str, Any]:
        return self._call("screen_type", {"text": text})

    def scroll(
        self,
        x: int,
        y: int,
        *,
        direction: str = "down",
        steps: int = 3,
    ) -> dict[str, Any]:
        return self._call(
            "screen_scroll",
            {"x": x, "y": y, "direction": direction, "steps": steps},
        )

    def activate(self, name: str) -> dict[str, Any]:
        return self._call("app_activate", {"name": name})


def _first_text(content: Any) -> str | None:
    """Extract the first text payload from an MCP CallToolResult.content list."""
    if not content:
        return None
    for block in content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            return text
    return None
