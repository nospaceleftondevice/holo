"""Unit tests for holo.discover.

Exercise the parser (schema validation, optional-field handling), the
SessionStore (upsert/remove/snapshot/fanout/stale sweep), the
zeroconf ServiceListener bridge, the CLI flag parsing, and the
Starlette HTTP+WS app via TestClient.

We don't open a real Zeroconf socket here — multicast on test
runners is flaky. Real-network behaviour is exercised by the manual
smoke test described in `docs/companion-spec.md` §7 and by the live
e2e checks in the PR description.
"""

from __future__ import annotations

import threading
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from holo.announce import (
    FIELD_CWD,
    FIELD_HOLO_PID,
    FIELD_HOLO_VERSION,
    FIELD_HOST,
    FIELD_IPS,
    FIELD_SESSION,
    FIELD_SSH_USER,
    FIELD_STARTED,
    FIELD_TMUX_SESSION,
    FIELD_USER,
    FIELD_V,
)
from holo.discover import (
    DEFAULT_CORS_ORIGINS,
    DEFAULT_JSON_WAIT_S,
    DEFAULT_SERVE_PORT,
    DEFAULT_STALE_AFTER_S,
    HoloListener,
    SessionStore,
    _instance_from_name,
    build_app,
    parse_txt,
)


def _txt(**kwargs: str) -> dict[bytes, bytes]:
    """Build a TXT-record dict with required defaults filled in."""
    base = {
        FIELD_V: "1",
        FIELD_HOST: "test-host.local",
        FIELD_USER: "alice",
        FIELD_HOLO_PID: "12345",
        FIELD_HOLO_VERSION: "0.1.0a12",
        FIELD_STARTED: "1700000000",
        FIELD_CWD: "/work",
    }
    base.update(kwargs)
    return {k.encode(): v.encode() for k, v in base.items()}


# --------------------------------------------------------------------- parser


