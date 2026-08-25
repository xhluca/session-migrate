import json
import uuid
from pathlib import Path

import pytest

from session_migrate.catalog import Catalog
from session_migrate.conversion import (
    ConversionOptions,
    convert_session,
    install_kimi_artifact,
    load_session,
    target_import_paths,
)
from session_migrate.discovery import locate_session
from session_migrate.errors import SessionMigrateError
from session_migrate.formats import kimi, muse, qwen
from session_migrate.inspection import detect_path_format, inspect_session
from session_migrate.model import (
    AgentFormat,
    Event,
    EventKind,
    Provenance,
    Role,
    Session,
    TargetFormat,
)

SESSION_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
KIMI_ID = f"session_{SESSION_ID}"
TIMESTAMP = "2026-08-25T12:00:00Z"
PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _jsonl(records: list[dict[str, object]]) -> bytes:
    return b"".join(
        (json.dumps(record, separators=(",", ":")) + "\n").encode() for record in records
    )


def _source(tmp_path: Path) -> Session:
    return Session(
        source_format=AgentFormat.CLAUDE,
        source_path=tmp_path / "source.jsonl",
        source_sha256="0" * 64,
        session_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        cwd=tmp_path,
        started_at=TIMESTAMP,
        cli_version="2.1.209",
        model="fixture-model",
        model_provider="openrouter",
        title="Repair timeline merging",
        raw_record_count=8,
        events=(
            Event(
                kind=EventKind.MESSAGE,
                role=Role.USER,
                text="Review the interval merge boundary.",
                timestamp=TIMESTAMP,
                provenance=Provenance(0, "user"),
            ),
            Event(
                kind=EventKind.MESSAGE,
                role=Role.ASSISTANT,
                text="I found the boundary regression.",
                timestamp="2026-08-25T12:00:01Z",
                provenance=Provenance(1, "assistant"),
            ),
            Event(
                kind=EventKind.TOOL_CALL,
                role=Role.ASSISTANT,
                tool_name="read_file",
                tool_call_id="call-fixture",
                timestamp="2026-08-25T12:00:02Z",
                payload={"input": {"path": "timeline.py"}},
                provenance=Provenance(2, "tool_call"),
            ),
            Event(
                kind=EventKind.TOOL_RESULT,
                role=Role.TOOL,
                tool_name="read_file",
                tool_call_id="call-fixture",
                text="def merge(events): ...",
                timestamp="2026-08-25T12:00:03Z",
                payload={"content": "def merge(events): ..."},
                provenance=Provenance(3, "tool_result"),
            ),
            Event(
                kind=EventKind.CONTEXT,
                role=Role.USER,
                timestamp="2026-08-25T12:00:04Z",
                payload={"block_type": "image", "image_url": PNG},
                provenance=Provenance(4, "image"),
            ),
            Event(
                kind=EventKind.COMPACTION,
                text="The interval boundary is the active task.",
                timestamp="2026-08-25T12:00:05Z",
                provenance=Provenance(5, "compaction"),
            ),
            Event(
                kind=EventKind.THINKING,
                role=Role.ASSISTANT,
                text="Private reasoning must not cross providers.",
                timestamp="2026-08-25T12:00:06Z",
                provenance=Provenance(6, "thinking"),
            ),
            Event(
                kind=EventKind.OPAQUE,
                payload={"reason": "fixture_runtime"},
                provenance=Provenance(7, "runtime"),
            ),
        ),
    )


def _kinds(session: Session) -> list[EventKind]:
    return [event.kind for event in session.events]


def test_qwen_round_trip_preserves_portable_history_and_title(tmp_path: Path) -> None:
    data, dropped = qwen.serialize(
        _source(tmp_path), session_id=SESSION_ID, cwd=tmp_path, timestamp=TIMESTAMP
    )
    qwen.validate_native_bytes(data, SESSION_ID)
    path = tmp_path / "qwen.jsonl"
    path.write_bytes(data)
    parsed = qwen.parse_session(path)

    assert parsed.source_format == AgentFormat.QWEN
    assert parsed.session_id == SESSION_ID
    assert parsed.title == "Repair timeline merging"
    assert _kinds(parsed) == [
        EventKind.MESSAGE,
        EventKind.MESSAGE,
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
        EventKind.CONTEXT,
    ]
    assert dropped == {
        "compaction": 1,
        "opaque:fixture_runtime": 1,
        "thinking:private": 1,
    }
    assert detect_path_format(path) == AgentFormat.QWEN
    assert inspect_session(path).format == "qwen"
    assert load_session(path).source_format == AgentFormat.QWEN


