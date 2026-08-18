import json
from pathlib import Path

import pytest

from session_bridge import conversion
from session_bridge.conversion import (
    ConversionOptions,
    convert_session,
    load_session,
    target_import_paths,
    write_artifact,
)
from session_bridge.errors import JsonlError, SessionBridgeError
from session_bridge.formats import claude, codex
from session_bridge.model import AgentFormat, EventKind, Role, Session

TARGET_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def write_jsonl(path: Path, records: list[dict[str, object]]) -> Path:
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    return path


def claude_record(
    record_type: str,
    record_uuid: str,
    parent_uuid: str | None,
    content: object,
    *,
    cwd: str,
) -> dict[str, object]:
    return {
        "type": record_type,
        "uuid": record_uuid,
        "parentUuid": parent_uuid,
        "sessionId": "11111111-1111-4111-8111-111111111111",
        "timestamp": "2026-08-17T12:00:00Z",
        "cwd": cwd,
        "version": "2.1.209",
        "message": {"role": record_type, "content": content},
    }


def semantic_signature(session: Session, *, omit: set[EventKind] | None = None) -> list[tuple]:
    omitted = omit or set()
    signature: list[tuple] = []
    for event in session.events:
        if event.kind in omitted:
            continue
        portable_payload: object = None
        if event.kind == EventKind.TOOL_CALL:
            portable_payload = event.payload.get("input")
        elif event.kind == EventKind.TOOL_RESULT:
            portable_payload = event.payload.get("content_blocks")
        elif event.kind == EventKind.CONTEXT:
            portable_payload = {
                "block_type": event.payload.get("block_type"),
                "image_url": event.payload.get("image_url"),
            }
        signature.append(
            (
                event.kind,
                event.role,
                event.text,
                event.tool_name,
                event.tool_call_id,
                json.dumps(portable_payload, sort_keys=True),
            )
        )
    return signature


