import json
from dataclasses import replace
from pathlib import Path

import pytest

from session_migrate.errors import SessionMigrateError
from session_migrate.formats import opencode, pi
from session_migrate.model import AgentFormat, Event, EventKind, Provenance, Role, Session

FIXTURES = Path(__file__).parent / "fixtures"
TARGET_UUID = "22222222-2222-4222-8222-222222222222"
TARGET_OPENCODE_ID = "ses_22222222222242228222222222222222"
IMAGE_URL = "data:image/png;base64,c3ludGhldGlj"
TOOL_IMAGE_URL = "data:image/png;base64,dG9vbC1pbWFnZQ=="


def event_signature(events: tuple[Event, ...]) -> list[tuple[object, ...]]:
    """Return the portable fields that both native targets can preserve."""

    result: list[tuple[object, ...]] = []
    for event in events:
        payload: object = None
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


def portable_session(tmp_path: Path, *, compaction: bool = False) -> Session:
    events = [
        Event(
            kind=EventKind.MESSAGE,
            role=Role.USER,
            text="SYNTHETIC_USER_MARKER",
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
            text="SYNTHETIC_ASSISTANT_MARKER",
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
            text="SYNTHETIC_TOOL_RESULT",
            tool_name="read",
            tool_call_id="synthetic_call_1",
            timestamp="2026-08-18T12:00:02Z",
            payload={
                "is_error": False,
                "content_blocks": [
                    {"type": "text", "text": "SYNTHETIC_TOOL_RESULT"},
                    {"type": "image", "image_url": TOOL_IMAGE_URL},
                ],
            },
            provenance=Provenance(2, "user", block_index=0),
        ),
    ]
    if compaction:
        events.append(
            Event(
                kind=EventKind.COMPACTION,
                role=Role.SYSTEM,
                text="SYNTHETIC_COMPACTION_MARKER",
                timestamp="2026-08-18T12:00:03Z",
                provenance=Provenance(3, "compact"),
            )
        )
    events.append(
        Event(
            kind=EventKind.MESSAGE,
            role=Role.ASSISTANT,
            text="SYNTHETIC_FINAL_MARKER",
            timestamp="2026-08-18T12:00:04Z",
            provenance=Provenance(4, "assistant", block_index=0),
        )
    )
    return Session(
        source_format=AgentFormat.CLAUDE,
        source_path=tmp_path / "synthetic-source.jsonl",
        source_sha256="0" * 64,
        session_id="11111111-1111-4111-8111-111111111111",
        cwd=tmp_path,
        started_at="2026-08-18T12:00:00Z",
        cli_version="2.1.209",
        model="fixture-model",
        title="SYNTHETIC_IMPORTED_NAME",
        events=tuple(events),
        raw_record_count=5,
    )


@pytest.mark.parametrize(
    ("parser", "path", "expected_id", "expected_records"),
    [
        (
            pi.parse,
            FIXTURES / "pi-0.80.6" / "basic.jsonl",
            "11111111-1111-4111-8111-111111111111",
            6,
        ),
        (
            opencode.parse,
            FIXTURES / "opencode-1.17.20" / "basic.json",
            "ses_11111111111141118111111111111111",
            8,
        ),
    ],
)
def test_sanitized_native_fixture_projection(
    parser: object, path: Path, expected_id: str, expected_records: int
) -> None:
    parsed = parser(path)  # type: ignore[operator]

    assert parsed.session_id == expected_id
    assert parsed.raw_record_count == expected_records
    assert [event.kind for event in parsed.events] == [
        EventKind.MESSAGE,
        EventKind.CONTEXT,
        EventKind.MESSAGE,
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
        EventKind.MESSAGE,
    ]
    assert parsed.events[0].text == "SYNTHETIC_USER_MARKER"
    assert parsed.events[1].payload["image_url"] == IMAGE_URL
    assert parsed.events[3].tool_call_id == "synthetic_call_1"
    assert parsed.events[4].payload["content_blocks"][1]["image_url"] == IMAGE_URL
    assert parsed.events[5].text == "SYNTHETIC_FINAL_MARKER"


