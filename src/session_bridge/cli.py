"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from session_bridge import __version__
from session_bridge.errors import SessionBridgeError
from session_bridge.inspection import inspect_session
from session_bridge.model import AgentFormat


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
    inspect_parser.add_argument("path", type=Path)
    inspect_parser.add_argument("--format", choices=tuple(AgentFormat))
    inspect_parser.add_argument("--json", action="store_true")

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
    try:
        if args.command == "inspect":
            source_format = AgentFormat(args.format) if args.format else None
            result = inspect_session(args.path, source_format=source_format)
            if args.json:
                print(result.to_json())
            else:
                _print_inspection(result.to_dict())
            return 0
        parser.error(f"{args.command!r} is specified but not implemented yet")
    except SessionBridgeError as exc:
        print(f"session-bridge: error: {exc}", file=sys.stderr)
        return 2
    return 2


def _print_inspection(summary: dict[str, object]) -> None:
    scalar_keys = (
        "format",
        "path",
        "bytes",
        "sha256",
        "records",
        "session_id",
        "cwd",
        "cli_version",
        "started_at",
        "tool_calls",
        "tool_results",
    )
    for key in scalar_keys:
        value = summary[key]
        print(f"{key}: {value if value is not None else '-'}")
    for key in ("record_types", "roles", "content_blocks", "event_types"):
        print(f"{key}: {json.dumps(summary[key], sort_keys=True)}")
