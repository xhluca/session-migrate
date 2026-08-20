"""OpenCode 1.17.20 import/export bundle adapter.

The writer emits the public JSON shape consumed by ``opencode import``.  It
never writes OpenCode's SQLite database directly.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from collections import Counter, defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from session_migrate.errors import SessionMigrateError
from session_migrate.formats.common import portable_data_image, string, valid_rfc3339
from session_migrate.jsonl import file_sha256
from session_migrate.model import AgentFormat, Event, EventKind, Provenance, Role, Session

PINNED_OPENCODE_VERSION = "1.17.20"
OPENCODE_NATIVE_IMPORT_SUPPORTED = True
OPENCODE_IMPORT_MEDIA_TYPES = {"image/gif", "image/jpeg", "image/png", "image/webp"}
MAX_NATIVE_BYTES = 256 * 1024 * 1024
MAX_NATIVE_MESSAGES = 1_000_000
MAX_NATIVE_PARTS = 4_000_000
MAX_JSON_DEPTH = 128
_NATIVE_RECORD_ID = re.compile(r"(?:msg|prt)_[0-9a-f]{12}[0-9A-Za-z]{14}")


@dataclass(frozen=True, slots=True)
class ParsedOpenCodeSession:
    """Content projection returned by :func:`parse_import` for verification."""

    session_id: str
    cwd: Path
    started_at: str
    title: str
    events: tuple[Event, ...]
    raw_record_count: int
    cli_version: str | None = None
    model: str | None = None
    provider: str | None = None
    parent_session: str | None = None
    losses: tuple[tuple[str, int], ...] = ()


def session_id_from_uuid(value: str) -> str:
    """Create a valid OpenCode session ID from a UUID string."""

    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise SessionMigrateError("OpenCode target session ID is not a valid UUID") from exc
    return f"ses_{parsed.hex}"


def serialize(
    session: Session,
    *,
    session_id: str,
    cwd: Path,
    cli_version: str = PINNED_OPENCODE_VERSION,
    provider_id: str = "anthropic",
    model_id: str | None = None,
    agent: str = "build",
    timestamp: str | None = None,
    title: str | None = None,
    slug: str | None = None,
) -> tuple[bytes, dict[str, int]]:
    """Serialize portable events as an official OpenCode import bundle."""

    if not session_id.startswith("ses_"):
        raise SessionMigrateError("OpenCode session IDs must start with 'ses_'")
    fallback_timestamp = valid_rfc3339(timestamp) or valid_rfc3339(session.started_at) or _utc_now()
    fallback_ms = _timestamp_ms(fallback_timestamp)
    target_model = model_id or session.model or "unknown"
    dropped: Counter[str] = Counter()
    messages: list[dict[str, Any]] = []
    latest_user_id: str | None = None
    latest_message_id: str | None = None
    generated_tool_ids: deque[str] = deque()
    seen_tool_call_ids: set[str] = set()
    seen_tool_result_ids: set[str] = set()
    tool_parts: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
    portable_boundary = 0

    pending_role: Role | None = None
    pending_source_record: int | None = None
    pending_timestamp: str | None = None
    pending_parts: list[dict[str, Any]] = []
    last_native_encoded = -1
    last_message_created_ms = -1

    def new_native_id(prefix: str, message_timestamp: str | int) -> str:
        nonlocal last_native_encoded
        requested_ms = (
            message_timestamp
            if isinstance(message_timestamp, int)
            else _timestamp_ms(message_timestamp)
        )
        # Match OpenCode's 48-bit ``timestamp * 0x1000 + counter`` field while
        # carrying overflow monotonically. The pinned implementation resets
        # its counter when a timestamp changes, which can move backward after
        # >4095 imported IDs share one source timestamp.
        candidate = (requested_ms * 0x1000 + 1) & ((1 << 48) - 1)
        encoded = max(candidate, last_native_encoded + 1)
        if encoded >= 1 << 48:
            raise SessionMigrateError("OpenCode timestamp exceeds its native ID range")
        last_native_encoded = encoded
        time_hex = encoded.to_bytes(6, "big").hex()
        alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
        random_suffix = "".join(alphabet[value % 62] for value in uuid.uuid4().bytes[:14])
        return f"{prefix}_{time_hex}{random_suffix}"

    def new_part_id(message_timestamp: str) -> str:
        return new_native_id("prt", message_timestamp)

    def ordered_message_ms(message_timestamp: str) -> int:
        nonlocal last_message_created_ms
        requested_ms = _timestamp_ms(message_timestamp)
        ordered_ms = max(requested_ms, last_message_created_ms)
        if ordered_ms != requested_ms:
            dropped["timestamp:native_order_adjusted"] += 1
        last_message_created_ms = ordered_ms
        return ordered_ms

    def append_user(parts: list[dict[str, Any]], message_timestamp: str) -> str:
        nonlocal latest_user_id, latest_message_id
        created_ms = ordered_message_ms(message_timestamp)
        message_id = new_native_id("msg", created_ms)
        for part in parts:
            part.update(
                {
                    "id": part.get("id") or new_native_id("prt", created_ms),
                    "sessionID": session_id,
                    "messageID": message_id,
                }
            )
        messages.append(
            {
                "info": {
                    "id": message_id,
                    "sessionID": session_id,
                    "role": "user",
                    "time": {"created": created_ms},
                    "agent": agent,
                    "model": {"providerID": provider_id, "modelID": target_model},
                },
                "parts": parts,
            }
        )
        latest_user_id = message_id
        latest_message_id = message_id
        return message_id

    def append_assistant(
        parts: list[dict[str, Any]], message_timestamp: str, *, summary: bool = False
    ) -> str:
        nonlocal latest_message_id
        created_ms = ordered_message_ms(message_timestamp)
        message_id = new_native_id("msg", created_ms)
        for part in parts:
            part.update(
                {
                    "id": part.get("id") or new_native_id("prt", created_ms),
                    "sessionID": session_id,
                    "messageID": message_id,
                }
            )
        parent_id = latest_user_id or latest_message_id
        if parent_id is None:
            parent_id = f"msg_import_root_{uuid.uuid4().hex}"
            dropped["assistant:missing_parent"] += 1
        info: dict[str, Any] = {
            "id": message_id,
            "sessionID": session_id,
            "role": "assistant",
            "time": {
                "created": created_ms,
                "completed": created_ms,
            },
            "parentID": parent_id,
            "modelID": target_model,
            "providerID": provider_id,
            "mode": agent,
            "agent": agent,
            "path": {"cwd": str(cwd), "root": str(cwd)},
            "cost": 0,
            "tokens": {
                "input": 0,
                "output": 0,
                "reasoning": 0,
                "cache": {"read": 0, "write": 0},
            },
            "finish": (
                "tool-calls" if any(part.get("type") == "tool" for part in parts) else "stop"
            ),
        }
        if summary:
            info["summary"] = True
        messages.append({"info": info, "parts": parts})
        latest_message_id = message_id
        return message_id

    def flush_message() -> None:
        nonlocal pending_role, pending_source_record, pending_timestamp, pending_parts
        if pending_role is None or not pending_parts:
            pending_role = None
            pending_source_record = None
            pending_timestamp = None
            pending_parts = []
            return
        message_timestamp = pending_timestamp or fallback_timestamp
        if pending_role == Role.USER:
            append_user(pending_parts, message_timestamp)
        else:
            append_assistant(pending_parts, message_timestamp)
        pending_role = None
        pending_source_record = None
        pending_timestamp = None
        pending_parts = []

    def queue_part(event: Event, role: Role, part: dict[str, Any]) -> None:
        nonlocal pending_role, pending_source_record, pending_timestamp
        source_record = event.provenance.record_index
        if pending_role is not None and (
            pending_role != role or pending_source_record != source_record
        ):
            flush_message()
        pending_role = role
        pending_source_record = source_record
        event_timestamp = _event_timestamp(event, fallback_timestamp, dropped)
        pending_timestamp = pending_timestamp or event_timestamp
        pending_parts.append(part)

    for event in session.events:
        if (
            event.kind == EventKind.MESSAGE
            and event.text
            and event.role
            in {
                Role.USER,
                Role.ASSISTANT,
            }
        ):
            portable_boundary += 1
            if event.payload.get("ui_only_projection") is True:
                dropped["message:ui_only_projection"] += 1
            queue_part(event, event.role, {"type": "text", "text": event.text})
            continue

        if event.kind == EventKind.TOOL_CALL:
            call_id = event.tool_call_id
            if not call_id:
                call_id = f"call_session_migrate_{uuid.uuid4().hex}"
                generated_tool_ids.append(call_id)
                dropped["tool_call:missing_id"] += 1
            tool_name = event.tool_name
            if not tool_name:
                tool_name = "unknown_tool"
                dropped["tool_call:missing_name"] += 1
            arguments = event.payload.get("input", {})
            if not isinstance(arguments, dict):
                arguments = {"input": arguments}
                dropped["tool_call:non_object_input"] += 1
            if call_id in seen_tool_call_ids:
                dropped["tool_call:duplicate_id"] += 1
            seen_tool_call_ids.add(call_id)
            if event.payload.get("namespace"):
                dropped["tool_call:namespace"] += 1
            event_timestamp = _event_timestamp(event, fallback_timestamp, dropped)
            part = {
                "type": "tool",
                "callID": call_id,
                "tool": tool_name,
                "state": {
                    "status": "pending",
                    "input": arguments,
                    "raw": json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
                },
                "_migration_timestamp": event_timestamp,
                "_migration_boundary": portable_boundary,
            }
            tool_parts[call_id].append(part)
            queue_part(event, Role.ASSISTANT, part)
            continue

        if event.kind == EventKind.TOOL_RESULT:
            flush_message()
            source_call_id = event.tool_call_id
            call_id = source_call_id
            if not call_id:
                call_id = (
                    generated_tool_ids.popleft()
                    if generated_tool_ids
                    else f"call_missing_{uuid.uuid4().hex}"
                )
                dropped["tool_result:missing_id"] += 1
            if source_call_id and source_call_id in seen_tool_result_ids:
                dropped["tool_result:duplicate_id"] += 1
            if source_call_id:
                seen_tool_result_ids.add(source_call_id)
            part = tool_parts[call_id].popleft() if tool_parts[call_id] else None
            if part is None:
                dropped["tool_result:orphan_id"] += 1
                call_id = f"call_session_migrate_orphan_{uuid.uuid4().hex}"
                part = {
                    "type": "tool",
                    "callID": call_id,
                    "tool": event.tool_name or "unknown_tool",
                    "state": {
                        "status": "pending",
                        "input": {},
                        "raw": "{}",
                    },
                    "_migration_timestamp": _event_timestamp(event, fallback_timestamp, dropped),
                    "_migration_boundary": portable_boundary,
                }
                append_assistant(
                    [part],
                    str(part["_migration_timestamp"]),
                )
            result_timestamp = _event_timestamp(event, fallback_timestamp, dropped)
            result_text, attachments = _tool_result(
                event,
                session_id,
                part,
                dropped,
                attachment_id=lambda timestamp=result_timestamp: new_part_id(timestamp),
            )
            start_timestamp = str(part.pop("_migration_timestamp", result_timestamp))
            if int(part.pop("_migration_boundary", portable_boundary)) < portable_boundary:
                dropped["tool_result:native_order_associated"] += 1
            if event.payload.get("is_error") is True:
                part["state"] = {
                    "status": "error",
                    "input": part["state"].get("input", {}),
                    "error": result_text,
                    "time": {
                        "start": _timestamp_ms(start_timestamp),
                        "end": _timestamp_ms(result_timestamp),
                    },
                }
            else:
                part["state"] = {
                    "status": "completed",
                    "input": part["state"].get("input", {}),
                    "output": result_text,
                    "title": str(part.get("tool") or "unknown_tool"),
                    "metadata": {},
                    "time": {
                        "start": _timestamp_ms(start_timestamp),
                        "end": _timestamp_ms(result_timestamp),
                    },
                }
                if attachments:
                    part["state"]["attachments"] = attachments
            continue

        if (
            event.kind == EventKind.CONTEXT
            and event.payload.get("block_type") == "image"
            and event.role == Role.USER
        ):
            part = _file_part(event.payload.get("image_url"), dropped, "context:image")
            if part:
                portable_boundary += 1
                queue_part(event, Role.USER, part)
            continue

        if event.kind == EventKind.COMPACTION and event.text:
            portable_boundary += 1
            flush_message()
            event_timestamp = _event_timestamp(event, fallback_timestamp, dropped)
            append_user([{"type": "compaction", "auto": True}], event_timestamp)
            append_assistant(
                [{"type": "text", "text": event.text}],
                event_timestamp,
                summary=True,
            )
            if event.payload.get("has_boundary_metadata") is True:
                dropped["compaction:boundary_metadata"] += 1
            continue

        dropped[_omission_key(event)] += 1

    flush_message()
    for queues in tool_parts.values():
        for part in queues:
            part.pop("_migration_timestamp", None)
            part.pop("_migration_boundary", None)

    updated_ms = max(
        (
            int(message["info"]["time"]["created"])
            for message in messages
            if isinstance(message.get("info"), dict)
        ),
        default=fallback_ms,
    )
    export_data = {
        "info": {
            "id": session_id,
            "slug": slug or f"session-migrate-{session_id.removeprefix('ses_')[:12]}",
            "projectID": "global",
            "directory": str(cwd),
            "title": title or session.title or "Imported session",
            "version": cli_version,
            "time": {"created": fallback_ms, "updated": updated_ms},
        },
        "messages": messages,
    }
    data = (json.dumps(export_data, ensure_ascii=False, indent=2) + "\n").encode()
    return data, dict(sorted(dropped.items()))


serialize_import = serialize


def parse_import(path: Path) -> ParsedOpenCodeSession:
    """Parse an official OpenCode import/export bundle into portable events.

    The pinned CLI stores sessions in SQLite, but its public ``export`` command
    emits this complete, versioned shape.  Reading that bundle keeps the
    database an OpenCode-owned implementation detail.
    """

    try:
        data = path.read_bytes()
    except OSError as exc:
        raise SessionMigrateError("cannot read OpenCode import bundle") from exc
    value = _decode_import_bundle(data)
    _validate_import_bundle(value)
    info = value["info"]
    session_id = string(info.get("id"))
    cwd = string(info.get("directory"))
    title = string(info.get("title"))
    time = info.get("time") if isinstance(info.get("time"), dict) else {}
    created = time.get("created")
    messages = value.get("messages")
    assert isinstance(messages, list)
    events: list[Event] = []
    part_count = 0
    latest_model = None
    latest_provider = None

    session_metadata = {
        "opencode_parent_session": info.get("parentID"),
        "opencode_session_summary": info.get("summary"),
        "opencode_session_revert": info.get("revert"),
        "opencode_session_share": info.get("share"),
        "opencode_session_permission": info.get("permission"),
        "opencode_session_metadata": info.get("metadata"),
    }
    for reason, metadata in session_metadata.items():
        if metadata is not None:
            events.append(
                Event(
                    kind=EventKind.OPAQUE,
                    timestamp=_iso_from_ms(created),
                    payload={"reason": reason},
                    provenance=Provenance(0, "session"),
                )
            )

    compaction_parents: dict[str, dict[str, Any]] = {}
    summary_parents: set[str] = set()
    for message in messages:
        message_info = message["info"]
        message_id = string(message_info.get("id")) or ""
        parts = message["parts"]
        compaction = next(
            (
                part
                for part in parts
                if isinstance(part, dict) and part.get("type") == "compaction"
            ),
            None,
        )
        if compaction is not None:
            compaction_parents[message_id] = compaction
        if message_info.get("role") == "assistant" and message_info.get("summary") is True:
            parent_id = string(message_info.get("parentID"))
            if parent_id:
                summary_parents.add(parent_id)

    for message_index, message in enumerate(messages):
        message_info = message["info"]
        parts = message.get("parts")
        assert isinstance(parts, list)
        role_name = string(message_info.get("role"))
        timestamp = _iso_from_ms(message_info.get("time", {}).get("created"))
        is_summary = message_info.get("summary") is True
        if role_name == "user":
            model = message_info.get("model")
            if isinstance(model, dict):
                latest_model = string(model.get("modelID")) or latest_model
                latest_provider = string(model.get("providerID")) or latest_provider
            system = string(message_info.get("system"))
            if system:
                events.append(
                    Event(
                        kind=EventKind.MESSAGE,
                        role=Role.SYSTEM,
                        text=system,
                        timestamp=timestamp,
                        payload={"source_subtype": "opencode_user_system"},
                        provenance=Provenance(message_index, role_name),
                    )
                )
            for field, reason in (
                ("summary", "opencode_user_summary_metadata"),
                ("format", "opencode_user_output_format"),
                ("tools", "opencode_user_tool_policy"),
            ):
                if message_info.get(field) is not None:
                    events.append(
                        _opaque_opencode_event(message_index, role_name, timestamp, reason)
                    )
        else:
            latest_model = string(message_info.get("modelID")) or latest_model
            latest_provider = string(message_info.get("providerID")) or latest_provider
            if message_info.get("error") is not None:
                events.append(
                    _opaque_opencode_event(
                        message_index,
                        role_name,
                        timestamp,
                        "opencode_assistant_error",
                    )
                )

        if is_summary:
            summary_text = "\n".join(
                string(part.get("text")) or ""
                for part in parts
                if isinstance(part, dict) and part.get("type") == "text"
            ).strip()
            parent_id = string(message_info.get("parentID")) or ""
            trigger = compaction_parents.get(parent_id)
            if summary_text:
                events.append(
                    Event(
                        kind=EventKind.COMPACTION,
                        role=Role.SYSTEM,
                        text=summary_text,
                        timestamp=timestamp,
                        payload={
                            "source_subtype": "opencode_compaction_summary",
                            "has_boundary_metadata": bool(
                                trigger
                                and (
                                    trigger.get("tail_start_id") is not None
                                    or trigger.get("overflow") is not None
                                )
                            ),
                        },
                        provenance=Provenance(message_index, role_name),
                    )
                )
            else:
                events.append(
                    _opaque_opencode_event(
                        message_index,
                        role_name,
                        timestamp,
                        "opencode_empty_compaction_summary",
                    )
                )

        for part_index, part in enumerate(parts):
            part_count += 1
            provenance = Provenance(message_index, role_name, block_index=part_index)
            assert isinstance(part, dict)
            part_type = string(part.get("type"))
            if part_type == "text":
                if is_summary:
                    continue
                text = string(part.get("text"))
                if part.get("ignored") is True:
                    events.append(
                        _opaque_part_event(
                            provenance, role_name, timestamp, "opencode_ignored_text"
                        )
                    )
                elif text:
                    events.append(
                        Event(
                            kind=EventKind.MESSAGE,
                            role=Role.USER if role_name == "user" else Role.ASSISTANT,
                            text=text,
                            timestamp=timestamp,
                            payload={
                                "synthetic": part.get("synthetic") is True,
                            },
                            provenance=provenance,
                        )
                    )
                else:
                    events.append(
                        _opaque_part_event(
                            provenance, role_name, timestamp, "opencode_empty_text"
                        )
                    )
            elif part_type == "file":
                image_url = _portable_file_url(part)
                if role_name == "user" and image_url:
                    events.append(
                        Event(
                            kind=EventKind.CONTEXT,
                            role=Role.USER,
                            timestamp=timestamp,
                            payload={
                                "block_type": "image",
                                "image_url": image_url,
                                "mime_type": string(part.get("mime")),
                            },
                            provenance=provenance,
                        )
                    )
                else:
                    events.append(
                        _opaque_part_event(
                            provenance,
                            role_name,
                            timestamp,
                            "opencode_nonportable_file",
                        )
                    )
                if part.get("source") is not None:
                    events.append(
                        _opaque_part_event(
                            provenance,
                            role_name,
                            timestamp,
                            "opencode_file_source_metadata",
                        )
                    )
            elif part_type == "tool" and role_name == "assistant":
                events.extend(_tool_part_events(part, timestamp, provenance))
            elif part_type == "reasoning" and role_name == "assistant":
                events.append(
                    Event(
                        kind=EventKind.THINKING,
                        role=Role.ASSISTANT,
                        text=string(part.get("text")),
                        timestamp=timestamp,
                        provenance=provenance,
                    )
                )
            elif part_type == "compaction" and role_name == "user":
                if string(message_info.get("id")) not in summary_parents:
                    events.append(
                        _opaque_part_event(
                            provenance,
                            role_name,
                            timestamp,
                            "opencode_unpaired_compaction",
                        )
                    )
            else:
                events.append(
                    _opaque_part_event(
                        provenance,
                        role_name,
                        timestamp,
                        f"opencode_{part_type or 'unknown'}_part",
                    )
                )
    losses = Counter(
        string(event.payload.get("reason")) or "opencode_opaque"
        for event in events
        if event.kind == EventKind.OPAQUE
    )
    return ParsedOpenCodeSession(
        session_id=session_id,
        cwd=Path(cwd),
        started_at=_iso_from_ms(created),
        title=title,
        events=tuple(events),
        raw_record_count=1 + len(messages) + part_count,
        cli_version=string(info.get("version")),
        model=latest_model or _session_model(info, "id"),
        provider=latest_provider or _session_model(info, "providerID"),
        parent_session=string(info.get("parentID")),
        losses=tuple(sorted(losses.items())),
    )


parse = parse_import


def parse_session(path: Path) -> Session:
    """Parse an official OpenCode export as an authoritative source session."""

    parsed = parse_import(path)
    return Session(
        source_format=AgentFormat.OPENCODE,
        source_path=path.resolve(),
        source_sha256=file_sha256(path),
        session_id=parsed.session_id,
        cwd=parsed.cwd,
        started_at=parsed.started_at,
        cli_version=parsed.cli_version,
        model=parsed.model,
        title=parsed.title,
        events=parsed.events,
        raw_record_count=parsed.raw_record_count,
        model_provider=parsed.provider,
    )


def validate_native_bytes(data: bytes, session_id: str) -> None:
    """Validate an official OpenCode import bundle before it is published."""

    _validate_import_bundle(_decode_import_bundle(data), expected_session_id=session_id)


def _decode_import_bundle(data: bytes) -> dict[str, Any]:
    if len(data) > MAX_NATIVE_BYTES:
        raise SessionMigrateError("OpenCode import bundle exceeds the safety limit")
    try:
        value = json.loads(
            data.decode("utf-8"),
            parse_constant=_reject_json_constant,
            object_pairs_hook=_unique_json_object,
        )
        _validate_json_depth(value)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise SessionMigrateError("OpenCode import bundle is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise SessionMigrateError("OpenCode import bundle is missing session info")
    return value


def _validate_import_bundle(value: dict[str, Any], expected_session_id: str | None = None) -> None:
    if not isinstance(value.get("info"), dict):
        raise SessionMigrateError("OpenCode import bundle is missing session info")
    info = value["info"]
    session_id = string(info.get("id"))
    if expected_session_id is not None and session_id != expected_session_id:
        raise SessionMigrateError("OpenCode bundle ID does not match the target ID")
    cwd = string(info.get("directory"))
    title = string(info.get("title"))
    version = string(info.get("version"))
    time = info.get("time") if isinstance(info.get("time"), dict) else {}
    created = time.get("created")
    updated = time.get("updated")
    if (
        not session_id
        or not session_id.startswith("ses_")
        or not cwd
        or "\x00" in cwd
        or not title
        or not version
    ):
        raise SessionMigrateError("OpenCode import bundle has invalid required metadata")
    if not _is_non_negative_int(created):
        raise SessionMigrateError("OpenCode import bundle has an invalid creation time")
    if not _is_non_negative_int(updated) or updated < created:
        raise SessionMigrateError("OpenCode import bundle has an invalid update time")
    messages = value.get("messages")
    if not isinstance(messages, list):
        raise SessionMigrateError("OpenCode import bundle is missing messages")
    if len(messages) > MAX_NATIVE_MESSAGES:
        raise SessionMigrateError("OpenCode import bundle contains too many messages")

    known_message_ids: set[str] = set()
    known_part_ids: set[str] = set()
    previous_message_id: str | None = None
    previous_message_created: int | None = None
    has_resumable_part = False
    part_count = 0
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("info"), dict):
            raise SessionMigrateError("OpenCode import bundle contains a malformed message")
        message_info = message["info"]
        message_id = string(message_info.get("id"))
        role = string(message_info.get("role"))
        if (
            not message_id
            or not message_id.startswith("msg_")
            or message_id in known_message_ids
            or message_info.get("sessionID") != session_id
            or role not in {"user", "assistant"}
        ):
            raise SessionMigrateError("OpenCode import bundle has invalid message metadata")
        if expected_session_id is not None and (
            not _NATIVE_RECORD_ID.fullmatch(message_id)
            or (previous_message_id is not None and message_id <= previous_message_id)
        ):
            raise SessionMigrateError("OpenCode message IDs are not native ascending IDs")
        message_created = (
            message_info.get("time", {}).get("created")
            if isinstance(message_info.get("time"), dict)
            else None
        )
        _iso_from_ms(message_created)
        if expected_session_id is not None and (
            not isinstance(message_created, int)
            or (previous_message_created is not None and message_created < previous_message_created)
        ):
            raise SessionMigrateError("OpenCode message timestamps are not ascending")
        if role == "assistant" and not string(message_info.get("parentID")):
            raise SessionMigrateError("OpenCode assistant message is missing its parent ID")
        _validate_message_info(message_info, role, message_created)
        known_message_ids.add(message_id)
        previous_message_id = message_id
        previous_message_created = message_created

        parts = message.get("parts")
        if not isinstance(parts, list):
            raise SessionMigrateError("OpenCode import bundle message is missing parts")
        part_count += len(parts)
        if part_count > MAX_NATIVE_PARTS:
            raise SessionMigrateError("OpenCode import bundle contains too many parts")
        previous_part_id: str | None = None
        for part in parts:
            if not isinstance(part, dict):
                raise SessionMigrateError("OpenCode import bundle contains a malformed part")
            part_id = string(part.get("id"))
            if (
                not part_id
                or not part_id.startswith("prt_")
                or part_id in known_part_ids
                or part.get("sessionID") != session_id
                or part.get("messageID") != message_id
                or not string(part.get("type"))
            ):
                raise SessionMigrateError("OpenCode import bundle has invalid part metadata")
            if expected_session_id is not None and (
                not _NATIVE_RECORD_ID.fullmatch(part_id)
                or (previous_part_id is not None and part_id <= previous_part_id)
            ):
                raise SessionMigrateError("OpenCode part IDs are not native ascending IDs")
            known_part_ids.add(part_id)
            previous_part_id = part_id
            _validate_part(part, session_id, message_id, known_part_ids)
            if part.get("type") in {"text", "file", "tool", "compaction"}:
                has_resumable_part = True
    if not has_resumable_part:
        raise SessionMigrateError("OpenCode import bundle has no resumable conversation context")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON member: {key}")
        result[key] = value
    return result


def _validate_json_depth(value: Any) -> None:
    pending: list[tuple[Any, int]] = [(value, 1)]
    while pending:
        current, depth = pending.pop()
        if depth > MAX_JSON_DEPTH:
            raise ValueError("OpenCode JSON nesting exceeds the safety limit")
        if isinstance(current, dict):
            pending.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            pending.extend((item, depth + 1) for item in current)


def _validate_message_info(info: dict[str, Any], role: str, created: Any) -> None:
    if role == "user":
        model = info.get("model")
        if (
            not string(info.get("agent"))
            or not isinstance(model, dict)
            or not string(model.get("providerID"))
            or not string(model.get("modelID"))
        ):
            raise SessionMigrateError("OpenCode user message has invalid runtime metadata")
        if info.get("system") is not None and not isinstance(info.get("system"), str):
            raise SessionMigrateError("OpenCode user message has invalid system metadata")
        if info.get("tools") is not None and not isinstance(info.get("tools"), dict):
            raise SessionMigrateError("OpenCode user message has invalid tool policy metadata")
        return

    time = info.get("time")
    completed = time.get("completed") if isinstance(time, dict) else None
    path = info.get("path")
    if (
        not string(info.get("parentID"))
        or not string(info.get("modelID"))
        or not string(info.get("providerID"))
        or not string(info.get("mode"))
        or not string(info.get("agent"))
        or not isinstance(path, dict)
        or not string(path.get("cwd"))
        or not string(path.get("root"))
        or not _is_finite_number(info.get("cost"))
    ):
        raise SessionMigrateError("OpenCode assistant message has invalid runtime metadata")
    if completed is not None and (
        not _is_non_negative_int(completed) or completed < created
    ):
        raise SessionMigrateError("OpenCode assistant message has invalid completion time")
    _validate_tokens(info.get("tokens"), "assistant message")
    if info.get("summary") is not None and not isinstance(info.get("summary"), bool):
        raise SessionMigrateError("OpenCode assistant message has invalid summary metadata")
    if info.get("error") is not None and not isinstance(info.get("error"), dict):
        raise SessionMigrateError("OpenCode assistant message has invalid error metadata")


def _validate_part(
    part: dict[str, Any],
    session_id: str,
    message_id: str,
    known_part_ids: set[str],
) -> None:
    part_type = string(part.get("type"))
    if part_type == "text":
        if not isinstance(part.get("text"), str):
            _invalid_part(part_type)
        _validate_optional_bool(part, "synthetic", part_type)
        _validate_optional_bool(part, "ignored", part_type)
        _validate_optional_time(part.get("time"), part_type)
        return
    if part_type == "reasoning":
        if not isinstance(part.get("text"), str):
            _invalid_part(part_type)
        _validate_required_time(part.get("time"), part_type)
        return
    if part_type == "file":
        _validate_file_part(part, session_id, message_id)
        return
    if part_type == "tool":
        if not string(part.get("callID")) or not string(part.get("tool")):
            _invalid_part(part_type)
        state = part.get("state")
        if not isinstance(state, dict) or not isinstance(state.get("input"), dict):
            _invalid_part(part_type)
        status = string(state.get("status"))
        if status == "pending":
            if not isinstance(state.get("raw"), str):
                _invalid_part(part_type)
        elif status == "running":
            _validate_required_time(state.get("time"), part_type)
        elif status == "completed":
            if (
                not isinstance(state.get("output"), str)
                or not isinstance(state.get("title"), str)
                or not isinstance(state.get("metadata"), dict)
            ):
                _invalid_part(part_type)
            _validate_required_time(state.get("time"), part_type, require_end=True)
            attachments = state.get("attachments")
            if attachments is not None:
                if not isinstance(attachments, list):
                    _invalid_part(part_type)
                for attachment in attachments:
                    if not isinstance(attachment, dict):
                        _invalid_part(part_type)
                    attachment_id = string(attachment.get("id"))
                    if (
                        not attachment_id
                        or not attachment_id.startswith("prt_")
                        or attachment_id in known_part_ids
                        or attachment.get("type") != "file"
                    ):
                        raise SessionMigrateError(
                            "OpenCode tool attachment has invalid part metadata"
                        )
                    known_part_ids.add(attachment_id)
                    _validate_file_part(attachment, session_id, message_id)
        elif status == "error":
            if not isinstance(state.get("error"), str):
                _invalid_part(part_type)
            _validate_required_time(state.get("time"), part_type, require_end=True)
        else:
            _invalid_part(part_type)
        return
    if part_type == "compaction":
        if not isinstance(part.get("auto"), bool):
            _invalid_part(part_type)
        _validate_optional_bool(part, "overflow", part_type)
        tail = part.get("tail_start_id")
        if tail is not None and (not string(tail) or not str(tail).startswith("msg_")):
            _invalid_part(part_type)
        return
    if part_type == "snapshot":
        if not isinstance(part.get("snapshot"), str):
            _invalid_part(part_type)
        return
    if part_type == "patch":
        files = part.get("files")
        if (
            not isinstance(part.get("hash"), str)
            or not isinstance(files, list)
            or not all(isinstance(item, str) for item in files)
        ):
            _invalid_part(part_type)
        return
    if part_type == "step-start":
        if part.get("snapshot") is not None and not isinstance(part.get("snapshot"), str):
            _invalid_part(part_type)
        return
    if part_type == "step-finish":
        if not isinstance(part.get("reason"), str) or not _is_finite_number(part.get("cost")):
            _invalid_part(part_type)
        _validate_tokens(part.get("tokens"), "step-finish part")
        return
    if part_type == "agent":
        if not string(part.get("name")):
            _invalid_part(part_type)
        return
    if part_type == "subtask":
        if not all(string(part.get(field)) for field in ("prompt", "description", "agent")):
            _invalid_part(part_type)
        return
    if part_type == "retry":
        if not _is_non_negative_int(part.get("attempt")) or not isinstance(
            part.get("error"), dict
        ):
            _invalid_part(part_type)
        retry_time = part.get("time")
        if not isinstance(retry_time, dict) or not _is_non_negative_int(
            retry_time.get("created")
        ):
            _invalid_part(part_type)
        return
    raise SessionMigrateError(f"OpenCode import bundle has unsupported part type: {part_type}")


def _validate_file_part(part: dict[str, Any], session_id: str, message_id: str) -> None:
    if (
        part.get("sessionID") != session_id
        or part.get("messageID") != message_id
        or not string(part.get("mime"))
        or not string(part.get("url"))
    ):
        _invalid_part("file")
    if part.get("filename") is not None and not isinstance(part.get("filename"), str):
        _invalid_part("file")
    source = part.get("source")
    if source is None:
        return
    if not isinstance(source, dict) or string(source.get("type")) not in {
        "file",
        "symbol",
        "resource",
    }:
        _invalid_part("file")
    text = source.get("text")
    if (
        not isinstance(text, dict)
        or not isinstance(text.get("value"), str)
        or not _is_finite_number(text.get("start"))
        or not _is_finite_number(text.get("end"))
    ):
        _invalid_part("file")


def _validate_tokens(value: Any, context: str) -> None:
    if not isinstance(value, dict):
        raise SessionMigrateError(f"OpenCode {context} has invalid token metadata")
    cache = value.get("cache")
    if (
        not all(_is_finite_number(value.get(field)) for field in ("input", "output", "reasoning"))
        or not isinstance(cache, dict)
        or not all(_is_finite_number(cache.get(field)) for field in ("read", "write"))
    ):
        raise SessionMigrateError(f"OpenCode {context} has invalid token metadata")


def _validate_optional_bool(part: dict[str, Any], field: str, part_type: str) -> None:
    if part.get(field) is not None and not isinstance(part.get(field), bool):
        _invalid_part(part_type)


def _validate_optional_time(value: Any, part_type: str) -> None:
    if value is not None:
        _validate_required_time(value, part_type)


def _validate_required_time(value: Any, part_type: str, *, require_end: bool = False) -> None:
    if not isinstance(value, dict) or not _is_non_negative_int(value.get("start")):
        _invalid_part(part_type)
    end = value.get("end")
    if require_end and not _is_non_negative_int(end):
        _invalid_part(part_type)
    if end is not None and (
        not _is_non_negative_int(end) or end < value["start"]
    ):
        _invalid_part(part_type)


def _invalid_part(part_type: str | None) -> None:
    raise SessionMigrateError(
        f"OpenCode import bundle has invalid {part_type or 'unknown'} part metadata"
    )


def _is_non_negative_int(value: Any) -> bool:
    return type(value) is int and value >= 0


def _is_finite_number(value: Any) -> bool:
    return type(value) in {int, float} and math.isfinite(value)


def _opaque_opencode_event(
    message_index: int,
    role_name: str | None,
    timestamp: str,
    reason: str,
) -> Event:
    return _opaque_part_event(
        Provenance(message_index, role_name), role_name, timestamp, reason
    )


def _opaque_part_event(
    provenance: Provenance,
    role_name: str | None,
    timestamp: str,
    reason: str,
) -> Event:
    return Event(
        kind=EventKind.OPAQUE,
        role=(
            Role.USER
            if role_name == "user"
            else Role.ASSISTANT
            if role_name == "assistant"
            else None
        ),
        timestamp=timestamp,
        payload={"reason": reason},
        provenance=provenance,
    )


def _portable_file_url(part: dict[str, Any]) -> str | None:
    mime = string(part.get("mime"))
    url = string(part.get("url"))
    if mime not in OPENCODE_IMPORT_MEDIA_TYPES or not url:
        return None
    if url.startswith(("https://", "http://")):
        return url
    image = portable_data_image(url)
    return url if image and image[0] == mime else None


def _session_model(info: dict[str, Any], field: str) -> str | None:
    model = info.get("model")
    return string(model.get(field)) if isinstance(model, dict) else None


def _tool_part_events(part: dict[str, Any], timestamp: str, provenance: Provenance) -> list[Event]:
    call_id = string(part.get("callID"))
    tool_name = string(part.get("tool"))
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    result = [
        Event(
            kind=EventKind.TOOL_CALL,
            role=Role.ASSISTANT,
            timestamp=timestamp,
            tool_name=tool_name,
            tool_call_id=call_id,
            payload={"input": state.get("input", {})},
            provenance=provenance,
        )
    ]
    status = string(state.get("status"))
    if status not in {"completed", "error"}:
        result.append(
            _opaque_part_event(
                provenance,
                "assistant",
                timestamp,
                f"opencode_tool_{status or 'unknown'}",
            )
        )
        return result
    content_blocks: list[dict[str, Any]] = []
    if status == "completed":
        output = state.get("output")
        if isinstance(output, str) and output:
            content_blocks.append({"type": "text", "text": output})
        attachments = state.get("attachments")
        if isinstance(attachments, list):
            for attachment in attachments:
                if isinstance(attachment, dict) and _portable_file_url(attachment):
                    content_blocks.append(
                        {
                            "type": "image",
                            "image_url": _portable_file_url(attachment),
                        }
                    )
                else:
                    result.append(
                        _opaque_part_event(
                            provenance,
                            "assistant",
                            timestamp,
                            "opencode_tool_attachment_non_image",
                        )
                    )
        text = output if isinstance(output, str) and output else None
    else:
        text = string(state.get("error"))
        if text:
            content_blocks.append({"type": "text", "text": text})
    result.append(
        Event(
            kind=EventKind.TOOL_RESULT,
            role=Role.TOOL,
            text=text,
            timestamp=timestamp,
            tool_name=tool_name,
            tool_call_id=call_id,
            payload={"is_error": status == "error", "content_blocks": content_blocks},
            provenance=provenance,
        )
    )
    if part.get("metadata") is not None or state.get("metadata"):
        result.append(
            _opaque_part_event(
                provenance,
                "assistant",
                timestamp,
                "opencode_tool_metadata",
            )
        )
    state_time = state.get("time")
    if (
        status == "completed"
        and isinstance(state_time, dict)
        and state_time.get("compacted") is not None
    ):
        result.append(
            _opaque_part_event(
                provenance,
                "assistant",
                timestamp,
                "opencode_tool_result_compacted",
            )
        )
    return result


def _tool_result(
    event: Event,
    session_id: str,
    tool_part: dict[str, Any],
    dropped: Counter[str],
    attachment_id: Callable[[], str],
) -> tuple[str, list[dict[str, Any]]]:
    blocks = event.payload.get("content_blocks")
    if not isinstance(blocks, list):
        blocks = []
    texts: list[str] = []
    attachments: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            dropped["tool_result:malformed_block"] += 1
            continue
        block_type = string(block.get("type"))
        if block_type in {"text", "input_text", "output_text"}:
            text = string(block.get("text"))
            if text:
                texts.append(text)
            else:
                dropped["tool_result:malformed_text"] += 1
        elif block_type in {"image", "input_image"}:
            attachment = _file_part(
                block.get("image_url") or block.get("url"),
                dropped,
                "tool_result:image",
            )
            if attachment:
                attachment.update(
                    {
                        "id": attachment_id(),
                        "sessionID": session_id,
                        "messageID": str(tool_part.get("messageID")),
                    }
                )
                attachments.append(attachment)
        else:
            dropped[f"tool_result:{block_type or 'unknown_block'}"] += 1
    if not texts and event.text:
        texts.append(event.text)
    return "\n".join(texts), attachments


def _file_part(value: Any, dropped: Counter[str], omission_key: str) -> dict[str, Any] | None:
    image_url = string(value)
    if not image_url:
        dropped[omission_key] += 1
        return None
    mime_type = _data_url_mime(image_url)
    if mime_type not in OPENCODE_IMPORT_MEDIA_TYPES:
        dropped[omission_key] += 1
        return None
    return {"type": "file", "mime": mime_type, "url": image_url}


def _data_url_mime(value: str) -> str | None:
    image = portable_data_image(value)
    return image[0] if image else None


def _event_timestamp(event: Event, fallback: str, dropped: Counter[str]) -> str:
    timestamp = valid_rfc3339(event.timestamp)
    if event.timestamp and not timestamp:
        dropped["timestamp:invalid"] += 1
    return timestamp or fallback


def _timestamp_ms(timestamp: str) -> int:
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return int(parsed.timestamp() * 1000)


def _iso_from_ms(value: Any) -> str:
    if not _is_non_negative_int(value):
        raise SessionMigrateError("OpenCode message has an invalid timestamp")
    return datetime.fromtimestamp(value / 1000, tz=UTC).isoformat().replace("+00:00", "Z")


def _omission_key(event: Event) -> str:
    if event.kind == EventKind.MESSAGE and event.role not in {Role.USER, Role.ASSISTANT}:
        return "message:privileged_role"
    if event.kind == EventKind.CONTEXT and event.role not in {Role.USER, None}:
        return "context:privileged_image"
    if event.kind == EventKind.OPAQUE:
        reason = string(event.payload.get("reason"))
        return f"opaque:{reason}" if reason else "opaque"
    return event.kind.value


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
