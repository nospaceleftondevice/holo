"""Tests for `holo.init_config`.

Interactive prompts are driven by stubbing `builtins.input`; IP
enumeration is mocked so tests don't depend on the host's network
configuration; `shutil.which` is patched per-test to simulate tmux
present/absent.
"""

from __future__ import annotations

import json
import sys

import pytest

from holo import init_config


@pytest.fixture
def cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def tty(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)


def _input_seq(monkeypatch, answers):
    """Replace builtins.input with a queue. Each call consumes the
    next answer; an empty queue raises so tests fail loudly instead
    of hanging."""
    queue = list(answers)
    def fake_input(prompt=""):
        if not queue:
            raise AssertionError(f"input() called past expected sequence (prompt={prompt!r})")
        return queue.pop(0)
    monkeypatch.setattr("builtins.input", fake_input)


# --- happy paths ----------------------------------------------------------

def test_writes_mcp_json_with_selected_ip_and_tmux(cwd, tty, monkeypatch):
    monkeypatch.setattr(init_config, "_enumerate_ips", lambda: ["10.0.0.5", "192.168.1.10"])
    monkeypatch.setattr(init_config.shutil, "which", lambda _: "/usr/local/bin/tmux")
    _input_seq(monkeypatch, ["2", ""])  # pick IP #2, accept tmux default

    rc = init_config.run("claude")
    assert rc == 0

    payload = json.loads((cwd / ".mcp.json").read_text())
    entry = payload["mcpServers"]["holo"]
    assert entry["command"] == "holo"
    assert entry["args"] == [
        "mcp", "--no-bookmarklet", "--announce",
        "--announce-ip", "192.168.1.10",
        "--announce-command", init_config.DEFAULT_TMUX_COMMAND,
    ]


def test_writes_mcp_json_without_tmux(cwd, tty, monkeypatch):
    monkeypatch.setattr(init_config, "_enumerate_ips", lambda: ["10.0.0.5"])
    monkeypatch.setattr(init_config.shutil, "which", lambda _: None)
    _input_seq(monkeypatch, ["1"])  # pick the only IP

    rc = init_config.run("claude")
    assert rc == 0

    entry = json.loads((cwd / ".mcp.json").read_text())["mcpServers"]["holo"]
    assert entry["args"] == [
        "mcp", "--no-bookmarklet", "--announce",
        "--announce-ip", "10.0.0.5",
    ]


def test_manual_ip_entry(cwd, tty, monkeypatch):
    monkeypatch.setattr(init_config, "_enumerate_ips", lambda: ["10.0.0.5"])
    monkeypatch.setattr(init_config.shutil, "which", lambda _: None)
    _input_seq(monkeypatch, ["2", "192.168.50.50"])  # "enter manually", then IP

    rc = init_config.run("claude")
    assert rc == 0
    entry = json.loads((cwd / ".mcp.json").read_text())["mcpServers"]["holo"]
    assert "--announce-ip" in entry["args"]
    assert entry["args"][entry["args"].index("--announce-ip") + 1] == "192.168.50.50"


def test_manual_ip_supports_trailing_dot_prefix(cwd, tty, monkeypatch):
    monkeypatch.setattr(init_config, "_enumerate_ips", lambda: [])
    monkeypatch.setattr(init_config.shutil, "which", lambda _: None)
    _input_seq(monkeypatch, ["1", "192.168.1."])  # manual entry, prefix

    rc = init_config.run("claude")
    assert rc == 0
    entry = json.loads((cwd / ".mcp.json").read_text())["mcpServers"]["holo"]
    assert entry["args"] == [
        "mcp", "--no-bookmarklet", "--announce",
        "--announce-ip", "192.168.1.",
    ]


def test_skip_ip_omits_flag(cwd, tty, monkeypatch):
    monkeypatch.setattr(init_config, "_enumerate_ips", lambda: ["10.0.0.5"])
    monkeypatch.setattr(init_config.shutil, "which", lambda _: None)
    _input_seq(monkeypatch, ["3"])  # skip option

    rc = init_config.run("claude")
    assert rc == 0
    entry = json.loads((cwd / ".mcp.json").read_text())["mcpServers"]["holo"]
    assert "--announce-ip" not in entry["args"]