def test_claude_parser_uses_last_prompt_leaf(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    path = write_jsonl(
        tmp_path / "claude.jsonl",
        [
            claude_record("user", "u1", None, "root", cwd=cwd),
            claude_record("assistant", "a1", "u1", "answer", cwd=cwd),
            claude_record("user", "active", "a1", "active branch", cwd=cwd),
            claude_record("user", "abandoned", "a1", "abandoned branch", cwd=cwd),
            {"type": "last-prompt", "leafUuid": "active"},
        ],
    )

    session = claude.parse(path)

    messages = [event.text for event in session.events if event.kind == EventKind.MESSAGE]
    assert messages == ["root", "answer", "active branch"]
    assert session.event_counts()["opaque"] == 1


def test_claude_parser_orders_child_after_physical_parent(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    path = write_jsonl(
        tmp_path / "claude-out-of-order.jsonl",
        [
            claude_record("user", "u1", None, "run it", cwd=cwd),
            claude_record(
                "user",
                "u2",
                "a1",
                [{"type": "tool_result", "tool_use_id": "tool-1", "content": "done"}],
                cwd=cwd,
            ),
            claude_record(
                "assistant",
                "a1",
                "u1",
                [{"type": "tool_use", "id": "tool-1", "name": "Read", "input": {}}],
                cwd=cwd,
            ),
            {"type": "last-prompt", "leafUuid": "u2"},
        ],
    )

    session = claude.parse(path)

    assert [event.kind for event in session.events] == [
        EventKind.MESSAGE,
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
    ]


def test_claude_active_meta_record_is_not_replayed_as_user_history(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    metadata = claude_record("user", "meta", "u1", "internal metadata", cwd=cwd)
    metadata["isMeta"] = True
    path = write_jsonl(
        tmp_path / "claude-meta-ancestor.jsonl",
        [
            claude_record("user", "u1", None, "actual request", cwd=cwd),
            metadata,
            claude_record("assistant", "a1", "meta", "actual answer", cwd=cwd),
            {"type": "last-prompt", "leafUuid": "a1"},
        ],
    )

    session = claude.parse(path)

    assert [event.text for event in session.events if event.kind == EventKind.MESSAGE] == [
        "actual request",
        "actual answer",
    ]
    artifact = convert_session(
        session,
        ConversionOptions(target_format=AgentFormat.CODEX, cwd=tmp_path),
    )
    assert artifact.dropped == {"opaque:active_graph_metadata_record": 1}


def test_claude_non_message_leaf_does_not_merge_abandoned_branch(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    path = write_jsonl(
        tmp_path / "claude-boundary-leaf.jsonl",
        [
            claude_record("user", "u1", None, "root", cwd=cwd),
            claude_record("assistant", "a1", "u1", "answer", cwd=cwd),
            {
                "type": "system",
                "subtype": "compact_boundary",
                "uuid": "boundary",
                "parentUuid": None,
                "logicalParentUuid": "a1",
                "sessionId": "11111111-1111-4111-8111-111111111111",
                "timestamp": "2026-08-17T12:00:02Z",
                "cwd": cwd,
                "version": "2.1.209",
            },
            claude_record("user", "abandoned", "a1", "not active", cwd=cwd),
            {"type": "last-prompt", "leafUuid": "boundary"},
        ],
    )

    session = claude.parse(path)

    assert [event.text for event in session.events if event.kind == EventKind.MESSAGE] == [
        "root",
        "answer",
    ]
    assert session.event_counts() == {"compaction": 1, "message": 2, "opaque": 1}


def test_claude_compaction_pair_maps_once_and_keeps_mainline(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    summary = claude_record("user", "summary", "boundary", "summary text", cwd=cwd)
    summary["isCompactSummary"] = True
    summary["isVisibleInTranscriptOnly"] = True
    path = write_jsonl(
        tmp_path / "claude-compaction.jsonl",
        [
            claude_record("user", "u1", None, "root", cwd=cwd),
            claude_record("assistant", "a1", "u1", "answer", cwd=cwd),
            {
                "type": "system",
                "subtype": "compact_boundary",
                "uuid": "boundary",
                "parentUuid": None,
                "logicalParentUuid": "a1",
                "sessionId": "11111111-1111-4111-8111-111111111111",
                "timestamp": "2026-08-17T12:00:02Z",
                "cwd": cwd,
                "version": "2.1.209",
            },
            summary,
            claude_record("assistant", "a2", "summary", "after summary", cwd=cwd),
            claude_record("user", "abandoned", "a1", "not active", cwd=cwd),
            {"type": "last-prompt", "leafUuid": "a2"},
        ],
    )

    source = claude.parse(path)
    assert [event.text for event in source.events if event.kind != EventKind.OPAQUE] == [
        "root",
        "answer",
        "summary text",
        "after summary",
    ]
    artifact = convert_session(
        source,
        ConversionOptions(target_format=AgentFormat.CODEX, session_id=TARGET_ID, cwd=tmp_path),
    )
    records = [json.loads(line) for line in artifact.native_bytes.splitlines()]
    assert sum(record["type"] == "compacted" for record in records) == 1
    assert artifact.dropped == {"opaque:inactive_or_metadata_conversation_record": 1}


def test_claude_compaction_logical_back_edge_terminates_cycle(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    summary = claude_record("user", "summary", "boundary", "summary text", cwd=cwd)
    summary["isCompactSummary"] = True
    summary["isVisibleInTranscriptOnly"] = True
    path = write_jsonl(
        tmp_path / "claude-compaction-back-edge.jsonl",
        [
            {
                "type": "system",
                "subtype": "compact_boundary",
                "uuid": "boundary",
                "parentUuid": None,
                "logicalParentUuid": "u2",
                "sessionId": "11111111-1111-4111-8111-111111111111",
                "timestamp": "2026-08-17T12:00:00Z",
                "cwd": cwd,
                "version": "2.1.209",
                "compactMetadata": {
                    "preservedSegment": {
                        "anchorUuid": "summary",
                        "headUuid": "a1",
                        "tailUuid": "u2",
                    },
                    "preservedMessages": {
                        "uuids": ["a1", "u2"],
                        "allUuids": ["a1", "u2"],
                    },
                },
            },
            summary,
            claude_record("assistant", "a1", "summary", "after summary", cwd=cwd),
            claude_record("user", "u2", "a1", "continue", cwd=cwd),
            claude_record("assistant", "a2", "u2", "complete", cwd=cwd),
            {"type": "last-prompt", "leafUuid": "a2"},
        ],
    )

    session = claude.parse(path)

    assert [event.text for event in session.events] == [
        "summary text",
        "after summary",
        "continue",
        "complete",
    ]


def test_claude_to_codex_preserves_messages_and_tools(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    source_path = write_jsonl(
        tmp_path / "claude.jsonl",
        [
            claude_record("user", "u1", None, "run it", cwd=cwd),
            claude_record(
                "assistant",
                "a1",
                "u1",
                [
                    {"type": "text", "text": "running"},
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "Shell",
                        "input": {"command": "pwd"},
                    },
                ],
                cwd=cwd,
            ),
            claude_record(
                "user",
                "u2",
                "a1",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": "/work",
                    }
                ],
                cwd=cwd,
            ),
            claude_record("assistant", "a2", "u2", "done", cwd=cwd),
        ],
    )
    source = load_session(source_path)

    artifact = convert_session(
        source,
        ConversionOptions(
            target_format=AgentFormat.CODEX,
            session_id=TARGET_ID,
            cwd=tmp_path,
        ),
    )
    target_path = tmp_path / "target.jsonl"
    target_path.write_bytes(artifact.native_bytes)
    reparsed = codex.parse(target_path)

    assert artifact.native_record_count == 9
    assert artifact.dropped == {}
    assert [event.kind for event in reparsed.events] == [
        EventKind.MESSAGE,
        EventKind.MESSAGE,
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
        EventKind.MESSAGE,
    ]
    call = reparsed.events[2]
    assert call.tool_name == "Shell"
    assert call.tool_call_id == "tool-1"
    assert call.payload["input"] == {"command": "pwd"}


def test_codex_to_claude_creates_linear_native_graph(tmp_path: Path) -> None:
    source_path = write_jsonl(
        tmp_path / "codex.jsonl",
        [
            {
                "timestamp": "2026-08-17T12:00:00Z",
                "type": "session_meta",
                "payload": {
                    "session_id": "22222222-2222-4222-8222-222222222222",
                    "id": "22222222-2222-4222-8222-222222222222",
                    "timestamp": "2026-08-17T12:00:00Z",
                    "cwd": str(tmp_path),
                    "originator": "codex",
                    "cli_version": "0.144.4",
                    "source": "cli",
                    "model_provider": "openai",
                },
            },
            {
                "timestamp": "2026-08-17T12:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello"}],
                },
            },
            {
                "timestamp": "2026-08-17T12:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "hi"}],
                },
            },
        ],
    )
    source = codex.parse(source_path)

    artifact = convert_session(
        source,
        ConversionOptions(
            target_format=AgentFormat.CLAUDE,
            session_id=TARGET_ID,
            cwd=tmp_path,
        ),
    )
    records = [json.loads(line) for line in artifact.native_bytes.splitlines()]

    assert [record["type"] for record in records] == ["user", "assistant"]
    assert records[0]["parentUuid"] is None
    assert records[1]["parentUuid"] == records[0]["uuid"]
    assert all(record["sessionId"] == TARGET_ID for record in records)


