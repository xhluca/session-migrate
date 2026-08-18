"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from session_bridge import __version__
from session_bridge.conversion import (
    ConversionOptions,
    content_free_result,
    convert_session,
    default_target_home,
    ensure_target_paths_available,
    load_session,
    target_import_paths,
    write_artifact,
)
from session_bridge.discovery import locate_session, normalized_session_id
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
    convert_parser.add_argument("path", type=Path)
    convert_parser.add_argument("--to", choices=("claude", "codex"), required=True)
    convert_parser.add_argument("--output", type=Path, required=True)
    _add_conversion_arguments(convert_parser)

    import_parser = subparsers.add_parser(
        "import", help="convert and install into a target agent home"
    )
    import_parser.add_argument("path", type=Path)
    import_parser.add_argument("--to", choices=("claude", "codex"), required=True)
    import_parser.add_argument("--home", type=Path)
    import_parser.add_argument("--dry-run", action="store_true")
    _add_conversion_arguments(import_parser)

    transfer_parser = subparsers.add_parser(
        "transfer", help="find a session by UUID and import it into the other agent"
    )
    transfer_parser.add_argument("source_id", help="source session UUID")
    transfer_parser.add_argument(
        "--from", dest="source_agent", choices=("claude", "codex"), required=True
    )
    transfer_parser.add_argument("--source-home", type=Path)
    transfer_parser.add_argument(
        "--source-cwd", type=Path, help="Claude project cwd used to disambiguate lookup"
    )
    transfer_parser.add_argument("--home", type=Path, help="target agent home")
    transfer_parser.add_argument("--dry-run", action="store_true")
    _add_conversion_arguments(transfer_parser, include_source_format=False)

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
        if args.command in {"convert", "import", "transfer"}:
            if args.command == "transfer":
                source_format = AgentFormat(args.source_agent)
                requested_source_id = normalized_session_id(args.source_id)
                source_home = args.source_home or default_target_home(source_format)
                source_path = locate_session(
                    source_format,
                    requested_source_id,
                    source_home,
                    cwd=args.source_cwd,
                )
                session = load_session(source_path, source_format)
                if not session.session_id:
                    raise SessionBridgeError(
                        "discovered transcript has no native session ID metadata"
                    )
                if normalized_session_id(session.session_id) != requested_source_id:
                    raise SessionBridgeError(
                        "discovered transcript metadata does not match the source UUID"
                    )
                target_format = (
                    AgentFormat.CODEX
                    if source_format == AgentFormat.CLAUDE
                    else AgentFormat.CLAUDE
                )
            else:
                source_format = AgentFormat(args.format) if args.format else None
                session = load_session(args.path, source_format)
                target_format = AgentFormat(args.to)
            artifact = convert_session(
                session,
                ConversionOptions(
                    target_format=target_format,
                    session_id=args.session_id,
                    cwd=args.cwd,
                    target_cli_version=args.target_cli_version,
                    model_provider=args.model_provider,
                    model=args.model,
                ),
            )
            if args.command == "convert":
                output_path = args.output
                manifest_path = output_path.with_name(
                    f"{output_path.name}.session-bridge.json"
                )
                dry_run = False
            else:
                home = args.home or default_target_home(target_format)
                output_path, manifest_path = target_import_paths(artifact, home)
                dry_run = args.dry_run
            result = content_free_result(
                artifact,
                output_path=output_path,
                manifest_path=manifest_path,
                dry_run=dry_run,
            )
            if not dry_run:
                write_artifact(
                    artifact,
                    output_path=output_path,
                    manifest_path=manifest_path,
                )
            else:
                ensure_target_paths_available(output_path, manifest_path)
            print(json.dumps(result, indent=2, sort_keys=True))
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


def _add_conversion_arguments(
    parser: argparse.ArgumentParser, *, include_source_format: bool = True
) -> None:
    if include_source_format:
        parser.add_argument(
            "--format", choices=tuple(AgentFormat), help="override source detection"
        )
    parser.add_argument("--session-id", help="target UUID (generated by default)")
    parser.add_argument("--cwd", type=Path, help="target working directory")
    parser.add_argument("--target-cli-version", help="version recorded in target metadata")
    parser.add_argument("--model-provider", default="openai", help="Codex model provider ID")
    parser.add_argument("--model", help="Claude model label for imported assistant records")
