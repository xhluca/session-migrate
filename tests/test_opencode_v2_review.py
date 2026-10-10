"""Regression coverage for native v2 content and validation before projection."""

import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from session_migrate.conversion import ConversionOptions, convert_session, load_session
from session_migrate.errors import SessionMigrateError
from session_migrate.formats import opencode
from session_migrate.model import AgentFormat, EventKind, TargetFormat

FIXTURES = Path(__file__).parent / "fixtures"
IMAGE_A = "data:image/png;base64,iVBORw0KGgo="
IMAGE_B = "data:image/jpeg;base64,/9j/2Q=="


def source_bundle(tmp_path):
    source = load_session(FIXTURES / "codex-0.144.4/basic.jsonl", AgentFormat.CODEX)
    return json.loads(convert_session(source, options(tmp_path)).native_bytes)


def options(tmp_path, release="2.0.23"):
    return ConversionOptions(
        TargetFormat.OPENCODE,
        target_cli_version=release,
        cwd=tmp_path,
        model_provider="fixture",
        model="test",
    )


def tool(bundle):
    return next(
        c
        for m in bundle["messages"]
        if m["type"] == "assistant"
        for c in m["content"]
        if c["type"] == "tool"
    )


def rewrite(bundle, tmp_path, release="2.0.23"):
    path = tmp_path / "source-v2.json"
    path.write_text(json.dumps(bundle))
    source = opencode.parse_session(path)
    artifact = convert_session(source, options(tmp_path, release))
    return json.loads(artifact.native_bytes), artifact.dropped, source


def mixed_blocks():
    # The basic fixture has no tool image; use independent synthetic images.
    return [
        {"type": "text", "text": "First image: copper"},
        {"type": "file", "uri": IMAGE_A, "mime": "image/png"},
        {"type": "text", "text": "Second image: blue"},
        {"type": "file", "uri": IMAGE_B, "mime": "image/jpeg"},
    ]


def test_preserve_tool_block_order(tmp_path):
    bundle = source_bundle(tmp_path)
    blocks = mixed_blocks()
    tool(bundle)["state"]["content"] = blocks
    rewritten, losses, _ = rewrite(bundle, tmp_path)
    assert tool(rewritten)["state"]["content"] == blocks, {
        "actual": tool(rewritten)["state"]["content"],
        "losses": losses,
    }


def test_structured_error_preserved_or_counted(tmp_path):
    bundle = source_bundle(tmp_path)
    error = {
        "type": "ConflictError",
        "message": "same error text",
        "status": 409,
        "response": {"body": "specific diagnostic"},
    }
    tool(bundle)["state"] = {"status": "error", "input": {"command": "true"}, "error": error}
    rewritten, losses, _ = rewrite(bundle, tmp_path)
    assert tool(rewritten)["state"]["error"] == error or any("error" in k for k in losses), {
        "actual": tool(rewritten)["state"]["error"],
        "losses": losses,
    }


@pytest.mark.parametrize("mutation", ["missing-system-text", "array-title"])
def test_malformed_native_fields_rejected_before_projection(tmp_path, mutation):
    bundle = source_bundle(tmp_path)
    if mutation == "array-title":
        bundle["info"]["title"] = []
    else:
        bundle["messages"].append(
            {
                "id": "msg_invalid_control",
                "type": "system",
                "time": {"created": bundle["info"]["time"]["updated"]},
            }
        )
    with pytest.raises(SessionMigrateError):
        opencode.validate_native_bytes(json.dumps(bundle).encode(), bundle["info"]["id"])


@pytest.mark.parametrize("release", ["2.0.22", "2.0.23"])
@pytest.mark.parametrize(
    "blocks",
    [mixed_blocks(), [{"type": "text", "text": ""}, *mixed_blocks(), {"type": "text", "text": ""}]],
)
def test_tool_blocks_preserve_segmentation_and_portable_payload(tmp_path, release, blocks):
    bundle = source_bundle(tmp_path)
    tool(bundle)["state"]["content"] = blocks
    rewritten, losses, source = rewrite(bundle, tmp_path, release)
    result = next(e for e in source.events if e.kind == EventKind.TOOL_RESULT)
    expected = [
        {"type": "text", "text": b["text"]}
        if b["type"] == "text"
        else {"type": "image", "image_url": b["uri"]}
        for b in blocks
    ]
    # Payload is compare=False: compare it explicitly instead of Event equality.
    assert result.payload["content_blocks"] == expected
    assert tool(rewritten)["state"]["content"] == blocks
    assert losses == {}


