"""Content-free format detection and structural inventory."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from session_migrate.errors import FormatDetectionError, JsonlError, SessionMigrateError
from session_migrate.formats import antigravity, cursor, vibe
from session_migrate.jsonl import (
    DEFAULT_MAX_TOTAL_BYTES,
    ensure_file_unchanged,
    file_sha256,
    file_snapshot,
    iter_jsonl,
)
from session_migrate.model import AgentFormat

CLAUDE_RECORD_TYPES = {
    "assistant",
    "user",
    "system",
    "progress",
    "summary",
    "file-history-snapshot",
    "custom-title",
    "ai-title",
    "queue-operation",
    "attachment",
    "last-prompt",
}
CODEX_RECORD_TYPES = {
    "session_meta",
    "response_item",
    "event_msg",
    "turn_context",
    "compacted",
    "world_state",
    "inter_agent_communication",
    "inter_agent_communication_metadata",
    "security_risk_score",
}
PI_ENTRY_TYPES = {
    "session",
    "message",
    "model_change",
    "thinking_level_change",
    "compaction",
    "branch_summary",
    "custom",
    "custom_message",
    "label",
    "session_info",
}
COPILOT_EVENT_TYPES = {
    "session.start",
    "session.resume",
    "session.shutdown",
    "session.compaction_complete",
    "session.binary_asset",
    "user.message",
    "assistant.message",
    "tool.execution_start",
    "tool.execution_complete",
}


@dataclass(frozen=True, slots=True)
class Inspection:
    format: str
    path: str
    bytes: int
    sha256: str
    records: int
    session_id: str | None
    cwd: str | None
    cli_version: str | None
    started_at: str | None
    record_types: dict[str, int]
    roles: dict[str, int]
    content_blocks: dict[str, int]
    event_types: dict[str, int]
    tool_calls: int
    tool_results: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)


def inspect_session(path: Path, *, source_format: AgentFormat | None = None) -> Inspection:
    if source_format == AgentFormat.ANTIGRAVITY:
        parsed = antigravity.parse_session(path)
        return _inspect_portable_database(parsed)
    if source_format == AgentFormat.CURSOR:
        parsed = cursor.project_session(cursor.parse(path), source_format=AgentFormat.CURSOR)
        return _inspect_portable_database(parsed)
    if source_format == AgentFormat.VIBE or (
        path.is_dir()
        and (path / vibe.META_FILENAME).is_file()
        and (path / vibe.MESSAGES_FILENAME).is_file()
    ):
        return _inspect_portable_database(vibe.parse_session(path))
    if source_format is None and _has_sqlite_header(path):
        try:
            parsed = antigravity.parse_session(path)
        except SessionMigrateError:
            try:
                parsed = cursor.project_session(
                    cursor.parse(path), source_format=AgentFormat.CURSOR
                )
            except SessionMigrateError as exc:
                raise FormatDetectionError(
                    "SQLite source is not a supported Antigravity or Cursor conversation database"
                ) from exc
        return _inspect_portable_database(parsed)
    before = file_snapshot(path)
    if source_format == AgentFormat.OPENCODE or source_format is None:
        document = _load_json_document(path, before.size)
        if document is not None and (
            source_format == AgentFormat.OPENCODE or _is_opencode_document(document)
        ):
            result = _inspect_opencode(path, before.size, document)
            ensure_file_unchanged(path, before)
            return result
    records = list(iter_jsonl(path))
    if not records:
        raise JsonlError(f"session file contains no JSON records: {path}")
    detected = source_format or detect_format([record.value for record in records])
    if detected == AgentFormat.VIBE:
        ensure_file_unchanged(path, before)
        return _inspect_portable_database(vibe.parse_session(path))

    record_types: Counter[str] = Counter()
    roles: Counter[str] = Counter()
    blocks: Counter[str] = Counter()
    events: Counter[str] = Counter()
    session_id = None
    cwd = None
    cli_version = None
    started_at = None
    tool_calls = 0
    tool_results = 0

    for record in records:
        value = record.value
        record_type = _string(value.get("type")) or "<missing>"
        record_types[record_type] += 1
        if detected == AgentFormat.CLAUDE:
            session_id = session_id or _string(value.get("sessionId"))
            cwd = cwd or _string(value.get("cwd"))
            cli_version = cli_version or _string(value.get("version"))
            started_at = started_at or _string(value.get("timestamp"))
            message = value.get("message")
            if isinstance(message, dict):
                role = _string(message.get("role")) or record_type
                roles[role] += 1
                content = message.get("content")
                if isinstance(content, str):
                    blocks["text"] += 1
                elif isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            blocks["<non-object>"] += 1
                            continue
                        block_type = _string(block.get("type")) or "<missing>"
                        blocks[block_type] += 1
                        tool_calls += block_type == "tool_use"
                        tool_results += block_type == "tool_result"
        elif detected == AgentFormat.CODEX:
            payload = value.get("payload")
            if not isinstance(payload, dict):
                continue
            if record_type == "session_meta":
                session_id = session_id or _string(payload.get("id"))
                cwd = cwd or _string(payload.get("cwd"))
                cli_version = cli_version or _string(payload.get("cli_version"))
                started_at = started_at or _string(payload.get("timestamp"))
            elif record_type == "response_item":
                response_type = _string(payload.get("type")) or "<missing>"
                events[response_type] += 1
                role = _string(payload.get("role"))
                if role:
                    roles[role] += 1
                content = payload.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict):
                            blocks[_string(block.get("type")) or "<missing>"] += 1
                tool_calls += response_type in {"function_call", "custom_tool_call"}
                tool_results += response_type in {
                    "function_call_output",
                    "custom_tool_call_output",
                }
            elif record_type == "event_msg":
                events[_string(payload.get("type")) or "<missing>"] += 1
        elif detected == AgentFormat.PI:
            if record_type == "session":
                session_id = session_id or _string(value.get("id"))
                cwd = cwd or _string(value.get("cwd"))
                started_at = started_at or _string(value.get("timestamp"))
                events[f"schema_v{value.get('version', '<missing>')}"] += 1
            elif record_type == "message":
                message = value.get("message")
                if not isinstance(message, dict):
                    continue
                role = _string(message.get("role")) or "<missing>"
                roles[role] += 1
                content = message.get("content")
                if isinstance(content, str):
                    blocks["text"] += 1
                elif isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            blocks["<non-object>"] += 1
                            continue
                        block_type = _string(block.get("type")) or "<missing>"
                        blocks[block_type] += 1
                        tool_calls += block_type == "toolCall"
                tool_results += role == "toolResult"
            else:
                events[record_type] += 1
        else:
            data = value.get("data")
            if not isinstance(data, dict):
                continue
            if record_type == "session.start":
                session_id = session_id or _string(data.get("sessionId"))
                cli_version = cli_version or _string(data.get("copilotVersion"))
                started_at = started_at or _string(data.get("startTime"))
                context = data.get("context")
                if isinstance(context, dict):
                    cwd = cwd or _string(context.get("cwd"))
            elif record_type == "user.message":
                roles["user"] += 1
                if isinstance(data.get("content"), str):
                    blocks["text"] += 1
                attachments = data.get("attachments")
                if isinstance(attachments, list):
                    for attachment in attachments:
                        attachment_type = (
                            _string(attachment.get("type"))
                            if isinstance(attachment, dict)
                            else "<non-object>"
                        )
                        blocks[attachment_type or "<missing>"] += 1
            elif record_type == "assistant.message":
                roles["assistant"] += 1
                if isinstance(data.get("content"), str):
                    blocks["text"] += 1
                requests = data.get("toolRequests")
                if isinstance(requests, list):
                    tool_calls += sum(isinstance(request, dict) for request in requests)
            elif record_type == "tool.execution_complete":
                tool_results += 1
            events[record_type] += 1

    digest = file_sha256(path)
    ensure_file_unchanged(path, before)
    return Inspection(
        format=detected.value,
        path=str(path.resolve()),
        bytes=before.size,
        sha256=digest,
        records=len(records),
        session_id=session_id,
        cwd=cwd,
        cli_version=cli_version,
        started_at=started_at,
        record_types=dict(sorted(record_types.items())),
        roles=dict(sorted(roles.items())),
        content_blocks=dict(sorted(blocks.items())),
        event_types=dict(sorted(events.items())),
        tool_calls=tool_calls,
        tool_results=tool_results,
    )


def detect_format(records: list[dict[str, Any] | Any]) -> AgentFormat:
    claude_decisive = False
    codex_decisive = False
    pi_decisive = False
    copilot_decisive = False
    vibe_decisive = False
    claude_score = 0
    codex_score = 0
    pi_score = 0
    for value in records:
        if not isinstance(value, dict):
            continue
        record_type = value.get("type")
        payload = value.get("payload")
        data = value.get("data")
        if (
            record_type == "session.start"
            and isinstance(data, dict)
            and isinstance(data.get("sessionId"), str)
            and data.get("version") == 1
        ):
            copilot_decisive = True
        if (
            record_type is None
            and value.get("role") in {"system", "user", "assistant", "tool"}
            and (
                isinstance(value.get("message_id"), str)
                or isinstance(value.get("tool_call_id"), str)
                or isinstance(value.get("tool_calls"), list)
                or value.get("context_boundary") == "compaction"
            )
        ):
            vibe_decisive = True
        if (
            record_type == "session"
            and value.get("version") in {1, 2, 3}
            and isinstance(value.get("id"), str)
            and isinstance(value.get("cwd"), str)
        ):
            pi_decisive = True
        elif record_type in PI_ENTRY_TYPES - {"session"} and "parentId" in value:
            pi_score += 2
        if record_type == "session_meta" and isinstance(payload, dict):
            codex_decisive = True
        elif record_type in CODEX_RECORD_TYPES and isinstance(payload, dict):
            codex_score += 2
        if record_type in {"user", "assistant"} and isinstance(value.get("message"), dict):
            claude_score += 2
            if any(key in value for key in ("sessionId", "uuid", "parentUuid")):
                claude_decisive = True
        elif record_type in CLAUDE_RECORD_TYPES - {"system"} and not isinstance(payload, dict):
            claude_score += 1
        if "sessionId" in value or "parentUuid" in value:
            claude_score += 3
    decisive = sum((claude_decisive, codex_decisive, pi_decisive, copilot_decisive, vibe_decisive))
    if decisive > 1:
        raise FormatDetectionError("session contains decisive markers for multiple native formats")
    if codex_decisive:
        return AgentFormat.CODEX
    if claude_decisive:
        return AgentFormat.CLAUDE
    if pi_decisive:
        return AgentFormat.PI
    if copilot_decisive:
        return AgentFormat.COPILOT
    if vibe_decisive:
        return AgentFormat.VIBE
    if pi_score and pi_score > max(claude_score, codex_score):
        return AgentFormat.PI
    if codex_score and codex_score > claude_score:
        return AgentFormat.CODEX
    if claude_score and claude_score > codex_score:
        return AgentFormat.CLAUDE
    raise FormatDetectionError(
        "cannot distinguish Claude Code, Codex, Pi, OpenCode, Copilot, or Vibe records; "
        "pass --format explicitly"
    )


def detect_path_format(path: Path) -> AgentFormat:
    """Detect JSON-document and JSONL source formats under the normal input bounds."""

    if path.is_dir():
        if (path / vibe.META_FILENAME).is_file() and (path / vibe.MESSAGES_FILENAME).is_file():
            vibe.parse_session(path)
            return AgentFormat.VIBE
        raise FormatDetectionError("directory is not a supported native session")

    if _has_sqlite_header(path):
        try:
            antigravity.parse(path)
        except SessionMigrateError:
            try:
                cursor.parse(path)
            except SessionMigrateError as exc:
                raise FormatDetectionError(
                    "SQLite source is not a supported Antigravity or Cursor conversation database"
                ) from exc
            return AgentFormat.CURSOR
        else:
            return AgentFormat.ANTIGRAVITY
    before = file_snapshot(path)
    document = _load_json_document(path, before.size)
    if document is not None and _is_opencode_document(document):
        detected = AgentFormat.OPENCODE
    else:
        detected = detect_format([record.value for record in iter_jsonl(path)])
    ensure_file_unchanged(path, before)
    return detected


def _load_json_document(path: Path, size: int) -> dict[str, Any] | None:
    if size > DEFAULT_MAX_TOTAL_BYTES:
        return None
    try:
        value = json.loads(path.read_bytes(), parse_constant=_reject_json_constant)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _is_opencode_document(value: dict[str, Any]) -> bool:
    info = value.get("info")
    messages = value.get("messages")
    return (
        isinstance(info, dict)
        and isinstance(info.get("id"), str)
        and info["id"].startswith("ses_")
        and isinstance(messages, list)
    )


def _inspect_opencode(path: Path, size: int, value: dict[str, Any]) -> Inspection:
    info = value.get("info")
    messages = value.get("messages")
    assert isinstance(info, dict) and isinstance(messages, list)
    record_types: Counter[str] = Counter({"session": 1})
    roles: Counter[str] = Counter()
    blocks: Counter[str] = Counter()
    events: Counter[str] = Counter()
    tool_calls = 0
    tool_results = 0
    part_count = 0
    for message in messages:
        if not isinstance(message, dict):
            record_types["<malformed-message>"] += 1
            continue
        message_info = message.get("info")
        role = _string(message_info.get("role")) if isinstance(message_info, dict) else None
        roles[role or "<missing>"] += 1
        record_types["message"] += 1
        parts = message.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            part_count += 1
            part_type = (
                _string(part.get("type")) if isinstance(part, dict) else None
            ) or "<non-object>"
            blocks[part_type] += 1
            events[part_type] += 1
            if part_type == "tool" and isinstance(part, dict):
                tool_calls += 1
                state = part.get("state")
                if isinstance(state, dict) and state.get("status") in {"completed", "error"}:
                    tool_results += 1
    created = info.get("time")
    created_ms = created.get("created") if isinstance(created, dict) else None
    started_at = None
    if isinstance(created_ms, int) and created_ms >= 0:
        try:
            started_at = (
                datetime.fromtimestamp(created_ms / 1000, tz=UTC).isoformat().replace("+00:00", "Z")
            )
        except (OverflowError, OSError, ValueError):
            started_at = None
    return Inspection(
        format=AgentFormat.OPENCODE.value,
        path=str(path.resolve()),
        bytes=size,
        sha256=file_sha256(path),
        records=1 + len(messages) + part_count,
        session_id=_string(info.get("id")),
        cwd=_string(info.get("directory")),
        cli_version=_string(info.get("version")),
        started_at=started_at,
        record_types=dict(sorted(record_types.items())),
        roles=dict(sorted(roles.items())),
        content_blocks=dict(sorted(blocks.items())),
        event_types=dict(sorted(events.items())),
        tool_calls=tool_calls,
        tool_results=tool_results,
    )


def _inspect_portable_database(session: Any) -> Inspection:
    record_types: Counter[str] = Counter()
    roles: Counter[str] = Counter()
    blocks: Counter[str] = Counter()
    events: Counter[str] = Counter()
    tool_calls = 0
    tool_results = 0
    for event in session.events:
        record_types[event.provenance.record_type or event.kind.value] += 1
        events[event.kind.value] += 1
        if event.role is not None:
            roles[event.role.value] += 1
        if event.text:
            blocks["text"] += 1
        content = event.payload.get("content_blocks")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    blocks[_string(block.get("type")) or "<missing>"] += 1
                else:
                    blocks["<non-object>"] += 1
        tool_calls += event.kind.value == "tool_call"
        tool_results += event.kind.value == "tool_result"
    try:
        byte_count = session.source_path.stat().st_size
    except OSError:
        byte_count = 0
    return Inspection(
        format=session.source_format.value,
        path=str(session.source_path),
        bytes=byte_count,
        sha256=session.source_sha256,
        records=session.raw_record_count,
        session_id=session.session_id,
        cwd=str(session.cwd) if session.cwd else None,
        cli_version=session.cli_version,
        started_at=session.started_at,
        record_types=dict(sorted(record_types.items())),
        roles=dict(sorted(roles.items())),
        content_blocks=dict(sorted(blocks.items())),
        event_types=dict(sorted(events.items())),
        tool_calls=tool_calls,
        tool_results=tool_results,
    )


def _has_sqlite_header(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            return stream.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
