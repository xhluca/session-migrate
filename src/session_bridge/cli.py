"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from session_bridge import __version__
from session_bridge.catalog import Catalog, CatalogEntry, default_catalog_path
from session_bridge.conversion import (
    OPENCODE_HOME_UNSUPPORTED,
    ConversionOptions,
    content_free_result,
    convert_session,
    default_target_home,
    ensure_target_paths_available,
    install_opencode_artifact,
    load_session,
    opencode_manifest_path,
    target_import_paths,
    write_artifact,
)
from session_bridge.discovery import locate_session, normalized_session_id
from session_bridge.errors import SessionBridgeError
from session_bridge.inspection import inspect_session
from session_bridge.model import AgentFormat, TargetFormat


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="session-bridge",
        description=(
            "Read Claude/Codex sessions and convert them to Claude, Codex, Pi, "
            "or OpenCode (Cursor import is explicitly unsupported)."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--catalog",
        type=_expanded_path,
        help="catalog database (default: SESSION_BRIDGE_CATALOG or XDG state)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser(
        "inspect", help="print a content-free structural session summary"
    )
    inspect_parser.add_argument("path", type=_expanded_path, help="source JSONL transcript")
    inspect_parser.add_argument(
        "--format", choices=tuple(AgentFormat), help="override source detection"
    )
    inspect_parser.add_argument(
        "--json", action="store_true", help="print the structural summary as JSON"
    )

    convert_parser = subparsers.add_parser("convert", help="convert a session file")
    convert_parser.add_argument("path", type=_expanded_path, help="source JSONL transcript")
    convert_parser.add_argument(
        "--to",
        choices=tuple(TargetFormat),
        required=True,
        help="target format; cursor is recognized but unsupported",
    )
    convert_parser.add_argument(
        "--output",
        type=_expanded_path,
        required=True,
        help="new target transcript or import-bundle path",
    )
    _add_conversion_arguments(convert_parser)

    import_parser = subparsers.add_parser(
        "import", help="convert and install into a target agent home"
    )
    import_parser.add_argument("path", type=_expanded_path, help="source JSONL transcript")
    import_parser.add_argument(
        "--to",
        choices=tuple(TargetFormat),
        required=True,
        help="target format; cursor is recognized but unsupported",
    )
    import_parser.add_argument(
        "--home", type=_expanded_path, help="target agent home"
    )
    import_parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "validate/collision-check without installing (OpenCode's official list "
            "probe may initialize normal XDG state)"
        ),
    )
    _add_conversion_arguments(import_parser)

    transfer_parser = subparsers.add_parser(
        "transfer", help="find a Claude/Codex session by UUID and import it"
    )
    transfer_parser.add_argument("source_id", nargs="?", help="source session UUID")
    transfer_parser.add_argument(
        "--from",
        dest="source_agent",
        choices=("claude", "codex"),
        help="source agent format",
    )
    transfer_parser.add_argument(
        "--to",
        choices=tuple(TargetFormat),
        help=(
            "target format; cursor is unsupported (default: opposite Claude/Codex source)"
        ),
    )
    transfer_parser.add_argument(
        "--catalog-id",
        help="select an exact source returned by catalog list/search",
    )
    transfer_parser.add_argument(
        "--source-home", type=_expanded_path, help="source agent home"
    )
    transfer_parser.add_argument(
        "--source-cwd",
        type=_expanded_path,
        help="Claude project cwd used to disambiguate lookup",
    )
    transfer_parser.add_argument(
        "--home", type=_expanded_path, help="target agent home"
    )
    transfer_parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "validate/collision-check without installing (OpenCode's official list "
            "probe may initialize normal XDG state)"
        ),
    )
    _add_conversion_arguments(transfer_parser, include_source_format=False)

    catalog_parser = subparsers.add_parser(
        "catalog", help="index, list, and search native sessions across agent homes"
    )
    catalog_commands = catalog_parser.add_subparsers(dest="catalog_command", required=True)

    refresh_parser = catalog_commands.add_parser(
        "refresh", help="incrementally scan every configured native session root"
    )
    refresh_parser.add_argument(
        "--claude-root",
        type=_expanded_path,
        action="append",
        default=[],
        help="register and scan an additional Claude configuration home (repeatable)",
    )
    refresh_parser.add_argument(
        "--codex-root",
        type=_expanded_path,
        action="append",
        default=[],
        help="register and scan an additional Codex home (repeatable)",
    )
    refresh_parser.add_argument(
        "--discover-under",
        type=_expanded_path,
        action="append",
        default=[],
        help="find project-local .claude/.codex homes below this subtree (repeatable)",
    )
    refresh_parser.add_argument(
        "--no-auto-roots",
        action="store_true",
        help=(
            "scan registered/explicit roots without adding default, environment, "
            "or ancestor roots"
        ),
    )
    refresh_parser.add_argument(
        "--validate",
        action="store_true",
        help="fully parse and dry-convert changed candidate sessions",
    )
    refresh_parser.add_argument("--json", action="store_true", help="print JSON")

    roots_parser = catalog_commands.add_parser("roots", help="manage persistent source roots")
    roots_commands = roots_parser.add_subparsers(dest="roots_command", required=True)
    roots_list = roots_commands.add_parser("list", help="list registered roots")
    roots_list.add_argument("--json", action="store_true", help="print JSON")
    roots_add = roots_commands.add_parser("add", help="register a native agent home")
    roots_add.add_argument("path", type=_expanded_path, help="Claude configuration or Codex home")
    roots_add.add_argument(
        "--format", choices=("claude", "codex"), required=True, help="native home format"
    )
    roots_add.add_argument("--json", action="store_true", help="print JSON")
    roots_remove = roots_commands.add_parser(
        "remove", help="remove a root and only its catalog rows"
    )
    roots_remove.add_argument("root_id", type=int, help="root ID from roots list")

    list_parser = catalog_commands.add_parser("list", help="list indexed sessions")
    _add_catalog_query_arguments(list_parser)
    list_parser.add_argument("--json", action="store_true", help="print JSON")

    search_parser = catalog_commands.add_parser(
        "search", help="search native titles/names and UUIDs"
    )
    search_parser.add_argument("query", help="case-insensitive title/name or UUID substring")
    _add_catalog_query_arguments(search_parser)
    search_parser.add_argument("--json", action="store_true", help="print JSON")

    show_parser = catalog_commands.add_parser("show", help="show one exact catalog entry")
    show_parser.add_argument("catalog_id", help="opaque ID from catalog list/search")
    show_parser.add_argument(
        "--include-paths", action="store_true", help="include sensitive root, path, and CWD"
    )
    show_parser.add_argument("--json", action="store_true", help="print JSON")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "catalog":
            return _run_catalog(args)
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
                if args.catalog_id:
                    if args.source_id:
                        raise SessionBridgeError(
                            "pass either SOURCE_UUID or --catalog-id, not both"
                        )
                    if args.source_home or args.source_cwd:
                        raise SessionBridgeError(
                            "--source-home/--source-cwd do not apply with --catalog-id"
                        )
                    with Catalog(_catalog_path(args)) as catalog:
                        entry = catalog.get_session(args.catalog_id, include_paths=True)
                        source_format, source_path = catalog.session_path_for_transfer(
                            args.catalog_id
                        )
                    if args.source_agent and AgentFormat(args.source_agent) != source_format:
                        raise SessionBridgeError(
                            "--from does not match the catalog session format"
                        )
                    requested_source_id = entry.session_id
                else:
                    if not args.source_id or not args.source_agent:
                        raise SessionBridgeError(
                            "transfer requires SOURCE_UUID with --from, or --catalog-id"
                        )
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
                if requested_source_id and (
                    normalized_session_id(session.session_id) != requested_source_id
                ):
                    raise SessionBridgeError(
                        "discovered transcript metadata does not match the source UUID"
                    )
                target_format = (
                    TargetFormat(args.to)
                    if args.to
                    else TargetFormat.CODEX
                    if source_format == AgentFormat.CLAUDE
                    else TargetFormat.CLAUDE
                )
            else:
                source_format = AgentFormat(args.format) if args.format else None
                session = load_session(args.path, source_format)
                target_format = TargetFormat(args.to)
            if target_format == TargetFormat.OPENCODE and getattr(args, "home", None):
                raise SessionBridgeError(OPENCODE_HOME_UNSUPPORTED)
            if args.target_cli and (
                target_format != TargetFormat.OPENCODE or args.command == "convert"
            ):
                raise SessionBridgeError(
                    "--target-cli only applies to OpenCode import and transfer"
                )
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
            elif target_format == TargetFormat.OPENCODE:
                output_path = f"opencode:{artifact.session_id}"
                manifest_path = opencode_manifest_path(artifact)
                dry_run = args.dry_run
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
            if target_format == TargetFormat.OPENCODE and args.command != "convert":
                install_opencode_artifact(
                    artifact,
                    manifest_path=manifest_path,
                    target_cli=args.target_cli,
                    dry_run=dry_run,
                )
            elif not dry_run:
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
    parser.add_argument(
        "--cwd", type=_expanded_path, help="absolute working directory stored in the target"
    )
    parser.add_argument(
        "--target-cli-version",
        help="metadata version only; the writer schema remains pinned",
    )
    parser.add_argument(
        "--target-cli",
        type=_expanded_path,
        help="OpenCode executable (or use OPENCODE_BIN, PATH, then ~/.opencode/bin/opencode)",
    )
    parser.add_argument(
        "--model-provider",
        help="Codex/Pi/OpenCode provider ID (target-specific default)",
    )
    parser.add_argument(
        "--model", help="Claude/Pi/OpenCode target model label"
    )


