"""Content-safe lookup of native sessions by UUID."""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

from session_migrate.errors import SessionMigrateError
from session_migrate.formats import claude, pi
from session_migrate.model import AgentFormat

_OPENCODE_SESSION_ID = re.compile(r"ses_[0-9A-Za-z]{1,128}")


def locate_session(
    source_format: AgentFormat,
    session_id: str,
    source_home: Path,
    *,
    cwd: Path | None = None,
) -> Path:
    """Locate one top-level native transcript without consulting mutable indexes."""

    normalized_id = normalized_source_id(source_format, session_id)
    home = Path(os.path.abspath(source_home.expanduser()))
    if source_format == AgentFormat.CLAUDE:
        matches = _claude_matches(home, normalized_id, cwd)
    elif source_format == AgentFormat.CODEX:
        if cwd is not None:
            raise SessionMigrateError("--source-cwd applies only to Claude/Pi session discovery")
        matches = _codex_matches(home, normalized_id)
    elif source_format == AgentFormat.PI:
        matches = _pi_matches(home, normalized_id, cwd)
    elif source_format == AgentFormat.COPILOT:
        if cwd is not None:
            raise SessionMigrateError(
                "--source-cwd applies only to Claude/Pi session discovery"
            )
        matches = [home / "session-state" / normalized_id / "events.jsonl"]
    else:
        raise SessionMigrateError(
            "OpenCode sessions are exported through its official CLI, not located as files"
        )
    matches = sorted({path for path in matches if path.is_file()})
    if not matches:
        raise SessionMigrateError(
            f"no {source_format.value} session found for UUID in the selected source home"
        )
    if len(matches) > 1:
        hint = (
            "pass --source-cwd"
            if source_format in {AgentFormat.CLAUDE, AgentFormat.PI}
            else "remove duplicates"
        )
        raise SessionMigrateError(
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


def _pi_matches(home: Path, session_id: str, cwd: Path | None) -> list[Path]:
    sessions = home / "sessions"
    if cwd is not None:
        return list(
            (sessions / pi.session_directory_name(cwd)).glob(f"*_{session_id}.jsonl")
        )
    return list(sessions.glob(f"*/*_{session_id}.jsonl"))


def normalized_session_id(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise SessionMigrateError(f"source session ID is not a valid UUID: {value}") from exc


def normalized_source_id(source_format: AgentFormat, value: str) -> str:
    """Normalize a native source ID without pretending every agent uses UUIDs."""

    if source_format == AgentFormat.OPENCODE:
        if not _OPENCODE_SESSION_ID.fullmatch(value):
            raise SessionMigrateError("source OpenCode session ID is invalid")
        return value
    return normalized_session_id(value)
