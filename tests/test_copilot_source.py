import json
from collections import Counter
from pathlib import Path

import pytest

from session_migrate.errors import SessionMigrateError
from session_migrate.formats import copilot
from session_migrate.jsonl import encode_jsonl
from session_migrate.model import AgentFormat, EventKind, Role

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "copilot-source-1.0.70"
EVENTS_FIXTURE = FIXTURE_ROOT / "copilot-source-native-events.jsonl"
WORKSPACE_FIXTURE = FIXTURE_ROOT / "copilot-source-workspace.yaml"
SESSION_ID = "33333333-3333-4333-8333-333333333333"
USER_IMAGE = "data:image/png;base64,c3ludGhldGljLWNvcGlsb3QtdXNlci1pbWFnZQ=="
TOOL_IMAGE = "data:image/png;base64,c3ludGhldGljLWNvcGlsb3QtdG9vbC1pbWFnZQ=="


def fixture_records() -> list[dict[str, object]]:
    return [json.loads(line) for line in EVENTS_FIXTURE.read_text().splitlines()]


def write_records(path: Path, records: list[dict[str, object]]) -> None:
    path.write_bytes(encode_jsonl(records))


def canonical_fixture(tmp_path: Path) -> Path:
    directory = tmp_path / SESSION_ID
    directory.mkdir()
    path = directory / "events.jsonl"
    path.write_bytes(EVENTS_FIXTURE.read_bytes())
    (directory / "workspace.yaml").write_bytes(WORKSPACE_FIXTURE.read_bytes())
    return path


def test_copilot_source_projects_native_messages_tools_media_and_compaction(
    tmp_path: Path,
) -> None:
    path = canonical_fixture(tmp_path)

    session = copilot.parse_session(path)
    by_kind = Counter(event.kind for event in session.events)
    messages = [event for event in session.events if event.kind == EventKind.MESSAGE]
    thinking = [event for event in session.events if event.kind == EventKind.THINKING]
    call = next(event for event in session.events if event.kind == EventKind.TOOL_CALL)
    result = next(event for event in session.events if event.kind == EventKind.TOOL_RESULT)
    images = [
        event.payload["image_url"]
        for event in session.events
        if event.kind == EventKind.CONTEXT and event.payload.get("block_type") == "image"
    ]
    opaque_reasons = {
        event.payload.get("reason")
        for event in session.events
        if event.kind == EventKind.OPAQUE
    }

    assert session.source_format == AgentFormat.COPILOT
    assert session.session_id == SESSION_ID
    assert session.cwd == Path("/synthetic/copilot-source/project")
    assert session.started_at == "2026-08-20T08:52:08.825Z"
    assert session.cli_version == "1.0.70"
    assert session.model == "fixture-model"
    assert session.model_provider == "github-copilot"
    assert session.title == "SYNTHETIC_COPILOT_SOURCE_TITLE"
    assert session.raw_record_count == 16
    assert len(session.source_sha256) == 64
    assert [(event.role, event.text) for event in messages] == [
        (Role.USER, "SYNTHETIC_COPILOT_SOURCE_USER"),
        (Role.ASSISTANT, "SYNTHETIC_COPILOT_SOURCE_ASSISTANT"),
    ]
    assert [event.text for event in thinking] == ["SYNTHETIC_COPILOT_SOURCE_THINKING"]
    assert call.tool_call_id == "call_copilot_source_probe"
    assert call.tool_name == "view"
    assert call.payload == {
        "input": {"path": "fixture.txt"},
        "namespace": "fixture-server",
    }
    assert result.tool_call_id == call.tool_call_id
    assert result.tool_name == "view"
    assert result.text == "SYNTHETIC_COPILOT_SOURCE_TOOL_RESULT"
    assert result.payload["content_blocks"] == [
        {"type": "text", "text": "SYNTHETIC_COPILOT_SOURCE_TOOL_RESULT"},
        {"type": "image", "image_url": TOOL_IMAGE},
    ]
    assert images == [USER_IMAGE]
    assert by_kind[EventKind.COMPACTION] == 1
    assert "copilot_user_transformed_content" in opaque_reasons
    assert "copilot_reasoning_opaque" in opaque_reasons
    assert "copilot_privileged_system_message" in opaque_reasons
    assert "copilot_subagent_scoped_event" in opaque_reasons
    assert "copilot_tool_structured_content" in opaque_reasons
    assert "copilot_tool_detailed_content" in opaque_reasons
    assert "SYNTHETIC_COPILOT_SOURCE_SUBAGENT" not in {
        event.text for event in session.events
    }


