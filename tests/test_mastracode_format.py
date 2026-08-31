import json
import os
import sqlite3
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from session_migrate.errors import JsonlError, SessionMigrateError
from session_migrate.formats import mastracode
from session_migrate.model import AgentFormat, Event, EventKind, Provenance, Role, Session

SESSION_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SECOND_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _source(tmp_path: Path) -> Session:
    return Session(
        source_format=AgentFormat.CLAUDE,
        source_path=tmp_path / "source.jsonl",
        source_sha256="0" * 64,
        session_id="11111111-1111-4111-8111-111111111111",
        cwd=tmp_path,
        started_at="2026-08-30T12:34:56Z",
        cli_version="2.1.209",
        model="fixture-model",
        title="MastraCode portable fixture",
        events=(
            Event(
                EventKind.MESSAGE,
                Provenance(0, "user"),
                role=Role.USER,
                timestamp="2026-08-30T12:34:56Z",
                text="MASTRACODE_IMPORTED_USER_ALPHA",
            ),
            Event(
                EventKind.CONTEXT,
                Provenance(0, "user", block_index=1),
                role=Role.USER,
                timestamp="2026-08-30T12:34:56Z",
                payload={
                    "block_type": "image",
                    "image_url": "data:image/png;base64,iVBORw0KGgo=",
                },
            ),
            Event(
                EventKind.MESSAGE,
                Provenance(1, "assistant"),
                role=Role.ASSISTANT,
                timestamp="2026-08-30T12:34:57Z",
                text="MASTRACODE_IMPORTED_ASSISTANT_BETA",
            ),
            Event(
                EventKind.TOOL_CALL,
                Provenance(1, "assistant", block_index=1),
                role=Role.ASSISTANT,
                timestamp="2026-08-30T12:34:57Z",
                tool_name="execute_command",
                tool_call_id="call_mastracode_fixture",
                payload={"input": {"command": "printf MASTRACODE_IMPORTED_TOOL_GAMMA"}},
            ),
            Event(
                EventKind.TOOL_RESULT,
                Provenance(2, "tool"),
                role=Role.TOOL,
                timestamp="2026-08-30T12:34:58Z",
                text="MASTRACODE_IMPORTED_TOOL_GAMMA",
                tool_name="execute_command",
                tool_call_id="call_mastracode_fixture",
                payload={"content": {"stdout": "MASTRACODE_IMPORTED_TOOL_GAMMA"}},
            ),
            Event(
                EventKind.THINKING,
                Provenance(3, "assistant"),
                role=Role.ASSISTANT,
                timestamp="2026-08-30T12:34:59Z",
                text="private provider reasoning",
                payload={"encrypted_content": "private encrypted provider payload"},
            ),
            Event(
                EventKind.COMPACTION,
                Provenance(4, "compaction"),
                timestamp="2026-08-30T12:35:00Z",
                text="MASTRACODE_IMPORTED_SUMMARY_DELTA",
            ),
            Event(
                EventKind.MESSAGE,
                Provenance(5, "user"),
                role=Role.USER,
                timestamp="2026-08-30T12:35:01Z",
                text="MASTRACODE_IMPORTED_FOLLOWUP_EPSILON",
            ),
            Event(
                EventKind.OPAQUE,
                Provenance(6, "future"),
                payload={"reason": "fixture_future_field"},
            ),
        ),
        raw_record_count=9,
        model_provider="fixture",
    )


def _artifact(tmp_path: Path, session_id: str = SESSION_ID) -> bytes:
    data, _dropped = mastracode.serialize(
        _source(tmp_path),
        session_id=session_id,
        cwd=tmp_path,
        timestamp="2026-08-30T12:34:56Z",
        resource_id="mastracode-format-tests",
    )
    return data


def _mutate(data: bytes, tmp_path: Path, sql: str, values: tuple[object, ...] = ()) -> bytes:
    path = tmp_path / "mutate.db"
    path.write_bytes(data)
    with sqlite3.connect(path) as db:
        db.execute(sql, values)
        db.commit()
    return path.read_bytes()


