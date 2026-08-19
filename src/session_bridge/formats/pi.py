"""Pi v3 JSONL adapter for portable session import and native validation.

The writer targets the documented ``@earendil-works/pi-coding-agent`` session
format.  Pi accepts an explicit session file through ``--session``; no database
mutation or private API is required.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from session_bridge.errors import SessionBridgeError
from session_bridge.formats.common import portable_data_image, string, valid_rfc3339
from session_bridge.jsonl import encode_jsonl, file_sha256, iter_jsonl
from session_bridge.model import AgentFormat, Event, EventKind, Provenance, Role, Session

PINNED_PI_VERSION = "0.80.6"
PI_SESSION_VERSION = 3
PI_NATIVE_IMPORT_SUPPORTED = True
MAX_NATIVE_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ParsedPiSession:
    """Content projection returned by :func:`parse` for verification."""

    session_id: str
    cwd: Path
    started_at: str
    name: str | None
    model: str | None
    provider: str | None
    parent_session: str | None
    events: tuple[Event, ...]
    raw_record_count: int


def serialize(
    session: Session,
    *,
    session_id: str,
    cwd: Path,
    cli_version: str = PINNED_PI_VERSION,
    provider: str = "anthropic",
    model: str | None = None,
    timestamp: str | None = None,
    name: str | None = None,
) -> tuple[bytes, dict[str, int]]:
    """Serialize portable events as a linear Pi v3 JSONL session tree."""

    del cli_version  # Pi's v3 header carries a schema version, not a CLI version.
    fallback_timestamp = valid_rfc3339(timestamp) or valid_rfc3339(session.started_at) or _utc_now()
    target_model = model or session.model or "unknown"
    records: list[dict[str, Any]] = [
        {
            "type": "session",
            "version": PI_SESSION_VERSION,
            "id": session_id,
            "timestamp": fallback_timestamp,
            "cwd": str(cwd),
        }
    ]
    dropped: Counter[str] = Counter()
    parent_id: str | None = None
    used_entry_ids: set[str] = set()
    generated_tool_ids: deque[str] = deque()
    seen_tool_call_ids: set[str] = set()
    seen_tool_result_ids: set[str] = set()
    tool_names: dict[str, str] = {}

    pending_role: Role | None = None
    pending_source_record: int | None = None
    pending_timestamp: str | None = None
    pending_blocks: list[dict[str, Any]] = []

    def new_entry_id() -> str:
        while True:
            candidate = uuid.uuid4().hex[:8]
            if candidate not in used_entry_ids:
                used_entry_ids.add(candidate)
                return candidate

    def append_entry(
        entry_type: str,
        values: dict[str, Any],
        *,
        entry_timestamp: str | None = None,
        entry_id: str | None = None,
    ) -> str:
        nonlocal parent_id
        current_id = entry_id or new_entry_id()
        if entry_id:
            used_entry_ids.add(entry_id)
        record = {
            "type": entry_type,
            "id": current_id,
            "parentId": parent_id,
            "timestamp": entry_timestamp or fallback_timestamp,
            **values,
        }
        records.append(record)
        parent_id = current_id
        return current_id

    def flush_message() -> None:
        nonlocal pending_role, pending_source_record, pending_timestamp, pending_blocks
        if pending_role is None or not pending_blocks:
            pending_role = None
            pending_source_record = None
            pending_timestamp = None
            pending_blocks = []
            return
        message_timestamp = pending_timestamp or fallback_timestamp
        if pending_role == Role.USER:
            content: str | list[dict[str, Any]]
            if len(pending_blocks) == 1 and pending_blocks[0].get("type") == "text":
                content = str(pending_blocks[0]["text"])
            else:
                content = pending_blocks
            message: dict[str, Any] = {
                "role": "user",
                "content": content,
                "timestamp": _timestamp_ms(message_timestamp),
            }
        else:
            message = {
                "role": "assistant",
                "content": pending_blocks,
                "api": _api_for_provider(provider),
                "provider": provider,
                "model": target_model,
                "usage": _empty_usage(),
                "stopReason": (
                    "toolUse"
                    if any(block.get("type") == "toolCall" for block in pending_blocks)
                    else "stop"
                ),
                "timestamp": _timestamp_ms(message_timestamp),
            }
        append_entry("message", {"message": message}, entry_timestamp=message_timestamp)
        pending_role = None
        pending_source_record = None
        pending_timestamp = None
        pending_blocks = []

    def queue_block(event: Event, role: Role, block: dict[str, Any]) -> None:
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
        pending_blocks.append(block)

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
            if event.payload.get("ui_only_projection") is True:
                dropped["message:ui_only_projection"] += 1
            queue_block(event, event.role, {"type": "text", "text": event.text})
            continue

        if event.kind == EventKind.TOOL_CALL:
            call_id = event.tool_call_id
            if not call_id:
                call_id = f"call_session_bridge_{uuid.uuid4().hex}"
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
            tool_names.setdefault(call_id, tool_name)
            if event.payload.get("namespace"):
                dropped["tool_call:namespace"] += 1
            queue_block(
                event,
                Role.ASSISTANT,
                {
                    "type": "toolCall",
                    "id": call_id,
                    "name": tool_name,
                    "arguments": arguments,
                },
            )
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
            elif call_id not in seen_tool_call_ids:
                dropped["tool_result:orphan_id"] += 1
            if source_call_id and source_call_id in seen_tool_result_ids:
                dropped["tool_result:duplicate_id"] += 1
            if source_call_id:
                seen_tool_result_ids.add(source_call_id)
            content = _tool_result_content(event, dropped)
            event_timestamp = _event_timestamp(event, fallback_timestamp, dropped)
            append_entry(
                "message",
                {
                    "message": {
                        "role": "toolResult",
                        "toolCallId": call_id,
                        "toolName": tool_names.get(call_id, "unknown_tool"),
                        "content": content,
                        "isError": event.payload.get("is_error") is True,
                        "timestamp": _timestamp_ms(event_timestamp),
                    }
                },
                entry_timestamp=event_timestamp,
            )
            continue

        if (
            event.kind == EventKind.CONTEXT
            and event.payload.get("block_type") == "image"
            and event.role == Role.USER
        ):
            image = _pi_image(event.payload.get("image_url"))
            if image:
                queue_block(event, Role.USER, image)
            else:
                dropped["context:image"] += 1
            continue

        if event.kind == EventKind.COMPACTION and event.text:
            flush_message()
            event_timestamp = _event_timestamp(event, fallback_timestamp, dropped)
            entry_id = new_entry_id()
            # A self anchor keeps the tree reference valid while telling Pi that no
            # pre-summary entries should be replayed. Pi's v3 runtime accepts this
            # and builds context as summary + entries appended after compaction.
            append_entry(
                "compaction",
                {
                    "summary": event.text,
                    "firstKeptEntryId": entry_id,
                    "tokensBefore": 0,
                },
                entry_timestamp=event_timestamp,
                entry_id=entry_id,
            )
            if event.payload.get("has_boundary_metadata") is True:
                dropped["compaction:boundary_metadata"] += 1
            continue

        dropped[_omission_key(event)] += 1

    flush_message()
    session_name = name or session.title
    if session_name:
        append_entry("session_info", {"name": session_name})
    return encode_jsonl(records), dict(sorted(dropped.items()))


def parse(path: Path) -> ParsedPiSession:
    """Parse the active, compaction-aware branch of a Pi v3 JSONL session."""

    raw = list(iter_jsonl(path))
    records = [dict(record.value) for record in raw]
    _validate_records(records)
    header = records[0]
    session_id = string(header.get("id"))
    cwd = string(header.get("cwd"))
    started_at = string(header.get("timestamp"))
    if not session_id or not cwd or not started_at:
        raise SessionBridgeError("Pi session header is missing required metadata")

    entries = records[1:]
    indexed: dict[str, dict[str, Any]] = {}
    record_indices: dict[str, int] = {}
    for offset, entry in enumerate(entries, start=1):
        entry_id = string(entry.get("id"))
        if not entry_id:
            raise SessionBridgeError("Pi session entry is missing an id")
        if entry_id in indexed:
            raise SessionBridgeError("Pi session contains a duplicate entry id")
        indexed[entry_id] = entry
        record_indices[entry_id] = offset
    path_entries = _active_path(entries, indexed)
    selected_ids = {string(entry.get("id")) for entry in path_entries}
    events: list[Event] = []
    model = None
    provider = None
    for entry in path_entries:
        entry_id = string(entry.get("id")) or ""
        entry_events = _entry_events(entry, record_indices[entry_id])
        events.extend(entry_events)
        if entry.get("type") == "model_change":
            model = string(entry.get("modelId")) or model
            provider = string(entry.get("provider")) or provider
        elif entry.get("type") == "message" and isinstance(entry.get("message"), dict):
            message = entry["message"]
            if message.get("role") == "assistant":
                model = string(message.get("model")) or model
                provider = string(message.get("provider")) or provider
    for entry in entries:
        if string(entry.get("id")) not in selected_ids:
            events.append(
                Event(
                    kind=EventKind.OPAQUE,
                    timestamp=string(entry.get("timestamp")),
                    payload={"reason": "inactive_pi_branch_entry"},
                    provenance=Provenance(
                        record_indices[string(entry.get("id")) or ""],
                        string(entry.get("type")),
                    ),
                )
            )
    name = next(
        (
            string(entry.get("name"))
            for entry in reversed(entries)
            if entry.get("type") == "session_info" and string(entry.get("name"))
        ),
        None,
    )
    return ParsedPiSession(
        session_id=session_id,
        cwd=Path(cwd),
        started_at=started_at,
        name=name,
        model=model,
        provider=provider,
        parent_session=string(header.get("parentSession")),
        events=tuple(events),
        raw_record_count=len(raw),
    )


def parse_session(path: Path) -> Session:
    """Parse Pi v3 into the bridge's authoritative source-session model."""

    parsed = parse(path)
    return Session(
        source_format=AgentFormat.PI,
        source_path=path.resolve(),
        source_sha256=file_sha256(path),
        session_id=parsed.session_id,
        cwd=parsed.cwd,
        started_at=parsed.started_at,
        cli_version=None,
        model=parsed.model,
        title=parsed.name,
        events=parsed.events,
        raw_record_count=parsed.raw_record_count,
        model_provider=parsed.provider,
    )


