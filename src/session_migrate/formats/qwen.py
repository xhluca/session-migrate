"""Qwen Code 0.22.1 append-only ChatRecord JSONL adapter."""

from __future__ import annotations

import json
import re
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from session_migrate.errors import SessionMigrateError
from session_migrate.formats.common import content_text, portable_data_image, string, valid_rfc3339
from session_migrate.jsonl import DEFAULT_MAX_TOTAL_BYTES, encode_jsonl, file_sha256, iter_jsonl
from session_migrate.model import AgentFormat, Event, EventKind, Provenance, Role, Session

PINNED_QWEN_VERSION = "0.22.1"
MAX_NATIVE_BYTES = DEFAULT_MAX_TOTAL_BYTES
_PROJECT_CHAR = re.compile(r"[^A-Za-z0-9]")


def serialize(
    session: Session,
    *,
    session_id: str,
    cwd: Path,
    cli_version: str = PINNED_QWEN_VERSION,
    model: str | None = None,
    timestamp: str | None = None,
    title: str | None = None,
) -> tuple[bytes, dict[str, int]]:
    """Write a fresh linear Qwen ChatRecord graph."""

    fallback = valid_rfc3339(timestamp) or valid_rfc3339(session.started_at) or _utc_now()
    records: list[dict[str, Any]] = []
    dropped: Counter[str] = Counter()
    parent: str | None = None
    seen_calls: set[str] = set()
    seen_results: set[str] = set()
    tool_names: dict[str, str] = {}
    pending: dict[str, Any] | None = None
    pending_source: int | None = None
    pending_event: Event | None = None

    def append(record_type: str, message: dict[str, Any] | None, event: Event | None) -> None:
        nonlocal parent
        record_id = str(uuid.uuid4())
        record: dict[str, Any] = {
            "uuid": record_id,
            "parentUuid": parent,
            "sessionId": session_id,
            "timestamp": _event_timestamp(event, fallback, dropped),
            "type": record_type,
            "cwd": str(cwd),
            "version": cli_version,
        }
        if message is not None:
            record["message"] = message
        if record_type == "user":
            record["provenance"] = "real_user"
        elif record_type == "assistant":
            record["provenance"] = "assistant_output"
            record["model"] = model or session.model or "unknown"
        elif record_type == "tool_result":
            record["provenance"] = "tool_result"
        records.append(record)
        parent = record_id

    def flush() -> None:
        nonlocal pending, pending_event, pending_source
        if pending is not None and pending.get("parts"):
            append(
                "user" if pending.get("role") == "user" else "assistant",
                pending,
                pending_event,
            )
        pending = None
        pending_source = None
        pending_event = None

    def message_for(event: Event, role: str) -> dict[str, Any]:
        nonlocal pending, pending_event, pending_source
        source = event.provenance.record_index
        if pending is None or pending.get("role") != role or pending_source != source:
            flush()
            pending = {"role": role, "parts": []}
            pending_source = source
            pending_event = event
        return pending

    for event in session.events:
        if event.kind == EventKind.MESSAGE and event.role in {Role.USER, Role.ASSISTANT}:
            if not event.text:
                continue
            message_for(event, "user" if event.role == Role.USER else "model")["parts"].append(
                {"text": event.text}
            )
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
            args = event.payload.get("input", {})
            if not isinstance(args, dict):
                args = {"input": args}
                dropped["tool_call:non_object_input"] += 1
            if call_id in seen_calls:
                dropped["tool_call:duplicate_id"] += 1
            seen_calls.add(call_id)
            tool_names.setdefault(call_id, name)
            if event.payload.get("namespace"):
                dropped["tool_call:namespace"] += 1
            message_for(event, "model")["parts"].append(
                {"functionCall": {"id": call_id, "name": name, "args": args}}
            )
            continue
        if event.kind == EventKind.TOOL_RESULT:
            flush()
            call_id = event.tool_call_id or f"call_missing_{uuid.uuid4().hex}"
            if not event.tool_call_id:
                dropped["tool_result:missing_id"] += 1
            elif call_id not in seen_calls:
                dropped["tool_result:orphan_id"] += 1
            if event.tool_call_id in seen_results:
                dropped["tool_result:duplicate_id"] += 1
            if event.tool_call_id:
                seen_results.add(event.tool_call_id)
            output = _tool_output(event, dropped)
            name = event.tool_name or tool_names.get(call_id) or "unknown_tool"
            message = {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "id": call_id,
                            "name": name,
                            "response": output,
                        }
                    }
                ],
            }
            append("tool_result", message, event)
            records[-1]["toolCallResult"] = {
                "callId": call_id,
                "status": "error" if event.payload.get("is_error") is True else "success",
                "resultDisplay": event.text or content_text(event.payload.get("content")),
            }
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
            media_type, data = image
            message_for(event, "user")["parts"].append(
                {"inlineData": {"mimeType": media_type, "data": data}}
            )
            continue
        dropped[_omission_key(event)] += 1
    flush()

    session_title = title or session.title
    if session_title:
        append("system", None, None)
        records[-1].update(
            {
                "subtype": "custom_title",
                "provenance": "system",
                "systemPayload": {"customTitle": session_title, "titleSource": "manual"},
            }
        )
    data = encode_jsonl(records)
    validate_native_bytes(data, session_id)
    return data, dict(sorted(dropped.items()))