def test_mastracode_round_trip_preserves_resumable_history(tmp_path: Path) -> None:
    data, dropped = mastracode.serialize(
        _source(tmp_path),
        session_id=SESSION_ID.upper(),
        cwd=tmp_path,
        timestamp="2026-08-30T12:34:56Z",
        resource_id="mastracode-format-tests",
    )
    parsed_artifact = mastracode.validate_native_bytes(data, SESSION_ID)
    target = tmp_path / "home" / "mastracode" / "mastra.db"
    installed = mastracode.install_native_bytes(data, target, session_id=SESSION_ID)
    session = mastracode.parse_session(installed, SESSION_ID)

    assert parsed_artifact.session_id == SESSION_ID
    assert parsed_artifact.resource_id == "mastracode-format-tests"
    assert parsed_artifact.cwd == tmp_path.resolve()
    assert parsed_artifact.cli_version == mastracode.PINNED_MASTRACODE_VERSION
    assert parsed_artifact.messages == mastracode.native_record_count(data)
    assert dropped == {
        "opaque:fixture_future_field": 1,
        "thinking:private": 1,
        "thinking:provider_payload": 1,
    }
    assert session.source_format == AgentFormat.MASTRACODE
    assert session.title == "MastraCode portable fixture"
    assert session.cwd == tmp_path.resolve()
    assert session.model == "fixture-model"
    assert session.model_provider is None
    assert session.event_counts() == {
        "compaction": 1,
        "context": 1,
        "message": 3,
        "tool_call": 1,
        "tool_result": 1,
    }
    assert any(
        event.kind == EventKind.TOOL_RESULT
        and event.payload["content"] == {"stdout": "MASTRACODE_IMPORTED_TOOL_GAMMA"}
        for event in session.events
    )
    assert oct(target.stat().st_mode & 0o777) == "0o600"
    assert oct(target.parent.stat().st_mode & 0o777) == "0o700"


def test_mastracode_accounts_for_each_omitted_tool_result_block(tmp_path: Path) -> None:
    source = _source(tmp_path)
    with_non_text_results = replace(
        source,
        events=tuple(
            replace(
                event,
                payload={
                    **event.payload,
                    "content_blocks": [
                        {"type": "text", "text": event.text},
                        {
                            "type": "image",
                            "image_url": "data:image/png;base64,iVBORw0KGgo=",
                        },
                        {"type": "document", "name": "result.pdf"},
                    ],
                },
            )
            if event.kind == EventKind.TOOL_RESULT
            else event
            for event in source.events
        ),
    )

    data, dropped = mastracode.serialize(
        with_non_text_results,
        session_id=SESSION_ID,
        cwd=tmp_path,
        timestamp="2026-08-30T12:34:56Z",
        resource_id="mastracode-loss-accounting",
    )
    mastracode.validate_native_bytes(data, SESSION_ID)
    target = tmp_path / "round-trip" / "mastra.db"
    installed = mastracode.install_native_bytes(data, target, session_id=SESSION_ID)
    reparsed = mastracode.parse_session(installed, SESSION_ID)

    assert dropped["tool_result:non_text_content"] == 2
    assert all(count > 0 for count in dropped.values())
    result = next(event for event in reparsed.events if event.kind == EventKind.TOOL_RESULT)
    assert result.payload["content"] == {"stdout": "MASTRACODE_IMPORTED_TOOL_GAMMA"}


def test_mastracode_inventory_is_content_free_and_handles_multiple_threads(
    tmp_path: Path,
) -> None:
    target = tmp_path / "mastra.db"
    first = _artifact(tmp_path)
    second = _artifact(tmp_path, SECOND_ID)
    mastracode.install_native_bytes(first, target, session_id=SESSION_ID)
    mastracode.install_native_bytes(second, target, session_id=SECOND_ID)

    rows = mastracode.list_sessions(target)
    assert {row.session_id for row in rows} == {SESSION_ID, SECOND_ID}
    assert all(row.title == "MastraCode portable fixture" for row in rows)
    assert all(row.cwd == tmp_path.resolve() for row in rows)
    assert all(row.records == mastracode.native_record_count(first) for row in rows)
    assert all(row.cli_version == mastracode.PINNED_MASTRACODE_VERSION for row in rows)
    assert set(asdict(rows[0])) == {
        "session_id",
        "title",
        "cwd",
        "started_at",
        "cli_version",
        "updated_ns",
        "records",
    }
    with pytest.raises(SessionMigrateError, match="multiple sessions"):
        mastracode.parse_session(target)
    assert mastracode.parse_session(target, SECOND_ID).session_id == SECOND_ID


