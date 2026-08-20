import json
from pathlib import Path

import pytest

from session_migrate import inspection
from session_migrate.errors import FormatDetectionError, JsonlError
from session_migrate.formats import antigravity, claude
from session_migrate.inspection import inspect_session
from session_migrate.model import AgentFormat


def write_jsonl(path: Path, records: list[dict[str, object]]) -> Path:
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    return path


def test_inspects_claude_without_printing_content(tmp_path: Path) -> None:
    path = write_jsonl(
        tmp_path / "claude.jsonl",
        [
            {
                "type": "user",
                "sessionId": "claude-session",
                "cwd": "/work",
                "version": "1.2.3",
                "timestamp": "2026-08-17T12:00:00Z",
                "message": {"role": "user", "content": "private prompt"},
            },
            {
                "type": "assistant",
                "sessionId": "claude-session",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "private answer"},
                        {"type": "tool_use", "id": "tool-1", "name": "Read", "input": {}},
                    ],
                },
            },
        ],
    )

    result = inspect_session(path)

    assert result.format == "claude"
    assert result.records == 2
    assert result.session_id == "claude-session"
    assert result.content_blocks == {"text": 2, "tool_use": 1}
    assert result.tool_calls == 1
    assert "private prompt" not in result.to_json()
    assert "private answer" not in result.to_json()


def test_inspects_codex_rollout(tmp_path: Path) -> None:
    path = write_jsonl(
        tmp_path / "codex.jsonl",
        [
            {
                "timestamp": "2026-08-17T12:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "codex-session",
                    "cwd": "/work",
                    "cli_version": "2.3.4",
                    "timestamp": "2026-08-17T12:00:00Z",
                },
            },
            {
                "timestamp": "2026-08-17T12:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "private prompt"}],
                },
            },
            {
                "timestamp": "2026-08-17T12:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "shell",
                    "arguments": "{}",
                    "call_id": "call-1",
                },
            },
        ],
    )

    result = inspect_session(path)

    assert result.format == "codex"
    assert result.session_id == "codex-session"
    assert result.event_types == {"function_call": 1, "message": 1}
    assert result.tool_calls == 1
    assert "private prompt" not in result.to_json()


def test_detects_and_inspects_antigravity_database_without_printing_content(
    tmp_path: Path,
) -> None:
    source = claude.parse(
        Path(__file__).parent / "fixtures" / "claude-2.1.209" / "basic.jsonl"
    )
    session_id = "99999999-9999-4999-8999-999999999999"
    data, _ = antigravity.serialize(
        source,
        session_id=session_id,
        cwd=tmp_path,
        timestamp="2026-08-20T12:00:00Z",
    )
    path = tmp_path / f"{session_id}.db"
    path.write_bytes(data)

    assert inspection.detect_path_format(path) == AgentFormat.ANTIGRAVITY
    result = inspect_session(path)

    assert result.format == "antigravity"
    assert result.session_id == session_id
    assert result.records == antigravity.native_record_count(data)
    assert result.tool_calls == 1
    serialized = result.to_json()
    assert "Remember synthetic migration context" not in serialized
    assert "SYNTHETIC_TOOL_RESULT" not in serialized


def test_inspects_pi_v3_without_printing_content(tmp_path: Path) -> None:
    path = write_jsonl(
        tmp_path / "pi.jsonl",
        [
            {
                "type": "session",
                "version": 3,
                "id": "11111111-1111-4111-8111-111111111111",
                "cwd": "/work",
                "timestamp": "2026-08-18T12:00:00Z",
            },
            {
                "type": "message",
                "id": "00000001",
                "parentId": None,
                "timestamp": "2026-08-18T12:00:00Z",
                "message": {"role": "user", "content": "private pi prompt"},
            },
            {
                "type": "message",
                "id": "00000002",
                "parentId": "00000001",
                "timestamp": "2026-08-18T12:00:01Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "private pi answer"},
                        {"type": "toolCall", "id": "call", "name": "read", "arguments": {}},
                    ],
                },
            },
        ],
    )

    result = inspect_session(path)

    assert result.format == "pi"
    assert result.session_id == "11111111-1111-4111-8111-111111111111"
    assert result.roles == {"assistant": 1, "user": 1}
    assert result.content_blocks == {"text": 2, "toolCall": 1}
    assert result.tool_calls == 1
    assert "private pi" not in result.to_json()


