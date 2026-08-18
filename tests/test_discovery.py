from pathlib import Path

import pytest

from session_bridge.discovery import locate_session
from session_bridge.errors import SessionBridgeError
from session_bridge.formats.claude import project_directory_name
from session_bridge.model import AgentFormat

SESSION_ID = "11111111-1111-4111-8111-111111111111"


def test_locates_claude_session_by_uuid_and_cwd(tmp_path: Path) -> None:
    home = tmp_path / "claude"
    cwd = tmp_path / "project"
    path = home / "projects" / project_directory_name(cwd) / f"{SESSION_ID}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("{}\n")

    assert locate_session(AgentFormat.CLAUDE, SESSION_ID, home, cwd=cwd) == path
    assert locate_session(AgentFormat.CLAUDE, SESSION_ID, home) == path


def test_claude_discovery_rejects_ambiguous_uuid(tmp_path: Path) -> None:
    home = tmp_path / "claude"
    for project in ("project-a", "project-b"):
        path = home / "projects" / project / f"{SESSION_ID}.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("{}\n")

    with pytest.raises(SessionBridgeError, match="multiple claude sessions"):
        locate_session(AgentFormat.CLAUDE, SESSION_ID, home)


def test_locates_codex_active_and_rejects_archive_duplicate(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    active = home / "sessions" / "2026" / "08" / "17" / f"rollout-time-{SESSION_ID}.jsonl"
    active.parent.mkdir(parents=True)
    active.write_text("{}\n")

    assert locate_session(AgentFormat.CODEX, SESSION_ID, home) == active

    archived = home / "archived_sessions" / f"rollout-time-{SESSION_ID}.jsonl"
    archived.parent.mkdir(parents=True)
    archived.write_text("{}\n")
    with pytest.raises(SessionBridgeError, match="multiple codex sessions"):
        locate_session(AgentFormat.CODEX, SESSION_ID, home)


def test_discovery_rejects_invalid_or_missing_uuid(tmp_path: Path) -> None:
    with pytest.raises(SessionBridgeError, match="not a valid UUID"):
        locate_session(AgentFormat.CLAUDE, "not-a-uuid", tmp_path)
    with pytest.raises(SessionBridgeError, match="no codex session found"):
        locate_session(AgentFormat.CODEX, SESSION_ID, tmp_path)
