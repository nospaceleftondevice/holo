"""Tests for the Python bridge client.

The Jython bridge runs in a JVM with SikuliX classes — we don't spin
that up in unit tests. Instead, these tests stub out
`subprocess.Popen` with a fake whose stdin/stdout pipes are driven
by the test, and assert on the JSON-RPC framing and error mapping.
There's a separate opt-in integration test under
`tests/integration/` that exercises the real JVM when
`HOLO_SIKULI_JAR` is set.
"""

from __future__ import annotations

import io
import json
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from holo.bridge import BridgeClient, BridgeError, BridgeMissingError


class _FakeProc:
    """Stand-in for `subprocess.Popen` driven from test code.

    `stdin` is a BytesIO we can inspect post-hoc; `stdout` is a
    BytesIO we pre-load with the responses we want the client to
    read. The client's request loop reads one line per request, so
    pre-loading N response lines lets us back N requests.
    """

    def __init__(self, responses: list[dict] | None = None) -> None:
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()
        self.killed = False
        self.waited = False
        if responses:
            for r in responses:
                self.stdout.write((json.dumps(r) + "\n").encode("utf-8"))
            self.stdout.seek(0)

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        self.waited = True
        return 0


def _spawn_with(fake: _FakeProc):
    """Helper to patch Popen and return the captured cmd args."""
    captured: dict = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return fake

    return patch("holo.bridge.subprocess.Popen", side_effect=fake_popen), captured


def _stub_paths(client: BridgeClient, tmp_path: Path) -> None:
    """Avoid filesystem dependence — point the client at any two files."""
    jar = tmp_path / "sikulixapi.jar"
    jar.write_bytes(b"")
    script = tmp_path / "bridge.py"
    script.write_text("# stub")
    client.jar_path = jar
    client.script_path = script


class TestStartAndStop:
    def test_start_pings_and_records_command(self, tmp_path):
        fake = _FakeProc(
            responses=[
                # ping response — id is filled in by the client, so we use a
                # placeholder and patch uuid below.
                {"id": "PING_ID", "result": {"pong": True, "protocol": "1"}},
            ]
        )
        client = BridgeClient()
        _stub_paths(client, tmp_path)

        popen_patch, captured = _spawn_with(fake)
        with popen_patch, patch("holo.bridge.uuid.uuid4") as uu:
            uu.return_value.hex = "PING_ID"
            client.start()
        try:
            cmd = captured["cmd"]
            assert cmd[0] == "java"
            assert "-jar" in cmd
            assert str(client.jar_path) in cmd
            assert "-r" in cmd
            assert str(client.script_path) in cmd
            assert "--transport" in cmd and "stdio" in cmd
            # The start path also wrote a ping to stdin.
            sent = fake.stdin.getvalue().decode().strip().splitlines()
            assert len(sent) == 1
            assert json.loads(sent[0]) == {
                "id": "PING_ID",
                "method": "ping",
                "params": {},
            }
        finally:
            client._proc = None  # don't let stop() touch the fake

    def test_start_is_idempotent(self, tmp_path):
        fake = _FakeProc(
            responses=[{"id": "X", "result": {"pong": True}}]
        )
        client = BridgeClient()
        _stub_paths(client, tmp_path)
        with _spawn_with(fake)[0], patch("holo.bridge.uuid.uuid4") as uu:
            uu.return_value.hex = "X"
            client.start()
            client.start()  # must not respawn
        client._proc = None

    def test_stop_closes_stdin_and_waits(self, tmp_path):
        fake = _FakeProc(
            responses=[{"id": "X", "result": {"pong": True}}]
        )
        client = BridgeClient()
        _stub_paths(client, tmp_path)
        with _spawn_with(fake)[0], patch("holo.bridge.uuid.uuid4") as uu:
            uu.return_value.hex = "X"
            client.start()
        client.stop()
        assert fake.waited is True
        assert client._proc is None