def parse_session(path: Path) -> Session:
    records = [dict(item.value) for item in iter_jsonl(path)]
    _validate_records(records)
    active = _active_path(records)
    active_ids = {record["uuid"] for record in active}
    events: list[Event] = []
    model = None
    for index, record in enumerate(records):
        if record["uuid"] not in active_ids:
            events.append(_opaque(index, record, "inactive_qwen_branch_record"))
    for record in active:
        index = records.index(record)
        record_type = record["type"]
        if record_type in {"user", "assistant", "tool_result"}:
            events.extend(_message_events(record, index))
            if record_type == "assistant":
                model = string(record.get("model")) or model
        elif record.get("subtype") != "custom_title":
            events.append(_opaque(index, record, "qwen_runtime_or_metadata_record"))
    first = records[0]
    title = next(
        (
            string(record.get("systemPayload", {}).get("customTitle"))
            for record in reversed(records)
            if record.get("subtype") == "custom_title"
            and isinstance(record.get("systemPayload"), dict)
        ),
        None,
    )
    return Session(
        source_format=AgentFormat.QWEN,
        source_path=path.resolve(),
        source_sha256=file_sha256(path),
        session_id=first["sessionId"],
        cwd=Path(first["cwd"]),
        started_at=first["timestamp"],
        cli_version=first["version"],
        model=model,
        title=title,
        events=tuple(events),
        raw_record_count=len(records),
        model_provider=None,
    )


parse = parse_session


def validate_native_bytes(data: bytes, session_id: str) -> None:
    _validate_records(_decode(data), expected_session_id=session_id)


def session_relative_path(cwd: Path, session_id: str) -> Path:
    return Path("projects") / project_directory_name(cwd) / "chats" / f"{session_id}.jsonl"


def project_directory_name(cwd: Path) -> str:
    return _PROJECT_CHAR.sub("-", str(cwd.resolve()))


def _decode(data: bytes) -> list[dict[str, Any]]:
    if not data or len(data) > MAX_NATIVE_BYTES:
        raise SessionMigrateError("Qwen session is empty or exceeds the native safety limit")
    records: list[dict[str, Any]] = []
    try:
        for line in data.decode().split("\n"):
            if not line.strip():
                continue
            value = json.loads(line, parse_constant=_reject_constant)
            if not isinstance(value, dict):
                raise SessionMigrateError("Qwen session record is not an object")
            records.append(value)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SessionMigrateError("Qwen session is not valid UTF-8 JSONL") from exc
    return records


