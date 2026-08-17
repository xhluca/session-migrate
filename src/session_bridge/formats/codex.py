"""Codex rollout JSONL reader and conservative native writer."""

from __future__ import annotations

import json
import uuid
from collections import Counter
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from session_bridge.formats.common import content_text, object_value, string
from session_bridge.jsonl import encode_jsonl, file_sha256, iter_jsonl
from session_bridge.model import AgentFormat, Event, EventKind, Provenance, Role, Session

PINNED_CODEX_VERSION = "0.144.4"


def parse(path: Path) -> Session:
    records = list(iter_jsonl(path))
    events: list[Event] = []
    fallback_events: list[Event] = []
    session_id = None
    cwd = None
    started_at = None
    cli_version = None
    model = None
    response_message_count = 0
    title = None

    for record in records:
        value = record.value
        record_type = string(value.get("type"))
        timestamp = string(value.get("timestamp"))
        payload = object_value(value.get("payload"))
        provenance = Provenance(record.index, record_type)
        if record_type == "session_meta":
            session_id = session_id or string(payload.get("id")) or string(
                payload.get("session_id")
            )
            cwd_value = string(payload.get("cwd"))
            cwd = cwd or (Path(cwd_value) if cwd_value else None)
            started_at = started_at or string(payload.get("timestamp")) or timestamp
            cli_version = cli_version or string(payload.get("cli_version"))
            continue
        if record_type == "response_item":
            parsed = _response_item_events(payload, timestamp, provenance)
            events.extend(parsed)
            response_message_count += sum(event.kind == EventKind.MESSAGE for event in parsed)
        elif record_type == "event_msg":
            event_type = string(payload.get("type"))
            if event_type == "user_message":
                fallback_events.append(
                    Event(
                        kind=EventKind.MESSAGE,
                        role=Role.USER,
                        text=string(payload.get("message")),
                        timestamp=timestamp,
                        provenance=provenance,
                    )
                )
            elif event_type == "agent_message":
                fallback_events.append(
                    Event(
                        kind=EventKind.MESSAGE,
                        role=Role.ASSISTANT,
                        text=string(payload.get("message")),
                        timestamp=timestamp,
                        provenance=provenance,
                    )
                )
            elif event_type == "task_started":
                model = model or string(payload.get("model"))
            elif event_type == "thread_name_updated":
                title = string(payload.get("name")) or title
        elif record_type == "compacted":
            events.append(
                Event(
                    kind=EventKind.COMPACTION,
                    role=Role.SYSTEM,
                    text=string(payload.get("message")),
                    timestamp=timestamp,
                    provenance=provenance,
                )
            )
        elif record_type == "turn_context":
            model = model or string(payload.get("model"))
            events.append(
                Event(
                    kind=EventKind.CONTEXT,
                    role=Role.SYSTEM,
                    timestamp=timestamp,
                    payload={"source_record_type": "turn_context"},
                    provenance=provenance,
                )
            )
        elif record_type not in {"world_state", "security_risk_score"}:
            events.append(
                Event(
                    kind=EventKind.OPAQUE,
                    timestamp=timestamp,
                    payload={"source_record_type": record_type or "<missing>"},
                    provenance=provenance,
                )
            )

    if response_message_count == 0:
        events.extend(event for event in fallback_events if event.text)
        events.sort(key=lambda event: event.provenance.record_index)
    return Session(
        source_format=AgentFormat.CODEX,
        source_path=path.resolve(),
        source_sha256=file_sha256(path),
        session_id=session_id,
        cwd=cwd,
        started_at=started_at,
        cli_version=cli_version,
        model=model,
        title=title,
        events=tuple(events),
        raw_record_count=len(records),
    )


