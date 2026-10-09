"""Portable v2 transfers, without native database writes or real providers."""

import json
from pathlib import Path

import pytest

from session_migrate import conversion
from session_migrate.conversion import ConversionOptions, convert_session, load_session
from session_migrate.errors import SessionMigrateError
from session_migrate.formats import opencode
from session_migrate.model import AgentFormat, Event, EventKind, Provenance, Role, TargetFormat

FIXTURES = Path(__file__).parent / "fixtures"


def artifact(tmp_path: Path, release: str = "2.0.23"):
    session = load_session(FIXTURES / "codex-0.144.4/basic.jsonl", AgentFormat.CODEX)
    return convert_session(
        session,
        ConversionOptions(
            target_format=TargetFormat.OPENCODE,
            target_cli_version=release,
            cwd=tmp_path,
            model_provider="fixture",
            model="test",
        ),
    )


def test_streamed_tool_source_retains_partial_input_through_portable_target(tmp_path):
    partial = '{"command":"UNIQUE_PARTIAL_INPUT'
    value = json.loads(artifact(tmp_path).native_bytes)
    tool = next(
        c
        for m in value["messages"]
        if m["type"] == "assistant"
        for c in m["content"]
        if c["type"] == "tool"
    )
    tool["state"] = {"status": "streaming", "input": partial}
    source_path = tmp_path / "streaming-source.json"
    source_path.write_text(json.dumps(value))
    source = opencode.parse_session(source_path)
    call = next(e for e in source.events if e.kind == EventKind.TOOL_CALL)
    assert call.payload["input"] == {"input": partial}
    target = convert_session(
        source,
        ConversionOptions(
            target_format=TargetFormat.OPENCODE,
            target_cli_version="2.0.23",
            cwd=tmp_path,
            model_provider="fixture",
            model="test",
        ),
    )
    exported = json.loads(target.native_bytes)
    archived = next(
        c
        for m in exported["messages"]
        if m["type"] == "assistant"
        for c in m["content"]
        if c["type"] == "tool"
    )
    assert archived["id"] == tool["id"]
    assert archived["state"]["input"] == {"input": partial}
    assert archived["state"]["status"] == "error"
    assert archived["state"]["error"]["type"] == "ImportedIncompleteTool"
    assert target.dropped["tool_call:v2_incomplete_archived"] == 1


@pytest.mark.parametrize(
    ("release", "validated", "warning_expected"),
    [
        ("1.17.20", "1.17.20", False),
        ("2.0.22", "2.0.23", False),
        ("2.0.23", "2.0.23", False),
        ("opencode v2.0.23", "2.0.23", False),
        ("2.0.24", "2.0.23", True),
        ("2.0.23-gateway.custom", "2.0.23", True),
    ],
)
def test_target_version_warning_matches_selected_schema(
    tmp_path, release, validated, warning_expected
):
    session = load_session(FIXTURES / "codex-0.144.4/basic.jsonl", AgentFormat.CODEX)
    target = convert_session(
        session,
        ConversionOptions(
            target_format=TargetFormat.OPENCODE,
            target_cli_version=release,
            cwd=tmp_path,
            model_provider="fixture",
            model="test",
        ),
    )
    warnings = [w for w in target.warnings if w["code"] == "unvalidated_target_version"]
    assert bool(warnings) is warning_expected
    if warning_expected:
        assert warnings[0]["validated"] == validated
        assert warnings[0]["observed"] == release
        assert "selected OpenCode 2.0 transfer schema" in warnings[0]["message"]
        assert "remains pinned" not in warnings[0]["message"]
    native = json.loads(target.native_bytes)
    assert ("location" in native["info"]) is opencode.is_v2(release)


def test_v2_flat_schema_portable_roundtrip(tmp_path):
    target = artifact(tmp_path)
    value = json.loads(target.native_bytes)
    assert value["info"]["location"] == {"directory": str(tmp_path)}
    assert value["info"]["model"] == {"id": "test", "providerID": "fixture"}
    assert all("info" not in m and "parts" not in m for m in value["messages"])
    opencode.validate_native_bytes(target.native_bytes, target.session_id)
    path = tmp_path / "bundle.json"
    path.write_bytes(target.native_bytes)
    parsed = opencode.parse_import(path)
    source = load_session(FIXTURES / "codex-0.144.4/basic.jsonl", AgentFormat.CODEX)
    assert [e.text for e in parsed.events if e.kind == EventKind.MESSAGE] == [
        e.text for e in source.events if e.kind == EventKind.MESSAGE
    ]
    assert [e.tool_call_id for e in parsed.events if e.kind == EventKind.TOOL_CALL] == [
        e.tool_call_id for e in source.events if e.kind == EventKind.TOOL_CALL
    ]


