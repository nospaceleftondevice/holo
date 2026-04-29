"""Python client for the SikuliX Jython bridge.

Spawns `java -jar sikulixapi.jar -r bridge/bridge.py` as a subprocess
(stdio transport) and exchanges line-delimited JSON-RPC over its
stdin/stdout. Methods on `BridgeClient` map to the bridge's handlers:
`activate`, `click`, `key`, `type_text`. Used by `channel.py` to
replace the macOS-specific input pipeline in `_macos.py`.

A second transport — connecting to a remote bridge over TCP — is
designed in but not yet implemented; it'll land alongside the cross-
host work in Phase 3. The `BridgeClient` interface is independent of
transport so the caller doesn't have to know which is in use.

Resource resolution (where to find `sikulixapi.jar` and `bridge.py`):

    1. Explicit kwargs (`jar_path=`, `script_path=`)
    2. Env vars `HOLO_SIKULI_JAR`, `HOLO_BRIDGE_SCRIPT`
    3. PyInstaller's `sys._MEIPASS` (release builds bundle both)
    4. Repo-root fallback: `<repo>/vendor/sikulixapi.jar` and
       `<repo>/bridge/bridge.py` (development)

If the jar can't be found, `BridgeClient.start()` raises
`BridgeMissingError` so callers can surface a clean diagnostic
instead of an opaque `FileNotFoundError`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class BridgeError(RuntimeError):
    """Raised when the bridge returns a JSON-RPC error envelope."""

    def __init__(self, code: int, message: str, trace: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.trace = trace


class BridgeMissingError(RuntimeError):
    """Raised when sikulixapi.jar / bridge.py cannot be located."""


@dataclass
class BridgeClient:
    """Synchronous client for the Jython bridge over stdio.

    One process per `BridgeClient`. Requests are serialised by an
    internal lock — concurrent callers wait their turn. JVM startup
    is slow (~2–5 s) so callers should keep the same client around
    for the life of the daemon.
    """

    jar_path: Path | None = None
    script_path: Path | None = None
    java_path: str = "java"
    extra_jvm_args: tuple[str, ...] = ()
    default_timeout: float = 10.0

    _proc: subprocess.Popen[bytes] | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def start(self) -> None:
        """Spawn the JVM subprocess and run a `ping` to confirm liveness."""
        if self._proc is not None:
            return
        jar = self._resolve_jar()
        script = self._resolve_script()
        cmd = [
            self.java_path,
            *self.extra_jvm_args,
            "-jar",
            str(jar),
            "-r",
            str(script),
            "--",
            "--transport",
            "stdio",
        ]
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        # Sanity check — first request after spawn must succeed, otherwise
        # the JVM is wedged and we want to know now rather than at first use.
        self.request("ping", timeout=max(self.default_timeout, 30.0))

    def stop(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.stdin is not None:
                self._proc.stdin.close()
            self._proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()
        finally:
            self._proc = None

    # ---- request/response ------------------------------------------------

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send `method`/`params`, block for the response, return `result`."""
        if self._proc is None:
            self.start()
        assert self._proc is not None and self._proc.stdin is not None
        assert self._proc.stdout is not None

        rid = uuid.uuid4().hex
        envelope = {"id": rid, "method": method, "params": params or {}}
        line = (json.dumps(envelope) + "\n").encode("utf-8")

        with self._lock:
            try:
                self._proc.stdin.write(line)
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                raise BridgeError(-32000, "bridge stdin closed: " + str(e)) from e

            raw = self._proc.stdout.readline()
            if not raw:
                stderr_tail = b""
                if self._proc.stderr is not None:
                    try:
                        stderr_tail = self._proc.stderr.read1(4096) or b""
                    except (ValueError, OSError):
                        pass
                raise BridgeError(
                    -32001,
                    "bridge stdout closed; stderr tail: "
                    + stderr_tail.decode("utf-8", errors="replace"),
                )

        try:
            response = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise BridgeError(-32002, "bad response: " + str(e)) from e

        if response.get("id") != rid:
            raise BridgeError(
                -32003,
                "id mismatch: expected " + rid + ", got " + str(response.get("id")),
            )

        if "error" in response:
            err = response["error"]
            raise BridgeError(
                err.get("code", -32603),
                err.get("message", "unknown error"),
                err.get("trace"),
            )
        return response.get("result", {})

    # ---- convenience verbs ----------------------------------------------

    def ping(self) -> dict[str, Any]:
        return self.request("ping")

    def activate(self, name: str) -> dict[str, Any]:
        return self.request("app.activate", {"name": name})

    def click(
        self,
        x: float,
        y: float,
        *,
        modifiers: list[str] | None = None,
    ) -> dict[str, Any]:
        return self.request(
            "screen.click",
            {"x": int(x), "y": int(y), "modifiers": modifiers or []},
        )

    def key(self, combo: str) -> dict[str, Any]:
        return self.request("screen.key", {"combo": combo})

    def type_text(self, text: str) -> dict[str, Any]:
        # Avoid clashing with the `type` builtin in callers' namespaces.
        return self.request("screen.type", {"text": text})

    def screenshot(
        self,
        *,
        region: dict[str, int] | None = None,
        timeout: float = 15.0,
    ) -> bytes:
        """Capture the screen (or a region) and return raw PNG bytes."""
        import base64 as _b64

        params: dict[str, Any] = {}
        if region is not None:
            params["region"] = region
        result = self.request("screen.shot", params, timeout=timeout)
        return _b64.b64decode(result["image"])

    def find_image(
        self,
        needle: bytes,
        *,
        region: dict[str, int] | None = None,
        score: float = 0.7,
        timeout: float = 15.0,
    ) -> dict[str, Any] | None:
        """Find `needle` (PNG bytes) on screen. Returns coords/score or None."""
        import base64 as _b64

        params: dict[str, Any] = {
            "needle": _b64.b64encode(needle).decode("ascii"),
            "score": score,
        }
        if region is not None:
            params["region"] = region
        return self.request("screen.find_image", params, timeout=timeout)

    # ---- resource resolution --------------------------------------------

    def _resolve_jar(self) -> Path:
        if self.jar_path is not None:
            return _require(Path(self.jar_path), "sikulixapi.jar")
        env = os.environ.get("HOLO_SIKULI_JAR")
        if env:
            return _require(Path(env), "sikulixapi.jar (HOLO_SIKULI_JAR)")
        for candidate in _candidate_jar_paths():
            if candidate.exists():
                return candidate
        raise BridgeMissingError(
            "SikuliX jar not found. Set HOLO_SIKULI_JAR or drop "
            "sikulixapi-*.jar / sikulixide-*.jar into vendor/ at the repo root."
        )

    def _resolve_script(self) -> Path:
        if self.script_path is not None:
            return _require(Path(self.script_path), "bridge.py")
        env = os.environ.get("HOLO_BRIDGE_SCRIPT")
        if env:
            return _require(Path(env), "bridge.py (HOLO_BRIDGE_SCRIPT)")
        for candidate in _candidate_script_paths():
            if candidate.exists():
                return candidate
        raise BridgeMissingError("bridge.py not found among PyInstaller / repo paths")


