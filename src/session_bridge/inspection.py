"""Content-free format detection and structural inventory."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from session_bridge.errors import FormatDetectionError, JsonlError
from session_bridge.jsonl import ensure_file_unchanged, file_sha256, file_snapshot, iter_jsonl
from session_bridge.model import AgentFormat

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
    before = file_snapshot(path)
    records = list(iter_jsonl(path))
    if not records:
        raise JsonlError(f"session file contains no JSON records: {path}")
    detected = source_format or detect_format([record.value for record in records])

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
        else:
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
    claude_score = 0
    codex_score = 0
    for value in records:
        if not isinstance(value, dict):
            continue
        record_type = value.get("type")
        payload = value.get("payload")
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
    if claude_decisive and codex_decisive:
        raise FormatDetectionError(
            "session contains decisive markers for both Claude Code and Codex"
        )
    if codex_decisive:
        return AgentFormat.CODEX
    if claude_decisive:
        return AgentFormat.CLAUDE
    if codex_score and codex_score > claude_score:
        return AgentFormat.CODEX
    if claude_score and claude_score > codex_score:
        return AgentFormat.CLAUDE
    raise FormatDetectionError(
        "cannot distinguish Claude Code from Codex session records; pass --format explicitly"
    )


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
