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

# TXT field names — single source of truth shared with discover.py so the
# announcer and the consumer can't drift. Spec: docs/companion-spec.md §2.4.
FIELD_V = "v"
FIELD_HOST = "host"
FIELD_USER = "user"
FIELD_SSH_USER = "ssh_user"
FIELD_SESSION = "session"
FIELD_HOLO_PID = "holo_pid"
FIELD_HOLO_VERSION = "holo_version"
FIELD_STARTED = "started"
FIELD_CWD = "cwd"
FIELD_IPS = "ips"
FIELD_TMUX_SESSION = "tmux_session"
FIELD_TMUX_WINDOW = "tmux_window"
# Optional capabilities-endpoint advertisement. When the daemon was
# launched with `--announce-capabilities`, these fields point at the
# in-process HTTP server (`holo.capabilities_server`). Discoverers
# fetch `http://<ip>:<caps_port>/capabilities` with the
# `X-Holo-Caps-Token: <caps_token>` header to read the host's hardware
# / software / package inventory. Both fields are optional and either
# both present or both absent.
FIELD_CAPS_PORT = "caps_port"
FIELD_CAPS_TOKEN = "caps_token"
# Phase 4 (CloudCity reverse-tunnel): published when this daemon has
# an active reverse-tunnel into a CloudCity host. The desktop SPA
# reads `tunnel_port` and re-routes its c2w VM's SSH command from
# `<announced_ip>:22` to `<cloudcity_loopback>:<tunnel_port>` so the
# c2w-net container — which on macOS Docker Desktop can't reach LAN
# peers directly — finds the tmux host via the tunnel instead.
# Cleared (TXT field omitted) when no tunnel is active.
FIELD_TUNNEL_PORT = "tunnel_port"

# Required even when other fields are missing. A TXT missing any of these
# is malformed and should be dropped.
REQUIRED_FIELDS: tuple[str, ...] = (
    FIELD_V,
    FIELD_HOST,
    FIELD_USER,
    FIELD_HOLO_PID,
    FIELD_HOLO_VERSION,
    FIELD_STARTED,
    FIELD_CWD,
)

# Fields parsed/emitted as integers in the JSON contract. TXT carries them
# as UTF-8 strings; discover.py converts.
INT_FIELDS: frozenset[str] = frozenset(
    {FIELD_HOLO_PID, FIELD_STARTED, FIELD_CAPS_PORT, FIELD_TUNNEL_PORT}
)

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
        caps_port: int | None = None,
        caps_token: str | None = None,
    ) -> None:
        self.session = session
        self.user = user or getpass.getuser()
        self.ssh_user = ssh_user
        self.port = port
        self.ips_override = ips
        # Both must be set together to be advertised — TXT carries them
        # only when the capabilities HTTP server is up. Validating the
        # pairing here keeps callers from publishing a port with no
        # token (which would let any LAN client read /capabilities).
        if (caps_port is None) != (caps_token is None):
            raise ValueError(
                "caps_port and caps_token must be set together (both "
                "present or both omitted)"
            )
        self.caps_port = caps_port
        self.caps_token = caps_token
        # Tunnel port (Phase 4) starts unset; ``set_tunnel_port`` flips
        # it on/off. When set, included in the broadcast TXT record so
        # the desktop SPA can route through it.
        self._tunnel_port: int | None = None
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

        put(FIELD_V, TXT_SCHEMA_VERSION)
        put(FIELD_HOST, socket.gethostname())
        put(FIELD_USER, self.user)
        put(FIELD_SSH_USER, self.ssh_user)
        put(FIELD_SESSION, self.session)
        put(FIELD_HOLO_PID, str(os.getpid()))
        put(FIELD_HOLO_VERSION, __version__)
        put(FIELD_STARTED, str(int(time.time())))
        put(FIELD_CWD, os.getcwd())

        ips = self._collect_ips()
        if ips:
            # Belt-and-suspenders: also expose IPs in TXT so a client
            # that doesn't follow up with an A-record query can dial
            # the host directly without resolving `<host>.local.`.
            put(FIELD_IPS, ",".join(ips))

        if os.environ.get("TMUX"):
            put(FIELD_TMUX_SESSION, _tmux_field("#S"))
            put(FIELD_TMUX_WINDOW, _tmux_field("#W"))

        if self.caps_port is not None and self.caps_token is not None:
            put(FIELD_CAPS_PORT, str(self.caps_port))
            put(FIELD_CAPS_TOKEN, self.caps_token)

        if self._tunnel_port is not None:
            put(FIELD_TUNNEL_PORT, str(self._tunnel_port))

        return props

    def _collect_ips(self) -> list[str]:
        """Pick IPv4 addresses to advertise.

        Order of preference:
        1. Explicit ``ips=`` constructor override (user-curated, e.g.
           "only the VPN-side address"). Each entry is either a
           literal IPv4 (advertised as-is) or a trailing-dot prefix
           (``192.168.1.``) used to filter the enumerated interface
           list — see :func:`_resolve_ip_overrides`.
        2. Auto-enumerate every interface via ``ifaddr``, dropping
           loopback (127.0.0.0/8) and link-local (169.254.0.0/16).
        3. Fall back to ``gethostbyname`` so we never advertise an
           empty A-record set.

        IPv6 is intentionally skipped for v1 — most LAN-discovery
        UIs key off IPv4 and adding v6 broadens the surface without
        helping anyone today.
        """
        if self.ips_override:
            resolved = _resolve_ip_overrides(self.ips_override)
            return [ip for ip in resolved if _is_usable_ipv4(ip)]
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

    def set_tunnel_port(self, port: int | None) -> None:
        """Add / clear ``tunnel_port`` on the published TXT record.

        Pass an integer to advertise an active reverse-tunnel; pass
        ``None`` to clear the field. When the announcer hasn't been
        started yet this is a pure config update — the new value will
        appear on the first ``start()``. When already started, the
        record is rebuilt and re-broadcast via ``Zeroconf.update_service``
        so listening companions see the change without needing to wait
        for a TTL refresh.

        Idempotent: setting to the current value is a no-op.
        """
        if self._tunnel_port == port:
            return
        self._tunnel_port = port
        if self._zeroconf is None or self._service_info is None:
            return
        # Build a fresh ServiceInfo with the updated TXT and ask
        # zeroconf to push it. We reuse the existing instance name +
        # addresses so listeners see this as an `update_service`, not
        # a `remove + add`.
        from zeroconf import ServiceInfo

        new_info = ServiceInfo(
            type_=self._service_info.type,
            name=self._service_info.name,
            addresses=list(self._service_info.addresses),
            port=self._service_info.port,
            properties=self.build_properties(),
            server=self._service_info.server,
        )
        try:
            self._zeroconf.update_service(new_info)
        except Exception:  # noqa: BLE001 — log + keep state in sync
            _log.exception("zeroconf update_service failed")
            return
        self._service_info = new_info

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