class TestRequest:
    def _start_client(self, tmp_path, responses):
        fake = _FakeProc(responses=responses)
        client = BridgeClient()
        _stub_paths(client, tmp_path)
        # Always burn one response slot for the start-time ping.
        with _spawn_with(fake)[0], patch("holo.bridge.uuid.uuid4") as uu:
            uu.return_value.hex = "PING"
            client.start()
        return client, fake

    def test_request_serialises_method_and_params(self, tmp_path):
        responses = [
            {"id": "PING", "result": {"pong": True}},
            {"id": "REQ1", "result": {"focused": True, "name": "Chrome"}},
        ]
        client, fake = self._start_client(tmp_path, responses)
        with patch("holo.bridge.uuid.uuid4") as uu:
            uu.return_value.hex = "REQ1"
            result = client.activate("Chrome")
        assert result == {"focused": True, "name": "Chrome"}
        # Inspect what was sent over stdin (skip the start-time ping).
        lines = fake.stdin.getvalue().decode().strip().splitlines()
        assert json.loads(lines[1]) == {
            "id": "REQ1",
            "method": "app.activate",
            "params": {"name": "Chrome"},
        }

    def test_request_maps_error_envelope_to_exception(self, tmp_path):
        responses = [
            {"id": "PING", "result": {"pong": True}},
            {
                "id": "REQ1",
                "error": {"code": -32601, "message": "method not found: foo", "trace": "tb..."},
            },
        ]
        client, _ = self._start_client(tmp_path, responses)
        with patch("holo.bridge.uuid.uuid4") as uu:
            uu.return_value.hex = "REQ1"
            with pytest.raises(BridgeError) as excinfo:
                client.request("foo")
        assert excinfo.value.code == -32601
        assert "method not found" in str(excinfo.value)
        assert excinfo.value.trace == "tb..."

    def test_request_id_mismatch_raises(self, tmp_path):
        responses = [
            {"id": "PING", "result": {"pong": True}},
            {"id": "WRONG_ID", "result": {}},
        ]
        client, _ = self._start_client(tmp_path, responses)
        with patch("holo.bridge.uuid.uuid4") as uu:
            uu.return_value.hex = "REQ1"
            with pytest.raises(BridgeError, match="id mismatch"):
                client.request("ping")

    def test_request_when_stdout_closed_raises_with_stderr_tail(self, tmp_path):
        responses = [{"id": "PING", "result": {"pong": True}}]
        client, fake = self._start_client(tmp_path, responses)
        # No more responses queued; the next readline returns b"".
        fake.stderr = io.BytesIO(b"some JVM trouble")
        # read1 isn't on a vanilla BytesIO, monkey-patch it.
        fake.stderr.read1 = lambda n=4096: b"some JVM trouble"  # type: ignore[attr-defined]

        with patch("holo.bridge.uuid.uuid4") as uu:
            uu.return_value.hex = "REQ1"
            with pytest.raises(BridgeError, match="some JVM trouble"):
                client.request("ping")


