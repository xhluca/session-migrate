"""Muse Code 0.2.1 durable session event-stream adapter."""

from __future__ import annotations

import json
import uuid
from collections import Counter
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from session_migrate.errors import SessionMigrateError
from session_migrate.formats.common import content_text, string, valid_rfc3339
from session_migrate.jsonl import DEFAULT_MAX_TOTAL_BYTES, encode_jsonl, file_sha256, iter_jsonl
from session_migrate.model import AgentFormat, Event, EventKind, Provenance, Role, Session

PINNED_MUSE_VERSION = "0.2.1"
PINNED_MUSE_BUILD_SHA = "b3170a534f"
MAX_NATIVE_BYTES = DEFAULT_MAX_TOTAL_BYTES


def serialize(
    session: Session,
    *,
    session_id: str,
    cwd: Path,
    cli_version: str = PINNED_MUSE_VERSION,
    model: str | None = None,
    provider: str | None = None,
    timestamp: str | None = None,
) -> tuple[bytes, dict[str, int]]:
    """Serialize portable history as Muse's durable session events."""

    fallback = valid_rfc3339(timestamp) or valid_rfc3339(session.started_at) or _utc_now()
    recorded_at = _timestamp_us(fallback)
    records: list[dict[str, Any]] = []
    dropped: Counter[str] = Counter()
    run_id: str | None = None
    source_run_id: str | None = None
    source_run_sequence = 0
    seen_calls: set[str] = set()
    seen_results: set[str] = set()

    def append(payload_type: str, payload: dict[str, Any], schema: int = 1) -> dict[str, Any]:
        nonlocal recorded_at
        recorded_at += 1
        records.append(
            {
                "causation_id": None,
                "durability": "durable",
                "id": str(uuid.uuid4()),
                "payload": payload,
                "payload_schema_version": schema,
                "payload_type": payload_type,
                "record_type": "event",
                "recorded_at": recorded_at,
                "schema_version": 1,
                "sequence": len(records) + 1,
                "stream": {"id": session_id, "kind": "session"},
            }
        )
        return records[-1]

    def end_run() -> None:
        nonlocal run_id, source_run_id
        if run_id is None or source_run_id is None:
            return
        append(
            "runtime.session",
            {
                "event": {
                    "eot_gate_ms": 0,
                    "kind": "terminal",
                    "reason": None,
                    "terminal": "completed",
                    "time_to_first_token_ms": 0,
                    "turn_duration_ms": 0,
                },
                "kind": "run",
                "run_id": run_id,
                "source_run_record_id": source_run_id,
                "source_run_record_sequence": source_run_sequence,
            },
        )
        run_id = None
        source_run_id = None

    def start_run(event: Event) -> None:
        nonlocal run_id, source_run_id, source_run_sequence
        end_run()
        intent_id = str(uuid.uuid4())
        accepted = append(
            "runtime.user_intent.accepted",
            {
                "bindings": [],
                "delivery_policy": "session_current",
                "intent_id": intent_id,
                "model_messages": [{"content": [{"kind": "text", "text": event.text}]}],
                "refill_blocks": [{"kind": "text", "text": event.text}],
                "semantic_kind": {"kind": "chat"},
                "source_session_id": session_id,
                "surface": "main",
                "wake_policy": "start_once",
            },
        )
        run_id = str(uuid.uuid4())
        source_run_id = str(uuid.uuid4())
        source_run_sequence += 1
        started = append(
            "runtime.session",
            {
                "event": {"kind": "started", "prompt": event.text},
                "kind": "run",
                "run_id": run_id,
                "source_run_record_id": source_run_id,
                "source_run_record_sequence": source_run_sequence,
            },
        )
        append(
            "runtime.user_intent.materialized",
            {
                "envelope_record_id": accepted["id"],
                "envelope_sequence": accepted["sequence"],
                "intent_id": intent_id,
                "outcome": {
                    "cut_before": f"session-migrate:{source_run_sequence}",
                    "kind": "top_level_turn_started",
                    "run_id": run_id,
                    "run_started_session_record_id": started["id"],
                    "run_started_source_record_id": source_run_id,
                },
                "source_session_id": session_id,
            },
        )

    append(
        "runtime.session.metadata",
        {
            "kind": "metadata",
            "record": {
                "build": {"semver": cli_version, "sha": PINNED_MUSE_BUILD_SHA},
                "model_id": model or session.model or "unknown",
                "provider_id": provider or session.model_provider or "openrouter",
                "tool_surface_version": "1",
                "web_search_mode": "off",
                "workspace_root": str(cwd),
            },
        },
    )
    append(
        "session.opened.observed",
        {
            "kind": "session_opened",
            "record": {
                "data_dir_free_bytes": 0,
                "resume": True,
                "schema_version": 1,
                "security_mode": "normal",
                "session_id": session_id,
                "workspace_free_bytes": 0,
            },
        },
    )
    for event in session.events:
        if event.kind == EventKind.MESSAGE and event.role == Role.USER and event.text:
            start_run(event)
            continue
        if event.kind == EventKind.MESSAGE and event.role == Role.ASSISTANT and event.text:
            if run_id is None or source_run_id is None:
                dropped["message:assistant_without_user_turn"] += 1
                continue
            message_id = str(uuid.uuid4())
            response_id = str(uuid.uuid4())
            append(
                "runtime.session",
                {
                    "event": {
                        "kind": "assistant_message_committed",
                        "message_id": message_id,
                        "provider_item_id": message_id,
                        "response_id": response_id,
                        "text": event.text,
                    },
                    "kind": "run",
                    "run_id": run_id,
                    "source_run_record_id": source_run_id,
                    "source_run_record_sequence": source_run_sequence,
                },
            )
            if event.payload.get("ui_only_projection") is True:
                dropped["message:ui_only_projection"] += 1
            continue
        if event.kind == EventKind.TOOL_CALL:
            if run_id is None or source_run_id is None:
                dropped["tool_call:without_user_turn"] += 1
                continue
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
            if event.payload.get("namespace"):
                dropped["tool_call:namespace"] += 1
            message_id = str(uuid.uuid4())
            response_id = str(uuid.uuid4())
            append(
                "runtime.session",
                {
                    "event": {
                        "kind": "assistant_tool_calls_committed",
                        "message_id": message_id,
                        "response_id": response_id,
                        "tool_calls": [
                            {
                                "args": json.dumps(args, ensure_ascii=False, separators=(",", ":")),
                                "call_id": call_id,
                                "id": call_id,
                                "name": name,
                            }
                        ],
                    },
                    "kind": "run",
                    "run_id": run_id,
                    "source_run_record_id": source_run_id,
                    "source_run_record_sequence": source_run_sequence,
                },
            )
            continue
        if event.kind == EventKind.TOOL_RESULT:
            if run_id is None:
                dropped["tool_result:without_user_turn"] += 1
                continue
            call_id = event.tool_call_id or f"call_missing_{uuid.uuid4().hex}"
            if not event.tool_call_id:
                dropped["tool_result:missing_id"] += 1
            elif call_id not in seen_calls:
                dropped["tool_result:orphan_id"] += 1
            if event.tool_call_id in seen_results:
                dropped["tool_result:duplicate_id"] += 1
            if event.tool_call_id:
                seen_results.add(event.tool_call_id)
            output = _tool_result_text(event, dropped)
            append(
                "runtime.session",
                {
                    "event": {
                        "batch_id": str(uuid.uuid4()),
                        "kind": "tool_result_batch_committed",
                        "results": [
                            {
                                "text": output or "",
                                "tool_call_id": call_id,
                                "tool_call_index": 0,
                            }
                        ],
                    },
                    "kind": "run",
                    "run_id": run_id,
                },
            )
            if event.payload.get("is_error") is True:
                dropped["tool_result:error_flag"] += 1
            continue
        dropped[_omission_key(event)] += 1
    end_run()
    if len(records) <= 2:
        raise SessionMigrateError("conversion produced no resumable conversation history")
    append(
        "session.end",
        {
            "kind": "session_end",
            "record": {
                "exit_reason": "completed",
                "resource_usage": {},
                "schema_version": 1,
                "session_id": session_id,
                "uptime_ms": 0,
            },
        },
    )
    data = encode_jsonl(records)
    validate_native_bytes(data, session_id)
    return data, dict(sorted(dropped.items()))


