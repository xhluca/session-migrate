"""Claude Code JSONL reader and conservative native writer."""

from __future__ import annotations

import re
import uuid
from collections import Counter, deque
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from session_bridge.errors import SessionBridgeError
from session_bridge.formats.common import (
    claude_source_from_image_url,
    content_text,
    image_url_from_claude_source,
    object_value,
    string,
    valid_rfc3339,
)
from session_bridge.jsonl import JsonlRecord, encode_jsonl, file_sha256, iter_jsonl
from session_bridge.model import AgentFormat, Event, EventKind, Provenance, Role, Session

PINNED_CLAUDE_VERSION = "2.1.209"
TITLE_TYPES = {"custom-title": "customTitle", "ai-title": "aiTitle"}


def parse(path: Path) -> Session:
    records = list(iter_jsonl(path))
    selected_records = _active_conversation_records(records)
    selected = {record.index for record in selected_records}
    selected_session_ids = {
        string(record.value.get("sessionId"))
        for record in selected_records
        if string(record.value.get("sessionId"))
    }
    if len(selected_session_ids) > 1:
        raise SessionBridgeError("Claude active graph contains mixed sessionId values")
    selected_compaction_summaries = {
        string(record.value.get("parentUuid"))
        for record in selected_records
        if record.value.get("isCompactSummary") is True
    }
    selected_boundaries = {
        string(record.value.get("uuid")): record.value
        for record in selected_records
        if record.value.get("type") == "system"
        and record.value.get("subtype") == "compact_boundary"
        and string(record.value.get("uuid"))
    }
    events: list[Event] = []
    session_id = None
    cwd = None
    started_at = None
    cli_version = None
    model = None
    custom_title = None
    ai_title = None

    for record in records:
        value = record.value
        record_type = string(value.get("type"))
        session_id = session_id or string(value.get("sessionId"))
        cwd = cwd or string(value.get("cwd"))
        started_at = started_at or string(value.get("timestamp"))
        cli_version = cli_version or string(value.get("version"))
        title_field = TITLE_TYPES.get(record_type or "")
        if title_field:
            title = string(value.get(title_field))
            if title and record_type == "custom-title":
                custom_title = title
            elif title:
                ai_title = title

    # Conversation semantics follow the UUID graph, not physical JSONL order.
    # Claude may append a child before its tool-call parent during streaming.
    for record in selected_records:
        value = record.value
        record_type = string(value.get("type"))
        if record_type == "system" and value.get("subtype") == "compact_boundary":
            if string(value.get("uuid")) in selected_compaction_summaries:
                continue
            events.append(
                Event(
                    kind=EventKind.COMPACTION,
                    provenance=_provenance(record),
                    timestamp=string(value.get("timestamp")),
                    payload={"source_subtype": "compact_boundary"},
                )
            )
            continue
        if record_type not in {"user", "assistant"}:
            if record_type not in {"last-prompt", "queue-operation"}:
                events.append(_opaque_event(record, "active_graph_metadata_record"))
            continue
        message = object_value(value.get("message"))
        if value.get("isCompactSummary") is True:
            boundary = selected_boundaries.get(string(value.get("parentUuid")))
            events.append(
                Event(
                    kind=EventKind.COMPACTION,
                    role=Role.SYSTEM,
                    text=content_text(message.get("content")),
                    timestamp=string(value.get("timestamp")),
                    payload={
                        "source_subtype": "compact_summary",
                        "has_boundary_metadata": bool(
                            boundary and isinstance(boundary.get("compactMetadata"), dict)
                        ),
                    },
                    provenance=_provenance(record),
                )
            )
            continue
        if value.get("isMeta") is True:
            events.append(_opaque_event(record, "active_graph_metadata_record"))
            continue
        role_name = string(message.get("role")) or record_type
        if role_name == "assistant":
            role = Role.ASSISTANT
        elif role_name == "user":
            role = Role.USER
        else:
            events.append(_opaque_event(record, "privileged_or_unknown_message_role"))
            continue
        if role == Role.ASSISTANT:
            model = model or string(message.get("model"))
        events.extend(_events_from_content(record, role, message.get("content")))
        if value.get("toolUseResult") is not None or value.get("sourceToolAssistantUUID"):
            events.append(_opaque_event(record, "top_level_tool_result_metadata"))

    # Non-selected records are accounting-only and cannot affect replay order.
    for record in records:
        if record.index in selected:
            continue
        value = record.value
        record_type = string(value.get("type"))
        if record_type in {"user", "assistant"} and isinstance(value.get("message"), dict):
            events.append(_opaque_event(record, "inactive_or_metadata_conversation_record"))
        elif record_type not in set(TITLE_TYPES) | {"last-prompt", "queue-operation"}:
            events.append(_opaque_event(record, "non_conversation_record"))

    return Session(
        source_format=AgentFormat.CLAUDE,
        source_path=path.resolve(),
        source_sha256=file_sha256(path),
        session_id=session_id,
        cwd=Path(cwd) if cwd else None,
        started_at=started_at,
        cli_version=cli_version,
        model=model,
        title=custom_title or ai_title,
        events=tuple(events),
        raw_record_count=len(records),
    )