def test_mastracode_install_is_transactional_and_collision_safe(tmp_path: Path) -> None:
    target = tmp_path / "mastra.db"
    data = _artifact(tmp_path)
    assert (
        mastracode.install_native_bytes(data, target, session_id=SESSION_ID, dry_run=True) == target
    )
    assert not target.exists()
    mastracode.install_native_bytes(data, target, session_id=SESSION_ID)
    before = target.read_bytes()
    with pytest.raises(SessionMigrateError, match="overwrite"):
        mastracode.install_native_bytes(data, target, session_id=SESSION_ID)
    assert target.read_bytes() == before
    mastracode.install_native_bytes(data, target, session_id=SESSION_ID, overwrite=True)
    assert mastracode.parse_session(target, SESSION_ID).session_id == SESSION_ID


def test_mastracode_reads_native_signal_reasoning_internal_parts_and_observation(
    tmp_path: Path,
) -> None:
    target = tmp_path / "mastra.db"
    mastracode.install_native_bytes(_artifact(tmp_path), target, session_id=SESSION_ID)
    with sqlite3.connect(target) as db:
        first = db.execute(
            'SELECT id,content FROM "mastra_messages" ORDER BY createdAt,id LIMIT 1'
        ).fetchone()
        assert first is not None
        content = json.loads(first[1])
        content["metadata"] = {"signal": {"source": "headless"}}
        db.execute(
            'UPDATE "mastra_messages" SET role="signal",type="user",content=? WHERE id=?',
            (json.dumps(content), first[0]),
        )
        db.execute(
            'INSERT INTO "mastra_messages" VALUES (?,?,?,?,?,?,?)',
            (
                "om-continuation",
                SESSION_ID,
                json.dumps(
                    {
                        "format": 2,
                        "parts": [{"type": "text", "text": "internal native reminder"}],
                    }
                ),
                "user",
                "v2",
                "2026-08-30T12:35:02Z",
                "mastracode-format-tests",
            ),
        )
        db.execute(
            'INSERT INTO "mastra_messages" VALUES (?,?,?,?,?,?,?)',
            (
                "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                SESSION_ID,
                json.dumps(
                    {
                        "format": 2,
                        "parts": [
                            {"type": "reasoning", "reasoning": "readable native reasoning"},
                            {"type": "future-native-part", "data": {"value": 1}},
                        ],
                    }
                ),
                "assistant",
                "v2",
                "2026-08-30T12:35:03Z",
                "mastracode-format-tests",
            ),
        )
        db.execute(
            'CREATE TABLE "mastra_observational_memory" ('
            '"threadId" TEXT,"activeObservations" TEXT,"updatedAt" TEXT)'
        )
        db.execute(
            'INSERT INTO "mastra_observational_memory" VALUES (?,?,?)',
            (SESSION_ID, "native active observation", "2026-08-30T12:35:04Z"),
        )
        db.commit()

    session = mastracode.parse_session(target, SESSION_ID)
    assert session.events[0].kind == EventKind.COMPACTION
    assert session.events[0].text == "native active observation"
    assert any(
        event.kind == EventKind.MESSAGE
        and event.role == Role.USER
        and event.text == "MASTRACODE_IMPORTED_USER_ALPHA"
        for event in session.events
    )
    assert any(
        event.kind == EventKind.THINKING
        and event.text == "readable native reasoning"
        and event.payload["source_readable_reasoning"] is True
        for event in session.events
    )
    assert sum(event.kind == EventKind.OPAQUE for event in session.events) == 2