def test_codex_developer_messages_are_not_converted_to_user_prompts(tmp_path: Path) -> None:
    source_path = write_jsonl(
        tmp_path / "codex-roles.jsonl",
        [
            {
                "timestamp": "2026-08-17T12:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "22222222-2222-4222-8222-222222222222",
                    "timestamp": "2026-08-17T12:00:00Z",
                    "cwd": str(tmp_path),
                    "originator": "codex",
                    "cli_version": "0.144.4",
                    "model_provider": "openai",
                },
            },
            {
                "timestamp": "2026-08-17T12:00:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "privileged instructions"}],
                },
            },
            {
                "timestamp": "2026-08-17T12:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "actual request"}],
                },
            },
        ],
    )

    source = codex.parse(source_path)
    assert [event.role for event in source.events] == [Role.SYSTEM, Role.USER]
    artifact = convert_session(
        source,
        ConversionOptions(target_format=AgentFormat.CLAUDE, session_id=TARGET_ID, cwd=tmp_path),
    )
    records = [json.loads(line) for line in artifact.native_bytes.splitlines()]
    assert [record["message"]["content"] for record in records] == ["actual request"]
    assert artifact.dropped == {"message:privileged_role": 1}


def test_codex_developer_images_are_not_converted_to_user_images(tmp_path: Path) -> None:
    source_path = write_jsonl(
        tmp_path / "codex-developer-image.jsonl",
        [
            {
                "timestamp": "2026-08-17T12:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "22222222-2222-4222-8222-222222222222",
                    "timestamp": "2026-08-17T12:00:00Z",
                    "cwd": str(tmp_path),
                    "originator": "codex",
                    "cli_version": "0.144.4",
                    "model_provider": "openai",
                },
            },
            {
                "timestamp": "2026-08-17T12:00:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "content": [
                        {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}
                    ],
                },
            },
            {
                "timestamp": "2026-08-17T12:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "actual request"}],
                },
            },
        ],
    )

    artifact = convert_session(
        codex.parse(source_path),
        ConversionOptions(target_format=AgentFormat.CLAUDE, session_id=TARGET_ID, cwd=tmp_path),
    )
    records = [json.loads(line) for line in artifact.native_bytes.splitlines()]
    assert [record["message"]["content"] for record in records] == ["actual request"]
    assert artifact.dropped == {"context:privileged_image": 1}


