"""Tests for `holo ide`.

`os.execvp` actually replaces the process, so we mock it. We also
mock `ensure_jar` to avoid real network calls / disk writes and
`shutil.which` to control whether `java` is "installed".
"""

from __future__ import annotations

from holo import cli


def test_ide_refuses_when_java_missing(monkeypatch, capsys):
    monkeypatch.setattr(cli, "__name__", "holo.cli")  # noop, for clarity
    import shutil
    monkeypatch.setattr(shutil, "which", lambda _: None)

    rc = cli._cmd_ide()
    assert rc == 1
    err = capsys.readouterr().err
    assert "`java` not on PATH" in err
    assert "OpenJDK" in err


def test_ide_execs_java_with_jar(monkeypatch, tmp_path):
    """Happy path: java on PATH, jar already cached — we exec into
    `java -jar <jar>`."""
    jar = tmp_path / "sikulixide-2.0.5.jar"
    jar.write_bytes(b"fake jar")

    import shutil
    monkeypatch.setattr(shutil, "which", lambda binary: "/opt/homebrew/bin/java")

    from holo import bridge
    monkeypatch.setattr(bridge, "ensure_jar", lambda **kw: jar)

    captured: dict = {}

    def fake_execvp(prog, argv):
        captured["prog"] = prog
        captured["argv"] = list(argv)
        # Raise to escape — real execvp never returns.
        raise SystemExit(0)

    monkeypatch.setattr(cli.os, "execvp", fake_execvp)

    try:
        cli._cmd_ide()
    except SystemExit:
        pass

    assert captured["prog"] == "/opt/homebrew/bin/java"
    assert captured["argv"] == ["/opt/homebrew/bin/java", "-jar", str(jar)]


def test_ide_propagates_bridge_error(monkeypatch, capsys):
    """If `ensure_jar` raises BridgeMissingError (e.g. download blocked
    via HOLO_BRIDGE_NO_DOWNLOAD=1), we surface a clean message and
    don't try to exec."""
    import shutil
    monkeypatch.setattr(shutil, "which", lambda _: "/opt/homebrew/bin/java")

    from holo import bridge

    def boom(**kw):
        raise bridge.BridgeMissingError("no jar and downloads disabled")

    monkeypatch.setattr(bridge, "ensure_jar", boom)

    called: dict = {"execvp": False}
    monkeypatch.setattr(
        cli.os,
        "execvp",
        lambda *a, **kw: called.__setitem__("execvp", True),
    )

    rc = cli._cmd_ide()
    assert rc == 1
    assert called["execvp"] is False
    err = capsys.readouterr().err
    assert "no jar and downloads disabled" in err


def test_ide_uses_subprocess_run_on_windows(monkeypatch, tmp_path):
    """On Windows there's no exec, so we run java as a subprocess and
    return its exit code unchanged."""
    jar = tmp_path / "sikulixide-2.0.5.jar"
    jar.write_bytes(b"fake jar")

    monkeypatch.setattr(cli.sys, "platform", "win32")

    import shutil
    monkeypatch.setattr(shutil, "which", lambda _: r"C:\jdk\bin\java.exe")

    from holo import bridge
    monkeypatch.setattr(bridge, "ensure_jar", lambda **kw: jar)

    captured: dict = {}

    class FakeCompleted:
        returncode = 42

    def fake_run(argv, *a, **kw):
        captured["argv"] = list(argv)
        return FakeCompleted()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    # os.execvp must NOT be called.
    def must_not_call(*a, **kw):
        raise AssertionError("os.execvp called on Windows path")

    monkeypatch.setattr(cli.os, "execvp", must_not_call)

    rc = cli._cmd_ide()
    assert rc == 42
    assert captured["argv"] == [r"C:\jdk\bin\java.exe", "-jar", str(jar)]
