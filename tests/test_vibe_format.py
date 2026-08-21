import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from session_migrate.conversion import (
    ConversionOptions,
    convert_session,
    install_vibe_artifact,
    load_session,
    target_import_paths,
)
from session_migrate.errors import SessionMigrateError
from session_migrate.formats import claude, vibe
from session_migrate.inspection import detect_path_format, inspect_session
from session_migrate.model import AgentFormat, Event, EventKind, Provenance, Role, TargetFormat

FIXTURES = Path(__file__).parent / "fixtures"
SESSION_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _artifact(tmp_path: Path):
    source = claude.parse(FIXTURES / "claude-2.1.209" / "basic.jsonl")
    return convert_session(
        source,
        ConversionOptions(
            target_format=TargetFormat.VIBE,
            session_id=SESSION_ID,
            cwd=tmp_path,
        ),
    )


def test_vibe_bundle_install_parse_and_inspect(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    parsed_bundle = vibe.validate_native_bytes(artifact.native_bytes, SESSION_ID)

    assert parsed_bundle.meta["session_id"] == SESSION_ID
    assert parsed_bundle.meta["total_messages"] == len(parsed_bundle.messages)
    assert artifact.native_record_count == len(parsed_bundle.messages)

    messages_path, manifest_path = install_vibe_artifact(
        artifact, target_home=tmp_path / "vibe-home"
    )
    session = vibe.parse_session(messages_path)

    assert messages_path.name == "messages.jsonl"
    assert (messages_path.parent / "meta.json").is_file()
    assert oct(messages_path.stat().st_mode & 0o777) == "0o600"
    assert oct(messages_path.parent.stat().st_mode & 0o777) == "0o700"
    assert manifest_path.is_file()
    assert session.source_format == AgentFormat.VIBE
    assert session.session_id == SESSION_ID
    assert any(event.kind == EventKind.TOOL_CALL for event in session.events)
    assert any(event.kind == EventKind.TOOL_RESULT for event in session.events)
    assert any(event.kind == EventKind.COMPACTION for event in session.events)
    assert detect_path_format(messages_path) == AgentFormat.VIBE
    assert detect_path_format(messages_path.parent) == AgentFormat.VIBE
    assert inspect_session(messages_path).format == "vibe"
    assert load_session(messages_path).session_id == SESSION_ID


def test_sanitized_vibe_native_fixture_projects_portable_history() -> None:
    session = vibe.parse_session(FIXTURES / "vibe-2.24.3/basic")

    assert session.session_id == SESSION_ID
    assert session.title == "Synthetic Vibe fixture"
    assert session.raw_record_count == 7
    assert [event.kind for event in session.events] == [
        EventKind.MESSAGE,
        EventKind.CONTEXT,
        EventKind.MESSAGE,
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
        EventKind.MESSAGE,
        EventKind.COMPACTION,
        EventKind.MESSAGE,
        EventKind.MESSAGE,
    ]


def test_vibe_reads_the_configured_model_provider_without_assuming_mistral(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "vibe"
    fixture.mkdir()
    meta = json.loads((FIXTURES / "vibe-2.24.3/basic/meta.json").read_text())
    meta["config"] = {
        "active_model": "fixture-alias",
        "models": {
            "fixture-alias": {
                "alias": "fixture-alias",
                "name": "fixture-model",
                "provider": "openai-compatible",
            }
        },
    }
    (fixture / vibe.META_FILENAME).write_text(json.dumps(meta))
    (fixture / vibe.MESSAGES_FILENAME).write_bytes(
        (FIXTURES / "vibe-2.24.3/basic/messages.jsonl").read_bytes()
    )

    session = vibe.parse_session(fixture)

    assert session.model == "fixture-model"
    assert session.model_provider == "openai-compatible"


def test_vibe_preserves_tools_images_compaction_and_its_readable_reasoning(
    tmp_path: Path,
) -> None:
    source = claude.parse(FIXTURES / "claude-2.1.209" / "basic.jsonl")
    artifact = _artifact(tmp_path)
    meta_bytes, message_bytes = vibe.native_files(artifact.native_bytes, SESSION_ID)
    directory = tmp_path / "native"
    directory.mkdir()
    (directory / vibe.META_FILENAME).write_bytes(meta_bytes)
    (directory / vibe.MESSAGES_FILENAME).write_bytes(message_bytes)
    reparsed = vibe.parse_session(directory)

    source_kinds = {event.kind for event in source.events}
    target_kinds = {event.kind for event in reparsed.events}
    assert EventKind.TOOL_CALL in source_kinds <= target_kinds
    assert EventKind.TOOL_RESULT in target_kinds
    assert EventKind.CONTEXT in target_kinds
    assert EventKind.COMPACTION in target_kinds

    # Vibe has a native readable reasoning field even though most writers omit it.
    value = json.loads(artifact.native_bytes)
    value["messages"][1]["reasoning_content"] = "Readable reasoning"
    value["messages"][1]["reasoning_message_id"] = "reason-1"
    value["meta"]["last_message_fingerprint"] = vibe._message_fingerprint(  # noqa: SLF001
        value["messages"][-1]
    )
    modified = (json.dumps(value, sort_keys=True) + "\n").encode()
    meta_bytes, message_bytes = vibe.native_files(modified, SESSION_ID)
    (directory / vibe.META_FILENAME).write_bytes(meta_bytes)
    (directory / vibe.MESSAGES_FILENAME).write_bytes(message_bytes)
    reparsed = vibe.parse_session(directory)
    assert any(
        event.kind == EventKind.THINKING and event.text == "Readable reasoning"
        for event in reparsed.events
    )
    rewritten = convert_session(
        reparsed,
        ConversionOptions(
            target_format=TargetFormat.VIBE,
            session_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            cwd=tmp_path,
        ),
    )
    rewritten_messages = json.loads(rewritten.native_bytes)["messages"]
    assert any(
        message.get("reasoning_content") == "Readable reasoning" for message in rewritten_messages
    )


def test_vibe_does_not_promote_other_agents_thinking_or_double_count_system(
    tmp_path: Path,
) -> None:
    source = claude.parse(FIXTURES / "claude-2.1.209" / "basic.jsonl")
    source = replace(
        source,
        events=(
            Event(
                kind=EventKind.MESSAGE,
                role=Role.SYSTEM,
                text="Privileged fixture",
                provenance=Provenance(-2, "system"),
            ),
            Event(
                kind=EventKind.THINKING,
                role=Role.ASSISTANT,
                text="Private fixture reasoning",
                provenance=Provenance(-1, "thinking"),
            ),
            *source.events,
        ),
    )
    artifact = convert_session(
        source,
        ConversionOptions(
            target_format=TargetFormat.VIBE,
            session_id=SESSION_ID,
            cwd=tmp_path,
        ),
    )

    assert artifact.dropped["message:privileged_role"] == 1
    assert artifact.dropped["thinking:private"] == 1
    assert json.loads(artifact.native_bytes)["meta"]["system_prompt"] is None
    assert b"Privileged fixture" not in artifact.native_bytes
    assert not any(
        message.get("reasoning_content") == "Private fixture reasoning"
        for message in json.loads(artifact.native_bytes)["messages"]
    )


def test_vibe_reports_known_runtime_and_unknown_message_fields(tmp_path: Path) -> None:
    fixture = tmp_path / "vibe"
    fixture.mkdir()
    (fixture / vibe.META_FILENAME).write_bytes(
        (FIXTURES / "vibe-2.24.3/basic/meta.json").read_bytes()
    )
    records = [
        json.loads(line)
        for line in (FIXTURES / "vibe-2.24.3/basic/messages.jsonl").read_text().splitlines()
    ]
    records[0]["input_text"] = "display-only duplicate"
    records[0]["resources"] = []
    records[0]["future_field"] = {"opaque": True}
    (fixture / vibe.MESSAGES_FILENAME).write_text(
        "".join(json.dumps(record) + "\n" for record in records)
    )

    session = vibe.parse_session(fixture)
    reasons = [
        event.payload.get("reason") for event in session.events if event.kind == EventKind.OPAQUE
    ]
    assert reasons.count("vibe_input_text") == 1
    assert reasons.count("vibe_resources") == 1
    assert reasons.count("vibe_unknown_message_field") == 1


def test_vibe_dry_run_collision_and_malformed_bundles_fail_closed(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    messages_path, manifest_path = install_vibe_artifact(
        artifact, target_home=tmp_path / "home", dry_run=True
    )
    assert not messages_path.exists()
    assert not manifest_path.exists()

    messages_path.parent.mkdir(parents=True)
    with pytest.raises(SessionMigrateError, match="overwrite"):
        install_vibe_artifact(artifact, target_home=tmp_path / "home", dry_run=True)

    value = json.loads(artifact.native_bytes)
    value["meta"]["session_id"] = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    with pytest.raises(SessionMigrateError, match="linkage"):
        vibe.validate_native_bytes(json.dumps(value).encode(), SESSION_ID)

    value = json.loads(artifact.native_bytes)
    value["messages"][0]["role"] = "developer"
    with pytest.raises(SessionMigrateError, match="invalid role"):
        vibe.validate_native_bytes(json.dumps(value).encode(), SESSION_ID)


def test_vibe_paths_and_environment_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIBE_HOME", str(tmp_path / "configured"))
    assert vibe.vibe_home() == tmp_path / "configured"
    artifact = _artifact(tmp_path)
    native, _manifest = target_import_paths(artifact, vibe.vibe_home())
    assert native == (
        tmp_path / "configured/logs/session/session_20260817_120000_aaaaaaaa/messages.jsonl"
    )
    assert os.path.commonpath([native, tmp_path]) == str(tmp_path)