@pytest.mark.parametrize(
    ("target", "target_id"), [(pi, TARGET_UUID), (opencode, TARGET_OPENCODE_ID)]
)
def test_writer_parser_round_trip_preserves_portable_semantics(
    tmp_path: Path, target: object, target_id: str
) -> None:
    source = portable_session(tmp_path)
    data, dropped = target.serialize(  # type: ignore[attr-defined]
        source,
        session_id=target_id,
        cwd=tmp_path,
        timestamp="2026-08-18T12:00:00Z",
    )
    extension = "jsonl" if target is pi else "json"
    path = tmp_path / f"converted.{extension}"
    path.write_bytes(data)

    target.validate_native_bytes(data, target_id)  # type: ignore[attr-defined]
    parsed = target.parse(path)  # type: ignore[attr-defined]

    assert dropped == {}
    assert parsed.session_id == target_id
    assert event_signature(parsed.events) == event_signature(source.events)


@pytest.mark.parametrize(
    ("target", "target_id"), [(pi, TARGET_UUID), (opencode, TARGET_OPENCODE_ID)]
)
def test_compaction_replaces_pre_summary_context(
    tmp_path: Path, target: object, target_id: str
) -> None:
    source = portable_session(tmp_path, compaction=True)
    data, dropped = target.serialize(  # type: ignore[attr-defined]
        source,
        session_id=target_id,
        cwd=tmp_path,
        timestamp="2026-08-18T12:00:00Z",
    )
    path = tmp_path / ("compacted.jsonl" if target is pi else "compacted.json")
    path.write_bytes(data)

    parsed = target.parse(path)  # type: ignore[attr-defined]

    assert dropped == {}
    assert event_signature(parsed.events) == event_signature(source.events)


@pytest.mark.parametrize(
    ("target", "target_id"), [(pi, TARGET_UUID), (opencode, TARGET_OPENCODE_ID)]
)
def test_writer_reports_every_intentional_omission(
    tmp_path: Path, target: object, target_id: str
) -> None:
    events = (
        Event(
            kind=EventKind.MESSAGE,
            role=Role.SYSTEM,
            text="system",
            provenance=Provenance(0, "system"),
        ),
        Event(
            kind=EventKind.MESSAGE,
            role=Role.USER,
            text="invalid time",
            timestamp="not-a-time",
            provenance=Provenance(1, "user"),
        ),
        Event(
            kind=EventKind.CONTEXT,
            role=Role.USER,
            payload={"block_type": "image", "image_url": "https://example.invalid/image.png"},
            provenance=Provenance(1, "user", block_index=1),
        ),
        Event(
            kind=EventKind.CONTEXT,
            role=Role.ASSISTANT,
            payload={"block_type": "image", "image_url": IMAGE_URL},
            provenance=Provenance(2, "assistant"),
        ),
        Event(
            kind=EventKind.THINKING,
            role=Role.ASSISTANT,
            text="private chain of thought",
            provenance=Provenance(3, "assistant"),
        ),
        Event(
            kind=EventKind.OPAQUE,
            payload={"reason": "synthetic_unknown"},
            provenance=Provenance(4, "unknown"),
        ),
        Event(
            kind=EventKind.TOOL_CALL,
            role=Role.ASSISTANT,
            tool_name="read",
            tool_call_id="call_1",
            payload={"input": {}, "namespace": "synthetic"},
            provenance=Provenance(5, "assistant"),
        ),
    )
    base = portable_session(tmp_path)
    source = Session(
        source_format=base.source_format,
        source_path=base.source_path,
        source_sha256=base.source_sha256,
        session_id=base.session_id,
        cwd=base.cwd,
        started_at=base.started_at,
        cli_version=base.cli_version,
        model=base.model,
        title=base.title,
        events=events,
        raw_record_count=len(events),
    )

    _, dropped = target.serialize(  # type: ignore[attr-defined]
        source,
        session_id=target_id,
        cwd=tmp_path,
        timestamp="2026-08-18T12:00:00Z",
    )

    assert dropped == {
        "context:image": 1,
        "context:privileged_image": 1,
        "message:privileged_role": 1,
        "opaque:synthetic_unknown": 1,
        "thinking": 1,
        "timestamp:invalid": 1,
        "tool_call:namespace": 1,
    }


