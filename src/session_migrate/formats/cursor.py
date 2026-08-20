"""Pinned clean-room Cursor Agent SQLite/protobuf text-session adapter.

The private store described here is verified only for Cursor Agent
``2026.03.20-44cb435`` on Linux x86_64.  This module contains independently
implemented SQLite and protobuf-wire handling; it contains no vendor source,
generated code, descriptors, or binary assets.

Only user and assistant text history is written.  Every unsupported semantic
class is counted by :func:`serialize`, and native source parsing projects only
text while returning its own counted ``losses``.  Installation fails closed
unless both the launcher and its shipped JavaScript bundle match the exact
tested build.
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
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from session_migrate.errors import SessionMigrateError
from session_migrate.formats.common import valid_rfc3339
from session_migrate.jsonl import write_private_atomic
from session_migrate.model import AgentFormat, Event, EventKind, Provenance, Role, Session

PINNED_CURSOR_VERSION = "2026.03.20-44cb435"
PINNED_CURSOR_LAUNCHER_SHA256 = (
    "8756ac4a808cc90b220416ac8743560aa473a94d6fe5911bb602c250c046c4a3"
)
PINNED_CURSOR_LAUNCHER_SIZE = 800
PINNED_CURSOR_BUNDLE_SHA256 = (
    "a7961f327172fa9eecdf69d3941c86a5c2785103bebaf63183ad8e9522f3f620"
)
PINNED_CURSOR_BUNDLE_SIZE = 7_361_289
PINNED_CURSOR_PROTO_CHUNK_SHA256 = (
    "7226059f6a648d5a25a4e0ef1f2bee363879baecc2468aa3ade4c6e481b15423"
)
PINNED_CURSOR_PROTO_CHUNK_SIZE = 11_839_834
PINNED_CURSOR_NODE_SHA256 = (
    "e0e46d3a1c0667117303412647cafcbcefb1be7612493015ec8fd6b7440162a4"
)
PINNED_CURSOR_NODE_SIZE = 129_074_464

MAX_NATIVE_BYTES = 256 * 1024 * 1024
MAX_BLOBS = 500_000
MAX_BLOB_BYTES = 64 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024
MAX_PROTO_FIELDS = 500_000
MAX_TEXT_BYTES = 64 * 1024 * 1024

_MODES = {"default", "auto-run", "plan", "background", "search", "debug"}
_METADATA_KEYS = {
    "agentId",
    "latestRootBlobId",
    "name",
    "createdAt",
    "mode",
    "lastUsedModel",
    "resumeBcId",
    "currentPlanUri",
}

_TABLE_SCHEMA: dict[str, tuple[tuple[str, str, int, str | None, int], ...]] = {
    "blobs": (
        ("id", "TEXT", 0, None, 1),
        ("data", "BLOB", 0, None, 0),
    ),
    "meta": (
        ("key", "TEXT", 0, None, 1),
        ("value", "TEXT", 0, None, 0),
    ),
}


@dataclass(frozen=True, slots=True)
class ParsedCursorSession:
    """Portable text projection and native metadata from one Cursor store."""

    session_id: str
    source_path: Path
    cwd: Path | None
    started_at: str
    cli_version: str
    model: str | None
    title: str
    events: tuple[Event, ...]
    raw_record_count: int
    snapshot_sha256: str
    losses: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class InstalledCursorSession:
    """Location written by :func:`install_database`."""

    conversation_path: Path


@dataclass(frozen=True, slots=True)
class _WireField:
    number: int
    wire_type: int
    value: int | bytes


@dataclass(frozen=True, slots=True)
class _NativeProjection:
    metadata: dict[str, Any]
    events: tuple[Event, ...]
    turn_count: int
    losses: Counter[str]
    reachable_blob_ids: frozenset[str]


def serialize(
    session: Session,
    *,
    session_id: str,
    cwd: Path,
    timestamp: str | None = None,
    title: str | None = None,
) -> tuple[bytes, dict[str, int]]:
    """Encode user/assistant text as a new Cursor content-addressed store.

    The returned counter is part of the API: callers must surface it rather
    than presenting this intentionally text-only conversion as lossless.
    """

    _require_uuid4(session_id, "Cursor session ID")
    workspace = _absolute_workspace(cwd)
    del workspace  # The workspace selects the install path, not database bytes.
    started_at = valid_rfc3339(timestamp) or valid_rfc3339(session.started_at) or _utc_now()
    dropped: Counter[str] = Counter()

    dropped["runtime_metadata:source_format"] += 1
    if session.cli_version:
        dropped["runtime_metadata:source_cli_version"] += 1
    if session.model:
        dropped["runtime_metadata:model"] += 1
    if session.model_provider:
        dropped["runtime_metadata:model_provider"] += 1

    turns: list[tuple[str, list[str]]] = []
    for event in session.events:
        image_count = _count_images(event.payload)
        if image_count:
            dropped["image:unsupported"] += image_count

        if event.kind == EventKind.MESSAGE:
            if event.role == Role.SYSTEM:
                dropped["system:unsupported"] += 1
                continue
            if event.role == Role.TOOL:
                dropped["tool_result:unsupported"] += 1
                continue
            if event.role not in {Role.USER, Role.ASSISTANT}:
                dropped["message:unknown_role"] += 1
                continue
            if not event.text:
                dropped["message:empty"] += 1
                continue
            _validate_text(event.text, "Cursor message")
            if event.timestamp:
                dropped["runtime_metadata:event_timestamp"] += 1
            if event.payload:
                dropped["runtime_metadata:message_payload"] += 1
            if event.role == Role.USER:
                turns.append((event.text, []))
            elif turns:
                turns[-1][1].append(event.text)
            else:
                dropped["message:assistant_without_user"] += 1
            continue

        if event.kind == EventKind.TOOL_CALL:
            dropped["tool_call:unsupported"] += 1
        elif event.kind == EventKind.TOOL_RESULT:
            dropped["tool_result:unsupported"] += 1
        elif event.kind == EventKind.THINKING:
            dropped["thinking:unsupported"] += 1
        elif event.kind == EventKind.COMPACTION:
            dropped["compaction:unsupported"] += 1
        elif event.kind == EventKind.CONTEXT:
            dropped["context:unsupported"] += 1
        elif event.kind == EventKind.OPAQUE:
            dropped["runtime_metadata:opaque_event"] += 1
        else:
            dropped[f"runtime_metadata:event:{event.kind.value}"] += 1

    if not turns:
        raise SessionMigrateError("Cursor target requires at least one portable user message")

    blobs: dict[str, bytes] = {}
    turn_ids: list[bytes] = []
    namespace = uuid.UUID(session_id)
    for turn_index, (user_text, assistant_texts) in enumerate(turns):
        message_id = str(uuid.uuid5(namespace, f"session-migrate-user-{turn_index}"))
        user = _field_text(1, user_text) + _field_text(2, message_id)
        user_id = _store_blob(blobs, user)

        step_ids: list[bytes] = []
        for assistant_text in assistant_texts:
            assistant = _field_bytes(1, _field_text(1, assistant_text))
            step_ids.append(_store_blob(blobs, assistant))

        agent_turn = _field_bytes(1, user_id)
        agent_turn += b"".join(_field_bytes(2, step_id) for step_id in step_ids)
        turn = _field_bytes(1, agent_turn)
        turn_ids.append(_store_blob(blobs, turn))

    root = b"".join(_field_bytes(8, turn_id) for turn_id in turn_ids)
    root_id = _store_blob(blobs, root)
    metadata = {
        "agentId": session_id,
        "latestRootBlobId": root_id.hex(),
        "name": title or session.title or "Imported conversation",
        "createdAt": _timestamp_ms(started_at),
        "mode": "default",
    }
    data = _build_database(metadata, blobs)
    validate_native_bytes(data, session_id)
    return data, dict(sorted(dropped.items()))


def parse(path: Path, *, cwd: Path | None = None) -> ParsedCursorSession:
    """Read one native store through a bounded, consistent SQLite snapshot."""

    source = path.expanduser()
    snapshot = snapshot_database_bytes(source)
    expected_id = _session_id_from_path(source)
    projection = _parse_database_bytes(snapshot, expected_session_id=expected_id)
    workspace = _absolute_workspace(cwd) if cwd is not None else None
    if workspace is not None:
        expected_parent = workspace_key(workspace)
        try:
            observed_parent = source.parent.parent.name
        except IndexError:
            observed_parent = ""
        if observed_parent and observed_parent != expected_parent:
            raise SessionMigrateError("Cursor store workspace directory does not match cwd")
    metadata = projection.metadata
    return ParsedCursorSession(
        session_id=metadata["agentId"],
        source_path=source.resolve(),
        cwd=workspace,
        started_at=_timestamp_text(metadata["createdAt"]),
        cli_version=PINNED_CURSOR_VERSION,
        model=metadata.get("lastUsedModel"),
        title=metadata["name"],
        events=projection.events,
        raw_record_count=projection.turn_count,
        snapshot_sha256=hashlib.sha256(snapshot).hexdigest(),
        losses=tuple(sorted(projection.losses.items())),
    )


def project_session(parsed: ParsedCursorSession, *, source_format: AgentFormat) -> Session:
    """Project a parsed store after the caller supplies its integrated enum value.

    Cursor is intentionally not added to the shared format enum in this isolated
    adapter change.  The catalog integrator should pass ``AgentFormat.CURSOR``
    once that shared enum member lands.
    """

    accounting_events = tuple(
        Event(
            kind=EventKind.OPAQUE,
            provenance=Provenance(
                parsed.raw_record_count + offset,
                "cursor.omission",
                block_index=occurrence,
            ),
            payload={"reason": f"cursor:{reason}"},
        )
        for offset, (reason, occurrence) in enumerate(
            (reason, occurrence)
            for reason, count in parsed.losses
            for occurrence in range(count)
        )
    )
    return Session(
        source_format=source_format,
        source_path=parsed.source_path,
        source_sha256=parsed.snapshot_sha256,
        session_id=parsed.session_id,
        cwd=parsed.cwd,
        started_at=parsed.started_at,
        cli_version=parsed.cli_version,
        model=parsed.model,
        title=parsed.title,
        events=(*parsed.events, *accounting_events),
        raw_record_count=parsed.raw_record_count + len(accounting_events),
        model_provider=None,
    )


def validate_native_bytes(data: bytes, session_id: str) -> None:
    """Strictly validate bytes generated for the pinned Cursor build."""

    _require_uuid4(session_id, "Cursor session ID")
    projection = _parse_database_bytes(data, expected_session_id=session_id, generated=True)
    if projection.losses:
        raise SessionMigrateError("generated Cursor store contains unsupported native state")
    if not any(event.role == Role.USER for event in projection.events):
        raise SessionMigrateError("generated Cursor store contains no user message")


def snapshot_database_bytes(path: Path) -> bytes:
    """Return a bounded SQLite backup that includes committed WAL state."""

    source = Path(os.path.abspath(path.expanduser()))
    try:
        info = source.lstat()
    except OSError as exc:
        raise SessionMigrateError("cannot inspect Cursor store database") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SessionMigrateError("Cursor store source must be a regular file")
    if info.st_size > MAX_NATIVE_BYTES:
        raise SessionMigrateError("Cursor store exceeds the database size limit")

    with tempfile.TemporaryDirectory(prefix="session-migrate-cursor-snapshot-") as directory:
        target = Path(directory) / "snapshot.db"
        uri = f"file:{quote(str(source), safe='/')}?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True, timeout=5) as origin:
                origin.execute("PRAGMA trusted_schema=OFF")
                with sqlite3.connect(target) as destination:
                    origin.backup(destination)
            data = target.read_bytes()
        except (OSError, sqlite3.Error) as exc:
            raise SessionMigrateError("cannot make a consistent Cursor store snapshot") from exc
    if not data or len(data) > MAX_NATIVE_BYTES:
        raise SessionMigrateError("Cursor store snapshot violates the database size limit")
    return data


def native_record_count(data: bytes) -> int:
    """Return the number of native conversation turns after validation."""

    return _parse_database_bytes(data, expected_session_id=None, generated=True).turn_count


def workspace_key(cwd: Path) -> str:
    """Return Cursor's MD5 workspace key for an absolute normalized path."""

    workspace = _absolute_workspace(cwd)
    return hashlib.md5(str(workspace).encode(), usedforsecurity=False).hexdigest()


