import json
from pathlib import Path

from session_bridge.conversion import (
    ConversionOptions,
    convert_session,
    load_session,
    target_import_paths,
    write_artifact,
)
from session_bridge.formats import claude, codex
from session_bridge.model import AgentFormat, EventKind, Role

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
    assert artifact.dropped == {"message": 1}


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
