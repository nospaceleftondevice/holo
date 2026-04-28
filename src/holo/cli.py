"""Minimal CLI surface for the Phase 0 walking skeleton.

Subcommands grow as primitive layers land. For now: `--version` and
`windows` (a smoke command that lists visible windows for manual
verification of the windows reader).
"""

from __future__ import annotations

import sys

from holo import __version__


def _cmd_windows() -> int:
    from holo.windows import list_windows

    try:
        windows = list_windows()
    except NotImplementedError as e:
        print(f"holo windows: {e}", file=sys.stderr)
        return 2
    if not windows:
        print("(no visible windows reported)")
        return 0
    for w in windows:
        title = w.title if w.title else "<unreadable>"
        print(f"{w.id:>8}  L{w.layer}  {w.owner!r:>24}  {title}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print(f"holo {__version__} — try `holo --version` or `holo windows`")
        return 0
    cmd = args[0]
    if cmd in {"-V", "--version"}:
        print(__version__)
        return 0
    if cmd == "windows":
        return _cmd_windows()
    print(f"holo: unknown command {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