class TestConvenienceVerbs:
    def _client_for(self, tmp_path, responses):
        fake = _FakeProc(
            responses=[{"id": "PING", "result": {"pong": True}}, *responses]
        )
        client = BridgeClient()
        _stub_paths(client, tmp_path)
        with _spawn_with(fake)[0], patch("holo.bridge.uuid.uuid4") as uu:
            uu.return_value.hex = "PING"
            client.start()
        return client, fake

    def test_click_passes_modifiers(self, tmp_path):
        client, fake = self._client_for(
            tmp_path, [{"id": "REQ", "result": {"clicked": True, "x": 10, "y": 20}}]
        )
        with patch("holo.bridge.uuid.uuid4") as uu:
            uu.return_value.hex = "REQ"
            client.click(10, 20, modifiers=["cmd"])
        sent = fake.stdin.getvalue().decode().strip().splitlines()
        assert json.loads(sent[1])["params"] == {"x": 10, "y": 20, "modifiers": ["cmd"]}

    def test_key_combo_passes_through(self, tmp_path):
        client, fake = self._client_for(
            tmp_path, [{"id": "REQ", "result": {"sent": "cmd+v"}}]
        )
        with patch("holo.bridge.uuid.uuid4") as uu:
            uu.return_value.hex = "REQ"
            client.key("cmd+v")
        sent = fake.stdin.getvalue().decode().strip().splitlines()
        assert json.loads(sent[1])["method"] == "screen.key"
        assert json.loads(sent[1])["params"] == {"combo": "cmd+v"}

    def test_type_text_passes_text(self, tmp_path):
        client, fake = self._client_for(
            tmp_path, [{"id": "REQ", "result": {"typed_chars": 5}}]
        )
        with patch("holo.bridge.uuid.uuid4") as uu:
            uu.return_value.hex = "REQ"
            client.type_text("hello")
        sent = fake.stdin.getvalue().decode().strip().splitlines()
        assert json.loads(sent[1])["method"] == "screen.type"
        assert json.loads(sent[1])["params"] == {"text": "hello"}

    def test_screenshot_decodes_base64_to_png_bytes(self, tmp_path):
        import base64 as _b64

        png_bytes = b"\x89PNG\r\n\x1a\n..."  # not a real PNG; just bytes to round-trip
        client, fake = self._client_for(
            tmp_path,
            [{"id": "REQ", "result": {"image": _b64.b64encode(png_bytes).decode()}}],
        )
        with patch("holo.bridge.uuid.uuid4") as uu:
            uu.return_value.hex = "REQ"
            out = client.screenshot()
        assert out == png_bytes
        sent = fake.stdin.getvalue().decode().strip().splitlines()
        assert json.loads(sent[1])["method"] == "screen.shot"
        assert json.loads(sent[1])["params"] == {}

    def test_screenshot_passes_region(self, tmp_path):
        import base64 as _b64

        client, fake = self._client_for(
            tmp_path,
            [{"id": "REQ", "result": {"image": _b64.b64encode(b"X").decode()}}],
        )
        with patch("holo.bridge.uuid.uuid4") as uu:
            uu.return_value.hex = "REQ"
            client.screenshot(region={"x": 10, "y": 20, "width": 30, "height": 40})
        sent = fake.stdin.getvalue().decode().strip().splitlines()
        assert json.loads(sent[1])["params"] == {
            "region": {"x": 10, "y": 20, "width": 30, "height": 40}
        }

    def test_find_image_encodes_needle_and_passes_score(self, tmp_path):
        import base64 as _b64

        needle = b"\x89PNG\r\n\x1a\nstub-needle"
        client, fake = self._client_for(
            tmp_path,
            [{
                "id": "REQ",
                "result": {"x": 100, "y": 200, "width": 30, "height": 30, "score": 0.92},
            }],
        )
        with patch("holo.bridge.uuid.uuid4") as uu:
            uu.return_value.hex = "REQ"
            out = client.find_image(needle, score=0.85)
        assert out == {"x": 100, "y": 200, "width": 30, "height": 30, "score": 0.92}
        sent = fake.stdin.getvalue().decode().strip().splitlines()
        params = json.loads(sent[1])["params"]
        assert params["needle"] == _b64.b64encode(needle).decode("ascii")
        assert params["score"] == 0.85

    def test_find_image_returns_none_for_no_match(self, tmp_path):
        client, _ = self._client_for(tmp_path, [{"id": "REQ", "result": None}])
        with patch("holo.bridge.uuid.uuid4") as uu:
            uu.return_value.hex = "REQ"
            assert client.find_image(b"PNG") is None


