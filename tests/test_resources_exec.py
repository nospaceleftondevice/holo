"""Unit + integration tests for the Phase 2.A exec primitive.

Covers ``holo.resources_exec`` (body validator, PATH-pinning symlink
farm, ``exec_in_resource``) and the ``holo_exec_in_resource`` MCP tool
surface registered by ``holo.mcp_server.build_server``.

The validator tests are pure — no subprocesses. The exec tests do
spawn ``/bin/sh`` with small POSIX-portable bodies; they should run
on any macOS / Linux dev machine without extra setup.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from collections.abc import Iterator

import pytest

from holo.announce import Resource
from holo.resources_exec import (
    BodyRejected,
    ExecResult,
    _strip_heredocs,
    caps_exec_bins,
    exec_in_resource,
    pinned_path_dir,
    validate_body,
)

# --------------------------------------------------------- caps_exec_bins


class TestCapsExecBins:
    def test_extracts_bare_names(self) -> None:
        assert caps_exec_bins(
            ["exec:ffprobe", "exec:python3", "readonly", "uid:holo-x", "sandbox"]
        ) == frozenset({"ffprobe", "python3"})

    def test_empty(self) -> None:
        assert caps_exec_bins([]) == frozenset()

    def test_rejects_empty_name(self) -> None:
        with pytest.raises(ValueError, match="empty name"):
            caps_exec_bins(["exec:"])

    def test_rejects_slashed_name(self) -> None:
        with pytest.raises(ValueError, match="must be a bare"):
            caps_exec_bins(["exec:/usr/bin/curl"])


# ------------------------------------------------------------- validate_body


class TestValidateBody:
    @pytest.mark.parametrize(
        "body",
        [
            "",
            "   ",
            "ffprobe file.mp4",
            "ffprobe a.mp4 | wc -l",
            "for f in *.mp4; do ffprobe \"$f\"; done",
            "if true; then ffprobe a.mp4; fi",
            "cd subdir; echo done; export X=1",
            "FOO=bar BAZ=qux ffprobe a.mp4",
            "r=$(ffprobe a.mp4)",
            "(ffprobe a.mp4)",
            "echo \"a; b\"; ffprobe x.mp4",
            "# comment\nffprobe a.mp4",
        ],
    )
    def test_accepts(self, body: str) -> None:
        validate_body(
            body, allowed_bins={"ffprobe", "wc", "echo", "find"}
        )

    @pytest.mark.parametrize(
        "body,error_fragment",
        [
            ("rm -rf /tmp", "'rm'"),
            ("ffprobe /etc/passwd", "/etc/passwd"),
            ("ffprobe ../../etc/passwd", "../../etc/passwd"),
            ("ffprobe a.mp4 | rm -rf /tmp", "'rm'"),
            ("find /etc -name foo", "/etc"),
            ("ffprobe a.mp4 > /tmp/out", "/tmp/out"),
        ],
    )
    def test_rejects(self, body: str, error_fragment: str) -> None:
        with pytest.raises(BodyRejected) as exc:
            validate_body(body, allowed_bins={"ffprobe", "find"})
        assert error_fragment in str(exc.value)

    def test_heredoc_python_is_not_parsed_as_shell(self) -> None:
        # The 'rm /etc/passwd' inside the heredoc would be rejected if
        # the static parser walked it as shell. Heredoc body is DATA.
        body = (
            'python3 - <<"PY"\n'
            'import os\n'
            'rm -rf /etc\n'
            'PY'
        )
        validate_body(body, allowed_bins={"python3"})

    def test_test_builtin_is_NOT_in_keywords(self) -> None:
        # Documented in module: `test` and `[` are deliberately NOT
        # in the keyword skip-list because they read arbitrary paths.
        # If a user wants it, they declare caps=exec:test explicitly.
        with pytest.raises(BodyRejected):
            validate_body("test -f a.mp4", allowed_bins=set())
        validate_body("test -f a.mp4", allowed_bins={"test"})


# ------------------------------------------------------ _strip_heredocs


class TestStripHeredocs:
    def test_preserves_line_count(self) -> None:
        body = (
            'echo before\n'
            'python3 - <<"PY"\n'
            'line1\n'
            'line2\n'
            'PY\n'
            'echo after\n'
        )
        stripped = _strip_heredocs(body)
        assert body.count("\n") == stripped.count("\n")

    def test_replaces_content_with_blanks(self) -> None:
        body = (
            'python3 - <<"PY"\n'
            'rm /etc/passwd\n'
            'PY'
        )
        stripped = _strip_heredocs(body)
        assert "rm /etc/passwd" not in stripped

    def test_unterminated_heredoc_passes_through(self) -> None:
        # /bin/sh -c will error at exec time; the stripper shouldn't crash.
        body = 'python3 - <<"PY"\nsomething\n'
        _strip_heredocs(body)

    def test_no_heredoc_unchanged(self) -> None:
        body = "echo hi; cat a.txt"
        assert _strip_heredocs(body) == body


# ------------------------------------------------------- pinned_path_dir


class TestPinnedPathDir:
    def test_symlinks_only_allowed(self) -> None:
        with pinned_path_dir(["echo", "cat"]) as bindir:
            assert sorted(os.listdir(bindir)) == ["cat", "echo"]
            for name in os.listdir(bindir):
                full = os.path.join(bindir, name)
                assert os.path.islink(full)

    def test_cleanup_on_exit(self) -> None:
        with pinned_path_dir(["echo"]) as bindir:
            parent = os.path.dirname(bindir)
            assert os.path.exists(parent)
        assert not os.path.exists(parent)

    def test_empty_allowlist_still_creates_bindir(self) -> None:
        with pinned_path_dir([]) as bindir:
            assert os.path.exists(bindir)
            assert os.listdir(bindir) == []

    def test_missing_binary_fails_before_creating_dir(
        self, tmp_path: object
    ) -> None:
        existing = set(os.listdir(tempfile.gettempdir()))
        with pytest.raises(FileNotFoundError):
            with pinned_path_dir(["echo", "nonexistent-xyz"]):
                pass
        # No leftover holo-run-* dirs
        after = set(os.listdir(tempfile.gettempdir()))
        leftover = [
            d for d in after - existing if d.startswith("holo-run-")
        ]
        assert leftover == []

    def test_rejects_slashed_name(self) -> None:
        with pytest.raises(ValueError, match="must not contain '/'"):
            with pinned_path_dir(["/usr/bin/echo"]):
                pass


# --------------------------------------------------------- exec_in_resource


@pytest.fixture
def resource_dir() -> Iterator[str]:
    with tempfile.TemporaryDirectory() as d:
        # A few decoy files for find / cat / wc to operate on.
        for n in ("a.mp4", "b.mp4", "c.txt"):
            open(os.path.join(d, n), "w").close()
        yield d


def _frames(resource: Resource, body: str, **kwargs: object) -> tuple[
    list[dict], ExecResult
]:
    frames: list[dict] = []
    result = exec_in_resource(
        resource, body, on_frame=frames.append, **kwargs  # type: ignore[arg-type]
    )
    return frames, result


class TestExecInResource:
    def test_simple_command_exits_0(self, resource_dir: str) -> None:
        r = Resource(name="m", path=resource_dir, caps=("exec:wc",))
        frames, result = _frames(r, "wc -l < a.mp4", timeout_s=5)
        assert result.exit_code == 0
        assert result.timed_out is False
        assert frames[0]["fd"] == "stdout"

    def test_env_vars_injected(self, resource_dir: str) -> None:
        r = Resource(name="movies", path=resource_dir, caps=("exec:echo",))
        frames, result = _frames(
            r,
            'echo H=$HOLO_HOST R=$HOLO_RESOURCE P=$HOLO_RESOURCE_PATH',
            timeout_s=5,
        )
        assert result.exit_code == 0
        out = next(f["data"] for f in frames if f["fd"] == "stdout")
        assert "R=movies" in out
        assert f"P={resource_dir}" in out

    def test_cwd_pinned_to_resource_path(self, resource_dir: str) -> None:
        r = Resource(name="m", path=resource_dir, caps=("exec:cat",))
        frames, result = _frames(r, "cat a.mp4", timeout_s=5)
        # a.mp4 is in resource_dir; if cwd weren't pinned, cat would fail
        # to find it (the process inherits no other cwd hints).
        assert result.exit_code == 0

    def test_stderr_routed_to_stderr_frame(
        self, resource_dir: str
    ) -> None:
        r = Resource(name="m", path=resource_dir, caps=("exec:echo",))
        frames, _ = _frames(r, "echo hello; echo bye 1>&2", timeout_s=5)
        kinds = {f["fd"] for f in frames}
        assert "stdout" in kinds and "stderr" in kinds

    def test_disallowed_command_rejected_before_spawn(
        self, resource_dir: str
    ) -> None:
        r = Resource(name="m", path=resource_dir, caps=("exec:echo",))
        with pytest.raises(BodyRejected):
            _frames(r, "rm -rf .", timeout_s=5)

    def test_abs_path_rejected_before_spawn(
        self, resource_dir: str
    ) -> None:
        r = Resource(name="m", path=resource_dir, caps=("exec:cat",))
        with pytest.raises(BodyRejected):
            _frames(r, "cat /etc/passwd", timeout_s=5)

    def test_missing_binary_rejected_with_fnf(
        self, resource_dir: str
    ) -> None:
        r = Resource(
            name="m",
            path=resource_dir,
            caps=("exec:nonexistent-xyz",),
        )
        with pytest.raises(FileNotFoundError):
            _frames(r, "nonexistent-xyz", timeout_s=5)

    def test_missing_resource_path_rejected_with_fnf(self) -> None:
        r = Resource(name="m", path="/nonexistent/path/xyz")
        with pytest.raises(FileNotFoundError):
            _frames(r, "echo hi", timeout_s=5)

    def test_timeout_sigkills(self, resource_dir: str) -> None:
        # `while true; do :; done` uses no external binaries — just shell
        # builtins — so it busy-loops without needing any cap.
        r = Resource(name="m", path=resource_dir, caps=())
        start = time.monotonic()
        frames, result = _frames(
            r, "while true; do :; done", timeout_s=1
        )
        elapsed = time.monotonic() - start
        assert result.timed_out is True
        # Tolerance for thread join + pump drain.
        assert 0.5 < elapsed < 3.0
        # POSIX: SIGKILL'd process exits with -SIGKILL or 128+9; on
        # macOS Python's subprocess reports -9.
        assert result.exit_code in (-9, 137)


# ---------------------------------------- holo_exec_in_resource MCP tool


@pytest.fixture
def mcp_server_with_resource(resource_dir: str):
    from holo.mcp_server import build_server

    r = Resource(
        name="movies",
        path=resource_dir,
        tags=("video-files",),
        caps=("exec:find", "exec:wc", "exec:echo"),
    )
    mcp, holo = build_server(announce_resources=[r])
    try:
        yield mcp, holo
    finally:
        holo.shutdown()


def _call(mcp: object, name: str, args: dict[str, object]) -> dict:
    """Sync helper: invoke an MCP tool and return its structured body."""
    _, body = asyncio.run(mcp.call_tool(name, args))  # type: ignore[attr-defined]
    return body


class TestMCPToolSurface:
    def test_tool_present_when_resources_declared(
        self, mcp_server_with_resource: tuple
    ) -> None:
        mcp, _ = mcp_server_with_resource

        async def chk() -> list[str]:
            tools = await mcp.list_tools()
            return [t.name for t in tools]

        assert "holo_exec_in_resource" in asyncio.run(chk())

    def test_tool_absent_when_no_resources(self) -> None:
        from holo.mcp_server import build_server

        mcp, holo = build_server()
        try:
            async def chk() -> list[str]:
                tools = await mcp.list_tools()
                return [t.name for t in tools]

            assert "holo_exec_in_resource" not in asyncio.run(chk())
        finally:
            holo.shutdown()

    def test_success_returns_frames(
        self, mcp_server_with_resource: tuple
    ) -> None:
        mcp, _ = mcp_server_with_resource
        body = _call(mcp, "holo_exec_in_resource", {
            "resource": "movies",
            "body": "find . -name \"*.mp4\" | wc -l",
            "timeout_s": 5,
        })
        assert body["exit"] == 0
        assert body["timed_out"] is False
        assert len(body["frames"]) >= 1
        assert body["frames"][0]["fd"] == "stdout"

    def test_unknown_resource_returns_structured_error(
        self, mcp_server_with_resource: tuple
    ) -> None:
        mcp, _ = mcp_server_with_resource
        body = _call(mcp, "holo_exec_in_resource", {
            "resource": "nope",
            "body": "echo hi",
        })
        assert body["error"] == "unknown-resource"
        assert body["known"] == ["movies"]

    def test_body_rejected_returns_structured_error(
        self, mcp_server_with_resource: tuple
    ) -> None:
        mcp, _ = mcp_server_with_resource
        body = _call(mcp, "holo_exec_in_resource", {
            "resource": "movies",
            "body": "rm -rf .",
        })
        assert body["error"] == "body-rejected"
        assert "rm" in body["message"]

    def test_abs_path_returns_body_rejected(
        self, mcp_server_with_resource: tuple
    ) -> None:
        mcp, _ = mcp_server_with_resource
        body = _call(mcp, "holo_exec_in_resource", {
            "resource": "movies",
            "body": "find /etc -name foo",
        })
        assert body["error"] == "body-rejected"
