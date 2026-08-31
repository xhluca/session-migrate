"""Devin CLI 3000.6.7 shared-SQLite session adapter.

Devin stores every local conversation in one ``sessions.db``.  A session is
therefore identified by ``(database path, sessions.id)``, not by a transcript
path.  ``message_nodes`` is a forest and ``sessions.main_chain_id`` names the
tip of the active branch; retries and edited-away branches must never be
replayed as conversation history.

The serialized artifact is a bounded, installable JSON bundle rather than a
copy of the shared database.  :func:`install_database` inserts it in one SQLite
transaction and refuses to replace an existing native identity.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import tempfile
import uuid
from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from session_migrate.errors import SessionMigrateError
from session_migrate.formats.common import content_text, portable_data_image, string, valid_rfc3339
from session_migrate.model import AgentFormat, Event, EventKind, Provenance, Role, Session

PINNED_DEVIN_VERSION = "3000.6.7"
PINNED_DEVIN_COMMIT = "260a97c8"
PINNED_DEVIN_LINUX_X64_BYTES = 174_094_920
PINNED_DEVIN_LINUX_X64_SHA256 = "862623068229249a5ac5a560d876532a40bb53fe16049ab7e415ac5d6b8ae36d"
PINNED_DEVIN_LINUX_X64_ARCHIVE_BYTES = 57_776_222
PINNED_DEVIN_LINUX_X64_ARCHIVE_SHA256 = (
    "f88edacea692553910d72f275515bd0b52b5d271d55250981b0c41011142d27b"
)

DEVIN_BUNDLE_SCHEMA = "session-migrate.devin.v1"
DEVIN_DATABASE_FILENAME = "sessions.db"
DEVIN_SCHEMA_VERSION = 16
MAX_BUNDLE_BYTES = 256 * 1024 * 1024
MAX_DATABASE_BYTES = 16 * 1024 * 1024 * 1024
MAX_CHAIN_NODES = 100_000
MAX_MESSAGE_BYTES = 64 * 1024 * 1024
MAX_CHAIN_BYTES = 256 * 1024 * 1024
MAX_JSON_DEPTH = 96
MAX_JSON_NODES = 1_000_000
MAX_IMAGE_BYTES = 64 * 1024 * 1024

_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_MIGRATIONS: tuple[tuple[int, str, str], ...] = (
    (1, "initial_schema", "9244923949255133246"),
    (2, "add_thinking_column", "14224532003883382682"),
    (3, "add_prompt_history", "12622061916787456317"),
    (4, "add_metadata_column", "10743897110053661924"),
    (5, "message_forest", "9668121967925007678"),
    (6, "add_node_metadata", "8078921727324108560"),
    (7, "add_shell_context", "258551201440621856"),
    (8, "add_session_cogs", "105359765896368723"),
    (9, "add_rendered_commits", "3762685451217257726"),
    (10, "add_workspace_dirs", "10474887258761036276"),
    (11, "add_prompt_history_is_shell", "11375493059147639310"),
    (12, "add_app_state", "14171664772219278324"),
    (13, "rename_permission_mode_to_agent_mode", "14037567567567316550"),
    (14, "tool_call_state", "9276893574920127698"),
    (15, "add_hidden_column", "3632756168171233470"),
    (16, "add_session_json_metadata", "4954231843905863386"),
)

_TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "sessions": (
        "id",
        "working_directory",
        "backend_type",
        "model",
        "agent_mode",
        "created_at",
        "last_activity_at",
        "title",
        "main_chain_id",
        "shell_last_seen_index",
        "cogs_json",
        "workspace_dirs",
        "hidden",
        "metadata",
    ),
    "prompt_history": ("id", "content", "timestamp", "session_id", "is_shell"),
    "message_nodes": (
        "row_id",
        "session_id",
        "node_id",
        "parent_node_id",
        "chat_message",
        "created_at",
        "metadata",
    ),
    "rendered_commits": (
        "id",
        "session_id",
        "sequence_number",
        "rendered_html",
        "created_at",
    ),
    "app_state": ("key", "value"),
    "tool_call_state": (
        "session_id",
        "tool_call_id",
        "tool_call_json",
        "tool_call_update_json",
    ),
    "refinery_schema_history": ("version", "name", "applied_on", "checksum"),
}


@dataclass(frozen=True, slots=True)
class DevinSessionSummary:
    """Content-free catalog metadata for one visible native identity."""

    session_id: str
    title: str | None
    cwd: Path | None
    started_at: str | None
    cli_version: str | None
    updated_ns: int | None
    records: int | None


@dataclass(frozen=True, slots=True)
class ParsedDevinBundle:
    """Validated install payload for one native session."""

    cli_version: str
    session: dict[str, Any]
    nodes: tuple[dict[str, Any], ...]
    prompt_history: tuple[dict[str, Any], ...]
    tool_call_state: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class _NativeSession:
    session_id: str
    cwd: Path
    model: str
    title: str | None
    created_at: int
    last_activity_at: int
    nodes: tuple[tuple[int, int | None, dict[str, Any], int], ...]
    snapshot_sha256: str


def normalized_session_id(value: str) -> str:
    """Validate a native Devin slug or UUID without normalizing its spelling."""

    if not isinstance(value, str) or not _SESSION_ID.fullmatch(value):
        raise SessionMigrateError("source Devin session ID must be a 1-128 character ASCII slug")
    return value


def data_root(
    home: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> Path:
    """Return the CLI data directory without creating it."""

    values = os.environ if environ is None else environ
    current_platform = sys.platform if platform is None else platform
    if current_platform == "win32":
        configured = values.get("APPDATA")
        if configured:
            return _absolute(Path(configured) / "devin" / "cli")
        return _absolute((home or Path.home()) / "AppData/Roaming/devin/cli")
    configured = values.get("XDG_DATA_HOME")
    if configured:
        return _absolute(Path(configured) / "devin" / "cli")
    user_home = _absolute(home or Path.home())
    if current_platform == "darwin":
        return user_home / "Library/Application Support/devin/cli"
    return user_home / ".local/share/devin/cli"


def database_path(root: Path) -> Path:
    """Resolve either a Devin data root or an explicit ``sessions.db`` path."""

    expanded = _absolute(root)
    return (
        expanded if expanded.name == DEVIN_DATABASE_FILENAME else expanded / DEVIN_DATABASE_FILENAME
    )


def session_relative_path() -> Path:
    """Return the single shared database path below the Devin data root."""

    return Path(DEVIN_DATABASE_FILENAME)


def resume_command(session_id: str) -> tuple[str, ...]:
    """Return the official native resume command arguments."""

    return ("devin", "--resume", normalized_session_id(session_id))


def serialize(
    session: Session,
    *,
    session_id: str,
    cwd: Path,
    cli_version: str = PINNED_DEVIN_VERSION,
    model: str | None = None,
    timestamp: str | None = None,
    title: str | None = None,
) -> tuple[bytes, dict[str, int]]:
    """Serialize portable history as one transaction-ready Devin bundle."""

    canonical_id = normalized_session_id(session_id)
    workspace = _absolute(cwd)
    started_text = valid_rfc3339(timestamp) or valid_rfc3339(session.started_at) or _utc_now()
    clock = _epoch_seconds(started_text)
    dropped: Counter[str] = Counter()
    nodes: list[dict[str, Any]] = []
    prompts: list[dict[str, Any]] = []
    tool_names: dict[str, str] = {}
    seen_calls: set[str] = set()
    seen_results: set[str] = set()
    next_node_id = 1
    parent_id: int | None = None

    def append_message(message: dict[str, Any], event_timestamp: str | None) -> dict[str, Any]:
        nonlocal next_node_id, parent_id, clock
        observed = _epoch_seconds(event_timestamp) if valid_rfc3339(event_timestamp) else clock
        created = max(clock, observed)
        value = {
            "node_id": next_node_id,
            "parent_node_id": parent_id,
            "chat_message": message,
            "created_at": created,
            "metadata": None,
        }
        nodes.append(value)
        parent_id = next_node_id
        next_node_id += 1
        clock = created + 1
        return value

    append_message(
        {
            "message_id": str(uuid.uuid4()),
            "role": "system",
            "content": "Imported portable session history.",
            "metadata": {},
        },
        started_text,
    )

    last_user_node: dict[str, Any] | None = None
    last_assistant_node: dict[str, Any] | None = None
    for event in session.events:
        if event.kind == EventKind.MESSAGE and event.role in {Role.USER, Role.ASSISTANT}:
            if not event.text:
                dropped["message:empty"] += 1
                continue
            message_id = str(uuid.uuid4())
            if event.role == Role.USER:
                last_user_node = append_message(
                    {
                        "message_id": message_id,
                        "role": "user",
                        "content": event.text,
                        "metadata": {"is_user_input": True},
                    },
                    event.timestamp,
                )
                last_assistant_node = None
                prompts.append(
                    {
                        "content": event.text,
                        "timestamp": last_user_node["created_at"],
                        "is_shell": 0,
                    }
                )
            else:
                last_assistant_node = append_message(
                    {
                        "message_id": message_id,
                        "role": "assistant",
                        "content": event.text,
                        "thinking": None,
                        "tool_calls": [],
                        "metadata": {},
                    },
                    event.timestamp,
                )
                last_user_node = None
            if event.payload.get("ui_only_projection") is True:
                dropped["message:ui_only_projection"] += 1
            continue

        if event.kind == EventKind.CONTEXT and event.role == Role.USER:
            image = portable_data_image(event.payload.get("image_url"))
            if event.payload.get("block_type") != "image" or image is None:
                dropped["context:image"] += 1
                continue
            media_type, encoded = image
            if len(encoded) > (MAX_IMAGE_BYTES * 4 // 3 + 4):
                dropped["context:image_oversized"] += 1
                continue
            if last_user_node is None:
                last_user_node = append_message(
                    {
                        "message_id": str(uuid.uuid4()),
                        "role": "user",
                        "content": "",
                        "metadata": {"is_user_input": True},
                    },
                    event.timestamp,
                )
            message = last_user_node["chat_message"]
            message.setdefault("images", []).append(
                {
                    "width": 0,
                    "height": 0,
                    "media_type": media_type,
                    "base64_data": encoded,
                }
            )
            continue

        if event.kind == EventKind.TOOL_CALL:
            call_id = event.tool_call_id or f"call_session_migrate_{uuid.uuid4().hex}"
            if not event.tool_call_id:
                dropped["tool_call:missing_id"] += 1
            if call_id in seen_calls:
                dropped["tool_call:duplicate_id"] += 1
                call_id = f"{call_id}__session_migrate_{uuid.uuid4().hex}"
            seen_calls.add(call_id)
            tool_name = event.tool_name or "unknown_tool"
            if not event.tool_name:
                dropped["tool_call:missing_name"] += 1
            arguments = event.payload.get("input", {})
            if not isinstance(arguments, dict):
                arguments = {"input": arguments}
                dropped["tool_call:non_object_input"] += 1
            if last_assistant_node is None:
                last_assistant_node = append_message(
                    {
                        "message_id": str(uuid.uuid4()),
                        "role": "assistant",
                        "content": "",
                        "thinking": None,
                        "tool_calls": [],
                        "metadata": {},
                    },
                    event.timestamp,
                )
            last_assistant_node["chat_message"]["tool_calls"].append(
                {
                    "id": call_id,
                    "index": len(last_assistant_node["chat_message"]["tool_calls"]),
                    "kind": "function",
                    "name": tool_name,
                    "arguments": arguments,
                }
            )
            tool_names[call_id] = tool_name
            last_user_node = None
            continue

        if event.kind == EventKind.TOOL_RESULT:
            call_id = event.tool_call_id
            if not call_id:
                dropped["tool_result:missing_id"] += 1
                continue
            if call_id not in seen_calls:
                dropped["tool_result:orphan_id"] += 1
                continue
            if call_id in seen_results:
                dropped["tool_result:duplicate_id"] += 1
                continue
            seen_results.add(call_id)
            result = event.text or content_text(event.payload.get("content")) or ""
            append_message(
                {
                    "message_id": str(uuid.uuid4()),
                    "role": "tool",
                    "content": result,
                    "tool_call_id": call_id,
                    "tool_name": tool_names.get(call_id),
                    "is_error": event.payload.get("is_error") is True,
                    "metadata": {},
                },
                event.timestamp,
            )
            blocks = event.payload.get("content_blocks")
            if isinstance(blocks, list):
                dropped["tool_result:non_text_content"] += sum(
                    1
                    for block in blocks
                    if not isinstance(block, dict) or block.get("type") != "text"
                )
            last_assistant_node = None
            last_user_node = None
            continue

        if event.kind == EventKind.COMPACTION and event.text:
            imported = f"[Imported conversation summary]\n{event.text}"
            node = append_message(
                {
                    "message_id": str(uuid.uuid4()),
                    "role": "user",
                    "content": imported,
                    "metadata": {"is_user_input": False, "session_migrate_compaction": True},
                },
                event.timestamp,
            )
            dropped["compaction:flattened"] += 1
            if event.payload.get("has_boundary_metadata") is True:
                dropped["compaction:boundary_metadata"] += 1
            if event.payload.get("replacement_history_expanded") is True:
                dropped["compaction:replacement_history_expanded"] += 1
            last_user_node = node
            last_assistant_node = None
            continue

        if event.kind == EventKind.THINKING:
            dropped["thinking:private"] += 1
            if event.payload.get("signature") or event.payload.get("encrypted_content"):
                dropped["thinking:provider_payload"] += 1
            continue

        if event.kind == EventKind.MESSAGE and event.role == Role.SYSTEM:
            dropped["system:runtime"] += 1
            continue

        dropped[_omission_key(event)] += 1

    if not any(node["chat_message"]["role"] == "user" for node in nodes):
        raise SessionMigrateError("Devin target requires at least one portable user message")

    session_row = {
        "id": canonical_id,
        "working_directory": str(workspace),
        "backend_type": "Windsurf",
        "model": model or session.model or "swe-1-6-fast",
        "agent_mode": "auto",
        "created_at": nodes[0]["created_at"],
        "last_activity_at": nodes[-1]["created_at"],
        "title": title or session.title or "Imported conversation",
        "main_chain_id": nodes[-1]["node_id"],
        "shell_last_seen_index": 0,
        "cogs_json": None,
        "workspace_dirs": json.dumps([str(workspace)], separators=(",", ":")),
        "hidden": 0,
        "metadata": json.dumps(
            {"session_migrate": {"schema": DEVIN_BUNDLE_SCHEMA}}, separators=(",", ":")
        ),
    }
    bundle = {
        "schema": DEVIN_BUNDLE_SCHEMA,
        "cli_version": cli_version,
        "session": session_row,
        "nodes": nodes,
        "prompt_history": prompts,
        # Completed calls are model-visible in message_nodes. Runtime tool-call
        # UI state is deliberately not fabricated without a signed native trace.
        "tool_call_state": [],
    }
    data = (
        json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    validate_native_bytes(data, canonical_id)
    return data, dict(sorted(dropped.items()))


def validate_native_bytes(data: bytes, session_id: str) -> ParsedDevinBundle:
    """Decode and strictly validate one generated import bundle."""

    expected_id = normalized_session_id(session_id)
    value = _decode_bundle(data)
    if value.get("schema") != DEVIN_BUNDLE_SCHEMA:
        raise SessionMigrateError("Devin bundle has an unsupported schema")
    cli_version = string(value.get("cli_version"))
    if cli_version != PINNED_DEVIN_VERSION:
        raise SessionMigrateError("Devin bundle CLI version does not match the pinned writer")
    session_row = value.get("session")
    nodes = value.get("nodes")
    prompts = value.get("prompt_history")
    tool_states = value.get("tool_call_state")
    if not isinstance(session_row, dict):
        raise SessionMigrateError("Devin bundle session metadata is missing")
    if not isinstance(nodes, list) or not nodes or len(nodes) > MAX_CHAIN_NODES:
        raise SessionMigrateError("Devin bundle has an invalid message-node list")
    if not isinstance(prompts, list) or len(prompts) > MAX_CHAIN_NODES:
        raise SessionMigrateError("Devin bundle has invalid prompt history")
    if not isinstance(tool_states, list) or len(tool_states) > MAX_CHAIN_NODES:
        raise SessionMigrateError("Devin bundle has invalid tool-call state")
    _validate_session_row(session_row, expected_id)
    _validate_generated_nodes(nodes, session_row)
    for prompt in prompts:
        if (
            not isinstance(prompt, dict)
            or not isinstance(prompt.get("content"), str)
            or not _non_negative_int(prompt.get("timestamp"))
            or prompt.get("is_shell") not in {0, 1}
        ):
            raise SessionMigrateError("Devin bundle has an invalid prompt-history record")
    for state in tool_states:
        call_id = string(state.get("tool_call_id")) if isinstance(state, dict) else None
        if not call_id or len(call_id.encode()) > 512:
            raise SessionMigrateError("Devin bundle has invalid tool-call state")
    return ParsedDevinBundle(
        cli_version=cli_version,
        session=dict(session_row),
        nodes=tuple(dict(node) for node in nodes),
        prompt_history=tuple(dict(prompt) for prompt in prompts),
        tool_call_state=tuple(dict(state) for state in tool_states),
    )


def native_record_count(data: bytes) -> int:
    """Count the native main-chain message nodes in a bundle."""

    value = _decode_bundle(data)
    nodes = value.get("nodes")
    if not isinstance(nodes, list):
        raise SessionMigrateError("Devin bundle has an invalid message-node list")
    return len(nodes)


def list_sessions(path: Path) -> tuple[DevinSessionSummary, ...]:
    """List visible logical sessions without reading conversation bodies."""

    database = database_path(path)
    with _read_database(database) as db:
        rows = db.execute(
            """
            SELECT id, title, working_directory, created_at, last_activity_at, main_chain_id
            FROM sessions WHERE hidden = 0 ORDER BY last_activity_at DESC, id
            """
        ).fetchall()
        summaries: list[DevinSessionSummary] = []
        for row in rows:
            session_id = normalized_session_id(_required_text(row[0], "Devin session ID"))
            created = _required_epoch(row[3], "Devin created_at")
            updated = _required_epoch(row[4], "Devin last_activity_at")
            main_chain_id = row[5]
            records = (
                _active_chain_count(db, session_id, main_chain_id)
                if _non_negative_int(main_chain_id)
                else 0
            )
            summaries.append(
                DevinSessionSummary(
                    session_id=session_id,
                    title=string(row[1]),
                    cwd=Path(row[2]) if string(row[2]) else None,
                    started_at=_timestamp_text(created),
                    cli_version=PINNED_DEVIN_VERSION,
                    updated_ns=updated * 1_000_000_000,
                    records=records,
                )
            )
        return tuple(summaries)


def parse_session(path: Path, session_id: str | None = None) -> Session:
    """Project one active native main chain into the portable model."""

    database = database_path(path)
    with _read_database(database) as db:
        selected_id = normalized_session_id(session_id) if session_id else _only_visible_id(db)
        native = _read_native_session(db, selected_id)
    events: list[Event] = []
    tool_names: dict[str, str] = {}
    for record_index, (node_id, _parent_id, message, created_at) in enumerate(native.nodes):
        timestamp = _timestamp_text(created_at)
        role = message.get("role")
        message_id = string(message.get("message_id"))
        provenance = Provenance(record_index, f"message_nodes:{role}", message_id or str(node_id))
        text = _message_text(message.get("content"))
        if role == "user":
            metadata = message.get("metadata")
            if (
                text
                and isinstance(metadata, dict)
                and metadata.get("session_migrate_compaction") is True
            ):
                prefix = "[Imported conversation summary]\n"
                if not text.startswith(prefix):
                    raise SessionMigrateError(
                        "Devin imported compaction marker has an invalid content prefix"
                    )
                events.append(
                    Event(
                        EventKind.COMPACTION,
                        provenance,
                        timestamp=timestamp,
                        text=text.removeprefix(prefix),
                        payload={"source": "session_migrate_import"},
                    )
                )
                continue
            if text:
                events.append(
                    Event(
                        EventKind.MESSAGE,
                        provenance,
                        role=Role.USER,
                        timestamp=timestamp,
                        text=text,
                    )
                )
            events.extend(_user_image_events(message, provenance, timestamp))
            continue
        if role == "assistant":
            thinking = message.get("thinking")
            if isinstance(thinking, dict) and string(thinking.get("thinking")):
                payload: dict[str, Any] = {"private": True}
                if string(thinking.get("signature")):
                    payload["signature"] = thinking["signature"]
                events.append(
                    Event(
                        EventKind.THINKING,
                        provenance,
                        role=Role.ASSISTANT,
                        timestamp=timestamp,
                        text=thinking["thinking"],
                        payload=payload,
                    )
                )
            if text:
                events.append(
                    Event(
                        EventKind.MESSAGE,
                        provenance,
                        role=Role.ASSISTANT,
                        timestamp=timestamp,
                        text=text,
                    )
                )
            calls = message.get("tool_calls", [])
            if not isinstance(calls, list):
                raise SessionMigrateError("Devin assistant message has invalid tool calls")
            for block_index, call in enumerate(calls):
                if not isinstance(call, dict):
                    raise SessionMigrateError("Devin assistant message has an invalid tool call")
                call_id = _required_text(call.get("id"), "Devin tool-call ID")
                name = _required_text(call.get("name"), "Devin tool name")
                arguments = call.get("arguments", {})
                if not isinstance(arguments, dict):
                    raise SessionMigrateError("Devin tool-call arguments are not an object")
                tool_names[call_id] = name
                events.append(
                    Event(
                        EventKind.TOOL_CALL,
                        Provenance(
                            record_index,
                            "message_nodes:assistant",
                            message_id or str(node_id),
                            block_index,
                        ),
                        role=Role.ASSISTANT,
                        timestamp=timestamp,
                        tool_name=name,
                        tool_call_id=call_id,
                        payload={"input": arguments},
                    )
                )
            if not text and not calls and not isinstance(thinking, dict):
                events.append(_opaque_event(provenance, timestamp, "devin_empty_assistant"))
            continue
        if role == "tool":
            call_id = _required_text(message.get("tool_call_id"), "Devin tool-result call ID")
            if call_id not in tool_names:
                raise SessionMigrateError("Devin main chain contains an orphan tool result")
            events.append(
                Event(
                    EventKind.TOOL_RESULT,
                    provenance,
                    role=Role.TOOL,
                    timestamp=timestamp,
                    text=text or "",
                    tool_name=string(message.get("tool_name")) or tool_names.get(call_id),
                    tool_call_id=call_id,
                    payload={"is_error": message.get("is_error") is True},
                )
            )
            if isinstance(message.get("images"), list) and message["images"]:
                events.append(_opaque_event(provenance, timestamp, "devin_tool_result_images"))
            continue
        if role == "system":
            events.append(_opaque_event(provenance, timestamp, "devin_system_message"))
            continue
        events.append(_opaque_event(provenance, timestamp, "devin_unknown_message_role"))

    return Session(
        source_format=AgentFormat.DEVIN,
        source_path=Path(f"devin:{native.session_id}"),
        source_sha256=native.snapshot_sha256,
        session_id=native.session_id,
        cwd=native.cwd,
        started_at=_timestamp_text(native.created_at),
        cli_version=PINNED_DEVIN_VERSION,
        model=native.model,
        title=native.title,
        events=tuple(events),
        raw_record_count=len(native.nodes),
    )


def install_database(
    data: bytes,
    target_root: Path,
    expected_session_id: str,
    *,
    dry_run: bool = False,
) -> Path:
    """Install one bundle into Devin's shared store in a single transaction."""

    parsed = validate_native_bytes(data, expected_session_id)
    database = database_path(target_root)
    if dry_run:
        _check_safe_existing_prefix(database.parent)
        if os.path.lexists(database):
            _validate_database_file(database)
            with _read_database(database) as db:
                if db.execute(
                    "SELECT 1 FROM sessions WHERE id = ?", (parsed.session["id"],)
                ).fetchone():
                    raise SessionMigrateError("refusing to overwrite an existing Devin session")
        else:
            with sqlite3.connect(":memory:") as db:
                _create_schema(db)
                _validate_schema(db)
        return database
    _ensure_private_directory(database.parent)
    if os.path.lexists(database):
        _validate_database_file(database)
        _insert_bundle(database, parsed)
        return database

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".sessions.", suffix=".db.tmp", dir=database.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    published: tuple[int, int] | None = None
    try:
        os.chmod(temporary, 0o600)
        with sqlite3.connect(temporary) as db:
            _create_schema(db)
        _insert_bundle(temporary, parsed)
        # The database is staged under a temporary name. WAL sidecars use that
        # name and cannot follow the final hard-link publication, so checkpoint
        # every committed page into the main file before publishing it.
        with sqlite3.connect(temporary) as db:
            checkpoint = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is None or checkpoint[0] != 0:
                raise SessionMigrateError("cannot checkpoint the staged Devin database")
        try:
            os.link(temporary, database)
        except FileExistsError as exc:
            raise SessionMigrateError(
                f"refusing to overwrite existing Devin store: {database}"
            ) from exc
        info = database.lstat()
        published = (info.st_dev, info.st_ino)
        temporary.unlink()
        temporary.with_name(f"{temporary.name}-wal").unlink(missing_ok=True)
        temporary.with_name(f"{temporary.name}-shm").unlink(missing_ok=True)
        _fsync_directory(database.parent)
        return database
    except BaseException:
        temporary.unlink(missing_ok=True)
        temporary.with_name(f"{temporary.name}-wal").unlink(missing_ok=True)
        temporary.with_name(f"{temporary.name}-shm").unlink(missing_ok=True)
        if published is not None:
            _unlink_if_same_file(database, published)
        raise


