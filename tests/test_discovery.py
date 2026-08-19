from pathlib import Path

import pytest

from session_migrate.discovery import locate_session
from session_migrate.errors import SessionMigrateError
from session_migrate.formats.claude import project_directory_name
from session_migrate.formats.pi import session_directory_name
from session_migrate.model import AgentFormat

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

    with pytest.raises(SessionMigrateError, match="multiple claude sessions"):
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
    with pytest.raises(SessionMigrateError, match="multiple codex sessions"):
        locate_session(AgentFormat.CODEX, SESSION_ID, home)


def test_discovery_rejects_invalid_or_missing_uuid(tmp_path: Path) -> None:
    with pytest.raises(SessionMigrateError, match="not a valid UUID"):
        locate_session(AgentFormat.CLAUDE, "not-a-uuid", tmp_path)
    with pytest.raises(SessionMigrateError, match="no codex session found"):
        locate_session(AgentFormat.CODEX, SESSION_ID, tmp_path)


def test_locates_pi_session_by_uuid_and_cwd_and_rejects_duplicates(tmp_path: Path) -> None:
    home = tmp_path / "pi"
    cwd = tmp_path / "project"
    first = home / "sessions" / session_directory_name(cwd) / f"time_{SESSION_ID}.jsonl"
    first.parent.mkdir(parents=True)
    first.write_text("{}\n")

    assert locate_session(AgentFormat.PI, SESSION_ID, home, cwd=cwd) == first
    assert locate_session(AgentFormat.PI, SESSION_ID, home) == first

    second = home / "sessions" / "--other--" / f"later_{SESSION_ID}.jsonl"
    second.parent.mkdir(parents=True)
    second.write_text("{}\n")
    with pytest.raises(SessionMigrateError, match="multiple pi sessions"):
        locate_session(AgentFormat.PI, SESSION_ID, home)
    assert locate_session(AgentFormat.PI, SESSION_ID, home, cwd=cwd) == first
