from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import ModuleType

import pytest


def _load_sanitizer() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts/native-corpus/sanitize-pi-omp.py"
    spec = importlib.util.spec_from_file_location("sanitize_pi_omp", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _records(cwd: str, session_id: str) -> list[dict[str, object]]:
    return [
        {
            "type": "session",
            "version": 3,
            "id": session_id,
            "timestamp": "2026-08-31T12:00:00Z",
            "cwd": cwd,
        },
        {
            "type": "message",
            "id": "entry-a",
            "parentId": None,
            "timestamp": "2026-08-31T12:00:01Z",
            "message": {
                "role": "user",
                "content": f"read {cwd}/timeline.py",
                "timestamp": 1788192001000,
            },
        },
        {
            "type": "message",
            "id": "entry-b",
            "parentId": "entry-a",
            "timestamp": "2026-08-31T12:00:02Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "unchanged"}],
                "provider": "session-migrate-loopback",
                "model": "fixture-model",
                "api": "openai-completions",
                "timestamp": 1788192002000,
            },
        },
    ]


@pytest.mark.parametrize("format_name", ("pi", "omp"))
def test_pi_omp_sanitizer_changes_only_approved_values_and_preserves_linkage(
    format_name: str, tmp_path: Path
) -> None:
    sanitizer = _load_sanitizer()
    source_id = "11111111-1111-4111-8111-111111111111"
    public_id = "22222222-2222-4222-8222-222222222222"
    source_cwd = "/private/native-capture/work"
    records = _records(source_cwd, source_id)
    source = tmp_path / "source.jsonl"
    body = bytearray()
    if format_name == "omp":
        body.extend(
            sanitizer._omp_title_slot(
                {
                    "type": "title",
                    "v": 1,
                    "title": "repair-event-window-boundary",
                    "source": "user",
                    "updatedAt": "2026-08-31T12:00:00Z",
                }
            )
        )
    body.extend("".join(json.dumps(record) + "\n" for record in records).encode())
    source.write_bytes(body)
    destination = tmp_path / "public.jsonl"

    counts = sanitizer.sanitize_capture(
        source,
        destination,
        source_cwd=source_cwd,
        source_session_id=source_id,
        public_session_id=public_id,
        format_name=format_name,
    )

    lines = destination.read_bytes().splitlines(keepends=True)
    if format_name == "omp":
        assert len(lines[0]) == sanitizer.OMP_TITLE_SLOT_BYTES
        lines = lines[1:]
    sanitized = [json.loads(line) for line in lines]
    assert counts == {"cwd": 2, "uuid": 1}
    assert sanitized[0]["id"] == public_id
    assert sanitized[0]["cwd"] == sanitizer.PUBLIC_CWD
    assert sanitized[1]["message"]["content"] == "read /fixture/work/timeline.py"
    assert [record.get("id") for record in sanitized[1:]] == ["entry-a", "entry-b"]
    assert [record.get("parentId") for record in sanitized[1:]] == [None, "entry-a"]
    assert sanitized[2]["message"]["content"] == [{"type": "text", "text": "unchanged"}]
    assert os.stat(destination).st_mode & 0o777 == 0o600


def test_pi_omp_sanitizer_fails_closed_when_private_values_are_absent(tmp_path: Path) -> None:
    sanitizer = _load_sanitizer()
    source = tmp_path / "source.jsonl"
    records = _records(
        "/different/private/work",
        "11111111-1111-4111-8111-111111111111",
    )
    source.write_text("".join(json.dumps(record) + "\n" for record in records))

    with pytest.raises(RuntimeError, match="did not match"):
        sanitizer.sanitize_capture(
            source,
            tmp_path / "public.jsonl",
            source_cwd="/private/native-capture/work",
            source_session_id="11111111-1111-4111-8111-111111111111",
            public_session_id="22222222-2222-4222-8222-222222222222",
            format_name="pi",
        )
