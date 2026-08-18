"""GitHub Copilot CLI 1.0.70 session-event adapter.

Copilot's canonical portable history is the append-only ``events.jsonl`` file
below ``$COPILOT_HOME/session-state/<uuid>``.  The global and per-session SQLite
files are projections/runtime state and are deliberately not synthesized.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from session_bridge.errors import SessionBridgeError
from session_bridge.formats.common import portable_data_image, string, valid_rfc3339
from session_bridge.jsonl import encode_jsonl, iter_jsonl
from session_bridge.model import Event, EventKind, Provenance, Role, Session

PINNED_COPILOT_VERSION = "1.0.70"
COPILOT_EVENT_VERSION = 1
MAX_NATIVE_BYTES = 256 * 1024 * 1024
_EMITTED_TYPES = {
    "session.start",
    "user.message",
    "assistant.message",
    "tool.execution_start",
    "tool.execution_complete",
    "session.compaction_complete",
}


@dataclass(frozen=True, slots=True)
class ParsedCopilotSession:
    """Portable projection of a Copilot event log used for verification."""

    session_id: str
    cwd: Path
    started_at: str
    cli_version: str
    events: tuple[Event, ...]
    raw_record_count: int


def serialize(
    session: Session,
    *,
    session_id: str,
    cwd: Path,
    cli_version: str = PINNED_COPILOT_VERSION,
    model: str | None = None,
    timestamp: str | None = None,
) -> tuple[bytes, dict[str, int]]:
    """Serialize portable events into Copilot's public session event schema."""

    fallback_timestamp = (
        valid_rfc3339(timestamp) or valid_rfc3339(session.started_at) or _utc_now()
    )
    target_model = model or session.model or "unknown"
    dropped: Counter[str] = Counter()
    records: list[dict[str, Any]] = []
    parent_id: str | None = None
    last_timestamp: datetime | None = None
    generated_tool_ids: deque[str] = deque()
    seen_tool_call_ids: set[str] = set()
    seen_tool_result_ids: set[str] = set()
    tool_names: dict[str, str] = {}
    tool_inputs: dict[str, Any] = {}

    def append_event(event_type: str, data: dict[str, Any], raw_timestamp: str) -> None:
        nonlocal parent_id, last_timestamp
        parsed = _parse_timestamp(raw_timestamp)
        if last_timestamp is not None and parsed < last_timestamp:
            parsed = last_timestamp + timedelta(microseconds=1)
            dropped["timestamp:native_order_adjusted"] += 1
        last_timestamp = parsed
        event_id = str(uuid.uuid4())
        records.append(
            {
                "type": event_type,
                "data": data,
                "id": event_id,
                "timestamp": _format_timestamp(parsed),
                "parentId": parent_id,
            }
        )
        parent_id = event_id

    append_event(
        "session.start",
        {
            "sessionId": session_id,
            "version": COPILOT_EVENT_VERSION,
            "producer": "session-bridge",
            "copilotVersion": cli_version,
            "startTime": fallback_timestamp,
            "selectedModel": target_model,
            "context": {"cwd": str(cwd)},
            "alreadyInUse": False,
            "remoteSteerable": False,
        },
        fallback_timestamp,
    )

    pending_role: Role | None = None
    pending_record_index: int | None = None
    pending_timestamp: str | None = None
    pending_text: list[str] = []
    pending_attachments: list[dict[str, Any]] = []
    pending_tools: list[tuple[str, str, Any]] = []

    def flush_message() -> None:
        nonlocal pending_role, pending_record_index, pending_timestamp
        nonlocal pending_text, pending_attachments, pending_tools
        if pending_role is None:
            return
        event_timestamp = pending_timestamp or fallback_timestamp
        content = "\n".join(pending_text)
        if pending_role == Role.USER:
            if content or pending_attachments:
                data: dict[str, Any] = {"content": content}
                if pending_attachments:
                    data["attachments"] = pending_attachments
                append_event("user.message", data, event_timestamp)
        else:
            requests = [
                {
                    "toolCallId": call_id,
                    "name": name,
                    "arguments": arguments,
                    "type": "function",
                }
                for call_id, name, arguments in pending_tools
            ]
            if content or requests:
                data = {
                    "messageId": str(uuid.uuid4()),
                    "model": target_model,
                    "content": content,
                }
                if requests:
                    data["toolRequests"] = requests
                append_event("assistant.message", data, event_timestamp)
                for call_id, name, arguments in pending_tools:
                    append_event(
                        "tool.execution_start",
                        {
                            "toolCallId": call_id,
                            "toolName": name,
                            "arguments": arguments,
                            "model": target_model,
                        },
                        event_timestamp,
                    )
        pending_role = None
        pending_record_index = None
        pending_timestamp = None
        pending_text = []
        pending_attachments = []
        pending_tools = []

    def queue(event: Event, role: Role) -> None:
        nonlocal pending_role, pending_record_index, pending_timestamp
        if pending_role is not None and (
            pending_role != role
            or pending_record_index != event.provenance.record_index
        ):
            flush_message()
        pending_role = role
        pending_record_index = event.provenance.record_index
        pending_timestamp = pending_timestamp or _event_timestamp(
            event, fallback_timestamp, dropped
        )

    for event in session.events:
        if event.kind == EventKind.MESSAGE and event.text and event.role in {
            Role.USER,
            Role.ASSISTANT,
        }:
            if event.payload.get("ui_only_projection") is True:
                dropped["message:ui_only_projection"] += 1
            queue(event, event.role)
            pending_text.append(event.text)
            continue

        if (
            event.kind == EventKind.CONTEXT
            and event.role == Role.USER
            and event.payload.get("block_type") == "image"
        ):
            image = _blob_attachment(event.payload.get("image_url"))
            if image:
                queue(event, Role.USER)
                pending_attachments.append(image)
            else:
                dropped["context:image"] += 1
            continue

        if event.kind == EventKind.TOOL_CALL:
            call_id = event.tool_call_id
            if not call_id:
                call_id = f"call_session_bridge_{uuid.uuid4().hex}"
                generated_tool_ids.append(call_id)
                dropped["tool_call:missing_id"] += 1
            name = event.tool_name
            if not name:
                name = "unknown_tool"
                dropped["tool_call:missing_name"] += 1
            arguments = event.payload.get("input", {})
            if not isinstance(arguments, dict):
                arguments = {"input": arguments}
                dropped["tool_call:non_object_input"] += 1
            if call_id in seen_tool_call_ids:
                dropped["tool_call:duplicate_id"] += 1
            seen_tool_call_ids.add(call_id)
            tool_names.setdefault(call_id, name)
            tool_inputs.setdefault(call_id, arguments)
            if event.payload.get("namespace"):
                dropped["tool_call:namespace"] += 1
            queue(event, Role.ASSISTANT)
            pending_tools.append((call_id, name, arguments))
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
                name = event.tool_name or "unknown_tool"
                arguments: dict[str, Any] = {}
                append_event(
                    "assistant.message",
                    {
                        "messageId": str(uuid.uuid4()),
                        "model": target_model,
                        "content": "",
                        "toolRequests": [
                            {
                                "toolCallId": call_id,
                                "name": name,
                                "arguments": arguments,
                                "type": "function",
                            }
                        ],
                    },
                    _event_timestamp(event, fallback_timestamp, dropped),
                )
                append_event(
                    "tool.execution_start",
                    {
                        "toolCallId": call_id,
                        "toolName": name,
                        "arguments": arguments,
                        "model": target_model,
                    },
                    _event_timestamp(event, fallback_timestamp, dropped),
                )
                seen_tool_call_ids.add(call_id)
                tool_names[call_id] = name
                tool_inputs[call_id] = arguments
            if source_call_id and source_call_id in seen_tool_result_ids:
                dropped["tool_result:duplicate_id"] += 1
            if source_call_id:
                seen_tool_result_ids.add(source_call_id)
            content, contents, binary, omissions = _tool_result(event)
            dropped.update(omissions)
            is_error = event.payload.get("is_error") is True
            complete: dict[str, Any] = {
                "toolCallId": call_id,
                "success": not is_error,
                "model": target_model,
            }
            if is_error:
                complete["error"] = {"message": content or "tool execution failed"}
            else:
                result: dict[str, Any] = {"content": content}
                if contents:
                    result["contents"] = contents
                if binary:
                    result["binaryResultsForLlm"] = binary
                complete["result"] = result
            append_event(
                "tool.execution_complete",
                complete,
                _event_timestamp(event, fallback_timestamp, dropped),
            )
            continue

        if event.kind == EventKind.COMPACTION and event.text:
            flush_message()
            append_event(
                "session.compaction_complete",
                {"success": True, "summaryContent": event.text},
                _event_timestamp(event, fallback_timestamp, dropped),
            )
            if event.payload.get("has_boundary_metadata") is True:
                dropped["compaction:boundary_metadata"] += 1
            continue

        dropped[_omission_key(event)] += 1

    flush_message()
    if not any(record["type"] == "user.message" for record in records):
        raise SessionBridgeError("Copilot target has no resumable user conversation history")
    if not any(record["type"] == "assistant.message" for record in records):
        raise SessionBridgeError("Copilot target has no resumable assistant conversation history")
    return encode_jsonl(records), dict(sorted(dropped.items()))


