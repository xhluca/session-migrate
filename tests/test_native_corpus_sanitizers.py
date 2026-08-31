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


def _load_openhands_sanitizer() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts/native-corpus/sanitize-openhands.py"
    spec = importlib.util.spec_from_file_location("sanitize_openhands", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_grok_sanitizer() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts/native-corpus/sanitize-grok.py"
    spec = importlib.util.spec_from_file_location("sanitize_grok", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_vibe_sanitizer() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts/native-corpus/sanitize-vibe.py"
    spec = importlib.util.spec_from_file_location("sanitize_vibe", path)
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


def test_openhands_sanitizer_preserves_native_events_and_excludes_runtime_state(
    tmp_path: Path,
) -> None:
    sanitizer = _load_openhands_sanitizer()
    session_hex = "99999999999949998999999999999999"
    conversation = tmp_path / "raw" / session_hex
    events = conversation / "events"
    events.mkdir(parents=True)
    (conversation / "base_state.json").write_text('{"secret_registry":{}}\n')
    private_cwd = "/private/work"
    private_runtime = "/private/runtime"
    records = [
        {
            "id": "11111111-1111-4111-8111-111111111111",
            "timestamp": "2026-08-31T12:00:00Z",
            "source": "agent",
            "system_prompt": {
                "cache_prompt": True,
                "type": "text",
                "text": f"vendor prompt for {private_cwd}",
            },
            "tools": [],
            "dynamic_context": {
                "cache_prompt": False,
                "type": "text",
                "text": f"runtime at {private_runtime}",
            },
            "kind": "SystemPromptEvent",
        },
        {
            "id": "22222222-2222-4222-8222-222222222222",
            "timestamp": "2026-08-31T12:00:01Z",
            "source": "user",
            "llm_message": {"role": "user", "content": "SM_CORPUS_7319"},
            "kind": "MessageEvent",
            "metadata": {"username": "private-user", "hostname": "private-host"},
        },
    ]
    for index, record in enumerate(records):
        path = events / f"event-{index:05d}-{record['id']}.json"
        path.write_text(json.dumps(record) + "\n")

    session_id, written, mutations = sanitizer.sanitize_conversation(
        conversation,
        tmp_path / "sanitized",
        source_cwd=private_cwd,
        source_runtime=private_runtime,
        source_username="private-user",
        source_hostname="private-host",
    )

    assert session_id == "99999999-9999-4999-8999-999999999999"
    assert len(written) == 2
    sanitized = [json.loads(path.read_text()) for path in written]
    assert [item["id"] for item in sanitized] == [item["id"] for item in records]
    assert sanitized[0]["system_prompt"]["text"] == sanitizer.SYSTEM_PLACEHOLDER
    assert sanitized[0]["dynamic_context"]["text"] == sanitizer.DYNAMIC_PLACEHOLDER
    assert sanitized[1]["llm_message"]["content"] == "SM_CORPUS_7319"
    assert sanitized[1]["metadata"] == {
        "username": sanitizer.PUBLIC_USERNAME,
        "hostname": sanitizer.PUBLIC_HOSTNAME,
    }
    assert mutations == {
        "base_state_excluded": 1,
        "capture_cwd": 1,
        "capture_runtime": 1,
        "capture_username": 1,
        "capture_hostname": 1,
        "system_prompt": 1,
        "dynamic_context": 1,
    }
    assert not (written[0].parents[1] / "base_state.json").exists()
    assert all(os.stat(path).st_mode & 0o777 == 0o600 for path in written)


def test_openhands_sanitizer_rejects_noncontiguous_event_sequence(tmp_path: Path) -> None:
    sanitizer = _load_openhands_sanitizer()
    conversation = tmp_path / "99999999999949998999999999999999"
    events = conversation / "events"
    events.mkdir(parents=True)
    (conversation / "base_state.json").write_text("{}\n")
    (events / "event-00001-11111111-1111-4111-8111-111111111111.json").write_text(
        "{}\n"
    )

    with pytest.raises(RuntimeError, match="contiguous native sequence"):
        sanitizer.sanitize_conversation(
            conversation,
            tmp_path / "sanitized",
            source_cwd="/private/work",
            source_runtime="/private/runtime",
            source_username="private-user",
            source_hostname="private-host",
        )


def test_grok_sanitizer_preserves_update_linkage_and_replaces_paths(tmp_path: Path) -> None:
    sanitizer = _load_grok_sanitizer()
    session_id = "95959595-9595-4959-8959-959595959595"
    source = tmp_path / "raw" / session_id
    source.mkdir(parents=True)
    private_cwd = "/private/work"
    private_home = "/private/grok-home"
    (source / "summary.json").write_text(
        json.dumps(
            {
                "info": {"id": session_id, "cwd": private_cwd},
                "grok_home": private_home,
                "generated_title": "repair-event-window-boundary",
            }
        )
    )
    update = {
        "timestamp": 1788219000,
        "method": "session/update",
        "params": {
            "sessionId": session_id,
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "call-audio",
                "status": "failed",
                "content": [
                    {
                        "type": "content",
                        "content": {
                            "type": "text",
                            "text": f"Cannot read {private_cwd}/corpus-tone.wav",
                        },
                    }
                ],
            },
        },
    }
    (source / "updates.jsonl").write_text(json.dumps(update) + "\n")

    actual_id, files, mutations = sanitizer.sanitize_session(
        source,
        tmp_path / "sanitized",
        source_cwd=private_cwd,
        source_home=private_home,
    )

    assert actual_id == session_id
    summary = json.loads(files[0].read_text())
    sanitized_update = json.loads(files[1].read_text())
    assert summary["info"]["cwd"] == sanitizer.PUBLIC_CWD
    assert summary["grok_home"] == sanitizer.PUBLIC_HOME
    assert sanitized_update["params"]["sessionId"] == session_id
    assert sanitizer.PUBLIC_CWD in json.dumps(sanitized_update)
    assert mutations == {"capture_cwd": 2, "capture_home": 1}
    assert all(os.stat(path).st_mode & 0o777 == 0o600 for path in files)


def test_grok_sanitizer_rejects_wrong_update_session(tmp_path: Path) -> None:
    sanitizer = _load_grok_sanitizer()
    session_id = "95959595-9595-4959-8959-959595959595"
    source = tmp_path / session_id
    source.mkdir()
    (source / "summary.json").write_text(
        json.dumps({"info": {"id": session_id, "cwd": "/private/work"}})
    )
    (source / "updates.jsonl").write_text(
        json.dumps(
            {
                "params": {
                    "sessionId": "96969696-9696-4969-8969-969696969696",
                    "update": {"sessionUpdate": "user_message_chunk"},
                }
            }
        )
        + "\n"
    )

    with pytest.raises(RuntimeError, match="invalid session linkage"):
        sanitizer.sanitize_session(
            source,
            tmp_path / "sanitized",
            source_cwd="/private/work",
            source_home="/private/grok-home",
        )


def test_vibe_sanitizer_replaces_runtime_state_and_copies_image(tmp_path: Path) -> None:
    sanitizer = _load_vibe_sanitizer()
    session_id = "76767676-7676-4676-8676-767676767676"
    private_cwd = "/private/work"
    private_home = str(tmp_path / "private-vibe")
    source = Path(private_home) / "logs/session/session_20260831_235533_76767676"
    attachment = source / "attachments/image.png"
    attachment.parent.mkdir(parents=True)
    attachment.write_bytes(b"native-image")
    messages = [
        {
            "role": "user",
            "content": f"Inspect {private_cwd}/image.png",
            "injected": False,
            "message_id": "message-1",
            "images": [
                {
                    "source": {
                        "kind": "file",
                        "path": f"{private_home}/logs/session/{source.name}/attachments/image.png",
                    },
                    "alias": "image.png",
                    "mime_type": "image/png",
                }
            ],
        }
    ]
    (source / "meta.json").write_text(
        json.dumps(
            {
                "session_id": session_id,
                "total_messages": 1,
                "username": "private-user",
                "environment": {"working_directory": private_cwd},
                "config": {"session_logging": {"save_dir": f"{private_home}/logs/session"}},
                "system_prompt": {"content": f"Runtime prompt for {private_cwd}"},
                "tools_available": [{"name": "private-runtime-tool"}],
            }
        )
    )
    (source / "messages.jsonl").write_text(json.dumps(messages[0]) + "\n")

    actual_id, files, mutations = sanitizer.sanitize_session(
        source,
        tmp_path / "sanitized",
        source_cwd=private_cwd,
        source_home=private_home,
        source_username="private-user",
    )

    assert actual_id == session_id
    meta = json.loads(files[0].read_text())
    message = json.loads(files[1].read_text())
    assert meta["username"] == sanitizer.PUBLIC_USERNAME
    assert meta["system_prompt"] is None
    assert meta["config"] == sanitizer.PUBLIC_CONFIG
    assert meta["tools_available"] == []
    assert meta["environment"]["working_directory"] == sanitizer.PUBLIC_CWD
    assert message["images"][0]["source"]["path"] == "attachments/image.png"
    assert files[2].read_bytes() == b"native-image"
    assert mutations == {
        "capture_cwd": 3,
        "capture_home": 2,
        "image_path": 1,
        "runtime_config": 1,
        "system_prompt": 1,
        "tool_inventory": 1,
        "username": 1,
    }
    assert all(os.stat(path).st_mode & 0o777 == 0o600 for path in files)


def test_vibe_sanitizer_rejects_attachment_outside_session(tmp_path: Path) -> None:
    sanitizer = _load_vibe_sanitizer()
    session_id = "76767676-7676-4676-8676-767676767676"
    source = tmp_path / "session"
    source.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"private")
    (source / "meta.json").write_text(
        json.dumps(
            {
                "session_id": session_id,
                "total_messages": 1,
                "username": "private-user",
                "environment": {"working_directory": "/private/work"},
                "config": {"save": "/private/vibe"},
                "system_prompt": {"content": "private prompt"},
                "tools_available": [],
            }
        )
    )
    (source / "messages.jsonl").write_text(
        json.dumps(
            {
                "role": "user",
                "content": "message",
                "images": [
                    {
                        "source": {"kind": "file", "path": str(outside)},
                        "alias": "outside.png",
                        "mime_type": "image/png",
                    }
                ],
            }
        )
        + "\n"
    )

    with pytest.raises(RuntimeError, match="escapes the session directory"):
        sanitizer.sanitize_session(
            source,
            tmp_path / "sanitized",
            source_cwd="/private/work",
            source_home="/private/vibe",
            source_username="private-user",
        )