@pytest.mark.parametrize(
    ("sql", "values", "match"),
    [
        (
            'UPDATE "mastra_messages" SET thread_id=?',
            (SECOND_ID,),
            "message count|linkage",
        ),
        (
            'UPDATE "mastra_messages" SET resourceId=?',
            ("wrong-resource",),
            "resource linkage",
        ),
        ('UPDATE "mastra_messages" SET role="developer"', (), "unsupported role"),
        ('UPDATE "mastra_messages" SET createdAt="not-a-date"', (), "timestamp"),
        ('UPDATE "mastra_messages" SET content="not-json"', (), "invalid JSON"),
        (
            'UPDATE "mastra_messages" SET content=?',
            (json.dumps({"format": 1, "parts": [{"type": "text", "text": "x"}]}),),
            "not format 2",
        ),
        (
            'UPDATE "mastra_messages" SET content=?',
            (json.dumps({"format": 2, "parts": [{"type": "future"}]}),),
            "unsupported part",
        ),
        (
            'UPDATE "mastra_messages" SET content=?',
            (json.dumps({"format": 2, "parts": [{"type": "text", "text": 7}]}),),
            "text part",
        ),
        (
            'UPDATE "mastra_messages" SET content=?',
            (
                json.dumps(
                    {
                        "format": 2,
                        "parts": [
                            {"type": "file", "mimeType": "image/png", "data": "not-an-image"}
                        ],
                    }
                ),
            ),
            "image part",
        ),
        (
            'UPDATE "mastra_messages" SET content=?',
            (
                json.dumps(
                    {
                        "format": 2,
                        "parts": [
                            {
                                "type": "tool-invocation",
                                "toolInvocation": {
                                    "state": "call",
                                    "toolCallId": "call-bad",
                                    "toolName": "bad",
                                    "args": [],
                                },
                            }
                        ],
                    }
                ),
            ),
            "invalid fields",
        ),
        (
            'UPDATE "mastra_messages" SET content=?',
            ('{"format":2,"format":2,"parts":[{"type":"text","text":"x"}]}',),
            "invalid JSON",
        ),
    ],
)
def test_mastracode_generated_artifact_validation_fails_closed(
    tmp_path: Path, sql: str, values: tuple[object, ...], match: str
) -> None:
    changed = _mutate(_artifact(tmp_path), tmp_path, sql, values)
    with pytest.raises(SessionMigrateError, match=match):
        mastracode.validate_native_bytes(changed, SESSION_ID)


def test_mastracode_rejects_schema_drift_and_unsafe_database_paths(tmp_path: Path) -> None:
    changed = _mutate(_artifact(tmp_path), tmp_path, 'CREATE TABLE "future" (value TEXT)')
    with pytest.raises(SessionMigrateError, match="table schema"):
        mastracode.validate_native_bytes(changed, SESSION_ID)

    real = tmp_path / "real.db"
    real.write_bytes(_artifact(tmp_path))
    symlink = tmp_path / "linked.db"
    symlink.symlink_to(real)
    with pytest.raises(JsonlError, match="symbolic link"):
        mastracode.list_sessions(symlink)


def test_mastracode_paths_ids_and_snapshot(tmp_path: Path) -> None:
    assert mastracode.normalized_session_id(SESSION_ID.upper()) == SESSION_ID
    for value in ("", "not-an-id", "00000000-0000-0000-0000-000000000000"):
        with pytest.raises(SessionMigrateError, match="UUID"):
            mastracode.normalized_session_id(value)

    assert mastracode.database_candidates(
        tmp_path, environ={"MASTRA_DB_PATH": "/configured/mastra.db"}
    ) == (Path("/configured/mastra.db"),)
    assert mastracode.database_candidates(
        tmp_path, environ={"MASTRA_APP_DATA_DIR": "/app-data"}
    ) == (Path("/app-data/mastra.db"),)
    assert mastracode.database_candidates(tmp_path, platform="linux", environ={}) == (
        tmp_path / ".local/share/mastracode/mastra.db",
    )
    assert mastracode.database_candidates(tmp_path, platform="darwin", environ={}) == (
        tmp_path / "Library/Application Support/mastracode/mastra.db",
    )
    assert mastracode.database_candidates(
        tmp_path, platform="win32", environ={"APPDATA": "C:/Users/fixture/AppData/Roaming"}
    ) == (Path("C:/Users/fixture/AppData/Roaming/mastracode/mastra.db"),)

    work = tmp_path / "Feature Work"
    work.mkdir()
    expected_hash = __import__("hashlib").sha256(str(work.resolve()).encode()).hexdigest()[:12]
    assert mastracode.resource_id_for_cwd(work) == f"feature-work-{expected_hash}"

    target = tmp_path / "mastra.db"
    target.write_bytes(_artifact(tmp_path))
    before = mastracode.database_snapshot(target)
    assert before == mastracode.database_snapshot(target)
    assert len(before.fingerprint) == 64
    os.utime(target, ns=(target.stat().st_atime_ns, target.stat().st_mtime_ns + 1))
    assert mastracode.database_snapshot(target) != before