def session_relative_path(session_id: str, cwd: Path) -> Path:
    """Return the safe path below a Cursor config root for one session."""

    _require_uuid4(session_id, "Cursor session ID")
    return Path("chats") / workspace_key(cwd) / session_id / "store.db"


def config_home(
    home: Path | None = None, *, environ: Mapping[str, str] | None = None
) -> Path:
    """Resolve the Cursor config root using the pinned CLI's precedence."""

    values = os.environ if environ is None else environ
    explicit = values.get("CURSOR_CONFIG_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    xdg = values.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        return Path(xdg).expanduser() / "cursor"
    return (home or Path.home()).expanduser() / ".cursor"


def verify_pinned_cli(
    executable: Path | None = None, *, environ: Mapping[str, str] | None = None
) -> Path:
    """Resolve and verify the exact tested launcher and JavaScript bundle."""

    values = dict(os.environ if environ is None else environ)
    candidate = str(executable) if executable else shutil.which(
        "cursor-agent", path=values.get("PATH")
    )
    if not candidate:
        raise SessionMigrateError("Cursor Agent executable 'cursor-agent' was not found")
    path = Path(candidate).expanduser().resolve()
    _verify_regular_digest(
        path,
        expected_size=PINNED_CURSOR_LAUNCHER_SIZE,
        expected_digest=PINNED_CURSOR_LAUNCHER_SHA256,
        description="Cursor Agent launcher",
    )
    bundle = path.parent / "index.js"
    _verify_regular_digest(
        bundle,
        expected_size=PINNED_CURSOR_BUNDLE_SIZE,
        expected_digest=PINNED_CURSOR_BUNDLE_SHA256,
        description="Cursor Agent bundle",
    )
    _verify_regular_digest(
        path.parent / "891.index.js",
        expected_size=PINNED_CURSOR_PROTO_CHUNK_SIZE,
        expected_digest=PINNED_CURSOR_PROTO_CHUNK_SHA256,
        description="Cursor Agent protocol chunk",
    )
    _verify_regular_digest(
        path.parent / "node",
        expected_size=PINNED_CURSOR_NODE_SIZE,
        expected_digest=PINNED_CURSOR_NODE_SHA256,
        description="Cursor Agent bundled Node runtime",
    )
    safe_env = {
        key: value
        for key, value in values.items()
        if key in {"HOME", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR"}
    }
    safe_env.setdefault("LC_ALL", "C")
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
        raise SessionMigrateError("cannot query the Cursor Agent version") from exc
    observed = completed.stdout.strip()
    if completed.returncode != 0 or observed != PINNED_CURSOR_VERSION:
        raise SessionMigrateError(
            "Cursor Agent version mismatch: expected "
            f"{PINNED_CURSOR_VERSION}, observed {observed or '<unavailable>'}"
        )
    return path


def install_database(
    data: bytes,
    *,
    session_id: str,
    cwd: Path,
    target_home: Path,
    target_cli: Path | None = None,
    dry_run: bool = False,
    environ: Mapping[str, str] | None = None,
) -> InstalledCursorSession:
    """Install a validated store without overwriting an existing session.

    ``target_home`` is the Cursor config root (normally ``~/.cursor`` or
    ``$XDG_CONFIG_HOME/cursor``), not process ``HOME``.
    """

    validate_native_bytes(data, session_id)
    verify_pinned_cli(target_cli, environ=environ)
    workspace = _absolute_workspace(cwd)
    root = _absolute_no_follow(target_home)
    target = root / session_relative_path(session_id, workspace)
    _check_safe_existing_prefix(root)
    _check_safe_existing_prefix(target.parent)
    if os.path.lexists(target):
        raise SessionMigrateError("Cursor session ID already exists; refusing to overwrite it")
    if dry_run:
        return InstalledCursorSession(target)

    _ensure_private_directory(root)
    _ensure_private_directory(target.parent)
    try:
        identity = write_private_atomic(target, data)
    except SessionMigrateError:
        raise
    try:
        info = target.lstat()
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != identity:
            raise SessionMigrateError("Cursor install path changed during publication")
        parsed = parse(target, cwd=workspace)
        if parsed.session_id != session_id:
            raise SessionMigrateError("installed Cursor session failed identity validation")
    except BaseException:
        _unlink_if_same_file(target, identity)
        raise
    return InstalledCursorSession(target)


def _build_database(metadata: Mapping[str, Any], blobs: Mapping[str, bytes]) -> bytes:
    with tempfile.TemporaryDirectory(prefix="session-migrate-cursor-build-") as directory:
        path = Path(directory) / "store.db"
        try:
            with sqlite3.connect(path) as db:
                db.execute("PRAGMA journal_mode=DELETE")
                db.execute("PRAGMA synchronous=FULL")
                db.execute("PRAGMA user_version=1")
                db.executescript(
                    """
                    CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB);
                    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
                    """
                )
                db.executemany(
                    "INSERT INTO blobs(id,data) VALUES(?,?)",
                    sorted((blob_id, value) for blob_id, value in blobs.items()),
                )
                metadata_bytes = json.dumps(
                    dict(metadata), ensure_ascii=False, separators=(",", ":"), allow_nan=False
                ).encode()
                db.execute(
                    "INSERT INTO meta(key,value) VALUES('0',?)", (metadata_bytes.hex(),)
                )
            data = path.read_bytes()
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise SessionMigrateError("cannot build Cursor store database") from exc
    if not data or len(data) > MAX_NATIVE_BYTES:
        raise SessionMigrateError("generated Cursor store violates the database size limit")
    return data


def _parse_database_bytes(
    data: bytes, *, expected_session_id: str | None, generated: bool = False
) -> _NativeProjection:
    if not data or len(data) > MAX_NATIVE_BYTES:
        raise SessionMigrateError("Cursor store violates the database size limit")
    with _database_from_bytes(data) as db:
        _validate_schema(db)
        metadata = _read_metadata(db)
        if expected_session_id is not None and metadata["agentId"] != expected_session_id:
            raise SessionMigrateError("Cursor metadata session ID does not match its directory")
        blobs = _read_blobs(db)
    projection = _project_native(metadata, blobs, generated=generated)
    if generated and projection.reachable_blob_ids != frozenset(blobs):
        raise SessionMigrateError("generated Cursor store contains unreachable blobs")
    return projection


@contextmanager
def _database_from_bytes(data: bytes) -> Iterator[sqlite3.Connection]:
    with tempfile.TemporaryDirectory(prefix="session-migrate-cursor-parse-") as directory:
        path = Path(directory) / "store.db"
        try:
            path.write_bytes(data)
            uri = f"file:{quote(str(path), safe='/')}?mode=ro&immutable=1"
            db = sqlite3.connect(uri, uri=True, timeout=5)
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("PRAGMA query_only=ON")
        except (OSError, sqlite3.Error) as exc:
            raise SessionMigrateError("cannot open Cursor store database") from exc
        try:
            yield db
        finally:
            db.close()


def _validate_schema(db: sqlite3.Connection) -> None:
    try:
        version = int(db.execute("PRAGMA user_version").fetchone()[0])
        if version != 1:
            raise SessionMigrateError("unsupported Cursor store schema version")
        integrity = db.execute("PRAGMA integrity_check(1)").fetchone()
        if integrity != ("ok",):
            raise SessionMigrateError("Cursor store failed SQLite integrity validation")
        objects = db.execute(
            "SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
    except sqlite3.Error as exc:
        raise SessionMigrateError("cannot inspect Cursor store schema") from exc
    if set(objects) != {("table", "blobs"), ("table", "meta")}:
        raise SessionMigrateError("Cursor store schema objects do not match the pinned build")
    for table, expected in _TABLE_SCHEMA.items():
        try:
            observed = tuple(
                (str(row[1]), str(row[2]), int(row[3]), row[4], int(row[5]))
                for row in db.execute(f'PRAGMA table_info("{table}")')
            )
        except sqlite3.Error as exc:
            raise SessionMigrateError("cannot inspect Cursor store columns") from exc
        if observed != expected:
            raise SessionMigrateError(
                f"Cursor store table {table!r} does not match the pinned schema"
            )


def _read_metadata(db: sqlite3.Connection) -> dict[str, Any]:
    try:
        rows = db.execute("SELECT key,value FROM meta ORDER BY key").fetchall()
    except sqlite3.Error as exc:
        raise SessionMigrateError("cannot read Cursor metadata") from exc
    if len(rows) != 1 or rows[0][0] != "0" or not isinstance(rows[0][1], str):
        raise SessionMigrateError("Cursor metadata table does not contain the pinned singleton")
    encoded = rows[0][1]
    if len(encoded) > MAX_METADATA_BYTES * 2:
        raise SessionMigrateError("Cursor metadata exceeds the size limit")
    try:
        raw = bytes.fromhex(encoded)
        if len(raw) > MAX_METADATA_BYTES:
            raise ValueError
        value = json.loads(raw.decode(), parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise SessionMigrateError("Cursor metadata is not valid hex-encoded JSON") from exc
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise SessionMigrateError("Cursor metadata must be a JSON object")
    unknown = set(value) - _METADATA_KEYS
    if unknown:
        raise SessionMigrateError("Cursor metadata contains fields outside the pinned schema")
    required = {"agentId", "latestRootBlobId", "name", "createdAt", "mode"}
    if not required <= set(value):
        raise SessionMigrateError("Cursor metadata is missing required fields")
    _require_uuid4(value.get("agentId"), "Cursor metadata agent ID")
    root = value.get("latestRootBlobId")
    if not _is_blob_hex(root):
        raise SessionMigrateError("Cursor metadata root blob ID is invalid")
    if not isinstance(value.get("name"), str):
        raise SessionMigrateError("Cursor metadata name must be text")
    _validate_text(value["name"], "Cursor metadata name")
    created = value.get("createdAt")
    if isinstance(created, bool) or not isinstance(created, int) or created < 0:
        raise SessionMigrateError("Cursor metadata creation time is invalid")
    if value.get("mode") not in _MODES:
        raise SessionMigrateError("Cursor metadata mode is outside the pinned enum")
    for key in ("lastUsedModel", "resumeBcId", "currentPlanUri"):
        if key in value and not isinstance(value[key], str):
            raise SessionMigrateError(f"Cursor metadata field {key!r} must be text")
    return value


def _read_blobs(db: sqlite3.Connection) -> dict[str, bytes]:
    try:
        count = int(db.execute("SELECT count(*) FROM blobs").fetchone()[0])
        if count <= 0 or count > MAX_BLOBS:
            raise SessionMigrateError("Cursor blob count violates the safety limit")
        rows = db.execute("SELECT id,data FROM blobs ORDER BY id").fetchall()
    except sqlite3.Error as exc:
        raise SessionMigrateError("cannot read Cursor blobs") from exc
    blobs: dict[str, bytes] = {}
    total = 0
    for blob_id, value in rows:
        if not _is_blob_hex(blob_id) or not isinstance(value, bytes | bytearray | memoryview):
            raise SessionMigrateError("Cursor blob row has an invalid ID or value")
        native = bytes(value)
        total += len(native)
        if len(native) > MAX_BLOB_BYTES or total > MAX_NATIVE_BYTES:
            raise SessionMigrateError("Cursor blob payload violates the safety limit")
        if hashlib.sha256(native).hexdigest() != blob_id:
            raise SessionMigrateError("Cursor blob digest does not match its ID")
        blobs[blob_id] = native
    return blobs


def _project_native(
    metadata: dict[str, Any], blobs: Mapping[str, bytes], *, generated: bool
) -> _NativeProjection:
    losses: Counter[str] = Counter()
    reachable: set[str] = set()
    root_id = metadata["latestRootBlobId"]
    root = _required_blob(blobs, bytes.fromhex(root_id), "conversation root")
    reachable.add(root_id)
    root_fields = _decode_message(root)
    turn_ids: list[bytes] = []
    for field in root_fields:
        if field.number == 8 and field.wire_type == 2 and isinstance(field.value, bytes):
            _require_blob_id(field.value, "Cursor turn")
            turn_ids.append(field.value)
        elif generated:
            raise SessionMigrateError("generated Cursor root contains unsupported fields")
        else:
            losses[_root_loss_key(field.number)] += 1
    if len(set(turn_ids)) != len(turn_ids):
        raise SessionMigrateError("Cursor root contains duplicate turn references")

    events: list[Event] = []
    for turn_index, turn_id in enumerate(turn_ids):
        turn_hex = turn_id.hex()
        turn = _required_blob(blobs, turn_id, "conversation turn")
        reachable.add(turn_hex)
        turn_fields = _decode_message(turn)
        agent_payloads = _bytes_values(turn_fields, 1)
        shell_payloads = _bytes_values(turn_fields, 2)
        unknown_turn = [field for field in turn_fields if field.number not in {1, 2}]
        if unknown_turn and generated:
            raise SessionMigrateError("generated Cursor turn contains unsupported fields")
        losses["runtime_metadata:unknown_turn_field"] += len(unknown_turn)
        if len(agent_payloads) + len(shell_payloads) != 1:
            raise SessionMigrateError("Cursor turn does not contain exactly one turn variant")
        if shell_payloads:
            if generated:
                raise SessionMigrateError("generated Cursor store contains a shell turn")
            losses["tool_call:shell_turn_unsupported"] += 1
            continue

        agent_fields = _decode_message(agent_payloads[0])
        user_ids = _bytes_values(agent_fields, 1)
        step_ids = _bytes_values(agent_fields, 2)
        request_ids = _bytes_values(agent_fields, 3)
        unknown_agent = [field for field in agent_fields if field.number not in {1, 2, 3}]
        if unknown_agent and generated:
            raise SessionMigrateError("generated Cursor agent turn contains unsupported fields")
        losses["runtime_metadata:unknown_agent_turn_field"] += len(unknown_agent)
        if len(user_ids) != 1:
            raise SessionMigrateError("Cursor agent turn does not contain one user reference")
        _require_blob_id(user_ids[0], "Cursor user message")
        if len(request_ids) > 1 or any(not value for value in request_ids):
            raise SessionMigrateError("Cursor agent turn request ID is malformed")
        if request_ids:
            losses["runtime_metadata:request_id"] += 1

        user_id = user_ids[0]
        user_hex = user_id.hex()
        user = _required_blob(blobs, user_id, "user message")
        reachable.add(user_hex)
        user_fields = _decode_message(user)
        user_text = _required_text(user_fields, 1, "Cursor user text")
        _required_text(user_fields, 2, "Cursor user message ID")
        for field in user_fields:
            if field.number in {1, 2}:
                continue
            if generated:
                raise SessionMigrateError("generated Cursor user message has unsupported fields")
            losses[_user_loss_key(field.number)] += 1
        events.append(
            Event(
                kind=EventKind.MESSAGE,
                role=Role.USER,
                text=user_text,
                provenance=Provenance(
                    turn_index, "cursor.user_message", source_id=user_hex, block_index=0
                ),
            )
        )

        for step_index, step_id in enumerate(step_ids, start=1):
            _require_blob_id(step_id, "Cursor conversation step")
            step_hex = step_id.hex()
            step = _required_blob(blobs, step_id, "conversation step")
            reachable.add(step_hex)
            step_fields = _decode_message(step)
            variants = [field for field in step_fields if field.number in {1, 2, 3}]
            unknown_steps = [field for field in step_fields if field.number not in {1, 2, 3}]
            if unknown_steps and generated:
                raise SessionMigrateError("generated Cursor step contains unsupported fields")
            losses["runtime_metadata:unknown_step_field"] += len(unknown_steps)
            if len(variants) != 1 or variants[0].wire_type != 2:
                raise SessionMigrateError("Cursor step does not contain one supported wire variant")
            payload = variants[0].value
            assert isinstance(payload, bytes)
            if variants[0].number == 1:
                assistant_fields = _decode_message(payload)
                assistant_text = _required_text(
                    assistant_fields, 1, "Cursor assistant text"
                )
                extra = [field for field in assistant_fields if field.number != 1]
                if extra and generated:
                    raise SessionMigrateError(
                        "generated Cursor assistant message has unsupported fields"
                    )
                losses["runtime_metadata:assistant_field"] += len(extra)
                events.append(
                    Event(
                        kind=EventKind.MESSAGE,
                        role=Role.ASSISTANT,
                        text=assistant_text,
                        provenance=Provenance(
                            turn_index,
                            "cursor.assistant_message",
                            source_id=step_hex,
                            block_index=step_index,
                        ),
                    )
                )
            elif variants[0].number == 2:
                if generated:
                    raise SessionMigrateError("generated Cursor store contains a tool call")
                losses["tool_call:unsupported"] += 1
            else:
                if generated:
                    raise SessionMigrateError("generated Cursor store contains thinking")
                losses["thinking:unsupported"] += 1

    if metadata.get("mode") != "default":
        losses["runtime_metadata:mode"] += 1
    if metadata.get("resumeBcId"):
        losses["runtime_metadata:resume_backend_id"] += 1
    if metadata.get("currentPlanUri"):
        losses["runtime_metadata:current_plan"] += 1
    losses = +losses  # Drop zero-count keys introduced by structural checks.
    return _NativeProjection(
        metadata,
        tuple(events),
        len(turn_ids),
        losses,
        frozenset(reachable),
    )


def _root_loss_key(number: int) -> str:
    if number == 1:
        return "system:unsupported"
    if number == 4:
        return "tool_call:pending_unsupported"
    if number in {6, 9, 11}:
        return "compaction:unsupported"
    return f"runtime_metadata:root_field_{number}"


def _user_loss_key(number: int) -> str:
    if number == 3:
        return "image:selected_context_unsupported"
    if number == 11:
        return "system:subagent_reminder_unsupported"
    if number == 12:
        return "context:client_resource_unsupported"
    return f"runtime_metadata:user_field_{number}"


def _store_blob(blobs: dict[str, bytes], value: bytes) -> bytes:
    if len(value) > MAX_BLOB_BYTES:
        raise SessionMigrateError("generated Cursor blob exceeds the size limit")
    digest = hashlib.sha256(value).digest()
    blobs[digest.hex()] = value
    return digest


def _required_blob(
    blobs: Mapping[str, bytes], blob_id: bytes, description: str
) -> bytes:
    value = blobs.get(blob_id.hex())
    if value is None:
        raise SessionMigrateError(f"Cursor {description} reference is missing")
    return value


def _decode_message(data: bytes) -> tuple[_WireField, ...]:
    if len(data) > MAX_BLOB_BYTES:
        raise SessionMigrateError("Cursor protobuf message exceeds the size limit")
    fields: list[_WireField] = []
    offset = 0
    while offset < len(data):
        if len(fields) >= MAX_PROTO_FIELDS:
            raise SessionMigrateError("Cursor protobuf message has too many fields")
        tag, offset = _decode_varint(data, offset)
        number, wire_type = tag >> 3, tag & 7
        if number <= 0:
            raise SessionMigrateError("Cursor protobuf message has an invalid field number")
        if wire_type == 0:
            value, offset = _decode_varint(data, offset)
        elif wire_type == 1:
            end = offset + 8
            if end > len(data):
                raise SessionMigrateError("Cursor protobuf fixed64 field is truncated")
            value = data[offset:end]
            offset = end
        elif wire_type == 2:
            size, offset = _decode_varint(data, offset)
            if size > MAX_BLOB_BYTES:
                raise SessionMigrateError("Cursor protobuf bytes field exceeds the size limit")
            end = offset + size
            if end > len(data):
                raise SessionMigrateError("Cursor protobuf bytes field is truncated")
            value = data[offset:end]
            offset = end
        elif wire_type == 5:
            end = offset + 4
            if end > len(data):
                raise SessionMigrateError("Cursor protobuf fixed32 field is truncated")
            value = data[offset:end]
            offset = end
        else:
            raise SessionMigrateError("Cursor protobuf message uses an unsupported wire type")
        fields.append(_WireField(number, wire_type, value))
    return tuple(fields)


def _decode_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise SessionMigrateError("Cursor protobuf varint is truncated")
        byte = data[offset]
        offset += 1
        if shift == 63 and byte > 1:
            raise SessionMigrateError("Cursor protobuf varint overflows uint64")
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, offset
    raise SessionMigrateError("Cursor protobuf varint exceeds ten bytes")


def _varint(value: int) -> bytes:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SessionMigrateError("cannot encode invalid Cursor protobuf varint")
    encoded = bytearray()
    while value >= 0x80:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _field_bytes(number: int, value: bytes) -> bytes:
    if number <= 0 or len(value) > MAX_BLOB_BYTES:
        raise SessionMigrateError("cannot encode invalid Cursor protobuf bytes field")
    return _varint(number * 8 + 2) + _varint(len(value)) + value


def _field_text(number: int, value: str) -> bytes:
    _validate_text(value, "Cursor protobuf text")
    return _field_bytes(number, value.encode())


def _bytes_values(fields: Sequence[_WireField], number: int) -> tuple[bytes, ...]:
    values: list[bytes] = []
    for field in fields:
        if field.number != number:
            continue
        if field.wire_type != 2 or not isinstance(field.value, bytes):
            raise SessionMigrateError("Cursor protobuf field uses the wrong wire type")
        values.append(field.value)
    return tuple(values)


def _required_text(fields: Sequence[_WireField], number: int, description: str) -> str:
    values = _bytes_values(fields, number)
    if len(values) != 1:
        raise SessionMigrateError(f"{description} is missing or repeated")
    try:
        value = values[0].decode()
    except UnicodeDecodeError as exc:
        raise SessionMigrateError(f"{description} is not valid UTF-8") from exc
    _validate_text(value, description)
    return value


def _validate_text(value: str, description: str) -> None:
    if not isinstance(value, str):
        raise SessionMigrateError(f"{description} must be text")
    if len(value.encode()) > MAX_TEXT_BYTES:
        raise SessionMigrateError(f"{description} exceeds the text size limit")


def _require_blob_id(value: bytes, description: str) -> None:
    if len(value) != 32:
        raise SessionMigrateError(f"{description} blob ID must contain 32 bytes")


def _is_blob_hex(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )


def _require_uuid4(value: Any, description: str) -> None:
    if not isinstance(value, str):
        raise SessionMigrateError(f"{description} must be a UUIDv4 string")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise SessionMigrateError(f"{description} must be a UUIDv4 string") from exc
    if parsed.version != 4 or str(parsed) != value.lower():
        raise SessionMigrateError(f"{description} must be a canonical UUIDv4 string")


def _absolute_workspace(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


def _session_id_from_path(path: Path) -> str | None:
    if path.name != "store.db":
        return None
    candidate = path.parent.name
    try:
        _require_uuid4(candidate, "Cursor session directory")
    except SessionMigrateError:
        return None
    return candidate


def _timestamp_ms(value: str) -> int:
    normalized = valid_rfc3339(value)
    if not normalized:
        raise SessionMigrateError("Cursor timestamp must be RFC 3339")
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SessionMigrateError("Cursor timestamp must be RFC 3339") from exc
    return int(parsed.timestamp() * 1000)


def _timestamp_text(value: int) -> str:
    try:
        parsed = datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise SessionMigrateError(
            "Cursor metadata creation time is outside the valid range"
        ) from exc
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _count_images(value: Any) -> int:
    if isinstance(value, dict):
        if value.get("type") == "image" or value.get("block_type") == "image":
            return 1
        return sum(_count_images(item) for item in value.values())
    if isinstance(value, list | tuple):
        return sum(_count_images(item) for item in value)
    return 0


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _verify_regular_digest(
    path: Path, *, expected_size: int, expected_digest: str, description: str
) -> None:
    try:
        info = path.stat()
    except OSError as exc:
        raise SessionMigrateError(f"cannot inspect the {description}") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_size != expected_size:
        raise SessionMigrateError(f"{description} does not match the pinned Cursor build")
    if _stream_sha256(path, maximum=expected_size) != expected_digest:
        raise SessionMigrateError(f"{description} digest does not match the pinned Cursor build")


def _stream_sha256(path: Path, *, maximum: int) -> str:
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                total += len(chunk)
                if total > maximum:
                    raise SessionMigrateError("pinned Cursor file changed during verification")
                digest.update(chunk)
    except OSError as exc:
        raise SessionMigrateError("cannot hash a pinned Cursor file") from exc
    return digest.hexdigest()


def _absolute_no_follow(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


def _check_safe_existing_prefix(path: Path) -> None:
    current = path
    while not os.path.lexists(current):
        if current.parent == current:
            return
        current = current.parent
    try:
        info = current.lstat()
    except OSError as exc:
        raise SessionMigrateError("cannot inspect Cursor install directory") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise SessionMigrateError("Cursor install path contains an unsafe directory")


def _ensure_private_directory(path: Path) -> None:
    missing: list[Path] = []
    current = path
    while not os.path.lexists(current):
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    _check_safe_existing_prefix(current)
    for directory in reversed(missing):
        with suppress(FileExistsError):
            directory.mkdir(mode=0o700)
        info = directory.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise SessionMigrateError("Cursor install directory changed during creation")


def _unlink_if_same_file(path: Path, identity: tuple[int, int]) -> None:
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == identity:
        path.unlink()