def test_qwen_branch_selection_discovery_and_malformed_graph(tmp_path: Path) -> None:
    data, _ = qwen.serialize(
        _source(tmp_path), session_id=SESSION_ID, cwd=tmp_path, timestamp=TIMESTAMP
    )
    records = [json.loads(line) for line in data.splitlines()]
    abandoned = dict(records[1])
    abandoned["uuid"] = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    abandoned["parentUuid"] = records[0]["uuid"]
    abandoned["message"] = {"role": "model", "parts": [{"text": "abandoned"}]}
    records.insert(2, abandoned)
    branched = b"".join(
        (json.dumps(record, separators=(",", ":")) + "\n").encode() for record in records
    )
    qwen.validate_native_bytes(branched, SESSION_ID)

    home = tmp_path / "home"
    path = home / qwen.session_relative_path(tmp_path, SESSION_ID)
    path.parent.mkdir(parents=True)
    path.write_bytes(branched)
    parsed = qwen.parse_session(path)
    assert all(event.text != "abandoned" for event in parsed.events)
    assert any(
        event.payload.get("reason") == "inactive_qwen_branch_record"
        for event in parsed.events
        if event.kind == EventKind.OPAQUE
    )
    assert locate_session(AgentFormat.QWEN, SESSION_ID, home, cwd=tmp_path) == path

    records[1]["parentUuid"] = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    malformed = b"".join((json.dumps(record) + "\n").encode() for record in records)
    with pytest.raises(SessionMigrateError, match="missing parent"):
        qwen.validate_native_bytes(malformed, SESSION_ID)


def test_kimi_bundle_install_parse_discovery_and_inspection(tmp_path: Path) -> None:
    artifact = convert_session(
        _source(tmp_path),
        ConversionOptions(
            target_format=TargetFormat.KIMI,
            session_id=SESSION_ID,
            cwd=tmp_path,
        ),
    )
    assert artifact.session_id == KIMI_ID
    parsed_bundle = kimi.validate_native_bytes(artifact.native_bytes, KIMI_ID)
    assert parsed_bundle.state["id"] == KIMI_ID
    assert artifact.dropped == {
        "opaque:fixture_runtime": 1,
        "thinking:private": 1,
    }

    home = tmp_path / "kimi-home"
    wire_path, manifest_path = install_kimi_artifact(artifact, target_home=home)
    session_dir = wire_path.parent.parent.parent
    assert (session_dir / kimi.STATE_FILENAME).is_file()
    assert manifest_path.is_file()
    assert oct(wire_path.stat().st_mode & 0o777) == "0o600"
    assert oct(session_dir.stat().st_mode & 0o777) == "0o700"

    parsed = kimi.parse_session(wire_path)
    assert parsed.source_format == AgentFormat.KIMI
    assert parsed.session_id == KIMI_ID
    assert parsed.title == "Repair timeline merging"
    assert _kinds(parsed) == [
        EventKind.MESSAGE,
        EventKind.MESSAGE,
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
        EventKind.CONTEXT,
        EventKind.COMPACTION,
    ]
    assert locate_session(AgentFormat.KIMI, SESSION_ID, home, cwd=tmp_path) == wire_path
    assert detect_path_format(session_dir) == AgentFormat.KIMI
    assert detect_path_format(wire_path) == AgentFormat.KIMI
    assert inspect_session(session_dir).format == "kimi"
    assert load_session(session_dir).source_format == AgentFormat.KIMI

    with pytest.raises(SessionMigrateError, match="overwrite"):
        install_kimi_artifact(artifact, target_home=home, dry_run=True)


def test_kimi_rejects_state_linkage_and_wire_protocol_drift(tmp_path: Path) -> None:
    data, _ = kimi.serialize(
        _source(tmp_path), session_id=KIMI_ID, cwd=tmp_path, timestamp=TIMESTAMP
    )
    value = json.loads(data)
    value["state"]["id"] = "session_bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    with pytest.raises(SessionMigrateError, match="linkage"):
        kimi.validate_native_bytes(json.dumps(value).encode(), KIMI_ID)

    value = json.loads(data)
    value["records"][0]["protocol_version"] = "9.9"
    with pytest.raises(SessionMigrateError, match="metadata header"):
        kimi.validate_native_bytes(json.dumps(value).encode(), KIMI_ID)