def parse_session(path: Path) -> Session:
    records = [dict(item.value) for item in iter_jsonl(path)]
    _validate_records(records)
    metadata = records[0]["payload"]["record"]
    session_id = records[0]["stream"]["id"]
    events: list[Event] = []
    for index, record in enumerate(records):
        if _is_retained_marker(record):
            omission = record["omitted_record"]
            omission_class = string(omission.get("omission_class")) or "unknown"
            events.append(_opaque(index, record, f"muse_retained_marker:{omission_class}"))
            continue
        payload_type = record["payload_type"]
        payload = record["payload"]
        timestamp = _us_timestamp(record["recorded_at"])
        provenance = Provenance(index, payload_type, record["id"])
        if payload_type == "runtime.user_intent.accepted":
            for message in payload.get("model_messages", []):
                if not isinstance(message, dict):
                    continue
                for content in message.get("content", []):
                    if (
                        isinstance(content, dict)
                        and content.get("kind") == "text"
                        and string(content.get("text"))
                    ):
                        events.append(
                            Event(
                                kind=EventKind.MESSAGE,
                                role=Role.USER,
                                text=content["text"],
                                timestamp=timestamp,
                                provenance=provenance,
                            )
                        )
            continue
        if payload_type == "runtime.session" and payload.get("kind") == "run":
            run_event = payload.get("event")
            if not isinstance(run_event, dict):
                events.append(_opaque(index, record, "muse_malformed_run_event"))
                continue
            kind = run_event.get("kind")
            if kind == "assistant_message_committed" and string(run_event.get("text")):
                events.append(
                    Event(
                        kind=EventKind.MESSAGE,
                        role=Role.ASSISTANT,
                        text=run_event["text"],
                        timestamp=timestamp,
                        provenance=provenance,
                    )
                )
            elif kind == "reasoning_committed":
                events.append(
                    Event(
                        kind=EventKind.THINKING,
                        role=Role.ASSISTANT,
                        text=string(run_event.get("text")),
                        timestamp=timestamp,
                        payload={"reason": "muse_private_thinking"},
                        provenance=provenance,
                    )
                )
            elif kind == "assistant_tool_calls_committed":
                for call in run_event.get("tool_calls", []):
                    if not isinstance(call, dict):
                        continue
                    arguments = call.get("args", {})
                    if isinstance(arguments, str):
                        with suppress(json.JSONDecodeError):
                            arguments = json.loads(arguments)
                    events.append(
                        Event(
                            kind=EventKind.TOOL_CALL,
                            role=Role.ASSISTANT,
                            tool_name=string(call.get("name")),
                            tool_call_id=string(call.get("call_id")) or string(call.get("id")),
                            timestamp=timestamp,
                            payload={"input": arguments},
                            provenance=provenance,
                        )
                    )
            elif kind == "tool_result_batch_committed":
                for result in run_event.get("results", []):
                    if isinstance(result, dict):
                        text = string(result.get("text"))
                        events.append(
                            Event(
                                kind=EventKind.TOOL_RESULT,
                                role=Role.TOOL,
                                text=text,
                                tool_call_id=string(result.get("tool_call_id")),
                                timestamp=timestamp,
                                payload={
                                    "content": text or "",
                                    "content_blocks": (
                                        [{"type": "text", "text": text}] if text else []
                                    ),
                                },
                                provenance=provenance,
                            )
                        )
            elif kind not in {"started", "model_completed", "terminal"}:
                events.append(_opaque(index, record, f"muse_run:{kind or 'unknown'}"))
            continue
        if payload_type not in {
            "runtime.session.metadata",
            "session.opened.observed",
            "session.end",
        }:
            events.append(_opaque(index, record, f"muse_native:{payload_type}"))
    build = metadata.get("build")
    return Session(
        source_format=AgentFormat.MUSE,
        source_path=path.resolve(),
        source_sha256=file_sha256(path),
        session_id=session_id,
        cwd=Path(metadata["workspace_root"]),
        started_at=_us_timestamp(records[0]["recorded_at"]),
        cli_version=string(build.get("semver")) if isinstance(build, dict) else None,
        model=string(metadata.get("model_id")),
        title=None,
        events=tuple(events),
        raw_record_count=len(records),
        model_provider=string(metadata.get("provider_id")),
    )


