"""Mistral Vibe 2.24.3 local-session adapter.

Vibe persists one resumable session as ``meta.json`` plus ``messages.jsonl``
inside ``$VIBE_HOME/logs/session/session_*_<short-id>``.  Conversion artifacts
use a small deterministic bundle so both native files can be validated before
the installer creates the final private directory.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
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

PINNED_VIBE_VERSION = "2.24.3"
VIBE_BUNDLE_SCHEMA = "session-migrate.vibe.v1"
META_FILENAME = "meta.json"
MESSAGES_FILENAME = "messages.jsonl"
MAX_BUNDLE_BYTES = DEFAULT_MAX_TOTAL_BYTES
MAX_MESSAGES = DEFAULT_MAX_RECORDS
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 1_000_000
VIBE_MESSAGE_FIELDS = {
    "role",
    "content",
    "images",
    "injected",
    "reasoning_content",
    "reasoning_payloads",
    "reasoning_message_id",
    "tool_calls",
    "name",
    "tool_call_id",
    "tool_result",
    "message_id",
    "user_display_content",
    "input_text",
    "resources",
    "manual_shell",
    "context_boundary",
}


@dataclass(frozen=True, slots=True)
class ParsedVibeBundle:
    meta: dict[str, Any]
    messages: tuple[dict[str, Any], ...]


def serialize(
    session: Session,
    *,
    session_id: str,
    cwd: Path,
    cli_version: str = PINNED_VIBE_VERSION,
    model: str | None = None,
    timestamp: str | None = None,
    title: str | None = None,
) -> tuple[bytes, dict[str, int]]:
    """Serialize portable history into a validated two-file Vibe bundle."""

    fallback_timestamp = valid_rfc3339(timestamp) or valid_rfc3339(session.started_at) or _utc_now()
    dropped: Counter[str] = Counter()
    messages: list[dict[str, Any]] = []
    seen_calls: set[str] = set()
    seen_results: set[str] = set()
    tool_names: dict[str, str] = {}
    pending: dict[str, Any] | None = None
    pending_source: int | None = None

    def flush() -> None:
        nonlocal pending, pending_source
        if pending is not None and _message_has_history(pending):
            messages.append(pending)
        pending = None
        pending_source = None

    def current_message(event: Event, role: str) -> dict[str, Any]:
        nonlocal pending, pending_source
        source_index = event.provenance.record_index
        if pending is None or pending.get("role") != role or pending_source != source_index:
            flush()
            pending = {
                "role": role,
                "content": "",
                "injected": False,
                "message_id": str(uuid.uuid4()),
            }
            pending_source = source_index
        return pending

    for event in session.events:
        if event.kind == EventKind.MESSAGE and event.role in {Role.USER, Role.ASSISTANT}:
            if not event.text:
                continue
            message = current_message(event, event.role.value)
            message["content"] = _join_text(string(message.get("content")), event.text)
            if event.payload.get("ui_only_projection") is True:
                dropped["message:ui_only_projection"] += 1
            continue

        if event.kind == EventKind.THINKING and event.role in {Role.ASSISTANT, None}:
            if not event.text or not (
                session.source_format == AgentFormat.VIBE
                and event.payload.get("source_readable_reasoning") is True
            ):
                dropped["thinking:private"] += 1
                continue
            message = current_message(event, "assistant")
            message["reasoning_content"] = _join_text(
                string(message.get("reasoning_content")), event.text
            )
            message.setdefault("reasoning_message_id", str(uuid.uuid4()))
            if event.payload.get("encrypted_content") or event.payload.get("signature"):
                dropped["thinking:provider_payload"] += 1
            continue

        if event.kind == EventKind.TOOL_CALL:
            call_id = event.tool_call_id
            if not call_id:
                call_id = f"call_session_migrate_{uuid.uuid4().hex}"
                dropped["tool_call:missing_id"] += 1
            tool_name = event.tool_name
            if not tool_name:
                tool_name = "unknown_tool"
                dropped["tool_call:missing_name"] += 1
            arguments = event.payload.get("input", {})
            if not isinstance(arguments, dict):
                arguments = {"input": arguments}
                dropped["tool_call:non_object_input"] += 1
            if call_id in seen_calls:
                dropped["tool_call:duplicate_id"] += 1
            seen_calls.add(call_id)
            tool_names.setdefault(call_id, tool_name)
            if event.payload.get("namespace"):
                dropped["tool_call:namespace"] += 1
            message = current_message(event, "assistant")
            calls = message.setdefault("tool_calls", [])
            assert isinstance(calls, list)
            calls.append(
                {
                    "id": call_id,
                    "index": len(calls),
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(
                            arguments, ensure_ascii=False, separators=(",", ":")
                        ),
                    },
                    "type": "function",
                }
            )
            continue

        if event.kind == EventKind.TOOL_RESULT:
            flush()
            call_id = event.tool_call_id
            if not call_id:
                call_id = f"call_missing_{uuid.uuid4().hex}"
                dropped["tool_result:missing_id"] += 1
            elif call_id not in seen_calls:
                dropped["tool_result:orphan_id"] += 1
            if event.tool_call_id and event.tool_call_id in seen_results:
                dropped["tool_result:duplicate_id"] += 1
            if event.tool_call_id:
                seen_results.add(event.tool_call_id)
            output = _portable_tool_output(event, dropped)
            text = event.text or content_text(event.payload.get("content"))
            if not text:
                text = json.dumps(output, ensure_ascii=False, separators=(",", ":"))
            native_text = (
                f"<tool_error>{text}</tool_error>"
                if event.payload.get("is_error") is True
                else text
            )
            messages.append(
                {
                    "role": "tool",
                    "content": native_text,
                    "injected": False,
                    "name": event.tool_name or tool_names.get(call_id) or "unknown_tool",
                    "tool_call_id": call_id,
                    "tool_result": {"output": output, "cancelled": False},
                }
            )
            continue

        if (
            event.kind == EventKind.CONTEXT
            and event.payload.get("block_type") == "image"
            and event.role == Role.USER
        ):
            image = portable_data_image(event.payload.get("image_url"))
            if image is None:
                dropped["context:image"] += 1
                continue
            media_type, encoded = image
            message = current_message(event, "user")
            images = message.setdefault("images", [])
            assert isinstance(images, list)
            images.append(
                {
                    "source": {"kind": "inline", "data": encoded},
                    "alias": f"image-{len(images) + 1}",
                    "mime_type": media_type,
                }
            )
            continue

        if event.kind == EventKind.COMPACTION and event.text:
            flush()
            messages.append(
                {
                    "role": "user",
                    "content": event.text,
                    "injected": True,
                    "message_id": str(uuid.uuid4()),
                    "context_boundary": "compaction",
                }
            )
            if event.payload.get("has_boundary_metadata") is True:
                dropped["compaction:boundary_metadata"] += 1
            if event.payload.get("replacement_history_expanded") is True:
                dropped["compaction:replacement_history_expanded"] += 1
            continue

        dropped[_omission_key(event)] += 1

    flush()
    if not any(message.get("role") in {"user", "assistant", "tool"} for message in messages):
        raise SessionMigrateError("conversion produced no resumable conversation history")

    meta = {
        "session_id": session_id,
        "parent_session_id": None,
        "start_time": fallback_timestamp,
        "end_time": fallback_timestamp,
        "git_commit": None,
        "git_branch": None,
        "environment": {"working_directory": str(cwd)},
        "username": "session-migrate",
        "child_sessions": [],
        "loops": [],
        "title": title or session.title,
        "title_source": "manual" if title or session.title else "auto",
        "total_messages": len(messages),
        "last_message_fingerprint": _message_fingerprint(messages[-1]),
        "tools_available": [],
        "config": {
            "writer": "session-migrate",
            "target_cli_version": cli_version,
            "model": model or session.model,
        },
        "agent_profile": None,
        "system_prompt": None,
        "import_provenance": {
            "source_format": session.source_format.value,
            "source_session_id": session.session_id,
        },
    }
    bundle = {
        "schema": VIBE_BUNDLE_SCHEMA,
        "meta": meta,
        "messages": messages,
    }
    data = (
        json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    validate_native_bytes(data, session_id)
    return data, dict(sorted(dropped.items()))


def parse(path: Path) -> Session:
    """Parse a native Vibe session directory or its ``messages.jsonl`` file."""

    session_dir, messages_path, meta_path = _source_paths(path)
    message_before = file_snapshot(messages_path)
    meta_before = file_snapshot(meta_path)
    records = [dict(record.value) for record in iter_jsonl(messages_path)]
    try:
        meta_bytes = meta_path.read_bytes()
    except OSError as exc:
        raise JsonlError(f"cannot read Vibe metadata: {exc.strerror or exc}") from exc
    if len(meta_bytes) > MAX_BUNDLE_BYTES:
        raise JsonlError("Vibe metadata exceeds the input safety limit")
    try:
        meta = json.loads(meta_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise JsonlError("Vibe meta.json is not valid JSON") from exc
    if not isinstance(meta, dict):
        raise JsonlError("Vibe meta.json is not a JSON object")
    _validate_json_shape(meta)
    session_id = _uuid_string(meta.get("session_id"), "Vibe metadata session_id")
    events: list[Event] = []
    system_prompt = string(meta.get("system_prompt"))
    if system_prompt:
        events.append(
            Event(
                kind=EventKind.MESSAGE,
                role=Role.SYSTEM,
                text=system_prompt,
                timestamp=valid_rfc3339(meta.get("start_time")),
                provenance=Provenance(-1, "meta.system_prompt"),
            )
        )
    for index, message in enumerate(records):
        events.extend(_parse_message(message, index, session_dir))
    digest = hashlib.sha256(meta_bytes + b"\0" + messages_path.read_bytes()).hexdigest()
    ensure_file_unchanged(messages_path, message_before)
    ensure_file_unchanged(meta_path, meta_before)
    environment = meta.get("environment")
    cwd_value = environment.get("working_directory") if isinstance(environment, dict) else None
    cwd = Path(cwd_value) if isinstance(cwd_value, str) and cwd_value else None
    config = meta.get("config")
    model, model_provider = _config_model_provider(config)
    version = string(config.get("target_cli_version")) if isinstance(config, dict) else None
    return Session(
        source_format=AgentFormat.VIBE,
        source_path=messages_path.resolve(),
        source_sha256=digest,
        session_id=session_id,
        cwd=cwd,
        started_at=valid_rfc3339(meta.get("start_time")),
        cli_version=version,
        model=model,
        title=string(meta.get("title")),
        events=tuple(events),
        raw_record_count=len(records),
        model_provider=model_provider,
    )


parse_session = parse


def _config_model_provider(config: Any) -> tuple[str | None, str | None]:
    """Read Vibe's active model/provider without assuming a Mistral backend."""

    if not isinstance(config, dict):
        return None, None
    generated_model = string(config.get("model"))
    active_alias = string(config.get("active_model"))
    models = config.get("models")
    if not active_alias or not isinstance(models, dict):
        return generated_model or active_alias, None
    active = models.get(active_alias)
    if not isinstance(active, dict):
        return active_alias, None
    return string(active.get("name")) or active_alias, string(active.get("provider"))