def test_muse_round_trip_and_retained_status_marker(tmp_path: Path) -> None:
    data, dropped = muse.serialize(
        _source(tmp_path), session_id=SESSION_ID, cwd=tmp_path, timestamp=TIMESTAMP
    )
    records = [json.loads(line) for line in data.splitlines()]
    for record in records[1:]:
        record["sequence"] += 1
        if record["payload_type"] == "runtime.user_intent.materialized":
            record["payload"]["envelope_sequence"] += 1
    marker = {
        "schema_version": 1,
        "stream": {"kind": "session", "id": SESSION_ID},
        "position": {
            "id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            "sequence": 2,
        },
        "retained_marker": "status omitted",
        "omitted_record": {
            "record_type": "status",
            "durability": "ephemeral",
            "payload_type": "runtime.session",
            "payload_schema_version": 1,
            "payload_kind": "task",
            "omission_class": "task_tool_delta_v1",
        },
    }
    records.insert(1, marker)
    marked = b"".join(
        (json.dumps(record, separators=(",", ":")) + "\n").encode() for record in records
    )
    muse.validate_native_bytes(marked, SESSION_ID)
    path = tmp_path / "muse.jsonl"
    path.write_bytes(marked)
    parsed = muse.parse_session(path)

    assert parsed.source_format == AgentFormat.MUSE
    assert parsed.session_id == SESSION_ID
    assert parsed.model == "fixture-model"
    assert parsed.model_provider == "openrouter"
    assert _kinds(parsed) == [
        EventKind.OPAQUE,
        EventKind.MESSAGE,
        EventKind.OPAQUE,
        EventKind.MESSAGE,
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
    ]
    assert parsed.events[0].payload["reason"] == ("muse_retained_marker:task_tool_delta_v1")
    assert parsed.events[2].payload["reason"] == ("muse_native:runtime.user_intent.materialized")
    assert dropped == {
        "compaction": 1,
        "context": 1,
        "opaque:fixture_runtime": 1,
        "thinking:private": 1,
    }
    assert detect_path_format(path) == AgentFormat.MUSE
    assert inspect_session(path).format == "muse"
    assert load_session(path).source_format == AgentFormat.MUSE


def test_muse_writer_emits_native_context_replay_lifecycle(tmp_path: Path) -> None:
    data, _ = muse.serialize(
        _source(tmp_path), session_id=SESSION_ID, cwd=tmp_path, timestamp=TIMESTAMP
    )
    records = [json.loads(line) for line in data.splitlines()]
    accepted = next(
        record for record in records if record["payload_type"] == "runtime.user_intent.accepted"
    )
    started = next(
        record
        for record in records
        if record["payload_type"] == "runtime.session"
        and record["payload"].get("event", {}).get("kind") == "started"
    )
    materialized = next(
        record for record in records if record["payload_type"] == "runtime.user_intent.materialized"
    )
    outcome = materialized["payload"]["outcome"]

    assert records[1]["payload"]["record"]["security_mode"] == "normal"
    assert accepted["payload"]["refill_blocks"]
    assert materialized["payload"]["envelope_record_id"] == accepted["id"]
    assert materialized["payload"]["envelope_sequence"] == accepted["sequence"]
    assert outcome["run_id"] == started["payload"]["run_id"]
    assert outcome["run_started_session_record_id"] == started["id"]
    assert outcome["run_started_source_record_id"] == started["payload"]["source_run_record_id"]

    invalid_security = [dict(record) for record in records]
    invalid_security[1] = json.loads(json.dumps(invalid_security[1]))
    invalid_security[1]["payload"]["record"]["security_mode"] = "default"
    with pytest.raises(SessionMigrateError, match="security mode"):
        muse.validate_native_bytes(_jsonl(invalid_security), SESSION_ID)

    missing_refill = json.loads(json.dumps(records))
    missing_refill[2]["payload"]["refill_blocks"] = []
    with pytest.raises(SessionMigrateError, match="prompt/refill"):
        muse.validate_native_bytes(_jsonl(missing_refill), SESSION_ID)

    broken_link = json.loads(json.dumps(records))
    broken_link[4]["payload"]["outcome"]["run_started_session_record_id"] = str(uuid.uuid4())
    with pytest.raises(SessionMigrateError, match="run linkage"):
        muse.validate_native_bytes(_jsonl(broken_link), SESSION_ID)


