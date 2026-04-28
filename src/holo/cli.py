import sys

from holo import __version__


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if args and args[0] in {"-V", "--version"}:
        print(__version__)
        return 0
    print(f"holo {__version__} — not implemented yet (Phase 0 scaffold).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
