"""MastraCode 0.37.1 LibSQL thread/message adapter.

MastraCode keeps every local conversation in one ``mastra.db``.  A portable
artifact produced here is a small, valid SQLite database containing exactly one
``mastra_threads`` row and its ``mastra_messages`` rows.  Installation merges
those rows into the native database in one transaction; it never replaces the
shared store.

The adapter targets the schemas emitted by ``mastracode@0.37.1``.  Thread
metadata is SQLite JSONB (not UTF-8 JSON).  A deliberately small JSONB codec is
included so the target project path survives without requiring SQLite >= 3.45
or a third-party Python package.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import subprocess
import tempfile
import uuid
from collections import Counter, deque
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from session_migrate.errors import JsonlError, SessionMigrateError
from session_migrate.formats.common import content_text, portable_data_image, string, valid_rfc3339
from session_migrate.jsonl import DEFAULT_MAX_RECORDS, write_private_atomic
from session_migrate.model import AgentFormat, Event, EventKind, Provenance, Role, Session

PINNED_MASTRACODE_VERSION = "0.37.1"
PINNED_MASTRACODE_SOURCE_COMMIT = "003e75745c5fd6a7af8464ece1d2930f81dd15af"
PINNED_MASTRACODE_NPM_TARBALL_BYTES = 1_557_550
PINNED_MASTRACODE_NPM_TARBALL_SHA256 = (
    "1ebdf39c630469d5a7c635c60cff339679d8a1a1c3a5647375133cfb5da0c0e9"
)
PINNED_MASTRACODE_CLI_JS_BYTES = 10_526
PINNED_MASTRACODE_CLI_JS_SHA256 = "9921609cd35cb9dc91c8a2ae5d606d937d904404f084b89d7b9739cca260f35b"

MAX_NATIVE_BYTES = 256 * 1024 * 1024
MAX_MESSAGE_BYTES = 64 * 1024 * 1024
MAX_INLINE_IMAGE_BYTES = 32 * 1024 * 1024
MAX_MESSAGES = DEFAULT_MAX_RECORDS
MAX_JSON_DEPTH = 96
MAX_JSON_NODES = 1_000_000
PORTABLE_TOOL_RESULT_SCHEMA = "session-migrate.mastracode-tool-result.v1"
MAX_JSONB_BYTES = 8 * 1024 * 1024
SQLITE_HEADER = b"SQLite format 3\x00"

THREAD_COLUMNS = (
    "id",
    "resourceId",
    "title",
    "metadata",
    "createdAt",
    "updatedAt",
)
MESSAGE_COLUMNS = (
    "id",
    "thread_id",
    "content",
    "role",
    "type",
    "createdAt",
    "resourceId",
)


@dataclass(frozen=True, slots=True)
class MastraCodeSessionInfo:
    """Content-free native session inventory row."""

    session_id: str
    title: str | None
    cwd: Path | None
    started_at: str | None
    cli_version: str | None
    updated_ns: int | None
    records: int | None


@dataclass(frozen=True, slots=True)
class DatabaseSnapshot:
    """Content-free identity of a database and its live WAL sidecars."""

    device: int
    inode: int
    size: int
    modified_ns: int
    fingerprint: str


@dataclass(frozen=True, slots=True)
class ParsedMastraCodeArtifact:
    session_id: str
    resource_id: str
    title: str | None
    cwd: Path | None
    cli_version: str
    messages: int


def normalized_session_id(value: str) -> str:
    """Return MastraCode's canonical UUID thread ID or fail closed."""

    try:
        parsed = uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise SessionMigrateError("MastraCode session ID must be a UUID") from exc
    if parsed.version not in {1, 3, 4, 5, 6, 7, 8}:
        raise SessionMigrateError("MastraCode session ID has an unsupported UUID version")
    return str(parsed)