def serialize(
    session: Session,
    *,
    session_id: str,
    cwd: Path,
    cli_version: str = PINNED_CODEX_VERSION,
    model_provider: str = "openai",
    timestamp: str | None = None,
) -> tuple[bytes, dict[str, int]]:
    """Serialize the model-visible subset accepted by Codex CLI 0.144.4."""

    fallback_timestamp = timestamp or session.started_at or _utc_now()
    records: list[dict[str, Any]] = [
        {
            "timestamp": fallback_timestamp,
            "type": "session_meta",
            "payload": {
                "session_id": session_id,
                "id": session_id,
                "timestamp": fallback_timestamp,
                "cwd": str(cwd),
                "originator": "agent-session-bridge",
                "cli_version": cli_version,
                "source": "cli",
                "model_provider": model_provider,
                "history_mode": "legacy",
            },
        }
    ]
    dropped: Counter[str] = Counter()
    for event in session.events:
        event_timestamp = event.timestamp or fallback_timestamp
        if event.kind == EventKind.MESSAGE and event.text:
            if event.role == Role.ASSISTANT:
                records.append(
                    _envelope(
                        event_timestamp,
                        "event_msg",
                        {"type": "agent_message", "message": event.text},
                    )
                )
                content_type = "output_text"
                role = "assistant"
            else:
                records.append(
                    _envelope(
                        event_timestamp,
                        "event_msg",
                        {"type": "user_message", "message": event.text},
                    )
                )
                content_type = "input_text"
                role = "user"
            records.append(
                _envelope(
                    event_timestamp,
                    "response_item",
                    {
                        "type": "message",
                        "role": role,
                        "content": [{"type": content_type, "text": event.text}],
                    },
                )
            )
        elif event.kind == EventKind.TOOL_CALL:
            arguments = event.payload.get("input", {})
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
            records.append(
                _envelope(
                    event_timestamp,
                    "response_item",
                    {
                        "type": "function_call",
                        "name": event.tool_name or "unknown_tool",
                        "arguments": arguments,
                        "call_id": event.tool_call_id or f"call_session_bridge_{uuid.uuid4().hex}",
                    },
                )
            )
        elif event.kind == EventKind.TOOL_RESULT:
            records.append(
                _envelope(
                    event_timestamp,
                    "response_item",
                    {
                        "type": "function_call_output",
                        "call_id": event.tool_call_id or "unknown_tool_call",
                        "output": event.text or "",
                    },
                )
            )
        elif event.kind == EventKind.COMPACTION and event.text:
            records.append(
                _envelope(
                    event_timestamp,
                    "compacted",
                    {"message": event.text},
                )
            )
        else:
            dropped[event.kind.value] += 1
    return encode_jsonl(records), dict(sorted(dropped.items()))


def rollout_relative_path(session_id: str, timestamp: str) -> Path:
    date = _parse_date(timestamp)
    filename_timestamp = date.strftime("%Y-%m-%dT%H-%M-%S")
    return (
        Path("sessions")
        / date.strftime("%Y")
        / date.strftime("%m")
        / date.strftime("%d")
        / f"rollout-{filename_timestamp}-{session_id}.jsonl"
    )


def _response_item_events(
    payload: dict[str, Any],
    timestamp: str | None,
    provenance: Provenance,
) -> list[Event]:
    item_type = string(payload.get("type"))
    if item_type == "message":
        role = Role.ASSISTANT if payload.get("role") == "assistant" else Role.USER
        result: list[Event] = []
        content = payload.get("content")
        if not isinstance(content, list):
            return result
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
            if block_type in {"input_text", "output_text", "text"}:
                text = string(block.get("text"))
                if text:
                    result.append(
                        Event(
                            kind=EventKind.MESSAGE,
                            role=role,
                            text=text,
                            timestamp=timestamp,
                            provenance=block_provenance,
                        )
                    )
            elif block_type in {"input_image", "image"}:
                result.append(
                    Event(
                        kind=EventKind.CONTEXT,
                        role=role,
                        timestamp=timestamp,
                        payload={"block_type": "image", "source": block},
                        provenance=block_provenance,
                    )
                )
            else:
                result.append(
                    Event(
                        kind=EventKind.OPAQUE,
                        role=role,
                        timestamp=timestamp,
                        payload={"source_block_type": block_type or "<missing>"},
                        provenance=block_provenance,
                    )
                )
        return result
    if item_type in {"function_call", "custom_tool_call"}:
        arguments: Any = payload.get("arguments", payload.get("input", {}))
        if isinstance(arguments, str):
            with suppress(json.JSONDecodeError):
                arguments = json.loads(arguments)
        return [
            Event(
                kind=EventKind.TOOL_CALL,
                role=Role.ASSISTANT,
                timestamp=timestamp,
                tool_name=string(payload.get("name")),
                tool_call_id=string(payload.get("call_id")) or string(payload.get("id")),
                payload={"input": arguments},
                provenance=provenance,
            )
        ]
    if item_type in {"function_call_output", "custom_tool_call_output"}:
        return [
            Event(
                kind=EventKind.TOOL_RESULT,
                role=Role.TOOL,
                timestamp=timestamp,
                text=content_text(payload.get("output")),
                tool_call_id=string(payload.get("call_id")),
                provenance=provenance,
            )
        ]
    if item_type == "reasoning":
        return [
            Event(
                kind=EventKind.THINKING,
                role=Role.ASSISTANT,
                timestamp=timestamp,
                payload={"source_item_type": "reasoning"},
                provenance=provenance,
            )
        ]
    if item_type:
        return [
            Event(
                kind=EventKind.OPAQUE,
                timestamp=timestamp,
                payload={"source_item_type": item_type},
                provenance=provenance,
            )
        ]
    return []


def _envelope(timestamp: str, record_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"timestamp": timestamp, "type": record_type, "payload": payload}


def _parse_date(timestamp: str) -> datetime:
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return datetime.now(UTC)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
