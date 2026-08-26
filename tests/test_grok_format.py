import json
from pathlib import Path

import pytest

from session_migrate.errors import JsonlError, SessionMigrateError
from session_migrate.formats import grok
from session_migrate.model import AgentFormat, Event, EventKind, Provenance, Role, Session

SESSION_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def envelope(update: dict[str, object]) -> dict[str, object]:
    return {
        "timestamp": 1787745600,
        "method": "session/update",
        "params": {"sessionId": SESSION_ID, "update": update},
    }


def write_native_session(tmp_path: Path) -> Path:
    session = tmp_path / "sessions" / "%2Ftmp%2Fgrok-project" / SESSION_ID
    session.mkdir(parents=True)
    summary = {
        "info": {"id": SESSION_ID, "cwd": "/tmp/grok-project"},
        "session_summary": "GROK_USER",
        "created_at": "2026-08-26T12:00:00Z",
        "updated_at": "2026-08-26T12:00:00Z",
        "num_messages": 7,
        "num_chat_messages": 2,
        "current_model_id": "grok-build",
        "chat_format_version": 1,
        "generated_title": "Synthetic Grok session",
    }
    updates = [
        envelope(
            {
                "sessionUpdate": "user_message_chunk",
                "content": {"type": "text", "text": "GROK_USER"},
            }
        ),
        envelope(
            {
                "sessionUpdate": "user_message_chunk",
                "content": {
                    "type": "image",
                    "data": "c3ludGhldGlj",
                    "mimeType": "image/png",
                    "uri": "data:image/png;base64,c3ludGhldGlj",
                },
            }
        ),
        envelope(
            {
                "sessionUpdate": "agent_thought_chunk",
                "content": {"type": "text", "text": "PRIVATE_GROK_THOUGHT"},
            }
        ),
        envelope(
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "call-grok-1",
                "title": "read",
                "kind": "other",
                "status": "pending",
                "rawInput": {"path": "a.txt"},
            }
        ),
        envelope(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "call-grok-1",
                "title": "read",
                "status": "completed",
                "content": [
                    {
                        "type": "content",
                        "content": {"type": "text", "text": "GROK_RESULT"},
                    }
                ],
            }
        ),
        envelope(
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "GROK_ASSISTANT"},
            }
        ),
        envelope({"sessionUpdate": "available_commands_update", "availableCommands": []}),
    ]
    (session / "summary.json").write_text(json.dumps(summary))
    (session / "updates.jsonl").write_text("".join(json.dumps(item) + "\n" for item in updates))
    return session


def test_grok_source_projects_native_updates(tmp_path: Path) -> None:
    source = grok.parse_session(write_native_session(tmp_path))

    assert source.source_format == AgentFormat.GROK
    assert source.session_id == SESSION_ID
    assert source.title == "Synthetic Grok session"
    assert source.event_counts() == {
        "context": 1,
        "message": 2,
        "opaque": 2,
        "tool_call": 1,
        "tool_result": 1,
    }
    call = next(item for item in source.events if item.kind == EventKind.TOOL_CALL)
    result = next(item for item in source.events if item.kind == EventKind.TOOL_RESULT)
    assert call.payload["input"] == {"path": "a.txt"}
    assert result.text == "GROK_RESULT"


def test_grok_writer_round_trips_messages_tools_and_image(tmp_path: Path) -> None:
    source = grok.parse_session(write_native_session(tmp_path))
    target_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

    data, dropped = grok.serialize(
        source,
        session_id=target_id,
        cwd=Path("/tmp/grok-target"),
        title="Migrated Grok session",
    )
    parsed = grok.validate_native_bytes(data, target_id)
    summary, updates = grok.native_files(data, target_id)

    assert parsed.summary["generated_title"] == "Migrated Grok session"
    assert json.loads(summary)["info"]["id"] == target_id
    assert len(updates.splitlines()) == len(parsed.updates)
    assert grok.native_record_count(data) == len(parsed.updates) + 1
    assert dropped == {
        "grok_available_commands_update": 1,
        "grok_private_thinking": 1,
    }


def test_grok_writer_counts_private_thinking_and_flattens_compaction(tmp_path: Path) -> None:
    source = Session(
        source_format=AgentFormat.CODEX,
        source_path=tmp_path / "source.jsonl",
        source_sha256="0" * 64,
        session_id=None,
        cwd=tmp_path,
        started_at="2026-08-26T12:00:00Z",
        cli_version=None,
        model=None,
        title=None,
        events=(
            Event(EventKind.MESSAGE, Provenance(0), role=Role.USER, text="hello"),
            Event(EventKind.THINKING, Provenance(1), role=Role.ASSISTANT, text="private"),
            Event(
                EventKind.COMPACTION,
                Provenance(2),
                role=Role.SYSTEM,
                text="summary",
                payload={"has_boundary_metadata": True},
            ),
        ),
        raw_record_count=3,
    )

    data, dropped = grok.serialize(source, session_id=SESSION_ID, cwd=tmp_path)

    grok.validate_native_bytes(data, SESSION_ID)
    assert dropped == {
        "compaction:boundary_metadata": 1,
        "compaction:flattened": 1,
        "thinking:private": 1,
    }


@pytest.mark.parametrize("mutation", ["wrong_session", "bad_method", "missing_content"])
def test_grok_source_rejects_malformed_updates(tmp_path: Path, mutation: str) -> None:
    session = write_native_session(tmp_path)
    path = session / "updates.jsonl"
    lines = path.read_text().splitlines()
    value = json.loads(lines[0])
    if mutation == "wrong_session":
        value["params"]["sessionId"] = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    elif mutation == "bad_method":
        value["method"] = "session/other"
    else:
        del value["params"]["update"]["content"]
    lines[0] = json.dumps(value)
    path.write_text("\n".join(lines) + "\n")

    with pytest.raises((JsonlError, SessionMigrateError)):
        grok.parse_session(session)


def test_grok_bundle_rejects_duplicate_json_and_wrong_target(tmp_path: Path) -> None:
    source = grok.parse_session(write_native_session(tmp_path))
    data, _ = grok.serialize(source, session_id=SESSION_ID, cwd=tmp_path)
    duplicate = data.decode().replace(
        '"schema":"session-migrate.grok.v1"',
        '"schema":"first","schema":"session-migrate.grok.v1"',
        1,
    )

    with pytest.raises(SessionMigrateError, match="valid UTF-8 JSON"):
        grok.validate_native_bytes(duplicate.encode(), SESSION_ID)
    with pytest.raises(SessionMigrateError, match="linkage"):
        grok.validate_native_bytes(data, "dddddddd-dddd-4ddd-8ddd-dddddddddddd")


def test_grok_cwd_encoding_matches_short_url_encoded_layout() -> None:
    assert grok.session_relative_path(Path("/tmp/a b"), SESSION_ID) == Path(
        "sessions/%2Ftmp%2Fa%20b"
    ) / SESSION_ID
