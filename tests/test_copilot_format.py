import json
from dataclasses import replace
from pathlib import Path

import pytest

from session_bridge.errors import SessionBridgeError
from session_bridge.formats import copilot
from session_bridge.model import AgentFormat, Event, EventKind, Provenance, Role, Session

TARGET_ID = "22222222-2222-4222-8222-222222222222"
IMAGE_URL = "data:image/png;base64,c3ludGhldGlj"
TOOL_IMAGE_URL = "data:image/png;base64,dG9vbC1pbWFnZQ=="


def source_session(tmp_path: Path) -> Session:
    return Session(
        source_format=AgentFormat.CLAUDE,
        source_path=tmp_path / "source.jsonl",
        source_sha256="0" * 64,
        session_id="11111111-1111-4111-8111-111111111111",
        cwd=tmp_path,
        started_at="2026-08-18T12:00:00Z",
        cli_version="2.1.209",
        model="fixture-model",
        title="SYNTHETIC_IMPORT",
        events=(
            Event(
                kind=EventKind.MESSAGE,
                role=Role.USER,
                text="SYNTHETIC_USER",
                timestamp="2026-08-18T12:00:00Z",
                provenance=Provenance(0, "user", block_index=0),
            ),
            Event(
                kind=EventKind.CONTEXT,
                role=Role.USER,
                timestamp="2026-08-18T12:00:00Z",
                payload={"block_type": "image", "image_url": IMAGE_URL},
                provenance=Provenance(0, "user", block_index=1),
            ),
            Event(
                kind=EventKind.MESSAGE,
                role=Role.ASSISTANT,
                text="SYNTHETIC_ASSISTANT",
                timestamp="2026-08-18T12:00:01Z",
                provenance=Provenance(1, "assistant", block_index=0),
            ),
            Event(
                kind=EventKind.TOOL_CALL,
                role=Role.ASSISTANT,
                tool_name="read",
                tool_call_id="synthetic_call_1",
                timestamp="2026-08-18T12:00:01Z",
                payload={"input": {"path": "fixture.txt"}},
                provenance=Provenance(1, "assistant", block_index=1),
            ),
            Event(
                kind=EventKind.TOOL_RESULT,
                role=Role.TOOL,
                text="SYNTHETIC_RESULT",
                tool_name="read",
                tool_call_id="synthetic_call_1",
                timestamp="2026-08-18T12:00:02Z",
                payload={
                    "content_blocks": [
                        {"type": "text", "text": "SYNTHETIC_RESULT"},
                        {"type": "image", "image_url": TOOL_IMAGE_URL},
                    ]
                },
                provenance=Provenance(2, "tool_result"),
            ),
            Event(
                kind=EventKind.COMPACTION,
                role=Role.SYSTEM,
                text="SYNTHETIC_SUMMARY",
                timestamp="2026-08-18T12:00:03Z",
                provenance=Provenance(3, "compaction"),
            ),
            Event(
                kind=EventKind.MESSAGE,
                role=Role.ASSISTANT,
                text="SYNTHETIC_FINAL",
                timestamp="2026-08-18T12:00:04Z",
                provenance=Provenance(4, "assistant"),
            ),
        ),
        raw_record_count=5,
    )


def signature(events: tuple[Event, ...]) -> list[tuple[object, ...]]:
    result = []
    for event in events:
        payload = None
        if event.kind == EventKind.TOOL_CALL:
            payload = event.payload.get("input")
        elif event.kind == EventKind.TOOL_RESULT:
            payload = event.payload.get("content_blocks")
        elif event.kind == EventKind.CONTEXT:
            payload = {
                "block_type": event.payload.get("block_type"),
                "image_url": event.payload.get("image_url"),
            }
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


def test_copilot_writer_parser_round_trip(tmp_path: Path) -> None:
    source = source_session(tmp_path)
    data, dropped = copilot.serialize(
        source,
        session_id=TARGET_ID,
        cwd=tmp_path,
        timestamp="2026-08-18T12:00:00Z",
    )
    path = tmp_path / "events.jsonl"
    path.write_bytes(data)

    copilot.validate_native_bytes(data, TARGET_ID)
    parsed = copilot.parse(path)
    records = [json.loads(line) for line in data.splitlines()]
    assets = {
        record["data"]["assetId"]: record["data"]
        for record in records
        if record["type"] == "session.binary_asset"
    }
    user_attachment = next(
        record["data"]["attachments"][0]
        for record in records
        if record["type"] == "user.message" and record["data"].get("attachments")
    )
    tool_reference = next(
        record["data"]["result"]["binaryResultsForLlm"][0]
        for record in records
        if record["type"] == "tool.execution_complete"
    )

    assert dropped == {"tool_result:image_provider_dependent": 1}
    assert len(assets) == 2
    assert user_attachment["assetId"] in assets
    assert tool_reference["assetId"] in assets
    assert "data" not in user_attachment
    assert "data" not in tool_reference
    assert parsed.session_id == TARGET_ID
    assert parsed.cwd == tmp_path
    assert signature(parsed.events) == signature(source.events)