def serialize(
    session: Session,
    *,
    session_id: str,
    cwd: Path,
    cli_version: str = PINNED_CLAUDE_VERSION,
    model: str | None = None,
    timestamp: str | None = None,
) -> tuple[bytes, dict[str, int]]:
    """Serialize the portable subset accepted by Claude Code 2.1.209."""

    fallback_timestamp = valid_rfc3339(timestamp) or valid_rfc3339(session.started_at) or _utc_now()
    target_model = model or session.model or "unknown"
    emitted: list[dict[str, Any]] = []
    dropped: Counter[str] = Counter()
    parent_uuid: str | None = None
    pending_role: Role | None = None
    pending_blocks: list[dict[str, Any]] = []
    pending_timestamp: str | None = None
    pending_source_record: int | None = None
    generated_tool_ids: deque[str] = deque()
    seen_tool_call_ids: set[str] = set()
    seen_tool_result_ids: set[str] = set()

    def flush() -> None:
        nonlocal parent_uuid, pending_role, pending_blocks, pending_timestamp
        nonlocal pending_source_record
        if pending_role is None or not pending_blocks:
            pending_role = None
            pending_blocks = []
            pending_timestamp = None
            pending_source_record = None
            return
        record_uuid = str(uuid.uuid4())
        record_timestamp = pending_timestamp or fallback_timestamp
        common: dict[str, Any] = {
            "parentUuid": parent_uuid,
            "isSidechain": False,
            "userType": "external",
            "cwd": str(cwd),
            "sessionId": session_id,
            "version": cli_version,
            "gitBranch": "",
            "uuid": record_uuid,
            "timestamp": record_timestamp,
        }
        if pending_role == Role.USER:
            content: str | list[dict[str, Any]]
            if len(pending_blocks) == 1 and pending_blocks[0].get("type") == "text":
                content = str(pending_blocks[0].get("text", ""))
            else:
                content = pending_blocks
            common.update(
                {
                    "type": "user",
                    "message": {"role": "user", "content": content},
                }
            )
        else:
            common.update(
                {
                    "type": "assistant",
                    "message": {
                        "id": f"msg_session_bridge_{uuid.uuid4().hex}",
                        "type": "message",
                        "role": "assistant",
                        "model": target_model,
                        "content": pending_blocks,
                        "stop_reason": "end_turn",
                        "stop_sequence": None,
                        "usage": {
                            "input_tokens": 0,
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0,
                            "output_tokens": 0,
                        },
                    },
                    "requestId": f"req_session_bridge_{uuid.uuid4().hex}",
                }
            )
        emitted.append(common)
        parent_uuid = record_uuid
        pending_role = None
        pending_blocks = []
        pending_timestamp = None
        pending_source_record = None

    for event in session.events:
        target_role: Role | None = None
        block: dict[str, Any] | None = None
        if (
            event.kind == EventKind.MESSAGE
            and event.text
            and event.role
            in {
                Role.USER,
                Role.ASSISTANT,
            }
        ):
            if event.payload.get("ui_only_projection") is True:
                dropped["message:ui_only_projection"] += 1
            target_role = event.role
            block = {"type": "text", "text": event.text}
        elif event.kind == EventKind.TOOL_CALL:
            target_role = Role.ASSISTANT
            tool_call_id = event.tool_call_id
            if not tool_call_id:
                tool_call_id = f"toolu_session_bridge_{uuid.uuid4().hex}"
                generated_tool_ids.append(tool_call_id)
                dropped["tool_call:missing_id"] += 1
            tool_name = event.tool_name
            if not tool_name:
                tool_name = "unknown_tool"
                dropped["tool_call:missing_name"] += 1
            tool_input = event.payload.get("input", {})
            if not isinstance(tool_input, dict):
                tool_input = {"input": tool_input}
                dropped["tool_call:non_object_input"] += 1
            if event.payload.get("namespace"):
                dropped["tool_call:namespace"] += 1
            if tool_call_id in seen_tool_call_ids:
                dropped["tool_call:duplicate_id"] += 1
            seen_tool_call_ids.add(tool_call_id)
            block = {
                "type": "tool_use",
                "id": tool_call_id,
                "name": tool_name,
                "input": tool_input,
            }
        elif event.kind == EventKind.TOOL_RESULT:
            target_role = Role.USER
            source_tool_call_id = event.tool_call_id
            tool_call_id = source_tool_call_id
            if not tool_call_id:
                tool_call_id = (
                    generated_tool_ids.popleft()
                    if generated_tool_ids
                    else f"toolu_missing_{uuid.uuid4().hex}"
                )
                dropped["tool_result:missing_id"] += 1
            elif tool_call_id not in seen_tool_call_ids:
                dropped["tool_result:orphan_id"] += 1
            if source_tool_call_id and source_tool_call_id in seen_tool_result_ids:
                dropped["tool_result:duplicate_id"] += 1
            if source_tool_call_id:
                seen_tool_result_ids.add(source_tool_call_id)
            result_content, omitted_blocks = _claude_tool_result_content(event)
            dropped.update(omitted_blocks)
            block = {
                "type": "tool_result",
                "tool_use_id": tool_call_id,
                "content": result_content,
                "is_error": bool(event.payload.get("is_error", False)),
            }
        elif (
            event.kind == EventKind.CONTEXT
            and event.payload.get("block_type") == "image"
            and event.role == Role.USER
        ):
            target_role = Role.USER
            source = claude_source_from_image_url(event.payload.get("image_url"))
            if source:
                block = {"type": "image", "source": source}
            else:
                dropped["context:image"] += 1
        else:
            dropped[_omission_key(event)] += 1

        if block is None or target_role is None:
            continue
        if pending_role is not None and (
            pending_role != target_role or pending_source_record != event.provenance.record_index
        ):
            flush()
        pending_role = target_role
        pending_source_record = event.provenance.record_index
        event_timestamp = valid_rfc3339(event.timestamp)
        if event.timestamp and not event_timestamp:
            dropped["timestamp:invalid"] += 1
        pending_timestamp = pending_timestamp or event_timestamp
        pending_blocks.append(block)
    flush()

    if session.title:
        emitted.append(
            {
                "type": "custom-title",
                "customTitle": session.title,
                "sessionId": session_id,
            }
        )
    return encode_jsonl(emitted), dict(sorted(dropped.items()))


