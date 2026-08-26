import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest

import session_migrate.catalog as catalog_module
from session_migrate.catalog import Catalog, auto_roots, default_catalog_path, discover_roots
from session_migrate.errors import JsonlError, SessionMigrateError
from session_migrate.formats import (
    antigravity,
    claude,
    cursor,
    grok,
    omp,
    openhands,
    vibe,
)
from session_migrate.model import AgentFormat

CLAUDE_ID = "11111111-1111-4111-8111-111111111111"
CLAUDE_DUPLICATE_ID = "22222222-2222-4222-8222-222222222222"
SIDECHAIN_ID = "33333333-3333-4333-8333-333333333333"
CODEX_ID = "44444444-4444-4444-8444-444444444444"
ARCHIVED_ID = "55555555-5555-4555-8555-555555555555"
PAGINATED_ID = "66666666-6666-4666-8666-666666666666"
CORRUPT_ID = "77777777-7777-4777-8777-777777777777"
PI_ID = "018f3d20-7a6b-7c8d-9e0f-123456789abc"
PI_PARENT_ID = "018f3d20-6a5b-7c8d-9e0f-123456789abc"
OMP_ID = "19191919-1919-4919-8919-191919191919"
OPENCODE_ID = "ses_295e9e462ffeKSKb526cRKYtpw"
OPENCODE_CHILD_ID = "ses_295e9e462ffeKSKb526cRKYtpx"
OPENCODE_ARCHIVED_ID = "ses_295e9e462ffeKSKb526cRKYtpy"
COPILOT_ID = "88888888-8888-4888-8888-888888888888"
ANTIGRAVITY_ID = "99999999-9999-4999-8999-999999999999"
CURSOR_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
VIBE_ID = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
GROK_ID = "16161616-1616-4616-8616-161616161616"
OPENHANDS_ID = "17171717-1717-4717-8717-171717171717"
KILO_ID = "ses_17171717171747178717171717171717"


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


def _pi_records(
    session_id: str, *, name: str = "Named Pi session", version: int = 3
) -> list[dict[str, object]]:
    return [
        {
            "type": "session",
            "version": version,
            "id": session_id,
            "timestamp": "2026-08-18T14:00:00Z",
            "cwd": "/synthetic/pi-work",
            "parentSession": f"2026-08-17T00-00-00-000Z_{PI_PARENT_ID}.jsonl",
        },
        {
            "type": "message",
            "id": "pi-user",
            "parentId": None,
            "timestamp": "2026-08-18T14:00:01Z",
            "message": {"role": "user", "content": "not indexed"},
        },
        {
            "type": "session_info",
            "id": "pi-name",
            "parentId": "pi-user",
            "timestamp": "2026-08-18T14:00:02Z",
            "name": name,
        },
    ]


def _write_omp_session(home: Path, cwd: Path, *, title: str = "Named OMP session") -> Path:
    source = claude.parse(Path(__file__).parent / "fixtures" / "claude-2.1.209" / "basic.jsonl")
    data, _ = omp.serialize(
        source,
        session_id=OMP_ID,
        cwd=cwd,
        timestamp="2026-08-25T12:00:00Z",
        name=title,
    )
    path = home / omp.session_relative_path(cwd, OMP_ID, "2026-08-25T12:00:00Z")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _catalog(tmp_path: Path) -> Catalog:
    return Catalog(tmp_path / "private-state" / "catalog.sqlite3")


