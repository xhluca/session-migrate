"""Focused tests for the exact Claude/Codex native-corpus sanitizers."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import ModuleType

import pytest


def _load(name: str) -> ModuleType:
    path = Path(__file__).parents[1] / f"scripts/native-corpus/sanitize-{name}.py"
    spec = importlib.util.spec_from_file_location(f"sanitize_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _claude_record(
    index: int,
    record_type: str,
    *,
    source_cwd: str,
    content: object | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "type": record_type,
        "sessionId": "11111111-1111-4111-8111-111111111111",
    }
    if record_type in {"user", "assistant"}:
        value.update(
            {
                "uuid": f"22222222-2222-4222-8222-{index:012d}",
                "parentUuid": None,
                "timestamp": f"2026-08-31T12:00:{index:02d}Z",
                "version": "2.1.209",
                "cwd": source_cwd,
                "message": {"role": record_type, "content": content or []},
            }
        )
    return value


def test_claude_sanitizer_rewrites_paths_and_generated_runtime_attachment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sanitizer = _load("claude")
    monkeypatch.setattr(sanitizer, "EXPECTED_RECORDS", 4)
    source_root = "/private/capture"
    source_cwd = f"{source_root}/work"
    records = [
        {
            "type": "attachment",
            "sessionId": "11111111-1111-4111-8111-111111111111",
            "attachment": {"type": "deferred_tools_delta", "addedLines": ["secret prompt"]},
        },
        _claude_record(
            1,
            "user",
            source_cwd=source_cwd,
            content=[
                {"type": "text", "text": "SM_CORPUS_7319"},
                {"type": "image", "source": {"type": "base64", "data": "aW1hZ2U="}},
                {"type": "document", "source": {"type": "base64", "data": "cGRm"}},
            ],
        ),
        _claude_record(
            2,
            "assistant",
            source_cwd=source_cwd,
            content=[
                {"type": "tool_use", "id": "a", "name": "Read", "input": {}},
                {"type": "tool_use", "id": "b", "name": "Read", "input": {}},
                {"type": "tool_use", "id": "c", "name": "Read", "input": {}},
            ],
        ),
        _claude_record(
            3,
            "user",
            source_cwd=source_cwd,
            content=[
                {"type": "tool_result", "tool_use_id": "a", "content": source_cwd},
                {"type": "tool_result", "tool_use_id": "b", "content": "ok"},
                {"type": "tool_result", "tool_use_id": "c", "content": "missing"},
            ],
        ),
    ]
    raw = tmp_path / "raw.jsonl"
    raw.write_text("".join(json.dumps(item) + "\n" for item in records))
    output = tmp_path / "out/native.jsonl"
    monkeypatch.setattr(
        sanitizer,
        "_replace",
        sanitizer._replace,
    )

    # The production capture has 21 generated lines and >=20 path mutations;
    # use the same behavioral code while relaxing only those trajectory totals.
    original = sanitizer.sanitize_transcript
    monkeypatch.setattr(sanitizer, "EXPECTED_RECORDS", 4)
    text = Path(sanitizer.__file__).read_text()
    assert 'runtime_attachment_lines"] != 21' in text
    # Pad the synthetic shape to the reviewed totals without weakening what is
    # asserted about the transformed values.
    records[0]["attachment"]["addedLines"] = ["secret prompt"] * 21  # type: ignore[index]
    for record in records[1:]:
        record["extra"] = [source_cwd] * 7
    raw.write_text("".join(json.dumps(item) + "\n" for item in records))
    session_id, counts = original(raw, output, source_cwd=source_cwd, source_root=source_root)

    sanitized = [json.loads(line) for line in output.read_text().splitlines()]
    assert session_id == "11111111-1111-4111-8111-111111111111"
    assert sanitized[0]["attachment"]["addedLines"] == [sanitizer.RUNTIME_PLACEHOLDER] * 21
    assert source_root not in output.read_text()
    assert sanitizer.PUBLIC_CWD in output.read_text()
    assert counts["runtime_attachment_lines"] == 21
    assert os.stat(output).st_mode & 0o777 == 0o600


def test_claude_sanitizer_rejects_unknown_runtime_attachment_shape(tmp_path: Path) -> None:
    sanitizer = _load("claude")
    raw = tmp_path / "raw.jsonl"
    raw.write_text(
        json.dumps(
            {
                "type": "attachment",
                "sessionId": "11111111-1111-4111-8111-111111111111",
                "attachment": {"type": "deferred_tools_delta", "addedLines": "private"},
            }
        )
        + "\n"
    )
    with pytest.raises(RuntimeError, match="unknown schema"):
        sanitizer.sanitize_transcript(
            raw,
            tmp_path / "out.jsonl",
            source_cwd="/private/capture/work",
            source_root="/private/capture",
        )


def test_codex_helper_redacts_instruction_blocks_and_account_values() -> None:
    sanitizer = _load("codex")
    message = {
        "content": [
            {"type": "input_text", "text": "private one"},
            {"type": "input_text", "text": "private two"},
        ]
    }
    assert sanitizer._replace_text_blocks(message, "PUBLIC") == 2
    assert [block["text"] for block in message["content"]] == ["PUBLIC", "PUBLIC"]
    assert sanitizer._redact_rate_limits(
        {"plan": "private", "used": 42.5, "active": True, "nested": [7, None]}
    ) == {
        "plan": sanitizer.ACCOUNT_PLACEHOLDER,
        "used": 0,
        "active": True,
        "nested": [0, None],
    }


def test_codex_sanitizer_rejects_nonreviewed_record_count(tmp_path: Path) -> None:
    sanitizer = _load("codex")
    raw = tmp_path / "rollout.jsonl"
    raw.write_text('{"timestamp":"2026-08-31T12:00:00Z","type":"session_meta","payload":{}}\n')
    with pytest.raises(RuntimeError, match="reviewed 34-record"):
        sanitizer.sanitize_rollout(
            raw,
            tmp_path / "out.jsonl",
            source_cwd="/private/capture/work",
            source_root="/private/capture",
        )