def project_directory_name(cwd: Path) -> str:
    """Encode an absolute project path using Claude Code's observed directory scheme."""

    resolved = str(cwd.resolve())
    return re.sub(r"[^A-Za-z0-9]", "-", resolved) or "-"


def _active_conversation_records(records: list[JsonlRecord]) -> list[JsonlRecord]:
    candidates = [
        record
        for record in records
        if record.value.get("type") in {"user", "assistant"}
        and isinstance(record.value.get("message"), dict)
        and record.value.get("isMeta") is not True
        and record.value.get("isSidechain") is not True
    ]
    if not candidates:
        if any(
            record.value.get("type") in {"user", "assistant"}
            and isinstance(record.value.get("message"), dict)
            and record.value.get("isSidechain") is True
            for record in records
        ):
            raise SessionBridgeError(
                "Claude sidechain/subagent transcripts cannot be converted directly; "
                "transfer the parent session"
            )
        return []
    by_uuid: dict[str, JsonlRecord] = {}
    for record in records:
        record_uuid = string(record.value.get("uuid"))
        if not record_uuid:
            continue
        if record_uuid in by_uuid:
            raise SessionBridgeError("Claude transcript contains a duplicate record UUID")
        by_uuid[record_uuid] = record
    recorded_leaf = next(
        (
            string(record.value.get("leafUuid"))
            for record in reversed(records)
            if record.value.get("type") == "last-prompt" and string(record.value.get("leafUuid"))
        ),
        None,
    )
    if recorded_leaf and recorded_leaf not in by_uuid:
        raise SessionBridgeError("Claude last-prompt references a missing leaf UUID")
    leaf_uuid = recorded_leaf or string(candidates[-1].value.get("uuid"))
    if not leaf_uuid or leaf_uuid not in by_uuid:
        return candidates

    leaf_to_root: list[JsonlRecord] = []
    seen: set[str] = set()
    cursor: str | None = leaf_uuid
    while cursor:
        if cursor in seen:
            raise SessionBridgeError("Claude active graph contains an ancestry cycle")
        seen.add(cursor)
        record = by_uuid.get(cursor)
        if record is None:
            raise SessionBridgeError("Claude active graph references a missing parent UUID")
        leaf_to_root.append(record)
        parent_uuid = string(record.value.get("parentUuid"))
        if parent_uuid:
            cursor = parent_uuid
            continue
        logical_parent = (
            string(record.value.get("logicalParentUuid"))
            if record.value.get("type") == "system"
            and record.value.get("subtype") == "compact_boundary"
            else None
        )
        if logical_parent and logical_parent in seen:
            if _valid_preserved_compaction_back_edge(record, logical_parent, seen, by_uuid):
                break
            raise SessionBridgeError("Claude active graph contains an ancestry cycle")
        cursor = logical_parent
    return list(reversed(leaf_to_root))


