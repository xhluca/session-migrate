"""Antigravity Desktop 2.18.1 SQLite/protobuf session adapter.

This module adapts conversations to and from the Antigravity Desktop application
(macOS/Electron), which stores individual cascade conversations as SQLite databases
under ``~/.gemini/antigravity/conversations/<uuid>.db`` and registers metadata in
``~/.gemini/antigravity/conversation_summaries.db``.

While the table schemas match the Antigravity CLI format, Desktop differs in:
  - ``trajectory_meta.source``: 1 (IDE / Hub) rather than 17 (CLI).
  - ``trajectory_metadata_blob.project_id``: ``"outside-of-project"`` (or project UUID)
    rather than ``"default-cli-project"``.
  - ``conversation_summaries``:
      - ``source``: ``""``
      - ``app_data_dir``: ``"antigravity"``
      - ``project_id``: ``"outside-of-project"``
      - ``status``: ``"CASCADE_RUN_STATUS_IDLE"``
  - Subagents: field 6 of ``trajectory_metadata_blob`` points to the root cascade ID,
    so subagent conversations have ``root != conversation_id`` and ``nesting_depth >= 1``.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import sys
from collections import Counter
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import quote

from session_migrate.errors import SessionMigrateError
from session_migrate.formats import antigravity
from session_migrate.formats.antigravity import (
    _SUMMARY_COLUMNS,
    STEP_STATUS_DONE,
    STEP_STATUS_ERROR,
    STEP_TYPE_GENERIC,
    STEP_TYPE_PLANNER_RESPONSE,
    STEP_TYPE_USER_INPUT,
    InstalledAntigravitySession,
    ParsedAntigravitySession,
    _absolute_no_follow,
    _database_from_bytes,
    _ensure_private_directory,
    _ensure_summary_database,
    _event_timestamp,
    _field_bytes,
    _field_text,
    _field_varint,
    _generic_argument_entries,
    _guard_matches_path,
    _last_user_input_index,
    _omission_key,
    _open_identity_guard,
    _read_summary_metadata,
    _require_uuid4,
    _session_id_from_path,
    _StepRow,
    _stream_sha256,
    _summary_values,
    _unlink_if_same_file,
    _utc_now,
    _validate_summary_schema,
    content_text,
    valid_rfc3339,
)
from session_migrate.jsonl import write_private_atomic
from session_migrate.model import AgentFormat, EventKind, Role, Session

PINNED_ANTIGRAVITY_DESKTOP_VERSION = "2.18.1"

# macOS arm64
PINNED_ANTIGRAVITY_DESKTOP_MACOS_ARM64_SHA256 = (
    "300ee20f3108a511be1149602b91ba84cbbdcc42213067151e255584129be30f"
)
PINNED_ANTIGRAVITY_DESKTOP_MACOS_ARM64_SIZE = 148_990_608

# Linux x64
PINNED_ANTIGRAVITY_DESKTOP_LINUX_X64_SHA256 = (
    "ab4937445fa3817bc374a1db71062de7daecdb093cbfb6bd36b80f443198670e"
)
PINNED_ANTIGRAVITY_DESKTOP_LINUX_X64_SIZE = 181_432_528

# Windows x64
PINNED_ANTIGRAVITY_DESKTOP_WINDOWS_X64_SHA256 = (
    "1ed84e6a1d1e51064d80c9f382ab3a519eb2775cb91d552c63064a18cfdf3cf2"
)
PINNED_ANTIGRAVITY_DESKTOP_WINDOWS_X64_SIZE = 163_640_320

PINNED_DESKTOP_SPECS: dict[str, tuple[int, str]] = {
    "darwin": (
        PINNED_ANTIGRAVITY_DESKTOP_MACOS_ARM64_SIZE,
        PINNED_ANTIGRAVITY_DESKTOP_MACOS_ARM64_SHA256,
    ),
    "linux": (
        PINNED_ANTIGRAVITY_DESKTOP_LINUX_X64_SIZE,
        PINNED_ANTIGRAVITY_DESKTOP_LINUX_X64_SHA256,
    ),
    "win32": (
        PINNED_ANTIGRAVITY_DESKTOP_WINDOWS_X64_SIZE,
        PINNED_ANTIGRAVITY_DESKTOP_WINDOWS_X64_SHA256,
    ),
}

DEFAULT_MACOS_LANGUAGE_SERVER_PATH = Path(
    "/Applications/Antigravity.app/Contents/Resources/bin/language_server"
)
DEFAULT_LINUX_LANGUAGE_SERVER_PATH = Path("/opt/Antigravity/resources/bin/language_server")
DEFAULT_WINDOWS_LANGUAGE_SERVER_PATH = Path(
    "Programs/antigravity/resources/bin/language_server.exe"
)


def default_language_server_path(
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Resolve the default language server binary path for the target platform.

    Can be overridden via the ``SESSION_MIGRATE_ANTIGRAVITY_DESKTOP_BIN``
    environment variable.
    """
    env = os.environ if environ is None else environ
    override = env.get("SESSION_MIGRATE_ANTIGRAVITY_DESKTOP_BIN")
    if override:
        return Path(override).expanduser()

    current_platform = sys.platform if platform is None else platform
    if current_platform == "darwin":
        return DEFAULT_MACOS_LANGUAGE_SERVER_PATH
    if current_platform == "win32":
        local_app_data = env.get("LOCALAPPDATA")
        if local_app_data:
            for sub in (
                "Programs/antigravity",
                "Programs/Antigravity",
                "programs/antigravity",
                "programs/Antigravity",
            ):
                candidate = Path(local_app_data) / sub / "resources" / "bin" / "language_server.exe"
                if candidate.is_file():
                    return candidate
        prog_files = env.get("PROGRAMFILES")
        if prog_files:
            for sub in ("antigravity", "Antigravity"):
                candidate = Path(prog_files) / sub / "resources" / "bin" / "language_server.exe"
                if candidate.is_file():
                    return candidate
        if local_app_data:
            return Path(local_app_data) / DEFAULT_WINDOWS_LANGUAGE_SERVER_PATH
        return Path("C:\\Program Files\\antigravity\\resources\\bin\\language_server.exe")

    # Linux / other Unix: check PATH launcher, then common installation directories
    launcher = shutil.which("antigravity", path=env.get("PATH"))
    if launcher:
        cand = Path(launcher).resolve().parent / "resources" / "bin" / "language_server"
        if cand.is_file():
            return cand
    user_home = (home or Path.home()).expanduser()
    for cand in (
        Path("/opt/Antigravity/resources/bin/language_server"),
        Path("/opt/Antigravity-x64/resources/bin/language_server"),
        Path("/opt/antigravity/resources/bin/language_server"),
        user_home / "Antigravity-x64/resources/bin/language_server",
        user_home / ".local/share/Antigravity/resources/bin/language_server",
        user_home / ".local/share/antigravity/resources/bin/language_server",
        Path("/usr/lib/antigravity/resources/bin/language_server"),
    ):
        if cand.is_file():
            return cand
    return DEFAULT_LINUX_LANGUAGE_SERVER_PATH