def parse(path: Path) -> ParsedCopilotSession:
    """Parse the bridge-supported projection of a Copilot event log."""

    raw = list(iter_jsonl(path))
    records = [dict(item.value) for item in raw]
    _validate_records(records)
    first_data = records[0]["data"]
    events: list[Event] = []
    calls_from_assistant: set[str] = set()
    starts: dict[str, tuple[str | None, Any]] = {}
    for index, record in enumerate(records):
        record_type = string(record.get("type")) or ""
        data = record.get("data")
        if not isinstance(data, dict):
            continue
        timestamp = string(record.get("timestamp"))
        provenance = Provenance(index, record_type, string(record.get("id")))
        if record_type == "user.message":
            text = string(data.get("content"))
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
            for block_index, attachment in enumerate(data.get("attachments", [])):
                if not isinstance(attachment, dict) or attachment.get("type") != "blob":
                    events.append(
                        Event(
                            kind=EventKind.OPAQUE,
                            role=Role.USER,
                            timestamp=timestamp,
                            payload={"source_block_type": "copilot_attachment"},
                            provenance=Provenance(
                                index, record_type, string(record.get("id")), block_index
                            ),
                        )
                    )
                    continue
                image_url = _attachment_image_url(attachment)
                events.append(
                    Event(
                        kind=EventKind.CONTEXT,
                        role=Role.USER,
                        timestamp=timestamp,
                        payload={"block_type": "image", "image_url": image_url},
                        provenance=Provenance(
                            index, record_type, string(record.get("id")), block_index
                        ),
                    )
                )
        elif record_type == "assistant.message":
            text = string(data.get("content"))
            if text:
                events.append(
                    Event(
                        kind=EventKind.MESSAGE,
                        role=Role.ASSISTANT,
                        text=text,
                        timestamp=timestamp,
                        provenance=provenance,
                    )
                )
            for block_index, request in enumerate(data.get("toolRequests", [])):
                if not isinstance(request, dict):
                    continue
                call_id = string(request.get("toolCallId"))
                if call_id:
                    calls_from_assistant.add(call_id)
                events.append(
                    Event(
                        kind=EventKind.TOOL_CALL,
                        role=Role.ASSISTANT,
                        tool_name=string(request.get("name")),
                        tool_call_id=call_id,
                        timestamp=timestamp,
                        payload={"input": request.get("arguments", {})},
                        provenance=Provenance(
                            index, record_type, string(record.get("id")), block_index
                        ),
                    )
                )
        elif record_type == "tool.execution_start":
            call_id = string(data.get("toolCallId"))
            if call_id:
                starts[call_id] = (
                    string(data.get("toolName")),
                    data.get("arguments", {}),
                )
        elif record_type == "tool.execution_complete":
            call_id = string(data.get("toolCallId"))
            name, _ = starts.get(call_id or "", (None, {}))
            result = data.get("result") if isinstance(data.get("result"), dict) else {}
            blocks = _portable_result_blocks(result)
            content = string(result.get("content"))
            error = data.get("error") if isinstance(data.get("error"), dict) else {}
            if data.get("success") is False:
                content = string(error.get("message")) or content
            events.append(
                Event(
                    kind=EventKind.TOOL_RESULT,
                    role=Role.TOOL,
                    text=content,
                    tool_name=name,
                    tool_call_id=call_id,
                    timestamp=timestamp,
                    payload={
                        "is_error": data.get("success") is False,
                        "content_blocks": blocks,
                    },
                    provenance=provenance,
                )
            )
        elif record_type == "session.compaction_complete":
            summary = string(data.get("summaryContent"))
            if data.get("success") is True and summary:
                events.append(
                    Event(
                        kind=EventKind.COMPACTION,
                        role=Role.SYSTEM,
                        text=summary,
                        timestamp=timestamp,
                        provenance=provenance,
                    )
                )
        elif record_type != "session.start":
            events.append(
                Event(
                    kind=EventKind.OPAQUE,
                    timestamp=timestamp,
                    payload={"source_event_type": record_type},
                    provenance=provenance,
                )
            )
    # A native log may contain a start without the corresponding assistant
    # message (older/foreign producer). Preserve it once instead of losing it.
    for call_id, (name, arguments) in starts.items():
        if call_id not in calls_from_assistant:
            events.append(
                Event(
                    kind=EventKind.TOOL_CALL,
                    role=Role.ASSISTANT,
                    tool_name=name,
                    tool_call_id=call_id,
                    payload={"input": arguments},
                    provenance=Provenance(0, "tool.execution_start"),
                )
            )
    return ParsedCopilotSession(
        session_id=first_data["sessionId"],
        cwd=Path(first_data["context"]["cwd"]),
        started_at=first_data["startTime"],
        cli_version=first_data["copilotVersion"],
        events=tuple(events),
        raw_record_count=len(records),
    )