def test_freeform_tool_input_is_wrapped_without_losing_call_or_result(tmp_path):
    from dataclasses import replace

    session = load_session(FIXTURES / "codex-0.144.4/basic.jsonl", AgentFormat.CODEX)
    original = next(e for e in session.events if e.kind == EventKind.TOOL_CALL)
    freeform = "*** Begin Patch\n+Unicode: é λ\n*** End Patch\n"
    session = replace(
        session,
        events=tuple(
            replace(e, payload={"input": freeform}) if e is original else e for e in session.events
        ),
    )
    target = convert_session(
        session,
        ConversionOptions(
            target_format=TargetFormat.OPENCODE,
            target_cli_version="2.0.23",
            cwd=tmp_path,
            model_provider="fixture",
            model="test",
        ),
    )
    native = json.loads(target.native_bytes)
    tool = next(
        item
        for message in native["messages"]
        if message["type"] == "assistant"
        for item in message["content"]
        if item["type"] == "tool" and item["id"] == original.tool_call_id
    )
    assert tool["state"]["input"] == {"input": freeform}
    assert tool["state"]["status"] == "completed"
    result = next(e for e in session.events if e.kind == EventKind.TOOL_RESULT)
    assert [item for item in tool["state"]["content"] if item["type"] == "text"] == [
        {"type": "text", "text": result.text}
    ]
    assert target.dropped["tool_call:non_object_input"] == 1


def test_cli_help_explains_opencode_schema_selection(capsys):
    from session_migrate.cli import build_parser

    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["convert", "--help"])
    assert exc.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "selects legacy or 2.0 transfer schema for OpenCode" in help_text
    assert "metadata version only; the writer schema remains pinned" not in help_text


def test_v2_tools_images_compaction_and_incomplete_call(tmp_path):
    from dataclasses import replace

    base = load_session(FIXTURES / "codex-0.144.4/basic.jsonl", AgentFormat.CODEX)
    image = "data:image/png;base64,aGVsbG8="
    events = (
        Event(
            kind=EventKind.MESSAGE, role=Role.USER, text="look", provenance=Provenance(1, "user")
        ),
        Event(
            kind=EventKind.CONTEXT,
            role=Role.USER,
            payload={"block_type": "image", "image_url": image},
            provenance=Provenance(1, "user"),
        ),
        Event(
            kind=EventKind.TOOL_CALL,
            role=Role.ASSISTANT,
            tool_call_id="call_one",
            tool_name="read",
            payload={"input": {"path": "a"}},
            provenance=Provenance(2, "assistant"),
        ),
        Event(
            kind=EventKind.TOOL_RESULT,
            role=Role.TOOL,
            tool_call_id="call_one",
            text="result",
            payload={
                "content_blocks": [
                    {"type": "text", "text": "result"},
                    {"type": "image", "image_url": image},
                ]
            },
            provenance=Provenance(3, "tool"),
        ),
        Event(
            kind=EventKind.COMPACTION,
            text="portable summary",
            provenance=Provenance(4, "compaction"),
        ),
        Event(
            kind=EventKind.TOOL_CALL,
            role=Role.ASSISTANT,
            tool_call_id="unfinished",
            tool_name="shell",
            payload={"input": {"cmd": "echo x"}},
            provenance=Provenance(5, "assistant"),
        ),
    )
    data, losses = opencode.serialize(
        replace(base, events=events), session_id="ses_fixture", cwd=tmp_path, cli_version="2.0.23"
    )
    opencode.validate_native_bytes(data, "ses_fixture")
    value = json.loads(data)
    assert value["messages"][0]["files"][0]["data"] == "aGVsbG8="
    completed = value["messages"][1]["content"][0]
    assert completed["id"] == "call_one"
    assert completed["state"]["content"][1]["uri"] == image
    assert value["messages"][2]["type"] == "compaction"
    assert value["messages"][2]["summary"] == "portable summary"
    assert value["messages"][3]["content"][0]["state"]["status"] == "error"
    assert losses["tool_call:v2_incomplete_archived"] == 1
    path = tmp_path / "rich.json"
    path.write_bytes(data)
    parsed = opencode.parse_import(path)
    assert any(
        e.kind == EventKind.COMPACTION and e.text == "portable summary" for e in parsed.events
    )
    assert any(
        e.kind == EventKind.TOOL_RESULT and e.tool_call_id == "call_one" and e.text == "result"
        for e in parsed.events
    )
    assert any(
        e.kind == EventKind.CONTEXT and e.payload["image_url"] == image for e in parsed.events
    )