@pytest.mark.parametrize("target", [TargetFormat.CLAUDE, TargetFormat.CODEX, TargetFormat.PI])
def test_v2_tool_order_survives_capable_targets(tmp_path, target):
    from session_migrate.formats import claude, codex, pi

    bundle = source_bundle(tmp_path)
    tool(bundle)["state"]["content"] = mixed_blocks()
    _, _, source = rewrite(bundle, tmp_path)
    migrated = convert_session(
        source, ConversionOptions(target, cwd=tmp_path, model_provider="fixture", model="test")
    )
    path = tmp_path / "target.jsonl"
    path.write_bytes(migrated.native_bytes)
    parser = {
        TargetFormat.CLAUDE: claude.parse,
        TargetFormat.CODEX: codex.parse,
        TargetFormat.PI: pi.parse_session,
    }[target]
    parsed = parser(path)
    results = [e for e in parsed.events if e.kind == EventKind.TOOL_RESULT]
    expected = [
        {"type": "text", "text": "First image: copper"},
        {"type": "image", "image_url": IMAGE_A},
        {"type": "text", "text": "Second image: blue"},
        {"type": "image", "image_url": IMAGE_B},
    ]
    assert results[0].payload["content_blocks"] == expected


@pytest.mark.parametrize("target", [TargetFormat.OPENCODE, TargetFormat.KILO])
def test_legacy_order_degradation_is_counted(tmp_path, target):
    bundle = source_bundle(tmp_path)
    tool(bundle)["state"]["content"] = mixed_blocks()
    _, _, source = rewrite(bundle, tmp_path)
    migrated = convert_session(
        source,
        ConversionOptions(
            target,
            target_cli_version="1.17.20" if target == TargetFormat.OPENCODE else "7.5.0",
            cwd=tmp_path,
        ),
    )
    value = json.loads(migrated.native_bytes)
    state = next(p["state"] for m in value["messages"] for p in m["parts"] if p["type"] == "tool")
    assert state["output"] == "First image: copper\nSecond image: blue"
    assert [a["url"] for a in state["attachments"]] == [IMAGE_A, IMAGE_B]
    assert migrated.dropped["tool_result:content_block_order"] == 1


def test_tool_error_fields_have_individual_safe_provenance_and_manifest_losses(tmp_path):
    bundle = source_bundle(tmp_path)
    tool(bundle)["state"] = {
        "status": "error",
        "input": {},
        "error": {
            "type": "ConflictError",
            "message": "same error text",
            "status": 409,
            "response": {"body": "SYNTHETIC_DIAGNOSTIC_NOT_PORTABLE"},
        },
    }
    rewritten, losses, source = rewrite(bundle, tmp_path)
    expected = {
        "opencode_v2_tool_error_type",
        "opencode_v2_tool_error_status",
        "opencode_v2_tool_error_response",
    }
    omitted = [e for e in source.events if e.kind == EventKind.OPAQUE]
    assert {e.payload["reason"] for e in omitted} == expected
    source_index = next(
        i
        for i, m in enumerate(bundle["messages"])
        if m["type"] == "assistant" and any(c["type"] == "tool" for c in m["content"])
    )
    assert all(e.provenance.record_index == source_index for e in omitted)
    assert losses == {f"opaque:{reason}": 1 for reason in expected}
    assert all("SYNTHETIC_DIAGNOSTIC" not in str(e.payload) for e in source.events)
    assert all("SYNTHETIC_DIAGNOSTIC" not in (e.text or "") for e in source.events)
    assert tool(rewritten)["state"]["error"] == {"type": "ToolError", "message": "same error text"}
    artifact = convert_session(source, options(tmp_path))
    manifest = artifact.manifest(output_path=tmp_path / "target.json")
    assert manifest["dropped_events"] == losses
    assert "SYNTHETIC_DIAGNOSTIC" not in json.dumps(manifest)


