"""OpenCode 2.1 transfers: renamed native data and referenced compaction tails."""

import json
from pathlib import Path

import pytest
from test_opencode_v2 import artifact

from session_migrate.conversion import ConversionOptions, convert_session
from session_migrate.errors import SessionMigrateError
from session_migrate.formats import opencode, opencode_v2
from session_migrate.model import EventKind, Role, TargetFormat


def v2_1_bundle(tmp_path: Path) -> dict:
    """A stock 2.0 transfer rewritten with the field names OpenCode 2.1 exports."""
    value = json.loads(artifact(tmp_path).native_bytes)
    for message in value["messages"]:
        if message["type"] == "compaction":
            message.pop("recent")
    return value


def write(tmp_path: Path, value: dict) -> Path:
    path = tmp_path / "v2.1.json"
    path.write_text(json.dumps(value))
    return path


def compaction(value: dict) -> dict:
    return next(m for m in value["messages"] if m["type"] == "compaction")


def test_native_data_is_accounted_like_its_2_0_names(tmp_path):
    value = v2_1_bundle(tmp_path)
    assistants = [m for m in value["messages"] if m["type"] == "assistant"]
    assistants[0]["native"] = {"step": "private-step"}
    assistants[0]["content"][0]["native"] = {"text": "private-text"}
    assistants[0]["content"].append(
        {
            "type": "reasoning",
            "text": "visible reasoning",
            "native": {"signature": "private-reasoning"},
            "time": {"created": value["info"]["time"]["created"]},
        }
    )
    tool = next(c for m in assistants for c in m["content"] if c["type"] == "tool")
    tool["native"] = {"call": "private-call", "result": "private-result"}
    parsed = opencode.parse_import(write(tmp_path, value))
    losses = dict(parsed.losses)
    assert losses["opencode_v2_providerState"] == 1
    assert losses["opencode_v2_content_state"] == 2
    assert losses["opencode_v2_content_providerState"] == 1
    assert "opencode_v2_unknown_assistant_content_fields" not in losses
    assert all("private" not in (e.text or "") for e in parsed.events)
    assert any(
        e.kind == EventKind.THINKING and e.text == "visible reasoning" for e in parsed.events
    )


def test_compaction_without_recent_rebuilds_kept_tail_from_keep_id(tmp_path):
    value = v2_1_bundle(tmp_path)
    marker = compaction(value)
    kept = value["messages"][: value["messages"].index(marker)]
    marker["metadata"] = {"keepID": kept[0]["id"]}
    expected = "\n\n".join(filter(None, map(opencode_v2._recent_text, kept)))
    assert expected.startswith("[User]: Remember synthetic migrator nonce BETA-2048.")
    assert "[Assistant tool call]: " in expected
    assert "[Tool result]: " in expected
    parsed = opencode.parse_import(write(tmp_path, value))
    summary = next(e for e in parsed.events if e.kind == EventKind.COMPACTION)
    assert summary.text == marker["summary"] + "\n\nRecent context:\n" + expected
    assert ("opencode_v2_metadata", 1) in parsed.losses


@pytest.mark.parametrize(
    "metadata, recent",
    [
        (None, ""),
        ({"keepID": "msg_not_in_this_transfer"}, ""),
        ({"recent": "[User]: upgraded 2.0 tail"}, "[User]: upgraded 2.0 tail"),
    ],
)
def test_compaction_tail_falls_back_without_a_resolvable_keep_id(tmp_path, metadata, recent):
    value = v2_1_bundle(tmp_path)
    marker = compaction(value)
    if metadata is not None:
        marker["metadata"] = metadata
    parsed = opencode.parse_import(write(tmp_path, value))
    summary = next(e for e in parsed.events if e.kind == EventKind.COMPACTION)
    assert summary.text == marker["summary"] + ("\n\nRecent context:\n" + recent if recent else "")