def test_decline_tmux_omits_announce_command(cwd, tty, monkeypatch):
    monkeypatch.setattr(init_config, "_enumerate_ips", lambda: ["10.0.0.5"])
    monkeypatch.setattr(init_config.shutil, "which", lambda _: "/usr/local/bin/tmux")
    _input_seq(monkeypatch, ["1", "n"])

    rc = init_config.run("claude")
    assert rc == 0
    entry = json.loads((cwd / ".mcp.json").read_text())["mcpServers"]["holo"]
    assert "--announce-command" not in entry["args"]


# --- input validation ----------------------------------------------------

def test_retries_on_non_integer_then_out_of_range(cwd, tty, monkeypatch, capsys):
    monkeypatch.setattr(init_config, "_enumerate_ips", lambda: ["10.0.0.5"])
    monkeypatch.setattr(init_config.shutil, "which", lambda _: None)
    _input_seq(monkeypatch, ["abc", "99", "1"])  # garbage, OOB, then valid

    rc = init_config.run("claude")
    assert rc == 0
    out = capsys.readouterr().out
    assert "Not a number" in out
    assert "Out of range" in out


def test_manual_entry_retries_on_empty(cwd, tty, monkeypatch, capsys):
    monkeypatch.setattr(init_config, "_enumerate_ips", lambda: [])
    monkeypatch.setattr(init_config.shutil, "which", lambda _: None)
    _input_seq(monkeypatch, ["1", "", "10.0.0.5"])  # manual, empty, retry

    rc = init_config.run("claude")
    assert rc == 0
    assert "Empty entry" in capsys.readouterr().out


# --- existing file refusal ------------------------------------------------

def test_refuses_when_mcp_json_exists(cwd, tty, monkeypatch, capsys):
    existing = cwd / ".mcp.json"
    existing.write_text('{"mcpServers": {"other": {"command": "x"}}}')
    monkeypatch.setattr(init_config, "_enumerate_ips", lambda: ["10.0.0.5"])
    monkeypatch.setattr(init_config.shutil, "which", lambda _: None)
    # No input() should be called.
    _input_seq(monkeypatch, [])

    rc = init_config.run("claude")
    assert rc == 1
    assert "already exists" in capsys.readouterr().err
    # File is untouched.
    assert "other" in existing.read_text()


def test_force_overwrites_existing(cwd, tty, monkeypatch):
    existing = cwd / ".mcp.json"
    existing.write_text('{"mcpServers": {"old": {"command": "x"}}}')
    monkeypatch.setattr(init_config, "_enumerate_ips", lambda: ["10.0.0.5"])
    monkeypatch.setattr(init_config.shutil, "which", lambda _: None)
    _input_seq(monkeypatch, ["1"])

    rc = init_config.run("claude", force=True)
    assert rc == 0
    payload = json.loads(existing.read_text())
    assert "holo" in payload["mcpServers"]
    assert "old" not in payload["mcpServers"]  # full overwrite by design


# --- non-interactive / unsupported CLI -----------------------------------

def test_refuses_non_tty(cwd, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    rc = init_config.run("claude")
    assert rc == 1
    assert "interactive terminal" in capsys.readouterr().err


def test_unsupported_cli(cwd, capsys):
    rc = init_config.run("codex")
    assert rc == 2
    err = capsys.readouterr().err
    assert "unsupported CLI" in err
    assert "codex" in err
    assert "claude" in err  # lists supported


# --- explicit cwd parameter ----------------------------------------------

def test_respects_explicit_cwd(tmp_path, tty, monkeypatch):
    monkeypatch.setattr(init_config, "_enumerate_ips", lambda: [])
    monkeypatch.setattr(init_config.shutil, "which", lambda _: None)
    _input_seq(monkeypatch, ["2"])  # skip option (only "enter manually" + "skip" when no IPs)

    target_dir = tmp_path / "elsewhere"
    target_dir.mkdir()
    rc = init_config.run("claude", cwd=target_dir)
    assert rc == 0
    assert (target_dir / ".mcp.json").exists()
    # Did NOT write to tmp_path/.mcp.json.
    assert not (tmp_path / ".mcp.json").exists()