@pytest.mark.parametrize("change", ["duplicate", "tool", "location", "tokens", "base64"])
def test_v2_rejects_malformed_portable_content(tmp_path, change):
    value = json.loads(artifact(tmp_path).native_bytes)
    if change == "duplicate":
        value["messages"].append(value["messages"][0])
    elif change == "location":
        value["info"]["location"] = None
    elif change == "tokens":
        value["info"]["tokens"] = {}
    elif change == "base64":
        value["messages"][0]["files"] = [{"data": "!!!", "mime": "image/png"}]
    else:
        value["messages"][1]["content"] = [
            {"type": "tool", "state": {"status": "completed", "input": {}, "content": []}}
        ]
    with pytest.raises(SessionMigrateError):
        opencode.validate_native_bytes(json.dumps(value).encode(), value["info"]["id"])


def test_v2_private_provider_state_accounted(tmp_path):
    value = json.loads(artifact(tmp_path).native_bytes)
    assistant = next(m for m in value["messages"] if m["type"] == "assistant")
    assistant["providerState"] = {"encrypted": "private"}
    path = tmp_path / "state.json"
    path.write_text(json.dumps(value))
    parsed = opencode.parse_import(path)
    assert ("opencode_v2_providerState", 1) in parsed.losses
    assert all("private" not in (e.text or "") for e in parsed.events)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2.0.23", True),
        ("2.0.22", True),
        ("opencode v2.0.23", True),
        ("2.0.23-gateway.3648f00e", True),
        ("2.1.0", False),
        ("3.0.0", False),
        ("9.9.9", False),
        ("1.17.20", True),
    ],
)
def test_version_schema_gate(value, expected):
    assert opencode.supported_version(value) is expected


def test_v2_cli_commands_and_conflict_not_success(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setattr(conversion, "_opencode_version", lambda *args: "2.0.23")
    calls = []

    def run(command, environ):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "Session already exists\n", "")

    monkeypatch.setattr(conversion, "_run_opencode", run)
    with pytest.raises(SessionMigrateError, match="refused"):
        conversion._invoke_opencode_import(
            Path("opencode"), tmp_path / "bundle.json", {}, cwd=tmp_path
        )
    assert calls[0][1:3] == ["session", "import"]
    assert calls[0][-3:] == ["--directory", str(tmp_path), "--standalone"]


