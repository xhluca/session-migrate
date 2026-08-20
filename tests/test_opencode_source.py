import hashlib
import json
from pathlib import Path

import pytest

from session_migrate.errors import SessionMigrateError
from session_migrate.formats import opencode
from session_migrate.model import AgentFormat, EventKind, Role

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "opencode-source-1.17.20"
    / "comprehensive.json"
)


def fixture_value() -> dict[str, object]:
    return json.loads(FIXTURE.read_text())


def write_fixture(tmp_path: Path, value: object) -> Path:
    path = tmp_path / "opencode-source-mutated.json"
    path.write_text(json.dumps(value))
    return path


def test_opencode_source_projects_metadata_and_portable_events() -> None:
    parsed = opencode.parse(FIXTURE)
    session = opencode.parse_session(FIXTURE)

    assert parsed.session_id == "ses_33333333333343338333333333333333"
    assert parsed.cwd == Path("/tmp/session-migrate-opencode-source")
    assert parsed.started_at == "2026-08-18T12:00:00Z"
    assert parsed.title == "SYNTHETIC_OPENCODE_SOURCE_TITLE"
    assert parsed.cli_version == opencode.PINNED_OPENCODE_VERSION
    assert parsed.model == "fixture-model"
    assert parsed.provider == "fixture"
    assert parsed.parent_session == "ses_22222222222242228222222222222222"
    assert parsed.raw_record_count == 18

    assert session.source_format == AgentFormat.OPENCODE
    assert session.source_path == FIXTURE.resolve()
    assert session.source_sha256 == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert session.model == "fixture-model"
    assert session.model_provider == "fixture"
    assert session.event_counts() == {
        "compaction": 1,
        "context": 1,
        "message": 3,
        "opaque": 15,
        "thinking": 1,
        "tool_call": 1,
        "tool_result": 1,
    }

    messages = [event for event in session.events if event.kind == EventKind.MESSAGE]
    assert [(event.role, event.text) for event in messages] == [
        (Role.SYSTEM, "SYNTHETIC_SYSTEM_MARKER"),
        (Role.USER, "SYNTHETIC_OPENCODE_USER_MARKER"),
        (Role.ASSISTANT, "SYNTHETIC_OPENCODE_ASSISTANT_MARKER"),
    ]
    image = next(event for event in session.events if event.kind == EventKind.CONTEXT)
    assert image.payload == {
        "block_type": "image",
        "image_url": "data:image/png;base64,c3ludGhldGlj",
        "mime_type": "image/png",
    }
    thinking = next(event for event in session.events if event.kind == EventKind.THINKING)
    assert thinking.text == "SYNTHETIC_REASONING_MARKER"

    call = next(event for event in session.events if event.kind == EventKind.TOOL_CALL)
    result = next(event for event in session.events if event.kind == EventKind.TOOL_RESULT)
    assert (call.tool_name, call.tool_call_id, call.payload["input"]) == (
        "read",
        "synthetic_opencode_call_1",
        {"path": "synthetic.txt"},
    )
    assert result.text == "SYNTHETIC_OPENCODE_TOOL_RESULT"
    assert result.payload["content_blocks"] == [
        {"type": "text", "text": "SYNTHETIC_OPENCODE_TOOL_RESULT"},
        {"type": "image", "image_url": "data:image/png;base64,dG9vbC1pbWFnZQ=="},
    ]
    compaction = next(
        event for event in session.events if event.kind == EventKind.COMPACTION
    )
    assert compaction.text == "SYNTHETIC_OPENCODE_COMPACTION_SUMMARY"
    assert compaction.payload == {
        "source_subtype": "opencode_compaction_summary",
        "has_boundary_metadata": True,
    }


def test_opencode_source_accounts_for_every_nonportable_fixture_feature() -> None:
    parsed = opencode.parse(FIXTURE)

    assert dict(parsed.losses) == {
        "opencode_file_source_metadata": 1,
        "opencode_ignored_text": 1,
        "opencode_nonportable_file": 1,
        "opencode_parent_session": 1,
        "opencode_patch_part": 1,
        "opencode_session_metadata": 1,
        "opencode_session_summary": 1,
        "opencode_snapshot_part": 1,
        "opencode_step-finish_part": 1,
        "opencode_step-start_part": 1,
        "opencode_tool_metadata": 1,
        "opencode_tool_result_compacted": 1,
        "opencode_user_output_format": 1,
        "opencode_user_summary_metadata": 1,
        "opencode_user_tool_policy": 1,
    }
    assert all(
        event.payload.get("reason") != "opencode_ignored_text"
        or event.text != "SYNTHETIC_IGNORED_MARKER"
        for event in parsed.events
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate_message_id",
        "cross_session_part",
        "unknown_part_type",
        "malformed_tool_state",
        "backward_tool_time",
        "invalid_user_model",
        "boolean_timestamp",
    ],
)
def test_opencode_source_rejects_malformed_official_shapes(
    tmp_path: Path, mutation: str
) -> None:
    value = fixture_value()
    messages = value["messages"]
    assert isinstance(messages, list)
    first = messages[0]
    assistant = messages[1]
    assert isinstance(first, dict) and isinstance(assistant, dict)
    if mutation == "duplicate_message_id":
        assistant["info"]["id"] = first["info"]["id"]
    elif mutation == "cross_session_part":
        first["parts"][0]["sessionID"] = "ses_wrong"
    elif mutation == "unknown_part_type":
        first["parts"][0]["type"] = "future-part"
    elif mutation == "malformed_tool_state":
        assistant["parts"][3]["state"]["input"] = []
    elif mutation == "backward_tool_time":
        assistant["parts"][3]["state"]["time"] = {"start": 2, "end": 1}
    elif mutation == "invalid_user_model":
        first["info"]["model"] = {"providerID": "fixture"}
    elif mutation == "boolean_timestamp":
        first["info"]["time"]["created"] = True

    with pytest.raises(SessionMigrateError):
        opencode.parse(write_fixture(tmp_path, value))


def test_opencode_source_rejects_duplicate_json_members(tmp_path: Path) -> None:
    text = FIXTURE.read_text().replace(
        '"slug": "synthetic-opencode-source",',
        '"slug": "first", "slug": "second",',
        1,
    )
    path = tmp_path / "opencode-source-duplicate-key.json"
    path.write_text(text)

    with pytest.raises(SessionMigrateError, match="valid UTF-8 JSON"):
        opencode.parse(path)


def test_opencode_source_rejects_excessive_json_nesting(tmp_path: Path) -> None:
    value = fixture_value()
    nested: object = "leaf"
    for _ in range(opencode.MAX_JSON_DEPTH + 1):
        nested = [nested]
    value["info"]["metadata"] = {"nested": nested}

    with pytest.raises(SessionMigrateError, match="valid UTF-8 JSON"):
        opencode.parse(write_fixture(tmp_path, value))


def test_opencode_source_enforces_message_and_part_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(opencode, "MAX_NATIVE_MESSAGES", 3)
    with pytest.raises(SessionMigrateError, match="too many messages"):
        opencode.parse(FIXTURE)

    monkeypatch.setattr(opencode, "MAX_NATIVE_MESSAGES", 10)
    monkeypatch.setattr(opencode, "MAX_NATIVE_PARTS", 2)
    with pytest.raises(SessionMigrateError, match="too many parts"):
        opencode.parse(FIXTURE)