def test_import_paths_are_native_and_manifest_is_private(tmp_path: Path) -> None:
    source_path = write_jsonl(
        tmp_path / "codex.jsonl",
        [
            {
                "timestamp": "2026-08-17T12:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "22222222-2222-4222-8222-222222222222",
                    "timestamp": "2026-08-17T12:00:00Z",
                    "cwd": str(tmp_path / "a.b_c-d e"),
                    "originator": "codex",
                    "cli_version": "0.144.4",
                    "model_provider": "openai",
                },
            },
            {
                "timestamp": "2026-08-17T12:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello"}],
                },
            },
        ],
    )
    artifact = convert_session(
        codex.parse(source_path),
        ConversionOptions(target_format=AgentFormat.CLAUDE, session_id=TARGET_ID),
    )
    native_path, manifest_path = target_import_paths(artifact, tmp_path / "claude-home")

    assert native_path.name == f"{TARGET_ID}.jsonl"
    assert "a-b-c-d-e" in native_path.parent.name
    write_artifact(artifact, output_path=native_path, manifest_path=manifest_path)
    assert native_path.exists()
    assert manifest_path.exists()
    assert native_path.stat().st_mode & 0o777 == 0o600
    assert manifest_path.stat().st_mode & 0o777 == 0o600
    manifest = json.loads(manifest_path.read_text())
    assert manifest["source"]["sha256"] == artifact.source.source_sha256
    assert manifest["target"]["sha256"] == artifact.target_sha256


def test_images_map_through_portable_data_urls(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    source_path = write_jsonl(
        tmp_path / "claude-image.jsonl",
        [
            claude_record(
                "user",
                "u1",
                None,
                [
                    {"type": "text", "text": "inspect this"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "c3ludGhldGlj",
                        },
                    },
                ],
                cwd=cwd,
            )
        ],
    )

    codex_artifact = convert_session(
        claude.parse(source_path),
        ConversionOptions(target_format=AgentFormat.CODEX, session_id=TARGET_ID, cwd=tmp_path),
    )
    codex_records = [json.loads(line) for line in codex_artifact.native_bytes.splitlines()]
    image_item = next(
        record
        for record in codex_records
        if record["type"] == "response_item"
        and record["payload"].get("content", [{}])[0].get("type") == "input_image"
    )
    assert image_item["payload"]["content"][0]["image_url"] == (
        "data:image/png;base64,c3ludGhldGlj"
    )
    assert codex_artifact.dropped == {}

    codex_path = tmp_path / "codex-image.jsonl"
    codex_path.write_bytes(codex_artifact.native_bytes)
    claude_artifact = convert_session(
        codex.parse(codex_path),
        ConversionOptions(
            target_format=AgentFormat.CLAUDE,
            session_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            cwd=tmp_path,
        ),
    )
    claude_records = [json.loads(line) for line in claude_artifact.native_bytes.splitlines()]
    image_block = next(
        block
        for record in claude_records
        for block in (
            record["message"]["content"]
            if isinstance(record["message"]["content"], list)
            else []
        )
        if block.get("type") == "image"
    )
    assert image_block["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": "c3ludGhldGlj",
    }
    assert claude_artifact.dropped == {}


def test_structured_tool_results_preserve_text_and_images(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    source_path = write_jsonl(
        tmp_path / "claude-tool-result.jsonl",
        [
            claude_record(
                "assistant",
                "a1",
                None,
                [{"type": "tool_use", "id": "tool-1", "name": "Read", "input": {}}],
                cwd=cwd,
            ),
            claude_record(
                "user",
                "u1",
                "a1",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": [
                            {"type": "text", "text": "result"},
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/jpeg",
                                    "data": "c3ludGhldGlj",
                                },
                            },
                            {"type": "tool_reference", "tool_name": "Read"},
                        ],
                    }
                ],
                cwd=cwd,
            ),
        ],
    )

    artifact = convert_session(
        claude.parse(source_path),
        ConversionOptions(target_format=AgentFormat.CODEX, session_id=TARGET_ID, cwd=tmp_path),
    )
    records = [json.loads(line) for line in artifact.native_bytes.splitlines()]
    output = next(
        record["payload"]["output"]
        for record in records
        if record["type"] == "response_item"
        and record["payload"].get("type") == "function_call_output"
    )
    assert output == [
        {"type": "input_text", "text": "result"},
        {
            "type": "input_image",
            "image_url": "data:image/jpeg;base64,c3ludGhldGlj",
        },
    ]
    assert artifact.dropped == {"tool_result:tool_reference": 1}