@pytest.mark.parametrize(
    ("target", "target_id"), [(pi, TARGET_UUID), (opencode, TARGET_OPENCODE_ID)]
)
def test_writers_reject_invalid_base64_user_and_tool_result_images(
    tmp_path: Path, target: object, target_id: str
) -> None:
    base = portable_session(tmp_path)
    events = tuple(
        replace(
            event,
            payload={"block_type": "image", "image_url": "data:image/png;base64,%%%="},
        )
        if event.kind == EventKind.CONTEXT
        else replace(
            event,
            payload={
                **event.payload,
                "content_blocks": [
                    {"type": "text", "text": "SYNTHETIC_TOOL_RESULT"},
                    {"type": "image", "image_url": "data:image/png;base64,abc"},
                ],
            },
        )
        if event.kind == EventKind.TOOL_RESULT
        else event
        for event in base.events
    )
    source = replace(base, events=events)

    data, dropped = target.serialize(  # type: ignore[attr-defined]
        source,
        session_id=target_id,
        cwd=tmp_path,
    )
    path = tmp_path / ("invalid-images.jsonl" if target is pi else "invalid-images.json")
    path.write_bytes(data)
    parsed = target.parse(path)  # type: ignore[attr-defined]

    assert dropped == {"context:image": 1, "tool_result:image": 1}
    assert all(event.kind != EventKind.CONTEXT for event in parsed.events)
    result = next(event for event in parsed.events if event.kind == EventKind.TOOL_RESULT)
    assert result.payload["content_blocks"] == [{"type": "text", "text": "SYNTHETIC_TOOL_RESULT"}]