def validate_native_bytes(data: bytes, session_id: str) -> None:
    """Validate a serialized Pi v3 artifact before it is published."""

    _validate_records(_decode_native_records(data), expected_session_id=session_id)


def session_relative_path(cwd: Path, session_id: str, timestamp: str) -> Path:
    """Return Pi's documented default path below ``~/.pi/agent``."""

    date = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    stamp = date.astimezone(UTC).strftime("%Y-%m-%dT%H-%M-%S-%f")[:-3] + "Z"
    return Path("sessions") / session_directory_name(cwd) / f"{stamp}_{session_id}.jsonl"


def session_directory_name(cwd: Path) -> str:
    """Encode a working directory using Pi's documented session bucket rule."""

    resolved = str(cwd.resolve())
    escaped = resolved.lstrip("/\\").replace("/", "-").replace("\\", "-").replace(":", "-")
    return f"--{escaped}--"


def _decode_native_records(data: bytes) -> list[dict[str, Any]]:
    if len(data) > MAX_NATIVE_BYTES:
        raise SessionBridgeError("Pi session exceeds the native artifact safety limit")
    records: list[dict[str, Any]] = []
    try:
        text = data.decode("utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            value = json.loads(line, parse_constant=_reject_json_constant)
            if not isinstance(value, dict):
                raise SessionBridgeError(
                    f"Pi session record at line {line_number} is not an object"
                )
            records.append(value)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SessionBridgeError("Pi session is not valid UTF-8 JSONL") from exc
    return records


def _validate_records(
    records: list[dict[str, Any]], expected_session_id: str | None = None
) -> None:
    if not records:
        raise SessionBridgeError("Pi session is empty")
    header = records[0]
    if header.get("type") != "session" or header.get("version") != PI_SESSION_VERSION:
        raise SessionBridgeError("Pi session does not have a supported v3 header")
    session_id = string(header.get("id"))
    if expected_session_id is not None and session_id != expected_session_id:
        raise SessionBridgeError("Pi session header ID does not match the target ID")
    if (
        not session_id
        or not string(header.get("cwd"))
        or not valid_rfc3339(header.get("timestamp"))
    ):
        raise SessionBridgeError("Pi session header is missing required metadata")

    known_ids: set[str] = set()
    entries = records[1:]
    has_resumable_context = False
    for entry in entries:
        entry_id = string(entry.get("id"))
        if not entry_id:
            raise SessionBridgeError("Pi session entry is missing an id")
        if entry_id in known_ids:
            raise SessionBridgeError("Pi session contains a duplicate entry id")
        parent_id = entry.get("parentId")
        if parent_id is not None and (not isinstance(parent_id, str) or parent_id not in known_ids):
            raise SessionBridgeError("Pi session tree references a missing parent")
        if not valid_rfc3339(entry.get("timestamp")):
            raise SessionBridgeError("Pi session entry has an invalid timestamp")
        known_ids.add(entry_id)

        entry_type = string(entry.get("type"))
        if entry_type == "message":
            _validate_message(entry.get("message"))
            has_resumable_context = True
        elif entry_type == "compaction":
            if not string(entry.get("summary")) or not string(entry.get("firstKeptEntryId")):
                raise SessionBridgeError("Pi compaction entry is missing required metadata")
            tokens_before = entry.get("tokensBefore")
            if not isinstance(tokens_before, int) or tokens_before < 0:
                raise SessionBridgeError("Pi compaction entry has invalid token metadata")
            has_resumable_context = True
        elif entry_type == "session_info":
            name = entry.get("name")
            if name is not None and not isinstance(name, str):
                raise SessionBridgeError("Pi session name is not a string")

    if entries:
        indexed = {str(entry["id"]): entry for entry in entries}
        _active_path(entries, indexed)
    if not has_resumable_context:
        raise SessionBridgeError("Pi session has no resumable conversation context")


def _validate_message(value: Any) -> None:
    if not isinstance(value, dict):
        raise SessionBridgeError("Pi message entry is missing its message")
    role = string(value.get("role"))
    if role not in {"user", "assistant", "toolResult"}:
        raise SessionBridgeError("Pi message entry has an unsupported role")
    content = value.get("content")
    if not isinstance(content, (str, list)):
        raise SessionBridgeError("Pi message entry has invalid content")
    if role == "toolResult" and not string(value.get("toolCallId")):
        raise SessionBridgeError("Pi tool result is missing its call ID")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _active_path(
    entries: list[dict[str, Any]], indexed: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    if not entries:
        return []
    current = entries[-1]
    reversed_path: list[dict[str, Any]] = []
    seen: set[str] = set()
    while current:
        entry_id = string(current.get("id")) or ""
        if entry_id in seen:
            raise SessionBridgeError("Pi session tree contains a cycle")
        seen.add(entry_id)
        reversed_path.append(current)
        parent_id = current.get("parentId")
        if parent_id is None:
            break
        if not isinstance(parent_id, str) or parent_id not in indexed:
            raise SessionBridgeError("Pi session tree references a missing parent")
        current = indexed[parent_id]
    reversed_path.reverse()
    return reversed_path


def _entry_events(entry: dict[str, Any], record_index: int) -> list[Event]:
    entry_type = string(entry.get("type"))
    timestamp = string(entry.get("timestamp"))
    provenance = Provenance(record_index, entry_type)
    if entry_type == "compaction":
        summary = string(entry.get("summary"))
        return (
            [
                Event(
                    kind=EventKind.COMPACTION,
                    role=Role.SYSTEM,
                    text=summary,
                    timestamp=timestamp,
                    payload={
                        "has_boundary_metadata": bool(
                            entry.get("details") is not None or entry.get("fromHook") is True
                        )
                    },
                    provenance=provenance,
                )
            ]
            if summary
            else []
        )
    if entry_type == "branch_summary":
        return [_opaque_pi_event(entry, record_index, "pi_branch_summary")]
    if entry_type == "custom_message":
        return [_opaque_pi_event(entry, record_index, "pi_custom_message")]
    if entry_type in {"model_change", "thinking_level_change", "label", "custom"}:
        return [_opaque_pi_event(entry, record_index, f"pi_{entry_type}")]
    if entry_type == "session_info":
        return []
    if entry_type != "message" or not isinstance(entry.get("message"), dict):
        return [_opaque_pi_event(entry, record_index, "unknown_pi_entry")]
    message = entry["message"]
    role_name = string(message.get("role"))
    if role_name == "user":
        return _content_events(message.get("content"), Role.USER, timestamp, provenance)
    if role_name == "assistant":
        result = _content_events(message.get("content"), Role.ASSISTANT, timestamp, provenance)
        stop_reason = string(message.get("stopReason"))
        if stop_reason not in {None, "stop", "toolUse"}:
            result.append(
                _opaque_pi_event(
                    entry,
                    record_index,
                    f"pi_assistant_stop_reason_{stop_reason}",
                )
            )
        return result
    if role_name == "toolResult":
        content_blocks = _portable_pi_result_blocks(message.get("content"))
        text = "\n".join(
            str(block["text"])
            for block in content_blocks
            if block.get("type") == "text" and block.get("text")
        )
        result = [
            Event(
                kind=EventKind.TOOL_RESULT,
                role=Role.TOOL,
                text=text or None,
                tool_name=string(message.get("toolName")),
                tool_call_id=string(message.get("toolCallId")),
                timestamp=timestamp,
                payload={
                    "is_error": message.get("isError") is True,
                    "content_blocks": content_blocks,
                },
                provenance=provenance,
            )
        ]
        if message.get("details") is not None:
            result.append(_opaque_pi_event(entry, record_index, "pi_tool_result_details"))
        return result
    reason = (
        "pi_bash_execution_message"
        if role_name == "bashExecution"
        else "pi_custom_message"
        if role_name == "custom"
        else "unknown_pi_message_role"
    )
    return [_opaque_pi_event(entry, record_index, reason)]


def _content_events(
    content: Any, role: Role, timestamp: str | None, provenance: Provenance
) -> list[Event]:
    if isinstance(content, str):
        return (
            [
                Event(
                    kind=EventKind.MESSAGE,
                    role=role,
                    text=content,
                    timestamp=timestamp,
                    provenance=provenance,
                )
            ]
            if content
            else []
        )
    if not isinstance(content, list):
        return []
    result: list[Event] = []
    for block_index, block in enumerate(content):
        block_provenance = Provenance(
            provenance.record_index,
            provenance.record_type,
            block_index=block_index,
        )
        if not isinstance(block, dict):
            result.append(Event(kind=EventKind.OPAQUE, provenance=block_provenance))
            continue
        block_type = string(block.get("type"))
        if block_type == "text" and string(block.get("text")):
            result.append(
                Event(
                    kind=EventKind.MESSAGE,
                    role=role,
                    text=string(block.get("text")),
                    timestamp=timestamp,
                    provenance=block_provenance,
                )
            )
        elif block_type == "image":
            data = string(block.get("data"))
            mime_type = string(block.get("mimeType"))
            result.append(
                Event(
                    kind=EventKind.CONTEXT,
                    role=role,
                    timestamp=timestamp,
                    payload={
                        "block_type": "image",
                        "image_url": (
                            f"data:{mime_type};base64,{data}" if data and mime_type else None
                        ),
                    },
                    provenance=block_provenance,
                )
            )
        elif block_type == "toolCall":
            arguments = block.get("arguments", {})
            result.append(
                Event(
                    kind=EventKind.TOOL_CALL,
                    role=Role.ASSISTANT,
                    timestamp=timestamp,
                    tool_name=string(block.get("name")),
                    tool_call_id=string(block.get("id")),
                    payload={"input": arguments},
                    provenance=block_provenance,
                )
            )
        elif block_type == "thinking":
            result.append(
                Event(
                    kind=EventKind.THINKING,
                    role=Role.ASSISTANT,
                    text=string(block.get("thinking")),
                    timestamp=timestamp,
                    provenance=block_provenance,
                )
            )
        else:
            result.append(
                Event(
                    kind=EventKind.OPAQUE,
                    role=role,
                    timestamp=timestamp,
                    payload={
                        "reason": f"pi_content_block_{block_type or 'missing'}",
                        "source_block_type": block_type or "<missing>",
                    },
                    provenance=block_provenance,
                )
            )
    return result


def _opaque_pi_event(entry: dict[str, Any], record_index: int, reason: str) -> Event:
    return Event(
        kind=EventKind.OPAQUE,
        timestamp=string(entry.get("timestamp")),
        payload={"reason": reason, "source_entry_type": string(entry.get("type"))},
        provenance=Provenance(record_index, string(entry.get("type"))),
    )


def _portable_pi_result_blocks(content: Any) -> list[dict[str, Any]]:
    if not isinstance(content, list):
        return [{"type": "text", "text": content}] if isinstance(content, str) else []
    result: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            result.append({"type": "text", "text": block["text"]})
        elif block.get("type") == "image":
            data = string(block.get("data"))
            mime_type = string(block.get("mimeType"))
            if data and mime_type:
                result.append(
                    {
                        "type": "image",
                        "image_url": f"data:{mime_type};base64,{data}",
                    }
                )
    return result


def _tool_result_content(event: Event, dropped: Counter[str]) -> list[dict[str, Any]]:
    source_blocks = event.payload.get("content_blocks")
    if not isinstance(source_blocks, list):
        source_blocks = []
    result: list[dict[str, Any]] = []
    for block in source_blocks:
        if not isinstance(block, dict):
            dropped["tool_result:malformed_block"] += 1
            continue
        block_type = string(block.get("type"))
        if block_type in {"text", "input_text", "output_text"}:
            text = string(block.get("text"))
            if text:
                result.append({"type": "text", "text": text})
            else:
                dropped["tool_result:malformed_text"] += 1
        elif block_type in {"image", "input_image"}:
            image = _pi_image(block.get("image_url") or block.get("url"))
            if image:
                result.append(image)
            else:
                dropped["tool_result:image"] += 1
        else:
            dropped[f"tool_result:{block_type or 'unknown_block'}"] += 1
    if not result and event.text:
        result.append({"type": "text", "text": event.text})
    return result


def _pi_image(value: Any) -> dict[str, str] | None:
    image = portable_data_image(value)
    if image is None:
        return None
    mime_type, data = image
    return {"type": "image", "data": data, "mimeType": mime_type}


def _event_timestamp(event: Event, fallback: str, dropped: Counter[str]) -> str:
    timestamp = valid_rfc3339(event.timestamp)
    if event.timestamp and not timestamp:
        dropped["timestamp:invalid"] += 1
    return timestamp or fallback


def _timestamp_ms(timestamp: str) -> int:
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return int(parsed.timestamp() * 1000)


def _api_for_provider(provider: str) -> str:
    return {
        "anthropic": "anthropic-messages",
        "openai": "openai-responses",
        "google": "google-generative-ai",
    }.get(provider, provider)


def _empty_usage() -> dict[str, Any]:
    return {
        "input": 0,
        "output": 0,
        "cacheRead": 0,
        "cacheWrite": 0,
        "totalTokens": 0,
        "cost": {
            "input": 0,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0,
            "total": 0,
        },
    }


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