CONTROLS = [
    ("system", {"text": "fixture update", "description": "fixture summary"}, ("text",)),
    ("synthetic", {"text": "fixture", "description": "fixture"}, ("text",)),
    (
        "skill",
        {"skill": "fixture", "name": "fixture", "text": "fixture"},
        ("skill", "name", "text"),
    ),
    ("agent-switched", {"agent": "build", "previous": "plan"}, ("agent",)),
    (
        "model-switched",
        {
            "model": {"id": "test", "providerID": "fixture"},
            "previous": {"id": "previous", "providerID": "fixture"},
        },
        ("model",),
    ),
    (
        "location-switched",
        {
            "location": {"directory": "/fixture"},
            "previous": {"location": {"directory": "/previous"}},
        },
        ("location",),
    ),
    (
        "shell",
        {
            "shellID": "sh_fixture",
            "command": "fixture",
            "status": "exited",
            "exit": 0,
            "output": {"output": "fixture", "cursor": 7, "size": 7, "truncated": False},
        },
        ("shellID", "command", "status"),
    ),
    ("idle", {"outcome": "succeeded"}, ("outcome",)),
    (
        "compaction",
        {"status": "running", "reason": "auto", "summary": "", "recent": ""},
        ("status", "reason", "summary", "recent"),
    ),
    (
        "compaction",
        {
            "status": "failed",
            "reason": "manual",
            "error": {"type": "FixtureError", "message": "fake"},
        },
        ("status", "reason", "error"),
    ),
]
REQUIRED_CONTROLS = [
    (kind, fields, required) for kind, fields, keys in CONTROLS for required in keys
]


def append_control(bundle, kind, fields):
    control = {
        "id": "msg_fixture_control",
        "type": kind,
        "time": {"created": bundle["info"]["time"]["updated"]},
        **fields,
    }
    bundle["messages"].append(control)
    return control


@pytest.mark.parametrize("kind,fields,required", REQUIRED_CONTROLS)
@pytest.mark.parametrize("malformed", ["missing", None, []])
def test_all_control_required_fields_validated_before_projection(
    tmp_path, kind, fields, required, malformed
):
    bundle = source_bundle(tmp_path)
    control = append_control(bundle, kind, fields)
    if malformed == "missing":
        control.pop(required)
    else:
        control[required] = malformed
    data = json.dumps(bundle).encode()
    path = tmp_path / "malformed.json"
    path.write_bytes(data)
    with pytest.raises(SessionMigrateError):
        opencode.validate_native_bytes(data, bundle["info"]["id"])
    with pytest.raises(SessionMigrateError):
        opencode.parse_session(path)


@pytest.mark.parametrize("kind,fields,required", CONTROLS)
def test_valid_control_variants_are_accepted_and_omissions_counted(
    tmp_path, kind, fields, required
):
    bundle = source_bundle(tmp_path)
    append_control(bundle, kind, fields)
    _, losses, source = rewrite(bundle, tmp_path)
    reason = (
        f"opencode_v2_compaction_{fields['status']}"
        if kind == "compaction"
        else f"opencode_v2_{kind}_record"
    )
    assert losses[f"opaque:{reason}"] == 1
    assert not any(
        e.text == fields.get("text") for e in source.events if e.kind == EventKind.MESSAGE
    )


@pytest.mark.parametrize(
    "field",
    [
        "title",
        "parentID",
        "agent",
        "model",
        "outcome",
        "subpath",
        "metadata",
        "permissions",
        "fork",
        "revert",
    ],
)
def test_native_optional_session_fields_accept_absence_but_reject_null(tmp_path, field):
    bundle = source_bundle(tmp_path)
    bundle["info"].pop(field, None)
    opencode.validate_native_bytes(json.dumps(bundle).encode(), bundle["info"]["id"])
    bundle["info"][field] = None
    with pytest.raises(SessionMigrateError):
        opencode.validate_native_bytes(json.dumps(bundle).encode(), bundle["info"]["id"])


@pytest.mark.parametrize("field", ["id", "projectID", "cost", "tokens", "time", "location"])
def test_native_required_session_fields_cannot_be_defaulted(tmp_path, field):
    bundle = source_bundle(tmp_path)
    bundle["info"].pop(field)
    with pytest.raises(SessionMigrateError):
        opencode.validate_native_bytes(json.dumps(bundle).encode(), "ses_fixture")


@pytest.mark.parametrize(
    "mutation",
    [
        "null-description",
        "bad-previous-model",
        "bad-previous-location",
        "bad-shell-output",
        "bad-clock",
        "duplicate-control-id",
        "bad-model-variant",
    ],
)
def test_dropped_control_nested_metadata_is_validated(tmp_path, mutation):
    bundle = source_bundle(tmp_path)
    if mutation == "null-description":
        append_control(bundle, "system", {"text": "fixture", "description": None})
    elif mutation == "bad-previous-model":
        append_control(
            bundle,
            "model-switched",
            {"model": {"id": "test", "providerID": "fixture"}, "previous": {"id": "fake"}},
        )
    elif mutation == "bad-previous-location":
        append_control(
            bundle,
            "location-switched",
            {"location": {"directory": "/fixture"}, "previous": {"location": {"directory": []}}},
        )
    elif mutation == "bad-shell-output":
        append_control(
            bundle,
            "shell",
            {
                "shellID": "sh_fixture",
                "command": "fixture",
                "status": "exited",
                "output": {"output": "fixture"},
            },
        )
    elif mutation == "bad-model-variant":
        append_control(
            bundle,
            "model-switched",
            {"model": {"id": "test", "providerID": "fixture", "variant": None}},
        )
    else:
        control = append_control(bundle, "idle", {"outcome": "succeeded"})
        if mutation == "bad-clock":
            control["time"]["completed"] = None
        else:
            bundle["messages"].append(control)
    with pytest.raises(SessionMigrateError):
        opencode.validate_native_bytes(json.dumps(bundle).encode(), bundle["info"]["id"])


