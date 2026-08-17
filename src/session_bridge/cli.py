"""Command-line entry point."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from session_bridge import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="session-bridge",
        description="Convert resumable Claude Code and Codex CLI sessions.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser(
        "inspect", help="print a content-free structural session summary"
    )
    inspect_parser.add_argument("path")

    convert_parser = subparsers.add_parser("convert", help="convert a session file")
    convert_parser.add_argument("path")
    convert_parser.add_argument("--to", choices=("claude", "codex"), required=True)
    convert_parser.add_argument("--output", required=True)

    import_parser = subparsers.add_parser(
        "import", help="convert and install into a target agent home"
    )
    import_parser.add_argument("path")
    import_parser.add_argument("--to", choices=("claude", "codex"), required=True)
    import_parser.add_argument("--home")
    import_parser.add_argument("--dry-run", action="store_true")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    parser.error(f"{args.command!r} is specified but not implemented yet")
    return 2

