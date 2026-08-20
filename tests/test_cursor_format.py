import hashlib
import json
import os
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from session_migrate.errors import SessionMigrateError
from session_migrate.formats import antigravity, claude, codex, copilot, cursor, opencode, pi
from session_migrate.model import AgentFormat, Event, EventKind, Provenance, Role, Session

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "cursor-agent-2026.03.20-44cb435"
    / "basic.json"
)
TARGET_ID = "44444444-5555-4666-8777-888888888888"


def portable_session(tmp_path: Path) -> Session:
    events = (
        Event(
            kind=EventKind.MESSAGE,
            role=Role.USER,
            text="CURSOR_USER_ALPHA",
            timestamp="2026-08-20T12:00:00Z",
            payload={
                "content_blocks": [
                    {
                        "type": "image",
                        "image_url": "data:image/png;base64,U1lOVEhFVElD",
                    }
                ]
            },
            provenance=Provenance(0, "user"),
        ),
        Event(
            kind=EventKind.MESSAGE,
            role=Role.ASSISTANT,
            text="CURSOR_ASSISTANT_OMEGA",
            timestamp="2026-08-20T12:00:01Z",
            provenance=Provenance(1, "assistant"),
        ),
        Event(
            kind=EventKind.THINKING,
            role=Role.ASSISTANT,
            text="CURSOR_PRIVATE_THINKING_MUST_NOT_SURVIVE",
            provenance=Provenance(2, "thinking"),
        ),
        Event(
            kind=EventKind.TOOL_CALL,
            role=Role.ASSISTANT,
            tool_name="read",
            tool_call_id="cursor-call-1",
            payload={"input": {"path": "CURSOR_TOOL_INPUT_MUST_NOT_SURVIVE"}},
            provenance=Provenance(3, "tool_call"),
        ),
        Event(
            kind=EventKind.TOOL_RESULT,
            role=Role.TOOL,
            text="CURSOR_TOOL_RESULT_MUST_NOT_SURVIVE",
            tool_call_id="cursor-call-1",
            payload={
                "content_blocks": [
                    {"type": "image", "image_url": "data:image/png;base64,VE9PTA=="}
                ]
            },
            provenance=Provenance(4, "tool_result"),
        ),
        Event(
            kind=EventKind.COMPACTION,
            role=Role.SYSTEM,
            text="CURSOR_COMPACTION_MUST_NOT_SURVIVE",
            provenance=Provenance(5, "compaction"),
        ),
        Event(
            kind=EventKind.MESSAGE,
            role=Role.SYSTEM,
            text="CURSOR_SYSTEM_MUST_NOT_SURVIVE",
            provenance=Provenance(6, "system"),
        ),
        Event(
            kind=EventKind.CONTEXT,
            role=Role.USER,
            payload={"block_type": "image", "image_url": "data:image/png;base64,Q1RY"},
            provenance=Provenance(7, "context"),
        ),
        Event(
            kind=EventKind.OPAQUE,
            payload={"native": "CURSOR_RUNTIME_MUST_NOT_SURVIVE"},
            provenance=Provenance(8, "runtime"),
        ),
    )
    return Session(
        source_format=AgentFormat.CLAUDE,
        source_path=tmp_path / "source.jsonl",
        source_sha256="0" * 64,
        session_id="11111111-1111-4111-8111-111111111111",
        cwd=tmp_path,
        started_at="2026-08-20T12:00:00Z",
        cli_version="2.1.209",
        model="fixture-model",
        title="Cursor fixture",
        events=events,
        raw_record_count=len(events),
        model_provider="anthropic",
    )


def fixture_database() -> bytes:
    value = json.loads(FIXTURE.read_text())
    metadata = {
        "agentId": value["agent_id"],
        "latestRootBlobId": value["latest_root_blob_id"],
        "name": value["name"],
        "createdAt": value["created_at"],
        "mode": value["mode"],
    }
    blobs = {row["id"]: bytes.fromhex(row["data_hex"]) for row in value["blobs"]}
    return cursor._build_database(metadata, blobs)