@pytest.mark.parametrize("mutation", ["missing-system-text", "array-title"])
def test_dry_run_rejects_invalid_native_source_without_target_side_effects(
    tmp_path, mutation, capsys
):
    from session_migrate.cli import main

    bundle = source_bundle(tmp_path)
    if mutation == "array-title":
        bundle["info"]["title"] = []
    else:
        append_control(bundle, "system", {})
    path = tmp_path / "invalid-source.json"
    path.write_text(json.dumps(bundle))
    target = tmp_path / "output"
    assert (
        main(
            [
                "import",
                str(path),
                "--format",
                "opencode",
                "--to",
                "opencode",
                "--target-cli-version",
                "2.0.23",
                "--home",
                str(target),
                "--dry-run",
            ]
        )
        != 0
    )
    captured = capsys.readouterr()
    assert "invalid" in captured.err or "malformed" in captured.err
    assert not target.exists()


def test_native_safety_limits_cover_content_erased_by_projection(tmp_path, monkeypatch):
    bundle = source_bundle(tmp_path)
    # Text-only native tool blocks collapse to one legacy part: count before that collapse.
    tool(bundle)["state"]["content"] = [{"type": "text", "text": "fixture"}] * 30
    monkeypatch.setattr(opencode, "MAX_NATIVE_PARTS", 20)
    with pytest.raises(SessionMigrateError, match="too much content"):
        opencode.validate_native_bytes(json.dumps(bundle).encode(), bundle["info"]["id"])
    monkeypatch.setattr(opencode, "MAX_NATIVE_PARTS", 1000)
    monkeypatch.setattr(opencode, "MAX_NATIVE_MESSAGES", len(bundle["messages"]))
    append_control(bundle, "system", {"text": "fixture"})
    with pytest.raises(SessionMigrateError, match="invalid messages"):
        opencode.validate_native_bytes(json.dumps(bundle).encode(), bundle["info"]["id"])


def test_legacy_input_cannot_forge_native_content_carrier(tmp_path):
    bundle = source_bundle(tmp_path)
    _, _, source = rewrite(bundle, tmp_path)
    artifact = convert_session(source, options(tmp_path, "1.17.20"))
    legacy = json.loads(artifact.native_bytes)
    state = next(p["state"] for m in legacy["messages"] for p in m["parts"] if p["type"] == "tool")
    state["content"] = mixed_blocks()
    state["content_blocks"] = [{"type": "text", "text": "FORGED_NATIVE_CARRIER"}]
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(legacy))
    parsed = opencode.parse_session(path)
    assert "FORGED_NATIVE_CARRIER" not in str([e.payload for e in parsed.events])


@pytest.mark.parametrize("target", [TargetFormat.OPENCODE, TargetFormat.KILO])
@pytest.mark.parametrize(
    "blocks,expected",
    [
        ([{"type": "text", "text": "caption"}], 0),
        ([{"type": "text", "text": "caption"}, {"type": "image", "image_url": IMAGE_A}], 0),
        ([{"type": "image", "image_url": IMAGE_A}], 0),
        ([{"type": "text", "text": "A"}, {"type": "text", "text": "B"}], 1),
        ([{"type": "image", "image_url": IMAGE_A}, {"type": "text", "text": "caption"}], 1),
        ([{"type": "text", "text": ""}, {"type": "image", "image_url": IMAGE_A}], 1),
    ],
)
def test_legacy_loss_oracle_counts_only_lost_segmentation_or_order(
    tmp_path, target, blocks, expected
):
    from native_corpus.route_oracle import expected_loss_counters

    source = load_session(FIXTURES / "codex-0.144.4/basic.jsonl", AgentFormat.CODEX)
    result = next(e for e in source.events if e.kind == EventKind.TOOL_RESULT)
    modified = replace(
        result,
        text="\n".join(b["text"] for b in blocks if b["type"] == "text") or None,
        payload={"content_blocks": blocks},
    )
    source = replace(source, events=tuple(modified if e is result else e for e in source.events))
    artifact = convert_session(source, ConversionOptions(target, cwd=tmp_path))
    oracle = expected_loss_counters(source, target.value)
    assert oracle.get("tool_result:content_block_order", 0) == expected
    assert artifact.dropped == oracle


