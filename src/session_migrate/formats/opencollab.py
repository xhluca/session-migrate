"""OpenCollab session-snapshot writer.

OpenCollab (https://github.com/RISE-X-Lab/OpenCollab) persists one resumable
snapshot per agent as a JSON document whose ``messages`` list holds bare
OpenAI chat-completions turns. ``opencollab --session <file>`` and
``Session.restore(path)`` accept a message-only snapshot and rebuild clean
runtime counters around it, which makes the format a natural target:
the adapter emits exactly that document and lets the native CLI own the rest.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from session_migrate.errors import SessionMigrateError
from session_migrate.formats.common import content_text, portable_data_image, string
from session_migrate.jsonl import DEFAULT_MAX_RECORDS, DEFAULT_MAX_TOTAL_BYTES
from session_migrate.model import Event, EventKind, Role, Session

PINNED_OPENCOLLAB_VERSION = "0.9.3"

SNAPSHOT_VERSION = 1
MAX_SNAPSHOT_BYTES = DEFAULT_MAX_TOTAL_BYTES
MAX_MESSAGES = DEFAULT_MAX_RECORDS
ALLOWED_ROLES = frozenset({"system", "user", "assistant", "tool"})
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z",
    re.IGNORECASE,
)


def opencollab_home(*, environ: dict[str, str] | None = None) -> Path:
    """Resolve the import staging home (OpenCollab reads snapshots by path)."""

    configured = (environ if environ is not None else dict(os.environ)).get("OPENCOLLAB_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".opencollab"


def session_relative_path(session_id: str, timestamp: str) -> Path:
    date = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(UTC)
    return Path("sessions") / date.strftime("%Y/%m/%d") / f"{session_id}.json"


def serialize(
    session: Session,
    *,
    session_id: str,
    cwd: Path,
    cli_version: str = PINNED_OPENCOLLAB_VERSION,
    model: str | None = None,
    timestamp: str | None = None,
    title: str | None = None,
) -> tuple[bytes, dict[str, int]]:
    """Serialize portable history into an OpenCollab session snapshot."""

    del cwd, cli_version, title  # the snapshot carries messages (and model) only
    messages: list[dict[str, Any]] = []
    dropped: Counter[str] = Counter()
    seen_calls: set[str] = set()
    seen_results: set[str] = set()

    for event in session.events:
        if event.kind == EventKind.MESSAGE and event.role is not None:
            text = event.text or ""
            if not text:
                dropped["message:empty"] += 1
                continue
            messages.append({"role": event.role.value, "content": text})
            continue

        if event.kind == EventKind.CONTEXT and event.role == Role.USER:
            image = portable_data_image(event.payload.get("image_url"))
            if event.payload.get("block_type") != "image" or image is None:
                dropped["context:image"] += 1
                continue
            media_type, encoded = image
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{media_type};base64,{encoded}"},
                        }
                    ],
                }
            )
            continue

        if event.kind == EventKind.TOOL_CALL:
            call_id = event.tool_call_id or ""
            if not call_id:
                dropped["tool_call:missing_id"] += 1
                continue
            if call_id in seen_calls:
                dropped["tool_call:duplicate_id"] += 1
                continue
            name = event.tool_name or "unknown_tool"
            if not event.tool_name:
                dropped["tool_call:missing_name"] += 1
            arguments = event.payload.get("input", {})
            if not isinstance(arguments, str):
                arguments = json.dumps(
                    arguments, ensure_ascii=False, separators=(",", ":"), allow_nan=False
                )
            seen_calls.add(call_id)
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ],
                }
            )
            continue

        if event.kind == EventKind.TOOL_RESULT:
            call_id = event.tool_call_id or ""
            if not call_id:
                dropped["tool_result:missing_id"] += 1
                continue
            if call_id not in seen_calls:
                dropped["tool_result:orphan_id"] += 1
                continue
            if call_id in seen_results:
                dropped["tool_result:duplicate_id"] += 1
                continue
            seen_results.add(call_id)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": _tool_result_content(event, dropped),
                }
            )
            continue

        if event.kind == EventKind.COMPACTION and event.text:
            messages.append(
                {
                    "role": "system",
                    "content": f"[CONTEXT SUMMARY]:\n{event.text}",
                }
            )
            continue

        if event.kind == EventKind.THINKING:
            dropped["thinking:private"] += 1
            if event.payload.get("encrypted_content") or event.payload.get("signature"):
                dropped["thinking:provider_payload"] += 1
            continue

        dropped[_omission_key(event)] += 1

    if not messages:
        raise SessionMigrateError("OpenCollab target requires at least one portable message")
    if len(messages) > MAX_MESSAGES:
        raise SessionMigrateError(
            f"OpenCollab target exceeds the {MAX_MESSAGES}-message native limit"
        )
    if not _UUID_RE.fullmatch(session_id):
        raise SessionMigrateError(f"session ID is not a valid UUID: {session_id}")

    value: dict[str, Any] = {"snapshot_version": SNAPSHOT_VERSION}
    resolved_model = string(model) or string(session.model)
    if resolved_model:
        value["model"] = resolved_model
    value["messages"] = messages

    data = (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()
    validate_native_bytes(data, session_id)
    return data, dict(sorted((key, count) for key, count in dropped.items() if count))


def validate_native_bytes(data: bytes, session_id: str) -> None:
    """Strictly validate a generated OpenCollab snapshot document."""

    del session_id  # snapshots are restored by explicit path, no embedded linkage
    if not data or len(data) > MAX_SNAPSHOT_BYTES:
        raise SessionMigrateError("OpenCollab snapshot is empty or exceeds the native size limit")
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SessionMigrateError("generated OpenCollab snapshot is not valid JSON") from exc
    if not isinstance(value, dict):
        raise SessionMigrateError("generated OpenCollab snapshot must be a JSON object")
    if value.get("snapshot_version") != SNAPSHOT_VERSION:
        raise SessionMigrateError(
            f"generated OpenCollab snapshot must declare snapshot_version={SNAPSHOT_VERSION}"
        )
    messages = value.get("messages")
    if not isinstance(messages, list) or not messages:
        raise SessionMigrateError("generated OpenCollab snapshot has no messages")
    for position, message in enumerate(messages, 1):
        _validate_message(message, position)
    for position, message in enumerate(messages, 1):
        if message["role"] != "tool":
            continue
        if not any(
            later.get("role") == "assistant"
            and any(
                call.get("id") == message["tool_call_id"]
                for call in later.get("tool_calls") or []
                if isinstance(call, dict)
            )
            for later in messages[: position - 1]
        ):
            raise SessionMigrateError(
                f"generated OpenCollab snapshot has a tool message at position {position} "
                "without a preceding matching tool_call"
            )


def native_record_count(data: bytes) -> int:
    value = json.loads(data)
    messages = value.get("messages", []) if isinstance(value, dict) else []
    return 1 + len(messages) if isinstance(messages, list) else 1


def _validate_message(message: Any, position: int) -> None:
    prefix = f"generated OpenCollab snapshot message at position {position}"
    if not isinstance(message, dict):
        raise SessionMigrateError(f"{prefix}: expected an object")
    role = message.get("role")
    if role not in ALLOWED_ROLES:
        raise SessionMigrateError(f"{prefix}: invalid role {role!r}")
    if role == "assistant":
        tool_calls = message.get("tool_calls")
        if tool_calls is not None and not isinstance(tool_calls, list):
            raise SessionMigrateError(f"{prefix}: 'tool_calls' must be a list")
        for call in tool_calls or []:
            if not isinstance(call, dict) or call.get("type") != "function":
                raise SessionMigrateError(f"{prefix}: tool calls must be typed 'function'")
            function = call.get("function")
            if not isinstance(function, dict) or not string(function.get("name")):
                raise SessionMigrateError(f"{prefix}: tool calls require a function name")
            if not isinstance(function.get("arguments"), str):
                raise SessionMigrateError(f"{prefix}: tool call arguments must be a string")
        if "content" in message and message["content"] is not None:
            _validate_content(message["content"], prefix)
        elif not tool_calls:
            raise SessionMigrateError(f"{prefix}: assistant turns need content or tool_calls")
        return
    if role == "tool" and not string(message.get("tool_call_id")):
        raise SessionMigrateError(f"{prefix}: tool turns need a tool_call_id")
    _validate_content(message.get("content"), prefix)


def _validate_content(content: Any, prefix: str) -> None:
    if isinstance(content, str):
        return
    if not isinstance(content, list) or not content:
        raise SessionMigrateError(f"{prefix}: content must be text or a non-empty part list")
    for part in content:
        if not isinstance(part, dict):
            raise SessionMigrateError(f"{prefix}: content parts must be objects")
        part_type = part.get("type")
        if part_type in {"text", "input_text", "output_text"}:
            if not isinstance(part.get("text"), str):
                raise SessionMigrateError(f"{prefix}: {part_type!r} parts require text")
            continue
        if part_type == "image_url":
            image_url = part.get("image_url")
            ok = (isinstance(image_url, str) and bool(image_url)) or (
                isinstance(image_url, dict) and string(image_url.get("url"))
            )
            if not ok:
                raise SessionMigrateError(f"{prefix}: image_url parts require a URL")
            continue
        raise SessionMigrateError(f"{prefix}: unsupported content part type {part_type!r}")


def _tool_result_content(event: Event, dropped: Counter[str]) -> str | list[dict[str, Any]]:
    blocks = event.payload.get("content_blocks")
    if not isinstance(blocks, list) or not blocks:
        return event.text or ""
    parts: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            dropped["tool_result:opaque_block"] += 1
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append({"type": "text", "text": block["text"]})
            continue
        image = portable_data_image(
            block.get("image_url") if block.get("type") == "image" else None
        )
        if image is not None:
            media_type, encoded = image
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{media_type};base64,{encoded}"},
                }
            )
            continue
        dropped["tool_result:non_text_content"] += 1
    if not parts:
        return event.text or content_text(blocks)
    if len(parts) == 1 and parts[0].get("type") == "text":
        return parts[0]["text"]
    return parts


def _omission_key(event: Event) -> str:
    return f"{event.kind.value}:not_portable"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
