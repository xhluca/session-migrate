import base64
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from session_migrate.errors import SessionMigrateError
from session_migrate.formats import devin
from session_migrate.model import AgentFormat, Event, EventKind, Provenance, Role, Session

SESSION_ID = "fix-timeline-merging"
OTHER_ID = "review-auth-boundary"
PNG_BYTES = b"\x89PNG\r\n\x1a\nsynthetic-devin-image"
PNG_URL = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode()


def portable_session(tmp_path: Path, *, title: str = "Fix timeline merging") -> Session:
    events = (
        Event(
            EventKind.MESSAGE,
            Provenance(0, "user"),
            role=Role.USER,
            text="DEVIN_USER_ALPHA",
            timestamp="2026-08-30T12:00:00Z",
        ),
        Event(
            EventKind.CONTEXT,
            Provenance(0, "user", block_index=1),
            role=Role.USER,
            timestamp="2026-08-30T12:00:00Z",
            payload={"block_type": "image", "image_url": PNG_URL},
        ),
        Event(
            EventKind.MESSAGE,
            Provenance(1, "assistant"),
            role=Role.ASSISTANT,
            text="DEVIN_ASSISTANT_BETA",
            timestamp="2026-08-30T12:00:01Z",
        ),
        Event(
            EventKind.THINKING,
            Provenance(1, "assistant", block_index=1),
            role=Role.ASSISTANT,
            text="PRIVATE_DEVIN_THOUGHT",
            timestamp="2026-08-30T12:00:01Z",
            payload={"signature": "provider-signature"},
        ),
        Event(
            EventKind.TOOL_CALL,
            Provenance(1, "assistant", block_index=2),
            role=Role.ASSISTANT,
            timestamp="2026-08-30T12:00:01Z",
            tool_name="read_file",
            tool_call_id="call-devin-1",
            payload={"input": {"path": "src/timeline.py"}},
        ),
        Event(
            EventKind.TOOL_RESULT,
            Provenance(2, "tool"),
            role=Role.TOOL,
            text="DEVIN_RESULT_GAMMA",
            timestamp="2026-08-30T12:00:02Z",
            tool_call_id="call-devin-1",
            payload={"is_error": False},
        ),
        Event(
            EventKind.COMPACTION,
            Provenance(3, "compaction"),
            role=Role.SYSTEM,
            text="DEVIN_SUMMARY_DELTA",
            timestamp="2026-08-30T12:00:03Z",
            payload={"has_boundary_metadata": True},
        ),
        Event(
            EventKind.MESSAGE,
            Provenance(4, "user"),
            role=Role.USER,
            text="DEVIN_POST_COMPACTION_EPSILON",
            timestamp="2026-08-30T12:00:04Z",
        ),
        Event(
            EventKind.MESSAGE,
            Provenance(5, "assistant"),
            role=Role.ASSISTANT,
            text="DEVIN_FINAL_ZETA",
            timestamp="2026-08-30T12:00:05Z",
        ),
    )
    return Session(
        source_format=AgentFormat.CLAUDE,
        source_path=tmp_path / "source.jsonl",
        source_sha256="0" * 64,
        session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        cwd=tmp_path,
        started_at="2026-08-30T12:00:00Z",
        cli_version="2.1.209",
        model="fixture-model",
        title=title,
        events=events,
        raw_record_count=len(events),
        model_provider="anthropic",
    )


def serialized(
    tmp_path: Path,
    *,
    session_id: str = SESSION_ID,
    title: str = "Fix timeline merging",
) -> bytes:
    data, _ = devin.serialize(
        portable_session(tmp_path, title=title),
        session_id=session_id,
        cwd=tmp_path,
    )
    return data


def installed(tmp_path: Path) -> tuple[Path, bytes]:
    data = serialized(tmp_path)
    database = devin.install_database(data, tmp_path / "devin", SESSION_ID)
    return database, data


