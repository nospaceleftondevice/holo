"""Single-process daemon that owns the WS server, registry, and channels.

Construct one `Daemon` per process; call `calibrate()` once per browser
tab. Each calibrated `Channel` is registered under its sid so the WS
server can route incoming handshakes to the right one. Channels for
which the WS handshake never lands stay on the QR/clipboard path
indefinitely — the WS poller is opportunistic, not load-bearing.

This sits above `Channel` so the existing test suite can keep
constructing bare `Channel()` instances for the QR-only path; only
production callers (`cli.py`, future SDK users) go through the daemon.

The Daemon also owns the SikuliX bridge — a single JVM subprocess
shared across all channels. The bridge is started lazily on first
access so tests and dev environments without OpenJDK / the SikuliX
jar fall through to the legacy macOS-only input path in `_macos.py`.
"""

from __future__ import annotations

from holo.bridge import BridgeClient, BridgeError, BridgeMissingError
from holo.channel import Channel
from holo.registry import ChannelRegistry
from holo.ws_server import WSServer


class Daemon:
    def __init__(self, *, hide_qr: bool = False, use_bridge: bool = False) -> None:
        self.hide_qr = hide_qr
        self.use_bridge = use_bridge
        self.registry = ChannelRegistry()
        self.ws_server = WSServer(self.registry)
        self.ws_server.start()
        self._bridge: BridgeClient | None = None
        self._bridge_attempted: bool = False

    @property
    def bridge(self) -> BridgeClient | None:
        """Lazy-start the SikuliX bridge if `use_bridge=True`.

        Returns the live `BridgeClient` on first success, `None` for
        every call when `use_bridge=False`, or `None` after a failed
        start (channels then fall through to the legacy `_macos.py`
        path). Opt-in keeps tests / dev environments without OpenJDK
        on the legacy path even when the jar happens to be present.
        """
        if not self.use_bridge:
            return None
        if self._bridge_attempted:
            return self._bridge
        self._bridge_attempted = True
        client = BridgeClient()
        try:
            client.start()
        except (BridgeMissingError, BridgeError, OSError):
            self._bridge = None
            return None
        self._bridge = client
        return self._bridge

    def calibrate(self, *, timeout: float | None = None) -> Channel:
        ch = Channel(daemon=self, hide_qr=self.hide_qr)
        sid = ch.wait_for_calibration(timeout=timeout)
        self.registry.register(sid, ch)
        return ch

    def shutdown(self) -> None:
        self.ws_server.stop()
        if self._bridge is not None:
            self._bridge.stop()
            self._bridge = None