def test_muse_conversion_defaults_to_its_native_meta_provider(tmp_path: Path) -> None:
    artifact = convert_session(
        _source(tmp_path),
        ConversionOptions(
            target_format=TargetFormat.MUSE,
            session_id=SESSION_ID,
            cwd=tmp_path,
            model="meta/muse-glimmer-30b",
        ),
    )
    metadata = json.loads(artifact.native_bytes.splitlines()[0])["payload"]["record"]
    assert metadata["provider_id"] == "meta"


def test_muse_discovery_and_malformed_marker_fail_closed(tmp_path: Path) -> None:
    data, _ = muse.serialize(
        _source(tmp_path), session_id=SESSION_ID, cwd=tmp_path, timestamp=TIMESTAMP
    )
    home = tmp_path / "muse-home"
    path = home / muse.session_relative_path(SESSION_ID, TIMESTAMP)
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    assert locate_session(AgentFormat.MUSE, SESSION_ID, home) == path

    records = [json.loads(line) for line in data.splitlines()]
    records[0]["sequence"] = 2
    malformed = b"".join((json.dumps(record) + "\n").encode() for record in records)
    with pytest.raises(SessionMigrateError, match="sequence"):
        muse.validate_native_bytes(malformed, SESSION_ID)


def test_new_target_paths_and_environment_homes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    muse_home = tmp_path / "xdg-data/muse"
    muse_artifact = convert_session(
        source,
        ConversionOptions(
            target_format=TargetFormat.MUSE,
            session_id=SESSION_ID,
            cwd=tmp_path,
        ),
    )
    muse_native, muse_manifest = target_import_paths(muse_artifact, muse_home)
    assert muse_home in muse_native.parents
    assert muse_manifest == muse_home / "session-migrate/manifests" / (
        f"{muse_artifact.session_id}.json"
    )
    for target, variable, configured in (
        (TargetFormat.QWEN, "QWEN_HOME", tmp_path / "qwen"),
        (TargetFormat.KIMI, "KIMI_CODE_HOME", tmp_path / "kimi"),
    ):
        monkeypatch.setenv(variable, str(configured))
        artifact = convert_session(
            source,
            ConversionOptions(target_format=target, session_id=SESSION_ID, cwd=tmp_path),
        )
        native_path, manifest_path = target_import_paths(artifact, configured)
        assert configured in native_path.parents
        assert manifest_path == configured / "session-migrate/manifests" / (
            f"{artifact.session_id}.json"
        )


def test_catalog_indexes_and_searches_all_three_new_native_stores(tmp_path: Path) -> None:
    source = _source(tmp_path)
    muse_home = tmp_path / "muse-home"
    qwen_home = tmp_path / "qwen-home"
    kimi_home = tmp_path / "kimi-home"

    muse_data, _ = muse.serialize(source, session_id=SESSION_ID, cwd=tmp_path, timestamp=TIMESTAMP)
    muse_path = muse_home / muse.session_relative_path(SESSION_ID, TIMESTAMP)
    muse_path.parent.mkdir(parents=True)
    muse_path.write_bytes(muse_data)

    qwen_data, _ = qwen.serialize(source, session_id=SESSION_ID, cwd=tmp_path, timestamp=TIMESTAMP)
    qwen_path = qwen_home / qwen.session_relative_path(tmp_path, SESSION_ID)
    qwen_path.parent.mkdir(parents=True)
    qwen_path.write_bytes(qwen_data)

    artifact = convert_session(
        source,
        ConversionOptions(
            target_format=TargetFormat.KIMI,
            session_id=SESSION_ID,
            cwd=tmp_path,
        ),
    )
    install_kimi_artifact(artifact, target_home=kimi_home)

    with Catalog(tmp_path / "state/catalog.sqlite3") as catalog:
        result = catalog.refresh(
            include_auto=False,
            muse_roots=(muse_home,),
            qwen_roots=(qwen_home,),
            kimi_roots=(kimi_home,),
        )
        entries = catalog.list_sessions(limit=10)
        matches = catalog.list_sessions(query="timeline merging", limit=10)

        assert result.files_seen == 3
        assert result.statuses == {"candidate": 3}
        assert {entry.format for entry in entries} == {"muse", "qwen", "kimi"}
        assert {entry.format for entry in matches} == {"qwen", "kimi"}
        for entry in entries:
            selected = catalog.session_source_for_transfer(entry.catalog_id)
            assert selected.format.value == entry.format
            assert selected.path is not None and selected.path.is_file()
