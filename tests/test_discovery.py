from pathlib import Path

import pytest

from session_migrate.discovery import locate_session, normalized_source_id
from session_migrate.errors import SessionMigrateError
from session_migrate.formats.claude import project_directory_name
from session_migrate.formats.cursor import workspace_key
from session_migrate.formats.grok import encode_cwd
from session_migrate.formats.omp import session_directory_name as omp_session_directory_name
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


def test_locates_omp_session_by_uuid_and_cwd_and_rejects_duplicates(tmp_path: Path) -> None:
    home = tmp_path / "omp"
    cwd = tmp_path / "project"
    first = home / "sessions" / omp_session_directory_name(cwd) / f"time_{SESSION_ID}.jsonl"
    first.parent.mkdir(parents=True)
    first.write_text("{}\n")

    assert locate_session(AgentFormat.OMP, SESSION_ID, home, cwd=cwd) == first
    assert locate_session(AgentFormat.OMP, SESSION_ID, home) == first

    second = home / "sessions" / "--other--" / f"later_{SESSION_ID}.jsonl"
    second.parent.mkdir(parents=True)
    second.write_text("{}\n")
    with pytest.raises(SessionMigrateError, match="multiple omp sessions"):
        locate_session(AgentFormat.OMP, SESSION_ID, home)
    assert locate_session(AgentFormat.OMP, SESSION_ID, home, cwd=cwd) == first


def test_locates_copilot_event_log(tmp_path: Path) -> None:
    home = tmp_path / "copilot"
    path = home / "session-state" / SESSION_ID / "events.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("{}\n")

    assert locate_session(AgentFormat.COPILOT, SESSION_ID, home) == path


def test_locates_antigravity_conversation_database(tmp_path: Path) -> None:
    home = tmp_path / "antigravity-cli"
    path = home / "conversations" / f"{SESSION_ID}.db"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"synthetic")

    assert locate_session(AgentFormat.ANTIGRAVITY, SESSION_ID, home) == path


def test_locates_cursor_store_by_uuid_and_cwd_and_rejects_duplicates(
    tmp_path: Path,
) -> None:
    home = tmp_path / "cursor"
    cwd = tmp_path / "project"
    first = home / "chats" / workspace_key(cwd) / SESSION_ID / "store.db"
    first.parent.mkdir(parents=True)
    first.write_bytes(b"synthetic")

    assert locate_session(AgentFormat.CURSOR, SESSION_ID, home, cwd=cwd) == first
    assert locate_session(AgentFormat.CURSOR, SESSION_ID, home) == first

    second = home / "chats" / ("f" * 32) / SESSION_ID / "store.db"
    second.parent.mkdir(parents=True)
    second.write_bytes(b"synthetic")
    with pytest.raises(SessionMigrateError, match="multiple cursor sessions"):
        locate_session(AgentFormat.CURSOR, SESSION_ID, home)
    assert locate_session(AgentFormat.CURSOR, SESSION_ID, home, cwd=cwd) == first


def test_normalizes_native_opencode_id_and_requires_official_export(tmp_path: Path) -> None:
    native_id = "ses_295e9e462ffeKSKb526cRKYtpw"
    assert normalized_source_id(AgentFormat.OPENCODE, native_id) == native_id
    with pytest.raises(SessionMigrateError, match="official CLI"):
        locate_session(AgentFormat.OPENCODE, native_id, tmp_path)
    with pytest.raises(SessionMigrateError, match="OpenCode session ID is invalid"):
        normalized_source_id(AgentFormat.OPENCODE, "../not-an-id")


def test_locates_grok_and_openhands_directory_sessions_and_normalizes_kilo(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "project"
    grok_home = tmp_path / "grok"
    grok_path = grok_home / "sessions" / encode_cwd(cwd) / SESSION_ID
    grok_path.mkdir(parents=True)
    (grok_path / "summary.json").write_text("{}")
    assert locate_session(AgentFormat.GROK, SESSION_ID, grok_home, cwd=cwd) == grok_path
    assert locate_session(AgentFormat.GROK, SESSION_ID, grok_home) == grok_path

    openhands_home = tmp_path / "openhands"
    events = openhands_home / SESSION_ID.replace("-", "") / "events"
    events.mkdir(parents=True)
    assert locate_session(AgentFormat.OPENHANDS, SESSION_ID, openhands_home) == events

    native_id = "ses_295e9e462ffeKSKb526cRKYtpw"
    assert normalized_source_id(AgentFormat.KILO, native_id) == native_id
    with pytest.raises(SessionMigrateError, match="Kilo session ID is invalid"):
        normalized_source_id(AgentFormat.KILO, "../not-an-id")