parse = parse_session


def validate_native_bytes(data: bytes, session_id: str) -> None:
    _validate_records(_decode(data), expected_session_id=session_id)


def session_relative_path(session_id: str, timestamp: str) -> Path:
    date = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(UTC)
    return Path("sessions") / date.strftime("%Y/%m/%d") / session_id / "session.jsonl"


def _decode(data: bytes) -> list[dict[str, Any]]:
    if not data or len(data) > MAX_NATIVE_BYTES:
        raise SessionMigrateError("Muse session is empty or exceeds the native safety limit")
    records: list[dict[str, Any]] = []
    try:
        for line in data.decode().split("\n"):
            if not line.strip():
                continue
            value = json.loads(line, parse_constant=_reject_constant)
            if not isinstance(value, dict):
                raise SessionMigrateError("Muse session record is not an object")
            records.append(value)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SessionMigrateError("Muse session is not valid UTF-8 JSONL") from exc
    return records


def _validate_records(
    records: list[dict[str, Any]], expected_session_id: str | None = None
) -> None:
    if not records:
        raise SessionMigrateError("Muse session is empty")
    stream_id: str | None = None
    ids: set[str] = set()
    has_history = False
    previous_recorded_at = -1
    for expected_sequence, record in enumerate(records, start=1):
        if _is_retained_marker(record):
            position = record["position"]
            if position.get("sequence") != expected_sequence:
                raise SessionMigrateError("Muse retained-marker sequence is not contiguous")
            marker_id = _uuid(position.get("id"), "Muse retained-marker id")
            if marker_id in ids:
                raise SessionMigrateError("Muse session contains a duplicate event id")
            ids.add(marker_id)
            stream = record["stream"]
            current_id = _uuid(stream.get("id"), "Muse stream id")
            stream_id = stream_id or current_id
            if current_id != stream_id or (
                expected_session_id and current_id != expected_session_id
            ):
                raise SessionMigrateError("Muse event stream linkage is inconsistent")
            continue
        if (
            record.get("record_type") != "event"
            or record.get("schema_version") != 1
            or record.get("durability") != "durable"
        ):
            raise SessionMigrateError("Muse event envelope is unsupported")
        if record.get("sequence") != expected_sequence:
            raise SessionMigrateError("Muse event sequence is not contiguous")
        record_id = _uuid(record.get("id"), "Muse record id")
        if record_id in ids:
            raise SessionMigrateError("Muse session contains a duplicate event id")
        ids.add(record_id)
        recorded_at = record.get("recorded_at")
        if not isinstance(recorded_at, int) or recorded_at <= previous_recorded_at:
            raise SessionMigrateError("Muse event timestamps are not strictly increasing")
        previous_recorded_at = recorded_at
        stream = record.get("stream")
        if not isinstance(stream, dict) or stream.get("kind") != "session":
            raise SessionMigrateError("Muse event has invalid stream metadata")
        current_id = _uuid(stream.get("id"), "Muse stream id")
        stream_id = stream_id or current_id
        if current_id != stream_id or (expected_session_id and current_id != expected_session_id):
            raise SessionMigrateError("Muse event stream linkage is inconsistent")
        if (
            not string(record.get("payload_type"))
            or not isinstance(record.get("payload_schema_version"), int)
            or not isinstance(record.get("payload"), dict)
        ):
            raise SessionMigrateError("Muse event payload metadata is malformed")
        if record["payload_type"] in {"runtime.user_intent.accepted", "runtime.session"}:
            has_history = True
    first = records[0]
    if first.get("payload_type") != "runtime.session.metadata":
        raise SessionMigrateError("Muse session does not begin with canonical metadata")
    metadata = first["payload"]
    record = metadata.get("record") if metadata.get("kind") == "metadata" else None
    if not isinstance(record, dict) or not string(record.get("workspace_root")):
        raise SessionMigrateError("Muse session metadata is malformed")
    if not has_history:
        raise SessionMigrateError("Muse session has no resumable conversation history")
    _validate_native_turn_semantics(records)


