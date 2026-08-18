"""Content-safe lookup of native sessions by UUID."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from session_bridge.errors import SessionBridgeError
from session_bridge.formats import claude
from session_bridge.model import AgentFormat


def locate_session(
    source_format: AgentFormat,
    session_id: str,
    source_home: Path,
    *,
    cwd: Path | None = None,
) -> Path:
    """Locate one top-level native transcript without consulting mutable indexes."""

    normalized_id = normalized_session_id(session_id)
    home = Path(os.path.abspath(source_home.expanduser()))
    if source_format == AgentFormat.CLAUDE:
        matches = _claude_matches(home, normalized_id, cwd)
    else:
        if cwd is not None:
            raise SessionBridgeError("--source-cwd applies only to Claude session discovery")
        matches = _codex_matches(home, normalized_id)
    matches = sorted({path for path in matches if path.is_file()})
    if not matches:
        raise SessionBridgeError(
            f"no {source_format.value} session found for UUID in the selected source home"
        )
    if len(matches) > 1:
        hint = "pass --source-cwd" if source_format == AgentFormat.CLAUDE else "remove duplicates"
        raise SessionBridgeError(
            f"multiple {source_format.value} sessions matched the UUID; {hint}"
        )
    return matches[0]


def _claude_matches(home: Path, session_id: str, cwd: Path | None) -> list[Path]:
    projects = home / "projects"
    if cwd is not None:
        return [projects / claude.project_directory_name(cwd) / f"{session_id}.jsonl"]
    return list(projects.glob(f"*/{session_id}.jsonl"))


def _codex_matches(home: Path, session_id: str) -> list[Path]:
    active = home.glob(f"sessions/*/*/*/rollout-*-{session_id}.jsonl")
    archived = (home / "archived_sessions").glob(f"rollout-*-{session_id}.jsonl")
    return [*active, *archived]


def normalized_session_id(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise SessionBridgeError(f"source session ID is not a valid UUID: {value}") from exc