def validate_native_bytes(data: bytes, session_id: str) -> ParsedVibeBundle:
    """Validate a pre-install Vibe bundle without touching agent state."""

    if not data or len(data) > MAX_BUNDLE_BYTES:
        raise SessionMigrateError("generated Vibe bundle is empty or exceeds the safety limit")
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SessionMigrateError("generated Vibe bundle is not valid JSON") from exc
    if not isinstance(value, dict) or value.get("schema") != VIBE_BUNDLE_SCHEMA:
        raise SessionMigrateError("generated Vibe bundle has an unsupported schema")
    _validate_json_shape(value)
    meta = value.get("meta")
    messages = value.get("messages")
    if not isinstance(meta, dict) or not isinstance(messages, list):
        raise SessionMigrateError("generated Vibe bundle is missing meta or messages")
    if len(messages) > MAX_MESSAGES or not all(isinstance(item, dict) for item in messages):
        raise SessionMigrateError("generated Vibe bundle has invalid message records")
    if _uuid_string(meta.get("session_id"), "generated Vibe session_id") != session_id:
        raise SessionMigrateError("generated Vibe bundle session linkage is invalid")
    _validate_meta(meta)
    for index, message in enumerate(messages):
        _validate_message(message, index)
    if not messages or not any(
        message.get("role") in {"user", "assistant", "tool"} for message in messages
    ):
        raise SessionMigrateError("generated Vibe bundle has no resumable conversation history")
    if meta.get("total_messages") != len(messages):
        raise SessionMigrateError("generated Vibe metadata message count is inconsistent")
    expected = _message_fingerprint(messages[-1])
    if meta.get("last_message_fingerprint") != expected:
        raise SessionMigrateError("generated Vibe metadata fingerprint is inconsistent")
    return ParsedVibeBundle(meta=dict(meta), messages=tuple(dict(item) for item in messages))


