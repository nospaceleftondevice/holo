"""mDNS / DNS-SD announcer for holo MCP sessions.

Broadcasts a `_holo-session._tcp.local.` service so a companion
desktop app on the same LAN can discover live holo sessions and
build "droid" connections (SSH + tmux attach) to reach them.

No authentication material is ever broadcast — credentials live in
the user's SSH config / agent. The TXT record only carries the
metadata the desktop UI needs to show a session and construct a
connection command.

The implementation uses python-zeroconf, which speaks the mDNS
protocol directly via raw sockets — Avahi (Linux) and Bonjour
(Windows) are NOT required.

Usage:
    a = HoloAnnouncer(session="claude-1", user="brad")
    a.start()
    ...
    a.stop()
"""

from __future__ import annotations

import getpass
import logging
import os
import socket
import subprocess
import time
import uuid
from typing import TYPE_CHECKING

from holo import __version__

if TYPE_CHECKING:
    from zeroconf import ServiceInfo, Zeroconf

SERVICE_TYPE = "_holo-session._tcp.local."
TXT_SCHEMA_VERSION = "1"

_log = logging.getLogger(__name__)


class HoloAnnouncer:
    """Lifecycle wrapper around a single Zeroconf service registration.

    Construct with the metadata you want to broadcast, call ``start()``
    to register, and ``stop()`` to unregister cleanly. Idempotent —
    repeat calls to ``start()`` after a successful start are no-ops,
    and ``stop()`` is safe to call before ``start()`` or twice.
    """

    def __init__(
        self,
        *,
        session: str | None = None,
        user: str | None = None,
        ssh_user: str | None = None,
        port: int = 0,
        ips: list[str] | None = None,
    ) -> None:
        self.session = session
        self.user = user or getpass.getuser()
        self.ssh_user = ssh_user
        self.port = port
        self.ips_override = ips
        self._zeroconf: Zeroconf | None = None
        self._service_info: ServiceInfo | None = None

    def build_properties(self) -> dict[bytes, bytes]:
        """Assemble the TXT record.

        Only specified or auto-detectable fields are included — the
        omit-when-not-specified rule keeps the record compact and lets
        the desktop UI distinguish "unset" from "set to empty".
        """
        props: dict[bytes, bytes] = {}

        def put(key: str, value: str | None) -> None:
            if value is None or value == "":
                return
            props[key.encode("utf-8")] = value.encode("utf-8")

        put("v", TXT_SCHEMA_VERSION)
        put("host", socket.gethostname())
        put("user", self.user)
        put("ssh_user", self.ssh_user)
        put("session", self.session)
        put("holo_pid", str(os.getpid()))
        put("holo_version", __version__)
        put("started", str(int(time.time())))
        put("cwd", os.getcwd())

        ips = self._collect_ips()
        if ips:
            # Belt-and-suspenders: also expose IPs in TXT so a client
            # that doesn't follow up with an A-record query can dial
            # the host directly without resolving `<host>.local.`.
            put("ips", ",".join(ips))

        if os.environ.get("TMUX"):
            put("tmux_session", _tmux_field("#S"))
            put("tmux_window", _tmux_field("#W"))

        return props

    def _collect_ips(self) -> list[str]:
        """Pick IPv4 addresses to advertise.

        Order of preference:
        1. Explicit ``ips=`` constructor override (user-curated, e.g.
           "only the VPN-side address").
        2. Auto-enumerate every interface via ``ifaddr``, dropping
           loopback (127.0.0.0/8) and link-local (169.254.0.0/16).
        3. Fall back to ``gethostbyname`` so we never advertise an
           empty A-record set.

        IPv6 is intentionally skipped for v1 — most LAN-discovery
        UIs key off IPv4 and adding v6 broadens the surface without
        helping anyone today.
        """
        if self.ips_override:
            return [ip for ip in self.ips_override if _is_usable_ipv4(ip)]
        enumerated = _enumerate_local_ipv4()
        if enumerated:
            return enumerated
        try:
            return [socket.gethostbyname(socket.gethostname())]
        except OSError:
            return []

    def start(self) -> None:
        """Register the service with the local zeroconf stack.

        Opens a multicast socket on 224.0.0.251:5353. On
        ``allow_name_change=True``, conflict-resolution renames the
        instance (``-2``, ``-3``…) instead of raising — but we already
        salt the instance name with a UUID, so renames are unlikely.
        """
        from zeroconf import IPVersion, ServiceInfo, Zeroconf

        if self._zeroconf is not None:
            return

        instance = self._instance_name()
        properties = self.build_properties()
        addresses = [
            socket.inet_aton(ip) for ip in self._collect_ips()
        ] or [socket.inet_aton("127.0.0.1")]

        self._zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
        self._service_info = ServiceInfo(
            type_=SERVICE_TYPE,
            name=f"{instance}.{SERVICE_TYPE}",
            addresses=addresses,
            port=self.port,
            properties=properties,
            server=f"{socket.gethostname().split('.')[0]}.local.",
        )
        self._zeroconf.register_service(
            self._service_info, allow_name_change=True
        )

    def stop(self) -> None:
        if self._zeroconf is None:
            return
        try:
            if self._service_info is not None:
                self._zeroconf.unregister_service(self._service_info)
        except Exception:
            _log.exception("zeroconf unregister failed")
        try:
            self._zeroconf.close()
        except Exception:
            _log.exception("zeroconf close failed")
        finally:
            self._zeroconf = None
            self._service_info = None

    def _instance_name(self) -> str:
        # DNS label cap is 63 bytes (RFC 1035); GitHub Actions runner
        # hostnames are ~60 bytes alone, so naïve concatenation blows
        # the limit. Compute the budget left after the pid/salt suffix
        # and truncate the human-readable body to fit.
        pid = str(os.getpid())
        salt = uuid.uuid4().hex[:6]
        suffix = f"-{pid}-{salt}"
        budget = 63 - len(suffix)
        if self.session:
            body = f"holo-{self.session}"
        else:
            body = f"holo-{socket.gethostname().split('.')[0]}"
        return body[:budget] + suffix