def test_copilot_jsonl_validator_preserves_unicode_line_separators(tmp_path: Path) -> None:
    base = source_session(tmp_path)
    marker = "SYNTHETIC\u2028LINE\u2029SEPARATORS"
    source = replace(base, events=(replace(base.events[0], text=marker),))

    data, _ = copilot.serialize(source, session_id=TARGET_ID, cwd=tmp_path)
    path = tmp_path / "unicode-separators.jsonl"
    path.write_bytes(data)

    copilot.validate_native_bytes(data, TARGET_ID)
    assert copilot.parse(path).events[0].text == marker


def test_copilot_groups_text_blocks_around_an_image_with_warning(tmp_path: Path) -> None:
    source = source_session(tmp_path)
    fragments = (
        Event(
            kind=EventKind.MESSAGE,
            role=Role.USER,
            text="SYNTHETIC_AFTER_IMAGE_ONE",
            timestamp="2026-08-18T12:00:00Z",
            provenance=Provenance(0, "user", block_index=2),
        ),
        Event(
            kind=EventKind.MESSAGE,
            role=Role.USER,
            text="SYNTHETIC_AFTER_IMAGE_TWO",
            timestamp="2026-08-18T12:00:00Z",
            provenance=Provenance(0, "user", block_index=3),
        ),
    )
    grouped = replace(source, events=source.events[:2] + fragments + source.events[2:])

    data, dropped = copilot.serialize(grouped, session_id=TARGET_ID, cwd=tmp_path)
    path = tmp_path / "grouped.jsonl"
    path.write_bytes(data)
    parsed = copilot.parse(path)

    assert parsed.events[0].text == (
        "SYNTHETIC_USER\nSYNTHETIC_AFTER_IMAGE_ONE\nSYNTHETIC_AFTER_IMAGE_TWO"
    )
    assert parsed.events[1].kind == EventKind.CONTEXT
    assert dropped == {
        "message:native_text_blocks_grouped": 2,
        "tool_result:image_provider_dependent": 1,
    }


def test_copilot_timestamps_are_made_nondecreasing(tmp_path: Path) -> None:
    source = source_session(tmp_path)
    events = list(source.events)
    events[-1] = replace(events[-1], timestamp="2026-08-18T11:00:00Z")

    data, dropped = copilot.serialize(
        replace(source, events=tuple(events)),
        session_id=TARGET_ID,
        cwd=tmp_path,
    )
    records = [json.loads(line) for line in data.splitlines()]

    assert dropped == {
        "timestamp:native_order_adjusted": 1,
        "tool_result:image_provider_dependent": 1,
    }
    assert [record["timestamp"] for record in records] == sorted(
        record["timestamp"] for record in records
    )


def test_copilot_writer_reports_omissions_and_malformed_blocks(tmp_path: Path) -> None:
    source = source_session(tmp_path)
    extra = (
        Event(
            kind=EventKind.MESSAGE,
            role=Role.SYSTEM,
            text="privileged",
            provenance=Provenance(5, "system"),
        ),
        Event(
            kind=EventKind.THINKING,
            role=Role.ASSISTANT,
            text="private",
            provenance=Provenance(6, "thinking"),
        ),
        Event(
            kind=EventKind.TOOL_RESULT,
            role=Role.TOOL,
            tool_call_id="orphan",
            text="fallback",
            payload={"content_blocks": [7, {"type": "image", "image_url": "bad"}]},
            provenance=Provenance(7, "tool_result"),
        ),
    )

    data, dropped = copilot.serialize(
        replace(source, events=source.events + extra),
        session_id=TARGET_ID,
        cwd=tmp_path,
    )

    copilot.validate_native_bytes(data, TARGET_ID)
    assert dropped == {
        "message:privileged_role": 1,
        "thinking": 1,
        "timestamp:native_order_adjusted": 3,
        "tool_result:image": 1,
        "tool_result:image_provider_dependent": 1,
        "tool_result:malformed_block": 1,
        "tool_result:orphan_id": 1,
    }


def test_copilot_rejects_broken_parent_chain(tmp_path: Path) -> None:
    data, _ = copilot.serialize(source_session(tmp_path), session_id=TARGET_ID, cwd=tmp_path)
    records = [json.loads(line) for line in data.splitlines()]
    records[1]["parentId"] = None
    broken = b"\n".join(json.dumps(record).encode() for record in records) + b"\n"

    with pytest.raises(SessionBridgeError, match="parent chain"):
        copilot.validate_native_bytes(broken, TARGET_ID)


def test_copilot_preserves_resumable_interrupted_user_turn(tmp_path: Path) -> None:
    source = source_session(tmp_path)
    interrupted = replace(source, events=(source.events[0], source.events[1]))

    data, dropped = copilot.serialize(interrupted, session_id=TARGET_ID, cwd=tmp_path)
    copilot.validate_native_bytes(data, TARGET_ID)
    path = tmp_path / "interrupted.jsonl"
    path.write_bytes(data)

    parsed = copilot.parse(path)
    assert dropped == {}
    assert signature(parsed.events) == signature(interrupted.events)


def test_copilot_workspace_sidecar_is_private_writer_input(tmp_path: Path) -> None:
    data = copilot.workspace_bytes(
        session_id=TARGET_ID,
        cwd=tmp_path,
        timestamp="2026-08-18T12:00:00Z",
        title='quoted: "name"',
    )

    assert b'id: "22222222-2222-4222-8222-222222222222"' in data
    assert b'name: "quoted: \\"name\\""' in data
    assert b"user_named: true" in data