def test_native_v2_isolated_import_export_dry_run_and_collision(tmp_path):
    """Opt-in actual native CLI integration; only synthetic fixture data."""
    import os
    from dataclasses import replace

    native = os.environ.get("SESSION_MIGRATE_TEST_OPENCODE_V2")
    if not native:
        pytest.skip("set SESSION_MIGRATE_TEST_OPENCODE_V2 to a real v2 binary")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(tmp_path),
        "OPENCODE_DISABLE_AUTOUPDATE": "true",
        "OPENCODE_CONFIG_CONTENT": '{"disabled_providers":["opencode"]}',
    }
    for key, name in [
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_STATE_HOME", "state"),
    ]:
        env[key] = str(tmp_path / name)
    for value in [env["HOME"], *(env[k] for k in env if k.startswith("XDG_"))]:
        Path(value).mkdir()
    release = conversion._opencode_version(Path(native), env)
    assert release in {"2.0.22", "2.0.23"}
    target = artifact(tmp_path, release)
    # Include a native completed compaction and linked tool output from the Codex fixture.
    value = json.loads(target.native_bytes)
    image = "data:image/png;base64,aGVsbG8="
    user = next(m for m in value["messages"] if m["type"] == "user")
    user["files"] = [{"data": "aGVsbG8=", "mime": "image/png", "source": {"type": "inline"}}]
    tool = next(
        c
        for m in value["messages"]
        if m["type"] == "assistant"
        for c in m["content"]
        if c["type"] == "tool"
    )
    tool["state"]["content"].append({"type": "file", "uri": image, "mime": "image/png"})
    value["messages"].append(
        {
            "id": "msg_native_compaction",
            "type": "compaction",
            "time": {"created": value["info"]["time"]["updated"]},
            "status": "completed",
            "reason": "auto",
            "summary": "Retain forty one.",
            "recent": "Latest request.",
        }
    )
    target = replace(target, native_bytes=(json.dumps(value) + "\n").encode())
    manifest = tmp_path / "manifest.json"
    cli = Path(native)
    conversion.install_opencode_artifact(
        target, manifest_path=manifest, target_cli=cli, environ=env, dry_run=True
    )
    assert not manifest.exists()
    conversion.install_opencode_artifact(
        target, manifest_path=manifest, target_cli=cli, environ=env
    )
    assert manifest.exists()
    exported = conversion.load_opencode_session(target.session_id, source_cli=cli, environ=env)
    assert exported.cli_version == release
    assert any(e.kind == EventKind.COMPACTION and "forty one" in e.text for e in exported.events)
    assert any(e.kind == EventKind.TOOL_RESULT for e in exported.events)
    assert any(
        e.kind == EventKind.CONTEXT and e.payload.get("image_url") == image for e in exported.events
    )
    # Native global conflict is also protected when the project-filtered preflight misses it.
    other = tmp_path / "other-manifest.json"
    with pytest.raises(SessionMigrateError, match="overwrite|refused"):
        conversion.install_opencode_artifact(
            target, manifest_path=other, target_cli=cli, environ=env
        )
    assert not other.exists()
    from session_migrate.catalog import Catalog

    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        result = catalog.refresh(
            opencode_roots=(Path(env["XDG_DATA_HOME"]) / "opencode",), include_auto=False
        )
        assert result.root_errors == 0
        entries = catalog.list_sessions(agent_format=AgentFormat.OPENCODE, include_paths=True)
        assert [entry.session_id for entry in entries] == [target.session_id]
        assert entries[0].cli_version == release
        assert entries[0].cwd == str(tmp_path)


def test_v2_content_free_inspection(tmp_path):
    from session_migrate.inspection import inspect_session

    path = tmp_path / "inspection.json"
    path.write_bytes(artifact(tmp_path).native_bytes)
    result = inspect_session(path, source_format=AgentFormat.OPENCODE)
    assert result.cwd == str(tmp_path)
    assert result.tool_calls > 0 and result.tool_results > 0
    assert result.roles.get("user", 0) > 0
    assert result.records > 0


def test_v2_autodetection_uses_flat_schema_while_legacy_remains_ambiguous(tmp_path):
    from session_migrate.errors import FormatDetectionError
    from session_migrate.inspection import detect_path_format, inspect_session

    path = tmp_path / "auto.json"
    path.write_bytes(artifact(tmp_path).native_bytes)
    assert detect_path_format(path) == AgentFormat.OPENCODE
    assert inspect_session(path).format == AgentFormat.OPENCODE.value
    assert load_session(path).source_format == AgentFormat.OPENCODE
    session = load_session(FIXTURES / "codex-0.144.4/basic.jsonl", AgentFormat.CODEX)
    legacy = convert_session(session, ConversionOptions(TargetFormat.OPENCODE))
    path.write_bytes(legacy.native_bytes)
    with pytest.raises(FormatDetectionError, match="no reliable producer marker"):
        detect_path_format(path)


