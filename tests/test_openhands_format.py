import json
from pathlib import Path

import pytest

from session_migrate.errors import JsonlError, SessionMigrateError
from session_migrate.formats import openhands
from session_migrate.model import AgentFormat, Event, EventKind, Provenance, Role, Session

SESSION_ID = "77777777-7777-4777-8777-777777777777"


def event(event_id: str, kind: str, source: str, **values: object) -> dict[str, object]:
    return {
        "id": event_id,
        "timestamp": "2026-08-26T12:00:00.000001",
        "source": source,
        **values,
        "kind": kind,
    }


def write_native_session(tmp_path: Path) -> Path:
    conversation = tmp_path / SESSION_ID.replace("-", "")
    events = conversation / "events"
    events.mkdir(parents=True)
    records = [
        event(
            "00000000-0000-4000-8000-000000000001",
            "SystemPromptEvent",
            "agent",
            system_prompt={
                "cache_prompt": False,
                "type": "text",
                "text": "Synthetic system prompt",
            },
            tools=[],
            dynamic_context={"cache_prompt": False, "type": "text", "text": ""},
        ),
        event(
            "00000000-0000-4000-8000-000000000002",
            "MessageEvent",
            "user",
            llm_message={
                "role": "user",
                "content": [
                    {"cache_prompt": False, "type": "text", "text": "OPENHANDS_USER"},
                    {
                        "type": "image",
                        "image_urls": ["data:image/png;base64,c3ludGhldGlj"],
                    },
                ],
                "thinking_blocks": [],
            },
            activated_skills=[],
            extended_content=[],
        ),
        event(
            "00000000-0000-4000-8000-000000000003",
            "ActionEvent",
            "agent",
            thought=[{"cache_prompt": False, "type": "text", "text": "private"}],
            thinking_blocks=[],
            action={"command": "{}", "kind": "TerminalAction"},
            tool_name="terminal",
            tool_call_id="call-openhands-1",
            tool_call={
                "id": "call-openhands-1",
                "name": "terminal",
                "arguments": "{\"command\":\"pwd\"}",
                "origin": "completion",
            },
        ),
        event(
            "00000000-0000-4000-8000-000000000004",
            "ObservationEvent",
            "environment",
            tool_name="terminal",
            tool_call_id="call-openhands-1",
            observation={
                "content": [
                    {
                        "cache_prompt": False,
                        "type": "text",
                        "text": "OPENHANDS_RESULT",
                    }
                ],
                "is_error": False,
                "kind": "TerminalObservation",
            },
            action_id="00000000-0000-4000-8000-000000000003",
        ),
        event(
            "00000000-0000-4000-8000-000000000005",
            "MessageEvent",
            "agent",
            llm_message={
                "role": "assistant",
                "content": [
                    {
                        "cache_prompt": False,
                        "type": "text",
                        "text": "OPENHANDS_ASSISTANT",
                    }
                ],
                "thinking_blocks": [{"type": "thinking", "thinking": "private"}],
            },
            activated_skills=["synthetic-skill"],
            extended_content=[],
        ),
        event(
            "00000000-0000-4000-8000-000000000006",
            "Condensation",
            "environment",
            forgotten_event_ids=[],
            summary="OPENHANDS_SUMMARY",
        ),
    ]
    for index, record in enumerate(records):
        path = events / f"event-{index:05d}-{record['id']}.json"
        path.write_text(json.dumps(record))
    return conversation


def test_openhands_source_projects_messages_tools_media_and_compaction(tmp_path: Path) -> None:
    session = openhands.parse_session(write_native_session(tmp_path))

    assert session.source_format == AgentFormat.OPENHANDS
    assert session.session_id == SESSION_ID
    assert session.raw_record_count == 6
    assert session.event_counts() == {
        "compaction": 1,
        "context": 1,
        "message": 2,
        "opaque": 4,
        "tool_call": 1,
        "tool_result": 1,
    }
    messages = [item.text for item in session.events if item.kind == EventKind.MESSAGE]
    assert messages == ["OPENHANDS_USER", "OPENHANDS_ASSISTANT"]
    call = next(item for item in session.events if item.kind == EventKind.TOOL_CALL)
    result = next(item for item in session.events if item.kind == EventKind.TOOL_RESULT)
    assert call.payload["input"] == {"command": "pwd"}
    assert (call.tool_call_id, result.tool_call_id, result.text) == (
        "call-openhands-1",
        "call-openhands-1",
        "OPENHANDS_RESULT",
    )


