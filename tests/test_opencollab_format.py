import json
from pathlib import Path

import pytest

from session_migrate.conversion import (
    ConversionOptions,
    convert_session,
    default_target_home,
    target_import_paths,
)
from session_migrate.errors import SessionMigrateError
from session_migrate.formats import opencollab
from session_migrate.model import (
    AgentFormat,
    Event,
    EventKind,
    Provenance,
    Role,
    Session,
    TargetFormat,
)

TARGET_UUID = "44444444-4444-4444-8444-444444444444"
NOW = "2026-10-09T08:00:00Z"
_PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _provenance(index: int = 0) -> Provenance:
    return Provenance(record_index=index, record_type="test")


def _session(events: tuple[Event, ...]) -> Session:
    return Session(
        source_format=AgentFormat.CODEX,
        source_path=Path("/tmp/rollout.jsonl"),
        source_sha256="0" * 64,
        session_id="11111111-2222-4333-8444-555555555555",
        cwd=Path("/tmp/session-migrate-opencollab"),
        started_at=NOW,
        cli_version="0.159.2",
        model="gpt-test",
        title="portable history",
        events=events,
        raw_record_count=len(events),
        model_provider="openai",
    )


def _portable_events() -> tuple[Event, ...]:
    return (
        Event(
            kind=EventKind.MESSAGE,
            provenance=_provenance(),
            role=Role.USER,
            timestamp=NOW,
            text="帮我修复登录问题",
        ),
        Event(
            kind=EventKind.MESSAGE,
            provenance=_provenance(1),
            role=Role.ASSISTANT,
            timestamp=NOW,
            text="好的，我先检查配置。",
        ),
        Event(
            kind=EventKind.THINKING,
            provenance=_provenance(2),
            role=Role.ASSISTANT,
            timestamp=NOW,
            text="private reasoning",
            payload={"encrypted_content": "opaque"},
        ),
        Event(
            kind=EventKind.TOOL_CALL,
            provenance=_provenance(3),
            role=Role.ASSISTANT,
            timestamp=NOW,
            tool_name="shell",
            tool_call_id="call_1",
            payload={"input": {"command": ["cat", "auth.log"]}},
        ),
        Event(
            kind=EventKind.TOOL_RESULT,
            provenance=_provenance(4),
            role=Role.TOOL,
            timestamp=NOW,
            tool_call_id="call_1",
            text="token expired",
            payload={"content_blocks": [{"type": "text", "text": "token expired"}]},
        ),
        Event(
            kind=EventKind.CONTEXT,
            provenance=_provenance(5),
            role=Role.USER,
            timestamp=NOW,
            payload={"block_type": "image", "image_url": _PNG},
        ),
        Event(
            kind=EventKind.COMPACTION,
            provenance=_provenance(6),
            role=Role.SYSTEM,
            timestamp=NOW,
            text="Earlier turns were summarized.",
            payload={"replacement_history_expanded": True},
        ),
        Event(
            kind=EventKind.TOOL_RESULT,
            provenance=_provenance(7),
            role=Role.TOOL,
            timestamp=NOW,
            tool_call_id="call_orphan",
            text="orphan output",
            payload={"content_blocks": [{"type": "text", "text": "orphan output"}]},
        ),
    )


def test_writer_emits_resumable_opencollab_snapshot() -> None:
    data, dropped = opencollab.serialize(
        _session(_portable_events()),
        session_id=TARGET_UUID,
        cwd=Path("/tmp/session-migrate-opencollab"),
    )

    opencollab.validate_native_bytes(data, TARGET_UUID)
    value = json.loads(data)
    assert value["snapshot_version"] == 1
    assert value["model"] == "gpt-test"
    roles = [message["role"] for message in value["messages"]]
    assert roles == ["user", "assistant", "assistant", "tool", "user", "system"]
    tool_call = value["messages"][2]["tool_calls"][0]
    assert tool_call == {
        "id": "call_1",
        "type": "function",
        "function": {
            "name": "shell",
            "arguments": json.dumps(
                {"command": ["cat", "auth.log"]}, ensure_ascii=False, separators=(",", ":")
            ),
        },
    }
    assert value["messages"][2]["content"] is None
    assert value["messages"][3] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "token expired",
    }
    assert value["messages"][4]["content"][0]["type"] == "image_url"
    assert value["messages"][5]["content"].startswith("[CONTEXT SUMMARY]:")
    assert dropped == {
        "thinking:private": 1,
        "thinking:provider_payload": 1,
        "tool_result:orphan_id": 1,
    }
    assert opencollab.native_record_count(data) == 1 + len(value["messages"])


def test_writer_keeps_pre_string_tool_arguments_verbatim() -> None:
    events = (
        Event(
            kind=EventKind.MESSAGE,
            provenance=_provenance(),
            role=Role.USER,
            timestamp=NOW,
            text="run it",
        ),
        Event(
            kind=EventKind.TOOL_CALL,
            provenance=_provenance(1),
            role=Role.ASSISTANT,
            timestamp=NOW,
            tool_name="exec",
            tool_call_id="call_js",
            payload={"input": "const x = 1; return x;"},
        ),
        Event(
            kind=EventKind.TOOL_RESULT,
            provenance=_provenance(2),
            role=Role.TOOL,
            timestamp=NOW,
            tool_call_id="call_js",
            text="1",
            payload={"content_blocks": [{"type": "text", "text": "1"}]},
        ),
    )

    data, dropped = opencollab.serialize(
        _session(events), session_id=TARGET_UUID, cwd=Path("/tmp/session-migrate-opencollab")
    )

    assert dropped == {}
    value = json.loads(data)
    arguments = value["messages"][1]["tool_calls"][0]["function"]["arguments"]
    assert arguments == "const x = 1; return x;"


