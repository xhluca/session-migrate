import json
import sqlite3
import stat
from pathlib import Path

import pytest

import session_bridge.catalog as catalog_module
from session_bridge.catalog import Catalog, auto_roots, default_catalog_path, discover_roots
from session_bridge.errors import JsonlError, SessionBridgeError
from session_bridge.model import AgentFormat

CLAUDE_ID = "11111111-1111-4111-8111-111111111111"
CLAUDE_DUPLICATE_ID = "22222222-2222-4222-8222-222222222222"
SIDECHAIN_ID = "33333333-3333-4333-8333-333333333333"
CODEX_ID = "44444444-4444-4444-8444-444444444444"
ARCHIVED_ID = "55555555-5555-4555-8555-555555555555"
PAGINATED_ID = "66666666-6666-4666-8666-666666666666"
CORRUPT_ID = "77777777-7777-4777-8777-777777777777"


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
    session_id: str,
    *,
    title: str | None = None,
    history_mode: str = "legacy",
    current_name_field: bool = False,
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
                "payload": {
                    "type": "thread_name_updated",
                    ("thread_name" if current_name_field else "name"): title,
                },
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
    corrupt = first / "projects" / "-synthetic" / f"{CORRUPT_ID}.jsonl"
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

        duplicate_two.unlink()
        catalog.refresh(include_auto=False)
        remaining_duplicate = catalog.list_sessions(query=CLAUDE_DUPLICATE_ID)
        assert len(remaining_duplicate) == 1
        assert remaining_duplicate[0].duplicate is False

        sidechains = catalog.list_sessions(kinds=("sidechain",))
        assert len(sidechains) == 1
        assert sidechains[0].status == "unsupported"
        assert sidechains[0].reason == "claude_sidechain"

        corrupt_by_filename = catalog.list_sessions(query=CORRUPT_ID)
        assert len(corrupt_by_filename) == 1
        assert corrupt_by_filename[0].status == "corrupt"
        assert corrupt_by_filename[0].session_id is None
        assert corrupt_by_filename[0].filename_session_id == CORRUPT_ID

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
    _write_jsonl(
        active,
        _codex_records(CODEX_ID, title="Rollout event name", current_name_field=True),
    )
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

        # A transiently unavailable vendor cache must not erase title metadata
        # already derived from it when the authoritative JSONL changes.
        database.rename(home / "temporarily-unavailable.sqlite")
        with archived.open("a") as stream:
            stream.write(
                json.dumps(
                    {
                        "timestamp": "2026-08-18T13:00:03Z",
                        "type": "world_state",
                        "payload": {},
                    }
                )
                + "\n"
            )
        catalog.refresh(include_auto=False)
        retained_name = catalog.list_sessions(query="native saved name")
        assert len(retained_name) == 1
        assert retained_name[0].title == "Native saved name"


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

    shared = tmp_path / "preexisting-shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    with Catalog(shared / "catalog.sqlite3"):
        pass
    assert stat.S_IMODE(shared.stat().st_mode) == 0o755


