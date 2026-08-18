import json
import sqlite3
import stat
from pathlib import Path

from session_bridge.catalog import Catalog, auto_roots, default_catalog_path
from session_bridge.model import AgentFormat

CLAUDE_ID = "11111111-1111-4111-8111-111111111111"
CLAUDE_DUPLICATE_ID = "22222222-2222-4222-8222-222222222222"
SIDECHAIN_ID = "33333333-3333-4333-8333-333333333333"
CODEX_ID = "44444444-4444-4444-8444-444444444444"
ARCHIVED_ID = "55555555-5555-4555-8555-555555555555"
PAGINATED_ID = "66666666-6666-4666-8666-666666666666"


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")


def _claude_records(session_id: str, title: str) -> list[dict[str, object]]:
    return [
        {
            "type": "user",
            "uuid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "parentUuid": None,
            "sessionId": session_id,
            "timestamp": "2026-08-18T12:00:00Z",
            "cwd": "/synthetic/project",
            "version": "2.1.209",
            "isSidechain": False,
            "message": {"role": "user", "content": "not indexed"},
        },
        {"type": "ai-title", "aiTitle": "Older synthetic title", "sessionId": session_id},
        {"type": "custom-title", "customTitle": title, "sessionId": session_id},
    ]


def _codex_records(
    session_id: str, *, title: str | None = None, history_mode: str = "legacy"
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = [
        {
            "timestamp": "2026-08-18T13:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "session_id": session_id,
                "timestamp": "2026-08-18T13:00:00Z",
                "cwd": "/synthetic/work",
                "cli_version": "0.144.4",
                "history_mode": history_mode,
            },
        },
        {
            "timestamp": "2026-08-18T13:00:01Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "not indexed"}],
            },
        },
    ]
    if title:
        records.append(
            {
                "timestamp": "2026-08-18T13:00:02Z",
                "type": "event_msg",
                "payload": {"type": "thread_name_updated", "name": title},
            }
        )
    return records


def _catalog(tmp_path: Path) -> Catalog:
    return Catalog(tmp_path / "private-state" / "catalog.sqlite3")


def test_refresh_indexes_main_sidechain_corrupt_duplicate_and_missing(tmp_path: Path) -> None:
    first = tmp_path / "claude-one"
    second = tmp_path / "claude-two"
    main = first / "projects" / "-synthetic" / f"{CLAUDE_ID}.jsonl"
    duplicate_one = first / "projects" / "-synthetic" / f"{CLAUDE_DUPLICATE_ID}.jsonl"
    duplicate_two = second / "projects" / "-other" / f"{CLAUDE_DUPLICATE_ID}.jsonl"
    sidechain = (
        first
        / "projects"
        / "-synthetic"
        / CLAUDE_ID
        / "subagents"
        / "agent-synthetic.jsonl"
    )
    corrupt = first / "projects" / "-synthetic" / "malformed.jsonl"
    _write_jsonl(main, _claude_records(CLAUDE_ID, "Named synthetic session"))
    _write_jsonl(duplicate_one, _claude_records(CLAUDE_DUPLICATE_ID, "First duplicate"))
    _write_jsonl(duplicate_two, _claude_records(CLAUDE_DUPLICATE_ID, "Second duplicate"))
    side_records = _claude_records(SIDECHAIN_ID, "Nested synthetic sidechain")
    side_records[0]["isSidechain"] = True
    _write_jsonl(sidechain, side_records)
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_text("{not-json}\n")

    with _catalog(tmp_path) as catalog:
        result = catalog.refresh(
            claude_roots=(first, second), include_auto=False
        )
        assert result.files_seen == 5
        assert result.statuses == {"candidate": 3, "corrupt": 1, "unsupported": 1}

        named = catalog.list_sessions(query="named synthetic")
        assert len(named) == 1
        assert named[0].title == "Named synthetic session"
        assert named[0].path is None
        assert named[0].cwd is None

        by_uuid = catalog.list_sessions(query=CLAUDE_DUPLICATE_ID)
        assert len(by_uuid) == 2
        assert all(entry.duplicate for entry in by_uuid)

        sidechains = catalog.list_sessions(kinds=("sidechain",))
        assert len(sidechains) == 1
        assert sidechains[0].status == "unsupported"
        assert sidechains[0].reason == "claude_sidechain"

        main.unlink()
        refreshed = catalog.refresh(include_auto=False)
        assert refreshed.missing == 1
        assert catalog.list_sessions(query=CLAUDE_ID) == []
        missing = catalog.list_sessions(
            query=CLAUDE_ID,
            include_missing=True,
            include_paths=True,
            statuses=("missing",),
        )
        assert len(missing) == 1
        assert missing[0].status == "missing"
        assert missing[0].path == str(main.resolve())


