"""Unit tests for holo.capabilities and holo.capabilities_server.

We exercise the parsers (which we control), the cache TTL behaviour,
and the HTTP server's auth + no-CORS contract. The actual subprocess
invocations to package managers are mocked — the test box won't have
all of (brew, apt, port, winget, choco) installed at once.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import patch

import pytest

from holo.capabilities import (
    CAPABILITIES_SCHEMA_VERSION,
    DEFAULT_SOFTWARE_PROBES,
    SUPPORTED_PKG_MANAGERS,
    CapabilitiesProbe,
    parse_pkg_managers,
    parse_software_list,
    probe_hardware,
    probe_packages,
    probe_software,
)

# --------------------------------------------------------------------- parsers


class TestParseSoftwareList:
    def test_basic(self) -> None:
        assert parse_software_list("ffmpeg,ollama,git") == [
            "ffmpeg",
            "ollama",
            "git",
        ]

    def test_dedupe(self) -> None:
        assert parse_software_list("git,git,node") == ["git", "node"]

    def test_strip_whitespace(self) -> None:
        assert parse_software_list("  ffmpeg ,ollama  ,git") == [
            "ffmpeg",
            "ollama",
            "git",
        ]

    def test_empty_entries_dropped(self) -> None:
        assert parse_software_list("ffmpeg,,ollama,") == ["ffmpeg", "ollama"]

    def test_all_empty_returns_empty_list(self) -> None:
        assert parse_software_list(",,,") == []


class TestParsePkgManagers:
    def test_known_accepted(self) -> None:
        accepted, unknown = parse_pkg_managers("brew,apt,winget")
        assert accepted == ["brew", "apt", "winget"]
        assert unknown == []

    def test_unknown_separated(self) -> None:
        accepted, unknown = parse_pkg_managers("brew,pacmen,apt")
        assert accepted == ["brew", "apt"]
        assert unknown == ["pacmen"]

    def test_case_insensitive(self) -> None:
        accepted, _ = parse_pkg_managers("BREW,APT")
        assert accepted == ["brew", "apt"]

    def test_dedupe(self) -> None:
        accepted, _ = parse_pkg_managers("brew,brew,apt")
        assert accepted == ["brew", "apt"]


# -------------------------------------------------------------------- hardware


class TestProbeHardware:
    def test_returns_expected_keys(self) -> None:
        info = probe_hardware()
        for key in ("os", "os_version", "arch", "cpu_model", "cores", "ram_gb"):
            assert key in info, f"missing {key} in {info}"

    def test_cores_positive(self) -> None:
        info = probe_hardware()
        assert info["cores"] >= 1

    def test_os_lowercase(self) -> None:
        info = probe_hardware()
        # darwin / linux / windows — all single-word, lowercase tokens
        assert info["os"] == info["os"].lower()
        assert info["os"] in {"darwin", "linux", "windows"}


# -------------------------------------------------------------------- software


class TestProbeSoftware:
    def test_known_binary_found(self) -> None:
        # `ls` exists on macOS and Linux; `cmd` on Windows. Pick `ls`
        # since the test suite is macOS-first.
        result = probe_software(["ls"])
        assert "ls" in result
        assert result["ls"].endswith("/ls")

    def test_unknown_binary_omitted(self) -> None:
        result = probe_software(["definitely-not-installed-xyzzy"])
        assert result == {}

    def test_macos_app_bundle_fallback(self, tmp_path: Any) -> None:
        # Patch the bundle map so we don't depend on Chrome being
        # installed; build a fake .app dir and verify the lookup hits it.
        fake_app = tmp_path / "Fake.app"
        fake_app.mkdir()
        with patch.dict(
            "holo.capabilities._MACOS_APP_BUNDLES",
            {"fake-bundle": str(fake_app)},
        ):
            result = probe_software(["fake-bundle"], system="Darwin")
        assert result == {"fake-bundle": str(fake_app)}

    def test_macos_bundle_not_used_on_linux(self, tmp_path: Any) -> None:
        fake_app = tmp_path / "Fake.app"
        fake_app.mkdir()
        with patch.dict(
            "holo.capabilities._MACOS_APP_BUNDLES",
            {"fake-bundle": str(fake_app)},
        ):
            result = probe_software(["fake-bundle"], system="Linux")
        assert result == {}


# -------------------------------------------------------------------- packages


def _stub_run(stdout: str, returncode: int = 0) -> Any:
    """Build a CompletedProcess-like stub for subprocess.run."""

    class _R:
        def __init__(self, stdout: str, returncode: int) -> None:
            self.stdout = stdout
            self.returncode = returncode
            self.stderr = ""

    return _R(stdout, returncode)


class TestProbePackages:
    def test_brew_parsing(self) -> None:
        sample = "ffmpeg 7.0.1\naom 3.12.1\nbash 5.3.9\n"
        with patch("holo.capabilities.shutil.which", return_value="/x/brew"), \
             patch("holo.capabilities.subprocess.run", return_value=_stub_run(sample)):
            out = probe_packages(["brew"])
        assert out == {
            "brew": [
                {"name": "ffmpeg", "version": "7.0.1"},
                {"name": "aom", "version": "3.12.1"},
                {"name": "bash", "version": "5.3.9"},
            ]
        }

    def test_brew_multiple_versions_takes_first(self) -> None:
        # `brew list --versions` emits `name v1 v2 v3` for kegs with
        # multiple installed versions. We canonicalize on the first.
        sample = "openssl@3 3.4.1 3.4.0\n"
        with patch("holo.capabilities.shutil.which", return_value="/x/brew"), \
             patch("holo.capabilities.subprocess.run", return_value=_stub_run(sample)):
            out = probe_packages(["brew"])
        assert out["brew"] == [{"name": "openssl@3", "version": "3.4.1"}]

    def test_apt_parsing(self) -> None:
        sample = "bash 5.2.21-2\nzsh 5.9-6\n"
        with patch("holo.capabilities.shutil.which", return_value="/x/dpkg-query"), \
             patch("holo.capabilities.subprocess.run", return_value=_stub_run(sample)):
            out = probe_packages(["apt"])
        assert out == {
            "apt": [
                {"name": "bash", "version": "5.2.21-2"},
                {"name": "zsh", "version": "5.9-6"},
            ]
        }

    def test_rpm_parsing_via_dnf_alias(self) -> None:
        sample = "bash 5.2.21\nzsh 5.9\n"
        with patch("holo.capabilities.shutil.which", return_value="/x/rpm"), \
             patch("holo.capabilities.subprocess.run", return_value=_stub_run(sample)):
            out = probe_packages(["dnf"])
        assert out == {
            "dnf": [
                {"name": "bash", "version": "5.2.21"},
                {"name": "zsh", "version": "5.9"},
            ]
        }

    def test_port_parsing(self) -> None:
        sample = (
            "The following ports are currently installed:\n"
            "  ffmpeg @7.0.1_0 (active)\n"
            "  aom @3.12.1_0 (active)\n"
        )
        with patch("holo.capabilities.shutil.which", return_value="/x/port"), \
             patch("holo.capabilities.subprocess.run", return_value=_stub_run(sample)):
            out = probe_packages(["port"])
        assert out == {
            "port": [
                {"name": "ffmpeg", "version": "7.0.1"},
                {"name": "aom", "version": "3.12.1"},
            ]
        }

    def test_choco_parsing(self) -> None:
        sample = "git|2.45.0\nffmpeg|7.0.1\n"
        with patch("holo.capabilities.shutil.which", return_value=r"C:\x\choco.exe"), \
             patch("holo.capabilities.subprocess.run", return_value=_stub_run(sample)):
            out = probe_packages(["choco"])
        assert out == {
            "choco": [
                {"name": "git", "version": "2.45.0"},
                {"name": "ffmpeg", "version": "7.0.1"},
            ]
        }

    def test_winget_parsing(self) -> None:
        sample = (
            "Name      Id              Version\n"
            "----------------------------------\n"
            "Git       Git.Git         2.45.0\n"
            "Firefox   Mozilla.Firefox 125.0\n"
        )
        with patch("holo.capabilities.shutil.which", return_value=r"C:\x\winget.exe"), \
             patch("holo.capabilities.subprocess.run", return_value=_stub_run(sample)):
            out = probe_packages(["winget"])
        assert out == {
            "winget": [
                {"name": "Git", "version": "2.45.0"},
                {"name": "Firefox", "version": "125.0"},
            ]
        }

    def test_pacman_parsing(self) -> None:
        sample = "bash 5.2.21-1\nzsh 5.9-1\n"
        with patch("holo.capabilities.shutil.which", return_value="/x/pacman"), \
             patch("holo.capabilities.subprocess.run", return_value=_stub_run(sample)):
            out = probe_packages(["pacman"])
        assert out == {
            "pacman": [
                {"name": "bash", "version": "5.2.21-1"},
                {"name": "zsh", "version": "5.9-1"},
            ]
        }

    def test_missing_manager_omitted(self) -> None:
        with patch("holo.capabilities.shutil.which", return_value=None):
            out = probe_packages(["brew", "apt", "dnf"])
        assert out == {}

    def test_command_failure_omitted(self) -> None:
        with patch("holo.capabilities.shutil.which", return_value="/x/brew"), \
             patch(
                 "holo.capabilities.subprocess.run",
                 return_value=_stub_run("", returncode=1),
             ):
            out = probe_packages(["brew"])
        assert out == {}

    def test_unknown_manager_silently_skipped(self) -> None:
        # CLI parsing rejects unknowns up front; the runtime path is
        # defensive. Verify it doesn't blow up.
        out = probe_packages(["definitely-not-a-manager"])
        assert out == {}


# ------------------------------------------------------------------- aggregator


class TestCapabilitiesProbe:
    def test_collect_returns_schema(self) -> None:
        probe = CapabilitiesProbe(software=[], packages=[])
        snap = probe.collect()
        assert snap["schema"] == CAPABILITIES_SCHEMA_VERSION
        assert "host" in snap
        assert "software" in snap
        assert "packages" in snap
        assert "generated_at" in snap

    def test_default_software_list(self) -> None:
        probe = CapabilitiesProbe()
        assert probe.software_names == list(DEFAULT_SOFTWARE_PROBES)

    def test_cache_returns_same_dict_within_ttl(self) -> None:
        probe = CapabilitiesProbe(software=[], packages=[], cache_ttl_s=60.0)
        a = probe.collect()
        b = probe.collect()
        # Same identity — second call returned the cached object.
        assert a is b

    def test_cache_refreshes_after_ttl(self) -> None:
        probe = CapabilitiesProbe(software=[], packages=[], cache_ttl_s=0.05)
        a = probe.collect()
        time.sleep(0.08)
        b = probe.collect()
        assert a is not b

    def test_force_bypasses_cache(self) -> None:
        probe = CapabilitiesProbe(software=[], packages=[], cache_ttl_s=60.0)
        a = probe.collect()
        b = probe.collect(force=True)
        assert a is not b


# ------------------------------------------------------------------ HTTP server


@pytest.fixture
def caps_app() -> Any:
    """Build the Starlette app via TestClient — no real socket needed."""
    from starlette.testclient import TestClient

    from holo.capabilities import CapabilitiesProbe
    from holo.capabilities_server import CapabilitiesServer

    server = CapabilitiesServer(
        probe=CapabilitiesProbe(software=[], packages=[]),
        token="test-token-fixed",
    )
    client = TestClient(server._build_app())  # noqa: SLF001 — test surface
    return server, client


class TestCapabilitiesServerHTTP:
    def test_healthz_no_auth(self, caps_app: Any) -> None:
        _, client = caps_app
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

    def test_healthz_returns_no_fingerprint_data(self, caps_app: Any) -> None:
        # /healthz must NOT include host name, interfaces, or anything
        # else that a LAN scanner could use to identify holo hosts.
        _, client = caps_app
        body = client.get("/healthz").json()
        # Only the status field should be present.
        assert set(body.keys()) == {"status"}

    def test_capabilities_no_token_401(self, caps_app: Any) -> None:
        _, client = caps_app
        r = client.get("/capabilities")
        assert r.status_code == 401
        assert r.json() == {"error": "unauthorized"}

    def test_capabilities_wrong_token_401(self, caps_app: Any) -> None:
        _, client = caps_app
        r = client.get(
            "/capabilities", headers={"X-Holo-Caps-Token": "wrong-token"}
        )
        assert r.status_code == 401

    def test_capabilities_right_token_200(self, caps_app: Any) -> None:
        _, client = caps_app
        r = client.get(
            "/capabilities",
            headers={"X-Holo-Caps-Token": "test-token-fixed"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["schema"] == CAPABILITIES_SCHEMA_VERSION
        assert "host" in body

    def test_no_cors_headers_on_capabilities(self, caps_app: Any) -> None:
        # The browser-block defense relies on the absence of these
        # headers. If a maintainer adds a CORSMiddleware later this
        # test will catch it.
        _, client = caps_app
        r = client.get(
            "/capabilities",
            headers={"X-Holo-Caps-Token": "test-token-fixed"},
        )
        for header in (
            "access-control-allow-origin",
            "access-control-allow-headers",
            "access-control-allow-methods",
        ):
            assert header not in {k.lower() for k in r.headers}

    def test_no_cors_headers_on_healthz(self, caps_app: Any) -> None:
        _, client = caps_app
        r = client.get("/healthz")
        for header in (
            "access-control-allow-origin",
            "access-control-allow-headers",
            "access-control-allow-methods",
        ):
            assert header not in {k.lower() for k in r.headers}

    def test_token_compare_constant_time(self, caps_app: Any) -> None:
        # Verify we're calling secrets.compare_digest, not raw `==`.
        # Patch compare_digest to a sentinel that records calls.
        server, client = caps_app
        called = {"n": 0}

        def _watcher(a: str, b: str) -> bool:
            called["n"] += 1
            return a == b

        with patch("holo.capabilities_server.secrets.compare_digest", _watcher):
            client.get(
                "/capabilities",
                headers={"X-Holo-Caps-Token": "test-token-fixed"},
            )
        assert called["n"] >= 1


class TestPkgManagerCoverage:
    """Sanity check: every name in SUPPORTED_PKG_MANAGERS resolves to a probe."""

    def test_every_supported_manager_has_a_probe(self) -> None:
        from holo.capabilities import _PKG_PROBES

        for name in SUPPORTED_PKG_MANAGERS:
            assert name in _PKG_PROBES, (
                f"{name} is in SUPPORTED_PKG_MANAGERS but has no probe entry"
            )