def test_inspects_opencode_export_document_without_printing_content(tmp_path: Path) -> None:
    path = tmp_path / "opencode.json"
    path.write_text(
        json.dumps(
            {
                "info": {
                    "id": "ses_11111111111141118111111111111111",
                    "directory": "/work",
                    "title": "private title",
                    "version": "1.17.20",
                    "time": {"created": 1787054400000, "updated": 1787054401000},
                },
                "messages": [
                    {
                        "info": {
                            "id": "msg_00000000000100000000000000",
                            "sessionID": "ses_11111111111141118111111111111111",
                            "role": "user",
                            "time": {"created": 1787054400000},
                        },
                        "parts": [
                            {
                                "id": "prt_00000000000200000000000000",
                                "sessionID": "ses_11111111111141118111111111111111",
                                "messageID": "msg_00000000000100000000000000",
                                "type": "text",
                                "text": "private opencode prompt",
                            }
                        ],
                    }
                ],
            },
            indent=2,
        )
    )

    result = inspect_session(path)

    assert result.format == "opencode"
    assert result.session_id == "ses_11111111111141118111111111111111"
    assert result.record_types == {"message": 1, "session": 1}
    assert result.roles == {"user": 1}
    assert result.content_blocks == {"text": 1}
    assert "private opencode" not in result.to_json()
    assert "private title" not in result.to_json()


def test_inspects_copilot_event_log_without_printing_content(tmp_path: Path) -> None:
    path = write_jsonl(
        tmp_path / "copilot.jsonl",
        [
            {
                "type": "session.start",
                "id": "11111111-1111-4111-8111-111111111111",
                "timestamp": "2026-08-18T12:00:00Z",
                "parentId": None,
                "data": {
                    "sessionId": "22222222-2222-4222-8222-222222222222",
                    "version": 1,
                    "copilotVersion": "1.0.70",
                    "startTime": "2026-08-18T12:00:00Z",
                    "context": {"cwd": "/work"},
                },
            },
            {
                "type": "user.message",
                "id": "33333333-3333-4333-8333-333333333333",
                "timestamp": "2026-08-18T12:00:01Z",
                "parentId": "11111111-1111-4111-8111-111111111111",
                "data": {"content": "private copilot prompt"},
            },
        ],
    )

    result = inspect_session(path)

    assert result.format == "copilot"
    assert result.session_id == "22222222-2222-4222-8222-222222222222"
    assert result.roles == {"user": 1}
    assert result.content_blocks == {"text": 1}
    assert "private copilot" not in result.to_json()


def test_rejects_malformed_json_without_echoing_content(tmp_path: Path) -> None:
    path = tmp_path / "broken.jsonl"
    path.write_text('{"private": "secret"\n')

    with pytest.raises(JsonlError, match="line 1") as raised:
        inspect_session(path)

    assert "secret" not in str(raised.value)


def test_detection_scans_beyond_first_200_records(tmp_path: Path) -> None:
    records: list[dict[str, object]] = [
        {"type": "unrecognized", "ordinal": index} for index in range(250)
    ]
    records.append(
        {
            "type": "session_meta",
            "payload": {
                "id": "codex-session",
                "timestamp": "2026-08-17T12:00:00Z",
                "cwd": "/work",
            },
        }
    )
    path = write_jsonl(tmp_path / "long-codex.jsonl", records)

    assert inspect_session(path).format == "codex"


def test_detection_rejects_mixed_decisive_formats(tmp_path: Path) -> None:
    path = write_jsonl(
        tmp_path / "mixed.jsonl",
        [
            {"type": "session_meta", "payload": {"id": "codex-session"}},
            {
                "type": "user",
                "uuid": "claude-record",
                "sessionId": "claude-session",
                "message": {"role": "user", "content": "prompt"},
            },
        ],
    )

    with pytest.raises(FormatDetectionError, match="multiple native formats"):
        inspect_session(path)


def test_detection_rejects_weak_system_record(tmp_path: Path) -> None:
    path = write_jsonl(tmp_path / "weak.jsonl", [{"type": "system"}])

    with pytest.raises(FormatDetectionError, match="cannot distinguish"):
        inspect_session(path)


def test_inspection_rejects_source_change_during_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_jsonl(
        tmp_path / "changing.jsonl",
        [
            {
                "type": "user",
                "sessionId": "session",
                "message": {"role": "user", "content": "prompt"},
            }
        ],
    )
    original_hash = inspection.file_sha256

    def hash_then_append(source_path: Path) -> str:
        digest = original_hash(source_path)
        with source_path.open("a") as stream:
            stream.write("{}\n")
        return digest

    monkeypatch.setattr(inspection, "file_sha256", hash_then_append)

    with pytest.raises(JsonlError, match="source session changed"):
        inspect_session(path)
