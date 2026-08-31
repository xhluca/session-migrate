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


def _qwen_record(index: int, record_type: str, cwd: str) -> dict[str, object]:
    uuid = f"00000000-0000-4000-8000-{index:012d}"
    return {
        "uuid": uuid,
        "parentUuid": None if index == 0 else f"00000000-0000-4000-8000-{index - 1:012d}",
        "sessionId": "11111111-1111-4111-8111-111111111111",
        "timestamp": f"2026-08-31T12:00:{index:02d}Z",
        "type": record_type,
        "cwd": cwd,
        "version": "0.22.1",
    }


def test_qwen_sanitizer_preserves_graph_rejection_and_native_tools(tmp_path: Path) -> None:
    sanitizer = _load("qwen")
    cwd = "/private/qwen/work"
    records = [_qwen_record(index, "system", cwd) for index in range(11)]
    records[0]["subtype"] = "at_command"
    records[1]["type"] = "user"
    records[1]["message"] = {
        "role": "user",
        "parts": [{"text": f"[Unsupported image file. {sanitizer.IMAGE_REJECTION}.]"}],
    }
    records[4]["type"] = "assistant"
    records[4]["message"] = {
        "role": "model",
        "parts": [
            {
                "functionCall": {
                    "id": "call-1",
                    "name": "read_file",
                    "args": {"file_path": f"{cwd}/timeline.py"},
                }
            },
            {
                "functionCall": {
                    "id": "call-2",
                    "name": "read_file",
                    "args": {"file_path": f"{cwd}/CORPUS_NOTE.txt"},
                }
            },
        ],
    }
    records[10]["type"] = "assistant"
    records[10]["message"] = {"role": "model", "parts": [{"text": "done"}]}
    raw = tmp_path / "raw.jsonl"
    raw.write_text("".join(json.dumps(record) + "\n" for record in records))
    destination = tmp_path / "out/session.jsonl"

    session_id, counts = sanitizer.sanitize_chat(raw, destination, source_cwd=cwd)

    sanitized = [json.loads(line) for line in destination.read_text().splitlines()]
    assert session_id == "11111111-1111-4111-8111-111111111111"
    assert [record["uuid"] for record in sanitized] == [record["uuid"] for record in records]
    assert [record["parentUuid"] for record in sanitized] == [
        record["parentUuid"] for record in records
    ]
    assert all(record["cwd"] == sanitizer.PUBLIC_CWD for record in sanitized)
    assert sanitized[4]["message"]["parts"][0]["functionCall"]["args"]["file_path"] == (
        "/fixture/work/timeline.py"
    )
    assert sanitizer.IMAGE_REJECTION in sanitized[1]["message"]["parts"][0]["text"]
    assert counts == {"capture_cwd": 13}
    assert os.stat(destination).st_mode & 0o777 == 0o600


def test_qwen_sanitizer_rejects_unreviewed_trajectory(tmp_path: Path) -> None:
    sanitizer = _load("qwen")
    raw = tmp_path / "raw.jsonl"
    raw.write_text(json.dumps(_qwen_record(0, "user", "/private/work")) + "\n")
    with pytest.raises(RuntimeError, match="11-record"):
        sanitizer.sanitize_chat(raw, tmp_path / "out.jsonl", source_cwd="/private/work")


def _kimi_fixture(cwd: str, homedir: str) -> tuple[dict[str, object], list[dict[str, object]]]:
    state = {
        "id": "session_22222222-2222-4222-8222-222222222222",
        "version": 2,
        "cwd": cwd,
        "createdAt": 1,
        "updatedAt": 31,
        "archived": False,
        "agents": {"main": {"homedir": homedir, "type": "main"}},
        "custom": {},
        "lastTurnReason": "completed",
    }
    records: list[dict[str, object]] = [
        {"type": "metadata", "protocol_version": "1.5", "created_at": 1}
    ]
    for index in range(1, 31):
        records.append({"type": "runtime.marker", "time": index})
    records[2] = {
        "type": "profile.bind",
        "time": 2,
        "systemPrompt": f"private generated prompt for {cwd}",
        "environmentDisclosure": {"cwd": cwd},
        "agentsMdPaths": ["/tmp/AGENTS.md"],
    }
    records[5] = {
        "type": "context.append_message",
        "time": 5,
        "message": {"role": "user", "content": [{"type": "text", "text": "user"}], "toolCalls": []},
    }
    records[6] = {
        "type": "context.append_message",
        "time": 6,
        "message": {
            "role": "user",
            "origin": {"kind": "injection"},
            "content": [{"type": "text", "text": "generated injection"}],
            "toolCalls": [],
        },
    }
    for index, name in ((15, "Read"), (16, "Read")):
        records[index] = {
            "type": "context.append_loop_event",
            "time": index,
            "event": {"type": "tool.call", "name": name, "args": {"path": f"{cwd}/file"}},
        }
    return state, records


def test_kimi_sanitizer_scrubs_schema_fields_and_preserves_wire(tmp_path: Path) -> None:
    sanitizer = _load("kimi")
    cwd = "/private/kimi/work"
    homedir = "/private/kimi/session/agents/main"
    state, records = _kimi_fixture(cwd, homedir)
    source = tmp_path / "raw"
    wire = source / "agents/main/wire.jsonl"
    wire.parent.mkdir(parents=True)
    (source / "state.json").write_text(json.dumps(state))
    wire.write_text("".join(json.dumps(record) + "\n" for record in records))
    destination = tmp_path / "out"

    session_id, counts = sanitizer.sanitize_bundle(source, destination, source_cwd=cwd)

    sanitized_state = json.loads((destination / "state.json").read_text())
    sanitized_wire = destination / "agents/main/wire.jsonl"
    sanitized = [
        json.loads(line) for line in sanitized_wire.read_text().splitlines()
    ]
    assert session_id == "session_22222222-2222-4222-8222-222222222222"
    assert sanitized_state["cwd"] == sanitizer.PUBLIC_CWD
    assert sanitized_state["agents"]["main"]["homedir"] == "agents/main"
    assert sanitized[2]["systemPrompt"] == sanitizer.SYSTEM_PLACEHOLDER
    assert sanitized[2]["agentsMdPaths"] == [sanitizer.PUBLIC_AGENTS_MD]
    assert sanitized[6]["message"]["content"][0]["text"] == sanitizer.INJECTION_PLACEHOLDER
    assert [record["type"] for record in sanitized] == [record["type"] for record in records]
    assert counts == {
        "capture_paths": 6,
        "main_agent_homedir": 1,
        "system_prompt": 1,
        "injection_message": 1,
    }
    assert os.stat(destination / "state.json").st_mode & 0o777 == 0o600
    assert os.stat(destination / "agents/main/wire.jsonl").st_mode & 0o777 == 0o600


def test_kimi_sanitizer_rejects_unreviewed_record_count(tmp_path: Path) -> None:
    sanitizer = _load("kimi")
    cwd = "/private/kimi/work"
    state, records = _kimi_fixture(cwd, "/private/kimi/session/agents/main")
    source = tmp_path / "raw"
    wire = source / "agents/main/wire.jsonl"
    wire.parent.mkdir(parents=True)
    (source / "state.json").write_text(json.dumps(state))
    wire.write_text("".join(json.dumps(record) + "\n" for record in records[:-1]))
    with pytest.raises(RuntimeError, match="31-record"):
        sanitizer.sanitize_bundle(source, tmp_path / "out", source_cwd=cwd)