def test_missing_tool_ids_are_linked_and_reported(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    source_path = write_jsonl(
        tmp_path / "claude-missing-tool-id.jsonl",
        [
            claude_record(
                "assistant",
                "a1",
                None,
                [{"type": "tool_use", "name": "Read", "input": {}}],
                cwd=cwd,
            ),
            claude_record(
                "user",
                "u1",
                "a1",
                [{"type": "tool_result", "content": "result"}],
                cwd=cwd,
            ),
        ],
    )

    artifact = convert_session(
        claude.parse(source_path),
        ConversionOptions(target_format=AgentFormat.CODEX, session_id=TARGET_ID, cwd=tmp_path),
    )
    response_items = [
        json.loads(line)["payload"]
        for line in artifact.native_bytes.splitlines()
        if json.loads(line)["type"] == "response_item"
    ]
    call = next(item for item in response_items if item["type"] == "function_call")
    output = next(item for item in response_items if item["type"] == "function_call_output")
    assert call["call_id"] == output["call_id"]
    assert artifact.dropped == {"tool_call:missing_id": 1, "tool_result:missing_id": 1}


def test_orphan_and_duplicate_tool_ids_are_reported_in_both_directions(
    tmp_path: Path,
) -> None:
    cwd = str(tmp_path)
    claude_source = write_jsonl(
        tmp_path / "claude-invalid-tool-links.jsonl",
        [
            claude_record("user", "u0", None, "start", cwd=cwd),
            claude_record(
                "assistant",
                "a1",
                "u0",
                [
                    {"type": "tool_use", "id": "duplicate", "name": "Read", "input": {}},
                    {"type": "tool_use", "id": "duplicate", "name": "Read", "input": {}},
                ],
                cwd=cwd,
            ),
            claude_record(
                "user",
                "u1",
                "a1",
                [
                    {"type": "tool_result", "tool_use_id": "orphan", "content": "one"},
                    {"type": "tool_result", "tool_use_id": "orphan", "content": "two"},
                ],
                cwd=cwd,
            )
        ],
    )
    claude_artifact = convert_session(
        claude.parse(claude_source),
        ConversionOptions(target_format=AgentFormat.CODEX, cwd=tmp_path),
    )
    assert claude_artifact.dropped == {
        "tool_call:duplicate_id": 1,
        "tool_result:duplicate_id": 1,
        "tool_result:orphan_id": 2,
    }
    orphan_warning = next(
        warning
        for warning in claude_artifact.warnings
        if warning.get("event_kind") == "tool_result:orphan_id"
    )
    assert "record was retained" in orphan_warning["message"]

    codex_source = write_jsonl(
        tmp_path / "codex-invalid-tool-links.jsonl",
        [
            {
                "timestamp": "2026-08-17T12:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "22222222-2222-4222-8222-222222222222",
                    "cwd": cwd,
                    "cli_version": "0.144.4",
                },
            },
            *[
                {
                    "timestamp": "2026-08-17T12:00:01Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "Read",
                        "arguments": "{}",
                        "call_id": "duplicate",
                    },
                }
                for _ in range(2)
            ],
            *[
                {
                    "timestamp": "2026-08-17T12:00:02Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "orphan",
                        "output": output,
                    },
                }
                for output in ("one", "two")
            ],
        ],
    )
    codex_artifact = convert_session(
        codex.parse(codex_source),
        ConversionOptions(target_format=AgentFormat.CLAUDE, cwd=tmp_path),
    )
    assert codex_artifact.dropped == {
        "tool_call:duplicate_id": 1,
        "tool_result:duplicate_id": 1,
        "tool_result:orphan_id": 2,
    }


def test_metadata_only_session_has_specific_empty_history_error(tmp_path: Path) -> None:
    path = write_jsonl(
        tmp_path / "metadata-only.jsonl",
        [
            {
                "timestamp": "2026-08-17T12:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "22222222-2222-4222-8222-222222222222",
                    "cwd": str(tmp_path),
                    "cli_version": "0.144.4",
                },
            }
        ],
    )

    with pytest.raises(SessionBridgeError, match="no resumable conversation history"):
        convert_session(
            codex.parse(path),
            ConversionOptions(target_format=AgentFormat.CLAUDE, cwd=tmp_path),
        )