def _validate_records(
    records: list[dict[str, Any]], expected_session_id: str | None = None
) -> None:
    if not records:
        raise SessionMigrateError("Qwen session is empty")
    ids: set[str] = set()
    session_id: str | None = None
    has_history = False
    for record in records:
        record_id = _uuid(record.get("uuid"), "Qwen record uuid")
        if record_id in ids:
            raise SessionMigrateError("Qwen session contains a duplicate record uuid")
        parent = record.get("parentUuid")
        if parent is not None and (not isinstance(parent, str) or parent not in ids):
            raise SessionMigrateError("Qwen session record references a missing parent")
        ids.add(record_id)
        current_session = _uuid(record.get("sessionId"), "Qwen sessionId")
        session_id = session_id or current_session
        if current_session != session_id:
            raise SessionMigrateError("Qwen session contains mixed session IDs")
        if expected_session_id is not None and current_session != expected_session_id:
            raise SessionMigrateError("Qwen session linkage does not match the target ID")
        if not valid_rfc3339(record.get("timestamp")):
            raise SessionMigrateError("Qwen record has an invalid timestamp")
        if not string(record.get("cwd")) or not string(record.get("version")):
            raise SessionMigrateError("Qwen record is missing cwd or version metadata")
        record_type = record.get("type")
        if record_type not in {"user", "assistant", "tool_result", "system"}:
            raise SessionMigrateError("Qwen record has an unsupported type")
        if record_type in {"user", "assistant", "tool_result"}:
            _validate_message(record_type, record.get("message"))
            has_history = True
    _active_path(records)
    if not has_history:
        raise SessionMigrateError("Qwen session has no resumable conversation history")


def _validate_message(record_type: str, value: Any) -> None:
    if not isinstance(value, dict) or not isinstance(value.get("parts"), list):
        raise SessionMigrateError("Qwen message record is malformed")
    expected = "model" if record_type == "assistant" else "user"
    if value.get("role") != expected:
        raise SessionMigrateError("Qwen message role does not match its record type")
    for part in value["parts"]:
        if not isinstance(part, dict):
            raise SessionMigrateError("Qwen message part is not an object")


