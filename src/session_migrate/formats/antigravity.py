"""Antigravity CLI 1.1.16 SQLite/protobuf session adapter.

This module is a clean-room implementation of the small, observed wire subset
needed to move portable conversation history.  It does not contain generated
protobuf code, vendor descriptors, or copied vendor source.  Both the SQLite
schema and every protobuf field used here are pinned to the exact Linux x86_64
1.1.16 binary identified below; automatic installation fails for any other
binary.

The target writer intentionally emits only user messages, assistant messages,
and the render-proven generic tool trajectory.  Private thinking text, system
instructions, attachments, compaction provenance, and runtime policy are not
written and are included in the returned loss counter.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import uuid
from collections import Counter, deque
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

from session_migrate.errors import SessionMigrateError
from session_migrate.formats.common import content_text, string, valid_rfc3339
from session_migrate.jsonl import write_private_atomic
from session_migrate.model import AgentFormat, Event, EventKind, Provenance, Role, Session

PINNED_ANTIGRAVITY_VERSION = "1.1.16"
PINNED_ANTIGRAVITY_LINUX_X86_64_SHA256 = (
    "b233e6a4f38564a06a0d3220aa79f6a7c8f11da2b85fc8f0957f8a14d46e6cc9"
)
PINNED_ANTIGRAVITY_LINUX_X86_64_SIZE = 205_545_512

MAX_NATIVE_BYTES = 256 * 1024 * 1024
MAX_STEPS = 100_000
MAX_PROTO_FIELDS = 100_000
MAX_PROTO_VALUE_BYTES = 64 * 1024 * 1024
MAX_JSON_NODES = 100_000
MAX_TEXT_BYTES = 64 * 1024 * 1024
PROJECT_ID = "default-cli-project"

STEP_STATUS_DONE = 3
STEP_STATUS_ERROR = 7
STEP_TYPE_USER_INPUT = 14
STEP_TYPE_PLANNER_RESPONSE = 15
STEP_TYPE_VIEW_FILE = 8
STEP_TYPE_LIST_DIRECTORY = 9
STEP_TYPE_RUN_COMMAND = 21
STEP_TYPE_MCP_TOOL = 38
STEP_TYPE_FILE_CHANGE = 86
STEP_TYPE_CONVERSATION_HISTORY = 98
STEP_TYPE_SHELL_EXEC = 112
STEP_TYPE_WRITE_BLOB = 128
STEP_TYPE_GENERIC = 132

# Step oneof field numbers observed in the pinned producer.
_STEP_PAYLOAD_FIELDS = {
    STEP_TYPE_VIEW_FILE: 14,
    STEP_TYPE_LIST_DIRECTORY: 15,
    STEP_TYPE_USER_INPUT: 19,
    STEP_TYPE_PLANNER_RESPONSE: 20,
    STEP_TYPE_RUN_COMMAND: 28,
    23: 30,  # checkpoint
    STEP_TYPE_MCP_TOOL: 47,
    STEP_TYPE_FILE_CHANGE: 98,
    STEP_TYPE_CONVERSATION_HISTORY: 111,
    STEP_TYPE_SHELL_EXEC: 127,
    STEP_TYPE_WRITE_BLOB: 142,
    STEP_TYPE_GENERIC: 140,
}

_CONVERSATION_SCHEMA: dict[str, tuple[str, ...]] = {
    "trajectory_meta": ("trajectory_id", "cascade_id", "trajectory_type", "source"),
    "steps": (
        "idx",
        "step_type",
        "status",
        "has_subtrajectory",
        "metadata",
        "error_details",
        "permissions",
        "task_details",
        "render_info",
        "step_payload",
        "step_format",
    ),
    "gen_metadata": ("idx", "data", "size"),
    "executor_metadata": ("idx", "data"),
    "parent_references": ("idx", "data"),
    "trajectory_metadata_blob": ("id", "data"),
    "battle_mode_infos": ("idx", "data"),
}

_CONVERSATION_COLUMN_SCHEMA: dict[str, tuple[tuple[str, str, int, str | None, int], ...]] = {
    "trajectory_meta": (
        ("trajectory_id", "TEXT", 0, None, 1),
        ("cascade_id", "TEXT", 0, None, 0),
        ("trajectory_type", "INTEGER", 0, None, 0),
        ("source", "INTEGER", 0, None, 0),
    ),
    "steps": (
        ("idx", "INTEGER", 0, None, 1),
        ("step_type", "INTEGER", 1, "0", 0),
        ("status", "INTEGER", 1, "0", 0),
        ("has_subtrajectory", "numeric", 1, "false", 0),
        ("metadata", "BLOB", 0, None, 0),
        ("error_details", "BLOB", 0, None, 0),
        ("permissions", "BLOB", 0, None, 0),
        ("task_details", "BLOB", 0, None, 0),
        ("render_info", "BLOB", 0, None, 0),
        ("step_payload", "BLOB", 0, None, 0),
        ("step_format", "INTEGER", 1, "0", 0),
    ),
    "gen_metadata": (
        ("idx", "INTEGER", 0, None, 1),
        ("data", "BLOB", 0, None, 0),
        ("size", "INTEGER", 1, "0", 0),
    ),
    "executor_metadata": (
        ("idx", "INTEGER", 0, None, 1),
        ("data", "BLOB", 0, None, 0),
    ),
    "parent_references": (
        ("idx", "INTEGER", 0, None, 1),
        ("data", "BLOB", 0, None, 0),
    ),
    "trajectory_metadata_blob": (
        ("id", "TEXT", 0, '"main"', 1),
        ("data", "BLOB", 0, None, 0),
    ),
    "battle_mode_infos": (
        ("idx", "INTEGER", 0, None, 1),
        ("data", "BLOB", 0, None, 0),
    ),
}

_SUMMARY_COLUMNS = (
    "conversation_id",
    "title",
    "preview",
    "step_count",
    "last_modified_time",
    "workspace_uris",
    "status",
    "source",
    "project_id",
    "agent_name",
    "parent_conversation_id",
    "nesting_depth",
    "battle_id",
    "winning_conversation_id",
    "not_fully_idle",
    "killed",
    "last_user_input_time",
    "last_user_input_step_index",
    "app_data_dir",
)


@dataclass(frozen=True, slots=True)
class ParsedAntigravitySession:
    """Portable projection and source metadata from one conversation DB."""

    session_id: str
    trajectory_id: str
    cwd: Path | None
    started_at: str | None
    cli_version: str
    model: str | None
    title: str | None
    events: tuple[Event, ...]
    raw_record_count: int
    snapshot_sha256: str


@dataclass(frozen=True, slots=True)
class InstalledAntigravitySession:
    """Paths written by :func:`install_database`."""

    conversation_path: Path
    summaries_path: Path


@dataclass(frozen=True, slots=True)
class _WireField:
    number: int
    wire_type: int
    value: int | bytes


@dataclass(frozen=True, slots=True)
class _StepRow:
    index: int
    step_type: int
    status: int
    metadata: bytes | None
    payload: bytes


def serialize(
    session: Session,
    *,
    session_id: str,
    cwd: Path,
    cli_version: str = PINNED_ANTIGRAVITY_VERSION,
    model: str | None = None,
    timestamp: str | None = None,
    trajectory_id: str | None = None,
) -> tuple[bytes, dict[str, int]]:
    """Create a new independent Antigravity conversation database.

    ``cli_version`` is metadata for the conversion manifest.  The encoded
    schema always remains pinned to 1.1.16, and automatic installation later
    enforces both the executable version and digest.
    """

    # The minimal trajectory has no stable model/version metadata field.  The
    # orchestrator records both values in its external manifest.
    del cli_version, model
    _require_uuid4(session_id, "Antigravity conversation ID")
    target_trajectory_id = trajectory_id or str(uuid.uuid4())
    _require_uuid4(target_trajectory_id, "Antigravity trajectory ID")
    if target_trajectory_id == session_id:
        raise SessionMigrateError("Antigravity conversation and trajectory IDs must differ")

    started_at = valid_rfc3339(timestamp) or valid_rfc3339(session.started_at) or _utc_now()
    dropped: Counter[str] = Counter()
    rows: list[_StepRow] = []
    pending_calls: list[tuple[str | None, str, str, Any]] = []
    seen_call_ids: Counter[str] = Counter()
    generated_call_number = 0

    def append_step(step_type: int, status: int, native_payload: bytes) -> None:
        outer = _field_varint(1, step_type) + _field_varint(4, status)
        outer += _field_bytes(_STEP_PAYLOAD_FIELDS[step_type], native_payload)
        rows.append(_StepRow(len(rows), step_type, status, None, outer))

    for event in session.events:
        event_timestamp = _event_timestamp(event, started_at, dropped)
        del event_timestamp  # Per-step times are optional in the proven minimal schema.
        if event.kind == EventKind.MESSAGE and event.role == Role.USER:
            if event.text:
                append_step(STEP_TYPE_USER_INPUT, STEP_STATUS_DONE, _field_text(2, event.text))
            continue
        if event.kind == EventKind.MESSAGE and event.role == Role.ASSISTANT:
            if event.text:
                planner = _field_text(1, event.text) + _field_text(6, str(uuid.uuid4()))
                append_step(STEP_TYPE_PLANNER_RESPONSE, STEP_STATUS_DONE, planner)
            continue
        if event.kind == EventKind.TOOL_CALL:
            generated_call_number += 1
            source_call_id = event.tool_call_id
            target_call_id = source_call_id
            if not target_call_id:
                target_call_id = f"session-migrate-{session_id}-{generated_call_number}"
                dropped["tool_call:missing_id"] += 1
            elif seen_call_ids[target_call_id]:
                dropped["tool_call:duplicate_id"] += 1
                target_call_id = f"{target_call_id}-session-migrate-{generated_call_number}"
            seen_call_ids[target_call_id] += 1
            tool_name = event.tool_name or "unknown_tool"
            if not event.tool_name:
                dropped["tool_call:missing_name"] += 1
            if string(event.payload.get("namespace")):
                dropped["tool_call:namespace"] += 1
            tool_input = event.payload.get("input", {})
            try:
                _ensure_json_bounds(tool_input)
                arguments_json = json.dumps(
                    tool_input, ensure_ascii=False, separators=(",", ":"), allow_nan=False
                )
            except (TypeError, ValueError, SessionMigrateError):
                arguments_json = "{}"
                tool_input = {}
                dropped["tool_call:nonportable_input"] += 1
            native_call = (
                _field_text(1, target_call_id)
                + _field_text(2, tool_name)
                + _field_text(3, arguments_json)
            )
            planner = _field_text(6, str(uuid.uuid4())) + _field_bytes(7, native_call)
            append_step(STEP_TYPE_PLANNER_RESPONSE, STEP_STATUS_DONE, planner)
            pending_calls.append((source_call_id, target_call_id, tool_name, tool_input))
            continue
        if event.kind == EventKind.TOOL_RESULT:
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
                match_index = next(
                    iter(matching_indices),
                    None,
                )
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
        raise SessionMigrateError("Antigravity target requires at least one portable user message")

    data = _build_database(
        rows,
        conversation_id=session_id,
        trajectory_id=target_trajectory_id,
        started_at=started_at,
    )
    validate_native_bytes(data, session_id)
    return data, dict(sorted(dropped.items()))


def parse(path: Path) -> ParsedAntigravitySession:
    """Read one native DB through a consistent SQLite backup snapshot."""

    snapshot = snapshot_database_bytes(path)
    parsed = _parse_database_bytes(snapshot, expected_session_id=_session_id_from_path(path))
    title, cwd = _read_summary_metadata(path, parsed.session_id)
    return replace(parsed, title=title, cwd=cwd)


def parse_session(path: Path) -> Session:
    """Parse Antigravity as a first-class source without exposing private thought text."""

    parsed = parse(path)
    return Session(
        source_format=AgentFormat.ANTIGRAVITY,
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
    """Strictly validate a generated 1.1.16 conversation database."""

    _require_uuid4(session_id, "Antigravity conversation ID")
    parsed = _parse_database_bytes(data, expected_session_id=session_id, generated=True)
    if not any(
        event.kind == EventKind.MESSAGE and event.role == Role.USER for event in parsed.events
    ):
        raise SessionMigrateError("generated Antigravity session contains no user message")


def snapshot_database_bytes(path: Path) -> bytes:
    """Return a bounded, transactionally consistent copy, including live WAL state."""

    source = path.expanduser()
    try:
        info = source.lstat()
    except OSError as exc:
        raise SessionMigrateError("cannot inspect Antigravity conversation database") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SessionMigrateError("Antigravity conversation source must be a regular file")
    if info.st_size > MAX_NATIVE_BYTES:
        raise SessionMigrateError("Antigravity conversation exceeds the database safety limit")

    with tempfile.TemporaryDirectory(prefix="session-migrate-antigravity-snapshot-") as directory:
        target = Path(directory) / "snapshot.db"
        uri = f"file:{quote(str(source.resolve()), safe='/')}?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True, timeout=5) as origin:
                origin.execute("PRAGMA trusted_schema=OFF")
                with sqlite3.connect(target) as destination:
                    origin.backup(destination)
            data = target.read_bytes()
        except (OSError, sqlite3.Error) as exc:
            raise SessionMigrateError(
                "cannot make a consistent Antigravity database snapshot"
            ) from exc
    if not data or len(data) > MAX_NATIVE_BYTES:
        raise SessionMigrateError("Antigravity database snapshot violates the safety limit")
    return data


def session_relative_path(session_id: str) -> Path:
    _require_uuid4(session_id, "Antigravity conversation ID")
    return Path("conversations") / f"{session_id}.db"


def app_data_home(home: Path | None = None) -> Path:
    """Return the 1.1.16 CLI store below a chosen process HOME."""

    return (home or Path.home()).expanduser() / ".gemini" / "antigravity-cli"


def native_record_count(data: bytes) -> int:
    """Return the number of stored steps after full byte validation."""

    with _database_from_bytes(data) as db:
        _validate_database(db, expected_session_id=None, generated=True)
        return int(db.execute("SELECT count(*) FROM steps").fetchone()[0])


def verify_pinned_cli(
    executable: Path | None = None, *, environ: Mapping[str, str] | None = None
) -> Path:
    """Resolve and authenticate the exact tested Antigravity executable."""

    values = dict(os.environ if environ is None else environ)
    candidate = str(executable) if executable else shutil.which("agy", path=values.get("PATH"))
    if not candidate:
        raise SessionMigrateError("Antigravity CLI executable 'agy' was not found")
    path = Path(candidate).expanduser().resolve()
    try:
        info = path.stat()
    except OSError as exc:
        raise SessionMigrateError("cannot inspect the Antigravity CLI executable") from exc
    if not stat.S_ISREG(info.st_mode):
        raise SessionMigrateError("Antigravity CLI executable is not a regular file")
    if info.st_size != PINNED_ANTIGRAVITY_LINUX_X86_64_SIZE:
        raise SessionMigrateError(
            "Antigravity CLI binary mismatch: automatic import requires the exact "
            f"Linux x86_64 {PINNED_ANTIGRAVITY_VERSION} build"
        )
    digest = _stream_sha256(path, maximum=PINNED_ANTIGRAVITY_LINUX_X86_64_SIZE)
    if digest != PINNED_ANTIGRAVITY_LINUX_X86_64_SHA256:
        raise SessionMigrateError(
            "Antigravity CLI binary digest mismatch; refusing private-store installation"
        )
    safe_env = {
        key: value
        for key, value in values.items()
        if key in {"HOME", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR"}
    }
    safe_env.setdefault("LC_ALL", "C")
    # Even ``agy --version`` launches the updater in 1.1.16.  Verification
    # must be observational: never allow the exact executable being checked
    # to replace itself after its digest was accepted.
    safe_env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "1"
    try:
        completed = subprocess.run(
            [str(path), "--version"],
            env=safe_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=15,
            check=False,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SessionMigrateError("cannot query the Antigravity CLI version") from exc
    observed = completed.stdout.strip()
    if completed.returncode != 0 or observed != PINNED_ANTIGRAVITY_VERSION:
        raise SessionMigrateError(
            "Antigravity CLI version mismatch: expected "
            f"{PINNED_ANTIGRAVITY_VERSION}, observed {observed or '<unavailable>'}"
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
    """Install a validated conversation and its picker summary without overwrite.

    ``target_home`` is the Antigravity application directory (normally
    ``~/.gemini/antigravity-cli``), not process ``HOME``.  The conversation is
    published with atomic create-if-absent semantics while an immediate SQLite
    transaction reserves the matching summary ID.  A failed transaction removes
    only the newly created inode.  The workspace recent-selection cache is not
    touched because it represents active UI state, not transcript truth.
    """

    validate_native_bytes(data, session_id)
    verify_pinned_cli(target_cli, environ=environ)
    root = _absolute_no_follow(target_home)
    conversation_path = root / session_relative_path(session_id)
    summaries_path = root / "conversation_summaries.db"
    cwd = cwd.expanduser().resolve()
    summary_timestamp = valid_rfc3339(timestamp) or _utc_now()
    parsed = _parse_database_bytes(data, expected_session_id=session_id, generated=True)
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
    )

    if dry_run:
        if os.path.lexists(conversation_path):
            raise SessionMigrateError(
                "Antigravity conversation ID already exists; refusing to overwrite it"
            )
        if os.path.lexists(summaries_path):
            if stat.S_ISLNK(summaries_path.lstat().st_mode):
                raise SessionMigrateError("Antigravity summary database must not be a symlink")
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
                    "Antigravity summary database cannot be checked safely"
                ) from exc
            if collision:
                raise SessionMigrateError(
                    "Antigravity conversation ID already exists; refusing to overwrite it"
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
                    "Antigravity conversation ID already exists; refusing to overwrite it"
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
                raise SessionMigrateError("Antigravity install paths changed during transaction")
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


def _build_database(
    rows: Sequence[_StepRow],
    *,
    conversation_id: str,
    trajectory_id: str,
    started_at: str,
) -> bytes:
    with tempfile.TemporaryDirectory(prefix="session-migrate-antigravity-build-") as directory:
        path = Path(directory) / "conversation.db"
        try:
            with sqlite3.connect(path) as db:
                db.execute("PRAGMA journal_mode=DELETE")
                db.executescript(
                    """
                    CREATE TABLE trajectory_meta (
                      trajectory_id text, cascade_id text, trajectory_type integer, source integer,
                      PRIMARY KEY (trajectory_id)
                    );
                    CREATE TABLE steps (
                      idx integer, step_type integer NOT NULL DEFAULT 0,
                      status integer NOT NULL DEFAULT 0,
                      has_subtrajectory numeric NOT NULL DEFAULT false,
                      metadata blob, error_details blob, permissions blob, task_details blob,
                      render_info blob, step_payload blob,
                      step_format integer NOT NULL DEFAULT 0,
                      PRIMARY KEY (idx)
                    );
                    CREATE INDEX idx_steps_status ON steps(status);
                    CREATE INDEX idx_steps_step_type ON steps(step_type);
                    CREATE TABLE gen_metadata (
                      idx integer, data blob, size integer NOT NULL DEFAULT 0, PRIMARY KEY (idx)
                    );
                    CREATE TABLE executor_metadata (idx integer, data blob, PRIMARY KEY (idx));
                    CREATE TABLE parent_references (idx integer, data blob, PRIMARY KEY (idx));
                    CREATE TABLE trajectory_metadata_blob (
                      id text DEFAULT "main", data blob, PRIMARY KEY (id)
                    );
                    CREATE TABLE battle_mode_infos (idx integer, data blob, PRIMARY KEY (idx));
                    """
                )
                db.execute(
                    "INSERT INTO trajectory_meta VALUES(?,?,4,17)",
                    (trajectory_id, conversation_id),
                )
                db.executemany(
                    "INSERT INTO steps("
                    "idx,step_type,status,has_subtrajectory,metadata,step_payload,step_format"
                    ") VALUES(?,?,?,0,?,?,0)",
                    [
                        (row.index, row.step_type, row.status, row.metadata, row.payload)
                        for row in rows
                    ],
                )
                seconds = int(_parse_timestamp(started_at).timestamp())
                trajectory_metadata = (
                    _field_bytes(2, _field_varint(1, seconds))
                    + _field_text(6, conversation_id)
                    + _field_text(18, PROJECT_ID)
                )
                db.execute(
                    "INSERT INTO trajectory_metadata_blob(id,data) VALUES('main',?)",
                    (trajectory_metadata,),
                )
                result = db.execute("PRAGMA integrity_check").fetchone()
                if result != ("ok",):
                    raise SessionMigrateError(
                        "generated Antigravity database failed integrity check"
                    )
            data = path.read_bytes()
        except sqlite3.Error as exc:
            raise SessionMigrateError("cannot construct Antigravity conversation database") from exc
    if not data or len(data) > MAX_NATIVE_BYTES:
        raise SessionMigrateError("generated Antigravity database violates the safety limit")
    return data


def _parse_database_bytes(
    data: bytes, *, expected_session_id: str | None, generated: bool = False
) -> ParsedAntigravitySession:
    if not data or len(data) > MAX_NATIVE_BYTES:
        raise SessionMigrateError("Antigravity conversation violates the database safety limit")
    digest = hashlib.sha256(data).hexdigest()
    with _database_from_bytes(data) as db:
        conversation_id, trajectory_id, rows, started_at, metadata_losses = _validate_database(
            db, expected_session_id=expected_session_id, generated=generated
        )
        events = list(_project_events(rows))
        for reason, count in metadata_losses.items():
            for number in range(count):
                events.append(
                    Event(
                        kind=EventKind.OPAQUE,
                        payload={"reason": reason},
                        provenance=Provenance(len(rows) + number, "antigravity_native_metadata"),
                    )
                )
    return ParsedAntigravitySession(
        session_id=conversation_id,
        trajectory_id=trajectory_id,
        cwd=None,
        started_at=started_at,
        cli_version=PINNED_ANTIGRAVITY_VERSION,
        model=None,
        title=None,
        events=tuple(events),
        raw_record_count=len(rows),
        snapshot_sha256=digest,
    )


@contextmanager
def _database_from_bytes(data: bytes) -> Iterator[sqlite3.Connection]:
    if not data or len(data) > MAX_NATIVE_BYTES:
        raise SessionMigrateError("Antigravity conversation violates the database safety limit")
    with tempfile.TemporaryDirectory(prefix="session-migrate-antigravity-validate-") as directory:
        path = Path(directory) / "conversation.db"
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
            uri = f"file:{quote(str(path), safe='/')}?mode=ro&immutable=1"
            db = sqlite3.connect(uri, uri=True, timeout=5)
            try:
                db.execute("PRAGMA query_only=ON")
                db.execute("PRAGMA trusted_schema=OFF")
                yield db
            finally:
                db.close()
        except SessionMigrateError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise SessionMigrateError(
                "Antigravity conversation is not a readable SQLite DB"
            ) from exc


def _validate_database(
    db: sqlite3.Connection, *, expected_session_id: str | None, generated: bool
) -> tuple[str, str, tuple[_StepRow, ...], str | None, dict[str, int]]:
    try:
        page_size = int(db.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(db.execute("PRAGMA page_count").fetchone()[0])
        if page_size <= 0 or page_count <= 0 or page_size * page_count > MAX_NATIVE_BYTES:
            raise SessionMigrateError("Antigravity database page bounds are invalid")
        result = db.execute("PRAGMA integrity_check(1)").fetchone()
        if result != ("ok",):
            raise SessionMigrateError("Antigravity database failed integrity check")
        _validate_conversation_schema(db)
        trajectory_rows = db.execute(
            "SELECT trajectory_id,cascade_id,trajectory_type,source FROM trajectory_meta"
        ).fetchall()
        main_rows = [row for row in trajectory_rows if row[2:] == (4, 17)]
        if len(main_rows) != 1:
            raise SessionMigrateError(
                "Antigravity database must contain one CLI cascade trajectory"
            )
        if generated and len(trajectory_rows) != 1:
            raise SessionMigrateError("generated Antigravity database has auxiliary trajectories")
        trajectory_id, conversation_id, trajectory_type, source = main_rows[0]
        if not isinstance(trajectory_id, str) or not isinstance(conversation_id, str):
            raise SessionMigrateError("Antigravity trajectory identifiers are invalid")
        _require_uuid4(trajectory_id, "Antigravity trajectory ID")
        _require_uuid4(conversation_id, "Antigravity conversation ID")
        if expected_session_id is not None and conversation_id != expected_session_id:
            raise SessionMigrateError("Antigravity database conversation ID does not match target")
        if trajectory_type != 4 or source != 17:
            raise SessionMigrateError("Antigravity trajectory is not a pinned CLI cascade")

        raw_metadata = db.execute(
            "SELECT id,data FROM trajectory_metadata_blob ORDER BY id"
        ).fetchall()
        if (
            len(raw_metadata) != 1
            or raw_metadata[0][0] != "main"
            or not isinstance(raw_metadata[0][1], bytes)
        ):
            raise SessionMigrateError("Antigravity trajectory metadata is missing or ambiguous")
        started_at, trajectory_metadata_loss = _validate_trajectory_metadata(
            raw_metadata[0][1], conversation_id, generated=generated
        )

        count = int(db.execute("SELECT count(*) FROM steps").fetchone()[0])
        if count <= 0 or count > MAX_STEPS:
            raise SessionMigrateError("Antigravity step count violates the safety limit")
        sql_rows = db.execute(
            "SELECT idx,step_type,status,metadata,step_payload,has_subtrajectory,step_format "
            "FROM steps ORDER BY idx"
        ).fetchall()
        rows: list[_StepRow] = []
        for expected_index, value in enumerate(sql_rows):
            index, step_type, status, metadata, payload, has_subtrajectory, step_format = value
            if index != expected_index:
                raise SessionMigrateError("Antigravity step indices are not contiguous")
            if (
                not isinstance(step_type, int)
                or not isinstance(status, int)
                or not isinstance(payload, bytes)
                or metadata is not None
                and not isinstance(metadata, bytes)
            ):
                raise SessionMigrateError("Antigravity step row has invalid storage types")
            if has_subtrajectory not in (0, False) or step_format != 0:
                raise SessionMigrateError(
                    "Antigravity step uses an unsupported trajectory encoding"
                )
            if len(payload) > MAX_PROTO_VALUE_BYTES or (
                isinstance(metadata, bytes) and len(metadata) > MAX_PROTO_VALUE_BYTES
            ):
                raise SessionMigrateError("Antigravity protobuf value exceeds the safety limit")
            _validate_step(payload, step_type=step_type, status=status, generated=generated)
            rows.append(_StepRow(index, step_type, status, metadata, payload))
        metadata_losses: Counter[str] = Counter()
        if trajectory_metadata_loss:
            metadata_losses["antigravity_incomplete_trajectory_metadata"] += 1
        if len(trajectory_rows) > 1:
            metadata_losses["antigravity_auxiliary_trajectory_metadata"] += len(trajectory_rows) - 1
        for table in (
            "gen_metadata",
            "executor_metadata",
            "parent_references",
            "battle_mode_infos",
        ):
            auxiliary_count = int(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            if generated and auxiliary_count:
                raise SessionMigrateError("generated Antigravity database has runtime metadata")
            if auxiliary_count:
                metadata_losses[f"antigravity_{table}"] += auxiliary_count
        return (
            conversation_id,
            trajectory_id,
            tuple(rows),
            started_at,
            dict(metadata_losses),
        )
    except SessionMigrateError:
        raise
    except sqlite3.Error as exc:
        raise SessionMigrateError("Antigravity database schema or rows are invalid") from exc


def _validate_conversation_schema(db: sqlite3.Connection) -> None:
    objects = db.execute(
        "SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    tables = {name for kind, name in objects if kind == "table"}
    if tables != set(_CONVERSATION_SCHEMA):
        raise SessionMigrateError("Antigravity database table set does not match pinned 1.1.16")
    if any(kind not in {"table", "index"} for kind, _ in objects):
        raise SessionMigrateError("Antigravity database contains unsupported schema objects")
    for table, expected_columns in _CONVERSATION_SCHEMA.items():
        table_info = db.execute(f"PRAGMA table_info('{table}')").fetchall()
        actual = tuple(row[1] for row in table_info)
        if actual != expected_columns:
            raise SessionMigrateError(f"Antigravity {table} columns do not match pinned 1.1.16")
        actual_schema = tuple((row[1], row[2], row[3], row[4], row[5]) for row in table_info)
        if actual_schema != _CONVERSATION_COLUMN_SCHEMA[table]:
            raise SessionMigrateError(
                f"Antigravity {table} declarations do not match pinned 1.1.16"
            )
    for index_name, table, columns in (
        ("idx_steps_status", "steps", ("status",)),
        ("idx_steps_step_type", "steps", ("step_type",)),
    ):
        index_rows = db.execute(f"PRAGMA index_info('{index_name}')").fetchall()
        if tuple(row[2] for row in index_rows) != columns:
            raise SessionMigrateError(f"Antigravity {table} index does not match pinned 1.1.16")


def _validate_trajectory_metadata(
    data: bytes, conversation_id: str, *, generated: bool
) -> tuple[str | None, bool]:
    fields = _decode_message(data)
    root = _optional_text(fields, 6)
    if root is not None and root != conversation_id:
        raise SessionMigrateError("Antigravity trajectory metadata root ID is inconsistent")
    incomplete = root is None
    if generated and incomplete:
        raise SessionMigrateError("generated Antigravity trajectory metadata is incomplete")
    project = _optional_text(fields, 18)
    if project is not None and project != PROJECT_ID:
        if generated:
            raise SessionMigrateError("Antigravity trajectory metadata project is unsupported")
        incomplete = True
    timestamp_value = _optional_bytes(fields, 2)
    if timestamp_value is None:
        return None, True
    timestamp_fields = _decode_message(timestamp_value)
    seconds = _optional_varint(timestamp_fields, 1)
    nanos = _optional_varint(timestamp_fields, 2) or 0
    if seconds is None or nanos >= 1_000_000_000:
        raise SessionMigrateError("Antigravity trajectory timestamp is invalid")
    try:
        value = datetime.fromtimestamp(seconds + nanos / 1_000_000_000, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise SessionMigrateError(
            "Antigravity trajectory timestamp is outside supported range"
        ) from exc
    return value.isoformat().replace("+00:00", "Z"), incomplete


def _validate_step(data: bytes, *, step_type: int, status: int, generated: bool) -> None:
    fields = _decode_message(data)
    if _required_varint(fields, 1, "step type") != step_type:
        raise SessionMigrateError("Antigravity step payload type disagrees with its SQLite row")
    if _required_varint(fields, 4, "step status") != status:
        raise SessionMigrateError("Antigravity step payload status disagrees with its SQLite row")
    if status not in {1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12}:
        raise SessionMigrateError("Antigravity step status is outside pinned 1.1.16")
    expected_payload_field = _STEP_PAYLOAD_FIELDS.get(step_type)
    if generated and step_type not in {
        STEP_TYPE_USER_INPUT,
        STEP_TYPE_PLANNER_RESPONSE,
        STEP_TYPE_GENERIC,
    }:
        raise SessionMigrateError("generated Antigravity database contains unsupported step type")
    if expected_payload_field is not None:
        nested = _required_bytes(fields, expected_payload_field, "step oneof payload")
        _validate_known_step_payload(step_type, nested, generated=generated)
    elif generated:
        raise SessionMigrateError("generated Antigravity step has no pinned payload mapping")


def _validate_known_step_payload(step_type: int, data: bytes, *, generated: bool) -> None:
    fields = _decode_message(data)
    if step_type == STEP_TYPE_USER_INPUT:
        if _optional_text(fields, 2) is None and _optional_text(fields, 1) is None:
            raise SessionMigrateError("Antigravity user step contains no readable text")
    elif step_type == STEP_TYPE_PLANNER_RESPONSE:
        response = _optional_text(fields, 1)
        calls = _bytes_values(fields, 7)
        if generated and not response and not calls and not _planner_has_private_thinking(fields):
            raise SessionMigrateError("Antigravity planner step contains no portable marker")
        for call in calls:
            _decode_tool_call(call)
    elif step_type == STEP_TYPE_GENERIC:
        for entry in _bytes_values(fields, 1):
            entry_fields = _decode_message(entry)
            _required_text(entry_fields, 1, "generic argument key")
            _required_text(entry_fields, 2, "generic argument value")
        result = _required_bytes(fields, 2, "generic tool result")
        result_fields = _decode_message(result)
        _required_text(result_fields, 1, "generic result text", allow_empty=True)
        for entry in _bytes_values(result_fields, 2):
            metadata_fields = _decode_message(entry)
            _required_text(metadata_fields, 1, "generic result metadata key")
            _required_text(metadata_fields, 2, "generic result metadata value", allow_empty=True)
    elif step_type == STEP_TYPE_MCP_TOOL:
        call = _optional_bytes(fields, 2)
        if call is not None:
            _decode_tool_call(call)
    # Other known native shapes are projected conservatively below.  Decoding
    # the enclosing message here still enforces bounded, valid protobuf wire.


def _project_events(rows: Sequence[_StepRow]) -> tuple[Event, ...]:
    events: list[Event] = []
    pending: deque[tuple[str, str]] = deque()
    emitted_calls: Counter[str] = Counter()
    for row in rows:
        outer = _decode_message(row.payload)
        payload_number = _STEP_PAYLOAD_FIELDS.get(row.step_type)
        payload = _optional_bytes(outer, payload_number) if payload_number else None
        provenance = Provenance(row.index, f"antigravity_step_{row.step_type}")
        timestamp = _step_timestamp(row.metadata)
        if row.step_type == STEP_TYPE_USER_INPUT and payload is not None:
            fields = _decode_message(payload)
            text = _optional_text(fields, 2) or _optional_text(fields, 1)
            if text:
                events.append(
                    Event(
                        kind=EventKind.MESSAGE,
                        role=Role.USER,
                        text=text,
                        timestamp=timestamp,
                        provenance=provenance,
                    )
                )
            if _bytes_values(fields, 3) or _bytes_values(fields, 9):
                events.append(_opaque_event(row, "antigravity_user_context", timestamp))
            continue
        if row.step_type == STEP_TYPE_PLANNER_RESPONSE and payload is not None:
            fields = _decode_message(payload)
            has_thinking = _planner_has_private_thinking(fields)
            if has_thinking:
                events.append(
                    Event(
                        kind=EventKind.THINKING,
                        role=Role.ASSISTANT,
                        timestamp=timestamp,
                        payload={"source_block_type": "antigravity_private_thinking"},
                        provenance=provenance,
                    )
                )
            response = _optional_text(fields, 1)
            if response:
                events.append(
                    Event(
                        kind=EventKind.MESSAGE,
                        role=Role.ASSISTANT,
                        text=response,
                        timestamp=timestamp,
                        provenance=provenance,
                    )
                )
            call_values = _bytes_values(fields, 7)
            for block_index, call_data in enumerate(call_values):
                call_id, name, arguments = _decode_tool_call(call_data)
                emitted_calls[call_id] += 1
                pending.append((call_id, name))
                events.append(
                    Event(
                        kind=EventKind.TOOL_CALL,
                        role=Role.ASSISTANT,
                        tool_name=name,
                        tool_call_id=call_id,
                        timestamp=timestamp,
                        payload={"input": arguments},
                        provenance=Provenance(
                            row.index,
                            f"antigravity_step_{row.step_type}",
                            call_id,
                            block_index,
                        ),
                    )
                )
            if not response and not call_values and not has_thinking:
                events.append(_opaque_event(row, "antigravity_empty_planner_response", timestamp))
            continue
        if row.step_type == STEP_TYPE_GENERIC and payload is not None:
            fields = _decode_message(payload)
            result_fields = _decode_message(_required_bytes(fields, 2, "generic result"))
            result_text = _required_text(result_fields, 1, "generic result text", allow_empty=True)
            linked_call_id = _generic_result_call_id(result_fields)
            pending_index = next(
                (
                    index
                    for index, value in enumerate(pending)
                    if linked_call_id is not None and value[0] == linked_call_id
                ),
                None,
            )
            if pending_index is not None:
                call_id, tool_name = pending[pending_index]
                del pending[pending_index]
            else:
                call_id, tool_name = pending.popleft() if pending else (linked_call_id, "generic")
            events.append(
                Event(
                    kind=EventKind.TOOL_RESULT,
                    role=Role.TOOL,
                    text=result_text,
                    tool_name=tool_name,
                    tool_call_id=call_id,
                    timestamp=timestamp,
                    payload={
                        "is_error": row.status == STEP_STATUS_ERROR,
                        "content_blocks": (
                            [{"type": "text", "text": result_text}] if result_text else []
                        ),
                    },
                    provenance=provenance,
                )
            )
            continue
        if row.step_type == STEP_TYPE_MCP_TOOL and payload is not None:
            fields = _decode_message(payload)
            native_call = _optional_bytes(fields, 2)
            call_id: str | None = None
            tool_name: str | None = None
            if native_call is not None:
                call_id, tool_name, arguments = _decode_tool_call(native_call)
                if not emitted_calls[call_id]:
                    events.append(
                        Event(
                            kind=EventKind.TOOL_CALL,
                            role=Role.ASSISTANT,
                            tool_name=tool_name,
                            tool_call_id=call_id,
                            timestamp=timestamp,
                            payload={"input": arguments},
                            provenance=provenance,
                        )
                    )
                else:
                    emitted_calls[call_id] -= 1
            result = _optional_text(fields, 3) or _optional_text(fields, 8)
            if result is not None:
                events.append(
                    Event(
                        kind=EventKind.TOOL_RESULT,
                        role=Role.TOOL,
                        text=result,
                        tool_name=tool_name,
                        tool_call_id=call_id,
                        timestamp=timestamp,
                        payload={
                            "is_error": row.status == STEP_STATUS_ERROR,
                            "content_blocks": [{"type": "text", "text": result}],
                        },
                        provenance=provenance,
                    )
                )
            else:
                events.append(_opaque_event(row, "antigravity_mcp_without_text", timestamp))
            continue
        projected = _project_observed_tool(row, payload, pending, timestamp)
        if projected is not None:
            events.append(projected)
        else:
            events.append(_opaque_event(row, "antigravity_native_step", timestamp))
    return tuple(events)


def _project_observed_tool(
    row: _StepRow,
    payload: bytes | None,
    pending: deque[tuple[str, str]],
    timestamp: str | None,
) -> Event | None:
    if payload is None:
        return None
    fields = _decode_message(payload)
    input_text: str | None = None
    result_text: str | None = None
    tool_name: str | None = None
    if row.step_type == STEP_TYPE_RUN_COMMAND:
        tool_name = "run_command"
        input_text = _optional_text(fields, 23) or _optional_text(fields, 25)
        output = _optional_bytes(fields, 21) or _optional_bytes(fields, 26)
        result_text = _optional_text(_decode_message(output), 1) if output else None
        result_text = result_text or _optional_text(fields, 4) or _optional_text(fields, 5)
    elif row.step_type == STEP_TYPE_VIEW_FILE:
        tool_name = "view_file"
        input_text = _optional_text(fields, 1)
        result_text = _optional_text(fields, 4) or _optional_text(fields, 9)
    elif row.step_type == STEP_TYPE_SHELL_EXEC:
        tool_name = "shell_exec"
        input_text = _optional_text(fields, 1)
        result_text = _optional_text(fields, 3) or _optional_text(fields, 4)
    else:
        return None
    call_id, queued_name = pending.popleft() if pending else (None, tool_name)
    return Event(
        kind=EventKind.TOOL_RESULT,
        role=Role.TOOL,
        text=result_text,
        tool_name=queued_name or tool_name,
        tool_call_id=call_id,
        timestamp=timestamp,
        payload={
            "is_error": row.status == STEP_STATUS_ERROR,
            "content_blocks": ([{"type": "text", "text": result_text}] if result_text else []),
            **({"source_input": input_text} if input_text else {}),
        },
        provenance=Provenance(row.index, f"antigravity_step_{row.step_type}"),
    )


def _opaque_event(row: _StepRow, reason: str, timestamp: str | None) -> Event:
    return Event(
        kind=EventKind.OPAQUE,
        timestamp=timestamp,
        payload={"reason": reason, "source_step_type": row.step_type},
        provenance=Provenance(row.index, f"antigravity_step_{row.step_type}"),
    )


def _decode_tool_call(data: bytes) -> tuple[str, str, Any]:
    fields = _decode_message(data)
    call_id = _required_text(fields, 1, "tool call ID")
    name = _required_text(fields, 2, "tool call name")
    arguments_json = _required_text(fields, 3, "tool arguments JSON", allow_empty=True)
    try:
        arguments = json.loads(arguments_json or "{}", parse_constant=_reject_json_constant)
        _ensure_json_bounds(arguments)
    except (json.JSONDecodeError, ValueError, SessionMigrateError) as exc:
        raise SessionMigrateError("Antigravity tool arguments are not bounded JSON") from exc
    return call_id, name, arguments


def _generic_result_call_id(fields: Sequence[_WireField]) -> str | None:
    result: str | None = None
    for entry in _bytes_values(fields, 2):
        metadata = _decode_message(entry)
        if _optional_text(metadata, 1) != "session_migrate_call_id":
            continue
        value = _optional_text(metadata, 2)
        if value is None or not value or result is not None:
            raise SessionMigrateError("Antigravity generic result linkage is ambiguous")
        result = value
    return result


def _step_timestamp(metadata: bytes | None) -> str | None:
    if metadata is None:
        return None
    fields = _decode_message(metadata)
    timestamp = _optional_bytes(fields, 1)
    if timestamp is None:
        return None
    nested = _decode_message(timestamp)
    seconds = _optional_varint(nested, 1)
    nanos = _optional_varint(nested, 2) or 0
    if seconds is None or nanos >= 1_000_000_000:
        return None
    try:
        return (
            datetime.fromtimestamp(seconds + nanos / 1_000_000_000, tz=UTC)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except (OverflowError, OSError, ValueError):
        return None


def _planner_has_private_thinking(fields: Sequence[_WireField]) -> bool:
    return any(_values(fields, number, wire_type=2) for number in (3, 4, 14, 16)) or (
        _optional_varint(fields, 5) == 1
    )


def _decode_message(data: bytes) -> tuple[_WireField, ...]:
    if len(data) > MAX_PROTO_VALUE_BYTES:
        raise SessionMigrateError("Antigravity protobuf message exceeds the safety limit")
    fields: list[_WireField] = []
    offset = 0
    while offset < len(data):
        if len(fields) >= MAX_PROTO_FIELDS:
            raise SessionMigrateError("Antigravity protobuf field count exceeds the safety limit")
        key, offset = _decode_varint(data, offset)
        number = key >> 3
        wire_type = key & 7
        if number <= 0 or number > 536_870_911:
            raise SessionMigrateError("Antigravity protobuf contains an invalid field number")
        if wire_type == 0:
            value, offset = _decode_varint(data, offset)
        elif wire_type == 1:
            end = offset + 8
            if end > len(data):
                raise SessionMigrateError("Antigravity protobuf fixed64 value is truncated")
            value = data[offset:end]
            offset = end
        elif wire_type == 2:
            length, offset = _decode_varint(data, offset)
            if length > MAX_PROTO_VALUE_BYTES:
                raise SessionMigrateError("Antigravity protobuf value exceeds the safety limit")
            end = offset + length
            if end > len(data):
                raise SessionMigrateError("Antigravity protobuf byte value is truncated")
            value = data[offset:end]
            offset = end
        elif wire_type == 5:
            end = offset + 4
            if end > len(data):
                raise SessionMigrateError("Antigravity protobuf fixed32 value is truncated")
            value = data[offset:end]
            offset = end
        else:
            raise SessionMigrateError("Antigravity protobuf uses unsupported group wire encoding")
        fields.append(_WireField(number, wire_type, value))
    return tuple(fields)


def _decode_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    start = offset
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise SessionMigrateError("Antigravity protobuf varint is truncated")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            if data[start:offset] != _varint(value):
                raise SessionMigrateError("Antigravity protobuf varint is not canonical")
            return value, offset
    raise SessionMigrateError("Antigravity protobuf varint exceeds 64 bits")


def _varint(value: int) -> bytes:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise SessionMigrateError("cannot encode invalid Antigravity protobuf integer")
    result = bytearray()
    while value >= 0x80:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value)
    return bytes(result)


def _field_varint(number: int, value: int) -> bytes:
    return _varint(number << 3) + _varint(value)


def _field_bytes(number: int, value: bytes) -> bytes:
    if len(value) > MAX_PROTO_VALUE_BYTES:
        raise SessionMigrateError("cannot encode oversized Antigravity protobuf value")
    return _varint((number << 3) | 2) + _varint(len(value)) + value


def _field_text(number: int, value: str) -> bytes:
    encoded = value.encode("utf-8")
    if len(encoded) > MAX_TEXT_BYTES:
        raise SessionMigrateError("cannot encode oversized Antigravity text value")
    return _field_bytes(number, encoded)


def _values(
    fields: Sequence[_WireField], number: int | None, *, wire_type: int
) -> tuple[int | bytes, ...]:
    if number is None:
        return ()
    return tuple(
        field.value for field in fields if field.number == number and field.wire_type == wire_type
    )


def _bytes_values(fields: Sequence[_WireField], number: int) -> tuple[bytes, ...]:
    values = _values(fields, number, wire_type=2)
    return tuple(value for value in values if isinstance(value, bytes))


def _optional_bytes(fields: Sequence[_WireField], number: int | None) -> bytes | None:
    values = _values(fields, number, wire_type=2)
    if len(values) > 1:
        raise SessionMigrateError("Antigravity protobuf singular byte field is repeated")
    if not values:
        return None
    value = values[0]
    if not isinstance(value, bytes):
        raise SessionMigrateError("Antigravity protobuf byte field has the wrong wire type")
    return value


def _required_bytes(fields: Sequence[_WireField], number: int, description: str) -> bytes:
    value = _optional_bytes(fields, number)
    if value is None:
        raise SessionMigrateError(f"Antigravity {description} is missing")
    return value


def _optional_varint(fields: Sequence[_WireField], number: int) -> int | None:
    values = _values(fields, number, wire_type=0)
    if len(values) > 1:
        raise SessionMigrateError("Antigravity protobuf singular integer field is repeated")
    if not values:
        return None
    value = values[0]
    if not isinstance(value, int):
        raise SessionMigrateError("Antigravity protobuf integer field has wrong wire type")
    return value


def _required_varint(fields: Sequence[_WireField], number: int, description: str) -> int:
    value = _optional_varint(fields, number)
    if value is None:
        raise SessionMigrateError(f"Antigravity {description} is missing")
    return value


def _optional_text(fields: Sequence[_WireField], number: int) -> str | None:
    value = _optional_bytes(fields, number)
    if value is None:
        return None
    try:
        result = value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SessionMigrateError("Antigravity protobuf text is not UTF-8") from exc
    if len(value) > MAX_TEXT_BYTES:
        raise SessionMigrateError("Antigravity protobuf text exceeds the safety limit")
    return result


def _required_text(
    fields: Sequence[_WireField], number: int, description: str, *, allow_empty: bool = False
) -> str:
    value = _optional_text(fields, number)
    if value is None or not allow_empty and not value:
        raise SessionMigrateError(f"Antigravity {description} is missing")
    return value


def _generic_argument_entries(value: Any, dropped: Counter[str]) -> list[bytes]:
    if not isinstance(value, dict):
        dropped["tool_call:non_object_input"] += 1
        return []
    result: list[bytes] = []
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            dropped["tool_call:invalid_argument_name"] += 1
            continue
        try:
            rendered = json.dumps(item, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError):
            dropped["tool_call:nonportable_argument"] += 1
            continue
        entry = _field_text(1, key) + _field_text(2, rendered)
        result.append(_field_bytes(1, entry))
    return result


def _event_timestamp(event: Event, fallback: str, dropped: Counter[str]) -> str:
    if event.timestamp:
        valid = valid_rfc3339(event.timestamp)
        if valid:
            return valid
        dropped["timestamp:invalid"] += 1
    return fallback


def _omission_key(event: Event) -> str:
    if event.kind == EventKind.OPAQUE:
        reason = string(event.payload.get("reason"))
        return f"opaque:{reason}" if reason else "opaque"
    if event.kind == EventKind.CONTEXT:
        block_type = string(event.payload.get("block_type"))
        return f"context:{block_type}" if block_type else "context"
    return event.kind.value


def _ensure_json_bounds(value: Any) -> None:
    pending = [value]
    nodes = 0
    while pending:
        current = pending.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise SessionMigrateError("tool arguments exceed the JSON node safety limit")
        if isinstance(current, dict):
            if not all(isinstance(key, str) for key in current):
                raise SessionMigrateError("tool argument object keys must be strings")
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
        elif current is None or isinstance(current, (str, bool, int)):
            continue
        elif isinstance(current, float):
            if current != current or current in {float("inf"), float("-inf")}:
                raise SessionMigrateError("tool arguments contain a non-finite number")
        else:
            raise SessionMigrateError("tool arguments contain a non-JSON value")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _require_uuid4(value: str, description: str) -> None:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as exc:
        raise SessionMigrateError(f"{description} must be a canonical UUIDv4") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise SessionMigrateError(f"{description} must be a canonical UUIDv4")


def _session_id_from_path(path: Path) -> str | None:
    if path.suffix != ".db":
        return None
    try:
        _require_uuid4(path.stem, "Antigravity conversation filename")
    except SessionMigrateError:
        return None
    return path.stem


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SessionMigrateError("Antigravity timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise SessionMigrateError("Antigravity timestamp must include a timezone")
    return parsed


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _summary_values(
    *,
    session_id: str,
    title: str,
    preview: str,
    step_count: int,
    timestamp: str,
    cwd: Path,
    last_user_input_index: int,
) -> tuple[Any, ...]:
    bounded_title = title[:4_096]
    bounded_preview = preview[:16_384]
    workspace_uri = cwd.as_uri()
    return (
        session_id,
        bounded_title,
        bounded_preview,
        step_count,
        _parse_timestamp(timestamp).isoformat(sep=" "),
        json.dumps([workspace_uri], ensure_ascii=False, separators=(",", ":")),
        "",
        "antigravity-cli",
        PROJECT_ID,
        "",
        "",
        0,
        "",
        "",
        0,
        0,
        _parse_timestamp(timestamp).isoformat(sep=" "),
        last_user_input_index,
        "antigravity-cli",
    )


def _read_summary_metadata(path: Path, session_id: str) -> tuple[str | None, Path | None]:
    if path.parent.name != "conversations":
        return None, None
    summaries_path = path.parent.parent / "conversation_summaries.db"
    try:
        info = summaries_path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return None, None
        if info.st_size > MAX_NATIVE_BYTES:
            return None, None
        uri = f"file:{quote(str(summaries_path.resolve()), safe='/')}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=5) as db:
            db.execute("PRAGMA trusted_schema=OFF")
            _validate_summary_schema(db)
            row = db.execute(
                "SELECT title,workspace_uris FROM conversation_summaries WHERE conversation_id=?",
                (session_id,),
            ).fetchone()
    except (OSError, sqlite3.Error, SessionMigrateError):
        return None, None
    if row is None:
        return None, None
    title = row[0] if isinstance(row[0], str) and row[0] else None
    cwd: Path | None = None
    try:
        uris = json.loads(row[1], parse_constant=_reject_json_constant)
        if isinstance(uris, list) and uris and isinstance(uris[0], str):
            parsed = urlparse(uris[0])
            if parsed.scheme == "file" and not parsed.netloc:
                cwd = Path(unquote(parsed.path))
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    return title, cwd


def _last_user_input_index(data: bytes) -> int:
    with _database_from_bytes(data) as db:
        row = db.execute(
            "SELECT max(idx) FROM steps WHERE step_type=?", (STEP_TYPE_USER_INPUT,)
        ).fetchone()
    if row is None or not isinstance(row[0], int):
        raise SessionMigrateError("generated Antigravity database contains no user input")
    return row[0]


def _ensure_summary_database(path: Path) -> None:
    if os.path.lexists(path):
        try:
            mode = path.lstat().st_mode
        except OSError as exc:
            raise SessionMigrateError("cannot inspect Antigravity summary database") from exc
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise SessionMigrateError("Antigravity summary database must be a regular file")
        try:
            os.chmod(path, 0o600)
        except OSError as exc:
            raise SessionMigrateError("cannot make Antigravity summary database private") from exc
        with sqlite3.connect(path, timeout=5) as db:
            db.execute("PRAGMA trusted_schema=OFF")
            _validate_summary_schema(db)
        return

    with tempfile.TemporaryDirectory(
        prefix=".session-migrate-summary-", dir=path.parent
    ) as directory:
        staged = Path(directory) / "conversation_summaries.db"
        try:
            with sqlite3.connect(staged) as db:
                db.executescript(
                    """
                    CREATE TABLE conversation_summaries (
                      conversation_id text,
                      title text NOT NULL DEFAULT "",
                      preview text NOT NULL DEFAULT "",
                      step_count integer NOT NULL DEFAULT 0,
                      last_modified_time datetime NOT NULL,
                      workspace_uris text NOT NULL,
                      status text NOT NULL DEFAULT "",
                      source text NOT NULL DEFAULT "",
                      project_id text NOT NULL DEFAULT "",
                      agent_name text NOT NULL DEFAULT "",
                      parent_conversation_id text NOT NULL DEFAULT "",
                      nesting_depth integer NOT NULL DEFAULT 0,
                      battle_id text NOT NULL DEFAULT "",
                      winning_conversation_id text NOT NULL DEFAULT "",
                      not_fully_idle numeric NOT NULL DEFAULT false,
                      killed numeric NOT NULL DEFAULT false,
                      last_user_input_time datetime NOT NULL,
                      last_user_input_step_index integer NOT NULL DEFAULT -1,
                      app_data_dir text NOT NULL DEFAULT "",
                      PRIMARY KEY (conversation_id)
                    );
                    CREATE INDEX idx_conversation_summaries_last_user_input_time
                      ON conversation_summaries(last_user_input_time);
                    CREATE INDEX idx_conversation_summaries_last_modified_time
                      ON conversation_summaries(last_modified_time);
                    """
                )
                if db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                    raise SessionMigrateError("generated Antigravity summary DB failed integrity")
            data = staged.read_bytes()
        except sqlite3.Error as exc:
            raise SessionMigrateError("cannot create Antigravity summary database") from exc
    try:
        write_private_atomic(path, data)
    except Exception:
        # Another installer may have won the create race.  Accept only a valid
        # pinned summary database; all other failures remain fatal.
        if not os.path.lexists(path):
            raise
    with sqlite3.connect(path, timeout=5) as db:
        db.execute("PRAGMA trusted_schema=OFF")
        _validate_summary_schema(db)


def _validate_summary_schema(db: sqlite3.Connection) -> None:
    try:
        result = db.execute("PRAGMA integrity_check(1)").fetchone()
        if result != ("ok",):
            raise SessionMigrateError("Antigravity summary database failed integrity check")
        object_row = db.execute(
            "SELECT type FROM sqlite_master WHERE name='conversation_summaries'"
        ).fetchone()
        if object_row != ("table",):
            raise SessionMigrateError("Antigravity summary table is missing")
        columns = tuple(row[1] for row in db.execute("PRAGMA table_info('conversation_summaries')"))
        if columns != _SUMMARY_COLUMNS:
            raise SessionMigrateError("Antigravity summary columns do not match pinned 1.1.16")
        for index_name, expected_column in (
            ("idx_conversation_summaries_last_user_input_time", "last_user_input_time"),
            ("idx_conversation_summaries_last_modified_time", "last_modified_time"),
        ):
            rows = db.execute(f"PRAGMA index_info('{index_name}')").fetchall()
            if tuple(row[2] for row in rows) != (expected_column,):
                raise SessionMigrateError("Antigravity summary index does not match pinned 1.1.16")
    except SessionMigrateError:
        raise
    except sqlite3.Error as exc:
        raise SessionMigrateError("Antigravity summary database schema is invalid") from exc


def _absolute_no_follow(path: Path) -> Path:
    result = Path(os.path.abspath(path.expanduser()))
    current = Path(result.anchor)
    for component in result.parts[1:]:
        current /= component
        if os.path.lexists(current) and stat.S_ISLNK(current.lstat().st_mode):
            raise SessionMigrateError("refusing Antigravity install through a symbolic link")
    return result


def _ensure_private_directory(path: Path) -> None:
    path = _absolute_no_follow(path)
    missing: list[Path] = []
    current = path
    while not os.path.lexists(current):
        missing.append(current)
        current = current.parent
    if stat.S_ISLNK(current.lstat().st_mode) or not stat.S_ISDIR(current.lstat().st_mode):
        raise SessionMigrateError("Antigravity target parent is not a safe directory")
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError as exc:
            if stat.S_ISLNK(directory.lstat().st_mode) or not stat.S_ISDIR(
                directory.lstat().st_mode
            ):
                raise SessionMigrateError("Antigravity target directory creation raced") from exc
        os.chmod(directory, 0o700)
    if path.is_dir():
        os.chmod(path, 0o700)


def _unlink_if_same_file(path: Path, identity: tuple[int, int]) -> None:
    try:
        info = path.lstat()
        if (info.st_dev, info.st_ino) == identity and stat.S_ISREG(info.st_mode):
            path.unlink()
    except OSError:
        pass


def _open_identity_guard(
    path: Path,
    *,
    writable: bool = False,
    expected_identity: tuple[int, int] | None = None,
) -> int:
    flags = os.O_RDWR if writable else os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SessionMigrateError("cannot guard Antigravity install path") from exc
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        os.close(descriptor)
        raise SessionMigrateError("Antigravity install guard is not a regular file")
    if expected_identity is not None and (info.st_dev, info.st_ino) != expected_identity:
        os.close(descriptor)
        raise SessionMigrateError("Antigravity install path changed before it could be guarded")
    return descriptor


def _guard_matches_path(descriptor: int, path: Path) -> bool:
    try:
        guarded = os.fstat(descriptor)
        current = path.lstat()
    except OSError:
        return False
    return (guarded.st_dev, guarded.st_ino) == (current.st_dev, current.st_ino)


def _stream_sha256(path: Path, *, maximum: int) -> str:
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                total += len(chunk)
                if total > maximum:
                    raise SessionMigrateError("Antigravity executable exceeds pinned size")
                digest.update(chunk)
    except OSError as exc:
        raise SessionMigrateError("cannot hash the Antigravity CLI executable") from exc
    return digest.hexdigest()