def _valid_preserved_compaction_back_edge(
    boundary: JsonlRecord,
    logical_parent: str,
    seen: set[str],
    by_uuid: dict[str, JsonlRecord],
) -> bool:
    """Recognize Claude's metadata-declared preserved-segment loop."""

    metadata = boundary.value.get("compactMetadata")
    if not isinstance(metadata, dict):
        return False
    segment = metadata.get("preservedSegment")
    messages = metadata.get("preservedMessages")
    if not isinstance(segment, dict) or not isinstance(messages, dict):
        return False
    anchor = string(segment.get("anchorUuid"))
    head = string(segment.get("headUuid"))
    tail = string(segment.get("tailUuid"))
    boundary_uuid = string(boundary.value.get("uuid"))
    if not all((anchor, head, tail, boundary_uuid)) or logical_parent != tail:
        return False
    assert anchor is not None and head is not None and tail is not None
    assert boundary_uuid is not None
    anchor_record = by_uuid.get(anchor)
    if (
        anchor_record is None
        or anchor_record.value.get("isCompactSummary") is not True
        or string(anchor_record.value.get("parentUuid")) != boundary_uuid
    ):
        return False
    declared = messages.get("allUuids", messages.get("uuids"))
    if not isinstance(declared, list) or not declared:
        return False
    declared_ids = {value for value in declared if isinstance(value, str)}
    if not {head, tail}.issubset(declared_ids) or not declared_ids.issubset(seen):
        return False

    cursor: str | None = tail
    preserved_path: set[str] = set()
    while cursor and cursor != anchor:
        if cursor in preserved_path or cursor not in seen:
            return False
        preserved_path.add(cursor)
        record = by_uuid.get(cursor)
        if record is None:
            return False
        cursor = string(record.value.get("parentUuid"))
    return cursor == anchor and head in preserved_path


def _events_from_content(record: JsonlRecord, role: Role, content: Any) -> Iterable[Event]:
    timestamp = string(record.value.get("timestamp"))
    if isinstance(content, str):
        if content:
            yield Event(
                kind=EventKind.MESSAGE,
                role=role,
                text=content,
                timestamp=timestamp,
                provenance=_provenance(record),
            )
        return
    if not isinstance(content, list):
        yield _opaque_event(record, "unsupported_message_content")
        return
    for block_index, block in enumerate(content):
        provenance = _provenance(record, block_index=block_index)
        if not isinstance(block, dict):
            yield Event(kind=EventKind.OPAQUE, provenance=provenance)
            continue
        block_type = string(block.get("type"))
        if block_type == "text":
            text = string(block.get("text"))
            if text:
                yield Event(
                    kind=EventKind.MESSAGE,
                    role=role,
                    text=text,
                    timestamp=timestamp,
                    provenance=provenance,
                )
        elif block_type == "tool_use":
            yield Event(
                kind=EventKind.TOOL_CALL,
                role=Role.ASSISTANT,
                timestamp=timestamp,
                tool_name=string(block.get("name")),
                tool_call_id=string(block.get("id")),
                payload={"input": block.get("input", {})},
                provenance=provenance,
            )
        elif block_type == "tool_result":
            normalized_content = _portable_tool_result_content(block.get("content"))
            yield Event(
                kind=EventKind.TOOL_RESULT,
                role=Role.TOOL,
                timestamp=timestamp,
                text=content_text(block.get("content")),
                tool_call_id=string(block.get("tool_use_id")),
                payload={
                    "is_error": block.get("is_error") is True,
                    "content_blocks": normalized_content,
                },
                provenance=provenance,
            )
        elif block_type in {"thinking", "redacted_thinking"}:
            yield Event(
                kind=EventKind.THINKING,
                role=Role.ASSISTANT,
                timestamp=timestamp,
                payload={"source_block_type": block_type},
                provenance=provenance,
            )
        elif block_type in {"image", "document"}:
            payload: dict[str, Any] = {"block_type": block_type}
            if block_type == "image":
                image_url = image_url_from_claude_source(block.get("source"))
                if image_url:
                    payload["image_url"] = image_url
            yield Event(
                kind=EventKind.CONTEXT,
                role=role,
                timestamp=timestamp,
                payload=payload,
                provenance=provenance,
            )
        else:
            yield Event(
                kind=EventKind.OPAQUE,
                role=role,
                timestamp=timestamp,
                payload={"source_block_type": block_type or "<missing>"},
                provenance=provenance,
            )