def _expanded_path(value: str) -> Path:
    """Expand a user-supplied home marker consistently for every CLI path."""

    return Path(value).expanduser()


def _add_catalog_query_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--format", choices=("claude", "codex"), help="filter by source format"
    )
    parser.add_argument(
        "--status", action="append", default=[], help="filter by catalog status (repeatable)"
    )
    parser.add_argument(
        "--kind", action="append", default=[], help="filter by main/sidechain/subagent kind"
    )
    parser.add_argument(
        "--lifecycle",
        action="append",
        default=[],
        help="filter by project/active/archived lifecycle (repeatable)",
    )
    parser.add_argument("--since", help="include sessions started at/after this RFC-3339 time")
    parser.add_argument("--until", help="include sessions started at/before this RFC-3339 time")
    parser.add_argument(
        "--include-missing", action="store_true", help="include stale entries for deleted files"
    )
    parser.add_argument(
        "--include-paths",
        action="store_true",
        help="search and print sensitive root, path, and CWD metadata",
    )
    parser.add_argument("--limit", type=int, default=50, help="maximum rows (default: 50)")
    parser.add_argument("--offset", type=int, default=0, help="rows to skip")


def _catalog_path(args: argparse.Namespace) -> Path:
    return args.catalog or default_catalog_path()