def electron_user_data_dir(
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Return the Electron userData directory where app_storage.json resides."""
    env = os.environ if environ is None else environ
    current_platform = sys.platform if platform is None else platform
    user_home = (home or Path.home()).expanduser()

    if current_platform == "darwin":
        return user_home / "Library" / "Application Support" / "Antigravity"
    if current_platform == "win32":
        app_data = env.get("APPDATA")
        if app_data:
            return Path(app_data) / "Antigravity"
        return user_home / "AppData" / "Roaming" / "Antigravity"

    # Linux / XDG standard
    xdg_config = env.get("XDG_CONFIG_HOME")
    if xdg_config:
        return Path(xdg_config) / "Antigravity"
    return user_home / ".config" / "Antigravity"


MAX_NATIVE_BYTES = antigravity.MAX_NATIVE_BYTES
PROJECT_ID = antigravity.PROJECT_ID_DESKTOP  # "outside-of-project"
SOURCE_DESKTOP = antigravity.SOURCE_DESKTOP  # 1

session_relative_path = antigravity.session_relative_path
snapshot_database_bytes = antigravity.snapshot_database_bytes


def app_data_home(home: Path | None = None) -> Path:
    """Return the Antigravity Desktop store below a chosen process HOME."""
    return (home or Path.home()).expanduser() / ".gemini" / "antigravity"


def serialize(
    session: Session,
    *,
    session_id: str,
    cwd: Path,
    cli_version: str = PINNED_ANTIGRAVITY_DESKTOP_VERSION,
    model: str | None = None,
    timestamp: str | None = None,
    trajectory_id: str | None = None,
) -> tuple[bytes, dict[str, int]]:
    """Serialize portable conversation history as an Antigravity Desktop SQLite DB."""
    del cli_version, model
    _require_uuid4(session_id, "Antigravity conversation ID")
    import uuid

    target_trajectory_id = trajectory_id or str(uuid.uuid4())
    _require_uuid4(target_trajectory_id, "Antigravity trajectory ID")
    if target_trajectory_id == session_id:
        raise SessionMigrateError("Antigravity conversation and trajectory IDs must differ")

    started_at = valid_rfc3339(timestamp) or valid_rfc3339(session.started_at) or _utc_now()
    dropped: Counter[str] = Counter()
    rows: list[_StepRow] = []
    pending_calls: list[tuple[str | None, str, str, Any]] = []
    generated_call_number = 0

    def append_step(step_type: int, status: int, native_payload: bytes) -> None:
        outer = _field_varint(1, step_type) + _field_varint(4, status)
        outer += _field_bytes(antigravity._STEP_PAYLOAD_FIELDS[step_type], native_payload)
        rows.append(_StepRow(len(rows), step_type, status, None, outer))

    for event in session.events:
        _event_timestamp(event, started_at, dropped)
        if event.kind == EventKind.MESSAGE and event.role == Role.USER:
            if event.text:
                append_step(STEP_TYPE_USER_INPUT, STEP_STATUS_DONE, _field_text(2, event.text))
            continue
        if event.kind == EventKind.MESSAGE and event.role == Role.ASSISTANT:
            if event.text:
                planner = _field_text(1, event.text) + _field_text(6, str(uuid.uuid4()))
                append_step(STEP_TYPE_PLANNER_RESPONSE, STEP_STATUS_DONE, planner)
            continue
        if event.kind == EventKind.TOOL_CALL and event.role == Role.ASSISTANT:
            call_id = event.tool_call_id or f"session-migrate-{session_id}-{len(rows)}"
            name = event.tool_name or "unknown_tool"
            raw_input = event.payload.get("input", {})
            import json

            arguments_json = json.dumps(
                raw_input if isinstance(raw_input, dict) else {},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            native_call = (
                _field_text(1, call_id) + _field_text(2, name) + _field_text(3, arguments_json)
            )
            planner = _field_text(6, str(uuid.uuid4())) + _field_bytes(7, native_call)
            pending_calls.append((event.tool_call_id, call_id, name, raw_input))
            append_step(STEP_TYPE_PLANNER_RESPONSE, STEP_STATUS_DONE, planner)
            continue
        if event.kind == EventKind.TOOL_RESULT and event.role == Role.TOOL:
            match_index: int | None = None
            if event.tool_call_id:
                matching_indices = [
                    index
                    for index, pending in enumerate(pending_calls)
                    if pending[0] == event.tool_call_id
                ]
                if event.tool_name:
                    named_indices = [
                        index
                        for index in matching_indices
                        if pending_calls[index][2] == event.tool_name
                    ]
                    if named_indices:
                        matching_indices = named_indices
                match_index = next(iter(matching_indices), None)
            elif pending_calls:
                match_index = 0
                dropped["tool_result:missing_id"] += 1
            if match_index is not None:
                _, call_id, tool_name, tool_input = pending_calls.pop(match_index)
            else:
                generated_call_number += 1
                call_id = event.tool_call_id or (
                    f"session-migrate-{session_id}-{generated_call_number}"
                )
                tool_name = event.tool_name or "unknown_tool"
                tool_input = {}
                import json

                native_call = (
                    _field_text(1, call_id) + _field_text(2, tool_name) + _field_text(3, "{}")
                )
                append_step(
                    STEP_TYPE_PLANNER_RESPONSE,
                    STEP_STATUS_DONE,
                    _field_text(6, str(uuid.uuid4())) + _field_bytes(7, native_call),
                )
                dropped["tool_result:orphan_id"] += 1
            generic = b"".join(_generic_argument_entries(tool_input, dropped))
            result_text = event.text or content_text(event.payload.get("content_blocks"))
            linkage_entry = _field_text(1, "session_migrate_call_id") + _field_text(2, call_id)
            generic_result = _field_text(1, result_text or "") + _field_bytes(2, linkage_entry)
            generic += _field_bytes(2, generic_result)
            append_step(
                STEP_TYPE_GENERIC,
                STEP_STATUS_ERROR if event.payload.get("is_error") is True else STEP_STATUS_DONE,
                generic,
            )
            blocks = event.payload.get("content_blocks")
            if isinstance(blocks, list):
                for block in blocks:
                    if not isinstance(block, dict) or block.get("type") != "text":
                        dropped["tool_result:non_text_block"] += 1
            continue
        if event.kind == EventKind.THINKING:
            dropped["thinking:private"] += 1
        elif event.kind == EventKind.COMPACTION:
            dropped["compaction:no_stored_native_equivalent"] += 1
        elif event.kind == EventKind.MESSAGE:
            dropped["message:privileged_role"] += 1
        else:
            dropped[_omission_key(event)] += 1

    if not any(row.step_type == STEP_TYPE_USER_INPUT for row in rows):
        raise SessionMigrateError(
            "Antigravity Desktop target requires at least one portable user message"
        )

    data = antigravity._build_database(
        rows,
        conversation_id=session_id,
        trajectory_id=target_trajectory_id,
        started_at=started_at,
        source=SOURCE_DESKTOP,
        project_id=PROJECT_ID,
    )
    validate_native_bytes(data, session_id)
    return data, dict(sorted(dropped.items()))


def parse(path: Path) -> ParsedAntigravitySession:
    """Read one Desktop conversation DB through a consistent SQLite backup snapshot."""
    snapshot = snapshot_database_bytes(path)
    parsed = antigravity._parse_database_bytes(
        snapshot,
        expected_session_id=_session_id_from_path(path),
        expected_source=SOURCE_DESKTOP,
        expected_project_id=PROJECT_ID,
        is_desktop=True,
        cli_version=PINNED_ANTIGRAVITY_DESKTOP_VERSION,
    )
    title, cwd = _read_summary_metadata(path, parsed.session_id)
    return replace(parsed, title=title, cwd=cwd)


def parse_session(path: Path) -> Session:
    """Parse Antigravity Desktop as a first-class source."""
    parsed = parse(path)
    return Session(
        source_format=AgentFormat.ANTIGRAVITY_DESKTOP,
        source_path=path.resolve(),
        source_sha256=parsed.snapshot_sha256,
        session_id=parsed.session_id,
        cwd=parsed.cwd,
        started_at=parsed.started_at,
        cli_version=parsed.cli_version,
        model=parsed.model,
        title=parsed.title,
        events=parsed.events,
        raw_record_count=parsed.raw_record_count,
        model_provider="google",
    )


def validate_native_bytes(data: bytes, session_id: str) -> None:
    """Strictly validate a generated Antigravity Desktop conversation database."""
    _require_uuid4(session_id, "Antigravity conversation ID")
    parsed = antigravity._parse_database_bytes(
        data,
        expected_session_id=session_id,
        generated=True,
        expected_source=SOURCE_DESKTOP,
        expected_project_id=PROJECT_ID,
        is_desktop=True,
        cli_version=PINNED_ANTIGRAVITY_DESKTOP_VERSION,
    )
    if not any(
        event.kind == EventKind.MESSAGE and event.role == Role.USER for event in parsed.events
    ):
        raise SessionMigrateError("generated Antigravity Desktop session contains no user message")


def native_record_count(data: bytes) -> int:
    """Return the number of stored steps after full byte validation."""
    with _database_from_bytes(data) as db:
        antigravity._validate_database(
            db,
            expected_session_id=None,
            generated=True,
            expected_source=SOURCE_DESKTOP,
            expected_project_id=PROJECT_ID,
            is_desktop=True,
        )
        return int(db.execute("SELECT count(*) FROM steps").fetchone()[0])


def verify_pinned_desktop(
    executable: Path | None = None,
    *,
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Resolve and verify the Antigravity Desktop language server binary."""
    values = dict(os.environ if environ is None else environ)
    current_platform = sys.platform if platform is None else platform
    candidate: str | None = None
    if executable:
        candidate = str(executable)
    elif values.get("SESSION_MIGRATE_ANTIGRAVITY_DESKTOP_BIN"):
        candidate = values["SESSION_MIGRATE_ANTIGRAVITY_DESKTOP_BIN"]
    elif values.get("ANTIGRAVITY_DESKTOP_BIN"):
        candidate = values["ANTIGRAVITY_DESKTOP_BIN"]
    elif values.get("ANTIGRAVITY_LANGUAGE_SERVER_BIN"):
        candidate = values["ANTIGRAVITY_LANGUAGE_SERVER_BIN"]
    elif default_language_server_path(platform=current_platform, environ=values).is_file():
        candidate = str(default_language_server_path(platform=current_platform, environ=values))
    else:
        candidate = shutil.which("language_server", path=values.get("PATH"))

    if not candidate:
        raise SessionMigrateError("Antigravity Desktop executable 'language_server' was not found")
    path = Path(candidate).expanduser().resolve()
    try:
        info = path.stat()
    except OSError as exc:
        raise SessionMigrateError("cannot inspect the Antigravity Desktop executable") from exc
    if not stat.S_ISREG(info.st_mode):
        raise SessionMigrateError("Antigravity Desktop executable is not a regular file")

    if values.get("SESSION_MIGRATE_UNVALIDATED_DESKTOP_BIN") == "1":
        return path

    if current_platform not in PINNED_DESKTOP_SPECS:
        raise SessionMigrateError(
            f"Antigravity Desktop native binary verification is not supported on "
            f"{current_platform}; supported platforms are darwin, linux, win32. "
            "Set SESSION_MIGRATE_UNVALIDATED_DESKTOP_BIN=1 to proceed with an unvalidated binary."
        )

    expected_size, expected_digest = PINNED_DESKTOP_SPECS[current_platform]
    if info.st_size != expected_size:
        raise SessionMigrateError(
            f"Antigravity Desktop binary size mismatch on {current_platform}: expected "
            f"{expected_size} bytes, observed {info.st_size}"
        )
    digest = _stream_sha256(path, maximum=expected_size)
    if digest != expected_digest:
        raise SessionMigrateError(
            f"Antigravity Desktop binary digest mismatch on {current_platform}; "
            "refusing private-store installation"
        )
    return path


def install_database(
    data: bytes,
    *,
    session_id: str,
    cwd: Path,
    timestamp: str,
    title: str | None,
    target_home: Path,
    target_cli: Path | None = None,
    dry_run: bool = False,
    environ: Mapping[str, str] | None = None,
) -> InstalledAntigravitySession:
    """Install a validated conversation and its desktop summary without overwrite."""
    validate_native_bytes(data, session_id)
    if target_cli is not None or os.environ.get("SESSION_MIGRATE_VERIFY_DESKTOP_BIN") == "1":
        verify_pinned_desktop(target_cli, environ=environ)
    root = _absolute_no_follow(target_home)
    conversation_path = root / session_relative_path(session_id)
    summaries_path = root / "conversation_summaries.db"
    cwd = cwd.expanduser().resolve()
    summary_timestamp = valid_rfc3339(timestamp) or _utc_now()
    parsed = antigravity._parse_database_bytes(
        data,
        expected_session_id=session_id,
        generated=True,
        expected_source=SOURCE_DESKTOP,
        expected_project_id=PROJECT_ID,
        is_desktop=True,
        cli_version=PINNED_ANTIGRAVITY_DESKTOP_VERSION,
    )
    preview = next(
        (
            event.text
            for event in parsed.events
            if event.kind == EventKind.MESSAGE and event.role == Role.USER and event.text
        ),
        "Imported conversation",
    )
    summary = _summary_values(
        session_id=session_id,
        title=title or "Imported conversation",
        preview=preview,
        step_count=parsed.raw_record_count,
        timestamp=summary_timestamp,
        cwd=cwd,
        last_user_input_index=_last_user_input_index(data),
        status="CASCADE_RUN_STATUS_IDLE",
        source="",
        project_id=PROJECT_ID,
        app_data_dir="antigravity",
    )

    if dry_run:
        if os.path.lexists(conversation_path):
            raise SessionMigrateError(
                "Antigravity Desktop conversation ID already exists; refusing to overwrite it"
            )
        if os.path.lexists(summaries_path):
            if stat.S_ISLNK(summaries_path.lstat().st_mode):
                raise SessionMigrateError(
                    "Antigravity Desktop summary database must not be a symlink"
                )
            try:
                uri = f"file:{quote(str(summaries_path), safe='/')}?mode=ro"
                with sqlite3.connect(uri, uri=True, timeout=5) as summaries:
                    summaries.execute("PRAGMA trusted_schema=OFF")
                    _validate_summary_schema(summaries)
                    collision = summaries.execute(
                        "SELECT 1 FROM conversation_summaries WHERE conversation_id=?",
                        (session_id,),
                    ).fetchone()
            except sqlite3.Error as exc:
                raise SessionMigrateError(
                    "Antigravity Desktop summary database cannot be checked safely"
                ) from exc
            if collision:
                raise SessionMigrateError(
                    "Antigravity Desktop conversation ID already exists; refusing to overwrite it"
                )
        return InstalledAntigravitySession(conversation_path, summaries_path)

    _ensure_private_directory(root)
    _ensure_private_directory(conversation_path.parent)
    _ensure_summary_database(summaries_path)
    created_identity: tuple[int, int] | None = None
    conversation_guard: int | None = None
    summary_guard: int | None = None
    try:
        summary_guard = _open_identity_guard(summaries_path, writable=True)
        with sqlite3.connect(summaries_path, timeout=15, isolation_level=None) as summaries:
            summaries.execute("PRAGMA trusted_schema=OFF")
            _validate_summary_schema(summaries)
            summaries.execute("BEGIN IMMEDIATE")
            collision = summaries.execute(
                "SELECT 1 FROM conversation_summaries WHERE conversation_id=?", (session_id,)
            ).fetchone()
            if collision or os.path.lexists(conversation_path):
                summaries.execute("ROLLBACK")
                raise SessionMigrateError(
                    "Antigravity Desktop conversation ID already exists; refusing to overwrite it"
                )
            created_identity = write_private_atomic(conversation_path, data)
            conversation_guard = _open_identity_guard(
                conversation_path, expected_identity=created_identity
            )
            summaries.execute(
                f"INSERT INTO conversation_summaries({','.join(_SUMMARY_COLUMNS)}) "
                f"VALUES({','.join('?' for _ in _SUMMARY_COLUMNS)})",
                summary,
            )
            if not _guard_matches_path(conversation_guard, conversation_path) or not (
                _guard_matches_path(summary_guard, summaries_path)
            ):
                summaries.execute("ROLLBACK")
                raise SessionMigrateError(
                    "Antigravity Desktop install paths changed during transaction"
                )
            summaries.execute("COMMIT")
    except BaseException:
        if created_identity is not None:
            _unlink_if_same_file(conversation_path, created_identity)
        raise
    finally:
        if conversation_guard is not None:
            os.close(conversation_guard)
        if summary_guard is not None:
            os.close(summary_guard)
    return InstalledAntigravitySession(conversation_path, summaries_path)