def database_candidates(
    home: Path,
    *,
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[Path, ...]:
    """Return the single active local database path using native precedence."""

    env = os.environ if environ is None else environ
    if value := env.get("MASTRA_DB_PATH"):
        return (Path(value).expanduser(),)
    if value := env.get("MASTRA_APP_DATA_DIR"):
        return (Path(value).expanduser() / "mastra.db",)
    system = platform or os.sys.platform
    if system == "darwin":
        root = home / "Library" / "Application Support" / "mastracode"
    elif system.startswith("win"):
        root = Path(env.get("APPDATA", str(home / "AppData" / "Roaming"))) / "mastracode"
    else:
        root = Path(env.get("XDG_DATA_HOME", str(home / ".local" / "share"))) / "mastracode"
    return (root / "mastra.db",)


def database_path(home: Path, **kwargs: Any) -> Path:
    return database_candidates(home, **kwargs)[0]


def resource_id_for_cwd(cwd: Path) -> str:
    """Reproduce MastraCode's project resource ID calculation."""

    root = cwd.resolve()

    def git(*args: str) -> str | None:
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        value = completed.stdout.strip()
        return value if completed.returncode == 0 and value else None

    git_dir = git("rev-parse", "--git-dir")
    git_url: str | None = None
    main_repo: Path | None = None
    if git_dir:
        if project_root := git("rev-parse", "--show-toplevel"):
            root = Path(project_root).resolve()
        common = git("rev-parse", "--git-common-dir")
        if common and common != ".git" and common != git_dir:
            main_repo = (root / common).resolve().parent
        git_url = git("remote", "get-url", "origin")
        if not git_url:
            remotes = git("remote")
            if remotes:
                git_url = git("remote", "get-url", remotes.splitlines()[0])

    if git_url:
        source = _normalize_git_url(git_url)
        base = git_url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git") or "project"
    elif main_repo is not None:
        source = str(main_repo)
        base = root.name
    else:
        source = str(root)
        base = root.name
    slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
    return f"{slug or 'project'}-{hashlib.sha256(source.encode()).hexdigest()[:12]}"


def serialize(
    session: Session,
    *,
    session_id: str,
    cwd: Path,
    cli_version: str = PINNED_MASTRACODE_VERSION,
    model: str | None = None,
    timestamp: str | None = None,
    title: str | None = None,
    resource_id: str | None = None,
) -> tuple[bytes, dict[str, int]]:
    """Serialize portable history as a one-thread native LibSQL artifact."""

    canonical_id = normalized_session_id(session_id)
    if cli_version != PINNED_MASTRACODE_VERSION:
        raise SessionMigrateError(
            f"MastraCode target schema is pinned to {PINNED_MASTRACODE_VERSION}"
        )
    destination = cwd.resolve()
    native_resource = resource_id or resource_id_for_cwd(destination)
    _validate_text(native_resource, "MastraCode resource ID", maximum=1024)
    started = valid_rfc3339(timestamp) or valid_rfc3339(session.started_at) or _utc_now()
    clock = datetime.fromisoformat(started.replace("Z", "+00:00")).astimezone(UTC)
    dropped: Counter[str] = Counter()
    messages: list[tuple[str, str, str, str, str, str, str]] = []
    results: dict[str, deque[Event]] = {}
    for event in session.events:
        if event.kind == EventKind.TOOL_RESULT and event.tool_call_id:
            results.setdefault(event.tool_call_id, deque()).append(event)
    consumed_results: set[int] = set()
    seen_calls: set[str] = set()

    def append(role: str, native_type: str, parts: list[dict[str, Any]], event: Event) -> None:
        nonlocal clock
        requested = valid_rfc3339(event.timestamp)
        if requested:
            candidate = datetime.fromisoformat(requested.replace("Z", "+00:00")).astimezone(UTC)
            if candidate > clock:
                clock = candidate
        created = clock.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        clock += timedelta(milliseconds=1)
        content: dict[str, Any] = {"format": 2, "parts": parts}
        if event.kind == EventKind.COMPACTION:
            content["metadata"] = {"sessionMigrate": {"kind": "compaction"}}
        encoded = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode()) > MAX_MESSAGE_BYTES:
            raise SessionMigrateError("generated MastraCode message exceeds the safety limit")
        messages.append(
            (
                str(uuid.uuid4()),
                canonical_id,
                encoded,
                role,
                native_type,
                created,
                native_resource,
            )
        )

    for event in session.events:
        if event.kind == EventKind.MESSAGE and event.role in {
            Role.USER,
            Role.ASSISTANT,
            Role.SYSTEM,
        }:
            if not event.text:
                continue
            append(event.role.value, "v2", [{"type": "text", "text": event.text}], event)
            if event.payload.get("ui_only_projection") is True:
                dropped["message:ui_only_projection"] += 1
            continue

        if event.kind == EventKind.TOOL_CALL:
            call_id = event.tool_call_id or f"call_session_migrate_{uuid.uuid4().hex}"
            name = event.tool_name or "unknown_tool"
            if not event.tool_call_id:
                dropped["tool_call:missing_id"] += 1
            if not event.tool_name:
                dropped["tool_call:missing_name"] += 1
            if call_id in seen_calls:
                dropped["tool_call:duplicate_id"] += 1
                call_id = f"{call_id}__session_migrate_{uuid.uuid4().hex}"
            seen_calls.add(call_id)
            arguments = event.payload.get("input", {})
            if not isinstance(arguments, dict):
                arguments = {"input": arguments}
                dropped["tool_call:non_object_input"] += 1
            if event.payload.get("namespace"):
                dropped["tool_call:namespace"] += 1
            invocation: dict[str, Any] = {
                "state": "call",
                "toolCallId": call_id,
                "toolName": name,
                "args": arguments,
            }
            if event.tool_call_id and results.get(event.tool_call_id):
                result = results[event.tool_call_id].popleft()
                consumed_results.add(id(result))
                invocation.update(
                    {
                        "state": "result",
                        "result": _tool_result_value(result, dropped),
                        "isError": result.payload.get("is_error") is True,
                    }
                )
                if result.tool_name and result.tool_name != name:
                    dropped["tool_result:name_mismatch"] += 1
            append(
                "assistant",
                "v2",
                [{"type": "tool-invocation", "toolInvocation": invocation}],
                event,
            )
            continue

        if event.kind == EventKind.TOOL_RESULT:
            if id(event) in consumed_results:
                continue
            dropped["tool_result:orphan_id"] += 1
            if not event.tool_call_id:
                dropped["tool_result:missing_id"] += 1
            continue

        if event.kind == EventKind.THINKING:
            if (
                event.text
                and session.source_format == AgentFormat.MASTRACODE
                and event.payload.get("source_readable_reasoning") is True
            ):
                append(
                    "assistant",
                    "v2",
                    [{"type": "reasoning", "reasoning": event.text}],
                    event,
                )
            else:
                dropped["thinking:private"] += 1
            if event.payload.get("encrypted_content") or event.payload.get("signature"):
                dropped["thinking:provider_payload"] += 1
            continue

        if (
            event.kind == EventKind.CONTEXT
            and event.role == Role.USER
            and event.payload.get("block_type") == "image"
        ):
            image = portable_data_image(event.payload.get("image_url"))
            if image is None:
                dropped["context:image"] += 1
                continue
            media_type, encoded = image
            append(
                "user",
                "v2",
                [
                    {
                        "type": "file",
                        "mimeType": media_type,
                        "data": f"data:{media_type};base64,{encoded}",
                    }
                ],
                event,
            )
            continue

        if event.kind == EventKind.COMPACTION and event.text:
            # Historical system-role rows are excluded when MastraCode builds
            # the next provider request.  Store a tagged user row so the
            # portable summary actually participates in native resume; parsing
            # uses the tag to restore COMPACTION rather than MESSAGE.
            append("user", "v2", [{"type": "text", "text": event.text}], event)
            if event.payload.get("has_boundary_metadata") is True:
                dropped["compaction:boundary_metadata"] += 1
            if event.payload.get("replacement_history_expanded") is True:
                dropped["compaction:replacement_history_expanded"] += 1
            continue

        dropped[_omission_key(event)] += 1

    if not messages:
        raise SessionMigrateError("conversion produced no resumable conversation history")
    if len(messages) > MAX_MESSAGES:
        raise SessionMigrateError("generated MastraCode history contains too many messages")

    metadata_value: dict[str, Any] = {
        "projectPath": str(destination),
        "sessionMigrate": {
            "cliVersion": cli_version,
            "model": session.model,
            "sourceFormat": session.source_format.value,
        },
    }
    # MastraCode's headless runner applies ``--model`` before switching to an
    # existing thread.  The switch then restores currentModelId from metadata,
    # so a caller-supplied target model must be seeded here for deterministic
    # native resume.  When no target model is supplied, leave the native fields
    # absent and let the user's configured default apply.
    if model:
        _validate_text(model, "MastraCode model ID", maximum=1024)
        metadata_value["currentModelId"] = model
        metadata_value["modeModelId_build"] = model
    metadata = _jsonb_encode(metadata_value)
    native_title = title if title is not None else session.title
    if native_title is not None:
        _validate_text(native_title, "MastraCode title", maximum=4096)
    data = _build_database(
        thread=(
            canonical_id,
            native_resource,
            native_title or "",
            metadata,
            started,
            messages[-1][5],
        ),
        messages=messages,
    )
    validate_native_bytes(data, canonical_id)
    return data, dict(sorted(dropped.items()))


