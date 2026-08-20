import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from session_migrate.errors import SessionMigrateError
from session_migrate.formats import antigravity
from session_migrate.model import AgentFormat, Event, EventKind, Provenance, Role, Session

FIXTURE = Path(__file__).parent / "fixtures" / "antigravity-1.1.16" / "basic.json"
TARGET_ID = "33333333-3333-4333-8333-333333333333"
TRAJECTORY_ID = "44444444-4444-4444-8444-444444444444"


def portable_session(tmp_path: Path) -> Session:
    events = (
        Event(
            kind=EventKind.MESSAGE,
            role=Role.USER,
            text="AGY_USER_ALPHA",
            timestamp="2026-08-20T12:00:00Z",
            provenance=Provenance(0, "user"),
        ),
        Event(
            kind=EventKind.MESSAGE,
            role=Role.ASSISTANT,
            text="AGY_ASSISTANT_OMEGA",
            timestamp="2026-08-20T12:00:01Z",
            provenance=Provenance(1, "assistant"),
        ),
        Event(
            kind=EventKind.TOOL_CALL,
            role=Role.ASSISTANT,
            tool_name="echo_marker",
            tool_call_id="agy-call-1",
            timestamp="2026-08-20T12:00:02Z",
            payload={"input": {"text": "TOOL_INPUT_ALPHA", "count": 2}},
            provenance=Provenance(2, "tool_call"),
        ),
        Event(
            kind=EventKind.TOOL_RESULT,
            role=Role.TOOL,
            text="TOOL_RESULT_OMEGA",
            tool_name="echo_marker",
            tool_call_id="agy-call-1",
            timestamp="2026-08-20T12:00:03Z",
            payload={
                "is_error": False,
                "content_blocks": [{"type": "text", "text": "TOOL_RESULT_OMEGA"}],
            },
            provenance=Provenance(3, "tool_result"),
        ),
        Event(
            kind=EventKind.THINKING,
            role=Role.ASSISTANT,
            text="PRIVATE_THINKING_MUST_NOT_SURVIVE",
            provenance=Provenance(4, "thinking"),
        ),
    )
    return Session(
        source_format=AgentFormat.CLAUDE,
        source_path=tmp_path / "source.jsonl",
        source_sha256="0" * 64,
        session_id="11111111-1111-4111-8111-111111111111",
        cwd=tmp_path,
        started_at="2026-08-20T12:00:00Z",
        cli_version="2.1.209",
        model="fixture-model",
        title="Antigravity fixture",
        events=events,
        raw_record_count=len(events),
    )


def serialized(tmp_path: Path) -> bytes:
    data, _ = antigravity.serialize(
        portable_session(tmp_path),
        session_id=TARGET_ID,
        trajectory_id=TRAJECTORY_ID,
        cwd=tmp_path,
        timestamp="2026-08-20T12:00:00Z",
    )
    return data


def fixture_database(tmp_path: Path) -> Path:
    value = json.loads(FIXTURE.read_text())
    rows = tuple(
        antigravity._StepRow(
            row["idx"],
            row["step_type"],
            row["status"],
            None,
            bytes.fromhex(row["step_payload_hex"]),
        )
        for row in value["steps"]
    )
    data = antigravity._build_database(
        rows,
        conversation_id=value["conversation_id"],
        trajectory_id=value["trajectory_id"],
        started_at=value["created_at"],
    )
    path = tmp_path / f"{value['conversation_id']}.db"
    path.write_bytes(data)
    return path


def event_signature(events: tuple[Event, ...]) -> list[tuple[object, ...]]:
    result = []
    for event in events:
        if event.kind == EventKind.THINKING:
            continue
        payload = event.payload.get("input") if event.kind == EventKind.TOOL_CALL else None
        result.append(
            (
                event.kind,
                event.role,
                event.text,
                event.tool_name,
                event.tool_call_id,
                json.dumps(payload, sort_keys=True),
            )
        )
    return result


def test_writer_parser_round_trip_preserves_messages_and_generic_tools(tmp_path: Path) -> None:
    source = portable_session(tmp_path)
    data, dropped = antigravity.serialize(
        source,
        session_id=TARGET_ID,
        trajectory_id=TRAJECTORY_ID,
        cwd=tmp_path,
        timestamp="2026-08-20T12:00:00Z",
    )
    path = tmp_path / f"{TARGET_ID}.db"
    path.write_bytes(data)

    antigravity.validate_native_bytes(data, TARGET_ID)
    parsed = antigravity.parse(path)

    assert dropped == {"thinking:private": 1}
    assert b"PRIVATE_THINKING_MUST_NOT_SURVIVE" not in data
    assert antigravity.native_record_count(data) == 4
    assert event_signature(parsed.events) == event_signature(source.events)