def _resolve_ip_overrides(entries: list[str]) -> list[str]:
    """Resolve a mixed override list of literal IPs and prefix filters.

    Each entry in ``entries`` is one of:
      - a complete dotted-quad (``"192.168.1.5"``): kept verbatim, even
        if it's not on any local interface — sometimes the operator
        knows about a routable address holo can't see locally.
      - a trailing-dot prefix (``"192.168.1."``, ``"192."``): used as
        a string-prefix filter against locally enumerated interfaces.
        Only enumerated IPs starting with the prefix are kept.

    Order is preserved (entry order in the input drives entry order in
    the output) and duplicates are dropped. If a prefix matches no
    interface, it contributes nothing — the caller does NOT fall back
    to advertising every interface, since the user's intent in
    specifying a filter is "advertise only this subnet".

    Enumeration is skipped entirely when the override list contains
    no prefixes — the literal-only path was the original behaviour
    and shouldn't pay an ifaddr.get_adapters() cost on every start.
    """
    needs_enumeration = any(e.endswith(".") for e in entries if e)
    enumerated = _enumerate_local_ipv4() if needs_enumeration else []
    out: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if not entry:
            continue
        if entry.endswith("."):
            for ip in enumerated:
                if ip.startswith(entry) and ip not in seen:
                    seen.add(ip)
                    out.append(ip)
        elif entry not in seen:
            seen.add(entry)
            out.append(entry)
    return out


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


__all__ = [
    "FIELD_CAPS_PORT",
    "FIELD_CAPS_TOKEN",
    "FIELD_CWD",
    "FIELD_HOLO_PID",
    "FIELD_HOLO_VERSION",
    "FIELD_HOST",
    "FIELD_IPS",
    "FIELD_SESSION",
    "FIELD_SSH_USER",
    "FIELD_STARTED",
    "FIELD_TMUX_SESSION",
    "FIELD_TMUX_WINDOW",
    "FIELD_TUNNEL_PORT",
    "FIELD_USER",
    "FIELD_V",
    "HoloAnnouncer",
    "INT_FIELDS",
    "REQUIRED_FIELDS",
    "SERVICE_TYPE",
    "TXT_SCHEMA_VERSION",
]
