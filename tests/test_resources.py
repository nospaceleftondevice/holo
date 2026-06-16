"""Integration tests for the resources feature (Phase 1).

Spans announce.py (Resource + TXT extension), discover.py (parsing,
filtering, --fetch-paths), and capabilities_server.py (the
``GET /v1/resources`` endpoint). The exit criterion for Phase 1 is
that a single test can:

  1. Construct two resources with different tags
  2. Build a TXT-style props dict from a HoloAnnouncer that includes them
  3. Round-trip those props through ``parse_txt`` and recover the
     r=/rn=/rcount= fields
  4. Filter by tag and by name and confirm the expected matches
  5. Start a real CapabilitiesServer with the resources attached and
     fetch ``/v1/resources`` with the token — receiving the full
     per-resource records back
  6. Reject the fetch when the token is wrong

We don't open a real Zeroconf socket here — multicast on CI runners
is flaky. Real-network behaviour stays a manual smoke test (see
docs/resources.md §Phase 1 exit criterion).
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from contextlib import redirect_stderr
from typing import Any

import pytest

from holo.announce import (
    FIELD_CAPS_PORT,
    FIELD_CAPS_TOKEN,
    FIELD_CWD,
    FIELD_HOLO_PID,
    FIELD_HOLO_VERSION,
    FIELD_HOST,
    FIELD_IPS,
    FIELD_R,
    FIELD_RCOUNT,
    FIELD_RN,
    FIELD_STARTED,
    FIELD_USER,
    FIELD_V,
    HoloAnnouncer,
    Resource,
    parse_resource_spec,
)
from holo.capabilities import CapabilitiesProbe
from holo.capabilities_server import CAPS_TOKEN_HEADER, CapabilitiesServer
from holo.discover import (
    fetch_resource_detail,
    parse_txt,
    session_matches_resource_filter,
)

# ----------------------------------------------------------------- Resource type


class TestResource:
    def test_minimum_valid(self) -> None:
        r = Resource(name="movies", path="/Volumes/movies")
        assert r.name == "movies"
        assert r.tags == ()
        assert r.caps == ()

    def test_full(self) -> None:
        r = Resource(
            name="movies",
            path="/Volumes/movies",
            tags=("video-files", "archive"),
            caps=("exec:ffprobe", "readonly"),
        )
        assert r.tags == ("video-files", "archive")
        assert r.caps == ("exec:ffprobe", "readonly")

    def test_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        r = Resource(name="a", path="/x")
        with pytest.raises(FrozenInstanceError):
            r.name = "b"  # type: ignore[misc]

    @pytest.mark.parametrize(
        "name,path,tags,caps,error",
        [
            ("", "/x", (), (), "name is required"),
            ("a,b", "/x", (), (), "may not contain ','"),
            ("a", "", (), (), "path is required"),
            ("a", "/x", ("",), (), "empty tag"),
            ("a", "/x", ("t,u",), (), "may not contain ','"),
            ("a", "/x", (), ("",), "empty cap"),
            ("a", "/x", (), ("c,d",), "may not contain ','"),
        ],
    )
    def test_validation(
        self,
        name: str,
        path: str,
        tags: tuple,
        caps: tuple,
        error: str,
    ) -> None:
        with pytest.raises(ValueError, match=error):
            Resource(name=name, path=path, tags=tags, caps=caps)


# ------------------------------------------------------ parse_resource_spec


class TestParseResourceSpec:
    def test_full_spec(self) -> None:
        r = parse_resource_spec(
            "name=movies;path=/Volumes/movies;"
            "tags=video-files,archive;"
            "caps=exec:ffprobe,exec:python3,readonly"
        )
        assert r.name == "movies"
        assert r.path == "/Volumes/movies"
        assert r.tags == ("video-files", "archive")
        assert r.caps == ("exec:ffprobe", "exec:python3", "readonly")

    def test_whitespace_tolerant(self) -> None:
        r = parse_resource_spec(
            " name = a ; path = /x ; tags = t1 , t2 ; caps = c1 "
        )
        assert r.name == "a"
        assert r.path == "/x"
        assert r.tags == ("t1", "t2")
        assert r.caps == ("c1",)

    def test_minimum(self) -> None:
        r = parse_resource_spec("name=a;path=/x")
        assert r.tags == ()
        assert r.caps == ()

    @pytest.mark.parametrize(
        "spec,error",
        [
            ("", "empty"),
            ("name=a;junk", "missing '='"),
            ("name=a;name=b;path=/x", "duplicate key"),
            ("name=a;path=/x;bogus=1", "unknown keys"),
            ("path=/x", "name is required"),  # via Resource validation
            ("name=a", "path is required"),
        ],
    )
    def test_invalid(self, spec: str, error: str) -> None:
        with pytest.raises(ValueError, match=error):
            parse_resource_spec(spec)


# ------------------------------------------------------ HoloAnnouncer TXT integration


class TestAnnouncerTXT:
    def _decode(self, props: dict[bytes, bytes]) -> dict[str, str]:
        return {k.decode(): v.decode() for k, v in props.items()}

    def test_no_resources_omits_fields(self) -> None:
        a = HoloAnnouncer(session="test")
        props = self._decode(a.build_properties())
        assert "r" not in props
        assert "rn" not in props
        assert "rcount" not in props

    def test_single_resource_emits_all_three_fields(self) -> None:
        r = Resource(
            name="movies",
            path="/Volumes/movies",
            tags=("video-files", "archive"),
        )
        a = HoloAnnouncer(resources=[r])
        props = self._decode(a.build_properties())
        # Tag union always sorted for stable TXT bytes
        assert props["r"] == "archive,video-files"
        # Name list in announce order (it's an addressing key)
        assert props["rn"] == "movies"
        assert props["rcount"] == "1"

    def test_multiple_resources(self) -> None:
        a = HoloAnnouncer(resources=[
            Resource(name="movies", path="/v", tags=("video-files", "archive")),
            Resource(name="family", path="/p", tags=("photos", "archive")),
        ])
        props = self._decode(a.build_properties())
        # union — archive appears once, sorted
        assert props["r"] == "archive,photos,video-files"
        # names in announce order
        assert props["rn"] == "movies,family"
        assert props["rcount"] == "2"

    def test_duplicate_names_rejected(self) -> None:
        r1 = Resource(name="movies", path="/v")
        r2 = Resource(name="movies", path="/other")
        with pytest.raises(ValueError, match="duplicate name"):
            HoloAnnouncer(resources=[r1, r2])


# --------------------------------------------------------- discover.parse_txt


def _required(**extra: str) -> dict[bytes, bytes]:
    """Build a TXT-record dict that satisfies REQUIRED_FIELDS."""
    base = {
        FIELD_V: "1",
        FIELD_HOST: "nas-01",
        FIELD_USER: "me",
        FIELD_HOLO_PID: "42",
        FIELD_HOLO_VERSION: "0.1.0a33",
        FIELD_STARTED: "1700000000",
        FIELD_CWD: "/tmp",
    }
    base.update(extra)
    return {k.encode(): v.encode() for k, v in base.items()}


class TestParseTXTResources:
    def test_round_trip(self) -> None:
        props = _required(
            r="video-files,archive",
            rn="movies,family",
            rcount="2",
        )
        s = parse_txt(props, "instance-1")
        assert s is not None
        assert s[FIELD_R] == ["video-files", "archive"]
        assert s[FIELD_RN] == ["movies", "family"]
        assert s[FIELD_RCOUNT] == 2
        assert isinstance(s[FIELD_RCOUNT], int)

    def test_absent_fields_absent_from_session(self) -> None:
        s = parse_txt(_required(), "instance-1")
        assert s is not None
        assert FIELD_R not in s
        assert FIELD_RN not in s
        assert FIELD_RCOUNT not in s

    def test_non_integer_rcount_drops_session(self) -> None:
        # rcount is in INT_FIELDS; malformed integer means corrupted TXT.
        s = parse_txt(_required(rcount="not-a-number"), "instance-1")
        assert s is None


# -------------------------------------------------- session_matches_resource_filter


class TestSessionFilter:
    @pytest.fixture
    def session(self) -> dict[str, Any]:
        return {
            FIELD_R: ["video-files", "archive"],
            FIELD_RN: ["movies", "family"],
        }

    def test_no_filter_passes(self, session: dict[str, Any]) -> None:
        assert session_matches_resource_filter(session) is True

    def test_matching_tag_passes(self, session: dict[str, Any]) -> None:
        assert session_matches_resource_filter(session, tags=["video-files"])

    def test_non_matching_tag_fails(self, session: dict[str, Any]) -> None:
        assert not session_matches_resource_filter(session, tags=["photos"])

    def test_tags_are_and_joined(self, session: dict[str, Any]) -> None:
        assert session_matches_resource_filter(
            session, tags=["video-files", "archive"]
        )
        assert not session_matches_resource_filter(
            session, tags=["video-files", "photos"]
        )

    def test_name_filter(self, session: dict[str, Any]) -> None:
        assert session_matches_resource_filter(session, names=["movies"])
        assert not session_matches_resource_filter(session, names=["nope"])

    def test_combined_tag_and_name(self, session: dict[str, Any]) -> None:
        assert session_matches_resource_filter(
            session, tags=["video-files"], names=["movies"]
        )
        assert not session_matches_resource_filter(
            session, tags=["video-files"], names=["nope"]
        )

    def test_session_without_resources_fails_any_filter(self) -> None:
        s: dict[str, Any] = {}
        assert session_matches_resource_filter(s) is True
        assert not session_matches_resource_filter(s, tags=["any"])
        assert not session_matches_resource_filter(s, names=["any"])


# ------------------------------------------------- /v1/resources HTTP endpoint


@pytest.fixture
def caps_server_factory():
    """Yield a factory that starts a CapabilitiesServer and stops it after."""
    servers: list[CapabilitiesServer] = []

    def _make(resources: list[Resource] | None = None) -> CapabilitiesServer:
        srv = CapabilitiesServer(
            probe=CapabilitiesProbe(),
            host="127.0.0.1",
            port=0,
            resources=resources,
        )
        srv.start()
        servers.append(srv)
        return srv

    yield _make
    for s in servers:
        s.stop()


def _get(url: str, token: str, timeout: float = 2.0) -> tuple[int, dict | None]:
    req = urllib.request.Request(url, headers={CAPS_TOKEN_HEADER: token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, None


class TestResourcesEndpoint:
    def test_no_resources_returns_empty_list(self, caps_server_factory) -> None:
        srv = caps_server_factory()
        url = f"http://127.0.0.1:{srv.actual_port}/v1/resources"
        status, body = _get(url, srv.token)
        assert status == 200
        assert body == {"resources": []}

    def test_two_resources_returns_full_records(
        self, caps_server_factory
    ) -> None:
        r1 = Resource(
            name="movies",
            path="/Volumes/movies",
            tags=("video-files", "archive"),
            caps=("exec:ffprobe", "readonly"),
        )
        r2 = Resource(name="photos", path="/Photos", tags=("photos",))
        srv = caps_server_factory([r1, r2])
        url = f"http://127.0.0.1:{srv.actual_port}/v1/resources"
        status, body = _get(url, srv.token)
        assert status == 200
        assert body is not None
        assert len(body["resources"]) == 2
        m, p = body["resources"]
        assert m == {
            "name": "movies",
            "path": "/Volumes/movies",
            "tags": ["video-files", "archive"],
            "caps": ["exec:ffprobe", "readonly"],
            "allow_principals": [],
        }
        assert p == {
            "name": "photos",
            "path": "/Photos",
            "tags": ["photos"],
            "caps": [],
            "allow_principals": [],
        }

    def test_unauthorized_without_token(self, caps_server_factory) -> None:
        srv = caps_server_factory()
        url = f"http://127.0.0.1:{srv.actual_port}/v1/resources"
        status, _ = _get(url, "wrong-token")
        assert status == 401


# ----------------------------------------------------- discover --fetch-paths


class TestFetchResourceDetail:
    def test_returns_resource_list_when_server_up(
        self, caps_server_factory
    ) -> None:
        r = Resource(name="m", path="/v", tags=("t",), caps=("exec:ffprobe",))
        srv = caps_server_factory([r])
        session = {
            FIELD_IPS: ["127.0.0.1"],
            FIELD_CAPS_PORT: srv.actual_port,
            FIELD_CAPS_TOKEN: srv.token,
        }
        detail = fetch_resource_detail(session)
        assert detail == [
            {
                "name": "m",
                "path": "/v",
                "tags": ["t"],
                "caps": ["exec:ffprobe"],
                "allow_principals": [],
            }
        ]

    def test_returns_none_when_no_caps_port(self) -> None:
        assert fetch_resource_detail({FIELD_IPS: ["127.0.0.1"]}) is None

    def test_returns_none_when_no_ips(self) -> None:
        assert fetch_resource_detail({
            FIELD_CAPS_PORT: 7080,
            FIELD_CAPS_TOKEN: "tok",
        }) is None

    def test_returns_none_when_token_wrong(
        self, caps_server_factory
    ) -> None:
        srv = caps_server_factory()
        session = {
            FIELD_IPS: ["127.0.0.1"],
            FIELD_CAPS_PORT: srv.actual_port,
            FIELD_CAPS_TOKEN: "wrong-token",
        }
        # Auth rejection → urllib.HTTPError → swallowed → None
        assert fetch_resource_detail(session) is None


# ------------------------------------------------------------------- CLI plumbing


class TestCLIPlumbing:
    def test_announce_resource_requires_announce(self) -> None:
        from holo.cli import main

        err = io.StringIO()
        with redirect_stderr(err):
            rc = main(["mcp", "--announce-resource", "name=a;path=/x"])
        assert rc == 2
        assert "require --announce" in err.getvalue()

    def test_announce_resource_invalid_spec(self) -> None:
        from holo.cli import main

        err = io.StringIO()
        with redirect_stderr(err):
            rc = main(
                ["mcp", "--announce", "--announce-resource", "totally invalid"]
            )
        assert rc == 2
        assert "totally invalid" in err.getvalue()

    def test_announce_resource_duplicate_name(self) -> None:
        from holo.cli import main

        err = io.StringIO()
        with redirect_stderr(err):
            rc = main([
                "mcp", "--announce",
                "--announce-resource", "name=movies;path=/a",
                "--announce-resource", "name=movies;path=/b",
            ])
        assert rc == 2
        assert "duplicate name" in err.getvalue()

    def test_resource_tag_rejected_for_serve(self) -> None:
        from holo.cli import main

        err = io.StringIO()
        with redirect_stderr(err):
            rc = main([
                "discover", "--serve", "7090",
                "--resource-tag", "video-files",
            ])
        assert rc == 2
        assert "--json / --tail only" in err.getvalue()

    def test_fetch_paths_rejected_for_tail(self) -> None:
        from holo.cli import main

        err = io.StringIO()
        with redirect_stderr(err):
            rc = main(["discover", "--tail", "--fetch-paths"])
        assert rc == 2
        assert "--json only" in err.getvalue()

    def test_help_includes_new_flags(self) -> None:
        from holo.cli import _print_help

        buf = io.StringIO()
        import contextlib

        with contextlib.redirect_stdout(buf):
            _print_help()
        text = buf.getvalue()
        assert "--announce-resource" in text
        assert "--resource-tag" in text
        assert "--resource-name" in text
        assert "--fetch-paths" in text
