"""Cross-platform host-capabilities probe.

Three probe layers:

  1. **Hardware** — always collected when the probe runs:
     ``os``, ``os_version``, ``arch``, ``cpu_model``, ``cores``,
     ``ram_gb``. Cheap to gather and the routing-by-chip use case
     ("send transcription to the M4 host, not the M1") needs them.

  2. **Software** — names looked up via ``shutil.which``. On macOS
     a small known-bundle map handles `.app` paths that don't ship a
     binary on PATH (Chrome Canary etc).

  3. **Packages** — for each package manager the user opted into
     (``--probe-pkg brew,apt,winget,...``), shell out to that
     manager's "list installed" command and parse the output. If the
     manager isn't on PATH the probe is silently skipped.

Results are cached for ``cache_ttl_s`` seconds — agents typically
poll within seconds of each other and the package-manager probes
(especially ``brew list``) are slow.

The probe is read-only and never installs / modifies anything; it
only inspects the host. The data is meant to be served over the
authenticated capabilities HTTP endpoint
(:mod:`holo.capabilities_server`), which a discovering agent fetches
to decide where to route a task.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import threading
import time
from typing import Any

_log = logging.getLogger(__name__)


# Schema version of the JSON returned by `CapabilitiesProbe.collect()`.
# Bump when fields move or change semantics; additive new fields are
# safe under the same version.
CAPABILITIES_SCHEMA_VERSION = 1


# Default `shutil.which` lookup names. Tuned for the agent-routing use
# cases we know about today (browser pinning, transcription pipelines,
# local LLM serving). Override with `--probe-software a,b,c`.
DEFAULT_SOFTWARE_PROBES: tuple[str, ...] = (
    "chrome",
    "chrome-canary",
    "chrome-beta",
    "firefox",
    "ffmpeg",
    "whisper",
    "ollama",
    "docker",
    "git",
    "node",
    "python3",
)


# macOS .app bundles that don't always drop a CLI launcher on PATH.
# When `shutil.which("chrome-canary")` returns None we fall back to
# checking if the bundle directory exists. The bundle path is what
# we report — callers can use it with `open -a` or AppleScript.
_MACOS_APP_BUNDLES: dict[str, str] = {
    "chrome": "/Applications/Google Chrome.app",
    "chrome-canary": "/Applications/Google Chrome Canary.app",
    "chrome-beta": "/Applications/Google Chrome Beta.app",
    "firefox": "/Applications/Firefox.app",
    "safari": "/Applications/Safari.app",
    "safari-tp": "/Applications/Safari Technology Preview.app",
}


# Names of package managers the probe knows how to query. Pass any
# subset on the CLI with `--probe-pkg`. Unknown names are rejected at
# CLI parse time so users get a clear error rather than a silent skip.
SUPPORTED_PKG_MANAGERS: tuple[str, ...] = (
    "brew",
    "apt",
    "dpkg",
    "dnf",
    "yum",
    "rpm",
    "port",
    "pacman",
    "winget",
    "choco",
)


def _probe_brew() -> list[dict[str, str]] | None:
    out = _run_pkg_command(["brew", "list", "--versions"])
    if out is None:
        return None
    pkgs: list[dict[str, str]] = []
    for raw in out.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split()
        # `brew list --versions` emits `name v1 v2 v3` when multiple
        # versions are installed; we report the newest-listed as the
        # canonical version and ignore the rest. Good enough for
        # routing decisions.
        if len(parts) >= 2:
            pkgs.append({"name": parts[0], "version": parts[1]})
        else:
            pkgs.append({"name": parts[0], "version": ""})
    return pkgs


def _probe_apt() -> list[dict[str, str]] | None:
    # `dpkg-query` is more deterministic than `apt list --installed`,
    # which prints a "WARNING: ..." line on stderr that some shells
    # surface. Same data, cleaner output.
    out = _run_pkg_command(
        ["dpkg-query", "-W", "-f=${Package} ${Version}\n"]
    )
    if out is None:
        return None
    return _parse_two_column(out)


def _probe_rpm() -> list[dict[str, str]] | None:
    out = _run_pkg_command(["rpm", "-qa", "--qf", "%{NAME} %{VERSION}\n"])
    if out is None:
        return None
    return _parse_two_column(out)


def _probe_port() -> list[dict[str, str]] | None:
    out = _run_pkg_command(["port", "installed"])
    if out is None:
        return None
    pkgs: list[dict[str, str]] = []
    for raw in out.splitlines():
        # `port installed` indents package lines; the header line
        # ("The following ports are currently installed:") starts at
        # column 0, so we filter it out by indentation.
        if not raw.startswith(" "):
            continue
        body = raw.strip()
        if " @" not in body:
            continue
        name, rest = body.split(" @", 1)
        # `@7.0.1_0 (active)` → "7.0.1"
        version = rest.split()[0].split("_", 1)[0]
        pkgs.append({"name": name, "version": version})
    return pkgs


def _probe_pacman() -> list[dict[str, str]] | None:
    out = _run_pkg_command(["pacman", "-Q"])
    if out is None:
        return None
    return _parse_two_column(out)


def _probe_winget() -> list[dict[str, str]] | None:
    out = _run_pkg_command(
        ["winget", "list", "--accept-source-agreements"]
    )
    if out is None:
        return None
    pkgs: list[dict[str, str]] = []
    # winget output is human-formatted with a header bar of `---`. We
    # skip lines until we see the bar, then take everything after.
    seen_bar = False
    for raw in out.splitlines():
        if not seen_bar:
            if raw.strip().startswith("---"):
                seen_bar = True
            continue
        cells = raw.split()
        if not cells:
            continue
        # Best-effort: first cell = name, second = id, third = version.
        # Some entries lack a separate id — handle gracefully.
        name = cells[0]
        version = cells[2] if len(cells) >= 3 else ""
        pkgs.append({"name": name, "version": version})
    return pkgs


def _probe_choco() -> list[dict[str, str]] | None:
    # `-r` (raw) yields `name|version` per line — much easier than the
    # default human format.
    out = _run_pkg_command(["choco", "list", "--local-only", "-r"])
    if out is None:
        return None
    pkgs: list[dict[str, str]] = []
    for raw in out.splitlines():
        line = raw.strip()
        if not line or "|" not in line:
            continue
        name, _, version = line.partition("|")
        pkgs.append({"name": name, "version": version})
    return pkgs


# Dispatch table — defined after the probe functions so we can name
# them directly. `apt` / `dpkg` are aliases (both produce dpkg output);
# `dnf` / `yum` / `rpm` all share the rpm probe since they query the
# same backing database.
_PKG_PROBES: dict[str, Any] = {
    "brew": _probe_brew,
    "apt": _probe_apt,
    "dpkg": _probe_apt,
    "dnf": _probe_rpm,
    "yum": _probe_rpm,
    "rpm": _probe_rpm,
    "port": _probe_port,
    "pacman": _probe_pacman,
    "winget": _probe_winget,
    "choco": _probe_choco,
}


def _parse_two_column(text: str) -> list[dict[str, str]]:
    """Parse `name version` whitespace-separated lines into entries."""
    pkgs: list[dict[str, str]] = []
    for raw in text.splitlines():
        parts = raw.strip().split(None, 1)
        if len(parts) == 2:
            pkgs.append({"name": parts[0], "version": parts[1]})
        elif len(parts) == 1 and parts[0]:
            pkgs.append({"name": parts[0], "version": ""})
    return pkgs


def _run_pkg_command(
    cmd: list[str], timeout: float = 30.0
) -> str | None:
    """Run a package-manager probe command, return stdout or None.

    Returns None if:
      - the binary isn't on PATH (manager not installed),
      - the command exits non-zero,
      - the command times out or otherwise raises OSError.

    A None return is the signal to omit the manager's entry from the
    capabilities JSON entirely — distinct from "manager exists but has
    zero packages installed", which yields an empty list.
    """
    if shutil.which(cmd[0]) is None:
        return None
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        _log.debug("capabilities: %s probe failed: %s", cmd[0], e)
        return None
    if result.returncode != 0:
        _log.debug(
            "capabilities: %s probe exited %d: %s",
            cmd[0],
            result.returncode,
            (result.stderr or "").strip()[:200],
        )
        return None
    return result.stdout


# ----------------------------------------------------------------- hardware


def probe_hardware() -> dict[str, Any]:
    """Collect static host facts: OS, arch, CPU, RAM.

    Cheap (a single sysctl/proc read) and the data rarely changes — but
    we re-collect on every cache miss anyway because it costs almost
    nothing and avoids surprising consumers if the kernel hot-swaps
    something exotic underneath us.
    """
    return {
        "os": platform.system().lower(),  # darwin / linux / windows
        "os_version": _os_version(),
        "arch": platform.machine().lower(),  # arm64 / x86_64 / amd64
        "cpu_model": _cpu_model(),
        "cores": os.cpu_count() or 0,
        "ram_gb": _ram_gb(),
    }


def _os_version() -> str:
    """Return the user-meaningful OS version string."""
    system = platform.system()
    if system == "Darwin":
        # `platform.release()` returns the Darwin kernel version
        # (e.g. "24.0.0") on macOS, which means nothing to humans.
        # `mac_ver()` is supposed to return the marketing version
        # ("14.5") but lies on Pythons built against pre-Big-Sur SDKs
        # (Anaconda 3.13 returns "10.16" here even on Sequoia). Shell
        # out to `sw_vers` first — it reads
        # /System/Library/CoreServices/SystemVersion.plist directly and
        # returns the actual product version regardless of binary
        # compat shims.
        try:
            r = subprocess.run(
                ["sw_vers", "-productVersion"],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            if r.returncode == 0:
                out = r.stdout.strip()
                if out:
                    return out
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            mv = platform.mac_ver()[0]
            if mv:
                return mv
        except Exception:  # noqa: BLE001 — defensive; mac_ver shouldn't raise
            pass
    return platform.release()


def _cpu_model() -> str:
    """Best-effort CPU brand string per platform."""
    system = platform.system()
    if system == "Darwin":
        try:
            r = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            if r.returncode == 0:
                out = r.stdout.strip()
                if out:
                    return out
        except (OSError, subprocess.SubprocessError):
            pass
    if system == "Linux":
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("model name"):
                        return line.split(":", 1)[1].strip()
        except OSError:
            pass
    # Windows / fallback: platform.processor() returns something
    # useful on Windows (e.g. "Intel64 Family 6 Model 142...") and
    # often empty on macOS — by which time we've returned above.
    return platform.processor() or ""


def _ram_gb() -> float:
    """Total physical RAM in GiB (rounded to 1 decimal)."""
    system = platform.system()
    if system == "Darwin":
        try:
            r = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            if r.returncode == 0:
                bytes_ = int(r.stdout.strip())
                return round(bytes_ / 1024**3, 1)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    if system == "Linux":
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        # MemTotal is in kB
                        kb = int(line.split()[1])
                        return round(kb / 1024**2, 1)
        except (OSError, ValueError):
            pass
    if system == "Windows":
        try:
            return _windows_ram_gb()
        except Exception:  # noqa: BLE001 — defensive; windows-only path
            pass
    return 0.0


def _windows_ram_gb() -> float:
    """Read total physical RAM via the Win32 API.

    Isolated so the ctypes import doesn't run on macOS / Linux where
    `ctypes.windll` doesn't exist.
    """
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    m = MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(m)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))  # type: ignore[attr-defined]
    return round(m.ullTotalPhys / 1024**3, 1)


# ----------------------------------------------------------------- software


def probe_software(
    names: list[str], *, system: str | None = None
) -> dict[str, str]:
    """Map each name to the first install path found.

    Lookup order:
      1. ``shutil.which(name)`` (PATH binaries; cross-platform).
      2. On macOS, the well-known ``/Applications/<Bundle>.app`` for
         a few names that ship as bundles without a CLI launcher
         (Chrome Canary, etc).

    Names with no hit are omitted from the returned dict — the
    consumer treats absence as "not installed."
    """
    sys_name = system if system is not None else platform.system()
    out: dict[str, str] = {}
    for name in names:
        path = shutil.which(name)
        if path:
            out[name] = path
            continue
        if sys_name == "Darwin":
            bundle = _MACOS_APP_BUNDLES.get(name)
            if bundle and os.path.isdir(bundle):
                out[name] = bundle
    return out


# ----------------------------------------------------------------- packages


def probe_packages(
    managers: list[str],
) -> dict[str, list[dict[str, str]]]:
    """Run each requested package manager's "list installed" probe.

    Managers not in :data:`SUPPORTED_PKG_MANAGERS` are silently
    skipped (CLI parsing rejects them up front, so this is a defensive
    no-op). Managers that aren't installed on the host are also
    skipped — the result dict only contains keys for managers we
    successfully queried, even if the result list is empty.
    """
    out: dict[str, list[dict[str, str]]] = {}
    for name in managers:
        probe = _PKG_PROBES.get(name)
        if probe is None:
            continue
        try:
            result = probe()
        except Exception:  # noqa: BLE001 — keep going on partial failure
            _log.exception(
                "capabilities: %s probe raised; skipping", name
            )
            continue
        if result is not None:
            out[name] = result
    return out


# ------------------------------------------------------------- aggregator


class CapabilitiesProbe:
    """Owns the list of probes and a small TTL cache.

    The cache exists because package-manager probes are expensive
    (`brew list` ≈ 1 s on a warm machine) and the capabilities
    endpoint is meant to be polled. Hardware/software probes are
    cheap but bundled into the same cache for simplicity — there's
    no separate "fast probe / slow probe" distinction at the API.
    """

    def __init__(
        self,
        *,
        software: list[str] | None = None,
        packages: list[str] | None = None,
        cache_ttl_s: float = 60.0,
    ) -> None:
        self._software_names = (
            list(software) if software is not None else list(DEFAULT_SOFTWARE_PROBES)
        )
        self._pkg_managers = list(packages) if packages else []
        self._cache_ttl_s = float(cache_ttl_s)
        self._lock = threading.Lock()
        self._cached: tuple[float, dict[str, Any]] | None = None

    @property
    def software_names(self) -> list[str]:
        return list(self._software_names)

    @property
    def package_managers(self) -> list[str]:
        return list(self._pkg_managers)

    def collect(self, *, force: bool = False) -> dict[str, Any]:
        """Return the cached capability snapshot, refreshing if stale."""
        with self._lock:
            now = time.monotonic()
            if not force and self._cached is not None:
                ts, data = self._cached
                if now - ts < self._cache_ttl_s:
                    return data
            data = self._collect_locked()
            self._cached = (now, data)
            return data

    def _collect_locked(self) -> dict[str, Any]:
        # Hold the lock through subprocess.run calls. They're slow but
        # the alternative — releasing and re-acquiring — would let two
        # concurrent callers double-probe, defeating the cache.
        return {
            "schema": CAPABILITIES_SCHEMA_VERSION,
            "host": probe_hardware(),
            "software": probe_software(self._software_names),
            "packages": probe_packages(self._pkg_managers),
            "generated_at": int(time.time()),
        }


def parse_pkg_managers(value: str) -> tuple[list[str], list[str]]:
    """Split a CLI ``--probe-pkg`` value into (accepted, unknown).

    The CLI rejects unknown names rather than silently dropping them —
    a typo like ``--probe-pkg pacmen`` should fail loudly. Returning
    both lists lets the caller print a useful error.
    """
    accepted: list[str] = []
    unknown: list[str] = []
    seen: set[str] = set()
    for raw in value.split(","):
        name = raw.strip().lower()
        if not name or name in seen:
            continue
        seen.add(name)
        if name in _PKG_PROBES:
            accepted.append(name)
        else:
            unknown.append(name)
    return accepted, unknown


def parse_software_list(value: str) -> list[str]:
    """Split a CLI ``--probe-software`` value into a deduped list."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in value.split(","):
        name = raw.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


__all__ = [
    "CAPABILITIES_SCHEMA_VERSION",
    "DEFAULT_SOFTWARE_PROBES",
    "SUPPORTED_PKG_MANAGERS",
    "CapabilitiesProbe",
    "parse_pkg_managers",
    "parse_software_list",
    "probe_hardware",
    "probe_packages",
    "probe_software",
]