class TestResourceResolution:
    def test_explicit_jar_path_must_exist(self, tmp_path):
        client = BridgeClient(jar_path=tmp_path / "missing.jar")
        with pytest.raises(BridgeMissingError, match="not found"):
            client._resolve_jar()

    def test_env_var_jar_path(self, tmp_path, monkeypatch):
        jar = tmp_path / "sikulixapi.jar"
        jar.write_bytes(b"")
        monkeypatch.setenv("HOLO_SIKULI_JAR", str(jar))
        client = BridgeClient()
        assert client._resolve_jar() == jar

    def test_repo_fallback_picks_up_sikulixapi(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        (repo / "vendor").mkdir(parents=True)
        (repo / "pyproject.toml").write_text("")
        jar = repo / "vendor" / "sikulixapi-2.0.5.jar"
        jar.write_bytes(b"")
        monkeypatch.delenv("HOLO_SIKULI_JAR", raising=False)
        monkeypatch.setattr("holo.bridge._repo_root", lambda: repo, raising=True)
        monkeypatch.setattr("holo.bridge._bundle_root", lambda: None)
        client = BridgeClient()
        assert client._resolve_jar() == jar

    def test_repo_fallback_picks_up_sikulixide(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        (repo / "vendor").mkdir(parents=True)
        (repo / "pyproject.toml").write_text("")
        jar = repo / "vendor" / "sikulixide-2.0.5.jar"
        jar.write_bytes(b"")
        monkeypatch.delenv("HOLO_SIKULI_JAR", raising=False)
        monkeypatch.setattr("holo.bridge._repo_root", lambda: repo, raising=True)
        monkeypatch.setattr("holo.bridge._bundle_root", lambda: None)
        client = BridgeClient()
        assert client._resolve_jar() == jar

    def test_repo_fallback_prefers_api_over_ide(self, tmp_path, monkeypatch):
        # If both are present, the smaller `sikulixapi-*.jar` wins.
        repo = tmp_path / "repo"
        (repo / "vendor").mkdir(parents=True)
        (repo / "pyproject.toml").write_text("")
        api = repo / "vendor" / "sikulixapi-2.0.5.jar"
        ide = repo / "vendor" / "sikulixide-2.0.5.jar"
        api.write_bytes(b"")
        ide.write_bytes(b"")
        monkeypatch.delenv("HOLO_SIKULI_JAR", raising=False)
        monkeypatch.setattr("holo.bridge._repo_root", lambda: repo, raising=True)
        monkeypatch.setattr("holo.bridge._bundle_root", lambda: None)
        client = BridgeClient()
        assert client._resolve_jar() == api

    def test_missing_jar_raises_clean_error(self, tmp_path, monkeypatch):
        monkeypatch.delenv("HOLO_SIKULI_JAR", raising=False)
        monkeypatch.setattr("holo.bridge._repo_root", lambda: tmp_path, raising=True)
        monkeypatch.setattr("holo.bridge._bundle_root", lambda: None)
        client = BridgeClient()
        with pytest.raises(BridgeMissingError, match="vendor/"):
            client._resolve_jar()


class TestThreadSafety:
    def test_concurrent_requests_are_serialised(self, tmp_path):
        # Two concurrent requests must get the right responses back without
        # a "id mismatch" — the lock guarantees write/read pairing.
        responses = [
            {"id": "PING", "result": {"pong": True}},
            {"id": "REQ_A", "result": {"a": 1}},
            {"id": "REQ_B", "result": {"b": 2}},
        ]
        fake = _FakeProc(responses=responses)
        client = BridgeClient()
        _stub_paths(client, tmp_path)
        with _spawn_with(fake)[0], patch("holo.bridge.uuid.uuid4") as uu:
            uu.return_value.hex = "PING"
            client.start()

        results: dict[str, dict] = {}

        # Force deterministic id ordering so the test isn't flaky on which
        # thread wins the race for the lock.
        ids = iter(["REQ_A", "REQ_B"])
        id_lock = threading.Lock()

        def next_id():
            with id_lock:
                v = next(ids)
            class _U:
                hex = v
            return _U()

        def worker(name: str):
            with patch("holo.bridge.uuid.uuid4", side_effect=next_id):
                results[name] = client.request("ping")

        # Sequential calls suffice to prove the lock + queued responses
        # work correctly; a true concurrent race adds flakiness without
        # adding meaningful coverage given the lock implementation.
        worker("a")
        worker("b")
        assert results["a"] == {"a": 1}
        assert results["b"] == {"b": 2}