def _opaque_event(record: JsonlRecord, reason: str) -> Event:
    return Event(
        kind=EventKind.OPAQUE,
        timestamp=string(record.value.get("timestamp")),
        payload={"reason": reason},
        provenance=_provenance(record),
    )


def _portable_tool_result_content(value: Any) -> list[dict[str, Any]]:
    """Normalize Claude tool-result blocks without retaining source-specific wrappers."""

    if isinstance(value, str):
        return [{"type": "text", "text": value}]
    if not isinstance(value, list):
        return [{"type": "opaque"}]
    result: list[dict[str, Any]] = []
    for block in value:
        if isinstance(block, str):
            result.append({"type": "text", "text": block})
        elif isinstance(block, dict) and block.get("type") == "text":
            text = string(block.get("text"))
            if text:
                result.append({"type": "text", "text": text})
        elif isinstance(block, dict) and block.get("type") == "image":
            image_url = image_url_from_claude_source(block.get("source"))
            if image_url:
                result.append({"type": "image", "image_url": image_url})
            else:
                result.append({"type": "opaque"})
        elif isinstance(block, dict) and block.get("type") == "tool_reference":
            tool_name = string(block.get("tool_name"))
            if tool_name:
                result.append({"type": "tool_reference", "tool_name": tool_name})
            else:
                result.append({"type": "opaque"})
        else:
            result.append({"type": "opaque"})
    return result


def _claude_tool_result_content(event: Event) -> tuple[str | list[dict[str, Any]], Counter[str]]:
    blocks = event.payload.get("content_blocks")
    if not isinstance(blocks, list):
        return event.text or "", Counter()
    result: list[dict[str, Any]] = []
    omitted: Counter[str] = Counter()
    for portable in blocks:
        if not isinstance(portable, dict):
            omitted["tool_result:block"] += 1
            continue
        block_type = portable.get("type")
        if block_type == "text" and isinstance(portable.get("text"), str):
            result.append({"type": "text", "text": portable["text"]})
        elif block_type == "image":
            source = claude_source_from_image_url(portable.get("image_url"))
            if source:
                result.append({"type": "image", "source": source})
            else:
                omitted["tool_result:image"] += 1
        elif block_type == "tool_reference" and isinstance(portable.get("tool_name"), str):
            result.append({"type": "tool_reference", "tool_name": portable["tool_name"]})
        else:
            omitted[f"tool_result:{block_type or 'block'}"] += 1
    if not result:
        return event.text or "", omitted
    if len(result) == 1 and result[0].get("type") == "text":
        return str(result[0]["text"]), omitted
    return result, omitted


def _provenance(record: JsonlRecord, *, block_index: int | None = None) -> Provenance:
    return Provenance(
        record_index=record.index,
        record_type=string(record.value.get("type")),
        source_id=string(record.value.get("uuid")),
        block_index=block_index,
    )


def _omission_key(event: Event) -> str:
    if event.kind == EventKind.MESSAGE and event.role == Role.SYSTEM:
        return "message:privileged_role"
    if event.kind == EventKind.CONTEXT:
        if event.role == Role.SYSTEM and event.payload.get("block_type") == "image":
            return "context:privileged_image"
        if event.payload.get("source_record_type"):
            return f"context:{event.payload['source_record_type']}"
        return f"context:{event.payload.get('block_type', 'unknown')}"
    if event.kind == EventKind.COMPACTION and event.payload.get("replacement_history_expanded"):
        return "compaction:replacement_history_expanded"
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


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