@pytest.mark.parametrize(
    "value",
    [SESSION_ID, "bald-ketch", "a", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"],
)
def test_devin_native_session_ids_accept_slugs_and_uuids(value: str) -> None:
    assert devin.normalized_session_id(value) == value


@pytest.mark.parametrize("value", ["", "../escape", "has space", "é", "a" * 129])
def test_devin_native_session_ids_fail_closed(value: str) -> None:
    with pytest.raises(SessionMigrateError, match="ASCII slug"):
        devin.normalized_session_id(value)


def test_devin_data_roots_are_platform_and_xdg_aware(tmp_path: Path) -> None:
    assert devin.data_root(tmp_path, environ={}, platform="linux") == (
        tmp_path / ".local/share/devin/cli"
    )
    assert devin.data_root(tmp_path, environ={}, platform="darwin") == (
        tmp_path / "Library/Application Support/devin/cli"
    )
    assert (
        devin.data_root(
            tmp_path,
            environ={"XDG_DATA_HOME": str(tmp_path / "xdg")},
            platform="linux",
        )
        == tmp_path / "xdg/devin/cli"
    )
    assert (
        devin.data_root(
            tmp_path,
            environ={"APPDATA": str(tmp_path / "roaming")},
            platform="win32",
        )
        == tmp_path / "roaming/devin/cli"
    )
    assert devin.database_path(tmp_path / "root") == tmp_path / "root/sessions.db"
    assert devin.database_path(tmp_path / "sessions.db") == tmp_path / "sessions.db"
    assert devin.resume_command("bald-ketch") == ("devin", "--resume", "bald-ketch")


def test_devin_bundle_round_trip_preserves_messages_tools_image_and_metadata(
    tmp_path: Path,
) -> None:
    data, dropped = devin.serialize(
        portable_session(tmp_path),
        session_id=SESSION_ID,
        cwd=tmp_path,
    )
    parsed_bundle = devin.validate_native_bytes(data, SESSION_ID)
    database = devin.install_database(data, tmp_path / "native", SESSION_ID)
    source = devin.parse_session(database, SESSION_ID)

    assert parsed_bundle.cli_version == devin.PINNED_DEVIN_VERSION
    assert parsed_bundle.session["title"] == "Fix timeline merging"
    assert parsed_bundle.session["working_directory"] == str(tmp_path)
    assert parsed_bundle.session["model"] == "fixture-model"
    assert devin.native_record_count(data) == len(parsed_bundle.nodes) == 7
    assert dropped == {
        "compaction:boundary_metadata": 1,
        "compaction:flattened": 1,
        "thinking:private": 1,
        "thinking:provider_payload": 1,
    }
    assert source.source_format == AgentFormat.DEVIN
    assert source.source_path == Path(f"devin:{SESSION_ID}")
    assert source.session_id == SESSION_ID
    assert source.cwd == tmp_path
    assert source.model == "fixture-model"
    assert source.title == "Fix timeline merging"
    assert source.event_counts() == {
        "compaction": 1,
        "context": 1,
        "message": 4,
        "opaque": 1,
        "tool_call": 1,
        "tool_result": 1,
    }
    assert (
        next(event for event in source.events if event.kind == EventKind.CONTEXT).payload[
            "image_url"
        ]
        == PNG_URL
    )
    call = next(event for event in source.events if event.kind == EventKind.TOOL_CALL)
    result = next(event for event in source.events if event.kind == EventKind.TOOL_RESULT)
    assert call.tool_name == result.tool_name == "read_file"
    assert call.tool_call_id == result.tool_call_id == "call-devin-1"
    assert call.payload["input"] == {"path": "src/timeline.py"}
    assert result.text == "DEVIN_RESULT_GAMMA"
    compaction = next(event for event in source.events if event.kind == EventKind.COMPACTION)
    assert compaction.text == "DEVIN_SUMMARY_DELTA"
    assert all(event.text != "PRIVATE_DEVIN_THOUGHT" for event in source.events)


def test_devin_installer_creates_private_exact_v16_store_and_refuses_overwrite(
    tmp_path: Path,
) -> None:
    database, data = installed(tmp_path)

    assert stat_mode(database) == 0o600
    assert stat_mode(database.parent) == 0o700
    assert not list(database.parent.glob(".sessions.*"))
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT MAX(version) FROM refinery_schema_history").fetchone()[0] == 16
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM message_nodes").fetchone()[0] == 7
        assert db.execute("SELECT COUNT(*) FROM prompt_history").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM tool_call_state").fetchone()[0] == 0

    before = database.read_bytes()
    with pytest.raises(SessionMigrateError, match="overwrite an existing Devin session"):
        devin.install_database(data, database.parent, SESSION_ID)
    assert database.read_bytes() == before


def test_devin_install_dry_run_is_non_mutating_and_checks_collisions(tmp_path: Path) -> None:
    data = serialized(tmp_path)
    root = tmp_path / "missing" / "devin"

    candidate = devin.install_database(data, root, SESSION_ID, dry_run=True)

    assert candidate == root / "sessions.db"
    assert not root.exists()

    database = devin.install_database(data, root, SESSION_ID)
    before = database.read_bytes()
    with pytest.raises(SessionMigrateError, match="overwrite an existing Devin session"):
        devin.install_database(data, root, SESSION_ID, dry_run=True)
    assert database.read_bytes() == before


def test_devin_shared_store_lists_and_selects_multiple_logical_sessions(tmp_path: Path) -> None:
    database, _ = installed(tmp_path)
    other_data = serialized(tmp_path, session_id=OTHER_ID, title="Review auth boundary")
    devin.install_database(other_data, database, OTHER_ID)

    summaries = devin.list_sessions(database)
    assert {item.session_id for item in summaries} == {SESSION_ID, OTHER_ID}
    assert {item.title for item in summaries} == {
        "Fix timeline merging",
        "Review auth boundary",
    }
    assert all(item.cwd == tmp_path for item in summaries)
    assert all(item.cli_version == devin.PINNED_DEVIN_VERSION for item in summaries)
    assert all(item.started_at == "2026-08-30T12:00:00Z" for item in summaries)
    assert all(item.updated_ns == 1_788_091_206_000_000_000 for item in summaries)
    assert all(item.records == 7 for item in summaries)
    with pytest.raises(SessionMigrateError, match="requires an explicit session ID"):
        devin.parse_session(database)
    assert devin.parse_session(database, OTHER_ID).title == "Review auth boundary"


def test_devin_parser_walks_only_the_active_branch(tmp_path: Path) -> None:
    database, _ = installed(tmp_path)
    with sqlite3.connect(database) as db:
        db.executemany(
            """
            INSERT INTO message_nodes(
              session_id,node_id,parent_node_id,chat_message,created_at,metadata
            ) VALUES(?,?,?,?,?,NULL)
            """,
            [
                (
                    SESSION_ID,
                    80,
                    2,
                    json.dumps(
                        {
                            "message_id": "abandoned-a",
                            "role": "assistant",
                            "content": "ABANDONED_ASSISTANT",
                            "thinking": None,
                            "tool_calls": [],
                            "metadata": {},
                        }
                    ),
                    1_788_091_202,
                ),
                (
                    SESSION_ID,
                    81,
                    80,
                    json.dumps(
                        {
                            "message_id": "abandoned-r",
                            "role": "tool",
                            "content": "ABANDONED_RESULT",
                            "tool_call_id": "abandoned-call",
                            "metadata": {},
                        }
                    ),
                    1_788_091_203,
                ),
            ],
        )

    source = devin.parse_session(database, SESSION_ID)

    assert source.raw_record_count == 7
    assert all("ABANDONED" not in (event.text or "") for event in source.events)
    assert devin.list_sessions(database)[0].records == 7


def test_devin_session_digest_ignores_unrelated_shared_store_changes(tmp_path: Path) -> None:
    database, _ = installed(tmp_path)
    before = devin.parse_session(database, SESSION_ID).source_sha256

    other_data = serialized(tmp_path, session_id=OTHER_ID, title="Other")
    devin.install_database(other_data, database, OTHER_ID)

    assert devin.parse_session(database, SESSION_ID).source_sha256 == before


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("bad_json", "not valid JSON"),
        ("missing_parent", "broken parent link|incomplete"),
        ("cycle", "cycle|incomplete"),
        ("hidden", "hidden"),
        ("bad_timestamp", "created_at is invalid"),
        ("bad_cwd", "working directory is not absolute"),
        ("orphan_result", "orphan tool result"),
    ],
)
def test_devin_parser_rejects_malformed_active_trajectories(
    tmp_path: Path, mutation: str, message: str
) -> None:
    database, _ = installed(tmp_path)
    with sqlite3.connect(database) as db:
        if mutation == "bad_json":
            db.execute(
                "UPDATE message_nodes SET chat_message='{bad' WHERE session_id=? AND node_id=2",
                (SESSION_ID,),
            )
        elif mutation == "missing_parent":
            db.execute(
                "UPDATE message_nodes SET parent_node_id=999 WHERE session_id=? AND node_id=7",
                (SESSION_ID,),
            )
        elif mutation == "cycle":
            db.execute(
                "UPDATE message_nodes SET parent_node_id=7 WHERE session_id=? AND node_id=1",
                (SESSION_ID,),
            )
        elif mutation == "hidden":
            db.execute("UPDATE sessions SET hidden=1 WHERE id=?", (SESSION_ID,))
        elif mutation == "bad_timestamp":
            db.execute("UPDATE sessions SET created_at=-1 WHERE id=?", (SESSION_ID,))
        elif mutation == "bad_cwd":
            db.execute("UPDATE sessions SET working_directory='relative' WHERE id=?", (SESSION_ID,))
        else:
            message_row = json.loads(
                db.execute(
                    "SELECT chat_message FROM message_nodes WHERE session_id=? AND node_id=4",
                    (SESSION_ID,),
                ).fetchone()[0]
            )
            message_row["tool_call_id"] = "missing-call"
            db.execute(
                "UPDATE message_nodes SET chat_message=? WHERE session_id=? AND node_id=4",
                (json.dumps(message_row), SESSION_ID),
            )

    with pytest.raises(SessionMigrateError, match=message):
        devin.parse_session(database, SESSION_ID)