def test_copilot_source_uses_workspace_title_when_no_title_event(tmp_path: Path) -> None:
    records = fixture_records()
    title_index = next(i for i, row in enumerate(records) if row["type"] == "session.title_changed")
    removed = records.pop(title_index)
    records[title_index]["parentId"] = removed["parentId"]
    directory = tmp_path / SESSION_ID
    directory.mkdir()
    path = directory / "events.jsonl"
    write_records(path, records)
    (directory / "workspace.yaml").write_bytes(WORKSPACE_FIXTURE.read_bytes())

    assert copilot.parse_session(path).title == "SYNTHETIC_COPILOT_SOURCE_WORKSPACE_TITLE"


def test_copilot_source_can_be_rewritten_with_explicit_losses(tmp_path: Path) -> None:
    source = copilot.parse_session(canonical_fixture(tmp_path))

    data, losses = copilot.serialize(
        source,
        session_id="55555555-5555-4555-8555-555555555555",
        cwd=tmp_path,
        timestamp="2026-08-20T09:00:00Z",
    )
    output = tmp_path / "rewritten.jsonl"
    output.write_bytes(data)
    copilot.validate_native_bytes(data, "55555555-5555-4555-8555-555555555555")
    reparsed = copilot.parse_session(output)

    assert any(event.text == "SYNTHETIC_COPILOT_SOURCE_USER" for event in reparsed.events)
    assert any(event.text == "SYNTHETIC_COPILOT_SOURCE_ASSISTANT" for event in reparsed.events)
    assert any(event.text == "SYNTHETIC_COPILOT_SOURCE_SUMMARY" for event in reparsed.events)
    assert any(event.payload.get("image_url") == USER_IMAGE for event in reparsed.events)
    assert losses["thinking"] == 1
    assert losses["tool_call:namespace"] == 1
    assert losses["opaque:copilot_privileged_system_message"] == 1
    assert losses["opaque:copilot_subagent_scoped_event"] == 1


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda rows: rows[1].update(type="future.unknown"), "unsupported Copilot source"),
        (lambda rows: rows[2].update(parentId=None), "parent chain"),
        (lambda rows: rows[0]["data"].update(version=2), "version is unsupported"),
        (lambda rows: rows[1].update(ephemeral=True), "ephemeral event"),
        (
            lambda rows: rows[7]["data"]["toolRequests"][0].update(arguments="bad"),
            "arguments must be an object",
        ),
        (
            lambda rows: rows[4]["data"].update(byteLength=999),
            "integrity check failed",
        ),
        (
            lambda rows: rows[5]["data"]["attachments"][0].update(byteLength=999),
            "metadata does not match",
        ),
        (
            lambda rows: rows[10]["data"]["result"]["contents"][1].update(data="!!!"),
            "tool content is not base64",
        ),
    ],
)
def test_copilot_source_fails_closed_on_malformed_native_data(
    tmp_path: Path, mutation: object, message: str
) -> None:
    records = fixture_records()
    mutation(records)  # type: ignore[operator]
    path = tmp_path / "copilot-source-malformed.jsonl"
    write_records(path, records)

    with pytest.raises(SessionMigrateError, match=message):
        copilot.parse_session(path)


def test_copilot_source_rejects_structural_depth_bomb(tmp_path: Path) -> None:
    records = fixture_records()
    value: dict[str, object] = {}
    for _ in range(70):
        value = {"next": value}
    records[-1]["data"]["nested"] = value
    path = tmp_path / "copilot-source-deep.jsonl"
    write_records(path, records)

    with pytest.raises(SessionMigrateError, match="structural safety"):
        copilot.parse_session(path)


def test_copilot_source_rejects_directory_identity_mismatch(tmp_path: Path) -> None:
    directory = tmp_path / "77777777-7777-4777-8777-777777777777"
    directory.mkdir()
    path = directory / "events.jsonl"
    path.write_bytes(EVENTS_FIXTURE.read_bytes())

    with pytest.raises(SessionMigrateError, match="directory does not match"):
        copilot.parse_session(path)