def validate_native_bytes(data: bytes, session_id: str) -> ParsedMastraCodeArtifact:
    """Strictly validate a generated one-thread SQLite artifact."""

    canonical_id = normalized_session_id(session_id)
    if not data or len(data) > MAX_NATIVE_BYTES or not data.startswith(SQLITE_HEADER):
        raise SessionMigrateError("generated MastraCode artifact is not a bounded SQLite database")
    with _database_from_bytes(data) as db:
        _validate_database_schema(db, generated=True)
        result = db.execute("PRAGMA integrity_check").fetchone()
        if result is None or _text(result[0]) != "ok":
            raise SessionMigrateError("generated MastraCode artifact failed SQLite integrity check")
        rows = db.execute(
            "SELECT id,resourceId,title,CAST(metadata AS BLOB),createdAt,updatedAt "
            'FROM "mastra_threads"'
        ).fetchall()
        if len(rows) != 1 or _text(rows[0][0]) != canonical_id:
            raise SessionMigrateError("generated MastraCode artifact session linkage is invalid")
        resource = _required_text(rows[0][1], "generated MastraCode resource ID")
        title = _optional_text(rows[0][2])
        created = _required_text(rows[0][4], "generated MastraCode creation time")
        updated = _required_text(rows[0][5], "generated MastraCode update time")
        if not valid_rfc3339(created) or not valid_rfc3339(updated):
            raise SessionMigrateError("generated MastraCode artifact has invalid timestamps")
        metadata = _decode_metadata(rows[0][3])
        cwd = _metadata_cwd(metadata)
        version = _metadata_cli_version(metadata) or PINNED_MASTRACODE_VERSION
        message_rows = _message_rows(db, canonical_id)
        if not message_rows or len(message_rows) > MAX_MESSAGES:
            raise SessionMigrateError("generated MastraCode artifact has invalid message count")
        _validate_messages(message_rows, canonical_id, resource, generated=True)
        return ParsedMastraCodeArtifact(
            session_id=canonical_id,
            resource_id=resource,
            title=title or None,
            cwd=cwd,
            cli_version=version,
            messages=len(message_rows),
        )


def native_record_count(data: bytes) -> int:
    if not data.startswith(SQLITE_HEADER) or len(data) > MAX_NATIVE_BYTES:
        return 0
    try:
        with _database_from_bytes(data) as db:
            row = db.execute('SELECT COUNT(*) FROM "mastra_messages"').fetchone()
            return int(row[0]) if row else 0
    except (OSError, sqlite3.Error, SessionMigrateError, ValueError):
        return 0


def database_snapshot(path: Path) -> DatabaseSnapshot:
    """Snapshot DB/WAL identity without reading conversation bodies."""

    database = _require_database(path)
    base = database.stat()
    digest = hashlib.sha256()
    # The WAL-index (``-shm``) is a transient lock table: merely opening a
    # read-only connection changes its mtime.  Tracking it would make every
    # consistent read falsely look like a concurrent write.  The database and
    # WAL contain the durable state we need to fingerprint.
    for candidate in (database, Path(f"{database}-wal")):
        try:
            value = candidate.lstat()
        except FileNotFoundError:
            digest.update(candidate.name.encode() + b"\0missing\0")
            continue
        if stat.S_ISLNK(value.st_mode):
            raise JsonlError("MastraCode database sidecar must not be a symbolic link")
        digest.update(candidate.name.encode())
        digest.update(
            f"\0{value.st_dev}\0{value.st_ino}\0{value.st_size}\0{value.st_mtime_ns}\0".encode()
        )
    return DatabaseSnapshot(
        device=base.st_dev,
        inode=base.st_ino,
        size=base.st_size,
        modified_ns=base.st_mtime_ns,
        fingerprint=digest.hexdigest(),
    )


def list_sessions(path: Path) -> tuple[MastraCodeSessionInfo, ...]:
    """List every thread without reading any message content."""

    before = database_snapshot(path)
    with _consistent_copy(path) as db:
        _validate_database_schema(db, generated=False)
        rows = db.execute(
            "SELECT t.id,t.title,CAST(t.metadata AS BLOB),t.createdAt,t.updatedAt,"
            'COUNT(m.id) FROM "mastra_threads" t LEFT JOIN "mastra_messages" m '
            "ON m.thread_id=t.id GROUP BY t.id ORDER BY t.updatedAt DESC,t.id"
        ).fetchall()
    after = database_snapshot(path)
    if before != after:
        raise JsonlError("MastraCode database changed while it was being inventoried; retry")
    inventory: list[MastraCodeSessionInfo] = []
    for row in rows:
        session_id = normalized_session_id(_required_text(row[0], "MastraCode thread ID"))
        metadata = _decode_metadata(row[2])
        started = _portable_timestamp(_optional_text(row[3]))
        inventory.append(
            MastraCodeSessionInfo(
                session_id=session_id,
                title=_optional_text(row[1]) or None,
                cwd=_metadata_cwd(metadata),
                started_at=started,
                cli_version=_metadata_cli_version(metadata) or PINNED_MASTRACODE_VERSION,
                updated_ns=_rfc3339_ns(_optional_text(row[4])),
                records=int(row[5]),
            )
        )
    return tuple(inventory)