def _validate_native_turn_semantics(records: list[dict[str, Any]]) -> None:
    """Validate the native lifecycle fields Muse uses to rebuild model context."""

    accepted: dict[str, tuple[str, int]] = {}
    started: dict[str, tuple[str, str]] = {}
    for record in records:
        if _is_retained_marker(record):
            continue
        payload_type = record["payload_type"]
        payload = record["payload"]
        if payload_type == "session.opened.observed":
            observed = payload.get("record")
            if not isinstance(observed, dict) or observed.get("security_mode") not in {
                "normal",
                "approval_disabled",
                "sandbox_disabled",
                "yolo",
            }:
                raise SessionMigrateError("Muse session has an invalid security mode")
            continue
        if payload_type == "runtime.user_intent.accepted":
            intent_id = _uuid(payload.get("intent_id"), "Muse user intent id")
            model_messages = payload.get("model_messages")
            refill_blocks = payload.get("refill_blocks")
            if (
                not isinstance(model_messages, list)
                or not model_messages
                or not isinstance(refill_blocks, list)
                or not refill_blocks
            ):
                raise SessionMigrateError("Muse user intent has no native prompt/refill content")
            accepted[intent_id] = (record["id"], record["sequence"])
            continue
        if payload_type == "runtime.session" and payload.get("kind") == "run":
            event = payload.get("event")
            if not isinstance(event, dict):
                raise SessionMigrateError("Muse run event is malformed")
            run_id = _uuid(payload.get("run_id"), "Muse run id")
            if event.get("kind") == "started":
                source_id = _uuid(payload.get("source_run_record_id"), "Muse source run record id")
                started[run_id] = (record["id"], source_id)
            continue
        if payload_type != "runtime.user_intent.materialized":
            continue
        outcome = payload.get("outcome")
        if not isinstance(outcome, dict) or outcome.get("kind") != "top_level_turn_started":
            continue
        intent_id = _uuid(payload.get("intent_id"), "Muse materialized intent id")
        if accepted.get(intent_id) != (
            payload.get("envelope_record_id"),
            payload.get("envelope_sequence"),
        ):
            raise SessionMigrateError("Muse materialized intent envelope linkage is invalid")
        run_id = _uuid(outcome.get("run_id"), "Muse materialized run id")
        run_started = started.get(run_id)
        if run_started != (
            outcome.get("run_started_session_record_id"),
            outcome.get("run_started_source_record_id"),
        ):
            raise SessionMigrateError("Muse materialized run linkage is invalid")