def _opencode_database(home: Path, database_name: str = "opencode.db") -> sqlite3.Connection:
    home.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(home / database_name)
    connection.executescript(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, parent_id TEXT,
            slug TEXT NOT NULL, directory TEXT NOT NULL, title TEXT NOT NULL,
            version TEXT NOT NULL, time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL, time_archived INTEGER
        );
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, data TEXT);
        """
    )
    return connection


def _insert_opencode_session(
    connection: sqlite3.Connection,
    session_id: str,
    title: str,
    *,
    parent_id: str | None = None,
    archived: bool = False,
    updated: int = 1_787_054_400_000,
) -> None:
    connection.execute(
        "INSERT INTO session VALUES (?, 'global', ?, 'synthetic', ?, ?, '1.17.20', ?, ?, ?)",
        (
            session_id,
            parent_id,
            "/synthetic/opencode-work",
            title,
            1_787_050_800_000,
            updated,
            1_787_054_500_000 if archived else None,
        ),
    )


def _copilot_records(session_id: str, title: str) -> list[dict[str, object]]:
    return [
        {
            "type": "session.start",
            "data": {
                "sessionId": session_id,
                "version": 1,
                "producer": "copilot-agent",
                "copilotVersion": "1.0.70",
                "startTime": "2026-08-20T08:52:08.825Z",
                "context": {"cwd": "/synthetic/copilot-work"},
            },
            "id": "90000000-0000-4000-8000-000000000001",
            "timestamp": "2026-08-20T08:52:08.825Z",
            "parentId": None,
        },
        {
            "type": "session.title_changed",
            "data": {"title": title},
            "id": "90000000-0000-4000-8000-000000000002",
            "timestamp": "2026-08-20T08:52:08.826Z",
            "parentId": "90000000-0000-4000-8000-000000000001",
        },
        {
            "type": "user.message",
            "data": {"content": "forbidden Copilot prompt marker"},
            "id": "90000000-0000-4000-8000-000000000003",
            "timestamp": "2026-08-20T08:52:08.827Z",
            "parentId": "90000000-0000-4000-8000-000000000002",
        },
    ]


def test_refresh_indexes_main_sidechain_corrupt_duplicate_and_missing(tmp_path: Path) -> None:
    first = tmp_path / "claude-one"
    second = tmp_path / "claude-two"
    main = first / "projects" / "-synthetic" / f"{CLAUDE_ID}.jsonl"
    duplicate_one = first / "projects" / "-synthetic" / f"{CLAUDE_DUPLICATE_ID}.jsonl"
    duplicate_two = second / "projects" / "-other" / f"{CLAUDE_DUPLICATE_ID}.jsonl"
    sidechain = (
        first / "projects" / "-synthetic" / CLAUDE_ID / "subagents" / "agent-synthetic.jsonl"
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
        result = catalog.refresh(claude_roots=(first, second), include_auto=False)
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
    active = home / "sessions" / "2026" / "08" / "18" / f"rollout-synthetic-{CODEX_ID}.jsonl"
    archived = home / "archived_sessions" / f"rollout-synthetic-{ARCHIVED_ID}.jsonl"
    paginated = home / "sessions" / "2026" / "08" / "18" / f"rollout-synthetic-{PAGINATED_ID}.jsonl"
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
    connection.execute("INSERT INTO thread_spawn_edges VALUES (?, ?)", (CODEX_ID, ARCHIVED_ID))
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


def test_pi_sessions_are_named_searchable_validatable_and_version_guarded(
    tmp_path: Path,
) -> None:
    home = tmp_path / "pi-agent"
    bucket = home / "sessions" / "--synthetic-pi-work--"
    supported = bucket / f"2026-08-18T14-00-00-000Z_{PI_ID}.jsonl"
    unsupported = bucket / f"2026-08-18T14-00-01-000Z_{CLAUDE_ID}.jsonl"
    _write_jsonl(supported, _pi_records(PI_ID))
    _write_jsonl(
        unsupported,
        _pi_records(CLAUDE_ID, name="Unsupported old Pi session", version=2),
    )

    with _catalog(tmp_path) as catalog:
        result = catalog.refresh(pi_roots=(home,), include_auto=False)
        assert result.statuses == {"candidate": 1, "unsupported": 1}
        named = catalog.list_sessions(query="named pi", agent_format=AgentFormat.PI)
        assert len(named) == 1
        assert named[0].session_id == PI_ID
        assert named[0].filename_session_id == PI_ID
        assert named[0].title_kind == "session_name"
        assert named[0].lifecycle == "project"
        assert (
            catalog._connection.execute(  # noqa: SLF001
                "SELECT parent_session_id FROM sessions WHERE session_id = ?", (PI_ID,)
            ).fetchone()[0]
            == PI_PARENT_ID
        )

        deep = catalog.refresh(include_auto=False, validate=True)
        assert deep.scanned == 2
        assert catalog.list_sessions(query=PI_ID)[0].status == "validated"
        guarded = catalog.list_sessions(statuses=("unsupported",))
        assert len(guarded) == 1
        assert guarded[0].reason == "pi_session_version"


def test_omp_sessions_are_searchable_incremental_and_transferable(tmp_path: Path) -> None:
    home = tmp_path / "omp-agent"
    session = _write_omp_session(home, tmp_path, title="Fix timeline merging")

    with _catalog(tmp_path) as catalog:
        first = catalog.refresh(omp_roots=(home,), include_auto=False)
        assert first.files_seen == 1
        assert first.statuses == {"candidate": 1}
        matches = catalog.list_sessions(query="timeline merging", include_paths=True)
        assert len(matches) == 1
        assert matches[0].format == "omp"
        assert matches[0].session_id == OMP_ID
        assert matches[0].title == "Fix timeline merging"
        assert matches[0].title_kind == "session_title"
        assert matches[0].path == str(session)
        assert catalog.session_source_for_transfer(matches[0].catalog_id).path == session

        second = catalog.refresh(include_auto=False)
        assert second.scanned == 0
        assert second.unchanged == 1

        deep = catalog.refresh(include_auto=False, validate=True)
        assert deep.scanned == 1
        assert catalog.list_sessions(query=OMP_ID)[0].status == "validated"


def test_opencode_inventory_is_complete_virtual_private_and_incremental(
    tmp_path: Path,
) -> None:
    home = tmp_path / "opencode-data"
    connection = _opencode_database(home)
    _insert_opencode_session(connection, OPENCODE_ID, "OpenCode active title")
    _insert_opencode_session(
        connection,
        OPENCODE_CHILD_ID,
        "OpenCode child title",
        parent_id=OPENCODE_ID,
    )
    _insert_opencode_session(
        connection,
        OPENCODE_ARCHIVED_ID,
        "OpenCode archived title",
        archived=True,
    )
    _insert_opencode_session(connection, "../../malformed-id", "Malformed but indexed")
    connection.execute(
        "INSERT INTO message VALUES ('msg_secret', ?, ?)",
        (OPENCODE_ID, "forbidden OpenCode transcript marker"),
    )
    connection.commit()
    connection.close()

    with _catalog(tmp_path) as catalog:
        first = catalog.refresh(opencode_roots=(home,), include_auto=False)
        assert first.files_seen == 4
        assert first.scanned == 4
        assert first.statuses == {"candidate": 3, "corrupt": 1}
        assert catalog.list_sessions(query="forbidden OpenCode") == []
        malformed = catalog.list_sessions(query="malformed but indexed")
        assert len(malformed) == 1
        assert malformed[0].status == "corrupt"
        assert malformed[0].reason == "invalid_opencode_session_id"

        entries = catalog.list_sessions(agent_format=AgentFormat.OPENCODE, limit=10)
        assert {entry.session_id for entry in entries} == {
            OPENCODE_ID,
            OPENCODE_CHILD_ID,
            OPENCODE_ARCHIVED_ID,
            None,
        }
        child = catalog.list_sessions(query="child title")[0]
        assert child.kind == "subagent"
        archived = catalog.list_sessions(query="archived title")[0]
        assert archived.lifecycle == "archived"
        assert archived.records is None
        assert archived.bytes == 0

        source = catalog.session_source_for_transfer(child.catalog_id)
        assert source.format == AgentFormat.OPENCODE
        assert source.session_id == OPENCODE_CHILD_ID
        assert source.root == home.resolve()
        assert source.path is None
        assert source.is_virtual
        with pytest.raises(SessionMigrateError, match="native IDs"):
            catalog.session_path_for_transfer(child.catalog_id)

        second = catalog.refresh(include_auto=False)
        assert second.scanned == 0
        assert second.unchanged == 4

        # The fingerprint covers every indexed field, even if a malformed or
        # third-party writer forgets to advance time_updated.
        connection = sqlite3.connect(home / "opencode.db")
        connection.execute(
            "UPDATE session SET title = ? WHERE id = ?",
            ("Renamed without timestamp", OPENCODE_ID),
        )
        connection.commit()
        connection.close()
        changed = catalog.refresh(include_auto=False)
        assert changed.scanned == 1
        assert changed.unchanged == 3
        assert len(catalog.list_sessions(query="renamed without")) == 1

        connection = sqlite3.connect(home / "opencode.db")
        connection.execute("DELETE FROM session WHERE id = ?", (OPENCODE_ARCHIVED_ID,))
        connection.commit()
        connection.close()
        removed = catalog.refresh(include_auto=False)
        assert removed.missing == 1
        missing = catalog.list_sessions(
            query=OPENCODE_ARCHIVED_ID,
            include_missing=True,
            statuses=("missing",),
        )
        assert len(missing) == 1

        raw_catalog = (tmp_path / "private-state" / "catalog.sqlite3").read_bytes()
        assert b"forbidden OpenCode transcript marker" not in raw_catalog


def test_opencode_inventory_failures_retain_rows_and_reject_database_symlink(
    tmp_path: Path,
) -> None:
    home = tmp_path / "opencode-data"
    connection = _opencode_database(home)
    _insert_opencode_session(connection, OPENCODE_ID, "Retained OpenCode title")
    connection.commit()
    connection.close()
    with _catalog(tmp_path) as catalog:
        catalog.refresh(opencode_roots=(home,), include_auto=False)
        database = home / "opencode.db"
        moved = tmp_path / "vendor-database"
        database.rename(moved)
        database.symlink_to(moved)
        failed = catalog.refresh(include_auto=False)
        assert failed.root_errors == 1
        assert catalog.roots()[0].last_error == "opencode_database_symlink"
        assert len(catalog.list_sessions(query=OPENCODE_ID)) == 1


def test_grok_kilo_and_openhands_catalog_roots_are_complete_searchable_and_transferable(
    tmp_path: Path,
) -> None:
    source = claude.parse(Path(__file__).parent / "fixtures/claude-2.1.209/basic.jsonl")
    grok_home = tmp_path / "grok-home"
    grok_bytes, _ = grok.serialize(
        source,
        session_id=GROK_ID,
        cwd=tmp_path,
        timestamp="2026-08-25T12:00:00Z",
        title="Repair timeline merging",
    )
    grok_directory = grok_home / grok.session_relative_path(tmp_path, GROK_ID)
    grok_directory.mkdir(parents=True)
    summary, updates = grok.native_files(grok_bytes, GROK_ID)
    (grok_directory / "summary.json").write_bytes(summary)
    (grok_directory / "updates.jsonl").write_bytes(updates)

    openhands_home = tmp_path / "openhands-conversations"
    openhands_bytes, _ = openhands.serialize(
        source,
        session_id=OPENHANDS_ID,
        cwd=tmp_path,
        timestamp="2026-08-25T12:00:00Z",
    )
    openhands_events = openhands_home / openhands.session_relative_path(OPENHANDS_ID)
    openhands_events.mkdir(parents=True)
    for name, data in openhands.native_files(openhands_bytes, OPENHANDS_ID):
        (openhands_events / name).write_bytes(data)
    openhands_state = openhands_events.parent / "base_state.json"
    openhands_state.write_text(
        json.dumps(
            {
                "id": OPENHANDS_ID,
                "agent": {"llm": {"model": "openai/catalog-fixture"}},
                "workspace": {"working_dir": str(tmp_path), "kind": "LocalWorkspace"},
            }
        )
    )

    kilo_home = tmp_path / "kilo-data"
    connection = _opencode_database(kilo_home, "kilo.db")
    _insert_opencode_session(connection, KILO_ID, "Implement catalog keyword search")
    connection.execute("UPDATE session SET version = '7.5.0' WHERE id = ?", (KILO_ID,))
    connection.commit()
    connection.close()

    with _catalog(tmp_path) as catalog:
        first = catalog.refresh(
            grok_roots=(grok_home,),
            kilo_roots=(kilo_home,),
            openhands_roots=(openhands_home,),
            include_auto=False,
        )
        assert first.files_seen == 3
        assert first.root_errors == 0
        assert len(catalog.list_sessions(query="timeline merging")) == 1
        assert len(catalog.list_sessions(query="catalog keyword")) == 1
        openhands_matches = catalog.list_sessions(query="synthetic migrator nonce")
        assert len(openhands_matches) == 1
        assert openhands_matches[0].format == "openhands"
        entries = catalog.list_sessions(limit=10)
        assert {entry.format for entry in entries} == {"grok", "kilo", "openhands"}

        by_format = {entry.format: entry for entry in entries}
        grok_source = catalog.session_source_for_transfer(by_format["grok"].catalog_id)
        kilo_source = catalog.session_source_for_transfer(by_format["kilo"].catalog_id)
        openhands_source = catalog.session_source_for_transfer(by_format["openhands"].catalog_id)
        assert grok_source.path == grok_directory
        assert kilo_source.is_virtual and kilo_source.session_id == KILO_ID
        assert openhands_source.path == openhands_events

        second = catalog.refresh(include_auto=False)
        assert second.unchanged == 3
        assert second.scanned == 0

        state = json.loads(openhands_state.read_text())
        changed_cwd = tmp_path / "changed-workspace"
        state["workspace"]["working_dir"] = str(changed_cwd)
        openhands_state.write_text(json.dumps(state))
        third = catalog.refresh(include_auto=False)
        assert third.scanned == 1
        assert third.unchanged == 2
        assert catalog.list_sessions(query="synthetic migrator nonce", include_paths=True)[
            0
        ].cwd == str(changed_cwd)


def test_copilot_inventory_includes_valid_corrupt_missing_and_symlinked_logs(
    tmp_path: Path,
) -> None:
    home = tmp_path / "copilot"
    valid = home / "session-state" / COPILOT_ID / "events.jsonl"
    _write_jsonl(valid, _copilot_records(COPILOT_ID, "Copilot event title"))
    (valid.parent / "workspace.yaml").write_text('name: "Copilot picker name"\n')

    corrupt_id = "99999999-9999-4999-8999-999999999999"
    corrupt = home / "session-state" / corrupt_id / "events.jsonl"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_text("{not-json}\n")
    missing_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    (home / "session-state" / missing_id).mkdir(parents=True)
    symlink_id = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
    symlinked = home / "session-state" / symlink_id / "events.jsonl"
    symlinked.parent.mkdir(parents=True)
    outside = tmp_path / "outside-events.jsonl"
    _write_jsonl(outside, _copilot_records(symlink_id, "Must not follow"))
    symlinked.symlink_to(outside)

    with _catalog(tmp_path) as catalog:
        first = catalog.refresh(copilot_roots=(home,), include_auto=False)
        assert first.files_seen == 4
        assert first.statuses == {
            "candidate": 1,
            "corrupt": 1,
            "missing": 1,
            "unreadable": 1,
        }
        assert catalog.list_sessions(query="forbidden Copilot") == []
        named = catalog.list_sessions(query="Copilot event title")
        assert len(named) == 1
        assert named[0].session_id == COPILOT_ID
        assert named[0].lifecycle == "active"
        source = catalog.session_source_for_transfer(named[0].catalog_id)
        assert source.path == valid.resolve()
        assert not source.is_virtual

        assert catalog.list_sessions(query=missing_id) == []
        missing = catalog.list_sessions(
            query=missing_id,
            include_missing=True,
            statuses=("missing",),
        )
        assert len(missing) == 1
        assert missing[0].reason == "events_file_missing"
        unreadable = catalog.list_sessions(query=symlink_id, statuses=("unreadable",))
        assert len(unreadable) == 1
        assert unreadable[0].reason == "symlink_not_allowed"

        second = catalog.refresh(include_auto=False)
        assert second.unchanged == 3  # valid, corrupt, and blocked-symlink identities

        deep = catalog.refresh(include_auto=False, validate=True)
        validated = catalog.list_sessions(query=COPILOT_ID)[0]
        assert deep.root_errors == 0
        assert validated.status == "validated"

        # Sidecar picker names refresh without re-reading an unchanged event log.
        (valid.parent / "workspace.yaml").write_text('name: "Renamed Copilot picker"\n')
        catalog.refresh(include_auto=False)
        assert len(catalog.list_sessions(query="renamed copilot picker")) == 1
        assert (
            b"forbidden Copilot prompt marker"
            not in (tmp_path / "private-state" / "catalog.sqlite3").read_bytes()
        )


def test_opencode_and_copilot_auto_roots_follow_effective_environment(
    tmp_path: Path,
) -> None:
    user_home = tmp_path / "home"
    xdg_data = tmp_path / "xdg-data"
    (xdg_data / "opencode").mkdir(parents=True)
    default_opencode = user_home / ".local" / "share" / "opencode"
    default_opencode.mkdir(parents=True)
    custom_copilot = tmp_path / "custom-copilot"
    (custom_copilot / "session-state").mkdir(parents=True)
    (user_home / ".copilot" / "session-state").mkdir(parents=True)

    roots = auto_roots(
        cwd=tmp_path,
        environ={"XDG_DATA_HOME": str(xdg_data), "COPILOT_HOME": str(custom_copilot)},
        home=user_home,
    )
    root_set = {(agent_format.value, path, source) for agent_format, path, source in roots}
    assert ("opencode", xdg_data / "opencode", "environment") in root_set
    assert all(path != default_opencode for _, path, _ in roots)
    assert ("copilot", user_home / ".copilot", "default") in root_set
    assert ("copilot", custom_copilot, "environment") in root_set


@pytest.mark.skipif(
    not os.environ.get("SESSION_MIGRATE_REAL_OPENCODE_ROOT"),
    reason="set SESSION_MIGRATE_REAL_OPENCODE_ROOT for metadata-only real-store smoke",
)
def test_real_opencode_inventory_aggregate_is_content_free(tmp_path: Path) -> None:
    root = Path(os.environ["SESSION_MIGRATE_REAL_OPENCODE_ROOT"])
    with _catalog(tmp_path) as catalog:
        result = catalog.refresh(opencode_roots=(root,), include_auto=False)
        assert result.root_errors == 0
        assert result.files_seen > 0
        assert (
            result.files_seen
            == len(catalog.list_sessions(agent_format=AgentFormat.OPENCODE, limit=10_000))
            or result.files_seen > 10_000
        )
        assert all(
            row[0] == 0 and row[1] is None
            for row in catalog._connection.execute(  # noqa: SLF001
                "SELECT bytes, records FROM sessions WHERE format = 'opencode'"
            )
        )
        incremental = catalog.refresh(include_auto=False)
        assert incremental.files_seen == result.files_seen
        assert incremental.scanned == 0
        assert incremental.unchanged == result.files_seen


def test_auto_roots_are_bounded_to_defaults_environment_and_ancestors(
    tmp_path: Path,
) -> None:
    user_home = tmp_path / "home"
    default_claude = user_home / ".claude" / "projects"
    default_claude.mkdir(parents=True)
    (user_home / ".pi" / "agent" / "sessions").mkdir(parents=True)
    (user_home / ".omp" / "agent" / "sessions").mkdir(parents=True)
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
    assert ("pi", user_home / ".pi" / "agent", "default") in root_set
    assert ("omp", user_home / ".omp" / "agent", "default") in root_set
    assert all(path != tmp_path / "elsewhere" / ".claude" for _, path, _ in roots)


def test_shared_pi_agent_environment_root_is_classified_from_native_header(
    tmp_path: Path,
) -> None:
    user_home = tmp_path / "home"
    omp_home = tmp_path / "custom-agent"
    _write_omp_session(omp_home, tmp_path)

    roots = auto_roots(
        cwd=tmp_path,
        environ={"PI_CODING_AGENT_DIR": str(omp_home)},
        home=user_home,
    )
    matching = [
        (agent_format.value, path, source)
        for agent_format, path, source in roots
        if path == omp_home
    ]
    assert matching == [("omp", omp_home, "environment")]


def test_catalog_files_are_private_and_default_path_is_configurable(tmp_path: Path) -> None:
    configured = tmp_path / "configured" / "sessions.sqlite3"
    assert default_catalog_path(environ={"SESSION_MIGRATE_CATALOG": str(configured)}) == configured
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
        assert (
            catalog._connection.execute(  # noqa: SLF001
                "SELECT value FROM catalog_meta WHERE key = 'schema_version'"
            ).fetchone()[0]
            == "4"
        )
        # Future source adapters do not require another destructive table
        # rebuild; the public API still gates roots through AgentFormat.
        roots_sql = catalog._connection.execute(  # noqa: SLF001
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'roots'"
        ).fetchone()[0]
        assert "CHECK" not in roots_sql.upper()
        pi_root = catalog.add_root(AgentFormat.PI, tmp_path / "pi-agent")
        assert pi_root.format == "pi"


def test_corrupt_catalog_fails_with_recoverable_content_safe_error(tmp_path: Path) -> None:
    database = tmp_path / "corrupt-catalog.sqlite3"
    database.write_bytes(b"this is not a SQLite database")

    with pytest.raises(SessionMigrateError, match="move the disposable database aside"):
        Catalog(database)


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
    codex_path = codex_home / "archived_sessions" / f"rollout-synthetic-{CODEX_ID}.jsonl"
    _write_jsonl(codex_path, _codex_records(CODEX_ID, title="Later archive"))

    with _catalog(tmp_path) as catalog:
        catalog.refresh(claude_roots=(claude_home,), codex_roots=(codex_home,), include_auto=False)
        later = catalog.list_sessions(since="2026-08-18T12:30:00Z")
        assert len(later) == 1
        assert later[0].lifecycle == "archived"
        earlier = catalog.list_sessions(until="2026-08-18T12:30:00+00:00", lifecycles=("project",))
        assert len(earlier) == 1
        assert earlier[0].format == "claude"
        with pytest.raises(SessionMigrateError, match="timezone-aware RFC-3339"):
            catalog.list_sessions(since="2026-08-18")


def test_title_search_casefolds_unicode_and_bounds_stored_native_metadata(
    tmp_path: Path,
) -> None:
    claude_home = tmp_path / "claude"
    claude_path = claude_home / "projects" / "-synthetic" / f"{CLAUDE_ID}.jsonl"
    _write_jsonl(claude_path, _claude_records(CLAUDE_ID, "Straße investigation"))
    codex_home = tmp_path / "codex"
    codex_path = (
        codex_home / "sessions" / "2026" / "08" / "18" / f"rollout-synthetic-{CODEX_ID}.jsonl"
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
        catalog.refresh(claude_roots=(claude_home,), codex_roots=(codex_home,), include_auto=False)
        assert len(catalog.list_sessions(query="STRASSE")) == 1
        assert len(catalog.list_sessions(query="investigation STRASSE")) == 1
        assert catalog.list_sessions(query="STRASSE unrelated") == []
        bounded = catalog.list_sessions(query="x" * 512)
        assert len(bounded) == 1
        assert bounded[0].title == "x" * 512
        assert catalog.list_sessions(query="x" * 513) == []


def test_sidechain_native_agent_identity_is_searchable_without_paths(tmp_path: Path) -> None:
    home = tmp_path / "claude"
    sidechain = (
        home / "projects" / "-synthetic" / CLAUDE_ID / "subagents" / "agent-native-key.jsonl"
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


def test_catalog_indexes_antigravity_database_and_native_picker_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = claude.parse(Path(__file__).parent / "fixtures" / "claude-2.1.209" / "basic.jsonl")
    data, _ = antigravity.serialize(
        source,
        session_id=ANTIGRAVITY_ID,
        cwd=tmp_path,
        timestamp="2026-08-20T12:00:00Z",
    )
    home = tmp_path / "antigravity-cli"
    monkeypatch.setattr(antigravity, "verify_pinned_cli", lambda *args, **kwargs: Path("agy"))
    installed = antigravity.install_database(
        data,
        session_id=ANTIGRAVITY_ID,
        cwd=tmp_path,
        timestamp="2026-08-20T12:00:00Z",
        title="Searchable Antigravity title",
        target_home=home,
    )

    with _catalog(tmp_path) as catalog:
        first = catalog.refresh(antigravity_roots=(home,), include_auto=False)
        assert first.files_seen == 1
        assert first.statuses == {"candidate": 1}
        matches = catalog.list_sessions(query="antigravity title", include_paths=True)
        assert len(matches) == 1
        assert matches[0].format == "antigravity"
        assert matches[0].session_id == ANTIGRAVITY_ID
        assert matches[0].title == "Searchable Antigravity title"
        assert matches[0].path == str(installed.conversation_path)
        source_ref = catalog.session_source_for_transfer(matches[0].catalog_id)
        assert source_ref.path == installed.conversation_path

        second = catalog.refresh(include_auto=False)
        assert second.scanned == 0
        assert second.unchanged == 1


def test_catalog_indexes_cursor_store_title_and_transfer_source(tmp_path: Path) -> None:
    source = claude.parse(Path(__file__).parent / "fixtures" / "claude-2.1.209" / "basic.jsonl")
    data, _ = cursor.serialize(
        source,
        session_id=CURSOR_ID,
        cwd=tmp_path,
        timestamp="2026-08-20T12:00:00Z",
        title="Searchable Cursor title",
    )
    home = tmp_path / "cursor"
    store = home / cursor.session_relative_path(CURSOR_ID, tmp_path)
    store.parent.mkdir(parents=True)
    store.write_bytes(data)

    with _catalog(tmp_path) as catalog:
        first = catalog.refresh(cursor_roots=(home,), include_auto=False)
        assert first.files_seen == 1
        assert first.statuses == {"candidate": 1}
        matches = catalog.list_sessions(query="cursor title", include_paths=True)
        assert len(matches) == 1
        assert matches[0].format == "cursor"
        assert matches[0].session_id == CURSOR_ID
        assert matches[0].title == "Searchable Cursor title"
        assert matches[0].path == str(store)
        source_ref = catalog.session_source_for_transfer(matches[0].catalog_id)
        assert source_ref.path == store

        second = catalog.refresh(include_auto=False)
        assert second.scanned == 0
        assert second.unchanged == 1


def test_catalog_retains_declared_cursor_chat_with_missing_store(tmp_path: Path) -> None:
    home = tmp_path / "cursor"
    chat = home / "chats" / ("a" * 32) / CURSOR_ID
    chat.mkdir(parents=True)

    with _catalog(tmp_path) as catalog:
        result = catalog.refresh(cursor_roots=(home,), include_auto=False)
        assert result.files_seen == 1
        entries = catalog.list_sessions(include_missing=True)
        assert len(entries) == 1
        assert entries[0].format == "cursor"
        assert entries[0].session_id is None
        assert entries[0].filename_session_id == CURSOR_ID
        assert entries[0].status == "missing"
        assert entries[0].reason == "store_database_missing"


def test_catalog_indexes_vibe_title_and_tracks_both_native_files(tmp_path: Path) -> None:
    source = claude.parse(Path(__file__).parent / "fixtures" / "claude-2.1.209" / "basic.jsonl")
    data, _ = vibe.serialize(
        source,
        session_id=VIBE_ID,
        cwd=tmp_path,
        timestamp="2026-08-20T12:00:00Z",
        title="Searchable Vibe title",
    )
    meta_bytes, messages_bytes = vibe.native_files(data, VIBE_ID)
    home = tmp_path / "vibe"
    messages = home / vibe.session_relative_path(VIBE_ID, "2026-08-20T12:00:00Z")
    messages.parent.mkdir(parents=True)
    messages.write_bytes(messages_bytes)
    meta = messages.parent / vibe.META_FILENAME
    meta.write_bytes(meta_bytes)

    with _catalog(tmp_path) as catalog:
        first = catalog.refresh(vibe_roots=(home,), include_auto=False)
        assert first.files_seen == 1
        assert first.statuses == {"candidate": 1}
        matches = catalog.list_sessions(query="vibe title", include_paths=True)
        assert len(matches) == 1
        assert matches[0].format == "vibe"
        assert matches[0].session_id == VIBE_ID
        assert matches[0].title == "Searchable Vibe title"
        assert matches[0].path == str(messages)
        assert catalog.session_source_for_transfer(matches[0].catalog_id).path == messages

        second = catalog.refresh(include_auto=False)
        assert second.scanned == 0
        assert second.unchanged == 1

        metadata = json.loads(meta.read_text())
        metadata["title"] = "Changed Vibe title"
        meta.write_text(json.dumps(metadata))
        changed = catalog.refresh(include_auto=False)
        assert changed.scanned == 1
        assert catalog.list_sessions(query="changed vibe")[0].title == "Changed Vibe title"


def test_discover_roots_is_bounded_and_requires_native_hidden_store_markers(
    tmp_path: Path,
) -> None:
    boundary = tmp_path / "workspace"
    claude_home = boundary / "one" / ".claude"
    codex_home = boundary / "two" / ".codex"
    pi_home = boundary / "three" / ".pi" / "agent"
    omp_home = boundary / "four" / ".omp" / "agent"
    copilot_home = boundary / "five" / ".copilot"
    antigravity_home = boundary / "six" / ".gemini" / "antigravity-cli"
    cursor_home = boundary / "seven" / ".cursor"
    vibe_home = boundary / "eight" / ".vibe"
    (claude_home / "projects").mkdir(parents=True)
    (codex_home / "archived_sessions").mkdir(parents=True)
    (pi_home / "sessions").mkdir(parents=True)
    (omp_home / "sessions").mkdir(parents=True)
    (copilot_home / "session-state").mkdir(parents=True)
    (antigravity_home / "conversations").mkdir(parents=True)
    (cursor_home / "chats").mkdir(parents=True)
    (vibe_home / "logs/session").mkdir(parents=True)
    (boundary / "ordinary" / "projects").mkdir(parents=True)
    outside = tmp_path / "outside" / ".claude"
    (outside / "projects").mkdir(parents=True)
    (boundary / "linked-outside").symlink_to(outside.parent, target_is_directory=True)

    found = discover_roots((boundary,))
    assert {(agent_format.value, path, source) for agent_format, path, source in found} == {
        ("claude", claude_home, "discovered"),
        ("codex", codex_home, "discovered"),
        ("pi", pi_home, "discovered"),
        ("omp", omp_home, "discovered"),
        ("copilot", copilot_home, "discovered"),
        ("antigravity", antigravity_home, "discovered"),
        ("cursor", cursor_home, "discovered"),
        ("vibe", vibe_home, "discovered"),
    }

    with _catalog(tmp_path) as catalog:
        result = catalog.refresh(discover_under=(boundary,), include_auto=False)
        assert result.roots == 8
        assert {root.source for root in catalog.roots()} == {"discovered"}


def test_discovery_fails_if_a_bounded_walk_is_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boundary = tmp_path / "workspace"
    boundary.mkdir()

    def incomplete_walk(*_args: object, **kwargs: object) -> list[object]:
        kwargs["onerror"](PermissionError("synthetic permission failure"))
        return []

    monkeypatch.setattr(catalog_module.os, "walk", incomplete_walk)

    with pytest.raises(SessionMigrateError, match="could not completely scan"):
        discover_roots((boundary,))


def test_incomplete_root_walk_retains_previous_catalog_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "claude"
    path = home / "projects" / "-synthetic" / f"{CLAUDE_ID}.jsonl"
    _write_jsonl(path, _claude_records(CLAUDE_ID, "Retained title"))

    with _catalog(tmp_path) as catalog:
        assert catalog.refresh(claude_roots=(home,), include_auto=False).root_errors == 0

        def incomplete_walk(*_args: object, **kwargs: object) -> list[object]:
            kwargs["onerror"](PermissionError("synthetic permission failure"))
            return []

        monkeypatch.setattr(catalog_module.os, "walk", incomplete_walk)
        result = catalog.refresh(include_auto=False)
        assert result.root_errors == 1
        retained = catalog.list_sessions(query=CLAUDE_ID)
        assert len(retained) == 1
        assert retained[0].status == "candidate"


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