def parse_session(path: Path, session_id: str | None = None) -> Session:
    """Project one thread from the central native database."""

    before = database_snapshot(path)
    with _consistent_copy(path) as db:
        _validate_database_schema(db, generated=False)
        if session_id is None:
            ids = [_text(row[0]) for row in db.execute('SELECT id FROM "mastra_threads"')]
            if len(ids) != 1:
                raise SessionMigrateError(
                    "MastraCode database contains multiple sessions; select a thread ID"
                )
            canonical_id = normalized_session_id(ids[0])
        else:
            canonical_id = normalized_session_id(session_id)
        thread = db.execute(
            "SELECT id,resourceId,title,CAST(metadata AS BLOB),createdAt,updatedAt "
            'FROM "mastra_threads" WHERE id=?',
            (canonical_id,),
        ).fetchone()
        if thread is None:
            raise SessionMigrateError(f"MastraCode session not found: {canonical_id}")
        rows = _message_rows(db, canonical_id)
        resource = _required_text(thread[1], "MastraCode resource ID")
        _validate_messages(rows, canonical_id, resource, generated=False)
        metadata = _decode_metadata(thread[3])
        events: list[Event] = []
        observation = _active_observation(db, canonical_id)
        if observation:
            events.append(
                Event(
                    kind=EventKind.COMPACTION,
                    text=observation,
                    timestamp=_portable_timestamp(_optional_text(thread[4])),
                    payload={"source": "mastracode_observational_memory"},
                    provenance=Provenance(-1, "observational_memory", canonical_id),
                )
            )
        for index, row in enumerate(rows):
            events.extend(_parse_message(row, index))
        digest = _session_digest(thread, rows, observation)
    after = database_snapshot(path)
    if before != after:
        raise JsonlError("MastraCode database changed while it was being read; retry")
    title = _optional_text(thread[2]) or _derived_title(events)
    model = _metadata_model(metadata)
    return Session(
        source_format=AgentFormat.MASTRACODE,
        source_path=path.resolve(),
        source_sha256=digest,
        session_id=canonical_id,
        cwd=_metadata_cwd(metadata),
        started_at=_portable_timestamp(_optional_text(thread[4])),
        cli_version=_metadata_cli_version(metadata) or PINNED_MASTRACODE_VERSION,
        model=model,
        title=title,
        events=tuple(events),
        raw_record_count=len(rows) + (1 if observation else 0),
        model_provider=model.split("/", 1)[0] if model and "/" in model else None,
    )


parse = parse_session


def install_native_bytes(
    data: bytes,
    target_db: Path,
    *,
    session_id: str,
    overwrite: bool = False,
    dry_run: bool = False,
) -> Path:
    """Transactionally merge one validated artifact into MastraCode's DB."""

    parsed = validate_native_bytes(data, session_id)
    target = target_db.expanduser()
    if target.exists():
        _require_database(target)
        with _database_from_bytes(data) as source:
            thread = source.execute(
                'SELECT id,resourceId,title,metadata,createdAt,updatedAt FROM "mastra_threads"'
            ).fetchone()
            messages = source.execute(
                "SELECT id,thread_id,content,role,type,createdAt,resourceId "
                'FROM "mastra_messages" ORDER BY createdAt,id'
            ).fetchall()
        with sqlite3.connect(target, timeout=15, isolation_level=None) as destination:
            destination.execute("PRAGMA trusted_schema=OFF")
            _validate_database_schema(destination, generated=False)
            exists = destination.execute(
                'SELECT 1 FROM "mastra_threads" WHERE id=?', (parsed.session_id,)
            ).fetchone()
            if exists and not overwrite:
                raise SessionMigrateError(
                    f"refusing to overwrite existing MastraCode session {parsed.session_id}"
                )
            if dry_run:
                return target
            try:
                destination.execute("BEGIN IMMEDIATE")
                if exists:
                    _delete_existing_thread(destination, parsed.session_id)
                destination.execute(
                    'INSERT INTO "mastra_threads" '
                    "(id,resourceId,title,metadata,createdAt,updatedAt) VALUES (?,?,?,?,?,?)",
                    thread,
                )
                destination.executemany(
                    'INSERT INTO "mastra_messages" '
                    "(id,thread_id,content,role,type,createdAt,resourceId) VALUES (?,?,?,?,?,?,?)",
                    messages,
                )
                destination.execute("COMMIT")
            except sqlite3.Error as exc:
                with suppress(sqlite3.Error):
                    destination.execute("ROLLBACK")
                raise SessionMigrateError("failed to install MastraCode session") from exc
        return target

    if dry_run:
        return target
    _mkdir_private(target.parent)
    write_private_atomic(target, data)
    return target


def _build_database(
    *,
    thread: tuple[str, str, str, bytes, str, str],
    messages: Sequence[tuple[str, str, str, str, str, str, str]],
) -> bytes:
    descriptor, name = tempfile.mkstemp(prefix="session-migrate-mastracode-", suffix=".db")
    os.close(descriptor)
    path = Path(name)
    try:
        with sqlite3.connect(path) as db:
            db.executescript(
                'CREATE TABLE "mastra_threads" ('
                '"id" TEXT NOT NULL PRIMARY KEY,'
                '"resourceId" TEXT NOT NULL,'
                '"title" TEXT NOT NULL,'
                '"metadata" TEXT,'
                '"createdAt" TEXT NOT NULL,'
                '"updatedAt" TEXT NOT NULL);'
                'CREATE TABLE "mastra_messages" ('
                '"id" TEXT NOT NULL PRIMARY KEY,'
                '"thread_id" TEXT NOT NULL,'
                '"content" TEXT NOT NULL,'
                '"role" TEXT NOT NULL,'
                '"type" TEXT NOT NULL,'
                '"createdAt" TEXT NOT NULL,'
                '"resourceId" TEXT);'
                'CREATE INDEX "mastra_messages_thread_created" '
                'ON "mastra_messages"("thread_id","createdAt","id");'
            )
            db.execute(
                'INSERT INTO "mastra_threads" '
                "(id,resourceId,title,metadata,createdAt,updatedAt) VALUES (?,?,?,?,?,?)",
                thread,
            )
            db.executemany(
                'INSERT INTO "mastra_messages" '
                "(id,thread_id,content,role,type,createdAt,resourceId) VALUES (?,?,?,?,?,?,?)",
                messages,
            )
            db.commit()
        data = path.read_bytes()
    finally:
        path.unlink(missing_ok=True)
    if len(data) > MAX_NATIVE_BYTES:
        raise SessionMigrateError("generated MastraCode artifact exceeds the safety limit")
    return data


