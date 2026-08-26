"""OpenHands CLI 1.16.0 event-log session adapter.

OpenHands resumes a conversation from ordered JSON event files below
``~/.openhands/conversations/<uuid-hex>/events``.  ``base_state.json`` is a
complete SDK runtime snapshot, not a metadata sidecar: the pinned CLI rebuilds
it when only the event log is present, so migration never copies credentials,
provider settings, or cached runtime state.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from session_migrate.errors import JsonlError, SessionMigrateError
from session_migrate.formats.common import content_text, portable_data_image, string, valid_rfc3339
from session_migrate.jsonl import (
    DEFAULT_MAX_RECORD_BYTES,
    DEFAULT_MAX_RECORDS,
    DEFAULT_MAX_TOTAL_BYTES,
)
from session_migrate.model import AgentFormat, Event, EventKind, Provenance, Role, Session

PINNED_OPENHANDS_VERSION = "1.16.0"
PINNED_OPENHANDS_LINUX_X64_BYTES = 88_139_576
PINNED_OPENHANDS_LINUX_X64_SHA256 = (
    "cb04ee2da91c698733d5201c55cbc08d81dccc9d64b666275abf68a4e0c590e3"
)
OPENHANDS_BUNDLE_SCHEMA = "session-migrate.openhands.v1"
OPENHANDS_BASE_STATE_POLICY = "runtime-rebuilt"
MAX_BUNDLE_BYTES = DEFAULT_MAX_TOTAL_BYTES
MAX_BASE_STATE_BYTES = DEFAULT_MAX_RECORD_BYTES
MAX_EVENTS = DEFAULT_MAX_RECORDS
MAX_JSON_DEPTH = 96
MAX_JSON_NODES = 1_000_000
_EVENT_NAME = re.compile(r"event-(\d{5})-([0-9a-f-]{36})\.json$")


@dataclass(frozen=True, slots=True)
class ParsedOpenHandsBundle:
    session_id: str
    cwd: Path
    cli_version: str
    model: str | None
    title: str | None
    picker_title: str | None
    base_state_policy: str
    events: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class OpenHandsSessionSnapshot:
    """Content-free identity of the event log and optional runtime snapshot."""

    device: int
    inode: int
    size: int
    modified_ns: int
    fingerprint: str


def serialize(
    session: Session,
    *,
    session_id: str,
    cwd: Path,
    cli_version: str = PINNED_OPENHANDS_VERSION,
    model: str | None = None,
    timestamp: str | None = None,
    title: str | None = None,
) -> tuple[bytes, dict[str, int]]:
    """Serialize portable history as an installable OpenHands event bundle."""

    canonical_id = _uuid(session_id, "OpenHands target session ID")
    started = valid_rfc3339(timestamp) or valid_rfc3339(session.started_at) or _utc_now()
    clock = datetime.fromisoformat(started.replace("Z", "+00:00")).astimezone(UTC)
    dropped: Counter[str] = Counter()
    records: list[dict[str, Any]] = []
    seen_calls: set[str] = set()
    seen_results: set[str] = set()
    pending_actions: dict[str, list[tuple[str, str, str]]] = {}

    def next_record(kind: str, source: str, **fields: Any) -> dict[str, Any]:
        nonlocal clock
        value = {
            "id": str(uuid.uuid4()),
            "timestamp": clock.replace(tzinfo=None).isoformat(timespec="microseconds"),
            "source": source,
            **fields,
            "kind": kind,
        }
        clock += timedelta(microseconds=1)
        records.append(value)
        return value

    next_record(
        "SystemPromptEvent",
        "agent",
        system_prompt={
            "cache_prompt": False,
            "type": "text",
            "text": "Imported portable session history.",
        },
        tools=[],
        dynamic_context={"cache_prompt": False, "type": "text", "text": ""},
    )

    for event in session.events:
        if event.kind == EventKind.MESSAGE and event.role in {Role.USER, Role.ASSISTANT}:
            if not event.text:
                continue
            role = event.role.value
            next_record(
                "MessageEvent",
                "user" if role == "user" else "agent",
                llm_message={
                    "role": role,
                    "content": [_text_block(event.text)],
                    "thinking_blocks": [],
                },
                activated_skills=[],
                extended_content=[],
            )
            if event.payload.get("ui_only_projection") is True:
                dropped["message:ui_only_projection"] += 1
            continue

        if event.kind == EventKind.CONTEXT and event.role == Role.USER:
            if event.payload.get("block_type") != "image":
                dropped[_omission_key(event)] += 1
                continue
            image = portable_data_image(event.payload.get("image_url"))
            if image is None:
                dropped["context:image"] += 1
                continue
            media_type, encoded = image
            next_record(
                "MessageEvent",
                "user",
                llm_message={
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "image_urls": [f"data:{media_type};base64,{encoded}"],
                        }
                    ],
                    "thinking_blocks": [],
                },
                activated_skills=[],
                extended_content=[],
            )
            continue

        if event.kind == EventKind.TOOL_CALL:
            source_call_id = event.tool_call_id
            call_id = source_call_id or f"call_session_migrate_{uuid.uuid4().hex}"
            if not event.tool_call_id:
                dropped["tool_call:missing_id"] += 1
            if call_id in seen_calls:
                dropped["tool_call:duplicate_id"] += 1
                call_id = f"{call_id}__session_migrate_{uuid.uuid4().hex}"
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
            native = next_record(
                "ActionEvent",
                "agent",
                thought=[],
                thinking_blocks=[],
                action={
                    "command": json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
                    "kind": "TerminalAction",
                },
                tool_name=name,
                tool_call_id=call_id,
                tool_call={
                    "id": call_id,
                    "name": name,
                    "arguments": json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
                    "origin": "completion",
                },
                # OpenHands groups parallel tool calls by the response that
                # produced them.  Portable history does not expose that native
                # response ID, so give each imported call a stable-in-file
                # synthetic group ID.  The field is required by SDK 1.21.0 and
                # omitting it makes a resumed conversation fail validation.
                llm_response_id=str(uuid.uuid4()),
                security_risk="LOW",
                summary=f"Imported {name} call",
            )
            pending_key = source_call_id or call_id
            pending_actions.setdefault(pending_key, []).append((call_id, native["id"], name))
            continue

        if event.kind == EventKind.TOOL_RESULT:
            source_call_id = event.tool_call_id
            if not event.tool_call_id:
                dropped["tool_result:missing_id"] += 1
                continue
            if source_call_id not in pending_actions or not pending_actions[source_call_id]:
                dropped["tool_result:orphan_id"] += 1
                if source_call_id in seen_results:
                    dropped["tool_result:duplicate_id"] += 1
                seen_results.add(source_call_id)
                continue
            if source_call_id in seen_results:
                dropped["tool_result:duplicate_id"] += 1
            seen_results.add(source_call_id)
            call_id, action_id, action_name = pending_actions[source_call_id].pop(0)
            if event.tool_name and event.tool_name != action_name:
                dropped["tool_result:name_mismatch"] += 1
            content = _tool_result_content(event, dropped)
            next_record(
                "ObservationEvent",
                "environment",
                tool_name=action_name,
                tool_call_id=call_id,
                observation={
                    "content": content,
                    "is_error": event.payload.get("is_error") is True,
                    "command": "imported portable tool result",
                    "exit_code": 1 if event.payload.get("is_error") is True else 0,
                    "timeout": False,
                    "metadata": {},
                    "kind": "TerminalObservation",
                },
                action_id=action_id,
            )
            continue

        if event.kind == EventKind.COMPACTION and event.text:
            next_record(
                "Condensation",
                "environment",
                forgotten_event_ids=[],
                summary=event.text,
                llm_response_id=str(uuid.uuid4()),
            )
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

    if not any(record["kind"] in {"MessageEvent", "ActionEvent"} for record in records[1:]):
        raise SessionMigrateError("conversion produced no resumable conversation history")
    bundle = {
        "schema": OPENHANDS_BUNDLE_SCHEMA,
        "base_state_policy": OPENHANDS_BASE_STATE_POLICY,
        "session_id": canonical_id,
        "cwd": str(cwd),
        "cli_version": cli_version,
        "model": model or session.model,
        "title": title or session.title,
        "events": records,
    }
    data = (
        json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    validate_native_bytes(data, canonical_id)
    return data, dict(sorted(dropped.items()))


def parse_session(path: Path) -> Session:
    """Parse one native OpenHands conversation directory or events directory."""

    conversation, events_dir = _source_paths(path)
    before = session_snapshot(events_dir)
    entries = _read_event_files(events_dir)
    base_state, base_state_bytes = _read_base_state(conversation)
    after = session_snapshot(events_dir)
    if after != before:
        raise JsonlError("OpenHands session changed while it was being read; retry")
    events: list[Event] = []
    for index, (_, value) in enumerate(entries):
        events.extend(_parse_event(value, index))
    first_timestamp = string(entries[0][1].get("timestamp")) if entries else None
    digest = hashlib.sha256()
    for name, value in entries:
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
        digest.update(b"\0")
    if base_state_bytes is not None:
        digest.update(b"base_state.json\0")
        digest.update(base_state_bytes)
        digest.update(b"\0")
    model, cwd = _derived_state_metadata(base_state, conversation.name)
    return Session(
        source_format=AgentFormat.OPENHANDS,
        source_path=conversation.resolve(),
        source_sha256=digest.hexdigest(),
        session_id=_uuid(conversation.name, "OpenHands conversation directory"),
        cwd=cwd,
        started_at=_portable_timestamp(first_timestamp),
        cli_version=PINNED_OPENHANDS_VERSION,
        model=model,
        title=_native_picker_title(value for _, value in entries),
        events=tuple(events),
        raw_record_count=len(entries),
        model_provider=model.split("/", 1)[0] if model and "/" in model else None,
    )


parse = parse_session


def validate_native_bytes(data: bytes, session_id: str) -> ParsedOpenHandsBundle:
    """Validate a generated bundle without touching OpenHands state."""

    if not data or len(data) > MAX_BUNDLE_BYTES:
        raise SessionMigrateError("generated OpenHands bundle is empty or exceeds the safety limit")
    try:
        value = json.loads(data, object_pairs_hook=_unique_object)
        _validate_json_shape(value)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise SessionMigrateError("generated OpenHands bundle is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict) or value.get("schema") != OPENHANDS_BUNDLE_SCHEMA:
        raise SessionMigrateError("generated OpenHands bundle has an unsupported schema")
    canonical_id = _uuid(value.get("session_id"), "generated OpenHands session ID")
    if canonical_id != _uuid(session_id, "OpenHands target session ID"):
        raise SessionMigrateError("generated OpenHands bundle session linkage is invalid")
    cwd = string(value.get("cwd"))
    version = string(value.get("cli_version"))
    model = string(value.get("model"))
    title = string(value.get("title"))
    base_state_policy = string(value.get("base_state_policy")) or OPENHANDS_BASE_STATE_POLICY
    events = value.get("events")
    if (
        not cwd
        or "\x00" in cwd
        or not version
        or (model is not None and "\x00" in model)
        or (title is not None and "\x00" in title)
        or base_state_policy != OPENHANDS_BASE_STATE_POLICY
        or not isinstance(events, list)
    ):
        raise SessionMigrateError("generated OpenHands bundle has invalid metadata")
    if not events or len(events) > MAX_EVENTS or not all(isinstance(item, dict) for item in events):
        raise SessionMigrateError("generated OpenHands bundle has invalid events")
    for index, event in enumerate(events):
        _validate_event(event, index)
    _validate_event_sequence(events)
    if events[0].get("kind") != "SystemPromptEvent":
        raise SessionMigrateError("generated OpenHands history must start with a system event")
    if not any(event.get("kind") in {"MessageEvent", "ActionEvent"} for event in events[1:]):
        raise SessionMigrateError(
            "generated OpenHands bundle has no resumable conversation history"
        )
    return ParsedOpenHandsBundle(
        session_id=canonical_id,
        cwd=Path(cwd),
        cli_version=version,
        model=model,
        title=title,
        picker_title=_native_picker_title(events),
        base_state_policy=base_state_policy,
        events=tuple(dict(event) for event in events),
    )


def native_record_count(data: bytes) -> int:
    value = json.loads(data)
    events = value.get("events", []) if isinstance(value, dict) else []
    return len(events) if isinstance(events, list) else 0


def native_files(data: bytes, session_id: str) -> tuple[tuple[str, bytes], ...]:
    """Return event files only; SDK 1.21.0 rebuilds ``base_state.json`` on resume.

    The base state contains the complete agent configuration and credential
    fields.  It has no title or CLI-version field, and installing a partial
    value makes the pinned SDK take its strict restore path.  Target cwd/model
    are therefore carried in the validated bundle and supplied at first
    resume; the SDK then persists its own complete, redacted runtime snapshot.
    """

    parsed = validate_native_bytes(data, session_id)
    files = []
    for index, event in enumerate(parsed.events):
        name = f"event-{index:05d}-{event['id']}.json"
        content = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        files.append((name, content))
    return tuple(files)


def session_snapshot(path: Path) -> OpenHandsSessionSnapshot:
    """Snapshot the complete authoritative session state without reading bodies."""

    conversation, events_dir = _source_paths(path)
    try:
        directory = events_dir.lstat()
        conversation_stat = conversation.lstat()
    except OSError as exc:
        raise JsonlError("OpenHands session directory is unavailable") from exc
    if (
        events_dir.is_symlink()
        or not events_dir.is_dir()
        or conversation.is_symlink()
        or not conversation.is_dir()
    ):
        raise JsonlError("OpenHands session directory is invalid")

    event_paths = tuple(sorted(events_dir.glob("event-*.json")))
    if not event_paths or len(event_paths) > MAX_EVENTS:
        raise JsonlError("OpenHands event log is empty or exceeds the record limit")
    candidates = list(event_paths)
    base_state_path = conversation / "base_state.json"
    if os.path.lexists(base_state_path):
        candidates.append(base_state_path)

    components = [
        (
            f"conversation:{conversation_stat.st_dev}:{conversation_stat.st_ino}:"
            f"{conversation_stat.st_mtime_ns}"
        ),
        f"events:{directory.st_dev}:{directory.st_ino}:{directory.st_mtime_ns}",
    ]
    total_size = 0
    newest = max(directory.st_mtime_ns, conversation_stat.st_mtime_ns)
    for candidate in candidates:
        try:
            info = candidate.lstat()
        except OSError as exc:
            raise JsonlError("OpenHands session state is unavailable") from exc
        if candidate.is_symlink() or not candidate.is_file():
            raise JsonlError("OpenHands session state is not a regular file")
        if candidate == base_state_path and info.st_size > MAX_BASE_STATE_BYTES:
            raise JsonlError("OpenHands base state exceeds the input safety limit")
        total_size += info.st_size
        if total_size > MAX_BUNDLE_BYTES + MAX_BASE_STATE_BYTES:
            raise JsonlError("OpenHands session exceeds the input safety limit")
        newest = max(newest, info.st_mtime_ns)
        components.append(
            f"{candidate.relative_to(conversation)}:{info.st_dev}:{info.st_ino}:"
            f"{info.st_size}:{info.st_mtime_ns}"
        )
    fingerprint = hashlib.sha256("\0".join(components).encode()).hexdigest()
    return OpenHandsSessionSnapshot(
        directory.st_dev,
        directory.st_ino,
        total_size,
        newest,
        fingerprint,
    )


def session_relative_path(session_id: str) -> Path:
    return Path(_uuid(session_id, "OpenHands target session ID").replace("-", "")) / "events"


def conversations_home(*, environ: dict[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    configured = values.get("OPENHANDS_CONVERSATIONS_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".openhands/conversations"


def _source_paths(path: Path) -> tuple[Path, Path]:
    candidate = path.expanduser()
    if candidate.is_file() and _EVENT_NAME.fullmatch(candidate.name):
        events = candidate.parent
        conversation = events.parent
    elif candidate.is_dir() and candidate.name == "events":
        events = candidate
        conversation = candidate.parent
    elif candidate.is_dir() and (candidate / "events").is_dir():
        conversation = candidate
        events = candidate / "events"
    else:
        raise SessionMigrateError(
            "OpenHands source must be a conversation directory containing an events directory"
        )
    _uuid(conversation.name, "OpenHands conversation directory")
    return conversation, events


def _read_event_files(events_dir: Path) -> list[tuple[str, dict[str, Any]]]:
    entries: list[tuple[str, dict[str, Any]]] = []
    total = 0
    paths = sorted(events_dir.glob("event-*.json"))
    if not paths or len(paths) > MAX_EVENTS:
        raise JsonlError("OpenHands event log is empty or exceeds the record limit")
    for expected, path in enumerate(paths):
        match = _EVENT_NAME.fullmatch(path.name)
        if not match or int(match.group(1)) != expected or path.is_symlink() or not path.is_file():
            raise JsonlError("OpenHands event filenames are invalid or non-contiguous")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise JsonlError(f"cannot read OpenHands event: {exc.strerror or exc}") from exc
        total += len(data)
        if total > MAX_BUNDLE_BYTES:
            raise JsonlError("OpenHands event log exceeds the input safety limit")
        try:
            value = json.loads(data, object_pairs_hook=_unique_object)
            _validate_json_shape(value)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise JsonlError("OpenHands event is not valid UTF-8 JSON") from exc
        if not isinstance(value, dict) or value.get("id") != match.group(2):
            raise JsonlError("OpenHands event filename and metadata disagree")
        _validate_event(value, expected)
        entries.append((path.name, value))
    if entries[0][1].get("kind") != "SystemPromptEvent":
        raise JsonlError("OpenHands event log must start with a system event")
    _validate_event_sequence([value for _, value in entries])
    return entries


def _parse_event(value: dict[str, Any], index: int) -> list[Event]:
    kind = str(value["kind"])
    timestamp = _portable_timestamp(value.get("timestamp"))
    provenance = Provenance(index, f"openhands.{kind}", str(value["id"]))
    if kind == "SystemPromptEvent":
        return [
            Event(
                kind=EventKind.OPAQUE,
                role=Role.SYSTEM,
                payload={"reason": "openhands_system_prompt"},
                timestamp=timestamp,
                provenance=provenance,
            )
        ]
    if kind == "MessageEvent":
        message = value["llm_message"]
        role = Role.USER if message["role"] == "user" else Role.ASSISTANT
        events = _content_events(message.get("content"), role, timestamp, provenance)
        if message.get("thinking_blocks"):
            events.append(
                Event(
                    kind=EventKind.OPAQUE,
                    role=Role.ASSISTANT,
                    payload={"reason": "openhands_private_thinking"},
                    timestamp=timestamp,
                    provenance=provenance,
                )
            )
        if value.get("activated_skills") or value.get("extended_content"):
            events.append(
                Event(
                    kind=EventKind.OPAQUE,
                    role=role,
                    payload={"reason": "openhands_message_runtime_metadata"},
                    timestamp=timestamp,
                    provenance=provenance,
                )
            )
        return events
    if kind == "ActionEvent":
        events: list[Event] = []
        if value.get("thought") or value.get("thinking_blocks"):
            events.append(
                Event(
                    kind=EventKind.OPAQUE,
                    role=Role.ASSISTANT,
                    payload={"reason": "openhands_private_thinking"},
                    timestamp=timestamp,
                    provenance=provenance,
                )
            )
        call = value.get("tool_call")
        arguments: Any = value.get("action", {})
        if isinstance(call, dict) and isinstance(call.get("arguments"), str):
            try:
                arguments = json.loads(call["arguments"])
            except json.JSONDecodeError:
                arguments = {"input": call["arguments"]}
        events.append(
            Event(
                kind=EventKind.TOOL_CALL,
                role=Role.ASSISTANT,
                tool_name=string(value.get("tool_name")),
                tool_call_id=string(value.get("tool_call_id")),
                payload={"input": arguments},
                timestamp=timestamp,
                provenance=provenance,
            )
        )
        return events
    if kind == "ObservationEvent":
        observation = value["observation"]
        content = observation.get("content")
        blocks = _portable_result_blocks(content)
        return [
            Event(
                kind=EventKind.TOOL_RESULT,
                role=Role.TOOL,
                text=content_text(content),
                tool_name=string(value.get("tool_name")),
                tool_call_id=string(value.get("tool_call_id")),
                payload={
                    "content_blocks": blocks,
                    "is_error": observation.get("is_error") is True,
                },
                timestamp=timestamp,
                provenance=provenance,
            )
        ]
    if kind == "Condensation":
        return [
            Event(
                kind=EventKind.COMPACTION,
                role=Role.SYSTEM,
                text=string(value.get("summary")),
                payload={"source_subtype": "openhands_condensation"},
                timestamp=timestamp,
                provenance=provenance,
            )
        ]
    return [
        Event(
            kind=EventKind.OPAQUE,
            payload={"reason": f"openhands_{kind}"},
            timestamp=timestamp,
            provenance=provenance,
        )
    ]


def _validate_event(value: dict[str, Any], index: int) -> None:
    event_id = _uuid(value.get("id"), f"OpenHands event {index} id")
    del event_id
    if not _native_timestamp(value.get("timestamp")):
        raise SessionMigrateError(f"OpenHands event {index} has an invalid timestamp")
    kind = string(value.get("kind"))
    source = string(value.get("source"))
    if not kind or source not in {"agent", "user", "environment"}:
        raise SessionMigrateError(f"OpenHands event {index} has invalid metadata")
    if kind == "SystemPromptEvent":
        if source != "agent" or not _text_content(value.get("system_prompt")):
            raise SessionMigrateError("OpenHands system event is malformed")
    elif kind == "MessageEvent":
        message = value.get("llm_message")
        if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
            raise SessionMigrateError("OpenHands message event is malformed")
        _validate_content(message.get("content"))
    elif kind == "ActionEvent":
        if not string(value.get("tool_name")) or not string(value.get("tool_call_id")):
            raise SessionMigrateError("OpenHands action event is malformed")
        _uuid(value.get("llm_response_id"), "OpenHands action response id")
        if not isinstance(value.get("action"), dict) or not isinstance(
            value.get("tool_call"), dict
        ):
            raise SessionMigrateError("OpenHands action event is malformed")
    elif kind == "ObservationEvent":
        if not string(value.get("tool_name")) or not string(value.get("tool_call_id")):
            raise SessionMigrateError("OpenHands observation event is malformed")
        observation = value.get("observation")
        if not isinstance(observation, dict) or not isinstance(observation.get("is_error"), bool):
            raise SessionMigrateError("OpenHands observation event is malformed")
        _validate_content(observation.get("content"))
    elif kind == "Condensation":
        if not string(value.get("summary")) or not isinstance(
            value.get("forgotten_event_ids"), list
        ):
            raise SessionMigrateError("OpenHands condensation event is malformed")
        _uuid(value.get("llm_response_id"), "OpenHands condensation response id")


def _validate_event_sequence(events: list[dict[str, Any]]) -> None:
    """Enforce the cross-event invariants consumed by SDK 1.21.0."""

    event_ids: set[str] = set()
    actions: dict[str, tuple[str, str]] = {}
    observed_actions: set[str] = set()
    for index, event in enumerate(events):
        event_id = str(event["id"])
        if event_id in event_ids:
            raise SessionMigrateError(f"OpenHands event {index} has a duplicate id")
        event_ids.add(event_id)
        if event.get("kind") == "ActionEvent":
            tool_call_id = str(event["tool_call_id"])
            actions[event_id] = (tool_call_id, str(event["tool_name"]))
        elif event.get("kind") == "ObservationEvent":
            action_id = str(event.get("action_id"))
            action = actions.get(action_id)
            if action is None:
                raise SessionMigrateError(
                    f"OpenHands observation event {index} has invalid action linkage"
                )
            if action_id in observed_actions:
                raise SessionMigrateError(
                    f"OpenHands observation event {index} duplicates an action result"
                )
            if action != (str(event["tool_call_id"]), str(event["tool_name"])):
                raise SessionMigrateError(
                    f"OpenHands observation event {index} disagrees with its action"
                )
            observed_actions.add(action_id)


def _validate_content(content: Any) -> None:
    if not isinstance(content, list) or not content:
        raise SessionMigrateError("OpenHands content is empty or malformed")
    for block in content:
        if not isinstance(block, dict):
            raise SessionMigrateError("OpenHands content block is malformed")
        block_type = block.get("type")
        if block_type == "text":
            if not isinstance(block.get("text"), str):
                raise SessionMigrateError("OpenHands text block is malformed")
        elif block_type == "image":
            urls = block.get("image_urls")
            if (
                not isinstance(urls, list)
                or not urls
                or not all(portable_data_image(item) is not None for item in urls)
            ):
                raise SessionMigrateError("OpenHands image block is malformed")
        else:
            raise SessionMigrateError("OpenHands content block type is unsupported")


def _content_events(
    content: Any,
    role: Role,
    timestamp: str | None,
    provenance: Provenance,
) -> list[Event]:
    events: list[Event] = []
    for block_index, block in enumerate(content if isinstance(content, list) else []):
        block_provenance = Provenance(
            provenance.record_index,
            provenance.record_type,
            provenance.source_id,
            block_index,
        )
        if block.get("type") == "text" and block.get("text"):
            events.append(
                Event(
                    kind=EventKind.MESSAGE,
                    role=role,
                    text=block["text"],
                    timestamp=timestamp,
                    provenance=block_provenance,
                )
            )
        elif block.get("type") == "image":
            for image_url in block.get("image_urls", []):
                events.append(
                    Event(
                        kind=EventKind.CONTEXT,
                        role=role,
                        payload={"block_type": "image", "image_url": image_url},
                        timestamp=timestamp,
                        provenance=block_provenance,
                    )
                )
    return events


def _tool_result_content(event: Event, dropped: Counter[str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    content = event.payload.get("content_blocks") or event.payload.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                dropped["tool_result:opaque"] += 1
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                result.append(_text_block(block["text"]))
            elif block.get("type") == "image":
                image = portable_data_image(block.get("image_url"))
                if image is None:
                    dropped["tool_result:opaque"] += 1
                else:
                    media_type, encoded = image
                    result.append(
                        {"type": "image", "image_urls": [f"data:{media_type};base64,{encoded}"]}
                    )
            else:
                dropped["tool_result:opaque"] += 1
    if not result:
        text = event.text or content_text(content) or ""
        result.append(_text_block(text))
    return result


def _portable_result_blocks(content: Any) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    if not isinstance(content, list):
        return blocks
    for block in content:
        if block.get("type") == "text":
            blocks.append({"type": "text", "text": block.get("text", "")})
        elif block.get("type") == "image":
            for image_url in block.get("image_urls", []):
                blocks.append({"type": "image", "image_url": image_url})
    return blocks


def _text_block(text: str) -> dict[str, Any]:
    return {"cache_prompt": False, "type": "text", "text": text}


def _text_content(value: Any) -> str | None:
    if isinstance(value, dict) and value.get("type") == "text":
        return string(value.get("text"))
    return None


def _read_base_state(conversation: Path) -> tuple[dict[str, Any] | None, bytes | None]:
    path = conversation / "base_state.json"
    if not os.path.lexists(path):
        return None, None
    try:
        before = path.lstat()
        if path.is_symlink() or not path.is_file():
            raise JsonlError("OpenHands base state is not a regular file")
        if before.st_size > MAX_BASE_STATE_BYTES:
            raise JsonlError("OpenHands base state exceeds the input safety limit")
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise JsonlError("OpenHands base state changed while it was opened; retry")
            data = stream.read(MAX_BASE_STATE_BYTES + 1)
        if len(data) > MAX_BASE_STATE_BYTES:
            raise JsonlError("OpenHands base state exceeds the input safety limit")
        value = json.loads(
            data,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
        _validate_json_bounds(value)
    except JsonlError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise JsonlError("OpenHands base state is not valid bounded UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise JsonlError("OpenHands base state is not a JSON object")
    return value, data


def _derived_state_metadata(
    value: dict[str, Any] | None, conversation_name: str
) -> tuple[str | None, Path | None]:
    if value is None:
        return None, None
    state_id = _uuid(value.get("id"), "OpenHands base state id")
    if state_id != _uuid(conversation_name, "OpenHands conversation directory"):
        raise JsonlError("OpenHands base state and conversation directory disagree")
    agent = value.get("agent")
    llm = agent.get("llm") if isinstance(agent, dict) else None
    model = string(llm.get("model")) if isinstance(llm, dict) else None
    workspace = value.get("workspace")
    cwd_value = None
    if isinstance(workspace, dict):
        cwd_value = string(workspace.get("working_dir")) or string(workspace.get("cwd"))
    if (model and "\x00" in model) or (cwd_value and "\x00" in cwd_value):
        raise JsonlError("OpenHands base state metadata is invalid")
    return model, Path(cwd_value) if cwd_value else None


def _native_picker_title(events: Iterable[dict[str, Any]]) -> str | None:
    """Match OpenHands CLI 1.16.0's first-user-text picker title."""

    for event in events:
        if event.get("kind") != "MessageEvent" or event.get("source") != "user":
            continue
        message = event.get("llm_message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list) or not content:
            continue
        first = content[0]
        if isinstance(first, dict) and first.get("type") == "text":
            title = string(first.get("text"))
            if title:
                return title
    return None


def _portable_timestamp(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _native_timestamp(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _uuid(value: Any, label: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise SessionMigrateError(f"{label} is not a valid UUID") from exc


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _validate_json_shape(value: Any) -> None:
    nodes = 0
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise ValueError("JSON structure exceeds safety limit")
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
        elif isinstance(current, float) and not current.is_integer():
            raise ValueError("non-finite or fractional numeric metadata")


def _validate_json_bounds(value: Any) -> None:
    nodes = 0
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise ValueError("JSON structure exceeds safety limit")
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"unsupported JSON constant: {value}")


def _omission_key(event: Event) -> str:
    if event.kind == EventKind.OPAQUE:
        return string(event.payload.get("reason")) or "opaque"
    return event.kind.value


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