def verify_pinned_binary(path: Path) -> None:
    """Fail unless ``path`` is the exact tested Linux x64 binary."""

    source = _absolute(path)
    try:
        info = source.lstat()
    except OSError as exc:
        raise SessionMigrateError("cannot inspect the Devin CLI binary") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise SessionMigrateError("Devin CLI binary is not a regular non-symlink file")
    if info.st_size != PINNED_DEVIN_LINUX_X64_BYTES:
        raise SessionMigrateError("Devin CLI binary size does not match the pinned build")
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != PINNED_DEVIN_LINUX_X64_SHA256:
        raise SessionMigrateError("Devin CLI binary digest does not match the pinned build")


def _decode_bundle(data: bytes) -> dict[str, Any]:
    if len(data) > MAX_BUNDLE_BYTES:
        raise SessionMigrateError("Devin bundle exceeds the native artifact safety limit")
    try:
        value = json.loads(
            data,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
        _validate_json_shape(value)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise SessionMigrateError("Devin bundle is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise SessionMigrateError("Devin bundle root is not a JSON object")
    return value


def _validate_session_row(value: dict[str, Any], expected_id: str) -> None:
    if value.get("id") != expected_id:
        raise SessionMigrateError("Devin bundle session ID does not match the target ID")
    required_text = ("working_directory", "backend_type", "model", "agent_mode")
    if any(not string(value.get(field)) for field in required_text):
        raise SessionMigrateError("Devin bundle session metadata is incomplete")
    if not Path(value["working_directory"]).is_absolute():
        raise SessionMigrateError("Devin bundle working directory is not absolute")
    if value.get("backend_type") != "Windsurf" or value.get("agent_mode") != "auto":
        raise SessionMigrateError("Devin bundle runtime selector is unsupported")
    if not _non_negative_int(value.get("created_at")) or not _non_negative_int(
        value.get("last_activity_at")
    ):
        raise SessionMigrateError("Devin bundle timestamps are invalid")
    if value["last_activity_at"] < value["created_at"]:
        raise SessionMigrateError("Devin bundle session timestamps are out of order")
    if value.get("title") is not None and not isinstance(value.get("title"), str):
        raise SessionMigrateError("Devin bundle title is invalid")
    if not _non_negative_int(value.get("main_chain_id")) or value["main_chain_id"] == 0:
        raise SessionMigrateError("Devin bundle main-chain tip is invalid")
    if value.get("shell_last_seen_index") != 0 or value.get("hidden") != 0:
        raise SessionMigrateError("Devin bundle visibility metadata is invalid")
    if value.get("cogs_json") is not None:
        raise SessionMigrateError("Devin bundle must not synthesize runtime cogs")
    for field in ("workspace_dirs", "metadata"):
        raw = value.get(field)
        if not isinstance(raw, str):
            raise SessionMigrateError(f"Devin bundle {field} is invalid")
        try:
            decoded = json.loads(raw, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, ValueError) as exc:
            raise SessionMigrateError(f"Devin bundle {field} is not valid JSON") from exc
        if field == "workspace_dirs" and decoded != [value["working_directory"]]:
            raise SessionMigrateError("Devin bundle workspace list does not match its cwd")


def _validate_generated_nodes(nodes: list[Any], session_row: dict[str, Any]) -> None:
    expected_parent: int | None = None
    total_bytes = 0
    has_user = False
    seen_calls: set[str] = set()
    seen_results: set[str] = set()
    for index, node in enumerate(nodes, start=1):
        if not isinstance(node, dict):
            raise SessionMigrateError("Devin bundle contains a non-object message node")
        if node.get("node_id") != index or node.get("parent_node_id") != expected_parent:
            raise SessionMigrateError("Devin bundle message chain is not contiguous")
        if not _non_negative_int(node.get("created_at")):
            raise SessionMigrateError("Devin bundle message timestamp is invalid")
        if node.get("metadata") is not None:
            raise SessionMigrateError("Devin bundle must not synthesize node metadata")
        message = node.get("chat_message")
        if not isinstance(message, dict):
            raise SessionMigrateError("Devin bundle message node has no chat message")
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode()
        total_bytes += len(encoded)
        if len(encoded) > MAX_MESSAGE_BYTES or total_bytes > MAX_CHAIN_BYTES:
            raise SessionMigrateError("Devin bundle message history exceeds the safety limit")
        role = _validate_chat_message(message)
        has_user = has_user or role == "user"
        if role == "assistant":
            for call in message.get("tool_calls", []):
                call_id = call["id"]
                if call_id in seen_calls:
                    raise SessionMigrateError("Devin bundle repeats a tool-call ID")
                seen_calls.add(call_id)
        elif role == "tool":
            call_id = message["tool_call_id"]
            if call_id not in seen_calls:
                raise SessionMigrateError("Devin bundle contains an orphan tool result")
            if call_id in seen_results:
                raise SessionMigrateError("Devin bundle repeats a tool-result ID")
            seen_results.add(call_id)
        expected_parent = index
    if not has_user:
        raise SessionMigrateError("Devin bundle has no resumable user context")
    if session_row["main_chain_id"] != len(nodes):
        raise SessionMigrateError("Devin bundle main-chain tip does not match its nodes")
    if session_row["created_at"] != nodes[0]["created_at"]:
        raise SessionMigrateError("Devin bundle creation time does not match its first node")
    if session_row["last_activity_at"] != nodes[-1]["created_at"]:
        raise SessionMigrateError("Devin bundle activity time does not match its tip")


def _validate_chat_message(message: dict[str, Any]) -> str:
    role = string(message.get("role"))
    if role not in {"system", "user", "assistant", "tool"}:
        raise SessionMigrateError("Devin chat message has an unsupported role")
    if not string(message.get("message_id")):
        raise SessionMigrateError("Devin chat message is missing its message ID")
    content = message.get("content")
    if not isinstance(content, (str, list)):
        raise SessionMigrateError("Devin chat message has invalid content")
    _message_text(content)
    if role == "assistant":
        thinking = message.get("thinking")
        if thinking is not None and (
            not isinstance(thinking, dict) or not string(thinking.get("thinking"))
        ):
            raise SessionMigrateError("Devin assistant thinking is invalid")
        calls = message.get("tool_calls")
        if not isinstance(calls, list):
            raise SessionMigrateError("Devin assistant message has invalid tool calls")
        for call in calls:
            if (
                not isinstance(call, dict)
                or not string(call.get("id"))
                or not string(call.get("name"))
                or call.get("kind") != "function"
                or not isinstance(call.get("arguments"), dict)
                or not _non_negative_int(call.get("index"))
            ):
                raise SessionMigrateError("Devin assistant message has an invalid tool call")
    if role == "tool" and not string(message.get("tool_call_id")):
        raise SessionMigrateError("Devin tool result is missing its call ID")
    images = message.get("images")
    if images is not None:
        if not isinstance(images, list) or len(images) > MAX_CHAIN_NODES:
            raise SessionMigrateError("Devin chat message has invalid images")
        for image in images:
            _validate_image(image)
    return role


def _message_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise SessionMigrateError("Devin message content has an unsupported shape")
    parts: list[str] = []
    for part in value:
        if not isinstance(part, dict):
            raise SessionMigrateError("Devin message content contains an invalid part")
        part_type = part.get("type")
        if part_type == "text" and isinstance(part.get("text"), str):
            parts.append(part["text"])
        elif part_type not in {"image", "image_url"}:
            raise SessionMigrateError("Devin message content contains an unknown part type")
    return "\n".join(parts)


def _validate_image(value: Any) -> None:
    if not isinstance(value, dict) or not isinstance(value.get("base64_data"), str):
        raise SessionMigrateError("Devin message image is invalid")
    encoded = value["base64_data"]
    if len(encoded) > (MAX_IMAGE_BYTES * 4 // 3 + 4):
        raise SessionMigrateError("Devin message image exceeds the safety limit")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise SessionMigrateError("Devin message image is not valid base64") from exc
    if len(decoded) > MAX_IMAGE_BYTES:
        raise SessionMigrateError("Devin message image exceeds the safety limit")
    media_type = value.get("media_type") or value.get("mime_type")
    if media_type is not None and media_type not in {
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/webp",
    }:
        raise SessionMigrateError("Devin message image has an unsupported media type")


def _user_image_events(
    message: dict[str, Any], provenance: Provenance, timestamp: str
) -> list[Event]:
    images = message.get("images")
    if not isinstance(images, list):
        return []
    events: list[Event] = []
    for block_index, image in enumerate(images):
        _validate_image(image)
        encoded = image["base64_data"]
        decoded = base64.b64decode(encoded, validate=True)
        media_type = image.get("media_type") or image.get("mime_type") or _sniff_image(decoded)
        if media_type is None:
            events.append(_opaque_event(provenance, timestamp, "devin_unknown_image_type"))
            continue
        events.append(
            Event(
                EventKind.CONTEXT,
                Provenance(
                    provenance.record_index,
                    provenance.record_type,
                    provenance.source_id,
                    block_index,
                ),
                role=Role.USER,
                timestamp=timestamp,
                payload={
                    "block_type": "image",
                    "image_url": f"data:{media_type};base64,{encoded}",
                },
            )
        )
    return events


def _sniff_image(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


@contextmanager
def _read_database(path: Path) -> Iterator[sqlite3.Connection]:
    _validate_database_file(path)
    uri = f"file:{quote(str(path), safe='/')}?mode=ro"
    try:
        db = sqlite3.connect(uri, uri=True, timeout=2)
    except sqlite3.Error as exc:
        raise SessionMigrateError("cannot open the Devin session database") from exc
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("BEGIN")
        _validate_schema(db)
        yield db
    except sqlite3.Error as exc:
        raise SessionMigrateError("cannot read the Devin session database") from exc
    finally:
        with suppress(sqlite3.Error):
            db.rollback()
        db.close()


def _validate_database_file(path: Path) -> None:
    _check_safe_existing_prefix(path.parent)
    try:
        info = path.lstat()
    except OSError as exc:
        raise SessionMigrateError("cannot inspect the Devin session database") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise SessionMigrateError("Devin session database is not a regular non-symlink file")
    if info.st_size > MAX_DATABASE_BYTES:
        raise SessionMigrateError("Devin session database exceeds the safety limit")


def _validate_schema(db: sqlite3.Connection) -> None:
    for table, expected in _TABLE_COLUMNS.items():
        rows = db.execute(f'PRAGMA table_info("{table}")').fetchall()
        if tuple(row[1] for row in rows) != expected:
            raise SessionMigrateError("Devin session database schema does not match 3000.6.7")
    latest = db.execute(
        "SELECT version, name, checksum FROM refinery_schema_history ORDER BY version"
    ).fetchall()
    if tuple((row[0], row[1], str(row[2])) for row in latest) != _MIGRATIONS:
        raise SessionMigrateError("Devin session database migrations do not match 3000.6.7")


def _only_visible_id(db: sqlite3.Connection) -> str:
    rows = db.execute("SELECT id FROM sessions WHERE hidden = 0 ORDER BY id LIMIT 2").fetchall()
    if not rows:
        raise SessionMigrateError("Devin session database has no visible sessions")
    if len(rows) != 1:
        raise SessionMigrateError("Devin shared database requires an explicit session ID")
    return normalized_session_id(_required_text(rows[0][0], "Devin session ID"))


def _active_chain_count(db: sqlite3.Connection, session_id: str, tip: Any) -> int:
    if not _non_negative_int(tip) or tip == 0:
        return 0
    rows = db.execute(
        """
        WITH RECURSIVE chain(node_id, parent_node_id, depth) AS (
          SELECT node_id, parent_node_id, 0 FROM message_nodes
          WHERE session_id = ?1 AND node_id = ?2
          UNION ALL
          SELECT m.node_id, m.parent_node_id, c.depth + 1 FROM chain c
          JOIN message_nodes m ON m.session_id = ?1 AND m.node_id = c.parent_node_id
          WHERE c.depth < ?3
        )
        SELECT node_id, parent_node_id FROM chain
        """,
        (session_id, tip, MAX_CHAIN_NODES),
    ).fetchall()
    if not rows:
        raise SessionMigrateError("Devin main-chain tip does not exist")
    seen: set[int] = set()
    for node_id, _parent in rows:
        if node_id in seen:
            raise SessionMigrateError("Devin message forest contains a parent cycle")
        seen.add(node_id)
    if len(rows) > MAX_CHAIN_NODES or rows[-1][1] is not None:
        raise SessionMigrateError("Devin main chain exceeds the safety limit or is incomplete")
    return len(rows)


def _read_native_session(db: sqlite3.Connection, session_id: str) -> _NativeSession:
    row = db.execute(
        """
        SELECT working_directory, model, created_at, last_activity_at, title,
               main_chain_id, hidden
        FROM sessions WHERE id = ?
        """,
        (session_id,),
    ).fetchone()
    if row is None:
        raise SessionMigrateError("Devin session ID is not present in the selected store")
    if row[6] != 0:
        raise SessionMigrateError("Devin session is hidden and cannot be migrated")
    cwd = _required_text(row[0], "Devin working directory")
    if not Path(cwd).is_absolute():
        raise SessionMigrateError("Devin working directory is not absolute")
    model = _required_text(row[1], "Devin model")
    created = _required_epoch(row[2], "Devin created_at")
    updated = _required_epoch(row[3], "Devin last_activity_at")
    if updated < created:
        raise SessionMigrateError("Devin session timestamps are out of order")
    tip = row[5]
    if not _non_negative_int(tip) or tip == 0:
        raise SessionMigrateError("Devin session has no active message chain")
    rows = db.execute(
        """
        WITH RECURSIVE chain(node_id, parent_node_id, chat_message, created_at, depth) AS (
          SELECT node_id, parent_node_id, chat_message, created_at, 0
          FROM message_nodes WHERE session_id = ?1 AND node_id = ?2
          UNION ALL
          SELECT m.node_id, m.parent_node_id, m.chat_message, m.created_at, c.depth + 1
          FROM chain c
          JOIN message_nodes m ON m.session_id = ?1 AND m.node_id = c.parent_node_id
          WHERE c.depth < ?3
        )
        SELECT node_id, parent_node_id, chat_message, created_at, depth
        FROM chain ORDER BY depth DESC
        """,
        (session_id, tip, MAX_CHAIN_NODES),
    ).fetchall()
    if not rows:
        raise SessionMigrateError("Devin main-chain tip does not exist")
    if len(rows) > MAX_CHAIN_NODES or rows[0][1] is not None:
        raise SessionMigrateError("Devin main chain exceeds the safety limit or is incomplete")
    seen: set[int] = set()
    expected_parent: int | None = None
    decoded: list[tuple[int, int | None, dict[str, Any], int]] = []
    snapshot_nodes: list[dict[str, Any]] = []
    total_bytes = 0
    for node_id, parent_id, raw_message, node_created, _depth in rows:
        if not _non_negative_int(node_id) or node_id == 0 or node_id in seen:
            raise SessionMigrateError("Devin main chain contains an invalid or repeated node")
        if parent_id != expected_parent:
            raise SessionMigrateError("Devin main chain has a broken parent link")
        if not isinstance(raw_message, str):
            raise SessionMigrateError("Devin message node has invalid JSON storage")
        encoded = raw_message.encode()
        total_bytes += len(encoded)
        if len(encoded) > MAX_MESSAGE_BYTES or total_bytes > MAX_CHAIN_BYTES:
            raise SessionMigrateError("Devin main chain exceeds the message safety limit")
        try:
            message = json.loads(
                raw_message,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_json_constant,
            )
            _validate_json_shape(message)
        except (json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise SessionMigrateError("Devin message node is not valid JSON") from exc
        if not isinstance(message, dict):
            raise SessionMigrateError("Devin message node is not a JSON object")
        _validate_chat_message(message)
        node_timestamp = _required_epoch(node_created, "Devin message timestamp")
        decoded.append((node_id, parent_id, message, node_timestamp))
        snapshot_nodes.append(
            {
                "node_id": node_id,
                "parent_node_id": parent_id,
                "chat_message": message,
                "created_at": node_timestamp,
            }
        )
        seen.add(node_id)
        expected_parent = node_id
    if expected_parent != tip:
        raise SessionMigrateError("Devin main-chain walk did not reach its declared tip")
    snapshot = {
        "session": {
            "id": session_id,
            "working_directory": cwd,
            "model": model,
            "created_at": created,
            "last_activity_at": updated,
            "title": row[4],
            "main_chain_id": tip,
        },
        "nodes": snapshot_nodes,
    }
    snapshot_bytes = json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return _NativeSession(
        session_id=session_id,
        cwd=Path(cwd),
        model=model,
        title=string(row[4]),
        created_at=created,
        last_activity_at=updated,
        nodes=tuple(decoded),
        snapshot_sha256=hashlib.sha256(snapshot_bytes).hexdigest(),
    )


def _insert_bundle(path: Path, parsed: ParsedDevinBundle) -> None:
    try:
        db = sqlite3.connect(path, timeout=5)
    except sqlite3.Error as exc:
        raise SessionMigrateError("cannot open the Devin target database") from exc
    try:
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        _validate_schema(db)
        session = parsed.session
        if db.execute("SELECT 1 FROM sessions WHERE id = ?", (session["id"],)).fetchone():
            raise SessionMigrateError("refusing to overwrite an existing Devin session")
        db.execute(
            """
            INSERT INTO sessions(
              id, working_directory, backend_type, model, agent_mode, created_at,
              last_activity_at, title, main_chain_id, shell_last_seen_index,
              cogs_json, workspace_dirs, hidden, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(session[column] for column in _TABLE_COLUMNS["sessions"]),
        )
        db.executemany(
            """
            INSERT INTO message_nodes(
              session_id, node_id, parent_node_id, chat_message, created_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    session["id"],
                    node["node_id"],
                    node["parent_node_id"],
                    json.dumps(
                        node["chat_message"],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    node["created_at"],
                    node["metadata"],
                )
                for node in parsed.nodes
            ],
        )
        db.executemany(
            """
            INSERT INTO prompt_history(content, timestamp, session_id, is_shell)
            VALUES(?, ?, ?, ?)
            """,
            [
                (prompt["content"], prompt["timestamp"], session["id"], prompt["is_shell"])
                for prompt in parsed.prompt_history
            ],
        )
        db.executemany(
            """
            INSERT INTO tool_call_state(
              session_id, tool_call_id, tool_call_json, tool_call_update_json
            ) VALUES(?, ?, ?, ?)
            """,
            [
                (
                    session["id"],
                    state["tool_call_id"],
                    state.get("tool_call_json"),
                    state.get("tool_call_update_json"),
                )
                for state in parsed.tool_call_state
            ],
        )
        db.commit()
    except SessionMigrateError:
        db.rollback()
        raise
    except sqlite3.Error as exc:
        db.rollback()
        raise SessionMigrateError("cannot install the Devin session transaction") from exc
    finally:
        db.close()


def _create_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE refinery_schema_history(
          version int4 PRIMARY KEY, name VARCHAR(255), applied_on VARCHAR(255),
          checksum VARCHAR(255));
        CREATE TABLE sessions (
          id TEXT PRIMARY KEY, working_directory TEXT NOT NULL, backend_type TEXT NOT NULL,
          model TEXT NOT NULL, agent_mode TEXT NOT NULL, created_at INTEGER NOT NULL,
          last_activity_at INTEGER NOT NULL, title TEXT, main_chain_id INTEGER,
          shell_last_seen_index INTEGER DEFAULT 0, cogs_json TEXT, workspace_dirs TEXT,
          hidden INTEGER NOT NULL DEFAULT 0, metadata TEXT);
        CREATE INDEX idx_sessions_activity ON sessions(last_activity_at DESC);
        CREATE INDEX idx_sessions_hidden ON sessions(hidden);
        CREATE TABLE prompt_history (
          id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL,
          timestamp INTEGER NOT NULL, session_id TEXT NOT NULL,
          is_shell INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX idx_prompt_history_timestamp ON prompt_history(timestamp DESC);
        CREATE INDEX idx_prompt_history_session ON prompt_history(session_id);
        CREATE TABLE message_nodes (
          row_id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
          node_id INTEGER NOT NULL, parent_node_id INTEGER, chat_message TEXT NOT NULL,
          created_at INTEGER NOT NULL, metadata TEXT,
          FOREIGN KEY (session_id) REFERENCES sessions(id), UNIQUE(session_id, node_id));
        CREATE INDEX idx_message_nodes_session ON message_nodes(session_id);
        CREATE TABLE rendered_commits (
          id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
          sequence_number INTEGER NOT NULL, rendered_html TEXT NOT NULL,
          created_at INTEGER NOT NULL, FOREIGN KEY (session_id) REFERENCES sessions(id),
          UNIQUE(session_id, sequence_number));
        CREATE INDEX idx_rendered_commits_session ON rendered_commits(session_id, sequence_number);
        CREATE TABLE app_state (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL);
        CREATE TABLE tool_call_state (
          session_id TEXT NOT NULL, tool_call_id TEXT NOT NULL, tool_call_json TEXT,
          tool_call_update_json TEXT, PRIMARY KEY (session_id, tool_call_id),
          FOREIGN KEY (session_id) REFERENCES sessions(id));
        """
    )
    applied = _utc_now().replace("Z", "000Z")
    db.executemany(
        """
        INSERT INTO refinery_schema_history(version, name, applied_on, checksum)
        VALUES(?, ?, ?, ?)
        """,
        [(version, name, applied, checksum) for version, name, checksum in _MIGRATIONS],
    )
    db.commit()


def _opaque_event(provenance: Provenance, timestamp: str, reason: str) -> Event:
    return Event(
        EventKind.OPAQUE,
        provenance,
        timestamp=timestamp,
        payload={"reason": reason},
    )


def _required_text(value: Any, description: str) -> str:
    result = string(value)
    if not result:
        raise SessionMigrateError(f"{description} is missing")
    return result


def _required_epoch(value: Any, description: str) -> int:
    if not _non_negative_int(value):
        raise SessionMigrateError(f"{description} is invalid")
    return value


def _non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _epoch_seconds(value: str | None) -> int:
    timestamp = valid_rfc3339(value)
    if not timestamp:
        return int(datetime.now(UTC).timestamp())
    return int(datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp())


def _timestamp_text(value: int) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _omission_key(event: Event) -> str:
    if event.kind == EventKind.MESSAGE:
        return f"message:{event.role.value if event.role else 'unknown'}"
    return event.kind.value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _validate_json_shape(value: Any) -> None:
    stack: list[tuple[Any, int]] = [(value, 1)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if depth > MAX_JSON_DEPTH or nodes > MAX_JSON_NODES:
            raise ValueError("JSON structure exceeds safety limits")
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


def _ensure_private_directory(path: Path) -> None:
    missing: list[Path] = []
    cursor = path
    while not os.path.lexists(cursor):
        missing.append(cursor)
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    if os.path.lexists(cursor):
        info = cursor.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise SessionMigrateError("Devin target root has an unsafe existing prefix")
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            info = directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise SessionMigrateError(
                    "Devin target root has an unsafe existing prefix"
                ) from None


def _check_safe_existing_prefix(path: Path) -> None:
    cursor = path
    while not os.path.lexists(cursor):
        if cursor.parent == cursor:
            return
        cursor = cursor.parent
    info = cursor.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise SessionMigrateError("Devin target root has an unsafe existing prefix")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _unlink_if_same_file(path: Path, identity: tuple[int, int]) -> None:
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == identity:
        path.unlink()
        _fsync_directory(path.parent)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