def fixture_with_native_losses() -> bytes:
    value = json.loads(FIXTURE.read_text())
    by_id = {row["id"]: bytes.fromhex(row["data_hex"]) for row in value["blobs"]}
    user_id = bytes.fromhex(
        "c77338cf00a17162d4d9ea54591d7d3b9e71bc54b59d1b09107e1d2f0f499d10"
    )
    assistant_id = bytes.fromhex(
        "77850406ef7f711aba9ef5dc0462f00002b4be387b6b5ff9b37d8d5efed29cee"
    )
    blobs = {user_id.hex(): by_id[user_id.hex()], assistant_id.hex(): by_id[assistant_id.hex()]}
    thinking = cursor._field_bytes(
        3, cursor._field_text(1, "CURSOR_NATIVE_PRIVATE_THINKING")
    )
    thinking_id = cursor._store_blob(blobs, thinking)
    tool = cursor._field_bytes(2, cursor._field_text(1, "CURSOR_NATIVE_PRIVATE_TOOL"))
    tool_id = cursor._store_blob(blobs, tool)
    agent = cursor._field_bytes(1, user_id)
    agent += b"".join(
        cursor._field_bytes(2, step_id)
        for step_id in (assistant_id, thinking_id, tool_id)
    )
    turn_id = cursor._store_blob(blobs, cursor._field_bytes(1, agent))
    root_id = cursor._store_blob(blobs, cursor._field_bytes(8, turn_id))
    metadata = {
        "agentId": TARGET_ID,
        "latestRootBlobId": root_id.hex(),
        "name": "Synthetic source losses",
        "createdAt": value["created_at"],
        "mode": "default",
    }
    return cursor._build_database(metadata, blobs)


def mutate_database(data: bytes, tmp_path: Path, statements: tuple[str, ...]) -> bytes:
    path = tmp_path / "mutated.db"
    path.write_bytes(data)
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA journal_mode=DELETE")
        for statement in statements:
            db.execute(statement)
    return path.read_bytes()


def test_writer_round_trip_is_text_only_and_counts_every_omission(tmp_path: Path) -> None:
    data, losses = cursor.serialize(
        portable_session(tmp_path),
        session_id=TARGET_ID,
        cwd=tmp_path,
        timestamp="2026-08-20T12:00:00Z",
    )
    expected_losses = {
        "compaction:unsupported": 1,
        "context:unsupported": 1,
        "image:unsupported": 3,
        "runtime_metadata:event_timestamp": 2,
        "runtime_metadata:message_payload": 1,
        "runtime_metadata:model": 1,
        "runtime_metadata:model_provider": 1,
        "runtime_metadata:opaque_event": 1,
        "runtime_metadata:source_cli_version": 1,
        "runtime_metadata:source_format": 1,
        "system:unsupported": 1,
        "thinking:unsupported": 1,
        "tool_call:unsupported": 1,
        "tool_result:unsupported": 1,
    }
    assert losses == expected_losses
    for private_marker in (
        b"CURSOR_PRIVATE_THINKING_MUST_NOT_SURVIVE",
        b"CURSOR_TOOL_INPUT_MUST_NOT_SURVIVE",
        b"CURSOR_TOOL_RESULT_MUST_NOT_SURVIVE",
        b"CURSOR_COMPACTION_MUST_NOT_SURVIVE",
        b"CURSOR_SYSTEM_MUST_NOT_SURVIVE",
        b"CURSOR_RUNTIME_MUST_NOT_SURVIVE",
    ):
        assert private_marker not in data

    path = tmp_path / TARGET_ID / "store.db"
    path.parent.mkdir()
    path.write_bytes(data)
    parsed = cursor.parse(path)

    assert parsed.session_id == TARGET_ID
    assert parsed.title == "Cursor fixture"
    assert parsed.started_at == "2026-08-20T12:00:00.000Z"
    assert parsed.losses == ()
    assert parsed.raw_record_count == 1
    assert [(event.role, event.text) for event in parsed.events] == [
        (Role.USER, "CURSOR_USER_ALPHA"),
        (Role.ASSISTANT, "CURSOR_ASSISTANT_OMEGA"),
    ]
    assert cursor.native_record_count(data) == 1


def test_multiple_native_turns_preserve_order_and_group_assistants(tmp_path: Path) -> None:
    source = portable_session(tmp_path)
    events = (
        source.events[0],
        source.events[1],
        replace(source.events[1], text="CURSOR_ASSISTANT_SECOND", provenance=Provenance(2)),
        replace(source.events[0], text="CURSOR_USER_BETA", provenance=Provenance(3)),
        replace(source.events[1], text="CURSOR_ASSISTANT_FINAL", provenance=Provenance(4)),
    )
    source = replace(source, events=events, model=None, model_provider=None)

    data, _ = cursor.serialize(source, session_id=TARGET_ID, cwd=tmp_path)
    path = tmp_path / "store.db"
    path.write_bytes(data)
    parsed = cursor.parse(path)

    assert parsed.raw_record_count == 2
    assert [event.text for event in parsed.events] == [
        "CURSOR_USER_ALPHA",
        "CURSOR_ASSISTANT_OMEGA",
        "CURSOR_ASSISTANT_SECOND",
        "CURSOR_USER_BETA",
        "CURSOR_ASSISTANT_FINAL",
    ]
    assert [event.provenance.record_index for event in parsed.events] == [0, 0, 0, 1, 1]


