"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from session_migrate import __version__
from session_migrate.catalog import Catalog, CatalogEntry, default_catalog_path
from session_migrate.conversion import (
    KILO_HOME_UNSUPPORTED,
    OPENCODE_HOME_UNSUPPORTED,
    ConversionOptions,
    content_free_result,
    convert_session,
    default_target_home,
    ensure_target_paths_available,
    install_antigravity_artifact,
    install_copilot_artifact,
    install_cursor_artifact,
    install_grok_artifact,
    install_kilo_artifact,
    install_kimi_artifact,
    install_opencode_artifact,
    install_openhands_artifact,
    install_vibe_artifact,
    kilo_manifest_path,
    load_kilo_session,
    load_opencode_session,
    load_session,
    opencode_manifest_path,
    target_import_paths,
    write_artifact,
)
from session_migrate.discovery import locate_session, normalized_source_id
from session_migrate.errors import SessionMigrateError
from session_migrate.inspection import inspect_session
from session_migrate.model import AgentFormat, TargetFormat


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="session-migrate",
        description=(
            "Migrate Claude, Codex, Pi, Oh My Pi, OpenCode, Copilot, Antigravity, Vibe, "
            "experimental Cursor, Muse, Qwen, Kimi, Grok, Kilo, and OpenHands sessions "
            "between native formats."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--catalog",
        type=_expanded_path,
        help="catalog database (default: SESSION_MIGRATE_CATALOG or XDG state)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser(
        "inspect", help="print a content-free structural session summary"
    )
    inspect_parser.add_argument(
        "path",
        type=_expanded_path,
        help="source transcript, native session directory, or supported export bundle",
    )
    inspect_parser.add_argument(
        "--format", choices=tuple(AgentFormat), help="override source detection"
    )
    inspect_parser.add_argument(
        "--json", action="store_true", help="print the structural summary as JSON"
    )

    convert_parser = subparsers.add_parser("convert", help="convert a session file")
    convert_parser.add_argument(
        "path",
        type=_expanded_path,
        help="source transcript, native session directory, or supported export bundle",
    )
    convert_parser.add_argument(
        "--to",
        choices=tuple(TargetFormat),
        required=True,
        help="target format; Cursor support is pinned, text-only, and experimental",
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
    import_parser.add_argument(
        "path",
        type=_expanded_path,
        help="source transcript, native session directory, or supported export bundle",
    )
    import_parser.add_argument(
        "--to",
        choices=tuple(TargetFormat),
        required=True,
        help="target format; Cursor support is pinned, text-only, and experimental",
    )
    import_parser.add_argument("--home", type=_expanded_path, help="target agent home")
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
        "transfer", help="find a native session by ID or catalog title and import it"
    )
    transfer_parser.add_argument("source_id", nargs="?", help="native source session ID")
    transfer_parser.add_argument(
        "--from",
        dest="source_agent",
        choices=tuple(AgentFormat),
        help="source agent format",
    )
    transfer_parser.add_argument(
        "--to",
        choices=tuple(TargetFormat),
        help=(
            "target format; Cursor support is pinned, text-only, and experimental "
            "(default: opposite Claude/Codex source)"
        ),
    )
    transfer_parser.add_argument(
        "--catalog-id",
        help="select an exact source returned by catalog list/search",
    )
    transfer_parser.add_argument(
        "--title",
        help="select one unique session title/name from the existing catalog",
    )
    transfer_parser.add_argument("--source-home", type=_expanded_path, help="source agent home")
    transfer_parser.add_argument(
        "--source-cli",
        type=_expanded_path,
        help="OpenCode or Kilo source executable used for the official export",
    )
    transfer_parser.add_argument(
        "--source-cwd",
        type=_expanded_path,
        help="Claude/Pi/OMP/Cursor/Vibe/Qwen/Kimi project cwd used to disambiguate lookup",
    )
    transfer_parser.add_argument("--home", type=_expanded_path, help="target agent home")
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
        "--pi-root",
        type=_expanded_path,
        action="append",
        default=[],
        help="register and scan an additional Pi agent home (repeatable)",
    )
    refresh_parser.add_argument(
        "--omp-root",
        type=_expanded_path,
        action="append",
        default=[],
        help="register and scan an additional Oh My Pi agent home (repeatable)",
    )
    refresh_parser.add_argument(
        "--opencode-root",
        type=_expanded_path,
        action="append",
        default=[],
        help="register and scan an additional OpenCode data home (repeatable)",
    )
    refresh_parser.add_argument(
        "--copilot-root",
        type=_expanded_path,
        action="append",
        default=[],
        help="register and scan an additional Copilot home (repeatable)",
    )
    refresh_parser.add_argument(
        "--antigravity-root",
        type=_expanded_path,
        action="append",
        default=[],
        help="register and scan an additional Antigravity CLI data home (repeatable)",
    )
    refresh_parser.add_argument(
        "--cursor-root",
        type=_expanded_path,
        action="append",
        default=[],
        help="register and scan an additional Cursor config home (repeatable)",
    )
    refresh_parser.add_argument(
        "--vibe-root",
        type=_expanded_path,
        action="append",
        default=[],
        help="register and scan an additional Mistral Vibe home (repeatable)",
    )
    refresh_parser.add_argument(
        "--muse-root",
        type=_expanded_path,
        action="append",
        default=[],
        help="register and scan an additional Muse data home (repeatable)",
    )
    refresh_parser.add_argument(
        "--qwen-root",
        type=_expanded_path,
        action="append",
        default=[],
        help="register and scan an additional Qwen home (repeatable)",
    )
    refresh_parser.add_argument(
        "--kimi-root",
        type=_expanded_path,
        action="append",
        default=[],
        help="register and scan an additional Kimi Code home (repeatable)",
    )
    refresh_parser.add_argument(
        "--grok-root",
        type=_expanded_path,
        action="append",
        default=[],
        help="register and scan an additional Grok home (repeatable)",
    )
    refresh_parser.add_argument(
        "--kilo-root",
        type=_expanded_path,
        action="append",
        default=[],
        help="register and scan an additional Kilo data home (repeatable)",
    )
    refresh_parser.add_argument(
        "--openhands-root",
        type=_expanded_path,
        action="append",
        default=[],
        help="register and scan an additional OpenHands conversations root (repeatable)",
    )
    refresh_parser.add_argument(
        "--discover-under",
        type=_expanded_path,
        action="append",
        default=[],
        help=("find conventional project-local agent homes below this subtree (repeatable)"),
    )
    refresh_parser.add_argument(
        "--no-auto-roots",
        action="store_true",
        help=(
            "scan registered/explicit roots without adding default, environment, or ancestor roots"
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
    roots_add.add_argument("path", type=_expanded_path, help="native agent data/home root")
    roots_add.add_argument(
        "--format",
        choices=tuple(AgentFormat),
        required=True,
        help="native home format",
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
        "search", help="search native titles/names and UUIDs by keyword"
    )
    search_parser.add_argument(
        "query",
        help="case-insensitive keywords (all must match a title/name, ID, or enabled path)",
    )
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
                session = None
                virtual_source_environ = None
                if args.catalog_id or args.title:
                    if args.source_id:
                        raise SessionMigrateError(
                            "pass only one of SOURCE_ID, --catalog-id, or --title"
                        )
                    if args.catalog_id and args.title:
                        raise SessionMigrateError("pass either --catalog-id or --title, not both")
                    if args.source_home or args.source_cwd:
                        raise SessionMigrateError(
                            "--source-home/--source-cwd do not apply with catalog selection"
                        )
                    with Catalog(_catalog_path(args)) as catalog:
                        catalog_id = args.catalog_id
                        if args.title:
                            title = args.title.strip()
                            if not title:
                                raise SessionMigrateError("--title cannot be empty")
                            source_filter = (
                                AgentFormat(args.source_agent) if args.source_agent else None
                            )
                            matches = catalog.list_sessions(
                                query=title,
                                include_paths=True,
                                agent_format=source_filter,
                                limit=10_000,
                            )
                            exact = [
                                match
                                for match in matches
                                if match.title and match.title.casefold() == title.casefold()
                            ]
                            candidates = exact or matches
                            if not candidates:
                                raise SessionMigrateError(
                                    "catalog title was not found; refresh the catalog "
                                    "or search first"
                                )
                            if len(candidates) != 1:
                                raise SessionMigrateError(
                                    "catalog title is ambiguous; use catalog search "
                                    "and --catalog-id"
                                )
                            catalog_id = candidates[0].catalog_id
                        assert catalog_id is not None
                        entry = catalog.get_session(catalog_id, include_paths=True)
                        source_reference = catalog.session_source_for_transfer(catalog_id)
                    source_format = source_reference.format
                    source_path = source_reference.path
                    if args.source_agent and AgentFormat(args.source_agent) != source_format:
                        raise SessionMigrateError(
                            "--from does not match the catalog session format"
                        )
                    requested_source_id = entry.session_id
                    if source_format in {AgentFormat.OPENCODE, AgentFormat.KILO}:
                        virtual_source_environ = dict(os.environ)
                        virtual_source_environ["XDG_DATA_HOME"] = str(source_reference.root.parent)
                else:
                    if not args.source_id or not args.source_agent:
                        raise SessionMigrateError(
                            "transfer requires SOURCE_ID with --from, --catalog-id, or --title"
                        )
                    source_format = AgentFormat(args.source_agent)
                    requested_source_id = normalized_source_id(source_format, args.source_id)
                    if source_format in {AgentFormat.OPENCODE, AgentFormat.KILO}:
                        if args.source_home or args.source_cwd:
                            raise SessionMigrateError(
                                f"{source_format.value} source transfer uses its normal "
                                "HOME/XDG environment; "
                                "--source-home/--source-cwd do not apply"
                            )
                        loader = (
                            load_opencode_session
                            if source_format == AgentFormat.OPENCODE
                            else load_kilo_session
                        )
                        session = loader(requested_source_id, source_cli=args.source_cli)
                    else:
                        source_home = args.source_home or default_target_home(source_format)
                        source_path = locate_session(
                            source_format,
                            requested_source_id,
                            source_home,
                            cwd=args.source_cwd,
                        )
                if args.source_cli and source_format not in {
                    AgentFormat.OPENCODE,
                    AgentFormat.KILO,
                }:
                    raise SessionMigrateError("--source-cli applies only to OpenCode/Kilo transfer")
                if session is None:
                    if source_format in {AgentFormat.OPENCODE, AgentFormat.KILO}:
                        if not requested_source_id:
                            raise SessionMigrateError(
                                f"cataloged {source_format.value} session is missing its "
                                "native session ID"
                            )
                        loader = (
                            load_opencode_session
                            if source_format == AgentFormat.OPENCODE
                            else load_kilo_session
                        )
                        session = loader(
                            requested_source_id,
                            source_cli=args.source_cli,
                            environ=virtual_source_environ,
                        )
                    else:
                        assert source_path is not None
                        session = load_session(source_path, source_format)
                if not session.session_id:
                    raise SessionMigrateError(
                        "discovered transcript has no native session ID metadata"
                    )
                if requested_source_id and (
                    normalized_source_id(source_format, session.session_id) != requested_source_id
                ):
                    raise SessionMigrateError(
                        "discovered transcript metadata does not match the source UUID"
                    )
                if args.to:
                    target_format = TargetFormat(args.to)
                elif source_format == AgentFormat.CLAUDE:
                    target_format = TargetFormat.CODEX
                elif source_format == AgentFormat.CODEX:
                    target_format = TargetFormat.CLAUDE
                else:
                    raise SessionMigrateError(
                        f"{source_format.value} source transfer requires an explicit --to target"
                    )
            else:
                source_format = AgentFormat(args.format) if args.format else None
                session = load_session(args.path, source_format)
                target_format = TargetFormat(args.to)
            if target_format == TargetFormat.OPENCODE and getattr(args, "home", None):
                raise SessionMigrateError(OPENCODE_HOME_UNSUPPORTED)
            if target_format == TargetFormat.KILO and getattr(args, "home", None):
                raise SessionMigrateError(KILO_HOME_UNSUPPORTED)
            if args.target_cli and (
                target_format
                not in {
                    TargetFormat.OPENCODE,
                    TargetFormat.KILO,
                    TargetFormat.ANTIGRAVITY,
                    TargetFormat.CURSOR,
                }
                or args.command == "convert"
            ):
                raise SessionMigrateError(
                    "--target-cli only applies to OpenCode/Kilo/Antigravity/Cursor import "
                    "and transfer"
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
                manifest_path = output_path.with_name(f"{output_path.name}.session-migrate.json")
                dry_run = False
            elif target_format == TargetFormat.OPENCODE:
                output_path = f"opencode:{artifact.session_id}"
                manifest_path = opencode_manifest_path(artifact)
                dry_run = args.dry_run
            elif target_format == TargetFormat.KILO:
                output_path = f"kilo:{artifact.session_id}"
                manifest_path = kilo_manifest_path(artifact)
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
            elif target_format == TargetFormat.KILO and args.command != "convert":
                install_kilo_artifact(
                    artifact,
                    manifest_path=manifest_path,
                    target_cli=args.target_cli,
                    dry_run=dry_run,
                )
            elif target_format == TargetFormat.COPILOT and args.command != "convert":
                install_copilot_artifact(
                    artifact,
                    target_home=home,
                    dry_run=dry_run,
                )
            elif target_format == TargetFormat.ANTIGRAVITY and args.command != "convert":
                install_antigravity_artifact(
                    artifact,
                    target_home=home,
                    target_cli=args.target_cli,
                    dry_run=dry_run,
                )
            elif target_format == TargetFormat.CURSOR and args.command != "convert":
                install_cursor_artifact(
                    artifact,
                    target_home=home,
                    target_cli=args.target_cli,
                    dry_run=dry_run,
                )
            elif target_format == TargetFormat.VIBE and args.command != "convert":
                install_vibe_artifact(
                    artifact,
                    target_home=home,
                    dry_run=dry_run,
                )
            elif target_format == TargetFormat.KIMI and args.command != "convert":
                install_kimi_artifact(
                    artifact,
                    target_home=home,
                    dry_run=dry_run,
                )
            elif target_format == TargetFormat.GROK and args.command != "convert":
                install_grok_artifact(artifact, target_home=home, dry_run=dry_run)
            elif target_format == TargetFormat.OPENHANDS and args.command != "convert":
                install_openhands_artifact(artifact, target_home=home, dry_run=dry_run)
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
    except SessionMigrateError as exc:
        print(f"session-migrate: error: {exc}", file=sys.stderr)
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
        help=(
            "OpenCode, Kilo, Antigravity, or Cursor executable for native import "
            "(otherwise resolve the pinned CLI from its normal location/PATH)"
        ),
    )
    parser.add_argument(
        "--model-provider",
        help="Codex/Pi/OMP/OpenCode/Muse provider ID (target-specific default)",
    )
    parser.add_argument(
        "--model",
        help=(
            "Claude/Pi/OMP/OpenCode/Kilo/Copilot/Antigravity/Vibe/Muse/Qwen/Kimi/"
            "Grok/OpenHands target model label"
        ),
    )


def _expanded_path(value: str) -> Path:
    """Expand a user-supplied home marker consistently for every CLI path."""

    return Path(value).expanduser()


def _add_catalog_query_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--format", choices=tuple(AgentFormat), help="filter by source format")
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
                pi_roots=args.pi_root,
                omp_roots=args.omp_root,
                opencode_roots=args.opencode_root,
                copilot_roots=args.copilot_root,
                antigravity_roots=args.antigravity_root,
                cursor_roots=args.cursor_root,
                vibe_roots=args.vibe_root,
                muse_roots=args.muse_root,
                qwen_roots=args.qwen_root,
                kimi_roots=args.kimi_root,
                grok_roots=args.grok_root,
                kilo_roots=args.kilo_root,
                openhands_roots=args.openhands_root,
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
                    raise SessionMigrateError("catalog root ID was not found")
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
    raise SessionMigrateError("catalog subcommand is not implemented")


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
