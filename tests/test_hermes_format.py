import json
import os
import sqlite3
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from session_migrate.errors import JsonlError, SessionMigrateError
from session_migrate.formats import hermes
from session_migrate.model import AgentFormat, Event, EventKind, Provenance, Role, Session

NATIVE_ID = "20260830_123456_111111"
SECOND_ID = "20260830_133456_222222"


def _portable_session(tmp_path: Path) -> Session:
    return Session(
        source_format=AgentFormat.CLAUDE,
        source_path=tmp_path / "source.jsonl",
        source_sha256="0" * 64,
        session_id="11111111-1111-4111-8111-111111111111",
        cwd=tmp_path,
        started_at="2026-08-30T12:34:56Z",
        cli_version="2.1.209",
        model="fixture-model",
        title="Hermes portable fixture",
        events=(
            Event(
                EventKind.MESSAGE,
                Provenance(0, "user"),
                role=Role.USER,
                timestamp="2026-08-30T12:34:56Z",
                text="HERMES_PORTABLE_USER_ALPHA",
            ),
            Event(
                EventKind.CONTEXT,
                Provenance(0, "user", block_index=1),
                role=Role.USER,
                timestamp="2026-08-30T12:34:56Z",
                payload={
                    "block_type": "image",
                    "image_url": "data:image/png;base64,aGVybWVz",
                },
            ),
            Event(
                EventKind.MESSAGE,
                Provenance(1, "assistant"),
                role=Role.ASSISTANT,
                timestamp="2026-08-30T12:34:57Z",
                text="HERMES_PORTABLE_ASSISTANT_BETA",
            ),
            Event(
                EventKind.TOOL_CALL,
                Provenance(1, "assistant", block_index=1),
                role=Role.ASSISTANT,
                timestamp="2026-08-30T12:34:57Z",
                tool_name="terminal",
                tool_call_id="call_hermes_fixture",
                payload={"input": {"command": "pwd"}},
            ),
            Event(
                EventKind.TOOL_RESULT,
                Provenance(2, "tool"),
                role=Role.TOOL,
                timestamp="2026-08-30T12:34:58Z",
                text="HERMES_PORTABLE_TOOL_GAMMA",
                tool_name="terminal",
                tool_call_id="call_hermes_fixture",
                payload={
                    "is_error": False,
                    "content_blocks": [{"type": "text", "text": "HERMES_PORTABLE_TOOL_GAMMA"}],
                },
            ),
            Event(
                EventKind.THINKING,
                Provenance(3, "assistant"),
                role=Role.ASSISTANT,
                timestamp="2026-08-30T12:34:59Z",
                text="never serialize this private trace",
                payload={"encrypted_content": "never serialize this either"},
            ),
            Event(
                EventKind.COMPACTION,
                Provenance(4, "compaction"),
                role=Role.SYSTEM,
                timestamp="2026-08-30T12:35:00Z",
                text="HERMES_PORTABLE_SUMMARY_DELTA",
            ),
            Event(
                EventKind.MESSAGE,
                Provenance(5, "user"),
                role=Role.USER,
                timestamp="2026-08-30T12:35:01Z",
                text="HERMES_PORTABLE_FOLLOWUP_EPSILON",
            ),
        ),
        raw_record_count=8,
        model_provider="loopback",
    )