def test_invalid_event_timestamp_falls_back_and_is_reported(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    first = claude_record("user", "u1", None, "hello", cwd=cwd)
    second = claude_record("assistant", "a1", "u1", "hi", cwd=cwd)
    second["timestamp"] = "not-a-timestamp"
    source_path = write_jsonl(tmp_path / "claude-time.jsonl", [first, second])

    artifact = convert_session(
        claude.parse(source_path),
        ConversionOptions(
            target_format=AgentFormat.CODEX,
            session_id=TARGET_ID,
            cwd=tmp_path,
            target_cli_version="9.9.9",
        ),
    )
    records = [json.loads(line) for line in artifact.native_bytes.splitlines()]
    assert all(record["timestamp"] != "not-a-timestamp" for record in records)
    assert artifact.dropped == {"timestamp:invalid": 1}
    assert any(warning["code"] == "unvalidated_target_version" for warning in artifact.warnings)


def test_load_session_rejects_source_change_during_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_jsonl(
        tmp_path / "changing.jsonl",
        [claude_record("user", "u1", None, "hello", cwd=str(tmp_path))],
    )
    original_parse = claude.parse

    def parse_then_append(source_path: Path) -> Session:
        session = original_parse(source_path)
        with source_path.open("a") as stream:
            stream.write("{}\n")
        return session

    monkeypatch.setattr(claude, "parse", parse_then_append)

    with pytest.raises(JsonlError, match="source session changed"):
        load_session(path, AgentFormat.CLAUDE)


def test_rejects_paginated_and_expands_replacement_history(tmp_path: Path) -> None:
    base_meta = {
        "timestamp": "2026-08-17T12:00:00Z",
        "type": "session_meta",
        "payload": {
            "id": "22222222-2222-4222-8222-222222222222",
            "timestamp": "2026-08-17T12:00:00Z",
            "cwd": str(tmp_path),
            "cli_version": "0.144.4",
            "model_provider": "openai",
        },
    }
    paginated = json.loads(json.dumps(base_meta))
    paginated["payload"]["history_mode"] = "paginated"
    paginated_path = write_jsonl(tmp_path / "paginated.jsonl", [paginated])
    with pytest.raises(SessionBridgeError, match="history mode"):
        codex.parse(paginated_path)

    replacement_path = write_jsonl(
        tmp_path / "replacement.jsonl",
        [
            base_meta,
            {
                "timestamp": "2026-08-17T12:00:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "visible history"}],
                },
            },
            {
                "timestamp": "2026-08-17T12:00:01Z",
                "type": "event_msg",
                "payload": {"type": "context_compacted"},
            },
            {
                "timestamp": "2026-08-17T12:00:01Z",
                "type": "compacted",
                "payload": {
                    "message": "summary",
                    "replacement_history": [
                        {
                            "type": "compaction",
                            "encrypted_content": "opaque-provider-state",
                        }
                    ],
                },
            },
        ],
    )
    session = codex.parse(replacement_path)
    assert [event.kind for event in session.events] == [
        EventKind.MESSAGE,
        EventKind.COMPACTION,
    ]
    artifact = convert_session(
        session,
        ConversionOptions(target_format=AgentFormat.CLAUDE, cwd=tmp_path),
    )
    assert artifact.dropped == {"compaction:replacement_history_expanded": 1}
    assert "visible pre-compaction transcript" in artifact.warnings[0]["message"]


def test_codex_mixed_ui_messages_recover_only_unmatched_fallback(tmp_path: Path) -> None:
    path = write_jsonl(
        tmp_path / "partial-rollout.jsonl",
        [
            {
                "timestamp": "2026-08-17T12:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "22222222-2222-4222-8222-222222222222",
                    "cwd": str(tmp_path),
                    "cli_version": "0.144.4",
                },
            },
            {
                "timestamp": "2026-08-17T12:00:01Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "canonical"},
            },
            {
                "timestamp": "2026-08-17T12:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "canonical"}],
                },
            },
            {
                "timestamp": "2026-08-17T12:00:03Z",
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": "partial only"},
            },
        ],
    )

    session = codex.parse(path)

    assert [event.kind for event in session.events] == [EventKind.MESSAGE, EventKind.MESSAGE]
    assert [event.text for event in session.events] == ["canonical", "partial only"]
    assert session.events[-1].payload == {"ui_only_projection": True}
    artifact = convert_session(
        session,
        ConversionOptions(target_format=AgentFormat.CLAUDE, cwd=tmp_path),
    )
    assert artifact.dropped == {"message:ui_only_projection": 1}
    assert "retained as visible conversation history" in artifact.warnings[0]["message"]