def test_writer_rejects_history_without_portable_messages() -> None:
    events = (
        Event(
            kind=EventKind.THINKING,
            provenance=_provenance(),
            role=Role.ASSISTANT,
            timestamp=NOW,
            text="private",
        ),
    )

    with pytest.raises(SessionMigrateError, match="at least one portable message"):
        opencollab.serialize(
            _session(events), session_id=TARGET_UUID, cwd=Path("/tmp/session-migrate-opencollab")
        )


def test_validator_rejects_malformed_snapshots() -> None:
    data, _ = opencollab.serialize(
        _session(_portable_events()[:2]),
        session_id=TARGET_UUID,
        cwd=Path("/tmp/session-migrate-opencollab"),
    )
    value = json.loads(data)

    with pytest.raises(SessionMigrateError, match="not valid JSON"):
        opencollab.validate_native_bytes(b"{nope", TARGET_UUID)
    with pytest.raises(SessionMigrateError, match="must be a JSON object"):
        opencollab.validate_native_bytes(b"[1]", TARGET_UUID)
    with pytest.raises(SessionMigrateError, match="snapshot_version"):
        opencollab.validate_native_bytes(
            json.dumps({**value, "snapshot_version": 2}).encode(), TARGET_UUID
        )
    with pytest.raises(SessionMigrateError, match="no messages"):
        opencollab.validate_native_bytes(
            json.dumps({**value, "messages": []}).encode(), TARGET_UUID
        )

    bad_role = json.dumps({**value, "messages": [{"role": "developer", "content": "hi"}]}).encode()
    with pytest.raises(SessionMigrateError, match="invalid role"):
        opencollab.validate_native_bytes(bad_role, TARGET_UUID)

    missing_tool_id = json.dumps(
        {**value, "messages": [{"role": "tool", "tool_call_id": "", "content": "x"}]}
    ).encode()
    with pytest.raises(SessionMigrateError, match="tool_call_id"):
        opencollab.validate_native_bytes(missing_tool_id, TARGET_UUID)

    custom_tool_type = json.dumps(
        {
            **value,
            "messages": [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_x",
                            "type": "custom",
                            "function": {"name": "t", "arguments": "{}"},
                        }
                    ],
                }
            ],
        }
    ).encode()
    with pytest.raises(SessionMigrateError, match="typed 'function'"):
        opencollab.validate_native_bytes(custom_tool_type, TARGET_UUID)

    non_string_arguments = json.dumps(
        {
            **value,
            "messages": [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_x",
                            "type": "function",
                            "function": {"name": "t", "arguments": {"a": 1}},
                        }
                    ],
                }
            ],
        }
    ).encode()
    with pytest.raises(SessionMigrateError, match="arguments must be a string"):
        opencollab.validate_native_bytes(non_string_arguments, TARGET_UUID)

    orphan_tool = json.dumps(
        {
            **value,
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "tool", "tool_call_id": "call_missing", "content": "x"},
            ],
        }
    ).encode()
    with pytest.raises(SessionMigrateError, match="without a preceding matching tool_call"):
        opencollab.validate_native_bytes(orphan_tool, TARGET_UUID)

    unsupported_part = json.dumps(
        {
            **value,
            "messages": [
                {"role": "user", "content": [{"type": "audio", "payload": {"b64": "x"}}]},
            ],
        }
    ).encode()
    with pytest.raises(SessionMigrateError, match="unsupported content part type"):
        opencollab.validate_native_bytes(unsupported_part, TARGET_UUID)


def test_convert_session_dispatches_opencollab_target(tmp_path: Path) -> None:
    artifact = convert_session(
        _session(_portable_events()),
        ConversionOptions(
            target_format=TargetFormat.OPENCOLLAB, session_id=TARGET_UUID, cwd=tmp_path
        ),
    )

    assert artifact.target_format == TargetFormat.OPENCOLLAB
    opencollab.validate_native_bytes(artifact.native_bytes, TARGET_UUID)
    assert artifact.dropped["tool_result:orphan_id"] == 1
    assert artifact.native_record_count == opencollab.native_record_count(artifact.native_bytes)


def test_target_import_paths_partition_by_date_under_home(tmp_path: Path) -> None:
    artifact = convert_session(
        _session(_portable_events()),
        ConversionOptions(
            target_format=TargetFormat.OPENCOLLAB, session_id=TARGET_UUID, cwd=tmp_path
        ),
    )

    native_path, manifest_path = target_import_paths(artifact, tmp_path)
    assert native_path == tmp_path / "sessions" / "2026" / "10" / "09" / f"{TARGET_UUID}.json"
    assert manifest_path == tmp_path / "session-migrate" / "manifests" / f"{TARGET_UUID}.json"


def test_default_target_home_honors_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENCOLLAB_HOME", raising=False)
    assert default_target_home(TargetFormat.OPENCOLLAB) == Path.home() / ".opencollab"

    monkeypatch.setenv("OPENCOLLAB_HOME", "/tmp/opencollab-home")
    assert default_target_home(TargetFormat.OPENCOLLAB) == Path("/tmp/opencollab-home")


def test_session_relative_path_uses_utc_calendar_layout() -> None:
    path = opencollab.session_relative_path(TARGET_UUID, NOW)
    assert path == Path("sessions") / "2026" / "10" / "09" / f"{TARGET_UUID}.json"
