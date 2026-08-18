import json
from pathlib import Path

import pytest

from session_bridge import inspection
from session_bridge.errors import FormatDetectionError, JsonlError
from session_bridge.inspection import inspect_session


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

    with pytest.raises(FormatDetectionError, match="both Claude Code and Codex"):
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