def _run_catalog(args: argparse.Namespace) -> int:
    with Catalog(_catalog_path(args)) as catalog:
        if args.catalog_command == "refresh":
            result = catalog.refresh(
                claude_roots=args.claude_root,
                codex_roots=args.codex_root,
                discover_under=args.discover_under,
                include_auto=not args.no_auto_roots,
                validate=args.validate,
            )
            data = result.to_dict()
            if args.json:
                print(json.dumps(data, indent=2, sort_keys=True))
            else:
                for key in (
                    "roots",
                    "files_seen",
                    "scanned",
                    "unchanged",
                    "missing",
                    "root_errors",
                ):
                    print(f"{key}: {data[key]}")
                print(f"statuses: {json.dumps(data['statuses'], sort_keys=True)}")
            return 0 if result.root_errors == 0 else 2
        if args.catalog_command == "roots":
            if args.roots_command == "list":
                roots = [root.to_dict() for root in catalog.roots()]
                if args.json:
                    print(json.dumps(roots, indent=2, sort_keys=True))
                else:
                    for root in roots:
                        print(
                            f"{root['id']}\t{root['format']}\t{root['source']}\t"
                            f"{root['last_scan_status'] or '-'}\t{root['path']}"
                        )
                return 0
            if args.roots_command == "add":
                root = catalog.add_root(AgentFormat(args.format), args.path)
                if args.json:
                    print(json.dumps(root.to_dict(), indent=2, sort_keys=True))
                else:
                    print(f"registered root {root.id}: {root.format} {root.path}")
                return 0
            if args.roots_command == "remove":
                if not catalog.remove_root(args.root_id):
                    raise SessionBridgeError("catalog root ID was not found")
                print(f"removed catalog root {args.root_id}; native files were not changed")
                return 0
        if args.catalog_command in {"list", "search"}:
            entries = catalog.list_sessions(
                query=args.query if args.catalog_command == "search" else None,
                include_paths=args.include_paths,
                include_missing=args.include_missing,
                agent_format=AgentFormat(args.format) if args.format else None,
                statuses=args.status,
                kinds=args.kind,
                lifecycles=args.lifecycle,
                since=args.since,
                until=args.until,
                limit=args.limit,
                offset=args.offset,
            )
            _print_catalog_entries(entries, as_json=args.json, include_paths=args.include_paths)
            return 0
        if args.catalog_command == "show":
            entry = catalog.get_session(args.catalog_id, include_paths=args.include_paths)
            if args.json:
                print(json.dumps(entry.to_dict(), indent=2, sort_keys=True))
            else:
                for key, value in entry.to_dict().items():
                    if value is not None:
                        print(f"{key}: {value}")
            return 0
    raise SessionBridgeError("catalog subcommand is not implemented")


def _print_catalog_entries(
    entries: Sequence[CatalogEntry], *, as_json: bool, include_paths: bool
) -> None:
    if as_json:
        print(json.dumps([entry.to_dict() for entry in entries], indent=2, sort_keys=True))
        return
    headings = ["CATALOG_ID", "FORMAT", "KIND", "STATUS", "UUID", "TITLE"]
    if include_paths:
        headings.append("PATH")
    print("\t".join(headings))
    for entry in entries:
        values = [
            entry.catalog_id,
            entry.format,
            entry.kind,
            entry.status,
            entry.session_id or entry.filename_session_id or "-",
            _single_line(entry.title or "-"),
        ]
        if include_paths:
            values.append(_single_line(entry.path or "-"))
        print("\t".join(values))


def _single_line(value: str) -> str:
    return " ".join(value.splitlines()).replace("\t", " ")