def test_rejects_invalid_claude_graphs(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    duplicate_path = write_jsonl(
        tmp_path / "duplicate.jsonl",
        [
            claude_record("user", "same", None, "one", cwd=cwd),
            claude_record("assistant", "same", None, "two", cwd=cwd),
        ],
    )
    with pytest.raises(SessionBridgeError, match="duplicate record UUID"):
        claude.parse(duplicate_path)

    mixed = claude_record("assistant", "a1", "u1", "two", cwd=cwd)
    mixed["sessionId"] = "99999999-9999-4999-8999-999999999999"
    mixed_path = write_jsonl(
        tmp_path / "mixed-session.jsonl",
        [claude_record("user", "u1", None, "one", cwd=cwd), mixed],
    )
    with pytest.raises(SessionBridgeError, match="mixed sessionId"):
        claude.parse(mixed_path)

    broken_path = write_jsonl(
        tmp_path / "broken-parent.jsonl",
        [claude_record("assistant", "a1", "missing", "answer", cwd=cwd)],
    )
    with pytest.raises(SessionBridgeError, match="missing parent UUID"):
        claude.parse(broken_path)


def test_rejects_standalone_claude_sidechain_with_precise_error(tmp_path: Path) -> None:
    record = claude_record("user", "u1", None, "subagent prompt", cwd=str(tmp_path))
    record["isSidechain"] = True
    path = write_jsonl(tmp_path / "sidechain.jsonl", [record])

    with pytest.raises(SessionBridgeError, match="sidechain/subagent"):
        claude.parse(path)


def test_custom_tool_input_is_wrapped_and_reported(tmp_path: Path) -> None:
    source_path = write_jsonl(
        tmp_path / "codex-custom-tool.jsonl",
        [
            {
                "timestamp": "2026-08-17T12:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "22222222-2222-4222-8222-222222222222",
                    "timestamp": "2026-08-17T12:00:00Z",
                    "cwd": str(tmp_path),
                    "cli_version": "0.144.4",
                    "model_provider": "openai",
                },
            },
            {
                "timestamp": "2026-08-17T12:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "freeform",
                    "input": "raw input",
                    "call_id": "call-1",
                },
            },
        ],
    )

    artifact = convert_session(
        codex.parse(source_path),
        ConversionOptions(target_format=AgentFormat.CLAUDE, session_id=TARGET_ID, cwd=tmp_path),
    )
    records = [json.loads(line) for line in artifact.native_bytes.splitlines()]
    assert records[0]["message"]["content"][0]["input"] == {"input": "raw input"}
    assert artifact.dropped == {"tool_call:non_object_input": 1}


def test_invalid_image_media_is_omitted_and_reported(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    source_path = write_jsonl(
        tmp_path / "invalid-image.jsonl",
        [
            claude_record(
                "user",
                "u1",
                None,
                [
                    {"type": "text", "text": "safe text remains"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "text/html",
                            "data": "PGgxPk5PUEU8L2gxPg==",
                        },
                    },
                ],
                cwd=cwd,
            )
        ],
    )

    artifact = convert_session(
        claude.parse(source_path),
        ConversionOptions(target_format=AgentFormat.CODEX, session_id=TARGET_ID, cwd=tmp_path),
    )
    assert artifact.dropped == {"context:image": 1}
    assert b"text/html" not in artifact.native_bytes


def test_rejects_conversion_without_resumable_history(tmp_path: Path) -> None:
    source_path = write_jsonl(
        tmp_path / "title-only.jsonl",
        [
            {
                "type": "custom-title",
                "customTitle": "Synthetic title",
                "sessionId": "11111111-1111-4111-8111-111111111111",
            }
        ],
    )

    with pytest.raises(SessionBridgeError, match="no resumable conversation history"):
        convert_session(
            claude.parse(source_path),
            ConversionOptions(target_format=AgentFormat.CODEX, session_id=TARGET_ID, cwd=tmp_path),
        )


def test_unknown_structured_tool_output_is_counted(tmp_path: Path) -> None:
    source_path = write_jsonl(
        tmp_path / "codex-encrypted-tool-output.jsonl",
        [
            {
                "timestamp": "2026-08-17T12:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "22222222-2222-4222-8222-222222222222",
                    "timestamp": "2026-08-17T12:00:00Z",
                    "cwd": str(tmp_path),
                    "cli_version": "0.144.4",
                    "model_provider": "openai",
                },
            },
            {
                "timestamp": "2026-08-17T12:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "tool",
                    "arguments": "{}",
                    "call_id": "call-1",
                },
            },
            {
                "timestamp": "2026-08-17T12:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": [
                        {"type": "encrypted_content", "data": "opaque"},
                        17,
                    ],
                },
            },
        ],
    )

    artifact = convert_session(
        codex.parse(source_path),
        ConversionOptions(target_format=AgentFormat.CLAUDE, session_id=TARGET_ID, cwd=tmp_path),
    )
    assert artifact.dropped == {"tool_result:opaque": 2}