def test_devin_parser_rejects_schema_drift_symlinks_and_unknown_identity(tmp_path: Path) -> None:
    database, _ = installed(tmp_path)
    with pytest.raises(SessionMigrateError, match="not present"):
        devin.parse_session(database, "unknown-session")

    symlink = tmp_path / "linked-root"
    symlink.symlink_to(database.parent, target_is_directory=True)
    with pytest.raises(SessionMigrateError, match="unsafe existing prefix"):
        devin.parse_session(symlink, SESSION_ID)

    with sqlite3.connect(database) as db:
        db.execute("UPDATE refinery_schema_history SET checksum='wrong' WHERE version=16")
    with pytest.raises(SessionMigrateError, match="migrations do not match"):
        devin.parse_session(database, SESSION_ID)


def test_devin_bundle_rejects_duplicate_keys_and_structural_tampering(tmp_path: Path) -> None:
    data = serialized(tmp_path)
    with pytest.raises(SessionMigrateError, match="not valid UTF-8 JSON"):
        devin.validate_native_bytes(
            b'{"schema":"session-migrate.devin.v1","schema":"duplicate"}',
            SESSION_ID,
        )

    value = json.loads(data)
    value["nodes"][1]["parent_node_id"] = 999
    with pytest.raises(SessionMigrateError, match="not contiguous"):
        devin.validate_native_bytes(json.dumps(value).encode(), SESSION_ID)

    value = json.loads(data)
    value["nodes"][3]["chat_message"]["tool_call_id"] = "unknown-call"
    with pytest.raises(SessionMigrateError, match="orphan tool result"):
        devin.validate_native_bytes(json.dumps(value).encode(), SESSION_ID)