def _create_store(path: Path, *, schema_version: int = 26) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT NOT NULL, user_id TEXT,
            model TEXT, model_config TEXT, parent_session_id TEXT,
            started_at REAL NOT NULL, ended_at REAL, end_reason TEXT,
            message_count INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0,
            cwd TEXT, billing_provider TEXT, title TEXT, title_source TEXT,
            last_activity_at REAL, archived INTEGER DEFAULT 0,
            hidden INTEGER DEFAULT 0
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            role TEXT NOT NULL, content TEXT, tool_call_id TEXT, tool_calls TEXT,
            tool_name TEXT, timestamp REAL NOT NULL, finish_reason TEXT,
            reasoning TEXT, reasoning_content TEXT, reasoning_details TEXT,
            observed INTEGER DEFAULT 0, _compressed_summary INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1, compacted INTEGER DEFAULT 0,
            api_content TEXT, display_kind TEXT, display_metadata TEXT
        );
        """
    )
    db.execute("INSERT INTO schema_version VALUES (?)", (schema_version,))
    return db


def _insert_session(
    db: sqlite3.Connection,
    session_id: str,
    *,
    title: str,
    started: float = 1_788_093_296.0,
    cwd: str = "/synthetic/hermes-work",
) -> None:
    db.execute(
        """INSERT INTO sessions(
               id,source,model,model_config,started_at,ended_at,end_reason,
               message_count,tool_call_count,cwd,billing_provider,title,title_source,
               last_activity_at,archived,hidden
           ) VALUES (?, 'cli', 'loopback/fixture-model', '{}', ?, ?, 'cli_close',
                     8, 1, ?, 'loopback', ?, 'user', ?, 0, 0)""",
        (session_id, started, started + 20, cwd, title, started + 19),
    )


def _insert_message(
    db: sqlite3.Connection,
    session_id: str,
    role: str,
    content: object,
    timestamp: float,
    **values: object,
) -> None:
    stored = json.dumps(content) if isinstance(content, list) else content
    db.execute(
        """INSERT INTO messages(
               session_id,role,content,tool_call_id,tool_calls,tool_name,timestamp,
               finish_reason,reasoning,reasoning_content,reasoning_details,observed,
               _compressed_summary,active,compacted,api_content,display_kind,display_metadata
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            session_id,
            role,
            stored,
            values.get("tool_call_id"),
            json.dumps(values["tool_calls"]) if values.get("tool_calls") else None,
            values.get("tool_name"),
            timestamp,
            values.get("finish_reason"),
            values.get("reasoning"),
            values.get("reasoning_content"),
            values.get("reasoning_details"),
            0,
            int(bool(values.get("compressed_summary"))),
            int(values.get("active", True)),
            int(bool(values.get("compacted"))),
            None,
            None,
            None,
        ),
    )


def _native_store(tmp_path: Path, *, include_second: bool = True) -> Path:
    path = tmp_path / "state.db"
    db = _create_store(path)
    _insert_session(db, NATIVE_ID, title="Hermes native fixture")
    _insert_message(
        db,
        NATIVE_ID,
        "user",
        "HERMES_ARCHIVED_USER",
        1_788_093_296.0,
        active=False,
        compacted=True,
    )
    _insert_message(
        db,
        NATIVE_ID,
        "assistant",
        "HERMES_ARCHIVED_ASSISTANT",
        1_788_093_297.0,
        active=False,
        compacted=True,
    )
    _insert_message(
        db,
        NATIVE_ID,
        "user",
        "[CONTEXT SUMMARY]:\nHERMES_NATIVE_SUMMARY",
        1_788_093_298.0,
        compressed_summary=True,
    )
    _insert_message(
        db,
        NATIVE_ID,
        "user",
        [
            {"type": "text", "text": "HERMES_NATIVE_USER"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,aGVybWVz"},
            },
        ],
        1_788_093_299.0,
    )
    tool_calls = [
        {
            "id": "call_native_1",
            "call_id": "call_native_1",
            "response_item_id": "fc_native_1",
            "type": "function",
            "function": {"name": "terminal", "arguments": '{"command":"pwd"}'},
        }
    ]
    _insert_message(
        db,
        NATIVE_ID,
        "assistant",
        "HERMES_NATIVE_ASSISTANT",
        1_788_093_300.0,
        tool_calls=tool_calls,
        finish_reason="tool_calls",
    )
    _insert_message(
        db,
        NATIVE_ID,
        "tool",
        '{"output":"HERMES_NATIVE_TOOL_RESULT","exit_code":0,"error":null}',
        1_788_093_301.0,
        tool_call_id="call_native_1",
        tool_name="terminal",
    )
    _insert_message(
        db,
        NATIVE_ID,
        "assistant",
        "HERMES_NATIVE_FINAL",
        1_788_093_302.0,
        reasoning="private native trace",
    )
    _insert_message(
        db,
        NATIVE_ID,
        "user",
        "HERMES_REWOUND_USER",
        1_788_093_303.0,
        active=False,
    )
    if include_second:
        _insert_session(
            db,
            SECOND_ID,
            title="Second Hermes fixture",
            started=1_788_096_896.0,
            cwd="/synthetic/other-work",
        )
        _insert_message(db, SECOND_ID, "user", "HERMES_SECOND_PRIVATE_BODY", 1_788_096_896.0)
    db.commit()
    db.close()
    return path


