"""OpenCode 1.17.20 import/export bundle adapter.

The writer emits the public JSON shape consumed by ``opencode import``.  It
never writes OpenCode's SQLite database directly.
"""

from __future__ import annotations

import json
import re
import uuid
from collections import Counter, defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from session_bridge.errors import SessionBridgeError
from session_bridge.formats.common import string, valid_rfc3339
from session_bridge.model import Event, EventKind, Provenance, Role, Session

PINNED_OPENCODE_VERSION = "1.17.20"
OPENCODE_NATIVE_IMPORT_SUPPORTED = True
OPENCODE_IMPORT_MEDIA_TYPES = {"image/gif", "image/jpeg", "image/png", "image/webp"}
MAX_NATIVE_BYTES = 256 * 1024 * 1024
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


def session_id_from_uuid(value: str) -> str:
    """Create a valid OpenCode session ID from a UUID string."""

    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise SessionBridgeError("OpenCode target session ID is not a valid UUID") from exc
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
        raise SessionBridgeError("OpenCode session IDs must start with 'ses_'")
    fallback_timestamp = (
        valid_rfc3339(timestamp) or valid_rfc3339(session.started_at) or _utc_now()
    )
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

    pending_role: Role | None = None
    pending_source_record: int | None = None
    pending_timestamp: str | None = None
    pending_parts: list[dict[str, Any]] = []
    last_native_id_ms = -1
    native_id_counter = 0

    def new_native_id(prefix: str, message_timestamp: str) -> str:
        nonlocal last_native_id_ms, native_id_counter
        requested_ms = _timestamp_ms(message_timestamp)
        ordered_ms = max(requested_ms, last_native_id_ms)
        if ordered_ms != last_native_id_ms:
            last_native_id_ms = ordered_ms
            native_id_counter = 0
        native_id_counter += 1
        encoded = ordered_ms * 0x1000 + native_id_counter
        time_hex = encoded.to_bytes(7, "big")[-6:].hex()
        alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
        random_suffix = "".join(alphabet[value % 62] for value in uuid.uuid4().bytes[:14])
        return f"{prefix}_{time_hex}{random_suffix}"

    def new_message_id(message_timestamp: str) -> str:
        return new_native_id("msg", message_timestamp)

    def new_part_id(message_timestamp: str) -> str:
        return new_native_id("prt", message_timestamp)

    def append_user(parts: list[dict[str, Any]], message_timestamp: str) -> str:
        nonlocal latest_user_id, latest_message_id
        message_id = new_message_id(message_timestamp)
        for part in parts:
            part.update(
                {
                    "id": part.get("id") or new_part_id(message_timestamp),
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
                    "time": {"created": _timestamp_ms(message_timestamp)},
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
        message_id = new_message_id(message_timestamp)
        for part in parts:
            part.update(
                {
                    "id": part.get("id") or new_part_id(message_timestamp),
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
                "created": _timestamp_ms(message_timestamp),
                "completed": _timestamp_ms(message_timestamp),
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
        if event.kind == EventKind.MESSAGE and event.text and event.role in {
            Role.USER,
            Role.ASSISTANT,
        }:
            if event.payload.get("ui_only_projection") is True:
                dropped["message:ui_only_projection"] += 1
            queue_part(event, event.role, {"type": "text", "text": event.text})
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
                "_bridge_timestamp": event_timestamp,
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
                part = {
                    "type": "tool",
                    "callID": call_id,
                    "tool": event.tool_name or "unknown_tool",
                    "state": {
                        "status": "pending",
                        "input": {},
                        "raw": "{}",
                    },
                    "_bridge_timestamp": _event_timestamp(
                        event, fallback_timestamp, dropped
                    ),
                }
                append_assistant(
                    [part],
                    str(part["_bridge_timestamp"]),
                )
            result_timestamp = _event_timestamp(event, fallback_timestamp, dropped)
            result_text, attachments = _tool_result(
                event,
                session_id,
                part,
                dropped,
                attachment_id=lambda timestamp=result_timestamp: new_part_id(timestamp),
            )
            start_timestamp = str(part.pop("_bridge_timestamp", result_timestamp))
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
                queue_part(event, Role.USER, part)
            continue

        if event.kind == EventKind.COMPACTION and event.text:
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
            part.pop("_bridge_timestamp", None)

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
            "slug": slug or f"session-bridge-{session_id.removeprefix('ses_')[:12]}",
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
    """Parse the portable history from an OpenCode import/export JSON bundle."""

    try:
        data = path.read_bytes()
    except OSError as exc:
        raise SessionBridgeError("cannot read OpenCode import bundle") from exc
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
    for message_index, message in enumerate(messages):
        if not isinstance(message, dict) or not isinstance(message.get("info"), dict):
            raise SessionBridgeError("OpenCode import bundle contains a malformed message")
        message_info = message["info"]
        parts = message.get("parts")
        if not isinstance(parts, list):
            raise SessionBridgeError("OpenCode import bundle message is missing parts")
        role_name = string(message_info.get("role"))
        timestamp = _iso_from_ms(message_info.get("time", {}).get("created"))
        is_summary = message_info.get("summary") is True
        for part_index, part in enumerate(parts):
            part_count += 1
            provenance = Provenance(message_index, role_name, block_index=part_index)
            if not isinstance(part, dict):
                events.append(Event(kind=EventKind.OPAQUE, provenance=provenance))
                continue
            part_type = string(part.get("type"))
            if part_type == "text" and string(part.get("text")):
                if is_summary:
                    events.append(
                        Event(
                            kind=EventKind.COMPACTION,
                            role=Role.SYSTEM,
                            text=string(part.get("text")),
                            timestamp=timestamp,
                            provenance=provenance,
                        )
                    )
                elif role_name in {"user", "assistant"}:
                    events.append(
                        Event(
                            kind=EventKind.MESSAGE,
                            role=Role.USER if role_name == "user" else Role.ASSISTANT,
                            text=string(part.get("text")),
                            timestamp=timestamp,
                            provenance=provenance,
                        )
                    )
            elif part_type == "file" and role_name == "user":
                events.append(
                    Event(
                        kind=EventKind.CONTEXT,
                        role=Role.USER,
                        timestamp=timestamp,
                        payload={
                            "block_type": "image",
                            "image_url": string(part.get("url")),
                            "mime_type": string(part.get("mime")),
                        },
                        provenance=provenance,
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
                # Structural trigger; the following summary assistant is the
                # model-visible compacted history represented in our event model.
                continue
            else:
                events.append(
                    Event(
                        kind=EventKind.OPAQUE,
                        role=(
                            Role.USER
                            if role_name == "user"
                            else Role.ASSISTANT
                            if role_name == "assistant"
                            else None
                        ),
                        timestamp=timestamp,
                        payload={"source_part_type": part_type or "<missing>"},
                        provenance=provenance,
                    )
                )
    return ParsedOpenCodeSession(
        session_id=session_id,
        cwd=Path(cwd),
        started_at=_iso_from_ms(created),
        title=title,
        events=tuple(events),
        raw_record_count=1 + len(messages) + part_count,
    )


parse = parse_import


def validate_native_bytes(data: bytes, session_id: str) -> None:
    """Validate an official OpenCode import bundle before it is published."""

    _validate_import_bundle(_decode_import_bundle(data), expected_session_id=session_id)


def _decode_import_bundle(data: bytes) -> dict[str, Any]:
    if len(data) > MAX_NATIVE_BYTES:
        raise SessionBridgeError("OpenCode import bundle exceeds the safety limit")
    try:
        value = json.loads(data.decode("utf-8"), parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SessionBridgeError("OpenCode import bundle is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise SessionBridgeError("OpenCode import bundle is missing session info")
    return value


def _validate_import_bundle(
    value: dict[str, Any], expected_session_id: str | None = None
) -> None:
    if not isinstance(value.get("info"), dict):
        raise SessionBridgeError("OpenCode import bundle is missing session info")
    info = value["info"]
    session_id = string(info.get("id"))
    if expected_session_id is not None and session_id != expected_session_id:
        raise SessionBridgeError("OpenCode bundle ID does not match the target ID")
    cwd = string(info.get("directory"))
    title = string(info.get("title"))
    time = info.get("time") if isinstance(info.get("time"), dict) else {}
    created = time.get("created")
    updated = time.get("updated")
    if not session_id or not session_id.startswith("ses_") or not cwd or not title:
        raise SessionBridgeError("OpenCode import bundle has invalid required metadata")
    if not isinstance(created, int) or created < 0:
        raise SessionBridgeError("OpenCode import bundle has an invalid creation time")
    if not isinstance(updated, int) or updated < created:
        raise SessionBridgeError("OpenCode import bundle has an invalid update time")
    messages = value.get("messages")
    if not isinstance(messages, list):
        raise SessionBridgeError("OpenCode import bundle is missing messages")

    known_message_ids: set[str] = set()
    known_part_ids: set[str] = set()
    previous_message_id: str | None = None
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("info"), dict):
            raise SessionBridgeError("OpenCode import bundle contains a malformed message")
        message_info = message["info"]
        message_id = string(message_info.get("id"))
        role = string(message_info.get("role"))
        if (
            not message_id
            or message_id in known_message_ids
            or message_info.get("sessionID") != session_id
            or role not in {"user", "assistant"}
        ):
            raise SessionBridgeError("OpenCode import bundle has invalid message metadata")
        if expected_session_id is not None and (
            not _NATIVE_RECORD_ID.fullmatch(message_id)
            or (previous_message_id is not None and message_id <= previous_message_id)
        ):
            raise SessionBridgeError("OpenCode message IDs are not native ascending IDs")
        _iso_from_ms(
            message_info.get("time", {}).get("created")
            if isinstance(message_info.get("time"), dict)
            else None
        )
        if role == "assistant" and not string(message_info.get("parentID")):
            raise SessionBridgeError("OpenCode assistant message is missing its parent ID")
        known_message_ids.add(message_id)
        previous_message_id = message_id

        parts = message.get("parts")
        if not isinstance(parts, list):
            raise SessionBridgeError("OpenCode import bundle message is missing parts")
        previous_part_id: str | None = None
        for part in parts:
            if not isinstance(part, dict):
                raise SessionBridgeError("OpenCode import bundle contains a malformed part")
            part_id = string(part.get("id"))
            if (
                not part_id
                or part_id in known_part_ids
                or part.get("sessionID") != session_id
                or part.get("messageID") != message_id
                or not string(part.get("type"))
            ):
                raise SessionBridgeError("OpenCode import bundle has invalid part metadata")
            if expected_session_id is not None and (
                not _NATIVE_RECORD_ID.fullmatch(part_id)
                or (previous_part_id is not None and part_id <= previous_part_id)
            ):
                raise SessionBridgeError("OpenCode part IDs are not native ascending IDs")
            known_part_ids.add(part_id)
            previous_part_id = part_id


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _tool_part_events(
    part: dict[str, Any], timestamp: str, provenance: Provenance
) -> list[Event]:
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
        return result
    content_blocks: list[dict[str, Any]] = []
    if status == "completed":
        output = state.get("output")
        if isinstance(output, str) and output:
            content_blocks.append({"type": "text", "text": output})
        attachments = state.get("attachments")
        if isinstance(attachments, list):
            for attachment in attachments:
                if isinstance(attachment, dict) and string(attachment.get("url")):
                    content_blocks.append(
                        {"type": "image", "image_url": string(attachment.get("url"))}
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
    if not value.startswith("data:"):
        return None
    header, separator, data = value.partition(",")
    if not separator or not data or not header.endswith(";base64"):
        return None
    return header[len("data:") : -len(";base64")]


def _event_timestamp(event: Event, fallback: str, dropped: Counter[str]) -> str:
    timestamp = valid_rfc3339(event.timestamp)
    if event.timestamp and not timestamp:
        dropped["timestamp:invalid"] += 1
    return timestamp or fallback


def _timestamp_ms(timestamp: str) -> int:
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return int(parsed.timestamp() * 1000)


def _iso_from_ms(value: Any) -> str:
    if not isinstance(value, int) or value < 0:
        raise SessionBridgeError("OpenCode message has an invalid timestamp")
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
