"""Agent-neutral conversation event model."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class AgentFormat(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"
    PI = "pi"
    OPENCODE = "opencode"
    COPILOT = "copilot"
    ANTIGRAVITY = "antigravity"
    CURSOR = "cursor"
    VIBE = "vibe"
    MUSE = "muse"
    QWEN = "qwen"
    KIMI = "kimi"


class TargetFormat(StrEnum):
    """Writable destinations, kept separate from detectable source formats."""

    CLAUDE = "claude"
    CODEX = "codex"
    PI = "pi"
    OPENCODE = "opencode"
    COPILOT = "copilot"
    ANTIGRAVITY = "antigravity"
    CURSOR = "cursor"
    VIBE = "vibe"
    MUSE = "muse"
    QWEN = "qwen"
    KIMI = "kimi"


class Role(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class EventKind(StrEnum):
    MESSAGE = "message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    THINKING = "thinking"
    COMPACTION = "compaction"
    CONTEXT = "context"
    OPAQUE = "opaque"


@dataclass(frozen=True, slots=True)
class Provenance:
    record_index: int
    record_type: str | None = None
    source_id: str | None = None
    block_index: int | None = None


@dataclass(frozen=True, slots=True)
class Event:
    kind: EventKind
    provenance: Provenance
    role: Role | None = None
    timestamp: str | None = None
    text: str | None = None
    tool_name: str | None = None
    tool_call_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class Session:
    source_format: AgentFormat
    source_path: Path
    source_sha256: str
    session_id: str | None
    cwd: Path | None
    started_at: str | None
    cli_version: str | None
    model: str | None
    title: str | None
    events: tuple[Event, ...]
    raw_record_count: int
    model_provider: str | None = None

    def event_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(event.kind.value for event in self.events).items()))