def _opaque(index: int, record: dict[str, Any], reason: str) -> Event:
    position = record.get("position")
    return Event(
        kind=EventKind.OPAQUE,
        timestamp=_us_timestamp(record.get("recorded_at")),
        payload={"reason": reason},
        provenance=Provenance(
            index,
            string(record.get("payload_type")),
            string(record.get("id"))
            or (string(position.get("id")) if isinstance(position, dict) else None),
        ),
    )


def _is_retained_marker(record: dict[str, Any]) -> bool:
    """Recognize Muse's durable placeholder for an omitted ephemeral status record."""

    position = record.get("position")
    stream = record.get("stream")
    omitted = record.get("omitted_record")
    return (
        record.get("schema_version") == 1
        and isinstance(record.get("retained_marker"), str)
        and isinstance(position, dict)
        and set(position) == {"id", "sequence"}
        and isinstance(position.get("sequence"), int)
        and isinstance(stream, dict)
        and stream.get("kind") == "session"
        and isinstance(omitted, dict)
        and omitted.get("record_type") == "status"
        and omitted.get("durability") == "ephemeral"
        and string(omitted.get("payload_type")) is not None
        and isinstance(omitted.get("payload_schema_version"), int)
        and string(omitted.get("payload_kind")) is not None
        and string(omitted.get("omission_class")) is not None
    )


def _omission_key(event: Event) -> str:
    if event.kind == EventKind.OPAQUE:
        reason = event.payload.get("reason")
        return f"opaque:{reason}" if isinstance(reason, str) and reason else "opaque"
    if event.kind == EventKind.THINKING:
        return "thinking:private"
    return event.kind.value


def _tool_result_text(event: Event, dropped: Counter[str]) -> str:
    source = event.payload.get("content_blocks")
    if not isinstance(source, list):
        return event.text or content_text(event.payload.get("content"))
    texts: list[str] = []
    for block in source:
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
            dropped["tool_result:image"] += 1
        else:
            dropped[f"tool_result:{block_type or 'unknown_block'}"] += 1
    if not texts and event.text:
        texts.append(event.text)
    return "\n".join(texts)


def _uuid(value: Any, label: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise SessionMigrateError(f"{label} is not a UUID") from exc


def _timestamp_us(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1_000_000)


def _us_timestamp(value: Any) -> str | None:
    if not isinstance(value, int) or value < 0:
        return None
    return datetime.fromtimestamp(value / 1_000_000, UTC).isoformat().replace("+00:00", "Z")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _reject_constant(value: str) -> None:
    raise ValueError(f"unsupported JSON constant: {value}")
