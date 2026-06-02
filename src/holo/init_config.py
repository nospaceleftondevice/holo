"""`holo init <cli>` — scaffold an MCP config file for a given CLI.

Currently supports:
- ``claude`` → ``.mcp.json`` in the current directory (Claude Code's
  project-scoped MCP config format).

The user is prompted for:
1. ``--announce-ip``: pick from the locally-enumerated IPv4 set, or
   enter a literal IP / trailing-dot prefix manually, or skip.
2. ``--announce-command``: when ``tmux`` is on PATH, propose a
   tmux-attach command for the desktop SPA to run after SSH-ing in.

Refuses to clobber an existing file unless ``force=True``.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

# Suggested when tmux is detected. The backticks are deliberate: the
# remote shell evaluates `basename $PWD` at attach time, so the user
# gets a session name derived from whatever directory the remote
# session is in. Users who want a fixed name baked in can edit the
# generated `.mcp.json`.
DEFAULT_TMUX_COMMAND = 'bash -lc "tmux -u attach -t `basename $PWD`"'

# Order matters — first match wins in dispatch.
SUPPORTED_CLIS = {"claude": ".mcp.json"}


def _enumerate_ips() -> list[str]:
    """Reuse announce's IP enumeration so the menu matches what the
    daemon would actually broadcast without an override."""
    from holo.announce import _enumerate_local_ipv4
    return _enumerate_local_ipv4()


def _prompt_announce_ip(ips: list[str]) -> str | None:
    """Render an IP-selection menu and return the user's choice.

    Returns:
        - the selected IP string (or manually-entered value), or
        - None if the user explicitly skips.
    """
    print("Available local IPv4 addresses:")
    if ips:
        for i, ip in enumerate(ips, 1):
            print(f"  {i}) {ip}")
    else:
        print("  (none auto-detected)")
    manual_idx = len(ips) + 1
    skip_idx = len(ips) + 2
    print(f"  {manual_idx}) Enter manually")
    print(f"  {skip_idx}) Skip (omit --announce-ip)")

    while True:
        raw = input(f"Select [1-{skip_idx}]: ").strip()
        if not raw:
            continue
        try:
            n = int(raw)
        except ValueError:
            print(f"  Not a number — pick 1-{skip_idx}")
            continue
        if 1 <= n <= len(ips):
            return ips[n - 1]
        if n == manual_idx:
            while True:
                manual = input("  IP or trailing-dot prefix (e.g. 192.168.1.): ").strip()
                if manual:
                    return manual
                print("  Empty entry — try again")
        if n == skip_idx:
            return None
        print(f"  Out of range — pick 1-{skip_idx}")


def _prompt_tmux_command() -> str | None:
    """If tmux is on PATH, prompt to confirm DEFAULT_TMUX_COMMAND.
    Returns the command string if confirmed, else None."""
    if shutil.which("tmux") is None:
        return None
    print()
    print("Detected tmux on this machine.")
    print("Suggested --announce-command for the desktop SPA to run after SSH:")
    print(f"  {DEFAULT_TMUX_COMMAND}")
    ans = input("Use this command? [Y/n]: ").strip().lower()
    if ans in ("", "y", "yes"):
        return DEFAULT_TMUX_COMMAND
    return None


def _build_args(*, announce_ip: str | None, announce_command: str | None) -> list[str]:
    """Compose the `holo mcp ...` arg list for the generated config."""
    args = ["mcp", "--no-bookmarklet", "--announce"]
    if announce_ip:
        args += ["--announce-ip", announce_ip]
    if announce_command:
        args += ["--announce-command", announce_command]
    return args


def _write_claude_config(target: Path, args: list[str]) -> None:
    config = {
        "mcpServers": {
            "holo": {
                "command": "holo",
                "args": args,
            }
        }
    }
    target.write_text(json.dumps(config, indent=2) + "\n")


def run(cli: str, *, force: bool = False, cwd: Path | None = None) -> int:
    """Implements `holo init <cli>`. Returns CLI exit code."""
    if cli not in SUPPORTED_CLIS:
        supported = ", ".join(sorted(SUPPORTED_CLIS))
        sys.stderr.write(
            f"holo init: unsupported CLI {cli!r}. Supported: {supported}\n"
        )
        return 2

    base = cwd or Path.cwd()
    filename = SUPPORTED_CLIS[cli]
    target = base / filename

    if target.exists() and not force:
        sys.stderr.write(
            f"holo init: {target} already exists. Remove it or re-run with "
            "--force to overwrite.\n"
        )
        return 1

    if not sys.stdin.isatty():
        sys.stderr.write(
            "holo init: requires an interactive terminal — prompts read from "
            "stdin. Re-run from a TTY.\n"
        )
        return 1

    print(f"holo init {cli} — will write {target}")
    print()

    try:
        ips = _enumerate_ips()
    except Exception as e:  # noqa: BLE001 — enumeration is best-effort
        sys.stderr.write(f"holo init: IP enumeration failed: {e}\n")
        ips = []

    announce_ip = _prompt_announce_ip(ips)
    announce_command = _prompt_tmux_command()

    args = _build_args(announce_ip=announce_ip, announce_command=announce_command)
    _write_claude_config(target, args)

    print()
    print(f"✓ wrote {target}")
    print()
    print("Configured holo mcp invocation:")
    quoted = " ".join(_shell_quote(a) for a in args)
    print(f"  holo {quoted}")
    return 0


def _shell_quote(s: str) -> str:
    """Best-effort shell quoting for the human-readable echo line. Not
    used in the JSON payload — only when printing the recap so users
    can spot anything that needs escaping."""
    import shlex
    return shlex.quote(s)
