"""Tests for the Phase 2.B TOML loader + CLI integration + holo_list_resources.

Covers:
  - ``holo.resources_config.load_resources_toml`` — schema, errors,
    list-of-strings validation, Resource validation passthrough.
  - ``holo mcp --resources-config`` CLI plumbing — value required,
    mutex with --announce-resource, structured errors on missing /
    malformed config.
  - ``holo_list_resources`` MCP method — payload shape and conditional
    registration matching the existing holo_exec_in_resource pattern.
  - ``allow_principals`` round-trips through Resource, the TOML loader,
    the HTTP /v1/resources endpoint, and the MCP list_resources tool.

The TOML loader is pure stdlib (``tomllib``); the MCP tests use the
same in-process FastMCP harness as test_resources_exec.py.
"""

from __future__ import annotations

import asyncio
import io
from contextlib import redirect_stderr
from pathlib import Path

import pytest

from holo.announce import Resource
from holo.resources_config import (
    DEFAULT_CONFIG_PATH,
    ResourcesConfigError,
    load_resources_toml,
)

# ---------------------------------------------------- TOML loader: success


class TestLoadResourcesToml:
    def _write(self, content: str, tmp_path: Path) -> Path:
        p = tmp_path / "resources.toml"
        p.write_text(content)
        return p

    def test_default_path_is_under_config_holo(self) -> None:
        assert str(DEFAULT_CONFIG_PATH).endswith(
            ".config/holo/resources.toml"
        )

    def test_full_spec_parses(self, tmp_path: Path) -> None:
        p = self._write(
            """
[resources.movies]
path = "/Volumes/movies"
tags = ["video-files", "archive"]
caps = ["exec:ffprobe", "exec:python3", "readonly"]
allow_principals = ["alice@laptop", "bob@office"]

[resources.photos]
path = "/Users/me/Photos"
tags = ["photos"]
""",
            tmp_path,
        )
        resources = load_resources_toml(p)
        assert len(resources) == 2
        m, p2 = resources
        assert m == Resource(
            name="movies",
            path="/Volumes/movies",
            tags=("video-files", "archive"),
            caps=("exec:ffprobe", "exec:python3", "readonly"),
            allow_principals=("alice@laptop", "bob@office"),
        )
        assert p2 == Resource(
            name="photos",
            path="/Users/me/Photos",
            tags=("photos",),
        )

    def test_minimal_spec_only_path(self, tmp_path: Path) -> None:
        p = self._write(
            """
[resources.basic]
path = "/some/path"
""",
            tmp_path,
        )
        resources = load_resources_toml(p)
        assert resources == [Resource(name="basic", path="/some/path")]

    def test_empty_resources_table(self, tmp_path: Path) -> None:
        p = self._write("[resources]\n", tmp_path)
        # Empty [resources] table is legitimate: declares "no resources".
        assert load_resources_toml(p) == []

    def test_order_preserved(self, tmp_path: Path) -> None:
        # TOML preserves insertion order; the loader must too so the
        # daemon's announce sees the operator's intended order.
        p = self._write(
            """
[resources.zulu]
path = "/z"

[resources.alpha]
path = "/a"

[resources.mike]
path = "/m"
""",
            tmp_path,
        )
        names = [r.name for r in load_resources_toml(p)]
        assert names == ["zulu", "alpha", "mike"]


# ----------------------------------------------------- TOML loader: errors


class TestLoadResourcesTomlErrors:
    def _write(self, content: str, tmp_path: Path) -> Path:
        p = tmp_path / "resources.toml"
        p.write_text(content)
        return p

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ResourcesConfigError, match="not found"):
            load_resources_toml(tmp_path / "nonexistent.toml")

    def test_malformed_toml(self, tmp_path: Path) -> None:
        p = self._write("not = valid = toml", tmp_path)
        with pytest.raises(ResourcesConfigError, match="not valid TOML"):
            load_resources_toml(p)

    def test_missing_resources_table(self, tmp_path: Path) -> None:
        p = self._write('foo = "bar"', tmp_path)
        with pytest.raises(
            ResourcesConfigError, match="missing top-level"
        ):
            load_resources_toml(p)

    def test_entry_not_a_table(self, tmp_path: Path) -> None:
        # [resources] with a scalar value — not a sub-table.
        p = self._write('[resources]\nfoo = "bar"', tmp_path)
        with pytest.raises(
            ResourcesConfigError, match=r"\[resources\.foo\] must be a table"
        ):
            load_resources_toml(p)

    def test_missing_path(self, tmp_path: Path) -> None:
        p = self._write(
            """
[resources.a]
tags = ["t"]
""",
            tmp_path,
        )
        with pytest.raises(
            ResourcesConfigError, match=r"missing required key 'path'"
        ):
            load_resources_toml(p)

    def test_unknown_key(self, tmp_path: Path) -> None:
        p = self._write(
            """
[resources.a]
path = "/x"
bogus = 1
""",
            tmp_path,
        )
        with pytest.raises(
            ResourcesConfigError, match="unknown keys"
        ):
            load_resources_toml(p)

    def test_list_with_non_strings(self, tmp_path: Path) -> None:
        p = self._write(
            """
[resources.a]
path = "/x"
tags = [1, 2]
""",
            tmp_path,
        )
        with pytest.raises(
            ResourcesConfigError, match="must be a list of strings"
        ):
            load_resources_toml(p)

    def test_resource_validation_fails(self, tmp_path: Path) -> None:
        # Syntactically valid TOML, but the tag value violates
        # Resource invariants (comma in tag).
        p = self._write(
            """
[resources.a]
path = "/x"
tags = ["bad,tag"]
""",
            tmp_path,
        )
        with pytest.raises(
            ResourcesConfigError, match=r"\[resources\.a\].*may not contain"
        ):
            load_resources_toml(p)