def test_codex_active_archive_paginated_and_native_titles(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    active = (
        home
        / "sessions"
        / "2026"
        / "08"
        / "18"
        / f"rollout-synthetic-{CODEX_ID}.jsonl"
    )
    archived = home / "archived_sessions" / f"rollout-synthetic-{ARCHIVED_ID}.jsonl"
    paginated = (
        home
        / "sessions"
        / "2026"
        / "08"
        / "18"
        / f"rollout-synthetic-{PAGINATED_ID}.jsonl"
    )
    _write_jsonl(active, _codex_records(CODEX_ID, title="Rollout event name"))
    _write_jsonl(archived, _codex_records(ARCHIVED_ID))
    _write_jsonl(paginated, _codex_records(PAGINATED_ID, history_mode="paginated"))

    database = home / "state_5.sqlite"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE threads (
            id TEXT, rollout_path TEXT, name TEXT, title TEXT,
            preview TEXT, first_user_message TEXT
        );
        CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT);
        """
    )
    connection.execute(
        "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?)",
        (
            ARCHIVED_ID,
            str(archived),
            "Native saved name",
            "Native saved title",
            "forbidden preview marker",
            "forbidden first-message marker",
        ),
    )
    connection.execute(
        "INSERT INTO thread_spawn_edges VALUES (?, ?)", (CODEX_ID, ARCHIVED_ID)
    )
    connection.commit()
    connection.close()

    with _catalog(tmp_path) as catalog:
        result = catalog.refresh(codex_roots=(home,), include_auto=False)
        assert result.statuses == {"candidate": 2, "unsupported": 1}
        event_name = catalog.list_sessions(query="rollout event")
        assert len(event_name) == 1
        assert event_name[0].title_kind == "thread_name"

        native_name = catalog.list_sessions(query="native saved name")
        assert len(native_name) == 1
        assert native_name[0].lifecycle == "archived"
        assert native_name[0].kind == "subagent"
        assert catalog.list_sessions(query="forbidden preview") == []
        assert catalog.list_sessions(query="forbidden first-message") == []

        unsupported = catalog.list_sessions(statuses=("unsupported",))
        assert len(unsupported) == 1
        assert unsupported[0].history_mode == "paginated"
        assert unsupported[0].reason == "codex_history_mode"


def test_refresh_is_incremental_and_validation_is_explicit(tmp_path: Path) -> None:
    home = tmp_path / "claude"
    session = home / "projects" / "-synthetic" / f"{CLAUDE_ID}.jsonl"
    _write_jsonl(session, _claude_records(CLAUDE_ID, "Incremental title"))

    with _catalog(tmp_path) as catalog:
        first = catalog.refresh(claude_roots=(home,), include_auto=False)
        assert first.scanned == 1
        assert first.unchanged == 0
        second = catalog.refresh(include_auto=False)
        assert second.scanned == 0
        assert second.unchanged == 1

        # Full conversion is deliberately deferred until explicitly requested.
        deep = catalog.refresh(include_auto=False, validate=True)
        assert deep.scanned == 1
        assert catalog.list_sessions()[0].status == "validated"
        assert catalog.list_sessions()[0].reason is None


def test_auto_roots_are_bounded_to_defaults_environment_and_ancestors(
    tmp_path: Path,
) -> None:
    user_home = tmp_path / "home"
    default_claude = user_home / ".claude" / "projects"
    default_claude.mkdir(parents=True)
    custom_codex = tmp_path / "custom-codex"
    (custom_codex / "sessions").mkdir(parents=True)
    project = tmp_path / "work" / "nested"
    (tmp_path / "work" / ".codex" / "archived_sessions").mkdir(parents=True)
    project.mkdir(parents=True)
    unrelated = tmp_path / "elsewhere" / ".claude" / "projects"
    unrelated.mkdir(parents=True)

    roots = auto_roots(
        cwd=project,
        environ={"CODEX_HOME": str(custom_codex)},
        home=user_home,
    )
    root_set = {(agent_format.value, path, source) for agent_format, path, source in roots}
    assert ("claude", user_home / ".claude", "default") in root_set
    assert ("codex", custom_codex, "environment") in root_set
    assert ("codex", tmp_path / "work" / ".codex", "project") in root_set
    assert all(path != tmp_path / "elsewhere" / ".claude" for _, path, _ in roots)


def test_catalog_files_are_private_and_default_path_is_configurable(tmp_path: Path) -> None:
    configured = tmp_path / "configured" / "sessions.sqlite3"
    assert default_catalog_path(environ={"SESSION_BRIDGE_CATALOG": str(configured)}) == configured
    with Catalog(configured):
        pass
    assert stat.S_IMODE(configured.stat().st_mode) == 0o600
    assert stat.S_IMODE(configured.parent.stat().st_mode) == 0o700


def test_remove_root_removes_catalog_rows_but_not_native_files(tmp_path: Path) -> None:
    home = tmp_path / "claude"
    session = home / "projects" / "-synthetic" / f"{CLAUDE_ID}.jsonl"
    _write_jsonl(session, _claude_records(CLAUDE_ID, "Preserved native source"))
    with _catalog(tmp_path) as catalog:
        root = catalog.add_root(AgentFormat.CLAUDE, home)
        catalog.refresh(include_auto=False)
        assert len(catalog.list_sessions()) == 1
        assert catalog.remove_root(root.id)
        assert catalog.list_sessions() == []
        assert session.exists()
        assert not catalog.remove_root(root.id)


def test_include_paths_controls_path_search_and_output(tmp_path: Path) -> None:
    home = tmp_path / "claude"
    session = home / "projects" / "encoded-marker" / f"{CLAUDE_ID}.jsonl"
    _write_jsonl(session, _claude_records(CLAUDE_ID, "Ordinary title"))
    with _catalog(tmp_path) as catalog:
        catalog.refresh(claude_roots=(home,), include_auto=False)
        assert catalog.list_sessions(query="encoded-marker") == []
        result = catalog.list_sessions(query="encoded-marker", include_paths=True)
        assert len(result) == 1
        assert result[0].path == str(session.resolve())
        assert result[0].root == str(home.resolve())
        assert result[0].cwd == "/synthetic/project"