def _validate_database_schema(db: sqlite3.Connection, *, generated: bool) -> None:
    try:
        tables = {
            _text(row[0])
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        required = {"mastra_threads", "mastra_messages"}
        if not required.issubset(tables) or (generated and tables != required):
            raise SessionMigrateError("MastraCode database has an unsupported table schema")
        for table, expected in (
            ("mastra_threads", THREAD_COLUMNS),
            ("mastra_messages", MESSAGE_COLUMNS),
        ):
            columns = tuple(_text(row[1]) for row in db.execute(f'PRAGMA table_info("{table}")'))
            if columns != expected:
                raise SessionMigrateError(f"MastraCode database has unsupported {table} columns")
    except sqlite3.Error as exc:
        raise SessionMigrateError("unable to inspect MastraCode database schema") from exc


def _message_rows(db: sqlite3.Connection, session_id: str) -> list[sqlite3.Row | tuple[Any, ...]]:
    try:
        return db.execute(
            "SELECT id,thread_id,content,role,type,createdAt,resourceId "
            'FROM "mastra_messages" WHERE thread_id=? ORDER BY createdAt,id',
            (session_id,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise SessionMigrateError("unable to read MastraCode messages") from exc


def _validate_messages(
    rows: Sequence[Sequence[Any]], session_id: str, resource_id: str, *, generated: bool
) -> None:
    if len(rows) > MAX_MESSAGES:
        raise SessionMigrateError("MastraCode session contains too many messages")
    ids: set[str] = set()
    calls: set[str] = set()
    for index, row in enumerate(rows):
        message_id = _required_text(row[0], f"MastraCode message {index} ID")
        if message_id in ids:
            raise SessionMigrateError("MastraCode session contains duplicate message IDs")
        ids.add(message_id)
        if _required_text(row[1], "MastraCode message thread") != session_id:
            raise SessionMigrateError("MastraCode message/thread linkage is invalid")
        native_resource = _optional_text(row[6])
        if native_resource and native_resource != resource_id:
            raise SessionMigrateError("MastraCode message/resource linkage is invalid")
        role = _required_text(row[3], "MastraCode message role")
        if role not in {"user", "assistant", "system", "signal"}:
            raise SessionMigrateError("MastraCode message has an unsupported role")
        native_type = _required_text(row[4], "MastraCode message type")
        if not native_type or len(native_type) > 128:
            raise SessionMigrateError("MastraCode message has an invalid type")
        timestamp = _required_text(row[5], "MastraCode message timestamp")
        if not valid_rfc3339(timestamp):
            raise SessionMigrateError("MastraCode message has an invalid timestamp")
        content = _decode_content(row[2], index)
        parts = content.get("parts")
        assert isinstance(parts, list)
        for part in parts:
            assert isinstance(part, dict)
            part_type = string(part.get("type"))
            if part_type == "tool-invocation":
                invocation = part.get("toolInvocation")
                if not isinstance(invocation, dict):
                    raise SessionMigrateError("MastraCode tool invocation is malformed")
                call_id = string(invocation.get("toolCallId"))
                name = string(invocation.get("toolName"))
                state = string(invocation.get("state"))
                args = invocation.get("args")
                allowed_states = (
                    {"call", "result"}
                    if generated
                    else {
                        "call",
                        "result",
                        "output-error",
                    }
                )
                if (
                    not call_id
                    or not name
                    or state not in allowed_states
                    or not isinstance(args, dict)
                ):
                    raise SessionMigrateError("MastraCode tool invocation has invalid fields")
                if generated and call_id in calls:
                    raise SessionMigrateError(
                        "generated MastraCode history has duplicate tool call IDs"
                    )
                calls.add(call_id)
                if state == "result" and "result" not in invocation:
                    raise SessionMigrateError("MastraCode completed tool invocation has no result")
                if state == "output-error":
                    _required_text(
                        invocation.get("errorText"),
                        "MastraCode failed tool invocation error",
                    )
                try:
                    _validate_json_shape(args)
                    if state == "result":
                        _validate_json_shape(invocation["result"])
                except ValueError as exc:
                    raise SessionMigrateError(
                        "MastraCode tool invocation exceeds the JSON shape limit"
                    ) from exc
            elif generated and part_type == "text":
                text = part.get("text")
                if not isinstance(text, str) or not text:
                    raise SessionMigrateError("generated MastraCode text part is invalid")
            elif generated and part_type == "reasoning":
                reasoning = part.get("reasoning")
                if not isinstance(reasoning, str) or not reasoning:
                    raise SessionMigrateError("generated MastraCode reasoning part is invalid")
            elif generated and part_type == "file":
                image = portable_data_image(part.get("data"))
                if image is None or part.get("mimeType") != image[0]:
                    raise SessionMigrateError("generated MastraCode image part is invalid")
            elif generated and part_type not in {"text", "reasoning", "file"}:
                raise SessionMigrateError(
                    "generated MastraCode message has unsupported part type: "
                    f"{part_type or 'unknown'}"
                )


def _decode_content(value: Any, index: int) -> dict[str, Any]:
    if isinstance(value, bytes):
        if len(value) > MAX_MESSAGE_BYTES:
            raise SessionMigrateError("MastraCode message exceeds the safety limit")
        try:
            raw = value.decode()
        except UnicodeDecodeError as exc:
            raise SessionMigrateError("MastraCode message is not UTF-8") from exc
    elif isinstance(value, str):
        if len(value.encode()) > MAX_MESSAGE_BYTES:
            raise SessionMigrateError("MastraCode message exceeds the safety limit")
        raw = value
    else:
        raise SessionMigrateError("MastraCode message content has an invalid SQLite type")
    try:
        content = json.loads(raw, object_pairs_hook=_unique_object)
        _validate_json_shape(content)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise SessionMigrateError(f"MastraCode message {index} has invalid JSON") from exc
    if not isinstance(content, dict) or content.get("format") != 2:
        raise SessionMigrateError("MastraCode message is not format 2")
    parts = content.get("parts")
    if (
        not isinstance(parts, list)
        or not parts
        or not all(isinstance(part, dict) for part in parts)
    ):
        raise SessionMigrateError("MastraCode message has invalid parts")
    return content


def _parse_message(row: Sequence[Any], index: int) -> list[Event]:
    message_id = _required_text(row[0], "MastraCode message ID")
    role_value = _required_text(row[3], "MastraCode message role")
    native_type = _required_text(row[4], "MastraCode message type")
    timestamp = _portable_timestamp(_optional_text(row[5]))
    content = _decode_content(row[2], index)
    metadata = content.get("metadata")
    session_migrate = metadata.get("sessionMigrate") if isinstance(metadata, dict) else None
    if message_id == "om-continuation" or (
        isinstance(session_migrate, dict) and session_migrate.get("internal") is True
    ):
        return [
            Event(
                kind=EventKind.OPAQUE,
                payload={"reason": "mastracode_internal_continuation"},
                provenance=Provenance(index, native_type, message_id),
            )
        ]
    role = {
        "user": Role.USER,
        "assistant": Role.ASSISTANT,
        "system": Role.SYSTEM,
        "signal": Role.USER,
    }[role_value]
    events: list[Event] = []
    for block_index, part in enumerate(content["parts"]):
        part_type = string(part.get("type"))
        provenance = Provenance(index, native_type, message_id, block_index)
        if part_type == "text" and isinstance(part.get("text"), str):
            kind = (
                EventKind.COMPACTION
                if isinstance(session_migrate, dict) and session_migrate.get("kind") == "compaction"
                else EventKind.MESSAGE
            )
            events.append(
                Event(
                    kind=kind,
                    role=None if kind == EventKind.COMPACTION else role,
                    text=part["text"],
                    timestamp=timestamp,
                    provenance=provenance,
                )
            )
            continue
        if part_type == "reasoning" and isinstance(part.get("reasoning"), str):
            events.append(
                Event(
                    kind=EventKind.THINKING,
                    role=Role.ASSISTANT,
                    text=part["reasoning"],
                    timestamp=timestamp,
                    payload={"source_readable_reasoning": True},
                    provenance=provenance,
                )
            )
            continue
        if part_type == "tool-invocation" and isinstance(part.get("toolInvocation"), dict):
            invocation = part["toolInvocation"]
            call_id = string(invocation.get("toolCallId"))
            name = string(invocation.get("toolName"))
            if call_id and name:
                arguments = invocation.get("args")
                events.append(
                    Event(
                        kind=EventKind.TOOL_CALL,
                        role=Role.ASSISTANT,
                        tool_name=name,
                        tool_call_id=call_id,
                        timestamp=timestamp,
                        payload={"input": arguments if isinstance(arguments, dict) else {}},
                        provenance=provenance,
                    )
                )
                state = invocation.get("state")
                if state == "result" and "result" in invocation:
                    result = invocation.get("result")
                    portable_result = (
                        result
                        if isinstance(result, dict)
                        and result.get("schema") == PORTABLE_TOOL_RESULT_SCHEMA
                        and set(result) == {"schema", "text", "content"}
                        and isinstance(result.get("text"), str)
                        else None
                    )
                    result_text = (
                        portable_result["text"]
                        if portable_result is not None
                        else content_text(result)
                    )
                    result_content = (
                        portable_result["content"] if portable_result is not None else result
                    )
                    events.append(
                        Event(
                            kind=EventKind.TOOL_RESULT,
                            role=Role.TOOL,
                            tool_name=name,
                            tool_call_id=call_id,
                            text=result_text,
                            timestamp=timestamp,
                            payload={
                                "content": result_content,
                                "is_error": invocation.get("isError") is True,
                            },
                            provenance=provenance,
                        )
                    )
                elif state == "output-error":
                    error_text = string(invocation.get("errorText"))
                    if error_text:
                        events.append(
                            Event(
                                kind=EventKind.TOOL_RESULT,
                                role=Role.TOOL,
                                tool_name=name,
                                tool_call_id=call_id,
                                text=error_text,
                                timestamp=timestamp,
                                payload={"is_error": True},
                                provenance=provenance,
                            )
                        )
                continue
        if part_type in {"file", "image"}:
            data = part.get("data") if part_type == "file" else part.get("image")
            media_type = string(part.get("mimeType")) or string(part.get("mediaType"))
            image_url = _portable_native_image(data, media_type)
            if image_url:
                events.append(
                    Event(
                        kind=EventKind.CONTEXT,
                        role=role,
                        timestamp=timestamp,
                        payload={"block_type": "image", "image_url": image_url},
                        provenance=provenance,
                    )
                )
                continue
        if part_type == "data-user-message" and isinstance(part.get("data"), dict):
            data = part["data"]
            text = string(data.get("message")) or string(data.get("content"))
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
                continue
        events.append(
            Event(
                kind=EventKind.OPAQUE,
                payload={"reason": "mastracode_part", "part_type": part_type},
                provenance=provenance,
            )
        )
    return events


def _active_observation(db: sqlite3.Connection, session_id: str) -> str | None:
    table = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mastra_observational_memory'"
    ).fetchone()
    if not table:
        return None
    try:
        row = db.execute(
            'SELECT activeObservations FROM "mastra_observational_memory" '
            "WHERE threadId=? ORDER BY updatedAt DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    return _optional_text(row[0]).strip() or None if row else None


def _decode_metadata(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, memoryview):
        raw = value.tobytes()
    elif isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    else:
        return {}
    if not raw or len(raw) > MAX_JSONB_BYTES:
        return {}
    try:
        decoded, end = _jsonb_element(raw, 0, depth=0, budget=[MAX_JSON_NODES])
    except (UnicodeDecodeError, ValueError, OverflowError, RecursionError):
        return {}
    return decoded if end == len(raw) and isinstance(decoded, dict) else {}


def _jsonb_element(data: bytes, offset: int, *, depth: int, budget: list[int]) -> tuple[Any, int]:
    if depth > MAX_JSON_DEPTH or budget[0] <= 0 or offset >= len(data):
        raise ValueError("invalid SQLite JSONB shape")
    budget[0] -= 1
    first = data[offset]
    size_code, kind = first >> 4, first & 0x0F
    if size_code <= 11:
        payload_size, header = size_code, 1
    else:
        size_bytes = {12: 1, 13: 2, 14: 4, 15: 8}[size_code]
        if offset + 1 + size_bytes > len(data):
            raise ValueError("truncated SQLite JSONB header")
        payload_size = int.from_bytes(data[offset + 1 : offset + 1 + size_bytes], "big")
        header = 1 + size_bytes
    start, end = offset + header, offset + header + payload_size
    if end > len(data):
        raise ValueError("truncated SQLite JSONB payload")
    payload = data[start:end]
    if kind == 0:
        if payload:
            raise ValueError("invalid JSONB null")
        return None, end
    if kind == 1:
        return True, end
    if kind == 2:
        return False, end
    if kind in {3, 4}:
        text = payload.decode("ascii")
        return int(text, 0 if kind == 4 else 10), end
    if kind in {5, 6}:
        return float(payload.decode("ascii")), end
    if kind in {7, 8, 9, 10}:
        text = payload.decode()
        if kind in {8, 9}:
            with suppress(json.JSONDecodeError):
                return json.loads(f'"{text}"'), end
        return text, end
    if kind == 11:
        values: list[Any] = []
        cursor = start
        while cursor < end:
            item, cursor = _jsonb_element(data, cursor, depth=depth + 1, budget=budget)
            values.append(item)
        if cursor != end:
            raise ValueError("invalid JSONB array")
        return values, end
    if kind == 12:
        values: dict[str, Any] = {}
        cursor = start
        while cursor < end:
            key, cursor = _jsonb_element(data, cursor, depth=depth + 1, budget=budget)
            value, cursor = _jsonb_element(data, cursor, depth=depth + 1, budget=budget)
            if not isinstance(key, str) or key in values:
                raise ValueError("invalid JSONB object key")
            values[key] = value
        if cursor != end:
            raise ValueError("invalid JSONB object")
        return values, end
    raise ValueError("reserved SQLite JSONB element type")


def _jsonb_encode(value: Any, *, depth: int = 0, budget: list[int] | None = None) -> bytes:
    if budget is None:
        budget = [MAX_JSON_NODES]
    if depth > MAX_JSON_DEPTH or budget[0] <= 0:
        raise SessionMigrateError("MastraCode metadata exceeds the JSONB shape limit")
    budget[0] -= 1
    if value is None:
        return _jsonb_header(0, 0)
    if value is True:
        return _jsonb_header(1, 0)
    if value is False:
        return _jsonb_header(2, 0)
    if isinstance(value, int) and not isinstance(value, bool):
        payload = str(value).encode()
        return _jsonb_header(3, len(payload)) + payload
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SessionMigrateError("MastraCode metadata cannot contain non-finite numbers")
        payload = json.dumps(value, allow_nan=False).encode()
        return _jsonb_header(5, len(payload)) + payload
    if isinstance(value, str):
        raw = value.encode()
        escaped = json.dumps(value, ensure_ascii=False)[1:-1].encode()
        kind, payload = (7, raw) if escaped == raw else (8, escaped)
        return _jsonb_header(kind, len(payload)) + payload
    if isinstance(value, (list, tuple)):
        payload = b"".join(_jsonb_encode(item, depth=depth + 1, budget=budget) for item in value)
        return _jsonb_header(11, len(payload)) + payload
    if isinstance(value, dict):
        pieces: list[bytes] = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise SessionMigrateError("MastraCode metadata keys must be strings")
            pieces.append(_jsonb_encode(key, depth=depth + 1, budget=budget))
            pieces.append(_jsonb_encode(item, depth=depth + 1, budget=budget))
        payload = b"".join(pieces)
        return _jsonb_header(12, len(payload)) + payload
    raise SessionMigrateError("MastraCode metadata contains an unsupported JSON value")


def _jsonb_header(kind: int, size: int) -> bytes:
    if size <= 11:
        return bytes([(size << 4) | kind])
    if size <= 0xFF:
        return bytes([0xC0 | kind, size])
    if size <= 0xFFFF:
        return bytes([0xD0 | kind]) + size.to_bytes(2, "big")
    if size <= 0xFFFFFFFF:
        return bytes([0xE0 | kind]) + size.to_bytes(4, "big")
    return bytes([0xF0 | kind]) + size.to_bytes(8, "big")


@contextmanager
def _database_from_bytes(data: bytes) -> Iterator[sqlite3.Connection]:
    descriptor, name = tempfile.mkstemp(prefix="session-migrate-mastracode-read-", suffix=".db")
    path = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(path, 0o600)
        uri = f"file:{quote(str(path), safe='/')}?mode=ro"
        db = sqlite3.connect(uri, uri=True, timeout=5)
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        try:
            yield db
        finally:
            db.close()
    except sqlite3.Error as exc:
        raise SessionMigrateError("unable to open MastraCode SQLite artifact") from exc
    finally:
        path.unlink(missing_ok=True)


@contextmanager
def _consistent_copy(path: Path) -> Iterator[sqlite3.Connection]:
    database = _require_database(path)
    uri = f"file:{quote(str(database), safe='/')}?mode=ro"
    source: sqlite3.Connection | None = None
    copy: sqlite3.Connection | None = None
    try:
        source = sqlite3.connect(uri, uri=True, timeout=5)
        source.execute("PRAGMA query_only=ON")
        source.execute("PRAGMA trusted_schema=OFF")
        copy = sqlite3.connect(":memory:")
        source.backup(copy)
        copy.execute("PRAGMA query_only=ON")
        copy.execute("PRAGMA trusted_schema=OFF")
        yield copy
    except sqlite3.Error as exc:
        raise SessionMigrateError("unable to snapshot MastraCode database") from exc
    finally:
        if copy is not None:
            copy.close()
        if source is not None:
            source.close()


def _require_database(path: Path) -> Path:
    try:
        value = path.expanduser().lstat()
    except OSError as exc:
        raise JsonlError(f"MastraCode database is unavailable: {path}") from exc
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode):
        raise JsonlError("MastraCode database must be a regular file, not a symbolic link")
    if value.st_size <= 0 or value.st_size > MAX_NATIVE_BYTES:
        raise JsonlError("MastraCode database is empty or exceeds the safety limit")
    with path.open("rb") as stream:
        if stream.read(len(SQLITE_HEADER)) != SQLITE_HEADER:
            raise JsonlError("MastraCode database does not have a SQLite header")
    return path.resolve()


def _delete_existing_thread(db: sqlite3.Connection, session_id: str) -> None:
    optional = (
        ("mastra_harness_sessions", "threadId"),
        ("mastra_observational_memory", "threadId"),
        ("mastra_thread_state", "threadId"),
    )
    for table, column in optional:
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone():
            db.execute(f'DELETE FROM "{table}" WHERE "{column}"=?', (session_id,))
    db.execute('DELETE FROM "mastra_messages" WHERE thread_id=?', (session_id,))
    db.execute('DELETE FROM "mastra_threads" WHERE id=?', (session_id,))


def _tool_result_value(event: Event, dropped: Counter[str]) -> Any:
    blocks = event.payload.get("content_blocks")
    if isinstance(blocks, list):
        unsupported = sum(
            1 for block in blocks if not isinstance(block, dict) or block.get("type") != "text"
        )
        if unsupported:
            dropped["tool_result:non_text_content"] += unsupported
    value = event.payload.get("content")
    if value is None:
        return event.text or ""
    try:
        json.dumps(value, allow_nan=False)
        _validate_json_shape(value)
        if event.text and content_text(value) != event.text:
            return {
                "schema": PORTABLE_TOOL_RESULT_SCHEMA,
                "text": event.text,
                "content": value,
            }
        return value
    except (TypeError, ValueError, RecursionError):
        dropped["tool_result:non_json_content"] += 1
        return event.text or content_text(value) or str(value)


def _portable_native_image(data: Any, media_type: str | None) -> str | None:
    """Validate bounded native data-URL or raw-base64 image storage."""

    if not isinstance(data, str) or not media_type:
        return None
    candidate = data if data.startswith("data:") else f"data:{media_type};base64,{data}"
    image = portable_data_image(candidate)
    if image is None or image[0] != media_type:
        return None
    encoded = image[1]
    if len(encoded) > 4 * ((MAX_INLINE_IMAGE_BYTES + 2) // 3):
        return None
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(decoded) > MAX_INLINE_IMAGE_BYTES:
        return None
    return f"data:{media_type};base64,{encoded}"


def _metadata_cwd(metadata: Mapping[str, Any]) -> Path | None:
    value = metadata.get("projectPath")
    return Path(value) if isinstance(value, str) and value and "\x00" not in value else None


def _metadata_cli_version(metadata: Mapping[str, Any]) -> str | None:
    migration = metadata.get("sessionMigrate")
    value = migration.get("cliVersion") if isinstance(migration, dict) else None
    return value if isinstance(value, str) and value else None


def _metadata_model(metadata: Mapping[str, Any]) -> str | None:
    for key in ("currentModelId", "modeModelId_build", "modeModelId_plan", "modeModelId_fast"):
        value = metadata.get(key)
        if isinstance(value, str) and value:
            return value
    migration = metadata.get("sessionMigrate")
    migrated = migration.get("model") if isinstance(migration, dict) else None
    if isinstance(migrated, str) and migrated:
        return migrated
    return None


def _session_digest(
    thread: Sequence[Any], rows: Sequence[Sequence[Any]], observation: str | None
) -> str:
    digest = hashlib.sha256()
    for value in [*thread, observation, *[item for row in rows for item in row]]:
        if value is None:
            raw = b""
        elif isinstance(value, bytes):
            raw = value
        elif isinstance(value, memoryview):
            raw = value.tobytes()
        else:
            raw = str(value).encode()
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return digest.hexdigest()


def _derived_title(events: Sequence[Event]) -> str | None:
    for event in events:
        if event.kind == EventKind.MESSAGE and event.role == Role.USER and event.text:
            compact = " ".join(event.text.split())
            return compact[:77] + "..." if len(compact) > 80 else compact
    return None


def _normalize_git_url(value: str) -> str:
    normalized = re.sub(r"\.git$", "", value)
    normalized = re.sub(r"^git@([^:]+):", r"https://\1/", normalized)
    normalized = re.sub(r"^ssh://git@", "https://", normalized)
    return normalized.lower()


def _portable_timestamp(value: str | None) -> str | None:
    return valid_rfc3339(value)


def _rfc3339_ns(value: str | None) -> int | None:
    parsed = valid_rfc3339(value)
    if not parsed:
        return None
    try:
        stamp = datetime.fromisoformat(parsed.replace("Z", "+00:00")).timestamp()
    except (OverflowError, ValueError):
        return None
    return int(stamp * 1_000_000_000)


def _validate_text(value: str, description: str, *, maximum: int) -> None:
    if not value or "\x00" in value or len(value.encode()) > maximum:
        raise SessionMigrateError(f"{description} is invalid")


def _required_text(value: Any, description: str) -> str:
    result = _optional_text(value)
    if not result or "\x00" in result:
        raise SessionMigrateError(f"{description} is invalid")
    return result


def _optional_text(value: Any) -> str:
    if value is None:
        return ""
    return _text(value)


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return str(value)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _validate_json_shape(value: Any) -> None:
    stack: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise ValueError("JSON shape exceeds the safety limit")
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
        elif not isinstance(current, (str, int, float, bool, type(None))):
            raise ValueError("JSON contains an unsupported value")
        elif isinstance(current, float) and not math.isfinite(current):
            raise ValueError("JSON contains a non-finite number")


def _omission_key(event: Event) -> str:
    if event.kind == EventKind.OPAQUE:
        reason = string(event.payload.get("reason")) or "unknown"
        return f"opaque:{reason}"
    return event.kind.value


def _mkdir_private(path: Path) -> None:
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for item in reversed(missing):
        item.mkdir(mode=0o700)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