@pytest.mark.parametrize("whitespace", ["", "   ", "\n\t"])
def test_private_only_compaction_preserves_readable_history(tmp_path, whitespace):
    value = json.loads(artifact(tmp_path).native_bytes)
    value["messages"] = [m for m in value["messages"] if m["type"] != "compaction"]
    value["messages"].append(
        {
            "id": "msg_private_compaction",
            "type": "compaction",
            "time": {"created": value["info"]["time"]["updated"]},
            "status": "completed",
            "reason": "auto",
            "summary": whitespace,
            "recent": whitespace,
            "providerContext": {"encrypted": "private"},
        }
    )
    path = tmp_path / "private-compaction.json"
    path.write_text(json.dumps(value))
    source = opencode.parse_session(path)
    assert not any(e.kind == EventKind.COMPACTION for e in source.events)
    assert any(e.kind == EventKind.MESSAGE and e.role == Role.USER for e in source.events)
    target = convert_session(
        source,
        ConversionOptions(
            target_format=TargetFormat.OPENCODE, target_cli_version="2.0.23", cwd=tmp_path
        ),
    )
    native = json.loads(target.native_bytes)
    assert not any(m["type"] == "compaction" for m in native["messages"])
    assert target.dropped["opaque:opencode_v2_compaction_empty_portable_summary"] == 1
    assert target.dropped["opaque:opencode_v2_providerContext"] == 1
    assert b'"encrypted"' not in target.native_bytes


def test_v2_reasoning_variant_loss_is_explicit(tmp_path):
    value = json.loads(artifact(tmp_path).native_bytes)
    value["info"]["model"]["variant"] = "high"
    assistant = next(m for m in value["messages"] if m["type"] == "assistant")
    assistant["model"]["variant"] = "max"
    path = tmp_path / "variants.json"
    path.write_text(json.dumps(value))
    parsed = opencode.parse_import(path)
    assert ("opencode_v2_session_model_variant", 1) in parsed.losses
    assert ("opencode_v2_message_model_variant", 1) in parsed.losses


@pytest.mark.parametrize("text", [None, 3, {}, []])
def test_v2_invalid_user_text_fails_closed(tmp_path, text):
    value = json.loads(artifact(tmp_path).native_bytes)
    user = next(m for m in value["messages"] if m["type"] == "user")
    user["text"] = text
    with pytest.raises(SessionMigrateError, match="user message has invalid text"):
        opencode.validate_native_bytes(json.dumps(value).encode(), value["info"]["id"])


def test_v2_unknown_message_type_fails_closed(tmp_path):
    value = json.loads(artifact(tmp_path).native_bytes)
    value["messages"][0]["type"] = "future-user"
    with pytest.raises(SessionMigrateError, match="unsupported message type"):
        opencode.validate_native_bytes(json.dumps(value).encode(), value["info"]["id"])


@pytest.mark.parametrize("extra_content", [None, 3, "unknown", [{"type": "future-part"}]])
def test_unknown_user_extension_is_accounted_without_crashing(tmp_path, extra_content):
    value = json.loads(artifact(tmp_path).native_bytes)
    user = next(m for m in value["messages"] if m["type"] == "user")
    user["content"] = extra_content
    path = tmp_path / "extension.json"
    path.write_text(json.dumps(value))
    parsed = opencode.parse_import(path)
    assert ("opencode_v2_unknown_message_fields", 1) in parsed.losses
    assert any(e.kind == EventKind.MESSAGE and e.role == Role.USER for e in parsed.events)


def test_file_metadata_omission_is_explicit(tmp_path):
    value = json.loads(artifact(tmp_path).native_bytes)
    user = next(m for m in value["messages"] if m["type"] == "user")
    user["files"] = [
        {
            "data": "aGVsbG8=",
            "mime": "image/png",
            "source": {"type": "uri", "uri": "file:///private/image.png"},
            "name": "image.png",
        }
    ]
    path = tmp_path / "file-metadata.json"
    path.write_text(json.dumps(value))
    parsed = opencode.parse_import(path)
    assert ("opencode_v2_file_metadata", 1) in parsed.losses
    assert any(e.kind == EventKind.CONTEXT for e in parsed.events)