def _tmux_field(spec: str) -> str | None:
    """Read a tmux format-string from the running tmux server.

    Returns None on any error — we'd rather emit no field than emit a
    stale or misleading one.
    """
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", spec],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    return out or None


def _is_usable_ipv4(addr: str) -> bool:
    """True iff `addr` is a non-loopback, non-link-local dotted-quad.

    Skips:
        - 127.0.0.0/8 (loopback) — companion reaching us via 127.x means
          something already went very wrong
        - 169.254.0.0/16 (link-local / APIPA) — DHCP failure addresses;
          almost never the routable LAN address the companion wants
    """
    parts = addr.split(".")
    if len(parts) != 4:
        return False
    try:
        octets = [int(p) for p in parts]
    except ValueError:
        return False
    if any(o < 0 or o > 255 for o in octets):
        return False
    if octets[0] == 127:
        return False
    if octets[0] == 169 and octets[1] == 254:
        return False
    return True


def _enumerate_local_ipv4() -> list[str]:
    """Walk every interface via `ifaddr` and return usable IPv4 addresses.

    Returns ``[]`` if `ifaddr` isn't importable; the caller falls back
    to ``socket.gethostbyname``. IPv6 entries (returned by ifaddr as
    tuples) are filtered out — see ``_collect_ips`` for rationale.
    """
    try:
        import ifaddr
    except ImportError:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for adapter in ifaddr.get_adapters():
        for ip in adapter.ips:
            if not isinstance(ip.ip, str):
                continue
            if not _is_usable_ipv4(ip.ip):
                continue
            if ip.ip in seen:
                continue
            seen.add(ip.ip)
            out.append(ip.ip)
    return out


__all__ = ["HoloAnnouncer", "SERVICE_TYPE", "TXT_SCHEMA_VERSION"]