def test_sanitized_fixture_matches_recovered_content_addressed_graph(tmp_path: Path) -> None:
    data = fixture_database()
    cursor.validate_native_bytes(data, TARGET_ID)
    path = tmp_path / TARGET_ID / "store.db"
    path.parent.mkdir()
    path.write_bytes(data)

    parsed = cursor.parse(path)

    assert [(event.role, event.text) for event in parsed.events] == [
        (Role.USER, "CURSOR_CLEANROOM_USER_ALPHA"),
        (Role.ASSISTANT, "CURSOR_CLEANROOM_ASSISTANT_OMEGA"),
    ]
    value = json.loads(FIXTURE.read_text())
    for row in value["blobs"]:
        payload = bytes.fromhex(row["data_hex"])
        assert hashlib.sha256(payload).hexdigest() == row["id"]


def test_native_source_losses_become_accounting_events_for_every_writer(
    tmp_path: Path,
) -> None:
    path = tmp_path / TARGET_ID / "store.db"
    path.parent.mkdir()
    path.write_bytes(fixture_with_native_losses())
    parsed = cursor.parse(path)

    assert dict(parsed.losses) == {
        "thinking:unsupported": 1,
        "tool_call:unsupported": 1,
    }
    assert all("PRIVATE" not in (event.text or "") for event in parsed.events)
    projected = cursor.project_session(parsed, source_format=AgentFormat.CODEX)
    opaque_reasons = [
        event.payload.get("reason")
        for event in projected.events
        if event.kind == EventKind.OPAQUE
    ]
    assert opaque_reasons == [
        "cursor:thinking:unsupported",
        "cursor:tool_call:unsupported",
    ]

    writers = (
        lambda: claude.serialize(projected, session_id=TARGET_ID, cwd=tmp_path),
        lambda: codex.serialize(projected, session_id=TARGET_ID, cwd=tmp_path),
        lambda: pi.serialize(projected, session_id=TARGET_ID, cwd=tmp_path),
        lambda: opencode.serialize(
            projected,
            session_id="ses_44444444555546668777888888888888",
            cwd=tmp_path,
        ),
        lambda: copilot.serialize(projected, session_id=TARGET_ID, cwd=tmp_path),
        lambda: antigravity.serialize(projected, session_id=TARGET_ID, cwd=tmp_path),
    )
    for writer in writers:
        _, losses = writer()
        assert losses["opaque:cursor:thinking:unsupported"] == 1
        assert losses["opaque:cursor:tool_call:unsupported"] == 1


@pytest.mark.parametrize(
    ("statements", "message"),
    [
        (("CREATE TABLE extra(value TEXT)",), "schema objects"),
        (
            (
                "UPDATE blobs SET data=x'00' WHERE id="
                "'c77338cf00a17162d4d9ea54591d7d3b9e71bc54b59d1b09107e1d2f0f499d10'",
            ),
            "digest does not match",
        ),
        (
            (
                "DELETE FROM blobs WHERE id="
                "'77850406ef7f711aba9ef5dc0462f00002b4be387b6b5ff9b37d8d5efed29cee'",
            ),
            "step reference is missing",
        ),
    ],
)
def test_parser_fails_closed_on_corrupt_native_stores(
    tmp_path: Path, statements: tuple[str, ...], message: str
) -> None:
    data = mutate_database(fixture_database(), tmp_path, statements)
    path = tmp_path / "corrupt-store.db"
    path.write_bytes(data)

    with pytest.raises(SessionMigrateError, match=message):
        cursor.parse(path)


def test_parser_rejects_unknown_metadata_fields(tmp_path: Path) -> None:
    data = fixture_database()
    path = tmp_path / "metadata.db"
    path.write_bytes(data)
    with sqlite3.connect(path) as db:
        encoded = db.execute("SELECT value FROM meta WHERE key='0'").fetchone()[0]
        value = json.loads(bytes.fromhex(encoded))
        value["unexpected"] = "fail closed"
        db.execute(
            "UPDATE meta SET value=? WHERE key='0'",
            (json.dumps(value, separators=(",", ":")).encode().hex(),),
        )

    with pytest.raises(SessionMigrateError, match="outside the pinned schema"):
        cursor.parse(path)


def test_paths_match_cursor_workspace_layout_and_reject_non_uuid4(tmp_path: Path) -> None:
    workspace = tmp_path / "some" / ".." / "work"
    normalized = Path(os.path.abspath(workspace))
    expected = hashlib.md5(str(normalized).encode(), usedforsecurity=False).hexdigest()

    assert cursor.workspace_key(workspace) == expected
    assert cursor.session_relative_path(TARGET_ID, workspace) == (
        Path("chats") / expected / TARGET_ID / "store.db"
    )
    assert not cursor.session_relative_path(TARGET_ID, workspace).is_absolute()
    with pytest.raises(SessionMigrateError, match="UUIDv4"):
        cursor.session_relative_path("../escape", workspace)