def test_malformed_known_tool_result_blocks_are_counted_in_both_directions(
    tmp_path: Path,
) -> None:
    cwd = str(tmp_path)
    claude_source = write_jsonl(
        tmp_path / "claude-malformed-tool-result.jsonl",
        [
            claude_record("user", "u1", None, "start", cwd=cwd),
            claude_record(
                "assistant",
                "a1",
                "u1",
                [{"type": "tool_use", "id": "call-1", "name": "Read", "input": {}}],
                cwd=cwd,
            ),
            claude_record(
                "user",
                "u2",
                "a1",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call-1",
                        "content": [
                            {"type": "image", "source": {"type": "base64"}},
                            {"type": "tool_reference"},
                        ],
                    }
                ],
                cwd=cwd,
            ),
        ],
    )
    claude_artifact = convert_session(
        claude.parse(claude_source),
        ConversionOptions(target_format=AgentFormat.CODEX, cwd=tmp_path),
    )
    assert claude_artifact.dropped == {"tool_result:opaque": 2}

    codex_source = write_jsonl(
        tmp_path / "codex-malformed-tool-result.jsonl",
        [
            {
                "timestamp": "2026-08-17T12:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "22222222-2222-4222-8222-222222222222",
                    "timestamp": "2026-08-17T12:00:00Z",
                    "cwd": cwd,
                    "cli_version": "0.144.4",
                    "model_provider": "openai",
                },
            },
            {
                "timestamp": "2026-08-17T12:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "Read",
                    "arguments": "{}",
                    "call_id": "call-1",
                },
            },
            {
                "timestamp": "2026-08-17T12:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": [
                        {"type": "input_image"},
                        {"type": "tool_reference"},
                    ],
                },
            },
        ],
    )
    codex_artifact = convert_session(
        codex.parse(codex_source),
        ConversionOptions(target_format=AgentFormat.CLAUDE, cwd=tmp_path),
    )
    assert codex_artifact.dropped == {"tool_result:opaque": 2}


def test_manifest_failure_does_not_delete_replaced_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_path = write_jsonl(
        tmp_path / "codex.jsonl",
        [
            {
                "timestamp": "2026-08-17T12:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "22222222-2222-4222-8222-222222222222",
                    "timestamp": "2026-08-17T12:00:00Z",
                    "cwd": str(tmp_path),
                    "cli_version": "0.144.4",
                    "model_provider": "openai",
                },
            },
            {
                "timestamp": "2026-08-17T12:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello"}],
                },
            },
        ],
    )
    artifact = convert_session(
        codex.parse(source_path),
        ConversionOptions(target_format=AgentFormat.CLAUDE, session_id=TARGET_ID, cwd=tmp_path),
    )
    output_path = tmp_path / "output.jsonl"
    manifest_path = tmp_path / "manifest.json"
    original_writer = conversion.write_private_atomic
    calls = 0

    def fail_after_replacement(path: Path, data: bytes) -> tuple[int, int]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_writer(path, data)
        output_path.unlink()
        output_path.write_bytes(b"replacement owned by another process")
        raise JsonlError("synthetic manifest failure")

    monkeypatch.setattr(conversion, "write_private_atomic", fail_after_replacement)

    with pytest.raises(JsonlError, match="synthetic manifest failure"):
        write_artifact(
            artifact,
            output_path=output_path,
            manifest_path=manifest_path,
        )

    assert output_path.read_bytes() == b"replacement owned by another process"


def test_claude_fixture_semantics_survive_codex_round_trip(tmp_path: Path) -> None:
    fixture = Path(__file__).parent / "fixtures" / "claude-2.1.209" / "basic.jsonl"
    source = claude.parse(fixture)
    artifact = convert_session(
        source,
        ConversionOptions(target_format=AgentFormat.CODEX, session_id=TARGET_ID, cwd=tmp_path),
    )
    converted = tmp_path / "converted-codex.jsonl"
    converted.write_bytes(artifact.native_bytes)
    reparsed = codex.parse(converted)

    assert semantic_signature(source, omit={EventKind.OPAQUE}) == semantic_signature(reparsed)


def test_codex_fixture_semantics_survive_claude_round_trip(tmp_path: Path) -> None:
    fixture = Path(__file__).parent / "fixtures" / "codex-0.144.4" / "basic.jsonl"
    source = codex.parse(fixture)
    artifact = convert_session(
        source,
        ConversionOptions(target_format=AgentFormat.CLAUDE, session_id=TARGET_ID, cwd=tmp_path),
    )
    converted = tmp_path / "converted-claude.jsonl"
    converted.write_bytes(artifact.native_bytes)
    reparsed = claude.parse(converted)

    assert semantic_signature(source, omit={EventKind.COMPACTION}) == semantic_signature(reparsed)
