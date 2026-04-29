"""Single-process daemon that owns the WS server, registry, and channels.

Construct one `Daemon` per process; call `calibrate()` once per browser
tab. Each calibrated `Channel` is registered under its sid so the WS
server can route incoming handshakes to the right one. Channels for
which the WS handshake never lands stay on the QR/clipboard path
indefinitely — the WS poller is opportunistic, not load-bearing.

This sits above `Channel` so the existing test suite can keep
constructing bare `Channel()` instances for the QR-only path; only
production callers (`cli.py`, future SDK users) go through the daemon.
"""

from __future__ import annotations

from holo.channel import Channel
from holo.registry import ChannelRegistry
from holo.ws_server import WSServer


class Daemon:
    def __init__(self, *, hide_qr: bool = False) -> None:
        self.hide_qr = hide_qr
        self.registry = ChannelRegistry()
        self.ws_server = WSServer(self.registry)
        self.ws_server.start()

    def calibrate(self, *, timeout: float | None = None) -> Channel:
        ch = Channel(daemon=self, hide_qr=self.hide_qr)
        sid = ch.wait_for_calibration(timeout=timeout)
        self.registry.register(sid, ch)
        return ch

    def shutdown(self) -> None:
        self.ws_server.stop()