def test_pi_rejects_bad_header_duplicate_ids_and_missing_parent(tmp_path: Path) -> None:
    bad_header = tmp_path / "bad-header.jsonl"
    bad_header.write_text('{"type":"session","version":2}\n', encoding="utf-8")
    with pytest.raises(SessionMigrateError, match="v3 header"):
        pi.parse(bad_header)

    duplicate = tmp_path / "duplicate.jsonl"
    duplicate.write_text(
        "\n".join(
            [
                '{"type":"session","version":3,"id":"s","timestamp":"2026-08-18T12:00:00Z","cwd":"/tmp"}',
                '{"type":"message","id":"same","parentId":null,"timestamp":"2026-08-18T12:00:00Z","message":{"role":"user","content":"one"}}',
                '{"type":"message","id":"same","parentId":"same","timestamp":"2026-08-18T12:00:00Z","message":{"role":"user","content":"two"}}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(SessionMigrateError, match="duplicate entry id"):
        pi.parse(duplicate)

    missing_parent = tmp_path / "missing-parent.jsonl"
    missing_parent.write_text(
        "\n".join(
            [
                '{"type":"session","version":3,"id":"s","timestamp":"2026-08-18T12:00:00Z","cwd":"/tmp"}',
                '{"type":"message","id":"child","parentId":"missing","timestamp":"2026-08-18T12:00:00Z","message":{"role":"user","content":"one"}}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(SessionMigrateError, match="missing parent"):
        pi.parse(missing_parent)


def test_pi_jsonl_validator_preserves_unicode_line_separators(tmp_path: Path) -> None:
    base = portable_session(tmp_path)
    marker = "SYNTHETIC\u2028LINE\u2029SEPARATORS"
    source = replace(base, events=(replace(base.events[0], text=marker),))

    data, _ = pi.serialize(source, session_id=TARGET_UUID, cwd=tmp_path)
    path = tmp_path / "unicode-separators.jsonl"
    path.write_bytes(data)

    pi.validate_native_bytes(data, TARGET_UUID)
    assert pi.parse(path).events[0].text == marker


def test_pi_fixture_is_an_authoritative_source_session() -> None:
    source = pi.parse_session(FIXTURES / "pi-0.80.6" / "basic.jsonl")

    assert source.source_format == AgentFormat.PI
    assert source.session_id == "11111111-1111-4111-8111-111111111111"
    assert source.title == "SYNTHETIC_IMPORTED_NAME"
    assert source.model == "fixture-model"
    assert source.model_provider == "anthropic"
    assert source.event_counts() == {
        "context": 1,
        "message": 3,
        "tool_call": 1,
        "tool_result": 1,
    }


def test_pi_source_accounts_for_unknown_tool_result_blocks(tmp_path: Path) -> None:
    path = tmp_path / "unknown-result-blocks.jsonl"
    records = [
        {
            "type": "session",
            "version": 3,
            "id": TARGET_UUID,
            "timestamp": "2026-08-18T12:00:00Z",
            "cwd": "/tmp",
        },
        {
            "type": "message",
            "id": "00000001",
            "parentId": None,
            "timestamp": "2026-08-18T12:00:01Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "synthetic-call",
                        "name": "read",
                        "arguments": {},
                    }
                ],
                "provider": "openai",
                "model": "fixture",
                "stopReason": "toolUse",
            },
        },
        {
            "type": "message",
            "id": "00000002",
            "parentId": "00000001",
            "timestamp": "2026-08-18T12:00:02Z",
            "message": {
                "role": "toolResult",
                "toolCallId": "synthetic-call",
                "toolName": "read",
                "content": [7, {"type": "future_result", "value": "opaque"}],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")

    source = pi.parse_session(path)
    result = next(event for event in source.events if event.kind == EventKind.TOOL_RESULT)
    assert result.payload["content_blocks"] == [
        {"type": "opaque", "source_type": "<non-object>"},
        {"type": "opaque", "source_type": "future_result"},
    ]

    _data, dropped = pi.serialize(source, session_id=TARGET_UUID, cwd=tmp_path)
    assert dropped == {"tool_result:opaque": 2}


def test_pi_source_selects_last_leaf_and_accounts_for_inactive_branch(tmp_path: Path) -> None:
    path = tmp_path / "branched.jsonl"
    records = [
        {
            "type": "session",
            "version": 3,
            "id": "11111111-1111-4111-8111-111111111111",
            "timestamp": "2026-08-18T12:00:00Z",
            "cwd": "/tmp",
        },
        {
            "type": "message",
            "id": "00000001",
            "parentId": None,
            "timestamp": "2026-08-18T12:00:00Z",
            "message": {"role": "user", "content": "root"},
        },
        {
            "type": "message",
            "id": "00000002",
            "parentId": "00000001",
            "timestamp": "2026-08-18T12:00:01Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "answer"}],
                "provider": "openai",
                "model": "fixture",
                "stopReason": "stop",
            },
        },
        {
            "type": "message",
            "id": "00000003",
            "parentId": "00000002",
            "timestamp": "2026-08-18T12:00:02Z",
            "message": {"role": "user", "content": "abandoned"},
        },
        {
            "type": "branch_summary",
            "id": "00000004",
            "parentId": "00000002",
            "timestamp": "2026-08-18T12:00:03Z",
            "fromId": "00000003",
            "summary": "branch summary",
        },
        {
            "type": "message",
            "id": "00000005",
            "parentId": "00000004",
            "timestamp": "2026-08-18T12:00:04Z",
            "message": {"role": "user", "content": "active"},
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")

    source = pi.parse_session(path)

    visible = [event.text for event in source.events if event.kind == EventKind.MESSAGE]
    reasons = [
        event.payload.get("reason") for event in source.events if event.kind == EventKind.OPAQUE
    ]
    assert visible == ["root", "answer", "active"]
    assert reasons == ["pi_branch_summary", "inactive_pi_branch_entry"]


def test_pi_source_accounts_for_parent_lineage_and_rejects_malformed_parent(
    tmp_path: Path,
) -> None:
    value = [
        {
            "type": "session",
            "version": 3,
            "id": TARGET_UUID,
            "parentSession": "11111111-1111-4111-8111-111111111111",
            "timestamp": "2026-08-18T12:00:00Z",
            "cwd": "/tmp",
        },
        {
            "type": "message",
            "id": "00000001",
            "parentId": None,
            "timestamp": "2026-08-18T12:00:01Z",
            "message": {"role": "user", "content": "synthetic"},
        },
    ]
    path = tmp_path / "parent.jsonl"
    path.write_text("\n".join(json.dumps(record) for record in value) + "\n")

    source = pi.parse_session(path)
    assert source.events[0].kind == EventKind.OPAQUE
    assert source.events[0].payload == {"reason": "pi_parent_session"}

    value[0]["parentSession"] = {"invalid": True}
    path.write_text("\n".join(json.dumps(record) for record in value) + "\n")
    with pytest.raises(SessionMigrateError, match="invalid parent metadata"):
        pi.parse_session(path)


def test_opencode_rejects_invalid_json_metadata_and_message_time(tmp_path: Path) -> None:
    invalid_json = tmp_path / "invalid.json"
    invalid_json.write_text("{", encoding="utf-8")
    with pytest.raises(SessionMigrateError, match="valid UTF-8 JSON"):
        opencode.parse(invalid_json)

    invalid_metadata = tmp_path / "metadata.json"
    invalid_metadata.write_text('{"info":{},"messages":[]}', encoding="utf-8")
    with pytest.raises(SessionMigrateError, match="required metadata"):
        opencode.parse(invalid_metadata)

    value = json.loads((FIXTURES / "opencode-1.17.20" / "basic.json").read_text())
    value["messages"][0]["info"]["time"]["created"] = "yesterday"
    invalid_time = tmp_path / "time.json"
    invalid_time.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(SessionMigrateError, match="invalid timestamp"):
        opencode.parse(invalid_time)


def test_target_specific_id_and_path_helpers(tmp_path: Path) -> None:
    assert opencode.session_id_from_uuid(TARGET_UUID) == TARGET_OPENCODE_ID
    with pytest.raises(SessionMigrateError, match="not a valid UUID"):
        opencode.session_id_from_uuid("not-a-uuid")
    assert (
        pi.session_relative_path(
            tmp_path,
            TARGET_UUID,
            "2026-08-18T12:00:00Z",
        )
        .as_posix()
        .startswith("sessions/--")
    )


def test_byte_validators_reject_target_id_mismatch(tmp_path: Path) -> None:
    source = portable_session(tmp_path)
    pi_data, _ = pi.serialize(source, session_id=TARGET_UUID, cwd=tmp_path)
    opencode_data, _ = opencode.serialize(source, session_id=TARGET_OPENCODE_ID, cwd=tmp_path)

    with pytest.raises(SessionMigrateError, match="does not match"):
        pi.validate_native_bytes(pi_data, "33333333-3333-4333-8333-333333333333")
    with pytest.raises(SessionMigrateError, match="does not match"):
        opencode.validate_native_bytes(opencode_data, "ses_mismatch")


def test_opencode_writer_uses_native_ascending_message_ids(tmp_path: Path) -> None:
    data, _ = opencode.serialize(
        portable_session(tmp_path),
        session_id=TARGET_OPENCODE_ID,
        cwd=tmp_path,
    )
    value = json.loads(data)
    message_ids = [message["info"]["id"] for message in value["messages"]]

    assert message_ids == sorted(message_ids)
    assert all(
        len(message_id) == 30 and message_id.startswith("msg_") for message_id in message_ids
    )


def test_opencode_native_ids_remain_ascending_past_same_millisecond_counter_range(
    tmp_path: Path,
) -> None:
    base = portable_session(tmp_path)
    events = tuple(
        Event(
            kind=EventKind.MESSAGE,
            role=Role.USER,
            text=f"synthetic-{index}",
            timestamp=("2026-08-18T12:00:00.001Z" if index == 2050 else "2026-08-18T12:00:00Z"),
            provenance=Provenance(index, "user"),
        )
        for index in range(2051)
    )
    source = replace(base, events=events, raw_record_count=len(events))

    data, _ = opencode.serialize(
        source,
        session_id=TARGET_OPENCODE_ID,
        cwd=tmp_path,
    )
    value = json.loads(data)
    native_time_fields = [
        int(native_id[4:16], 16)
        for message in value["messages"]
        for native_id in (
            message["info"]["id"],
            *(part["id"] for part in message["parts"]),
        )
    ]

    opencode.validate_native_bytes(data, TARGET_OPENCODE_ID)
    assert native_time_fields == sorted(native_time_fields)
    assert len(set(native_time_fields)) == len(native_time_fields)
    assert all(encoded < 1 << 48 for encoded in native_time_fields)


def test_opencode_writer_makes_native_message_times_monotonic(
    tmp_path: Path,
) -> None:
    base = portable_session(tmp_path)
    source = replace(
        base,
        events=(
            Event(
                kind=EventKind.MESSAGE,
                role=Role.USER,
                text="SYNTHETIC_FIRST",
                timestamp="2026-08-18T12:00:02Z",
                provenance=Provenance(0, "user"),
            ),
            Event(
                kind=EventKind.MESSAGE,
                role=Role.ASSISTANT,
                text="SYNTHETIC_SECOND",
                timestamp="2026-08-18T12:00:01Z",
                provenance=Provenance(1, "assistant"),
            ),
        ),
        raw_record_count=2,
    )

    data, dropped = opencode.serialize(
        source,
        session_id=TARGET_OPENCODE_ID,
        cwd=tmp_path,
    )
    value = json.loads(data)
    created = [message["info"]["time"]["created"] for message in value["messages"]]

    opencode.validate_native_bytes(data, TARGET_OPENCODE_ID)
    assert created == sorted(created)
    assert dropped == {"timestamp:native_order_adjusted": 1}


def test_opencode_reports_results_associated_across_intervening_messages(
    tmp_path: Path,
) -> None:
    base = portable_session(tmp_path)
    user, _image, assistant, call, result, final = base.events
    interstitial = replace(
        assistant,
        text="SYNTHETIC_INTERSTITIAL_MARKER",
        provenance=Provenance(2, "assistant"),
    )
    source = replace(base, events=(user, call, interstitial, result, final))

    data, dropped = opencode.serialize(
        source,
        session_id=TARGET_OPENCODE_ID,
        cwd=tmp_path,
    )
    path = tmp_path / "associated-result.json"
    path.write_bytes(data)
    parsed = opencode.parse(path)

    assert dropped == {"tool_result:native_order_associated": 1}
    assert [event.kind for event in parsed.events] == [
        EventKind.MESSAGE,
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
        EventKind.MESSAGE,
        EventKind.MESSAGE,
    ]


def test_opencode_reports_results_beyond_call_multiplicity_as_orphans(
    tmp_path: Path,
) -> None:
    base = portable_session(tmp_path)
    user, _image, _assistant, call, result, final = base.events
    repeated_result = replace(
        result,
        text="SYNTHETIC_REPEATED_RESULT",
        payload={"content_blocks": [{"type": "text", "text": "repeated"}]},
        provenance=Provenance(3, "tool_result"),
    )
    source = replace(base, events=(user, call, result, repeated_result, final))

    data, dropped = opencode.serialize(
        source,
        session_id=TARGET_OPENCODE_ID,
        cwd=tmp_path,
    )
    path = tmp_path / "duplicate-results.json"
    path.write_bytes(data)

    opencode.validate_native_bytes(data, TARGET_OPENCODE_ID)
    parsed = opencode.parse(path)
    calls = [event.tool_call_id for event in parsed.events if event.kind == EventKind.TOOL_CALL]
    results = [event.tool_call_id for event in parsed.events if event.kind == EventKind.TOOL_RESULT]
    assert dropped == {
        "tool_result:duplicate_id": 1,
        "tool_result:orphan_id": 1,
    }
    assert len(calls) == len(set(calls)) == 2
    assert results == calls


def test_cursor_writer_is_present_with_an_explicit_experimental_contract() -> None:
    root = Path(__file__).parents[1]
    documentation = (root / "docs" / "cursor-format.md").read_text(encoding="utf-8")

    assert (root / "src" / "session_migrate" / "formats" / "cursor.py").is_file()
    assert "experimental" in documentation.casefold()
    assert "text-only" in documentation.casefold()