def test_config_home_uses_pinned_precedence(tmp_path: Path) -> None:
    assert cursor.config_home(tmp_path, environ={}) == tmp_path / ".cursor"
    assert cursor.config_home(
        tmp_path, environ={"XDG_CONFIG_HOME": str(tmp_path / "xdg")}
    ) == tmp_path / "xdg" / "cursor"
    assert cursor.config_home(
        tmp_path,
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
            "CURSOR_CONFIG_DIR": str(tmp_path / "explicit"),
        },
    ) == tmp_path / "explicit"


def test_install_is_atomic_private_and_never_overwrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = fixture_database()
    monkeypatch.setattr(cursor, "verify_pinned_cli", lambda *args, **kwargs: tmp_path / "cli")
    workspace = tmp_path / "work"
    workspace.mkdir()
    target_home = tmp_path / "config" / "cursor"

    installed = cursor.install_database(
        data,
        session_id=TARGET_ID,
        cwd=workspace,
        target_home=target_home,
    )

    assert installed.conversation_path == target_home / cursor.session_relative_path(
        TARGET_ID, workspace
    )
    assert stat_mode(installed.conversation_path) == 0o600
    assert cursor.parse(installed.conversation_path, cwd=workspace).session_id == TARGET_ID
    with pytest.raises(SessionMigrateError, match="already exists|overwrite"):
        cursor.install_database(
            data,
            session_id=TARGET_ID,
            cwd=workspace,
            target_home=target_home,
        )


def test_install_rejects_symlinked_config_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = fixture_database()
    monkeypatch.setattr(cursor, "verify_pinned_cli", lambda *args, **kwargs: tmp_path / "cli")
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)

    with pytest.raises(SessionMigrateError, match="unsafe directory"):
        cursor.install_database(
            data,
            session_id=TARGET_ID,
            cwd=tmp_path,
            target_home=linked,
        )


def test_verify_pinned_cli_rejects_unrecognized_launcher(tmp_path: Path) -> None:
    launcher = tmp_path / "cursor-agent"
    launcher.write_bytes(b"x" * cursor.PINNED_CURSOR_LAUNCHER_SIZE)
    launcher.chmod(0o700)

    with pytest.raises(SessionMigrateError, match="digest"):
        cursor.verify_pinned_cli(launcher)


@pytest.mark.parametrize(
    "drifted_name", ["cursor-agent", "index.js", "891.index.js", "node"]
)
def test_verify_pinned_cli_rejects_drift_in_every_runtime_component(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drifted_name: str,
) -> None:
    contents = {
        "cursor-agent": b"launcher",
        "index.js": b"bundle",
        "891.index.js": b"proto-chunk",
        "node": b"runtime",
    }
    for name, value in contents.items():
        (tmp_path / name).write_bytes(value)
    monkeypatch.setattr(cursor, "PINNED_CURSOR_LAUNCHER_SIZE", len(contents["cursor-agent"]))
    monkeypatch.setattr(
        cursor,
        "PINNED_CURSOR_LAUNCHER_SHA256",
        hashlib.sha256(contents["cursor-agent"]).hexdigest(),
    )
    monkeypatch.setattr(cursor, "PINNED_CURSOR_BUNDLE_SIZE", len(contents["index.js"]))
    monkeypatch.setattr(
        cursor,
        "PINNED_CURSOR_BUNDLE_SHA256",
        hashlib.sha256(contents["index.js"]).hexdigest(),
    )
    monkeypatch.setattr(
        cursor, "PINNED_CURSOR_PROTO_CHUNK_SIZE", len(contents["891.index.js"])
    )
    monkeypatch.setattr(
        cursor,
        "PINNED_CURSOR_PROTO_CHUNK_SHA256",
        hashlib.sha256(contents["891.index.js"]).hexdigest(),
    )
    monkeypatch.setattr(cursor, "PINNED_CURSOR_NODE_SIZE", len(contents["node"]))
    monkeypatch.setattr(
        cursor,
        "PINNED_CURSOR_NODE_SHA256",
        hashlib.sha256(contents["node"]).hexdigest(),
    )
    monkeypatch.setattr(
        cursor.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout=cursor.PINNED_CURSOR_VERSION + "\n", stderr=""
        ),
    )
    assert cursor.verify_pinned_cli(tmp_path / "cursor-agent") == tmp_path / "cursor-agent"

    (tmp_path / drifted_name).write_bytes(contents[drifted_name] + b"-drift")
    with pytest.raises(SessionMigrateError, match="does not match the pinned Cursor build"):
        cursor.verify_pinned_cli(tmp_path / "cursor-agent")


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777