def validate_native_bytes(data: bytes, session_id: str) -> None:
    """Validate a generated Copilot event log before installation."""

    _validate_records(_decode_native_records(data), expected_session_id=session_id)


def workspace_bytes(
    *, session_id: str, cwd: Path, timestamp: str, title: str | None = None
) -> bytes:
    """Return Copilot's small picker/workspace sidecar as conservative YAML."""

    values: list[tuple[str, Any]] = [
        ("id", session_id),
        ("cwd", str(cwd)),
        ("client_name", "github/cli"),
    ]
    if title:
        values.extend((("name", title), ("user_named", True)))
    values.extend(
        (
            ("summary_count", 0),
            ("created_at", timestamp),
            ("updated_at", timestamp),
        )
    )
    lines = []
    for key, value in values:
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, int):
            rendered = str(value)
        else:
            rendered = json.dumps(value, ensure_ascii=False)
        lines.append(f"{key}: {rendered}")
    return ("\n".join(lines) + "\n").encode()


def session_relative_path(session_id: str) -> Path:
    return Path("session-state") / session_id / "events.jsonl"


def _decode_native_records(data: bytes) -> list[dict[str, Any]]:
    if len(data) > MAX_NATIVE_BYTES:
        raise SessionBridgeError("Copilot session exceeds the native artifact safety limit")
    records: list[dict[str, Any]] = []
    try:
        text = data.decode("utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            value = json.loads(line, parse_constant=_reject_json_constant)
            if not isinstance(value, dict):
                raise SessionBridgeError(
                    f"Copilot record {line_number} is not a JSON object"
                )
            records.append(value)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SessionBridgeError("generated Copilot session is not valid JSONL") from exc
    return records


def _validate_records(
    records: list[dict[str, Any]], *, expected_session_id: str | None = None
) -> None:
    if not records or records[0].get("type") != "session.start":
        raise SessionBridgeError("Copilot session must begin with session.start")
    first_data = records[0].get("data")
    if not isinstance(first_data, dict):
        raise SessionBridgeError("Copilot session.start data is invalid")
    session_id = string(first_data.get("sessionId"))
    if not session_id or (expected_session_id and session_id != expected_session_id):
        raise SessionBridgeError("Copilot session ID does not match the target")
    try:
        uuid.UUID(session_id)
    except ValueError as exc:
        raise SessionBridgeError("Copilot session ID is not a UUID") from exc
    if first_data.get("version") != COPILOT_EVENT_VERSION:
        raise SessionBridgeError("Copilot session event version is unsupported")
    if not all(
        string(first_data.get(key))
        for key in ("producer", "copilotVersion", "startTime")
    ):
        raise SessionBridgeError("Copilot session.start is missing required metadata")
    context = first_data.get("context")
    if not isinstance(context, dict) or not string(context.get("cwd")):
        raise SessionBridgeError("Copilot session.start has no working directory")

    prior_id: str | None = None
    seen_ids: set[str] = set()
    last_time: datetime | None = None
    calls: Counter[str] = Counter()
    results: Counter[str] = Counter()
    user_count = 0
    assistant_count = 0
    for index, record in enumerate(records):
        record_type = string(record.get("type"))
        if record_type not in _EMITTED_TYPES:
            raise SessionBridgeError(f"unsupported generated Copilot event: {record_type}")
        event_id = string(record.get("id"))
        try:
            parsed_id = uuid.UUID(event_id or "")
        except ValueError as exc:
            raise SessionBridgeError("Copilot event ID is not a UUID") from exc
        if parsed_id.version != 4 or event_id in seen_ids:
            raise SessionBridgeError("Copilot event IDs must be unique UUIDv4 values")
        seen_ids.add(event_id)
        if record.get("parentId") != prior_id:
            raise SessionBridgeError("Copilot event parent chain is not linear")
        prior_id = event_id
        timestamp = valid_rfc3339(record.get("timestamp"))
        if not timestamp:
            raise SessionBridgeError("Copilot event timestamp is invalid")
        parsed_time = _parse_timestamp(timestamp)
        if last_time is not None and parsed_time < last_time:
            raise SessionBridgeError("Copilot event timestamps are not ordered")
        last_time = parsed_time
        data = record.get("data")
        if not isinstance(data, dict):
            raise SessionBridgeError("Copilot event data must be an object")
        if index == 0:
            continue
        if record_type == "user.message":
            if not isinstance(data.get("content"), str):
                raise SessionBridgeError("Copilot user message content is invalid")
            user_count += 1
            _validate_attachments(data.get("attachments", []))
        elif record_type == "assistant.message":
            if not string(data.get("messageId")) or not isinstance(data.get("content"), str):
                raise SessionBridgeError("Copilot assistant message is invalid")
            assistant_count += 1
            requests = data.get("toolRequests", [])
            if not isinstance(requests, list):
                raise SessionBridgeError("Copilot toolRequests must be an array")
            for request in requests:
                if not isinstance(request, dict):
                    raise SessionBridgeError("Copilot tool request is invalid")
                call_id = string(request.get("toolCallId"))
                if not call_id or not string(request.get("name")):
                    raise SessionBridgeError("Copilot tool request is missing linkage")
                calls[call_id] += 1
        elif record_type == "tool.execution_start":
            if not string(data.get("toolCallId")) or not string(data.get("toolName")):
                raise SessionBridgeError("Copilot tool start is missing linkage")
        elif record_type == "tool.execution_complete":
            call_id = string(data.get("toolCallId"))
            if not call_id or not isinstance(data.get("success"), bool):
                raise SessionBridgeError("Copilot tool result is missing linkage")
            results[call_id] += 1
            if data["success"] and not isinstance(data.get("result"), dict):
                raise SessionBridgeError("successful Copilot tool result has no result data")
            if not data["success"] and not isinstance(data.get("error"), dict):
                raise SessionBridgeError("failed Copilot tool result has no error data")
        elif record_type == "session.compaction_complete":
            if data.get("success") is not True or not string(data.get("summaryContent")):
                raise SessionBridgeError("Copilot compaction summary is invalid")
    if not user_count or not assistant_count:
        raise SessionBridgeError("Copilot session has no resumable conversation history")
    for call_id, count in results.items():
        if count > calls[call_id]:
            raise SessionBridgeError("Copilot tool result has no preceding tool request")


def _validate_attachments(value: Any) -> None:
    if not isinstance(value, list):
        raise SessionBridgeError("Copilot attachments must be an array")
    for attachment in value:
        if not isinstance(attachment, dict) or attachment.get("type") != "blob":
            raise SessionBridgeError("generated Copilot attachment is unsupported")
        image = _attachment_image_url(attachment)
        if not image:
            raise SessionBridgeError("generated Copilot image attachment is invalid")


def _blob_attachment(value: Any) -> dict[str, str] | None:
    image = portable_data_image(value)
    if not image:
        return None
    mime_type, data = image
    extension = {"image/jpeg": "jpg"}.get(mime_type, mime_type.split("/", 1)[1])
    return {
        "type": "blob",
        "data": data,
        "mimeType": mime_type,
        "displayName": f"imported-image.{extension}",
    }


def _attachment_image_url(value: dict[str, Any]) -> str | None:
    data = string(value.get("data"))
    mime_type = string(value.get("mimeType"))
    if not data or not mime_type:
        return None
    candidate = f"data:{mime_type};base64,{data}"
    return candidate if portable_data_image(candidate) else None


def _tool_result(
    event: Event,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], Counter[str]]:
    source = event.payload.get("content_blocks")
    blocks = source if isinstance(source, list) else []
    contents: list[dict[str, Any]] = []
    binary: list[dict[str, Any]] = []
    text_parts: list[str] = []
    omitted: Counter[str] = Counter()
    for block in blocks:
        if not isinstance(block, dict):
            omitted["tool_result:malformed_block"] += 1
            continue
        block_type = string(block.get("type"))
        if block_type in {"text", "input_text", "output_text"}:
            text = string(block.get("text"))
            if text:
                text_parts.append(text)
                contents.append({"type": "text", "text": text})
            else:
                omitted["tool_result:malformed_text"] += 1
        elif block_type in {"image", "input_image"}:
            image = portable_data_image(block.get("image_url") or block.get("url"))
            if not image:
                omitted["tool_result:image"] += 1
                continue
            mime_type, data = image
            contents.append({"type": "image", "data": data, "mimeType": mime_type})
            binary.append(
                {
                    "type": "image",
                    "data": data,
                    "mimeType": mime_type,
                    "description": "imported tool image",
                }
            )
        else:
            omitted[f"tool_result:{block_type or 'unknown_block'}"] += 1
    if not text_parts and event.text:
        text_parts.append(event.text)
        contents.insert(0, {"type": "text", "text": event.text})
    return "\n".join(text_parts), contents, binary, omitted


