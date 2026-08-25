"""Kimi Code 0.38.0 session-state and wire-journal adapter."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from session_migrate.errors import JsonlError, SessionMigrateError
from session_migrate.formats.common import content_text, portable_data_image, string, valid_rfc3339
from session_migrate.jsonl import (
    DEFAULT_MAX_RECORDS,
    DEFAULT_MAX_TOTAL_BYTES,
    encode_jsonl,
    ensure_file_unchanged,
    file_snapshot,
    iter_jsonl,
)
from session_migrate.model import AgentFormat, Event, EventKind, Provenance, Role, Session

PINNED_KIMI_VERSION = "0.38.0"
WIRE_PROTOCOL_VERSION = "1.5"
KIMI_BUNDLE_SCHEMA = "session-migrate.kimi.v1"
STATE_FILENAME = "state.json"
WIRE_FILENAME = "wire.jsonl"
MAX_BUNDLE_BYTES = DEFAULT_MAX_TOTAL_BYTES
MAX_RECORDS = DEFAULT_MAX_RECORDS


@dataclass(frozen=True, slots=True)
class ParsedKimiBundle:
    state: dict[str, Any]
    records: tuple[dict[str, Any], ...]


def serialize(
    session: Session,
    *,
    session_id: str,
    cwd: Path,
    cli_version: str = PINNED_KIMI_VERSION,
    model: str | None = None,
    timestamp: str | None = None,
    title: str | None = None,
) -> tuple[bytes, dict[str, int]]:
    """Create a validated Kimi state/wire bundle without touching its home."""

    del cli_version, model
    fallback = valid_rfc3339(timestamp) or valid_rfc3339(session.started_at) or _utc_now()
    started_ms = _timestamp_ms(fallback)
    native_id = native_session_id(session_id)
    dropped: Counter[str] = Counter()
    records: list[dict[str, Any]] = [
        {"type": "metadata", "protocol_version": WIRE_PROTOCOL_VERSION, "created_at": started_ms}
    ]
    seen_calls: set[str] = set()
    seen_results: set[str] = set()
    tool_names: dict[str, str] = {}
    pending: dict[str, Any] | None = None
    pending_source: int | None = None
    pending_event: Event | None = None
    logical_time = started_ms

    def next_time(event: Event | None) -> int:
        nonlocal logical_time
        candidate = _event_ms(event, started_ms, dropped)
        logical_time = max(candidate, logical_time + 1)
        return logical_time

    def append_message(message: dict[str, Any], event: Event | None) -> None:
        records.append(
            {
                "type": "context.append_message",
                "agentId": "main",
                "message": message,
                "time": next_time(event),
            }
        )

    def flush() -> None:
        nonlocal pending, pending_event, pending_source
        if pending is not None and (pending.get("content") or pending.get("toolCalls")):
            append_message(pending, pending_event)
        pending = None
        pending_source = None
        pending_event = None

    def message_for(event: Event, role: str) -> dict[str, Any]:
        nonlocal pending, pending_event, pending_source
        source = event.provenance.record_index
        if pending is None or pending.get("role") != role or pending_source != source:
            flush()
            pending = {
                "role": role,
                "content": [],
                "toolCalls": [],
                "origin": {"kind": "user"} if role == "user" else None,
            }
            if pending["origin"] is None:
                del pending["origin"]
            pending_source = source
            pending_event = event
        return pending

    for event in session.events:
        if event.kind == EventKind.MESSAGE and event.role in {Role.USER, Role.ASSISTANT}:
            if not event.text:
                continue
            role = "user" if event.role == Role.USER else "assistant"
            message_for(event, role)["content"].append({"type": "text", "text": event.text})
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
            message_for(event, "assistant")["toolCalls"].append(
                {
                    "type": "function",
                    "id": call_id,
                    "name": name,
                    "arguments": json.dumps(args, ensure_ascii=False, separators=(",", ":")),
                }
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
            output, result_content = _tool_result_content(event, dropped)
            append_message(
                {
                    "role": "tool",
                    "name": event.tool_name or tool_names.get(call_id) or "unknown_tool",
                    "content": result_content,
                    "toolCalls": [],
                    "toolCallId": call_id,
                    **({"isError": True} if event.payload.get("is_error") is True else {}),
                },
                event,
            )
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
            message_for(event, "user")["content"].append(
                {"type": "image_url", "imageUrl": {"url": f"data:{media_type};base64,{data}"}}
            )
            continue
        if event.kind == EventKind.COMPACTION and event.text:
            flush()
            records.append(
                {
                    "type": "context.apply_compaction",
                    "agentId": "main",
                    "summary": event.text,
                    "compactedCount": 0,
                    "time": next_time(event),
                }
            )
            if event.payload.get("has_boundary_metadata") is True:
                dropped["compaction:boundary_metadata"] += 1
            if event.payload.get("replacement_history_expanded") is True:
                dropped["compaction:replacement_history_expanded"] += 1
            continue
        dropped[_omission_key(event)] += 1
    flush()
    if len(records) == 1:
        raise SessionMigrateError("conversion produced no resumable conversation history")
    session_title = title or session.title
    state = {
        "id": native_id,
        "version": 2,
        **({"title": session_title, "titleKind": "custom"} if session_title else {}),
        "cwd": str(cwd),
        "createdAt": started_ms,
        "updatedAt": max(logical_time, started_ms),
        "archived": False,
        "agents": {"main": {"homedir": "agents/main", "type": "main"}},
        "custom": {"writer": "session-migrate"},
        "lastTurnReason": "completed",
    }
    bundle = {"schema": KIMI_BUNDLE_SCHEMA, "state": state, "records": records}
    data = (
        json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    validate_native_bytes(data, native_id)
    return data, dict(sorted(dropped.items()))


def parse_session(path: Path) -> Session:
    session_dir, wire_path, state_path = _source_paths(path)
    wire_before = file_snapshot(wire_path)
    state_before = file_snapshot(state_path)
    records = [dict(item.value) for item in iter_jsonl(wire_path)]
    try:
        state_bytes = state_path.read_bytes()
        state = json.loads(state_bytes)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise JsonlError("Kimi state.json is unreadable or invalid") from exc
    if not isinstance(state, dict):
        raise JsonlError("Kimi state.json is not an object")
    _validate_state(state, session_dir)
    _validate_wire(records)
    events: list[Event] = []
    model = None
    for index, record in enumerate(records[1:], start=1):
        record_type = record["type"]
        if record_type == "context.append_message":
            events.extend(_context_message_events(record, index))
        elif record_type == "context.append_loop_event":
            events.extend(_loop_events(record, index))
        elif record_type == "context.apply_compaction":
            summary = string(record.get("summary")) or string(record.get("contextSummary"))
            events.append(
                Event(
                    kind=EventKind.COMPACTION if summary else EventKind.OPAQUE,
                    text=summary,
                    timestamp=_ms_timestamp(record.get("time")),
                    payload={} if summary else {"reason": "kimi_compaction_without_summary"},
                    provenance=Provenance(index, record_type),
                )
            )
        elif record_type == "llm.request":
            model = string(record.get("model")) or model
            events.append(_opaque(index, record, "kimi_runtime_record"))
        else:
            events.append(_opaque(index, record, "kimi_runtime_record"))
    ensure_file_unchanged(wire_path, wire_before)
    ensure_file_unchanged(state_path, state_before)
    digest = hashlib.sha256(state_bytes + b"\0" + wire_path.read_bytes()).hexdigest()
    return Session(
        source_format=AgentFormat.KIMI,
        source_path=wire_path.resolve(),
        source_sha256=digest,
        session_id=state["id"],
        cwd=Path(state["cwd"]),
        started_at=_ms_timestamp(state["createdAt"]),
        cli_version=PINNED_KIMI_VERSION,
        model=model,
        title=string(state.get("title")),
        events=tuple(events),
        raw_record_count=len(records),
        model_provider=None,
    )


parse = parse_session


def validate_native_bytes(data: bytes, session_id: str) -> ParsedKimiBundle:
    if not data or len(data) > MAX_BUNDLE_BYTES:
        raise SessionMigrateError("generated Kimi bundle is empty or exceeds the safety limit")
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SessionMigrateError("generated Kimi bundle is not valid JSON") from exc
    if not isinstance(value, dict) or value.get("schema") != KIMI_BUNDLE_SCHEMA:
        raise SessionMigrateError("generated Kimi bundle has an unsupported schema")
    state = value.get("state")
    records = value.get("records")
    if (
        not isinstance(state, dict)
        or not isinstance(records, list)
        or not all(isinstance(record, dict) for record in records)
    ):
        raise SessionMigrateError("generated Kimi bundle is missing state or wire records")
    if state.get("id") != session_id:
        raise SessionMigrateError("generated Kimi bundle session linkage is invalid")
    _validate_state(state, None)
    _validate_wire(records)
    return ParsedKimiBundle(dict(state), tuple(dict(record) for record in records))


def native_files(data: bytes, session_id: str, session_dir: Path) -> tuple[bytes, bytes]:
    parsed = validate_native_bytes(data, session_id)
    state = dict(parsed.state)
    agents = dict(state["agents"])
    main = dict(agents["main"])
    main["homedir"] = str(session_dir / "agents/main")
    agents["main"] = main
    state["agents"] = agents
    state_bytes = (json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    return state_bytes, encode_jsonl(parsed.records)


def native_record_count(data: bytes) -> int:
    value = json.loads(data)
    records = value.get("records", []) if isinstance(value, dict) else []
    return len(records) if isinstance(records, list) else 0


def native_session_id(value: str) -> str:
    raw = value.removeprefix("session_")
    try:
        return f"session_{uuid.UUID(raw)}"
    except ValueError as exc:
        raise SessionMigrateError("Kimi session ID is invalid") from exc


def portable_session_id(value: str) -> str:
    return native_session_id(value).removeprefix("session_")


def session_relative_path(cwd: Path, session_id: str) -> Path:
    native_id = native_session_id(session_id)
    return Path("sessions") / workdir_key(cwd) / native_id / "agents/main" / WIRE_FILENAME


def workdir_key(cwd: Path) -> str:
    normalized = str(cwd.resolve()).replace("\\", "/").rstrip("/") or "/"
    name = normalized.rsplit("/", 1)[-1]
    slug = re.sub(r"[^a-z0-9._-]+", "-", name.lower()).strip("-")[:40].strip("-")
    if slug in {"", ".", ".."}:
        slug = "workspace"
    return f"wd_{slug}_{hashlib.sha256(normalized.encode()).hexdigest()[:12]}"


def _source_paths(path: Path) -> tuple[Path, Path, Path]:
    resolved = path.resolve()
    if resolved.is_dir():
        session_dir = resolved
    elif (
        resolved.name == WIRE_FILENAME
        and resolved.parent.name == "main"
        and resolved.parent.parent.name == "agents"
    ):
        session_dir = resolved.parent.parent.parent
    elif resolved.name == STATE_FILENAME:
        session_dir = resolved.parent
    else:
        raise JsonlError(
            "Kimi source must be a session directory, state.json, or agents/main/wire.jsonl"
        )
    return session_dir, session_dir / "agents/main" / WIRE_FILENAME, session_dir / STATE_FILENAME


def _validate_state(state: dict[str, Any], session_dir: Path | None) -> None:
    native_session_id(state.get("id"))
    if state.get("version") != 2 or not string(state.get("cwd")):
        raise SessionMigrateError("Kimi state metadata has an unsupported version or cwd")
    for key in ("createdAt", "updatedAt"):
        if not isinstance(state.get(key), int) or state[key] < 0:
            raise SessionMigrateError("Kimi state metadata has an invalid timestamp")
    if not isinstance(state.get("archived"), bool):
        raise SessionMigrateError("Kimi state metadata has an invalid archive flag")
    agents = state.get("agents")
    main = agents.get("main") if isinstance(agents, dict) else None
    if not isinstance(main, dict) or main.get("type") != "main" or not string(main.get("homedir")):
        raise SessionMigrateError("Kimi state metadata is missing the main agent")
    if session_dir is not None:
        homedir = Path(main["homedir"])
        if homedir.is_absolute() and homedir.resolve() != (session_dir / "agents/main").resolve():
            raise SessionMigrateError("Kimi main-agent path does not match the session directory")


def _validate_wire(records: list[dict[str, Any]]) -> None:
    if not records or len(records) > MAX_RECORDS:
        raise SessionMigrateError("Kimi wire journal is empty or exceeds the record limit")
    header = records[0]
    if (
        header.get("type") != "metadata"
        or header.get("protocol_version") != WIRE_PROTOCOL_VERSION
        or not isinstance(header.get("created_at"), int)
    ):
        raise SessionMigrateError("Kimi wire journal has an unsupported metadata header")
    has_history = False
    for record in records[1:]:
        if not string(record.get("type")) or not isinstance(record.get("time"), int):
            raise SessionMigrateError("Kimi wire record is missing type or time")
        if record["type"] == "context.append_message":
            _validate_context_message(record.get("message"))
            has_history = True
        elif record["type"] == "context.append_loop_event":
            if not isinstance(record.get("event"), dict) or not string(record["event"].get("type")):
                raise SessionMigrateError("Kimi loop event is malformed")
            has_history = True
        elif record["type"] == "context.apply_compaction":
            if not (string(record.get("summary")) or string(record.get("contextSummary"))):
                raise SessionMigrateError("Kimi compaction record has no summary")
            has_history = True
    if not has_history:
        raise SessionMigrateError("Kimi wire journal has no resumable conversation history")


def _validate_context_message(message: Any) -> None:
    if not isinstance(message, dict) or message.get("role") not in {
        "system",
        "user",
        "assistant",
        "tool",
    }:
        raise SessionMigrateError("Kimi context message has an invalid role")
    if not isinstance(message.get("content"), list) or not isinstance(
        message.get("toolCalls"), list
    ):
        raise SessionMigrateError("Kimi context message is missing content or tool calls")
    for part in message["content"]:
        if not isinstance(part, dict) or part.get("type") not in {
            "text",
            "think",
            "image_url",
            "audio_url",
            "video_url",
        }:
            raise SessionMigrateError("Kimi context message contains an invalid content part")
    for call in message["toolCalls"]:
        if (
            not isinstance(call, dict)
            or call.get("type") != "function"
            or not string(call.get("id"))
            or not string(call.get("name"))
            or not isinstance(call.get("arguments"), (str, type(None)))
        ):
            raise SessionMigrateError("Kimi context message contains an invalid tool call")


def _context_message_events(record: dict[str, Any], index: int) -> list[Event]:
    message = record["message"]
    role_value = message["role"]
    origin = message.get("origin")
    origin_kind = origin.get("kind") if isinstance(origin, dict) else origin
    timestamp = _ms_timestamp(record["time"])
    if role_value in {"system"} or (role_value == "user" and origin_kind not in {None, "user"}):
        return [_opaque(index, record, f"kimi_{origin_kind or role_value}_message")]
    role = {"user": Role.USER, "assistant": Role.ASSISTANT, "tool": Role.TOOL}[role_value]
    if role == Role.TOOL:
        blocks: list[dict[str, Any]] = []
        texts: list[str] = []
        for part in message["content"]:
            if part["type"] == "text" and isinstance(part.get("text"), str):
                texts.append(part["text"])
                blocks.append({"type": "text", "text": part["text"]})
            elif part["type"] == "image_url":
                image = part.get("imageUrl")
                image_url = string(image.get("url")) if isinstance(image, dict) else None
                blocks.append(
                    {"type": "image", "image_url": image_url} if image_url else {"type": "opaque"}
                )
            else:
                blocks.append({"type": "opaque"})
        text = "\n".join(texts)
        return [
            Event(
                kind=EventKind.TOOL_RESULT,
                role=Role.TOOL,
                text=text or None,
                tool_name=string(message.get("name")),
                tool_call_id=string(message.get("toolCallId")),
                timestamp=timestamp,
                payload={
                    "content": text,
                    "content_blocks": blocks,
                    "is_error": message.get("isError") is True,
                },
                provenance=Provenance(index, record["type"]),
            )
        ]
    events: list[Event] = []
    for block_index, part in enumerate(message["content"]):
        provenance = Provenance(index, record["type"], block_index=block_index)
        if part["type"] == "text" and isinstance(part.get("text"), str):
            kind = EventKind.TOOL_RESULT if role == Role.TOOL else EventKind.MESSAGE
            events.append(
                Event(
                    kind=kind,
                    role=role,
                    text=part["text"],
                    tool_name=string(message.get("name")),
                    tool_call_id=string(message.get("toolCallId")),
                    timestamp=timestamp,
                    payload={"content": part["text"], "is_error": message.get("isError") is True}
                    if role == Role.TOOL
                    else {},
                    provenance=provenance,
                )
            )
        elif part["type"] == "think":
            events.append(
                Event(
                    kind=EventKind.THINKING,
                    role=Role.ASSISTANT,
                    text=string(part.get("think")),
                    timestamp=timestamp,
                    payload={
                        "encrypted_content": string(part.get("encrypted")),
                        "reason": "kimi_private_thinking",
                    },
                    provenance=provenance,
                )
            )
        elif part["type"] == "image_url":
            image = part.get("imageUrl")
            events.append(
                Event(
                    kind=EventKind.CONTEXT,
                    role=role,
                    timestamp=timestamp,
                    payload={
                        "block_type": "image",
                        "image_url": string(image.get("url")) if isinstance(image, dict) else None,
                    },
                    provenance=provenance,
                )
            )
        else:
            events.append(
                Event(
                    kind=EventKind.OPAQUE,
                    role=role,
                    timestamp=timestamp,
                    payload={"reason": f"kimi_{part['type']}_content"},
                    provenance=provenance,
                )
            )
    for call in message["toolCalls"]:
        arguments = call.get("arguments")
        try:
            parsed_args = json.loads(arguments) if isinstance(arguments, str) else {}
        except json.JSONDecodeError:
            parsed_args = arguments
        events.append(
            Event(
                kind=EventKind.TOOL_CALL,
                role=Role.ASSISTANT,
                tool_name=call["name"],
                tool_call_id=call["id"],
                timestamp=timestamp,
                payload={"input": parsed_args},
                provenance=Provenance(index, record["type"]),
            )
        )
    return events


def _loop_events(record: dict[str, Any], index: int) -> list[Event]:
    event = record["event"]
    event_type = event["type"]
    timestamp = _ms_timestamp(record["time"])
    provenance = Provenance(index, f"context.append_loop_event:{event_type}")
    if event_type == "content.part":
        part = event.get("part")
        if (
            isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        ):
            return [
                Event(
                    kind=EventKind.MESSAGE,
                    role=Role.ASSISTANT,
                    text=part["text"],
                    timestamp=timestamp,
                    provenance=provenance,
                )
            ]
        if isinstance(part, dict) and part.get("type") == "think":
            return [
                Event(
                    kind=EventKind.THINKING,
                    role=Role.ASSISTANT,
                    text=string(part.get("think")),
                    timestamp=timestamp,
                    payload={"reason": "kimi_private_thinking"},
                    provenance=provenance,
                )
            ]
    if event_type == "tool.call":
        return [
            Event(
                kind=EventKind.TOOL_CALL,
                role=Role.ASSISTANT,
                tool_name=string(event.get("name")),
                tool_call_id=string(event.get("toolCallId")),
                timestamp=timestamp,
                payload={"input": event.get("args", {})},
                provenance=provenance,
            )
        ]
    if event_type == "tool.result":
        result = event.get("result")
        output = result.get("output") if isinstance(result, dict) else result
        text = (
            output
            if isinstance(output, str)
            else json.dumps(output, ensure_ascii=False, default=str)
        )
        return [
            Event(
                kind=EventKind.TOOL_RESULT,
                role=Role.TOOL,
                text=text,
                tool_call_id=string(event.get("toolCallId")),
                timestamp=timestamp,
                payload={
                    "content": text,
                    "content_blocks": ([{"type": "text", "text": text}] if text else []),
                },
                provenance=provenance,
            )
        ]
    if event_type in {"step.begin", "step.end"}:
        return []
    return [_opaque(index, record, "kimi_unknown_loop_event")]


def _opaque(index: int, record: dict[str, Any], reason: str) -> Event:
    return Event(
        kind=EventKind.OPAQUE,
        timestamp=_ms_timestamp(record.get("time")),
        payload={"reason": reason},
        provenance=Provenance(index, string(record.get("type"))),
    )


def _omission_key(event: Event) -> str:
    if event.kind == EventKind.OPAQUE:
        reason = event.payload.get("reason")
        return f"opaque:{reason}" if isinstance(reason, str) and reason else "opaque"
    if event.kind == EventKind.THINKING:
        return "thinking:private"
    return event.kind.value


def _tool_result_content(event: Event, dropped: Counter[str]) -> tuple[str, list[dict[str, Any]]]:
    source = event.payload.get("content_blocks")
    blocks = source if isinstance(source, list) else []
    result: list[dict[str, Any]] = []
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
                result.append({"type": "text", "text": text})
            else:
                dropped["tool_result:malformed_text"] += 1
        elif block_type in {"image", "input_image"}:
            image = portable_data_image(block.get("image_url") or block.get("url"))
            if image:
                media_type, data = image
                result.append(
                    {
                        "type": "image_url",
                        "imageUrl": {"url": f"data:{media_type};base64,{data}"},
                    }
                )
            else:
                dropped["tool_result:image"] += 1
        else:
            dropped[f"tool_result:{block_type or 'unknown_block'}"] += 1
    content = event.payload.get("content")
    text = "\n".join(texts) or event.text or content_text(content)
    if not text and content not in (None, ""):
        dropped["tool_result:opaque"] += 1
        text = json.dumps(content, ensure_ascii=False, default=str)
    if text and not result:
        result.append({"type": "text", "text": text})
    return text or "", result


def _event_ms(event: Event | None, fallback: int, dropped: Counter[str]) -> int:
    if event is None or event.timestamp is None:
        return fallback
    valid = valid_rfc3339(event.timestamp)
    if valid is None:
        dropped["timestamp:invalid"] += 1
        return fallback
    return _timestamp_ms(valid)


def _timestamp_ms(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def _ms_timestamp(value: Any) -> str | None:
    if not isinstance(value, int) or value < 0:
        return None
    return datetime.fromtimestamp(value / 1000, UTC).isoformat().replace("+00:00", "Z")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
