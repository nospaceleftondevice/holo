"""Unit tests for `holo.remote_input.RemoteInputBackend`.

The MCP `ClientSession` and the `stdio_client` async context manager
are replaced with in-process fakes. Goals:

  - Verify the sync facade routes each method to the right tool name
    with the right argument shape.
  - Verify CallToolResult unwrapping handles `structuredContent`,
    falls back to text JSON, and propagates `isError=true`.
  - Verify start() raises a clean error when the session fails to
    initialize, and stop() is idempotent.

No real subprocess is spawned and no real network I/O occurs.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from holo import remote_input
from holo.remote_input import RemoteInputBackend, RemoteInputError

# --- fakes ---------------------------------------------------------------


class _FakeCallToolResult:
    def __init__(
        self,
        *,
        structuredContent: dict[str, Any] | None = None,
        content: list[Any] | None = None,
        isError: bool = False,
    ) -> None:
        self.structuredContent = structuredContent
        self.content = content or []
        self.isError = isError


class _FakeTextContent:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeClientSession:
    """Records call_tool invocations; returns canned results.

    The mock is shared across the test via `_current` so the test can
    set up canned responses and inspect calls after the backend runs.
    """

    _current: _FakeClientSession | None = None

    def __init__(self, read: Any, write: Any) -> None:
        self.read = read
        self.write = write
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.canned: dict[str, _FakeCallToolResult] = {}
        self.canned_exc: dict[str, BaseException] = {}
        self.init_called = False
        self.initialize_raises: BaseException | None = None
        _FakeClientSession._current = self

    async def __aenter__(self) -> _FakeClientSession:
        return self

    async def __aexit__(self, *a: Any) -> bool:
        return False

    async def initialize(self) -> None:
        if self.initialize_raises is not None:
            raise self.initialize_raises
        self.init_called = True

    async def call_tool(self, name: str, params: dict[str, Any]) -> _FakeCallToolResult:
        self.calls.append((name, params))
        if name in self.canned_exc:
            raise self.canned_exc[name]
        return self.canned.get(name, _FakeCallToolResult(structuredContent={}))


class _FakeStdioClient:
    """Async context manager replacement for stdio_client."""

    last_params: Any = None

    def __init__(self, params: Any) -> None:
        _FakeStdioClient.last_params = params

    async def __aenter__(self) -> tuple[Any, Any]:
        return object(), object()  # opaque read/write tokens

    async def __aexit__(self, *a: Any) -> bool:
        return False


@pytest.fixture(autouse=True)
def _patch_mcp_client(monkeypatch):
    """Replace the MCP client surface with in-process fakes for every test
    in this module. Each test gets a fresh `_FakeClientSession` accessible
    via `_FakeClientSession._current` after `backend.start()` completes.
    """
    monkeypatch.setattr(
        remote_input, "stdio_client", lambda params: _FakeStdioClient(params)
    )
    monkeypatch.setattr(remote_input, "ClientSession", _FakeClientSession)
    _FakeClientSession._current = None
    _FakeStdioClient.last_params = None
    yield


@pytest.fixture
def started_backend():
    """Start a backend, hand it to the test, tear it down on exit."""
    backend = RemoteInputBackend("test-host", 7777)
    backend.start()
    try:
        yield backend
    finally:
        backend.stop()


# --- input method routing -----------------------------------------------


def test_click_calls_screen_click_tool(started_backend):
    sess = _FakeClientSession._current
    sess.canned["screen_click"] = _FakeCallToolResult(
        structuredContent={"clicked": True, "x": 100, "y": 200}
    )

    result = started_backend.click(100, 200)

    assert result == {"clicked": True, "x": 100, "y": 200}
    assert sess.calls == [("screen_click", {"x": 100, "y": 200, "modifiers": []})]


def test_click_with_modifiers(started_backend):
    started_backend.click(50, 60, modifiers=["cmd", "shift"])
    sess = _FakeClientSession._current
    assert sess.calls[-1] == (
        "screen_click", {"x": 50, "y": 60, "modifiers": ["cmd", "shift"]}
    )


def test_key_calls_screen_key(started_backend):
    started_backend.key("cmd+s")
    sess = _FakeClientSession._current
    assert sess.calls == [("screen_key", {"combo": "cmd+s"})]


def test_type_text_calls_screen_type(started_backend):
    started_backend.type_text("hello world")
    sess = _FakeClientSession._current
    assert sess.calls == [("screen_type", {"text": "hello world"})]


def test_scroll_calls_screen_scroll_with_defaults(started_backend):
    started_backend.scroll(300, 400)
    sess = _FakeClientSession._current
    assert sess.calls == [
        ("screen_scroll", {"x": 300, "y": 400, "direction": "down", "steps": 3})
    ]


def test_scroll_with_explicit_args(started_backend):
    started_backend.scroll(10, 20, direction="up", steps=5)
    sess = _FakeClientSession._current
    assert sess.calls[-1] == (
        "screen_scroll", {"x": 10, "y": 20, "direction": "up", "steps": 5}
    )


def test_activate_calls_app_activate(started_backend):
    started_backend.activate("Google Chrome")
    sess = _FakeClientSession._current
    assert sess.calls == [("app_activate", {"name": "Google Chrome"})]


# --- CallToolResult unwrapping -------------------------------------------


def test_structured_content_returned_verbatim(started_backend):
    sess = _FakeClientSession._current
    sess.canned["screen_click"] = _FakeCallToolResult(
        structuredContent={"clicked": True, "x": 1, "y": 2}
    )
    assert started_backend.click(1, 2) == {"clicked": True, "x": 1, "y": 2}


def test_falls_back_to_parsing_text_json(started_backend):
    """When the remote doesn't populate structuredContent we parse the
    first text block as JSON."""
    sess = _FakeClientSession._current
    sess.canned["screen_key"] = _FakeCallToolResult(
        content=[_FakeTextContent('{"sent": "cmd+s"}')]
    )
    assert started_backend.key("cmd+s") == {"sent": "cmd+s"}


def test_non_json_text_wrapped(started_backend):
    sess = _FakeClientSession._current
    sess.canned["screen_key"] = _FakeCallToolResult(
        content=[_FakeTextContent("plain text reply")]
    )
    assert started_backend.key("cmd+s") == {"text": "plain text reply"}


def test_empty_result_returns_empty_dict(started_backend):
    sess = _FakeClientSession._current
    sess.canned["screen_key"] = _FakeCallToolResult()
    assert started_backend.key("cmd+s") == {}


def test_is_error_raises_remote_input_error(started_backend):
    sess = _FakeClientSession._current
    sess.canned["screen_click"] = _FakeCallToolResult(
        isError=True,
        content=[_FakeTextContent("permission denied")],
    )
    with pytest.raises(RemoteInputError, match="permission denied"):
        started_backend.click(1, 2)


def test_remote_call_exception_surfaces_as_remote_input_error(started_backend):
    sess = _FakeClientSession._current
    sess.canned_exc["screen_type"] = ConnectionResetError("peer closed")
    with pytest.raises(RemoteInputError, match="peer closed"):
        started_backend.type_text("x")


# --- lifecycle -----------------------------------------------------------


def test_start_passes_holo_connect_args_to_subprocess():
    """The `holo` invocation in the spawned connect subprocess should
    target the configured HOST:PORT."""
    backend = RemoteInputBackend("10.0.0.5", 7081, holo_exe="/usr/local/bin/holo")
    backend.start()
    try:
        params = _FakeStdioClient.last_params
        assert params.command == "/usr/local/bin/holo"
        assert params.args == ["connect", "10.0.0.5:7081"]
    finally:
        backend.stop()


def test_start_raises_on_initialize_failure(monkeypatch):
    """If the remote rejects the initialize handshake (bad magic prefix,
    wrong holo version, peer not listening), start() raises rather than
    leaving the daemon half-up."""

    class _BoomSession(_FakeClientSession):
        async def initialize(self) -> None:
            raise ConnectionRefusedError("nothing on port 7777")

    monkeypatch.setattr(remote_input, "ClientSession", _BoomSession)

    backend = RemoteInputBackend("h", 7777)
    with pytest.raises(RemoteInputError, match="nothing on port 7777"):
        backend.start()


def test_stop_is_idempotent(started_backend):
    started_backend.stop()
    started_backend.stop()  # no second teardown, no exception


def test_call_after_stop_raises(started_backend):
    started_backend.stop()
    with pytest.raises(RemoteInputError, match="already stopped"):
        started_backend.click(1, 2)


def test_start_is_idempotent(started_backend):
    """A second start() while already running is a no-op (doesn't spawn
    a second subprocess)."""
    first_session = _FakeClientSession._current
    started_backend.start()  # no-op
    assert _FakeClientSession._current is first_session


# --- multi-threaded use --------------------------------------------------


def test_concurrent_calls_from_threads_dont_deadlock(started_backend):
    """The sync facade marshals coroutines onto the dedicated event-loop
    thread; multiple sync callers from different threads should all
    complete without serializing through a per-call asyncio.run()."""
    sess = _FakeClientSession._current
    sess.canned["screen_click"] = _FakeCallToolResult(
        structuredContent={"clicked": True}
    )

    results: list[Any] = []
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            results.append(started_backend.click(i, i))
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    assert errors == [], f"unexpected errors: {errors}"
    assert len(results) == 8
    assert len(sess.calls) == 8