def _portable_result_blocks(result: dict[str, Any]) -> list[dict[str, Any]]:
    value = result.get("contents")
    blocks: list[dict[str, Any]] = []
    if isinstance(value, list):
        for block in value:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                blocks.append({"type": "text", "text": block["text"]})
            elif block.get("type") == "image":
                data = string(block.get("data"))
                mime_type = string(block.get("mimeType"))
                candidate = f"data:{mime_type};base64,{data}"
                if data and mime_type and portable_data_image(candidate):
                    blocks.append({"type": "image", "image_url": candidate})
    if not blocks and isinstance(result.get("content"), str):
        blocks.append({"type": "text", "text": result["content"]})
    return blocks


def _event_timestamp(event: Event, fallback: str, dropped: Counter[str]) -> str:
    timestamp = valid_rfc3339(event.timestamp)
    if event.timestamp and not timestamp:
        dropped["timestamp:invalid"] += 1
    return timestamp or fallback


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(UTC)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _omission_key(event: Event) -> str:
    if event.kind == EventKind.MESSAGE and event.role not in {Role.USER, Role.ASSISTANT}:
        return "message:privileged_role"
    if event.kind == EventKind.CONTEXT and event.role not in {Role.USER, None}:
        return "context:privileged_image"
    if event.kind == EventKind.OPAQUE:
        detail = next(
            (
                event.payload.get(key)
                for key in (
                    "reason",
                    "source_record_type",
                    "source_event_type",
                    "source_block_type",
                    "source_item_type",
                )
                if event.payload.get(key)
            ),
            "unknown",
        )
        return f"opaque:{detail}"
    return event.kind.value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
