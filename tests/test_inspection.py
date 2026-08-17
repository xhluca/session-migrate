import json
from pathlib import Path

import pytest

from session_bridge.errors import JsonlError
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