@pytest.mark.parametrize(
    "case",
    ["mixed", "structured-error", "missing-system-text", "array-title", "private-checkpoint"],
)
def test_stock_v2_accepts_and_cold_exports_review_sources_and_fixed_targets(tmp_path, case):
    binary = os.environ.get("SESSION_MIGRATE_TEST_OPENCODE_V2")
    if not binary:
        pytest.skip("set SESSION_MIGRATE_TEST_OPENCODE_V2 to stock 2.0.22 or 2.0.23")
    env = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "OPENCODE_DISABLE_AUTOUPDATE": "true",
        "OPENCODE_CONFIG_CONTENT": '{"warming":false,"disabled_providers":["opencode"]}',
    }
    for key in (
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "TMPDIR",
    ):
        directory = tmp_path / key.lower()
        directory.mkdir()
        env[key] = str(directory)

    def run(*args):
        result = subprocess.run(
            [binary, *args], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=90
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    assert run("--version").strip().removeprefix("opencode v") in {"2.0.22", "2.0.23"}
    bundle = source_bundle(tmp_path)
    if case == "mixed":
        tool(bundle)["state"]["content"] = mixed_blocks()
    elif case == "structured-error":
        tool(bundle)["state"] = {
            "status": "error",
            "input": {},
            "error": {
                "type": "ConflictError",
                "message": "same error text",
                "status": 409,
                "response": {"body": "specific synthetic diagnostic"},
            },
        }
    elif case == "array-title":
        bundle["info"]["title"] = []
    elif case == "private-checkpoint":
        bundle["messages"] = [m for m in bundle["messages"] if m["type"] != "compaction"]
        append_control(
            bundle,
            "compaction",
            {
                "status": "completed",
                "reason": "auto",
                "summary": "",
                "recent": "",
                "providerState": {"encrypted": "SYNTHETIC_PRIVATE_CHECKPOINT"},
                "providerContext": {
                    "version": 1,
                    "provenance": {
                        "providerID": "fixture",
                        "provider": "fixture",
                        "modelID": "test",
                        "route": "fixture",
                        "protocol": "fixture",
                        "endpoint": "fixture-digest",
                    },
                    "messages": [],
                },
            },
        )
    else:
        append_control(bundle, "system", {})
    path = tmp_path / "stock-source.json"
    path.write_text(json.dumps(bundle))
    if case in {"missing-system-text", "array-title"}:
        rejected = subprocess.run(
            [binary, "session", "import", str(path), "--directory", str(tmp_path), "--standalone"],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert rejected.returncode != 0
        assert "SchemaError" in rejected.stderr
        return
    run("session", "import", str(path), "--directory", str(tmp_path), "--standalone")
    accepted = json.loads(run("session", "export", bundle["info"]["id"], "--standalone"))
    assert tool(accepted)["state"] == tool(bundle)["state"]
    path.write_text(json.dumps(accepted))
    source = opencode.parse_session(path)
    migrated = convert_session(source, options(tmp_path))
    path.write_bytes(migrated.native_bytes)
    run("session", "import", str(path), "--directory", str(tmp_path), "--standalone")
    exported = json.loads(run("session", "export", migrated.session_id, "--standalone"))
    if case == "mixed":
        assert tool(exported)["state"]["content"] == mixed_blocks()
        # Stock may add its default model variant on import; this existing
        # metadata omission is independent of the ordered tool result.
        expected_losses = (
            {"opaque:opencode_v2_session_model_variant": 1}
            if accepted["info"].get("model", {}).get("variant")
            else {}
        )
        assert migrated.dropped == expected_losses
    elif case == "structured-error":
        assert tool(exported)["state"]["error"]["message"] == "same error text"
        assert migrated.dropped["opaque:opencode_v2_tool_error_response"] == 1
    else:
        assert not any(m["type"] == "compaction" for m in exported["messages"])
        for reason in ("providerContext", "providerState", "compaction_empty_portable_summary"):
            assert migrated.dropped[f"opaque:opencode_v2_{reason}"] == 1
        assert "SYNTHETIC_PRIVATE_CHECKPOINT" not in migrated.native_bytes.decode()