def _active_path(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {record["uuid"]: record for record in records}
    current: dict[str, Any] | None = records[-1]
    backward: list[dict[str, Any]] = []
    seen: set[str] = set()
    while current is not None:
        current_id = current["uuid"]
        if current_id in seen:
            raise SessionMigrateError("Qwen session graph contains a cycle")
        seen.add(current_id)
        backward.append(current)
        parent = current.get("parentUuid")
        if parent is None:
            break
        current = by_id.get(parent)
        if current is None:
            raise SessionMigrateError("Qwen session graph references a missing ancestor")
    return list(reversed(backward))


def _message_events(record: dict[str, Any], index: int) -> list[Event]:
    message = record["message"]
    record_type = record["type"]
    timestamp = record["timestamp"]
    provenance = Provenance(index, record_type, record["uuid"])
    events: list[Event] = []
    for block_index, part in enumerate(message["parts"]):
        block_provenance = Provenance(index, record_type, record["uuid"], block_index)
        text = part.get("text")
        if isinstance(text, str) and text:
            events.append(
                Event(
                    kind=EventKind.THINKING if part.get("thought") is True else EventKind.MESSAGE,
                    role=Role.ASSISTANT if record_type == "assistant" else Role.USER,
                    text=text,
                    timestamp=timestamp,
                    payload={"reason": "qwen_private_thinking"}
                    if part.get("thought") is True
                    else {},
                    provenance=block_provenance,
                )
            )
            continue
        call = part.get("functionCall")
        if isinstance(call, dict):
            events.append(
                Event(
                    kind=EventKind.TOOL_CALL,
                    role=Role.ASSISTANT,
                    tool_name=string(call.get("name")),
                    tool_call_id=string(call.get("id")),
                    timestamp=timestamp,
                    payload={"input": call.get("args", {})},
                    provenance=block_provenance,
                )
            )
            continue
        response = part.get("functionResponse")
        if isinstance(response, dict):
            payload = response.get("response", {})
            text_value, content_blocks = _portable_tool_output(payload)
            tool_result = record.get("toolCallResult")
            events.append(
                Event(
                    kind=EventKind.TOOL_RESULT,
                    role=Role.TOOL,
                    text=text_value or None,
                    tool_name=string(response.get("name")),
                    tool_call_id=string(response.get("id")),
                    timestamp=timestamp,
                    payload={
                        "content": text_value,
                        "content_blocks": content_blocks,
                        "is_error": isinstance(tool_result, dict)
                        and tool_result.get("status") == "error",
                    },
                    provenance=block_provenance,
                )
            )
            continue
        inline = part.get("inlineData")
        if isinstance(inline, dict):
            media_type = string(inline.get("mimeType"))
            data = string(inline.get("data"))
            image_url = f"data:{media_type};base64,{data}" if media_type and data else None
            events.append(
                Event(
                    kind=EventKind.CONTEXT,
                    role=Role.USER if record_type != "assistant" else Role.ASSISTANT,
                    timestamp=timestamp,
                    payload={"block_type": "image", "image_url": image_url},
                    provenance=block_provenance,
                )
            )
            continue
        events.append(
            Event(
                kind=EventKind.OPAQUE,
                timestamp=timestamp,
                payload={"reason": "qwen_unknown_message_part"},
                provenance=block_provenance,
            )
        )
    if not message["parts"]:
        events.append(
            Event(
                kind=EventKind.OPAQUE,
                timestamp=timestamp,
                payload={"reason": "qwen_empty_message"},
                provenance=provenance,
            )
        )
    return events


def _tool_output(event: Event, dropped: Counter[str]) -> dict[str, Any]:
    source = event.payload.get("content_blocks")
    blocks = source if isinstance(source, list) else []
    portable: list[dict[str, Any]] = []
    texts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            dropped["tool_result:malformed_block"] += 1
            continue
        block_type = string(block.get("type"))
        if block_type in {"text", "input_text", "output_text"}:
            text = string(block.get("text"))
            if text:
                texts.append(text)
                portable.append({"type": "text", "text": text})
            else:
                dropped["tool_result:malformed_text"] += 1
        elif block_type in {"image", "input_image"}:
            image = portable_data_image(block.get("image_url") or block.get("url"))
            if image:
                media_type, data = image
                portable.append({"type": "image", "image_url": f"data:{media_type};base64,{data}"})
            else:
                dropped["tool_result:image"] += 1
        else:
            dropped[f"tool_result:{block_type or 'unknown_block'}"] += 1
    content = event.payload.get("content")
    text = "\n".join(texts) or event.text or content_text(content)
    if not text and content not in (None, ""):
        dropped["tool_result:opaque"] += 1
        text = json.dumps(content, ensure_ascii=False, default=str)
    if text and not portable:
        portable.append({"type": "text", "text": text})
    return {
        "output": text or "",
        "sessionMigrateContent": portable,
        **({"error": text or "tool failed"} if event.payload.get("is_error") is True else {}),
    }


def _portable_tool_output(value: Any) -> tuple[str, list[dict[str, Any]]]:
    if isinstance(value, dict):
        output = value.get("output")
        text = output if isinstance(output, str) else content_text(output)
        source = value.get("sessionMigrateContent")
        if isinstance(source, list):
            blocks: list[dict[str, Any]] = []
            for block in source:
                if not isinstance(block, dict):
                    blocks.append({"type": "opaque"})
                    continue
                block_type = string(block.get("type"))
                if block_type == "text" and string(block.get("text")):
                    blocks.append({"type": "text", "text": block["text"]})
                elif block_type == "image" and portable_data_image(block.get("image_url")):
                    blocks.append({"type": "image", "image_url": block["image_url"]})
                else:
                    blocks.append({"type": "opaque"})
            return text or "", blocks
        if text:
            return text, [{"type": "text", "text": text}]
    text = content_text(value)
    if not text and value not in ({}, None):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return text or "", ([{"type": "text", "text": text}] if text else [])


def _opaque(index: int, record: dict[str, Any], reason: str) -> Event:
    return Event(
        kind=EventKind.OPAQUE,
        timestamp=valid_rfc3339(record.get("timestamp")),
        payload={"reason": reason},
        provenance=Provenance(index, string(record.get("type")), string(record.get("uuid"))),
    )


def _event_timestamp(event: Event | None, fallback: str, dropped: Counter[str]) -> str:
    if event is None or event.timestamp is None:
        return fallback
    value = valid_rfc3339(event.timestamp)
    if value:
        return value
    dropped["timestamp:invalid"] += 1
    return fallback


def _omission_key(event: Event) -> str:
    if event.kind == EventKind.OPAQUE:
        reason = event.payload.get("reason")
        return f"opaque:{reason}" if isinstance(reason, str) and reason else "opaque"
    if event.kind == EventKind.THINKING:
        return "thinking:private"
    return event.kind.value


def _uuid(value: Any, label: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise SessionMigrateError(f"{label} is not a UUID") from exc


def _reject_constant(value: str) -> None:
    raise ValueError(f"unsupported JSON constant: {value}")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
