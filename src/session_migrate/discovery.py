"""Content-safe lookup of native sessions by UUID."""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

from session_migrate.errors import SessionMigrateError
from session_migrate.formats import claude, cursor, kimi, omp, pi, qwen, vibe
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
            raise SessionMigrateError(
                "--source-cwd applies only to Claude/Pi/OMP/Cursor/Vibe/Qwen/Kimi discovery"
            )
        matches = _codex_matches(home, normalized_id)
    elif source_format == AgentFormat.PI:
        matches = _pi_matches(home, normalized_id, cwd)
    elif source_format == AgentFormat.OMP:
        matches = _omp_matches(home, normalized_id, cwd)
    elif source_format == AgentFormat.COPILOT:
        if cwd is not None:
            raise SessionMigrateError(
                "--source-cwd applies only to Claude/Pi/OMP/Cursor/Vibe/Qwen/Kimi discovery"
            )
        matches = [home / "session-state" / normalized_id / "events.jsonl"]
    elif source_format == AgentFormat.ANTIGRAVITY:
        if cwd is not None:
            raise SessionMigrateError(
                "--source-cwd applies only to Claude/Pi/OMP/Cursor/Vibe/Qwen/Kimi discovery"
            )
        matches = [home / "conversations" / f"{normalized_id}.db"]
    elif source_format == AgentFormat.CURSOR:
        matches = _cursor_matches(home, normalized_id, cwd)
    elif source_format == AgentFormat.VIBE:
        matches = _vibe_matches(home, normalized_id, cwd)
    elif source_format == AgentFormat.MUSE:
        if cwd is not None:
            raise SessionMigrateError(
                "--source-cwd applies only to Claude/Pi/OMP/Cursor/Vibe/Qwen/Kimi discovery"
            )
        matches = list(home.glob(f"sessions/*/*/*/{normalized_id}/session.jsonl"))
    elif source_format == AgentFormat.QWEN:
        matches = _qwen_matches(home, normalized_id, cwd)
    elif source_format == AgentFormat.KIMI:
        matches = _kimi_matches(home, normalized_id, cwd)
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
            if source_format
            in {
                AgentFormat.CLAUDE,
                AgentFormat.PI,
                AgentFormat.OMP,
                AgentFormat.CURSOR,
                AgentFormat.VIBE,
            }
            | {AgentFormat.QWEN, AgentFormat.KIMI}
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
        return list((sessions / pi.session_directory_name(cwd)).glob(f"*_{session_id}.jsonl"))
    return list(sessions.glob(f"*/*_{session_id}.jsonl"))


def _omp_matches(home: Path, session_id: str, cwd: Path | None) -> list[Path]:
    sessions = home / "sessions"
    if cwd is not None:
        return list((sessions / omp.session_directory_name(cwd)).glob(f"*_{session_id}.jsonl"))
    return list(sessions.glob(f"*/*_{session_id}.jsonl"))


def _cursor_matches(home: Path, session_id: str, cwd: Path | None) -> list[Path]:
    chats = home / "chats"
    if cwd is not None:
        return [chats / cursor.workspace_key(cwd) / session_id / "store.db"]
    return list(chats.glob(f"*/{session_id}/store.db"))


def _vibe_matches(home: Path, session_id: str, cwd: Path | None) -> list[Path]:
    candidates = list(
        (home / "logs/session").glob(f"session_*_{session_id[:8]}/{vibe.MESSAGES_FILENAME}")
    )
    matches: list[Path] = []
    for messages_path in candidates:
        meta_path = messages_path.parent / vibe.META_FILENAME
        try:
            metadata = json.loads(meta_path.read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(metadata, dict) or metadata.get("session_id") != session_id:
            continue
        if cwd is not None:
            environment = metadata.get("environment")
            stored = environment.get("working_directory") if isinstance(environment, dict) else None
            if not isinstance(stored, str):
                continue
            try:
                if Path(stored).resolve() != cwd.resolve():
                    continue
            except OSError:
                continue
        matches.append(messages_path)
    return matches


def _qwen_matches(home: Path, session_id: str, cwd: Path | None) -> list[Path]:
    projects = home / "projects"
    if cwd is not None:
        return [projects / qwen.project_directory_name(cwd) / "chats" / f"{session_id}.jsonl"]
    return list(projects.glob(f"*/chats/{session_id}.jsonl"))


def _kimi_matches(home: Path, session_id: str, cwd: Path | None) -> list[Path]:
    native_id = kimi.native_session_id(session_id)
    if cwd is not None:
        return [
            home
            / "sessions"
            / kimi.workdir_key(cwd)
            / native_id
            / "agents/main"
            / kimi.WIRE_FILENAME
        ]
    return list(home.glob(f"sessions/*/{native_id}/agents/main/{kimi.WIRE_FILENAME}"))


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
    if source_format == AgentFormat.KIMI:
        return kimi.native_session_id(value)
    return normalized_session_id(value)