def test_provider_compaction_window_is_private_and_keeps_readable_history(tmp_path):
    value = v2_1_bundle(tmp_path)
    marker = compaction(value)
    marker["summary"] = ""
    marker["native"] = {"messages": [{"role": "user", "content": "private-window"}]}
    source = opencode.parse_session(write(tmp_path, value))
    assert not any(e.kind == EventKind.COMPACTION for e in source.events)
    assert any(e.kind == EventKind.MESSAGE and e.role == Role.USER for e in source.events)
    target = convert_session(
        source,
        ConversionOptions(
            target_format=TargetFormat.OPENCODE, target_cli_version="2.1.4", cwd=tmp_path
        ),
    )
    assert target.dropped["opaque:opencode_v2_compaction_empty_portable_summary"] == 1
    assert target.dropped["opaque:opencode_v2_providerState"] == 1
    assert b"private-window" not in target.native_bytes


def test_idle_error_is_accepted_and_counted(tmp_path):
    value = v2_1_bundle(tmp_path)
    value["messages"].append(
        {
            "id": "msg_zz_idle",
            "type": "idle",
            "time": {"created": value["info"]["time"]["updated"]},
            "outcome": "failed",
            "error": {"type": "ProviderError", "message": "fixture"},
        }
    )
    losses = dict(opencode.parse_import(write(tmp_path, value)).losses)
    assert losses["opencode_v2_error"] == 1
    assert "opencode_v2_unknown_message_fields" not in losses


@pytest.mark.parametrize("recent", [None, [], 7])
def test_present_malformed_recent_still_fails_closed(tmp_path, recent):
    value = v2_1_bundle(tmp_path)
    compaction(value)["recent"] = recent
    with pytest.raises(SessionMigrateError):
        opencode.parse_import(write(tmp_path, value))


def test_recent_text_matches_opencode_rendering():
    long = "x" * 1_251
    assert opencode_v2._recent_text({"type": "reasoning", "text": "hidden"}) == ""
    assert opencode_v2._recent_text({"type": "system", "text": "hidden"}) == ""
    assert (
        opencode_v2._recent_text(
            {
                "type": "user",
                "text": "hi",
                "skills": [{"id": "s", "name": "lint", "text": "rules"}, {"id": "t", "name": "x"}],
                "files": [
                    {"mime": "image/png", "data": "", "source": {"type": "inline"}},
                    {"mime": "text/plain", "data": "", "source": {"type": "uri", "uri": "a.txt"}},
                    {"mime": "text/plain", "data": "", "name": "b", "source": {"type": "inline"}},
                ],
            }
        )
        == "[Skill activated: lint]\nrules\n[User]: hi\n[Attached image/png: inline attachment]"
        "\n[Attached text/plain: a.txt]\n[Attached text/plain: b]"
    )
    assert opencode_v2._recent_text(
        {
            "type": "assistant",
            "content": [
                {"type": "reasoning", "text": "hidden"},
                {"type": "text", "text": "done"},
                {
                    "type": "tool",
                    "name": "read",
                    "state": {
                        "status": "completed",
                        "input": {"path": "ä"},
                        "content": [
                            {"type": "text", "text": long},
                            {"type": "file", "uri": "u", "mime": "image/png"},
                        ],
                    },
                },
                {
                    "type": "tool",
                    "name": "bash",
                    "state": {"status": "error", "input": {}, "error": {"message": "no"}},
                },
                {"type": "tool", "name": "edit", "state": {"status": "streaming", "input": "{"}},
            ],
        }
    ) == (
        "[Assistant]: done\n"
        '[Assistant tool call]: read({"path":"ä"})\n'
        f"[Tool result]: {'x' * 1_250}\n[truncated]\n"
        "[Assistant tool call]: bash({})\n[Tool error]: no\n"
        "[Assistant tool call]: edit({)"
    )
    assert (
        opencode_v2._recent_text(
            {"type": "shell", "command": "ls", "output": {"output": "a"}, "metadata": {}}
        )
        == "[Shell]: ls\na"
    )
    assert (
        opencode_v2._recent_text(
            {"type": "shell", "command": "ls", "metadata": {"background": True}}
        )
        == ""
    )