def test_devin_bundle_enforces_bounded_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = serialized(tmp_path)
    monkeypatch.setattr(devin, "MAX_MESSAGE_BYTES", 32)

    with pytest.raises(SessionMigrateError, match="safety limit"):
        devin.validate_native_bytes(data, SESSION_ID)


def test_devin_tool_failures_are_preserved_and_unknown_native_roles_fail_closed(
    tmp_path: Path,
) -> None:
    database, _ = installed(tmp_path)
    with sqlite3.connect(database) as db:
        result = json.loads(
            db.execute(
                "SELECT chat_message FROM message_nodes WHERE session_id=? AND node_id=4",
                (SESSION_ID,),
            ).fetchone()[0]
        )
        result["is_error"] = True
        db.execute(
            "UPDATE message_nodes SET chat_message=? WHERE session_id=? AND node_id=4",
            (json.dumps(result), SESSION_ID),
        )
        final = json.loads(
            db.execute(
                "SELECT chat_message FROM message_nodes WHERE session_id=? AND node_id=7",
                (SESSION_ID,),
            ).fetchone()[0]
        )
        final["role"] = "planner"
        db.execute(
            "UPDATE message_nodes SET chat_message=? WHERE session_id=? AND node_id=7",
            (json.dumps(final), SESSION_ID),
        )

    with pytest.raises(SessionMigrateError, match="unsupported role"):
        devin.parse_session(database, SESSION_ID)

    with sqlite3.connect(database) as db:
        final["role"] = "assistant"
        db.execute(
            "UPDATE message_nodes SET chat_message=? WHERE session_id=? AND node_id=7",
            (json.dumps(final), SESSION_ID),
        )

    source = devin.parse_session(database, SESSION_ID)
    tool_result = next(event for event in source.events if event.kind == EventKind.TOOL_RESULT)
    assert tool_result.payload["is_error"] is True


def test_devin_serialization_accounts_for_unrepresentable_and_bad_tool_events(
    tmp_path: Path,
) -> None:
    source = replace(
        portable_session(tmp_path),
        events=(
            Event(EventKind.MESSAGE, Provenance(0), role=Role.USER, text="hello"),
            Event(
                EventKind.TOOL_RESULT,
                Provenance(1),
                role=Role.TOOL,
                text="orphan",
                tool_call_id="missing",
            ),
            Event(EventKind.OPAQUE, Provenance(2), payload={"reason": "runtime"}),
            Event(EventKind.MESSAGE, Provenance(3), role=Role.SYSTEM, text="runtime prompt"),
        ),
    )

    data, dropped = devin.serialize(source, session_id=SESSION_ID, cwd=tmp_path)

    devin.validate_native_bytes(data, SESSION_ID)
    assert dropped == {
        "opaque": 1,
        "system:runtime": 1,
        "tool_result:orphan_id": 1,
    }


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777