@pytest.mark.parametrize("named_files", [1, 2])
def test_tool_file_metadata_loss_is_counted_per_block_through_conversion(tmp_path, named_files):
    value = json.loads(artifact(tmp_path).native_bytes)
    tool = next(
        content
        for message in value["messages"]
        if message["type"] == "assistant"
        for content in message["content"]
        if content["type"] == "tool"
    )
    uri = "data:image/png;base64,aGVsbG8="
    file_block = {"type": "file", "uri": uri, "mime": "image/png"}
    tool["state"]["content"].extend(
        {**file_block, "name": f"synthetic-filename-{index}.png"} for index in range(named_files)
    )
    tool["state"]["content"].append(file_block)
    expected_uris = [block["uri"] for block in tool["state"]["content"] if block["type"] == "file"]
    path = tmp_path / "tool-file-metadata.json"
    path.write_text(json.dumps(value))
    source = opencode.parse_session(path)
    target = convert_session(
        source,
        ConversionOptions(TargetFormat.OPENCODE, target_cli_version="2.0.23", cwd=tmp_path),
    )
    assert target.dropped["opaque:opencode_v2_tool_file_metadata"] == named_files
    target_tool = next(
        content
        for message in json.loads(target.native_bytes)["messages"]
        if message["type"] == "assistant"
        for content in message["content"]
        if content["type"] == "tool" and content["id"] == tool["id"]
    )
    files = [block for block in target_tool["state"]["content"] if block["type"] == "file"]
    assert [block["uri"] for block in files] == expected_uris
    assert b"synthetic-filename" not in target.native_bytes


def test_kilo_metadata_override_keeps_legacy_schema(tmp_path):
    session = load_session(FIXTURES / "codex-0.144.4/basic.jsonl", AgentFormat.CODEX)
    target = convert_session(
        session,
        ConversionOptions(TargetFormat.KILO, target_cli_version="2.0.23", cwd=tmp_path),
    )
    value = json.loads(target.native_bytes)
    assert value["info"]["version"] == "2.0.23"
    assert "directory" in value["info"] and "location" not in value["info"]
    assert all("info" in message and "parts" in message for message in value["messages"])


def test_kilo_rejects_opencode_v2_schema(tmp_path):
    from session_migrate.formats import kilo
    from session_migrate.inspection import inspect_session

    target = artifact(tmp_path)
    with pytest.raises(SessionMigrateError, match="legacy nested"):
        kilo.validate_native_bytes(target.native_bytes, target.session_id)
    path = tmp_path / "opencode-v2.json"
    path.write_bytes(target.native_bytes)
    with pytest.raises(SessionMigrateError, match="legacy nested"):
        kilo.parse_session(path)
    with pytest.raises(SessionMigrateError, match="requires --format opencode"):
        inspect_session(path, source_format=AgentFormat.KILO)


@pytest.mark.parametrize("field, malformed", [("cost", "invalid"), ("tokens", {}), ("model", {})])
def test_v2_compaction_validates_native_metadata_before_projection(tmp_path, field, malformed):
    value = json.loads(artifact(tmp_path).native_bytes)
    compaction = next(message for message in value["messages"] if message["type"] == "compaction")
    compaction[field] = malformed
    with pytest.raises(SessionMigrateError):
        opencode.validate_native_bytes(json.dumps(value).encode(), value["info"]["id"])


def test_v2_running_tool_requires_native_metadata(tmp_path):
    value = json.loads(artifact(tmp_path).native_bytes)
    tool = next(
        content
        for message in value["messages"]
        if message["type"] == "assistant"
        for content in message["content"]
        if content["type"] == "tool"
    )
    tool["state"] = {"status": "running", "input": {}}
    with pytest.raises(SessionMigrateError, match="running tool has invalid metadata"):
        opencode.validate_native_bytes(json.dumps(value).encode(), value["info"]["id"])


@pytest.mark.parametrize(
    "field, malformed",
    [
        ("permissions", 42),
        ("permissions", [{"permission": "read", "pattern": "*", "action": "allow"}]),
        ("metadata", []),
        ("fork", 42),
        ("outcome", "not-outcome"),
        ("location", {"directory": 42}),
    ],
)
def test_v2_native_session_metadata_cannot_disappear_before_validation(tmp_path, field, malformed):
    value = json.loads(artifact(tmp_path).native_bytes)
    value["info"][field] = malformed
    with pytest.raises(SessionMigrateError):
        opencode.validate_native_bytes(json.dumps(value).encode(), value["info"]["id"])


def test_v2_tool_error_requires_structured_type(tmp_path):
    value = json.loads(artifact(tmp_path).native_bytes)
    tool = next(
        content
        for message in value["messages"]
        if message["type"] == "assistant"
        for content in message["content"]
        if content["type"] == "tool"
    )
    tool["state"] = {"status": "error", "input": {}, "error": {"message": "missing type"}}
    with pytest.raises(SessionMigrateError, match="invalid structured error"):
        opencode.validate_native_bytes(json.dumps(value).encode(), value["info"]["id"])
