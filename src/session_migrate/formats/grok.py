"""Grok 1.0.5 local ACP-update session adapter."""

from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from session_migrate.errors import JsonlError, SessionMigrateError
from session_migrate.formats.common import content_text, portable_data_image, string, valid_rfc3339
from session_migrate.jsonl import DEFAULT_MAX_RECORDS, DEFAULT_MAX_TOTAL_BYTES
from session_migrate.model import AgentFormat, Event, EventKind, Provenance, Role, Session

PINNED_GROK_VERSION = "1.0.5"
PINNED_GROK_LINUX_X64_BYTES = 166_854_368
PINNED_GROK_LINUX_X64_SHA256 = "9ba87444e1819e8f6104adbbf4676a870c204380aa5c3e1c38a926c4ea677238"
GROK_BUNDLE_SCHEMA = "session-migrate.grok.v1"
MAX_BUNDLE_BYTES = DEFAULT_MAX_TOTAL_BYTES
MAX_UPDATES = DEFAULT_MAX_RECORDS


@dataclass(frozen=True, slots=True)
class ParsedGrokBundle:
    summary: dict[str, Any]
    updates: tuple[dict[str, Any], ...]


def serialize(
    session: Session,
    *,
    session_id: str,
    cwd: Path,
    cli_version: str = PINNED_GROK_VERSION,
    model: str | None = None,
    timestamp: str | None = None,
    title: str | None = None,
) -> tuple[bytes, dict[str, int]]:
    """Serialize portable history into Grok's summary + ACP update contract."""

    canonical_id = _uuid(session_id, "Grok target session ID")
    started = valid_rfc3339(timestamp) or valid_rfc3339(session.started_at) or _utc_now()
    unix_timestamp = int(datetime.fromisoformat(started.replace("Z", "+00:00")).timestamp())
    updates: list[dict[str, Any]] = []
    dropped: Counter[str] = Counter()
    seen_calls: set[str] = set()
    seen_results: set[str] = set()
    tool_names: dict[str, str] = {}
    message_count = 0
    first_user = ""

    def append(update: dict[str, Any]) -> None:
        updates.append(
            {
                "timestamp": unix_timestamp,
                "method": "session/update",
                "params": {"sessionId": canonical_id, "update": update},
            }
        )

    for event in session.events:
        if event.kind == EventKind.MESSAGE and event.role in {Role.USER, Role.ASSISTANT}:
            if not event.text:
                continue
            kind = "user_message_chunk" if event.role == Role.USER else "agent_message_chunk"
            append({"sessionUpdate": kind, "content": {"type": "text", "text": event.text}})
            message_count += 1
            if event.role == Role.USER and not first_user:
                first_user = event.text
            if event.payload.get("ui_only_projection") is True:
                dropped["message:ui_only_projection"] += 1
            continue

        if event.kind == EventKind.CONTEXT and event.role == Role.USER:
            image = portable_data_image(event.payload.get("image_url"))
            if event.payload.get("block_type") != "image" or image is None:
                dropped["context:image"] += 1
                continue
            media_type, encoded = image
            append(
                {
                    "sessionUpdate": "user_message_chunk",
                    "content": {
                        "type": "image",
                        "data": encoded,
                        "mimeType": media_type,
                        "uri": f"data:{media_type};base64,{encoded}",
                    },
                }
            )
            continue

        if event.kind == EventKind.TOOL_CALL:
            call_id = event.tool_call_id or f"call_session_migrate_{uuid.uuid4().hex}"
            if not event.tool_call_id:
                dropped["tool_call:missing_id"] += 1
            if call_id in seen_calls:
                dropped["tool_call:duplicate_id"] += 1
            seen_calls.add(call_id)
            name = event.tool_name or "unknown_tool"
            if not event.tool_name:
                dropped["tool_call:missing_name"] += 1
            arguments = event.payload.get("input", {})
            if not isinstance(arguments, dict):
                arguments = {"input": arguments}
                dropped["tool_call:non_object_input"] += 1
            if event.payload.get("namespace"):
                dropped["tool_call:namespace"] += 1
            append(
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": call_id,
                    "title": name,
                    "kind": "other",
                    "status": "pending",
                    "rawInput": arguments,
                    "locations": [],
                }
            )
            tool_names.setdefault(call_id, name)
            continue

        if event.kind == EventKind.TOOL_RESULT:
            call_id = event.tool_call_id or f"call_missing_{uuid.uuid4().hex}"
            if not event.tool_call_id:
                dropped["tool_result:missing_id"] += 1
            elif call_id not in seen_calls:
                dropped["tool_result:orphan_id"] += 1
            if event.tool_call_id and call_id in seen_results:
                dropped["tool_result:duplicate_id"] += 1
            if event.tool_call_id:
                seen_results.add(call_id)
            result_text = event.text or content_text(event.payload.get("content")) or ""
            blocks = event.payload.get("content_blocks")
            if isinstance(blocks, list):
                unsupported = sum(
                    1
                    for block in blocks
                    if not isinstance(block, dict) or block.get("type") != "text"
                )
                if unsupported:
                    dropped["tool_result:non_text_content"] += unsupported
            append(
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": call_id,
                    "title": tool_names.get(call_id) or event.tool_name or "unknown_tool",
                    "status": "failed" if event.payload.get("is_error") is True else "completed",
                    "content": [
                        {
                            "type": "content",
                            "content": {"type": "text", "text": result_text},
                        }
                    ],
                    "rawOutput": {"session_migrate_text": result_text},
                }
            )
            continue

        if event.kind == EventKind.COMPACTION and event.text:
            append(
                {
                    "sessionUpdate": "user_message_chunk",
                    "content": {
                        "type": "text",
                        "text": f"[Imported conversation summary]\n{event.text}",
                    },
                }
            )
            dropped["compaction:flattened"] += 1
            if event.payload.get("has_boundary_metadata") is True:
                dropped["compaction:boundary_metadata"] += 1
            if event.payload.get("replacement_history_expanded") is True:
                dropped["compaction:replacement_history_expanded"] += 1
            continue

        if event.kind == EventKind.THINKING:
            dropped["thinking:private"] += 1
            if event.payload.get("encrypted_content") or event.payload.get("signature"):
                dropped["thinking:provider_payload"] += 1
            continue

        dropped[_omission_key(event)] += 1

    if not any(
        item["params"]["update"]["sessionUpdate"]
        in {"user_message_chunk", "agent_message_chunk", "tool_call"}
        for item in updates
    ):
        raise SessionMigrateError("conversion produced no resumable conversation history")
    summary = {
        "info": {"id": canonical_id, "cwd": str(cwd)},
        "session_summary": first_user[:500],
        "created_at": started,
        "updated_at": started,
        "num_messages": len(updates),
        "num_chat_messages": message_count,
        "current_model_id": model or session.model or "grok-build",
        "chat_format_version": 1,
        "last_active_at": started,
        "generated_title": title or session.title,
        "title_is_manual": bool(title or session.title),
    }
    bundle = {
        "schema": GROK_BUNDLE_SCHEMA,
        "cli_version": cli_version,
        "summary": summary,
        "updates": updates,
    }
    data = (
        json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    validate_native_bytes(data, canonical_id)
    return data, dict(sorted(dropped.items()))


def parse_session(path: Path) -> Session:
    """Parse a native Grok session directory, summary, or update log."""

    directory = _source_directory(path)
    summary_path = directory / "summary.json"
    updates_path = directory / "updates.jsonl"
    summary_bytes = _read_bounded(summary_path)
    updates_bytes = _read_bounded(updates_path)
    try:
        summary = json.loads(summary_bytes, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise JsonlError("Grok summary.json is not valid UTF-8 JSON") from exc
    if not isinstance(summary, dict):
        raise JsonlError("Grok summary.json is not a JSON object")
    info = summary.get("info")
    if not isinstance(info, dict):
        raise JsonlError("Grok summary is missing session info")
    session_id = _uuid(info.get("id"), "Grok source session ID")
    cwd_value = string(info.get("cwd"))
    if not cwd_value:
        raise JsonlError("Grok summary is missing its working directory")
    records = _decode_updates(updates_bytes, session_id)
    events: list[Event] = []
    for index, record in enumerate(records):
        events.extend(_parse_update(record, index))
    digest = hashlib.sha256(summary_bytes + b"\0" + updates_bytes).hexdigest()
    return Session(
        source_format=AgentFormat.GROK,
        source_path=directory.resolve(),
        source_sha256=digest,
        session_id=session_id,
        cwd=Path(cwd_value),
        started_at=valid_rfc3339(summary.get("created_at")),
        cli_version=PINNED_GROK_VERSION,
        model=string(summary.get("current_model_id")),
        title=string(summary.get("generated_title")) or string(summary.get("session_summary")),
        events=tuple(events),
        raw_record_count=len(records) + 1,
        model_provider="xai",
    )


parse = parse_session


def validate_native_bytes(data: bytes, session_id: str) -> ParsedGrokBundle:
    if not data or len(data) > MAX_BUNDLE_BYTES:
        raise SessionMigrateError("generated Grok bundle is empty or exceeds the safety limit")
    try:
        value = json.loads(data, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SessionMigrateError("generated Grok bundle is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict) or value.get("schema") != GROK_BUNDLE_SCHEMA:
        raise SessionMigrateError("generated Grok bundle has an unsupported schema")
    summary = value.get("summary")
    updates = value.get("updates")
    if not isinstance(summary, dict) or not isinstance(updates, list):
        raise SessionMigrateError("generated Grok bundle is missing summary or updates")
    info = summary.get("info")
    if not isinstance(info, dict) or _uuid(info.get("id"), "Grok summary ID") != _uuid(
        session_id, "Grok target session ID"
    ):
        raise SessionMigrateError("generated Grok bundle session linkage is invalid")
    if not string(info.get("cwd")) or not valid_rfc3339(summary.get("created_at")):
        raise SessionMigrateError("generated Grok summary metadata is invalid")
    if not isinstance(summary.get("num_messages"), int) or summary["num_messages"] != len(updates):
        raise SessionMigrateError("generated Grok summary count is inconsistent")
    if not updates or len(updates) > MAX_UPDATES:
        raise SessionMigrateError("generated Grok bundle has no resumable updates")
    encoded = b"".join(
        (json.dumps(item, separators=(",", ":")) + "\n").encode() for item in updates
    )
    _decode_updates(encoded, _uuid(session_id, "Grok target session ID"))
    return ParsedGrokBundle(dict(summary), tuple(dict(item) for item in updates))


def native_record_count(data: bytes) -> int:
    value = json.loads(data)
    updates = value.get("updates", []) if isinstance(value, dict) else []
    return 1 + len(updates) if isinstance(updates, list) else 0


def native_files(data: bytes, session_id: str) -> tuple[bytes, bytes]:
    parsed = validate_native_bytes(data, session_id)
    summary = (json.dumps(parsed.summary, ensure_ascii=False, indent=2) + "\n").encode()
    updates = b"".join(
        (json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        for item in parsed.updates
    )
    return summary, updates


def encode_cwd(cwd: Path) -> str:
    encoded = urllib.parse.quote(str(cwd), safe="")
    if len(encoded.encode()) > 255:
        raise SessionMigrateError("Grok target working directory is too long to encode safely")
    return encoded


def session_relative_path(cwd: Path, session_id: str) -> Path:
    return Path("sessions") / encode_cwd(cwd) / _uuid(session_id, "Grok target session ID")


def grok_home(*, environ: dict[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    configured = values.get("GROK_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".grok"


def _parse_update(record: dict[str, Any], index: int) -> list[Event]:
    update = record["params"]["update"]
    kind = update["sessionUpdate"]
    timestamp = datetime.fromtimestamp(record["timestamp"], UTC).isoformat().replace("+00:00", "Z")
    provenance = Provenance(index, f"grok.{kind}")
    if kind in {"user_message_chunk", "agent_message_chunk"}:
        role = Role.USER if kind.startswith("user") else Role.ASSISTANT
        content = update.get("content")
        if content.get("type") == "text":
            if role == Role.USER and content["text"].startswith(
                "[Imported conversation summary]\n"
            ):
                return [
                    Event(
                        EventKind.COMPACTION,
                        provenance,
                        role=Role.SYSTEM,
                        text=content["text"].removeprefix("[Imported conversation summary]\n"),
                        timestamp=timestamp,
                        payload={"source_subtype": "grok_imported_summary"},
                    )
                ]
            return [
                Event(
                    EventKind.MESSAGE,
                    provenance,
                    role=role,
                    text=content["text"],
                    timestamp=timestamp,
                )
            ]
        if content.get("type") == "image" and role == Role.USER:
            image_url = string(content.get("uri"))
            if not image_url:
                image_url = f"data:{content['mimeType']};base64,{content['data']}"
            return [
                Event(
                    EventKind.CONTEXT,
                    provenance,
                    role=role,
                    timestamp=timestamp,
                    payload={"block_type": "image", "image_url": image_url},
                )
            ]
    if kind == "agent_thought_chunk":
        return [
            Event(
                EventKind.OPAQUE,
                provenance,
                role=Role.ASSISTANT,
                timestamp=timestamp,
                payload={"reason": "grok_private_thinking"},
            )
        ]
    if kind == "tool_call":
        return [
            Event(
                EventKind.TOOL_CALL,
                provenance,
                role=Role.ASSISTANT,
                timestamp=timestamp,
                tool_name=string(update.get("title")),
                tool_call_id=string(update.get("toolCallId")),
                payload={"input": update.get("rawInput", {})},
            )
        ]
    if kind == "tool_call_update" and update.get("status") in {"completed", "failed"}:
        raw = update.get("rawOutput")
        text = raw.get("session_migrate_text") if isinstance(raw, dict) else None
        if not isinstance(text, str):
            text = _tool_update_text(update)
        return [
            Event(
                EventKind.TOOL_RESULT,
                provenance,
                role=Role.TOOL,
                timestamp=timestamp,
                tool_name=string(update.get("title")),
                tool_call_id=string(update.get("toolCallId")),
                text=text,
                payload={
                    "is_error": update.get("status") == "failed",
                    "content_blocks": [{"type": "text", "text": text}],
                },
            )
        ]
    return [
        Event(
            EventKind.OPAQUE,
            provenance,
            timestamp=timestamp,
            payload={"reason": f"grok_{kind}"},
        )
    ]


def _decode_updates(data: bytes, session_id: str) -> list[dict[str, Any]]:
    if len(data) > MAX_BUNDLE_BYTES:
        raise JsonlError("Grok updates.jsonl exceeds the input safety limit")
    records = []
    for line_number, line in enumerate(data.splitlines(), start=1):
        if not line.strip():
            continue
        if len(records) >= MAX_UPDATES:
            raise JsonlError("Grok update log exceeds the record limit")
        try:
            value = json.loads(line, object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise JsonlError(f"Grok update line {line_number} is not valid JSON") from exc
        if not isinstance(value, dict) or value.get("method") not in {
            "session/update",
            "_x.ai/session/update",
        }:
            raise JsonlError("Grok update envelope is malformed")
        params = value.get("params")
        update = params.get("update") if isinstance(params, dict) else None
        if (
            not isinstance(params, dict)
            or params.get("sessionId") != session_id
            or not isinstance(update, dict)
            or not string(update.get("sessionUpdate"))
            or not isinstance(value.get("timestamp"), int)
        ):
            raise JsonlError("Grok update linkage or metadata is invalid")
        _validate_update(update)
        records.append(value)
    if not records:
        raise JsonlError("Grok update log is empty")
    return records


def _validate_update(update: dict[str, Any]) -> None:
    kind = update["sessionUpdate"]
    if kind in {"user_message_chunk", "agent_message_chunk", "agent_thought_chunk"}:
        content = update.get("content")
        if not isinstance(content, dict) or content.get("type") not in {"text", "image"}:
            raise JsonlError("Grok message update is malformed")
        if content["type"] == "text" and not isinstance(content.get("text"), str):
            raise JsonlError("Grok text update is malformed")
        if content["type"] == "image" and (
            portable_data_image(content.get("uri")) is None
            and not (string(content.get("data")) and string(content.get("mimeType")))
        ):
            raise JsonlError("Grok image update is malformed")
    elif kind in {"tool_call", "tool_call_update"} and not string(update.get("toolCallId")):
        raise JsonlError("Grok tool update is malformed")


def _tool_update_text(update: dict[str, Any]) -> str:
    result = []
    for item in update.get("content", []) if isinstance(update.get("content"), list) else []:
        content = item.get("content") if isinstance(item, dict) else None
        if isinstance(content, dict) and content.get("type") == "text":
            result.append(str(content.get("text", "")))
    if result:
        return "".join(result)
    raw = update.get("rawOutput")
    return json.dumps(raw, ensure_ascii=False, separators=(",", ":")) if raw is not None else ""


def _source_directory(path: Path) -> Path:
    candidate = path.expanduser()
    directory = candidate if candidate.is_dir() else candidate.parent
    if not (directory / "summary.json").is_file() or not (directory / "updates.jsonl").is_file():
        raise SessionMigrateError(
            "Grok source must be a session directory containing summary.json and updates.jsonl"
        )
    return directory


def _read_bounded(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise JsonlError("Grok source path is not a regular file")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise JsonlError(f"cannot read Grok session file: {exc.strerror or exc}") from exc
    if not data or len(data) > MAX_BUNDLE_BYTES:
        raise JsonlError("Grok session file is empty or exceeds the input safety limit")
    return data


def _uuid(value: Any, label: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise SessionMigrateError(f"{label} is not a valid UUID") from exc


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _omission_key(event: Event) -> str:
    if event.kind == EventKind.OPAQUE:
        return string(event.payload.get("reason")) or "opaque"
    return event.kind.value


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
