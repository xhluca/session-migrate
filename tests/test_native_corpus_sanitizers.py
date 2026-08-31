import importlib.util
import json
import os
from pathlib import Path
from types import ModuleType

import pytest


def _load_copilot_sanitizer() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts/native-corpus/sanitize-copilot.py"
    spec = importlib.util.spec_from_file_location("sanitize_copilot", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _record(record_type: str, data: dict[str, object], index: int) -> dict[str, object]:
    return {
        "type": record_type,
        "id": f"00000000-0000-4000-8000-{index:012d}",
        "parentId": None if index == 0 else f"00000000-0000-4000-8000-{index - 1:012d}",
        "timestamp": f"2026-08-31T12:00:0{index}Z",
        "data": data,
    }


def test_copilot_sanitizer_preserves_envelopes_and_replaces_only_private_text(
    tmp_path: Path,
) -> None:
    sanitizer = _load_copilot_sanitizer()
    raw = tmp_path / "events.jsonl"
    private_cwd = "/private/capture/work"
    private_image = "/private/repo/corpus-card.png"
    private_document = "/private/repo/corpus-document.pdf"
    records = [
        _record(
            "session.start",
            {
                "sessionId": "89898989-8989-4989-8989-898989898989",
                "context": {"cwd": private_cwd},
            },
            0,
        ),
        _record("system.message", {"content": f"secret runtime at {private_cwd}"}, 1),
        _record(
            "user.message",
            {
                "content": "SM_CORPUS_7319",
                "attachments": [
                    {"path": private_image},
                    {
                        "path": private_document,
                        "taggedFilesEntry": f"* {private_document} (3 lines)",
                    },
                ],
            },
            2,
        ),
    ]
    raw.write_text("".join(json.dumps(record) + "\n" for record in records))
    destination = tmp_path / "out/events.jsonl"

    session_id, counts, system_count = sanitizer.sanitize_events(
        raw,
        destination,
        source_cwd=private_cwd,
        source_image=private_image,
        source_document=private_document,
    )

    sanitized = [json.loads(line) for line in destination.read_text().splitlines()]
    assert session_id == "89898989-8989-4989-8989-898989898989"
    assert [record["id"] for record in sanitized] == [record["id"] for record in records]
    assert [record["parentId"] for record in sanitized] == [
        record["parentId"] for record in records
    ]
    assert sanitized[0]["data"]["context"]["cwd"] == sanitizer.PUBLIC_CWD
    assert sanitized[1]["data"]["content"] == sanitizer.SYSTEM_PLACEHOLDER
    assert sanitized[2]["data"]["content"] == "SM_CORPUS_7319"
    assert sanitized[2]["data"]["attachments"][0]["path"] == sanitizer.PUBLIC_IMAGE
    assert sanitized[2]["data"]["attachments"][1]["path"] == sanitizer.PUBLIC_DOCUMENT
    assert (
        sanitized[2]["data"]["attachments"][1]["taggedFilesEntry"]
        == f"* {sanitizer.PUBLIC_DOCUMENT} (3 lines)"
    )
    assert counts == {private_cwd: 2, private_image: 1, private_document: 2}
    assert system_count == 1
    assert os.stat(destination).st_mode & 0o777 == 0o600


def test_copilot_sanitizer_fails_when_expected_private_path_is_absent(tmp_path: Path) -> None:
    sanitizer = _load_copilot_sanitizer()
    raw = tmp_path / "events.jsonl"
    records = [
        _record(
            "session.start",
            {
                "sessionId": "89898989-8989-4989-8989-898989898989",
                "context": {"cwd": "/private/capture/work"},
            },
            0,
        ),
        _record("system.message", {"content": "runtime"}, 1),
    ]
    raw.write_text("".join(json.dumps(record) + "\n" for record in records))

    with pytest.raises(RuntimeError, match="capture paths did not match"):
        sanitizer.sanitize_events(
            raw,
            tmp_path / "out/events.jsonl",
            source_cwd="/private/capture/work",
            source_image="/private/repo/corpus-card.png",
            source_document="/private/repo/corpus-document.pdf",
        )