# ----------------------------------------------------- CLI plumbing


class TestCLIResourcesConfig:
    def _rc_and_err(self, argv: list[str]) -> tuple[int, str]:
        from holo.cli import main

        err = io.StringIO()
        with redirect_stderr(err):
            rc = main(argv)
        return rc, err.getvalue()

    def test_requires_announce(self) -> None:
        rc, msg = self._rc_and_err(
            ["mcp", "--resources-config", "/tmp/foo.toml"]
        )
        assert rc == 2
        assert "require --announce" in msg

    def test_requires_a_value(self) -> None:
        rc, msg = self._rc_and_err(["mcp", "--announce", "--resources-config"])
        assert rc == 2
        assert "--resources-config requires a path" in msg

    def test_mutually_exclusive_with_announce_resource(self) -> None:
        rc, msg = self._rc_and_err([
            "mcp", "--announce",
            "--announce-resource", "name=a;path=/x",
            "--resources-config", "/tmp/foo.toml",
        ])
        assert rc == 2
        assert "mutually exclusive" in msg

    def test_missing_file_returns_2(self) -> None:
        rc, msg = self._rc_and_err([
            "mcp", "--announce", "--resources-config", "/nonexistent.toml"
        ])
        assert rc == 2
        assert "not found" in msg

    def test_help_includes_flag(self) -> None:
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            from holo.cli import _print_help

            _print_help()
        text = buf.getvalue()
        assert "--resources-config" in text


# --------------------------------------------- holo_list_resources MCP tool


@pytest.fixture
def server_with_resources():
    from holo.mcp_server import build_server

    r1 = Resource(
        name="movies",
        path="/Volumes/movies",
        tags=("video-files",),
        caps=("exec:ffprobe",),
        allow_principals=("alice@laptop",),
    )
    r2 = Resource(name="photos", path="/p", tags=("photos",))
    mcp, holo = build_server(announce_resources=[r1, r2])
    try:
        yield mcp, holo
    finally:
        holo.shutdown()


def _call(mcp: object, name: str, args: dict) -> dict:
    _, body = asyncio.run(mcp.call_tool(name, args))  # type: ignore[attr-defined]
    return body


class TestHoloListResources:
    def test_registered_when_resources_declared(
        self, server_with_resources: tuple
    ) -> None:
        mcp, _ = server_with_resources

        async def chk() -> list[str]:
            tools = await mcp.list_tools()
            return [t.name for t in tools]

        assert "holo_list_resources" in asyncio.run(chk())

    def test_not_registered_when_no_resources(self) -> None:
        from holo.mcp_server import build_server

        mcp, holo = build_server()
        try:
            async def chk() -> list[str]:
                tools = await mcp.list_tools()
                return [t.name for t in tools]

            assert "holo_list_resources" not in asyncio.run(chk())
        finally:
            holo.shutdown()

    def test_returns_full_records_with_principals(
        self, server_with_resources: tuple
    ) -> None:
        mcp, _ = server_with_resources
        body = _call(mcp, "holo_list_resources", {})
        assert body == {
            "resources": [
                {
                    "name": "movies",
                    "path": "/Volumes/movies",
                    "tags": ["video-files"],
                    "caps": ["exec:ffprobe"],
                    "allow_principals": ["alice@laptop"],
                },
                {
                    "name": "photos",
                    "path": "/p",
                    "tags": ["photos"],
                    "caps": [],
                    "allow_principals": [],
                },
            ],
        }


# -------------------------- TOML → CLI → server end-to-end smoke


class TestEndToEnd:
    def test_toml_config_loads_into_server(self, tmp_path: Path) -> None:
        """TOML file → CLI flag parser → build_server → resources visible."""
        from holo.mcp_server import build_server

        cfg = tmp_path / "resources.toml"
        cfg.write_text(
            """
[resources.movies]
path = "/Volumes/movies"
caps = ["exec:ffprobe"]
"""
        )
        # We can't fully drive `holo mcp` without starting the server,
        # but we can replay the load logic the CLI uses:
        resources = list(load_resources_toml(cfg))
        mcp, holo = build_server(announce_resources=resources)
        try:
            assert "movies" in holo._resource_by_name

            async def chk() -> dict:
                _, body = await mcp.call_tool("holo_list_resources", {})
                return body

            body = asyncio.run(chk())
            assert body["resources"][0]["name"] == "movies"
        finally:
            holo.shutdown()


# -------------------------- /v1/resources HTTP — allow_principals visible


def test_http_endpoint_surfaces_allow_principals(tmp_path: Path) -> None:
    """Phase 1's /v1/resources picks up the new field too."""
    import json
    import urllib.request

    from holo.capabilities import CapabilitiesProbe
    from holo.capabilities_server import CAPS_TOKEN_HEADER, CapabilitiesServer

    r = Resource(
        name="m",
        path="/x",
        caps=("exec:ffprobe",),
        allow_principals=("alice@laptop", "bob@office"),
    )
    srv = CapabilitiesServer(
        probe=CapabilitiesProbe(),
        host="127.0.0.1",
        port=0,
        resources=[r],
    )
    srv.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{srv.actual_port}/v1/resources",
            headers={CAPS_TOKEN_HEADER: srv.token},
        )
        with urllib.request.urlopen(req, timeout=2.0) as resp:  # noqa: S310
            body = json.loads(resp.read())
        assert body["resources"][0]["allow_principals"] == [
            "alice@laptop",
            "bob@office",
        ]
    finally:
        srv.stop()