def test_openhands_writer_round_trips_and_materializes_native_files(tmp_path: Path) -> None:
    source = openhands.parse_session(write_native_session(tmp_path))
    target_id = "88888888-8888-4888-8888-888888888888"

    data, dropped = openhands.serialize(
        source,
        session_id=target_id,
        cwd=tmp_path,
        title="Synthetic migrated session",
    )
    parsed = openhands.validate_native_bytes(data, target_id)
    files = openhands.native_files(data, target_id)

    assert parsed.session_id == target_id
    assert parsed.title == "Synthetic migrated session"
    assert len(files) == openhands.native_record_count(data)
    assert files[0][0].startswith("event-00000-")
    assert json.loads(files[0][1])["kind"] == "SystemPromptEvent"
    assert dropped["openhands_system_prompt"] == 1
    assert dropped["openhands_private_thinking"] == 2
    assert dropped["openhands_message_runtime_metadata"] == 1


def test_openhands_writer_preserves_linked_tools_and_user_images(tmp_path: Path) -> None:
    source = Session(
        source_format=AgentFormat.CLAUDE,
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
            Event(
                EventKind.CONTEXT,
                Provenance(0, block_index=1),
                role=Role.USER,
                payload={
                    "block_type": "image",
                    "image_url": "data:image/png;base64,c3ludGhldGlj",
                },
            ),
            Event(
                EventKind.TOOL_CALL,
                Provenance(1),
                role=Role.ASSISTANT,
                tool_name="read",
                tool_call_id="call-1",
                payload={"input": {"path": "a.txt"}},
            ),
            Event(
                EventKind.TOOL_RESULT,
                Provenance(2),
                role=Role.TOOL,
                tool_name="read",
                tool_call_id="call-1",
                text="result",
            ),
        ),
        raw_record_count=3,
    )
    data, dropped = openhands.serialize(source, session_id=SESSION_ID, cwd=tmp_path)
    records = openhands.validate_native_bytes(data, SESSION_ID).events

    call = next(item for item in records if item["kind"] == "ActionEvent")
    result = next(item for item in records if item["kind"] == "ObservationEvent")
    image = next(
        item
        for item in records
        if item["kind"] == "MessageEvent"
        and item["llm_message"]["content"][0]["type"] == "image"
    )
    assert call["tool_call_id"] == result["tool_call_id"] == "call-1"
    assert image["llm_message"]["content"][0]["image_urls"] == [
        "data:image/png;base64,c3ludGhldGlj"
    ]
    assert dropped == {}


@pytest.mark.parametrize("mutation", ["wrong_id", "gap", "bad_role", "unknown_block"])
def test_openhands_source_rejects_malformed_logs(
    tmp_path: Path, mutation: str
) -> None:
    conversation = write_native_session(tmp_path)
    events = conversation / "events"
    if mutation == "gap":
        second = sorted(events.glob("event-*.json"))[1]
        second.rename(events / second.name.replace("00001", "00009"))
    else:
        path = sorted(events.glob("event-*.json"))[1]
        value = json.loads(path.read_text())
        if mutation == "wrong_id":
            value["id"] = "99999999-9999-4999-8999-999999999999"
        elif mutation == "bad_role":
            value["llm_message"]["role"] = "developer"
        else:
            value["llm_message"]["content"][0]["type"] = "audio"
        path.write_text(json.dumps(value))

    with pytest.raises((JsonlError, SessionMigrateError)):
        openhands.parse_session(conversation)


def test_openhands_bundle_rejects_duplicate_members_and_wrong_linkage(tmp_path: Path) -> None:
    source = openhands.parse_session(write_native_session(tmp_path))
    data, _ = openhands.serialize(source, session_id=SESSION_ID, cwd=tmp_path)
    duplicate = data.decode().replace(
        '"schema":"session-migrate.openhands.v1"',
        '"schema":"first","schema":"session-migrate.openhands.v1"',
        1,
    )

    with pytest.raises(SessionMigrateError, match="valid UTF-8 JSON"):
        openhands.validate_native_bytes(duplicate.encode(), SESSION_ID)
    with pytest.raises(SessionMigrateError, match="linkage"):
        openhands.validate_native_bytes(
            data, "99999999-9999-4999-8999-999999999999"
        )