def test_native_session_id_validation_and_portable_mapping() -> None:
    assert hermes.normalized_session_id("20260830_123456_AABBCC") == ("20260830_123456_aabbcc")
    assert (
        hermes.native_session_id("11111111-1111-4111-8111-111111111111", "2026-08-30T12:34:56Z")
        == NATIVE_ID
    )
    for invalid in (
        "11111111-1111-4111-8111-111111111111",
        "20261340_999999_abcdef",
        "20260830_123456_zzzzzz",
        "../20260830_123456_abcdef",
    ):
        with pytest.raises(SessionMigrateError, match="Hermes session ID"):
            hermes.normalized_session_id(invalid)


def test_hermes_root_and_state_database_resolution(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit-hermes-root"
    configured = tmp_path / "configured-hermes-root"
    user_home = tmp_path / "user-home"

    assert hermes.state_database_path(explicit, environ={}) == explicit / "state.db"
    assert hermes.state_database_path(environ={"HERMES_HOME": str(configured)}) == (
        configured / "state.db"
    )
    assert hermes.hermes_home(user_home, environ={}) == user_home / ".hermes"


def test_hermes_bundle_round_trip_tools_media_compaction_and_private_omissions(
    tmp_path: Path,
) -> None:
    data, dropped = hermes.serialize(
        _portable_session(tmp_path),
        session_id=NATIVE_ID,
        cwd=tmp_path,
        timestamp="2026-08-30T12:34:56Z",
    )
    parsed = hermes.validate_native_bytes(data, NATIVE_ID)

    assert parsed.session_id == NATIVE_ID
    assert parsed.title == "Hermes portable fixture"
    assert parsed.cwd == tmp_path
    assert len(parsed.messages) == 7
    assert parsed.messages[3]["tool_calls"][0]["id"] == "call_hermes_fixture"
    assert parsed.messages[4]["tool_call_id"] == "call_hermes_fixture"
    assert parsed.messages[5]["_compressed_summary"] is True
    assert "never serialize this" not in data.decode()
    assert dropped == {
        "thinking:private": 1,
        "thinking:provider_payload": 1,
    }
    assert hermes.native_record_count(data) == 8


def test_hermes_bundle_round_trip_preserves_failed_tool_result(tmp_path: Path) -> None:
    source = _portable_session(tmp_path)
    failed = replace(
        source,
        events=tuple(
            replace(event, payload={**event.payload, "is_error": True})
            if event.kind == EventKind.TOOL_RESULT
            else event
            for event in source.events
        ),
    )
    data, dropped = hermes.serialize(failed, session_id=NATIVE_ID, cwd=tmp_path)
    bundle = hermes.validate_native_bytes(data, NATIVE_ID)
    tool = next(message for message in bundle.messages if message["role"] == "tool")
    envelope = json.loads(tool["content"])

    assert envelope == {
        "output": "HERMES_PORTABLE_TOOL_GAMMA",
        "error": "HERMES_PORTABLE_TOOL_GAMMA",
        "exit_code": 1,
    }
    assert "tool_result:is_error" not in dropped

    database = tmp_path / "round-trip" / "state.db"
    db = _create_store(database)
    _insert_session(db, NATIVE_ID, title="Hermes failed tool fixture", cwd=str(tmp_path))
    for message in bundle.messages:
        _insert_message(
            db,
            NATIVE_ID,
            message["role"],
            message["content"],
            message["timestamp"],
            tool_call_id=message.get("tool_call_id"),
            tool_calls=message.get("tool_calls"),
            tool_name=message.get("tool_name"),
            finish_reason=message.get("finish_reason"),
            compressed_summary=message.get("_compressed_summary"),
        )
    db.commit()
    db.close()

    reparsed = hermes.parse_session(database, NATIVE_ID)
    result = next(event for event in reparsed.events if event.kind == EventKind.TOOL_RESULT)
    assert result.text == "HERMES_PORTABLE_TOOL_GAMMA"
    assert result.payload["is_error"] is True


def test_hermes_bundle_rejects_duplicate_keys_or_invalid_tool_linkage(tmp_path: Path) -> None:
    data, _ = hermes.serialize(_portable_session(tmp_path), session_id=NATIVE_ID, cwd=tmp_path)
    duplicate = data.replace(b'"schema":', b'"schema":"duplicate","schema":', 1)
    with pytest.raises(SessionMigrateError, match="strict UTF-8 JSON"):
        hermes.validate_native_bytes(duplicate, NATIVE_ID)

    value = json.loads(data)
    tool = next(message for message in value["session"]["messages"] if message["role"] == "tool")
    tool["tool_call_id"] = "orphan"
    malformed = (json.dumps(value, separators=(",", ":")) + "\n").encode()
    with pytest.raises(SessionMigrateError, match="tool result linkage"):
        hermes.validate_native_bytes(malformed, NATIVE_ID)


def test_hermes_source_projects_active_context_and_accounts_for_archived_rows(
    tmp_path: Path,
) -> None:
    path = _native_store(tmp_path)
    session = hermes.parse_session(path, NATIVE_ID)

    assert session.source_format == AgentFormat.HERMES
    assert session.session_id == NATIVE_ID
    assert session.title == "Hermes native fixture"
    assert session.cwd == Path("/synthetic/hermes-work")
    assert session.model == "loopback/fixture-model"
    assert session.model_provider == "loopback"
    assert session.raw_record_count == 8
    assert session.event_counts() == {
        "compaction": 1,
        "context": 1,
        "message": 3,
        "opaque": 4,
        "tool_call": 1,
        "tool_result": 1,
    }
    summary = next(event for event in session.events if event.kind == EventKind.COMPACTION)
    assert summary.text == "HERMES_NATIVE_SUMMARY"
    call = next(event for event in session.events if event.kind == EventKind.TOOL_CALL)
    result = next(event for event in session.events if event.kind == EventKind.TOOL_RESULT)
    assert call.tool_call_id == result.tool_call_id == "call_native_1"
    assert call.payload["input"] == {"command": "pwd"}
    assert result.text == "HERMES_NATIVE_TOOL_RESULT"
    assert all(event.text != "private native trace" for event in session.events)


def test_hermes_inventory_is_content_free_and_requires_explicit_selection(
    tmp_path: Path,
) -> None:
    path = _native_store(tmp_path)
    inventory = hermes.list_sessions(path)

    assert [item.session_id for item in inventory] == [SECOND_ID, NATIVE_ID]
    assert inventory[1] == hermes.HermesSessionInventory(
        session_id=NATIVE_ID,
        title="Hermes native fixture",
        cwd=Path("/synthetic/hermes-work"),
        started_at="2026-08-30T12:34:56Z",
        cli_version=None,
        updated_ns=1_788_093_316_000_000_000,
        records=8,
    )
    serialized = json.dumps([asdict(item) for item in inventory], default=str)
    assert "HERMES_SECOND_PRIVATE_BODY" not in serialized
    assert "HERMES_NATIVE_USER" not in serialized
    with pytest.raises(SessionMigrateError, match="multiple sessions"):
        hermes.parse_session(path)


def test_hermes_single_session_can_be_selected_implicitly(tmp_path: Path) -> None:
    path = _native_store(tmp_path, include_second=False)
    assert hermes.parse_session(path).session_id == NATIVE_ID


def test_hermes_snapshot_includes_uncheckpointed_wal_commit(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    db = _create_store(path)
    db.execute("PRAGMA journal_mode=WAL")
    _insert_session(db, NATIVE_ID, title="WAL-backed Hermes fixture")
    db.commit()
    snapshot = hermes.database_snapshot(path)
    db.close()

    assert snapshot.sha256 == __import__("hashlib").sha256(snapshot.data).hexdigest()
    with hermes._database_from_bytes(snapshot.data) as copy:
        hermes._validate_database(copy)
        assert copy.execute("SELECT title FROM sessions").fetchone()[0] == (
            "WAL-backed Hermes fixture"
        )


def test_hermes_rejects_symlink_and_unsupported_schema(tmp_path: Path) -> None:
    path = _native_store(tmp_path / "native")
    link = tmp_path / "linked.db"
    os.symlink(path, link)
    with pytest.raises(JsonlError, match="non-symlink"):
        hermes.database_snapshot(link)

    old = tmp_path / "old.db"
    db = _create_store(old, schema_version=25)
    db.commit()
    db.close()
    with pytest.raises(JsonlError, match="expected 26"):
        hermes.list_sessions(old)