def test_catalog_v1_migrates_transactionally_and_preserves_roots_and_rows(
    tmp_path: Path,
) -> None:
    database = tmp_path / "old-catalog.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE catalog_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO catalog_meta VALUES ('schema_version', '1');
        CREATE TABLE roots (
            id INTEGER PRIMARY KEY, format TEXT NOT NULL, path TEXT NOT NULL,
            source TEXT NOT NULL, enabled INTEGER NOT NULL, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, last_scan_at TEXT, last_scan_status TEXT,
            last_error TEXT, UNIQUE(format, path)
        );
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY, catalog_id TEXT NOT NULL UNIQUE,
            root_id INTEGER NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
            relative_path TEXT NOT NULL, canonical_path TEXT NOT NULL,
            format TEXT NOT NULL, session_id TEXT, filename_session_id TEXT,
            display_title TEXT, display_title_kind TEXT, cwd TEXT, started_at TEXT,
            cli_version TEXT, history_mode TEXT, kind TEXT NOT NULL,
            lifecycle TEXT NOT NULL, parent_session_id TEXT, status TEXT NOT NULL,
            reason TEXT, records INTEGER, device INTEGER NOT NULL,
            inode INTEGER NOT NULL, bytes INTEGER NOT NULL, modified_ns INTEGER NOT NULL,
            indexed_at TEXT NOT NULL, validated_at TEXT, missing_since TEXT,
            UNIQUE(root_id, relative_path)
        );
        CREATE TABLE session_labels (
            id INTEGER PRIMARY KEY,
            session_row_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            kind TEXT NOT NULL, value TEXT NOT NULL, normalized TEXT NOT NULL,
            ordinal INTEGER NOT NULL, priority INTEGER NOT NULL,
            UNIQUE(session_row_id, kind, value)
        );
        """
    )
    old_title = "m" * 700
    connection.execute(
        "INSERT INTO roots VALUES (1, 'claude', ?, 'registered', 1, ?, ?, NULL, NULL, NULL)",
        (str(tmp_path / "claude"), "2026-08-18T00:00:00Z", "2026-08-18T00:00:00Z"),
    )
    connection.execute(
        """
        INSERT INTO sessions VALUES (
            1, 'oldcatalogid0001', 1, 'projects/synthetic.jsonl', ?, 'claude', ?, ?,
            ?, 'custom_title', '/synthetic', '2026-08-18T12:00:00Z', '2.1.209', NULL,
            'main', 'project', NULL, 'candidate', NULL, 2, 1, 2, 3, 4,
            '2026-08-18T00:00:00Z', NULL, NULL
        )
        """,
        (str(tmp_path / "source.jsonl"), CLAUDE_ID, CLAUDE_ID, old_title),
    )
    connection.execute(
        "INSERT INTO session_labels VALUES (1, 1, 'custom_title', ?, ?, 1, 100)",
        (old_title, old_title.casefold()),
    )
    connection.commit()
    connection.close()

    with Catalog(database) as catalog:
        assert len(catalog.roots()) == 1
        entries = catalog.list_sessions(since="2026-08-18T11:59:59Z")
        assert len(entries) == 1
        assert entries[0].title == "m" * 512
        assert len(catalog.list_sessions(query="m" * 512)) == 1
        columns = {
            str(row[1])
            for row in catalog._connection.execute(  # noqa: SLF001
                "PRAGMA table_info(session_labels)"
            ).fetchall()
        }
        assert "normalized" not in columns
        assert catalog._connection.execute(  # noqa: SLF001
            "SELECT value FROM catalog_meta WHERE key = 'schema_version'"
        ).fetchone()[0] == "2"


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


def test_lifecycle_and_rfc3339_time_filters(tmp_path: Path) -> None:
    claude_home = tmp_path / "claude"
    claude_path = claude_home / "projects" / "-synthetic" / f"{CLAUDE_ID}.jsonl"
    _write_jsonl(claude_path, _claude_records(CLAUDE_ID, "Earlier project"))
    codex_home = tmp_path / "codex"
    codex_path = (
        codex_home
        / "archived_sessions"
        / f"rollout-synthetic-{CODEX_ID}.jsonl"
    )
    _write_jsonl(codex_path, _codex_records(CODEX_ID, title="Later archive"))

    with _catalog(tmp_path) as catalog:
        catalog.refresh(
            claude_roots=(claude_home,), codex_roots=(codex_home,), include_auto=False
        )
        later = catalog.list_sessions(since="2026-08-18T12:30:00Z")
        assert len(later) == 1
        assert later[0].lifecycle == "archived"
        earlier = catalog.list_sessions(
            until="2026-08-18T12:30:00+00:00", lifecycles=("project",)
        )
        assert len(earlier) == 1
        assert earlier[0].format == "claude"
        with pytest.raises(SessionBridgeError, match="timezone-aware RFC-3339"):
            catalog.list_sessions(since="2026-08-18")


def test_title_search_casefolds_unicode_and_bounds_stored_native_metadata(
    tmp_path: Path,
) -> None:
    claude_home = tmp_path / "claude"
    claude_path = claude_home / "projects" / "-synthetic" / f"{CLAUDE_ID}.jsonl"
    _write_jsonl(claude_path, _claude_records(CLAUDE_ID, "Straße investigation"))
    codex_home = tmp_path / "codex"
    codex_path = (
        codex_home
        / "sessions"
        / "2026"
        / "08"
        / "18"
        / f"rollout-synthetic-{CODEX_ID}.jsonl"
    )
    _write_jsonl(codex_path, _codex_records(CODEX_ID))
    database = codex_home / "state_5.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, title TEXT)")
    connection.execute(
        "INSERT INTO threads VALUES (?, ?, ?)", (CODEX_ID, str(codex_path), "x" * 2_000)
    )
    connection.commit()
    connection.close()

    with _catalog(tmp_path) as catalog:
        catalog.refresh(
            claude_roots=(claude_home,), codex_roots=(codex_home,), include_auto=False
        )
        assert len(catalog.list_sessions(query="STRASSE")) == 1
        bounded = catalog.list_sessions(query="x" * 512)
        assert len(bounded) == 1
        assert bounded[0].title == "x" * 512
        assert catalog.list_sessions(query="x" * 513) == []


def test_sidechain_native_agent_identity_is_searchable_without_paths(tmp_path: Path) -> None:
    home = tmp_path / "claude"
    sidechain = (
        home
        / "projects"
        / "-synthetic"
        / CLAUDE_ID
        / "subagents"
        / "agent-native-key.jsonl"
    )
    records = _claude_records(CLAUDE_ID, "Nested title")
    records[0]["isSidechain"] = True
    records[0]["agentId"] = "native-agent-id"
    _write_jsonl(sidechain, records)
    with _catalog(tmp_path) as catalog:
        catalog.refresh(claude_roots=(home,), include_auto=False)
        by_field = catalog.list_sessions(query="native-agent-id")
        by_filename = catalog.list_sessions(query="agent-native-key")
        assert len(by_field) == 1
        assert len(by_filename) == 1
        assert by_field[0].catalog_id == by_filename[0].catalog_id
        assert by_field[0].path is None


def test_discover_roots_is_bounded_and_requires_native_hidden_store_markers(
    tmp_path: Path,
) -> None:
    boundary = tmp_path / "workspace"
    claude_home = boundary / "one" / ".claude"
    codex_home = boundary / "two" / ".codex"
    (claude_home / "projects").mkdir(parents=True)
    (codex_home / "archived_sessions").mkdir(parents=True)
    (boundary / "ordinary" / "projects").mkdir(parents=True)
    outside = tmp_path / "outside" / ".claude"
    (outside / "projects").mkdir(parents=True)
    (boundary / "linked-outside").symlink_to(outside.parent, target_is_directory=True)

    found = discover_roots((boundary,))
    assert {(agent_format.value, path, source) for agent_format, path, source in found} == {
        ("claude", claude_home, "discovered"),
        ("codex", codex_home, "discovered"),
    }

    with _catalog(tmp_path) as catalog:
        result = catalog.refresh(
            discover_under=(boundary,), include_auto=False
        )
        assert result.roots == 2
        assert {root.source for root in catalog.roots()} == {"discovered"}


def test_busy_oversized_and_unavailable_root_states_are_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "claude"
    busy_path = home / "projects" / "-synthetic" / f"{CLAUDE_ID}.jsonl"
    oversized_path = home / "projects" / "-synthetic" / "oversized.jsonl"
    _write_jsonl(busy_path, _claude_records(CLAUDE_ID, "Busy synthetic title"))
    oversized_path.touch()
    with oversized_path.open("r+b") as stream:
        stream.truncate(256 * 1024 * 1024 + 1)

    def changed(*_args: object, **_kwargs: object) -> None:
        raise JsonlError("source session changed while it was being read; retry")

    monkeypatch.setattr(catalog_module, "ensure_file_unchanged", changed)
    with _catalog(tmp_path) as catalog:
        first = catalog.refresh(claude_roots=(home,), include_auto=False)
        assert first.statuses == {"busy": 1, "oversized": 1}
        root = catalog.roots()[0]

        moved = tmp_path / "temporarily-unavailable"
        home.rename(moved)
        failed = catalog.refresh(include_auto=False)
        assert failed.root_errors == 1
        assert catalog.roots()[0].last_error == "root_unavailable"
        assert catalog.list_sessions(include_missing=True, limit=10)
        assert all(entry.status != "missing" for entry in catalog.list_sessions(limit=10))
        assert catalog.roots()[0].id == root.id