class TestParseTxt:
    def test_required_fields_produce_session(self) -> None:
        s = parse_txt(_txt(), "holo-test-1")
        assert s is not None
        assert s["instance"] == "holo-test-1"
        assert s[FIELD_V] == "1"
        assert s[FIELD_HOST] == "test-host.local"
        assert s[FIELD_USER] == "alice"
        assert s[FIELD_HOLO_PID] == 12345
        assert s[FIELD_STARTED] == 1700000000
        assert s[FIELD_CWD] == "/work"
        assert "last_seen" in s

    def test_missing_required_field_drops(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        props = _txt()
        del props[FIELD_HOST.encode()]
        with caplog.at_level("WARNING"):
            s = parse_txt(props, "holo-test-1")
        assert s is None
        assert any("missing required" in r.message for r in caplog.records)

    def test_unknown_schema_version_drops(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING"):
            s = parse_txt(_txt(v="2"), "holo-test-1")
        assert s is None
        assert any("unsupported schema" in r.message for r in caplog.records)

    def test_optional_fields_included_when_present(self) -> None:
        s = parse_txt(
            _txt(
                ssh_user="bob",
                session="claude-1",
                tmux_session="claude-1",
                tmux_window="0",
            ),
            "holo-x",
        )
        assert s is not None
        assert s[FIELD_SSH_USER] == "bob"
        assert s[FIELD_SESSION] == "claude-1"
        assert s[FIELD_TMUX_SESSION] == "claude-1"

    def test_optional_fields_excluded_when_absent(self) -> None:
        s = parse_txt(_txt(), "holo-x")
        assert s is not None
        assert FIELD_SSH_USER not in s
        assert FIELD_SESSION not in s
        assert FIELD_TMUX_SESSION not in s

    def test_ips_split_on_comma(self) -> None:
        s = parse_txt(_txt(ips="10.0.0.1,192.168.1.5"), "holo-x")
        assert s is not None
        assert s[FIELD_IPS] == ["10.0.0.1", "192.168.1.5"]

    def test_ips_strips_whitespace_and_empty(self) -> None:
        s = parse_txt(_txt(ips="10.0.0.1, ,192.168.1.5,"), "holo-x")
        assert s is not None
        assert s[FIELD_IPS] == ["10.0.0.1", "192.168.1.5"]

    def test_integer_fields_converted(self) -> None:
        s = parse_txt(_txt(holo_pid="999", started="42"), "holo-x")
        assert s is not None
        assert s[FIELD_HOLO_PID] == 999
        assert s[FIELD_STARTED] == 42
        assert isinstance(s[FIELD_HOLO_PID], int)
        assert isinstance(s[FIELD_STARTED], int)

    def test_non_integer_in_int_field_drops(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING"):
            s = parse_txt(_txt(holo_pid="not-an-int"), "holo-x")
        assert s is None
        assert any("non-integer" in r.message for r in caplog.records)

    def test_non_utf8_bytes_drops(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        props = _txt()
        # \xff alone is invalid UTF-8.
        props[b"v"] = b"\xff\xfe"
        with caplog.at_level("WARNING"):
            s = parse_txt(props, "holo-x")
        assert s is None

    def test_instance_from_name(self) -> None:
        assert (
            _instance_from_name(
                "holo-foo-1234-abc._holo-session._tcp.local."
            )
            == "holo-foo-1234-abc"
        )

    def test_instance_from_name_passthrough_on_malformed(self) -> None:
        # Defensive: if zeroconf hands us a name without the suffix, keep
        # the raw value rather than trim something unrelated.
        assert _instance_from_name("strange-name") == "strange-name"


# --------------------------------------------------------------- session store


class TestSessionStore:
    def _add(self, store: SessionStore, instance: str, **extra: Any) -> None:
        s = parse_txt(_txt(**extra), instance)
        assert s is not None
        store.upsert(s)

    def test_upsert_emits_add(self) -> None:
        events: list[dict[str, Any]] = []
        store = SessionStore()
        store.subscribe(events.append)
        self._add(store, "holo-1")
        assert events[-1]["type"] == "add"
        assert events[-1]["session"]["instance"] == "holo-1"

    def test_upsert_existing_emits_update(self) -> None:
        events: list[dict[str, Any]] = []
        store = SessionStore()
        self._add(store, "holo-1")  # subscribe AFTER initial add
        store.subscribe(events.append)
        self._add(store, "holo-1", session="changed")
        assert events == [
            {"type": "update", "session": events[0]["session"]}
        ]

    def test_remove_emits_remove(self) -> None:
        events: list[dict[str, Any]] = []
        store = SessionStore()
        self._add(store, "holo-1")
        store.subscribe(events.append)
        result = store.remove("holo-1")
        assert result == {"type": "remove", "instance": "holo-1"}
        assert events == [{"type": "remove", "instance": "holo-1"}]

    def test_remove_nonexistent_returns_none_and_emits_nothing(self) -> None:
        events: list[dict[str, Any]] = []
        store = SessionStore()
        store.subscribe(events.append)
        assert store.remove("nope") is None
        assert events == []

    def test_snapshot_sorted_by_started(self) -> None:
        store = SessionStore()
        self._add(store, "holo-young", started="2000")
        self._add(store, "holo-old", started="1000")
        self._add(store, "holo-mid", started="1500")
        snap = store.snapshot()
        assert [s["instance"] for s in snap] == ["holo-old", "holo-mid", "holo-young"]

    def test_unsubscribe_stops_callback(self) -> None:
        events: list[dict[str, Any]] = []
        store = SessionStore()
        token = store.subscribe(events.append)
        self._add(store, "holo-1")
        store.unsubscribe(token)
        self._add(store, "holo-2")
        assert len(events) == 1

    def test_subscriber_exception_does_not_break_fanout(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = SessionStore()

        def bad(_event: dict[str, Any]) -> None:
            raise RuntimeError("boom")

        good_events: list[dict[str, Any]] = []
        store.subscribe(bad)
        store.subscribe(good_events.append)
        with caplog.at_level("ERROR"):
            self._add(store, "holo-1")
        assert len(good_events) == 1
        assert any("subscriber raised" in r.message for r in caplog.records)

    def test_prune_stale_drops_old_entries(self) -> None:
        store = SessionStore()
        s1 = parse_txt(_txt(), "holo-old")
        s2 = parse_txt(_txt(session="x"), "holo-fresh")
        assert s1 is not None and s2 is not None
        s1["last_seen"] = 1000
        s2["last_seen"] = 9000
        store.upsert(s1)
        store.upsert(s2)

        events: list[dict[str, Any]] = []
        store.subscribe(events.append)
        pruned = store.prune_stale(stale_after_s=100, now=9050)
        assert pruned == 1
        assert {s["instance"] for s in store.snapshot()} == {"holo-fresh"}
        assert events == [{"type": "remove", "instance": "holo-old"}]

    def test_concurrent_upsert_does_not_lose_subscribers(self) -> None:
        # Smoke: hammer the store from multiple threads, verify the
        # subscriber list survives intact.
        store = SessionStore()
        events: list[dict[str, Any]] = []
        store.subscribe(events.append)

        def worker(start: int) -> None:
            for i in range(20):
                s = parse_txt(_txt(), f"inst-{start}-{i}")
                if s is not None:
                    store.upsert(s)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(events) == 80


# ------------------------------------------------------------ zeroconf bridge


class TestHoloListener:
    def test_add_service_fetches_and_upserts(self) -> None:
        store = SessionStore()
        zc = MagicMock()
        info = MagicMock()
        info.properties = _txt()
        zc.get_service_info.return_value = info

        listener = HoloListener(zc, store)
        listener.add_service(
            zc,
            "_holo-session._tcp.local.",
            "holo-x._holo-session._tcp.local.",
        )

        snap = store.snapshot()
        assert len(snap) == 1
        assert snap[0]["instance"] == "holo-x"

    def test_add_service_drops_when_lookup_returns_none(self) -> None:
        store = SessionStore()
        zc = MagicMock()
        zc.get_service_info.return_value = None
        HoloListener(zc, store).add_service(
            zc, "_holo-session._tcp.local.", "holo-x._holo-session._tcp.local."
        )
        assert store.snapshot() == []

    def test_remove_service_removes_by_instance(self) -> None:
        store = SessionStore()
        s = parse_txt(_txt(), "holo-x")
        assert s is not None
        store.upsert(s)
        zc = MagicMock()
        HoloListener(zc, store).remove_service(
            zc, "_holo-session._tcp.local.", "holo-x._holo-session._tcp.local."
        )
        assert store.snapshot() == []

    def test_update_service_updates_in_place(self) -> None:
        store = SessionStore()
        zc = MagicMock()
        info = MagicMock()
        info.properties = _txt()
        zc.get_service_info.return_value = info
        listener = HoloListener(zc, store)

        listener.add_service(
            zc, "_holo-session._tcp.local.", "holo-x._holo-session._tcp.local."
        )
        # Mutate the TXT and update.
        info.properties = _txt(session="changed")
        listener.update_service(
            zc, "_holo-session._tcp.local.", "holo-x._holo-session._tcp.local."
        )
        snap = store.snapshot()
        assert len(snap) == 1
        assert snap[0][FIELD_SESSION] == "changed"


# ----------------------------------------------------------------- CLI parsing


class TestCLIDiscover:
    def test_no_mode_errors(self, capsys: pytest.CaptureFixture[str]) -> None:
        from holo.cli import main

        rc = main(["discover"])
        assert rc == 2
        assert "pick exactly one mode" in capsys.readouterr().err

    def test_multiple_modes_errors(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from holo.cli import main

        rc = main(["discover", "--json", "--tail"])
        assert rc == 2
        assert "mutually exclusive" in capsys.readouterr().err

    def test_serve_without_port_errors(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from holo.cli import main

        rc = main(["discover", "--serve"])
        assert rc == 2
        assert "--serve requires" in capsys.readouterr().err

    def test_serve_invalid_port(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from holo.cli import main

        rc = main(["discover", "--serve", "not-a-port"])
        assert rc == 2

    def test_json_calls_oneshot_with_default_wait(self) -> None:
        from holo.cli import main

        with patch("holo.discover.run_oneshot") as run_oneshot:
            run_oneshot.return_value = 0
            rc = main(["discover", "--json"])
        assert rc == 0
        run_oneshot.assert_called_once_with(wait_s=DEFAULT_JSON_WAIT_S)

    def test_json_passes_explicit_wait(self) -> None:
        from holo.cli import main

        with patch("holo.discover.run_oneshot") as run_oneshot:
            run_oneshot.return_value = 0
            main(["discover", "--json", "--wait", "10"])
        run_oneshot.assert_called_once_with(wait_s=10.0)

    def test_tail_calls_run_tail(self) -> None:
        from holo.cli import main

        with patch("holo.discover.run_tail") as run_tail:
            run_tail.return_value = 0
            main(["discover", "--tail", "--stale-after", "60"])
        run_tail.assert_called_once_with(stale_after_s=60.0)

    def test_serve_calls_run_serve_with_cors(self) -> None:
        from holo.cli import main

        with patch("holo.discover.run_serve") as run_serve:
            run_serve.return_value = 0
            main(
                [
                    "discover",
                    "--serve",
                    "9000",
                    "--cors-origin",
                    "https://a.test, https://b.test",
                ]
            )
        run_serve.assert_called_once_with(
            port=9000,
            cors_origins=["https://a.test", "https://b.test"],
            stale_after_s=DEFAULT_STALE_AFTER_S,
        )

    def test_default_serve_port_is_7082(self) -> None:
        # Sanity check: keep the constant aligned with the brief.
        assert DEFAULT_SERVE_PORT == 7082

    def test_default_cors_origins(self) -> None:
        # Sanity check: keep the dev-time defaults aligned with the brief.
        assert "http://localhost:8888" in DEFAULT_CORS_ORIGINS
        assert "https://app-dev.tai.sh" in DEFAULT_CORS_ORIGINS


# ------------------------------------------------------------------ HTTP + WS


class TestHTTPApp:
    """Drive the Starlette app via TestClient — no real zeroconf."""

    def test_sessions_endpoint_returns_snapshot(self) -> None:
        from starlette.testclient import TestClient

        store = SessionStore()
        s = parse_txt(_txt(session="claude-1"), "holo-x")
        assert s is not None
        store.upsert(s)
        app = build_app(store=store)
        with TestClient(app) as client:
            resp = client.get("/sessions")
            assert resp.status_code == 200
            body = resp.json()
            assert len(body) == 1
            assert body[0]["instance"] == "holo-x"
            assert body[0][FIELD_SESSION] == "claude-1"

    def test_healthz_shape(self) -> None:
        from starlette.testclient import TestClient

        store = SessionStore()
        app = build_app(store=store)
        with TestClient(app) as client:
            resp = client.get("/healthz")
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "ok"
            assert isinstance(body["interfaces"], list)
            assert isinstance(body["zt_present"], bool)

    def test_events_ws_initial_snapshot(self) -> None:
        from starlette.testclient import TestClient

        store = SessionStore()
        s = parse_txt(_txt(), "holo-x")
        assert s is not None
        store.upsert(s)

        app = build_app(store=store)
        with TestClient(app) as client:
            with client.websocket_connect("/events") as ws:
                event = ws.receive_json()
                assert event["type"] == "add"
                assert event["session"]["instance"] == "holo-x"

    def test_cors_allow_origins_default(self) -> None:
        from starlette.testclient import TestClient

        store = SessionStore()
        app = build_app(store=store)
        with TestClient(app) as client:
            resp = client.get(
                "/sessions",
                headers={"origin": "http://localhost:8888"},
            )
            assert resp.headers.get("access-control-allow-origin") == (
                "http://localhost:8888"
            )

    def test_cors_origin_override(self) -> None:
        from starlette.testclient import TestClient

        store = SessionStore()
        app = build_app(
            store=store,
            cors_origins=["https://only-this.test"],
        )
        with TestClient(app) as client:
            resp = client.get(
                "/sessions",
                headers={"origin": "https://only-this.test"},
            )
            assert resp.headers.get("access-control-allow-origin") == (
                "https://only-this.test"
            )