def native_record_count(data: bytes) -> int:
    value = json.loads(data)
    messages = value.get("messages", []) if isinstance(value, dict) else []
    return len(messages) if isinstance(messages, list) else 0


def native_files(data: bytes, session_id: str) -> tuple[bytes, bytes]:
    """Return exact ``meta.json`` and ``messages.jsonl`` bytes for installation."""

    parsed = validate_native_bytes(data, session_id)
    meta_bytes = (
        json.dumps(parsed.meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()
    messages_bytes = encode_jsonl(parsed.messages)
    return meta_bytes, messages_bytes


def session_directory_name(session_id: str, timestamp: str) -> str:
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(UTC)
    return f"session_{parsed.strftime('%Y%m%d_%H%M%S')}_{session_id[:8]}"


def session_relative_path(session_id: str, timestamp: str) -> Path:
    return Path("logs/session") / session_directory_name(session_id, timestamp) / MESSAGES_FILENAME


def vibe_home(*, environ: dict[str, str] | None = None, home: Path | None = None) -> Path:
    values = os.environ if environ is None else environ
    configured = values.get("VIBE_HOME")
    return Path(configured).expanduser() if configured else (home or Path.home()) / ".vibe"


def _source_paths(path: Path) -> tuple[Path, Path, Path]:
    candidate = path.expanduser()
    session_dir = candidate if candidate.is_dir() else candidate.parent
    messages = session_dir / MESSAGES_FILENAME if candidate.is_dir() else candidate
    meta = session_dir / META_FILENAME
    if messages.name != MESSAGES_FILENAME or not messages.is_file() or not meta.is_file():
        raise SessionMigrateError(
            "Vibe source must be a session directory containing meta.json and messages.jsonl"
        )
    return session_dir, messages, meta


def _parse_message(message: dict[str, Any], index: int, session_dir: Path) -> list[Event]:
    _validate_message(message, index)
    role_name = str(message["role"])
    role = {
        "user": Role.USER,
        "assistant": Role.ASSISTANT,
        "tool": Role.TOOL,
        "system": Role.SYSTEM,
    }[role_name]
    provenance = Provenance(index, f"vibe.{role_name}", string(message.get("message_id")))
    if message.get("context_boundary") == "compaction":
        return [
            Event(
                kind=EventKind.COMPACTION,
                role=role,
                text=string(message.get("content")),
                payload={"source_boundary": "vibe"},
                provenance=provenance,
            )
        ]
    if message.get("injected") is True:
        return [
            Event(
                kind=EventKind.OPAQUE,
                role=role,
                payload={"reason": "vibe_injected_runtime_message"},
                provenance=provenance,
            )
        ]
    events: list[Event] = []
    reasoning = string(message.get("reasoning_content"))
    if reasoning:
        events.append(
            Event(
                kind=EventKind.THINKING,
                role=Role.ASSISTANT,
                text=reasoning,
                payload={"source_readable_reasoning": True},
                provenance=provenance,
            )
        )
    payloads = message.get("reasoning_payloads")
    if isinstance(payloads, list) and payloads:
        events.append(
            Event(
                kind=EventKind.OPAQUE,
                role=Role.ASSISTANT,
                payload={"reason": "vibe_reasoning_payloads"},
                provenance=provenance,
            )
        )
    content = string(message.get("content"))
    if role == Role.TOOL:
        result = message.get("tool_result")
        output = result.get("output") if isinstance(result, dict) else None
        payload = dict(output) if isinstance(output, dict) else {"content": output}
        text = string(payload.pop("session_migrate_text", None)) or content
        is_error = payload.pop("session_migrate_is_error", False) is True
        events.append(
            Event(
                kind=EventKind.TOOL_RESULT,
                role=Role.TOOL,
                text=text,
                tool_name=string(message.get("name")),
                tool_call_id=string(message.get("tool_call_id")),
                payload={**payload, "is_error": is_error},
                provenance=provenance,
            )
        )
        if isinstance(result, dict) and (
            result.get("duration") is not None
            or result.get("cancelled") is True
            or result.get("presentation") is not None
        ):
            events.append(
                Event(
                    kind=EventKind.OPAQUE,
                    role=Role.TOOL,
                    payload={"reason": "vibe_tool_result_runtime_metadata"},
                    provenance=provenance,
                )
            )
        events.extend(_runtime_metadata_events(message, index, role))
        return events
    if content:
        events.append(Event(kind=EventKind.MESSAGE, role=role, text=content, provenance=provenance))
    images = message.get("images")
    if isinstance(images, list):
        for block_index, image in enumerate(images):
            image_url = _image_url(image, session_dir)
            if image_url:
                events.append(
                    Event(
                        kind=EventKind.CONTEXT,
                        role=role,
                        payload={"block_type": "image", "image_url": image_url},
                        provenance=Provenance(
                            index,
                            f"vibe.{role_name}",
                            string(message.get("message_id")),
                            block_index,
                        ),
                    )
                )
            else:
                events.append(
                    Event(
                        kind=EventKind.OPAQUE,
                        role=role,
                        payload={"reason": "vibe_image_unreadable"},
                        provenance=Provenance(index, f"vibe.{role_name}", block_index=block_index),
                    )
                )
    calls = message.get("tool_calls")
    if isinstance(calls, list):
        for block_index, call in enumerate(calls):
            function = call.get("function") if isinstance(call, dict) else None
            raw_arguments = function.get("arguments") if isinstance(function, dict) else None
            try:
                arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else {}
            except json.JSONDecodeError:
                arguments = {"input": raw_arguments}
            events.append(
                Event(
                    kind=EventKind.TOOL_CALL,
                    role=Role.ASSISTANT,
                    tool_name=string(function.get("name")) if isinstance(function, dict) else None,
                    tool_call_id=string(call.get("id")) if isinstance(call, dict) else None,
                    payload={"input": arguments},
                    provenance=Provenance(index, "vibe.tool_call", block_index=block_index),
                )
            )
            if isinstance(call, dict) and call.get("presentation") is not None:
                events.append(
                    Event(
                        kind=EventKind.OPAQUE,
                        role=Role.ASSISTANT,
                        payload={"reason": "vibe_tool_call_presentation"},
                        provenance=Provenance(index, "vibe.tool_call", block_index=block_index),
                    )
                )
    events.extend(_runtime_metadata_events(message, index, role))
    return events


def _runtime_metadata_events(message: dict[str, Any], index: int, role: Role) -> list[Event]:
    events: list[Event] = []
    reasons = {
        "user_display_content": "vibe_user_display_content",
        "input_text": "vibe_input_text",
        "resources": "vibe_resources",
        "manual_shell": "vibe_manual_shell",
    }
    for field, reason in reasons.items():
        if message.get(field) is not None:
            events.append(
                Event(
                    kind=EventKind.OPAQUE,
                    role=role,
                    payload={"reason": reason},
                    provenance=Provenance(index, f"vibe.{field}"),
                )
            )
    if message.get("name") is not None and role != Role.TOOL:
        events.append(
            Event(
                kind=EventKind.OPAQUE,
                role=role,
                payload={"reason": "vibe_message_name"},
                provenance=Provenance(index, "vibe.name"),
            )
        )
    for field in sorted(message.keys() - VIBE_MESSAGE_FIELDS):
        events.append(
            Event(
                kind=EventKind.OPAQUE,
                role=role,
                payload={"reason": "vibe_unknown_message_field", "source_field": field},
                provenance=Provenance(index, f"vibe.unknown.{field}"),
            )
        )
    return events


def _validate_meta(meta: dict[str, Any]) -> None:
    required = {
        "session_id": str,
        "start_time": str,
        "environment": dict,
        "username": str,
        "child_sessions": list,
        "loops": list,
    }
    for key, expected_type in required.items():
        if not isinstance(meta.get(key), expected_type):
            raise SessionMigrateError(f"generated Vibe metadata field {key!r} is invalid")
    if not valid_rfc3339(meta.get("start_time")):
        raise SessionMigrateError("generated Vibe start_time is invalid")
    environment = meta["environment"]
    if not isinstance(environment.get("working_directory"), str):
        raise SessionMigrateError("generated Vibe working directory is invalid")


def _validate_message(message: dict[str, Any], index: int) -> None:
    role = message.get("role")
    if role not in {"system", "user", "assistant", "tool"}:
        raise SessionMigrateError(f"Vibe message {index} has an invalid role")
    if not isinstance(message.get("content", ""), str):
        raise SessionMigrateError(f"Vibe message {index} has invalid content")
    if role != "tool" and not isinstance(message.get("message_id"), str):
        raise SessionMigrateError(f"Vibe message {index} is missing message_id")
    if role == "tool" and not isinstance(message.get("tool_call_id"), str):
        raise SessionMigrateError(f"Vibe tool result {index} is missing tool_call_id")
    calls = message.get("tool_calls")
    if calls is not None:
        if not isinstance(calls, list):
            raise SessionMigrateError(f"Vibe message {index} has invalid tool_calls")
        for call in calls:
            function = call.get("function") if isinstance(call, dict) else None
            if (
                not isinstance(call, dict)
                or not isinstance(call.get("id"), str)
                or not isinstance(function, dict)
                or not isinstance(function.get("name"), str)
                or not isinstance(function.get("arguments"), str)
            ):
                raise SessionMigrateError(f"Vibe message {index} has a malformed tool call")
    images = message.get("images")
    if images is not None and (
        not isinstance(images, list) or not all(isinstance(item, dict) for item in images)
    ):
        raise SessionMigrateError(f"Vibe message {index} has malformed images")
    _validate_json_shape(message)


def _image_url(value: Any, session_dir: Path) -> str | None:
    if not isinstance(value, dict):
        return None
    mime_type = string(value.get("mime_type"))
    source = value.get("source")
    if not mime_type or not isinstance(source, dict):
        return None
    if source.get("kind") == "inline":
        data = string(source.get("data"))
        candidate = f"data:{mime_type};base64,{data}" if data else None
        return candidate if portable_data_image(candidate) else None
    if source.get("kind") != "file" or not isinstance(source.get("path"), str):
        return None
    path = Path(source["path"]).expanduser()
    if not path.is_absolute():
        path = session_dir / path
    try:
        before = file_snapshot(path)
        if before.size > 32 * 1024 * 1024 or not path.is_file():
            return None
        data = path.read_bytes()
        ensure_file_unchanged(path, before)
    except (OSError, JsonlError):
        return None
    candidate = f"data:{mime_type};base64,{base64.b64encode(data).decode()}"
    return candidate if portable_data_image(candidate) else None


def _portable_tool_output(event: Event, dropped: Counter[str]) -> dict[str, Any]:
    output: dict[str, Any] = {
        "session_migrate_text": event.text or "",
        "session_migrate_is_error": event.payload.get("is_error") is True,
    }
    blocks = event.payload.get("content_blocks")
    if isinstance(blocks, list):
        output["content_blocks"] = blocks
    content = event.payload.get("content", event.text or "")
    if isinstance(content, dict):
        output["content"] = content
        return output
    if isinstance(content, (str, int, float, bool)) or content is None:
        output["content"] = content
        return output
    if isinstance(content, list):
        output["content"] = content
        return output
    dropped["tool_result:opaque"] += 1
    output["content"] = str(content)
    return output


def _message_has_history(message: dict[str, Any]) -> bool:
    return bool(
        string(message.get("content"))
        or string(message.get("reasoning_content"))
        or message.get("tool_calls")
        or message.get("images")
    )


def _message_fingerprint(message: dict[str, Any]) -> str:
    # Match Vibe 2.24.3's exact ``LLMMessage.model_dump(exclude_none=True,
    # mode="json")`` boundary fingerprint. Pydantic materializes these false
    # defaults before SessionLogger decides whether it can append in place.
    normalized = dict(message)
    normalized.setdefault("content", "")
    normalized.setdefault("injected", False)
    tool_result = normalized.get("tool_result")
    if isinstance(tool_result, dict):
        normalized["tool_result"] = {
            **tool_result,
            "cancelled": tool_result.get("cancelled", False),
        }
    encoded = json.dumps(normalized, sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _join_text(before: str | None, after: str) -> str:
    return f"{before}\n{after}" if before else after


def _uuid_string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise SessionMigrateError(f"{label} is missing")
    try:
        normalized = str(uuid.UUID(value))
    except ValueError as exc:
        raise SessionMigrateError(f"{label} is not a valid UUID") from exc
    if normalized != value.lower():
        raise SessionMigrateError(f"{label} is not canonical")
    return normalized


def _validate_json_shape(value: Any) -> None:
    nodes = 0
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise SessionMigrateError("Vibe JSON exceeds structural safety limits")
        if isinstance(current, dict):
            if not all(isinstance(key, str) for key in current):
                raise SessionMigrateError("Vibe JSON contains a non-string object key")
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
        elif not isinstance(current, (str, int, float, bool, type(None))):
            raise SessionMigrateError("Vibe JSON contains an unsupported value")


def _omission_key(event: Event) -> str:
    if event.kind == EventKind.MESSAGE and event.role not in {Role.USER, Role.ASSISTANT}:
        return "message:privileged_role"
    if event.kind == EventKind.CONTEXT:
        return "context:privileged_image" if event.role != Role.USER else "context"
    if event.kind == EventKind.OPAQUE:
        reason = string(event.payload.get("reason"))
        return f"opaque:{reason}" if reason else "opaque"
    return event.kind.value


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