def _require(path: Path, label: str) -> Path:
    if not path.exists():
        raise BridgeMissingError(label + " not found at " + str(path))
    return path


def _bundle_root() -> Path | None:
    """PyInstaller's `_MEIPASS` if the daemon is running from a frozen build."""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else None


def _repo_root() -> Path:
    # Walk up from this file looking for the repo's pyproject.toml.
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return here.parent


def _candidate_jar_paths() -> list[Path]:
    """Search order for the SikuliX jar.

    Both `sikulixapi-*.jar` (headless API) and `sikulixide-*.jar` (IDE
    distribution that bundles the API) work — `java -jar X -r script.py`
    accepts either. Prefer the slimmer api jar when both are present.
    """
    out: list[Path] = []
    for base in _jar_search_dirs():
        for pattern in ("sikulixapi*.jar", "sikulixide*.jar"):
            out.extend(sorted(base.glob(pattern)))
    return out


def _jar_search_dirs() -> list[Path]:
    """Where to look for the SikuliX jar, in priority order.

    1. The PyInstaller bundle root (release builds bundle the jar in).
    2. `<repo>/vendor/` (development).
    3. `~/.cache/holo/` (downloaded-on-demand from a GitHub Release;
       see `holo install-bridge`).
    """
    out: list[Path] = []
    bundle = _bundle_root()
    if bundle is not None:
        out.append(bundle)
        out.append(bundle / "vendor")
    out.append(_repo_root() / "vendor")
    out.append(_user_cache_dir())
    return [d for d in out if d.exists()]


def _user_cache_dir() -> Path:
    """Best-effort XDG-style cache path for downloaded jars.

    Matches `~/.cache/holo` on Linux / unset-XDG macOS; respects
    `XDG_CACHE_HOME` if set; falls back to `~/Library/Caches/holo`
    on macOS when `XDG_CACHE_HOME` isn't set and we're on a path
    where the Apple convention is more idiomatic. We keep this
    simple — the install command writes here, and the resolver
    reads here.
    """
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg) / "holo"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "holo"
    return Path.home() / ".cache" / "holo"


def _candidate_script_paths() -> list[Path]:
    out: list[Path] = []
    bundle = _bundle_root()
    if bundle is not None:
        out.append(bundle / "bridge.py")
        out.append(bundle / "bridge" / "bridge.py")
    repo = _repo_root()
    out.append(repo / "bridge" / "bridge.py")
    return out