def test_sanitized_fixture_projects_content_free_thinking_and_tools(tmp_path: Path) -> None:
    parsed = antigravity.parse(fixture_database(tmp_path))

    assert parsed.raw_record_count == 3
    assert [(event.kind, event.text) for event in parsed.events] == [
        (EventKind.MESSAGE, "AGY_FIXTURE_USER_ALPHA"),
        (EventKind.THINKING, None),
        (EventKind.MESSAGE, "AGY_FIXTURE_ASSISTANT_OMEGA"),
        (EventKind.TOOL_CALL, None),
        (EventKind.TOOL_RESULT, "TOOL_RESULT_OMEGA"),
    ]
    assert parsed.events[3].tool_call_id == "fixture-call-1"
    assert parsed.events[3].payload["input"] == {"text": "TOOL_INPUT_ALPHA"}
    assert all(event.text != "AGY_PRIVATE_THINKING_NEVER_PROJECTED" for event in parsed.events)


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        (
            "UPDATE trajectory_meta SET cascade_id='55555555-5555-4555-8555-555555555555'",
            "conversation ID does not match",
        ),
        ("UPDATE steps SET step_type=15 WHERE idx=0", "payload type disagrees"),
        ("UPDATE steps SET idx=9 WHERE idx=0", "indices are not contiguous"),
        ("CREATE TABLE unexpected(value TEXT)", "table set does not match"),
    ],
)
def test_validator_fails_closed_on_database_inconsistency(
    tmp_path: Path, statement: str, message: str
) -> None:
    path = tmp_path / f"{TARGET_ID}.db"
    path.write_bytes(serialized(tmp_path))
    with sqlite3.connect(path) as db:
        db.execute(statement)

    with pytest.raises(SessionMigrateError, match=message):
        antigravity.validate_native_bytes(path.read_bytes(), TARGET_ID)


def test_validator_rejects_malformed_protobuf_and_id_mismatch(tmp_path: Path) -> None:
    path = tmp_path / f"{TARGET_ID}.db"
    path.write_bytes(serialized(tmp_path))
    with sqlite3.connect(path) as db:
        db.execute("UPDATE steps SET step_payload=x'80' WHERE idx=0")

    with pytest.raises(SessionMigrateError, match="varint is truncated"):
        antigravity.validate_native_bytes(path.read_bytes(), TARGET_ID)
    with pytest.raises(SessionMigrateError, match="does not match target"):
        antigravity.validate_native_bytes(
            serialized(tmp_path), "55555555-5555-4555-8555-555555555555"
        )


def test_snapshot_includes_uncheckpointed_wal_rows(tmp_path: Path) -> None:
    path = tmp_path / f"{TARGET_ID}.db"
    path.write_bytes(serialized(tmp_path))
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "INSERT INTO steps(idx,step_type,status,has_subtrajectory,step_payload,step_format) "
            "SELECT 4,step_type,status,has_subtrajectory,step_payload,step_format "
            "FROM steps WHERE idx=0"
        )
        connection.commit()
        parsed = antigravity.parse(path)
    finally:
        connection.close()

    assert parsed.raw_record_count == 5
    assert parsed.events[-1].text == "AGY_USER_ALPHA"


def test_install_is_private_transactional_and_collision_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = serialized(tmp_path)
    target_home = tmp_path / "agy-home" / ".gemini" / "antigravity-cli"
    monkeypatch.setattr(antigravity, "verify_pinned_cli", lambda *args, **kwargs: Path("agy"))

    dry = antigravity.install_database(
        data,
        session_id=TARGET_ID,
        cwd=tmp_path,
        timestamp="2026-08-20T12:00:00Z",
        title="Installed fixture",
        target_home=target_home,
        dry_run=True,
    )
    assert not target_home.exists()
    assert not dry.conversation_path.exists()

    installed = antigravity.install_database(
        data,
        session_id=TARGET_ID,
        cwd=tmp_path,
        timestamp="2026-08-20T12:00:00Z",
        title="Installed fixture",
        target_home=target_home,
    )
    assert stat.S_IMODE(installed.conversation_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(installed.summaries_path.stat().st_mode) == 0o600
    parsed = antigravity.parse(installed.conversation_path)
    assert parsed.session_id == TARGET_ID
    assert parsed.title == "Installed fixture"
    assert parsed.cwd == tmp_path
    with sqlite3.connect(installed.summaries_path) as db:
        summary = db.execute(
            "SELECT title,step_count,workspace_uris,last_user_input_step_index "
            "FROM conversation_summaries WHERE conversation_id=?",
            (TARGET_ID,),
        ).fetchone()
    assert summary == (
        "Installed fixture",
        4,
        json.dumps([tmp_path.as_uri()], separators=(",", ":")),
        0,
    )
    with pytest.raises(SessionMigrateError, match="already exists"):
        antigravity.install_database(
            data,
            session_id=TARGET_ID,
            cwd=tmp_path,
            timestamp="2026-08-20T12:00:00Z",
            title=None,
            target_home=target_home,
        )


def test_failed_summary_insert_removes_only_new_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = serialized(tmp_path)
    target_home = tmp_path / "target"
    monkeypatch.setattr(antigravity, "verify_pinned_cli", lambda *args, **kwargs: Path("agy"))
    original = antigravity._summary_values

    def invalid_summary(**kwargs: object) -> tuple[object, ...]:
        values = list(original(**kwargs))  # type: ignore[arg-type]
        values[3] = None
        return tuple(values)

    monkeypatch.setattr(antigravity, "_summary_values", invalid_summary)
    with pytest.raises(sqlite3.IntegrityError):
        antigravity.install_database(
            data,
            session_id=TARGET_ID,
            cwd=tmp_path,
            timestamp="2026-08-20T12:00:00Z",
            title=None,
            target_home=target_home,
        )

    assert not (target_home / antigravity.session_relative_path(TARGET_ID)).exists()
    with sqlite3.connect(target_home / "conversation_summaries.db") as db:
        assert db.execute("SELECT count(*) FROM conversation_summaries").fetchone() == (0,)


def test_exact_binary_gate_rejects_unpinned_executable(tmp_path: Path) -> None:
    executable = tmp_path / "agy"
    executable.write_bytes(b"not the pinned Antigravity binary")
    os.chmod(executable, 0o700)

    with pytest.raises(SessionMigrateError, match="binary mismatch"):
        antigravity.verify_pinned_cli(executable)
