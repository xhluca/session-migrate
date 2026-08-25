import base64
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from session_migrate.conversion import ConversionOptions, convert_session, load_session
from session_migrate.errors import SessionMigrateError
from session_migrate.formats import omp
from session_migrate.inspection import detect_path_format
from session_migrate.model import (
    AgentFormat,
    Event,
    EventKind,
    Provenance,
    Role,
    Session,
    TargetFormat,
)

SESSION_ID = "44444444-4444-4444-8444-444444444444"
IMAGE_BYTES = b"synthetic-omp-image"
IMAGE_URL = "data:image/png;base64," + base64.b64encode(IMAGE_BYTES).decode()
FIXTURE = Path(__file__).parent / "fixtures/omp-18.0.5/basic.jsonl"


def _source(tmp_path: Path) -> Session:
    events = (
        Event(
            kind=EventKind.MESSAGE,
            role=Role.USER,
            text="OMP_SYNTHETIC_USER_ALPHA",
            timestamp="2026-08-25T12:00:00Z",
            provenance=Provenance(0, "user"),
        ),
        Event(
            kind=EventKind.CONTEXT,
            role=Role.USER,
            timestamp="2026-08-25T12:00:00Z",
            payload={"block_type": "image", "image_url": IMAGE_URL},
            provenance=Provenance(0, "user", block_index=1),
        ),
        Event(
            kind=EventKind.MESSAGE,
            role=Role.ASSISTANT,
            text="OMP_SYNTHETIC_ASSISTANT_BETA",
            timestamp="2026-08-25T12:00:01Z",
            provenance=Provenance(1, "assistant"),
        ),
        Event(
            kind=EventKind.TOOL_CALL,
            role=Role.ASSISTANT,
            tool_name="read",
            tool_call_id="omp_call_1",
            timestamp="2026-08-25T12:00:01Z",
            payload={"input": {"path": "fixture.txt"}},
            provenance=Provenance(1, "assistant", block_index=1),
        ),
        Event(
            kind=EventKind.TOOL_RESULT,
            role=Role.TOOL,
            text="OMP_SYNTHETIC_RESULT_GAMMA",
            tool_name="read",
            tool_call_id="omp_call_1",
            timestamp="2026-08-25T12:00:02Z",
            payload={
                "is_error": False,
                "content_blocks": [{"type": "text", "text": "OMP_SYNTHETIC_RESULT_GAMMA"}],
            },
            provenance=Provenance(2, "tool_result"),
        ),
        Event(
            kind=EventKind.COMPACTION,
            role=Role.SYSTEM,
            text="OMP_SYNTHETIC_SUMMARY_DELTA",
            timestamp="2026-08-25T12:00:03Z",
            provenance=Provenance(3, "compaction"),
        ),
        Event(
            kind=EventKind.MESSAGE,
            role=Role.USER,
            text="OMP_SYNTHETIC_POST_COMPACTION_EPSILON",
            timestamp="2026-08-25T12:00:04Z",
            provenance=Provenance(4, "user"),
        ),
        Event(
            kind=EventKind.MESSAGE,
            role=Role.ASSISTANT,
            text="OMP_SYNTHETIC_FINAL_ZETA",
            timestamp="2026-08-25T12:00:05Z",
            provenance=Provenance(5, "assistant"),
        ),
    )
    return Session(
        source_format=AgentFormat.CLAUDE,
        source_path=tmp_path / "source.jsonl",
        source_sha256="0" * 64,
        session_id="11111111-1111-4111-8111-111111111111",
        cwd=tmp_path,
        started_at="2026-08-25T12:00:00Z",
        cli_version="2.1.209",
        model="fixture-model",
        title="Fix timeline merging",
        events=events,
        raw_record_count=len(events),
        model_provider="anthropic",
    )


def _write_native(tmp_path: Path, source: Session | None = None) -> tuple[Path, bytes]:
    data, dropped = omp.serialize(
        source or _source(tmp_path),
        session_id=SESSION_ID,
        cwd=tmp_path,
        timestamp="2026-08-25T12:00:00Z",
    )
    path = tmp_path / "omp.jsonl"
    path.write_bytes(data)
    assert dropped == {}
    return path, data


def test_omp_current_journal_round_trip_and_detection(tmp_path: Path) -> None:
    path, data = _write_native(tmp_path)

    assert data.index(b"\n") + 1 == omp.TITLE_SLOT_BYTES
    assert detect_path_format(path) == AgentFormat.OMP
    omp.validate_native_bytes(data, SESSION_ID)
    parsed = omp.parse(path)

    assert parsed.session_id == SESSION_ID
    assert parsed.name == "Fix timeline merging"
    assert parsed.model == "fixture-model"
    assert parsed.provider == "anthropic"
    assert [event.kind for event in parsed.events] == [
        EventKind.MESSAGE,
        EventKind.CONTEXT,
        EventKind.MESSAGE,
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
        EventKind.COMPACTION,
        EventKind.MESSAGE,
        EventKind.MESSAGE,
    ]
    assert parsed.events[1].payload["image_url"] == IMAGE_URL
    assert parsed.events[3].tool_call_id == parsed.events[4].tool_call_id == "omp_call_1"

    source = load_session(path)
    assert source.source_format == AgentFormat.OMP
    assert source.title == "Fix timeline merging"
    assert source.event_counts() == {
        "compaction": 1,
        "context": 1,
        "message": 4,
        "tool_call": 1,
        "tool_result": 1,
    }


def test_omp_sanitized_current_fixture_is_authoritative() -> None:
    data = FIXTURE.read_bytes()
    omp.validate_native_bytes(data, "19191919-1919-4919-8919-191919191919")
    source = omp.parse_session(FIXTURE)

    assert source.source_format == AgentFormat.OMP
    assert source.title == "OMP synthetic fixture"
    assert source.model == "fixture-model"
    assert source.model_provider == "session-migrate-loopback"
    assert source.event_counts() == {
        "compaction": 1,
        "context": 1,
        "message": 4,
        "tool_call": 1,
        "tool_result": 1,
    }


def test_omp_is_a_first_class_conversion_target(tmp_path: Path) -> None:
    artifact = convert_session(
        _source(tmp_path),
        ConversionOptions(
            target_format=TargetFormat.OMP,
            session_id=SESSION_ID,
            cwd=tmp_path,
        ),
    )

    assert artifact.target_format == TargetFormat.OMP
    assert artifact.target_cli_version == omp.PINNED_OMP_VERSION
    assert artifact.native_record_count == 8
    omp.validate_native_bytes(artifact.native_bytes, SESSION_ID)


def test_omp_reads_legacy_slotless_v3_only_when_explicit(tmp_path: Path) -> None:
    path, data = _write_native(tmp_path)
    slotless = tmp_path / "slotless.jsonl"
    slotless.write_bytes(data[omp.TITLE_SLOT_BYTES :])

    parsed = omp.parse(slotless)
    assert parsed.name == "Fix timeline merging"
    assert detect_path_format(slotless) == AgentFormat.PI
    with pytest.raises(SessionMigrateError, match="fixed-width title slot"):
        omp.validate_native_bytes(slotless.read_bytes(), SESSION_ID)


def test_omp_title_slot_is_bounded_and_authoritative(tmp_path: Path) -> None:
    source = replace(_source(tmp_path), title="界" * 300)
    path, data = _write_native(tmp_path, source)
    first, header = [json.loads(line) for line in data.splitlines()[:2]]

    assert len(data.splitlines(keepends=True)[0]) == omp.TITLE_SLOT_BYTES
    assert 0 < len(first["title"]) < 300
    assert header["title"] == first["title"]
    assert omp.parse(path).name == first["title"]


def test_omp_resolves_content_addressed_image_blobs(tmp_path: Path) -> None:
    root = tmp_path / "agent"
    path = root / omp.session_relative_path(tmp_path, SESSION_ID, "2026-08-25T12:00:00Z")
    path.parent.mkdir(parents=True)
    data, _ = omp.serialize(
        _source(tmp_path),
        session_id=SESSION_ID,
        cwd=tmp_path,
        timestamp="2026-08-25T12:00:00Z",
    )
    records = [json.loads(line) for line in data.splitlines()]
    digest = hashlib.sha256(IMAGE_BYTES).hexdigest()
    image = records[2]["message"]["content"][1]
    image["data"] = f"blob:sha256:{digest}"
    body = b"".join(
        (json.dumps(record, separators=(",", ":")) + "\n").encode() for record in records[1:]
    )
    path.write_bytes(data[: omp.TITLE_SLOT_BYTES] + body)
    blobs = root / "blobs"
    blobs.mkdir()
    (blobs / digest).write_bytes(IMAGE_BYTES)

    parsed = omp.parse(path)
    context = next(event for event in parsed.events if event.kind == EventKind.CONTEXT)
    assert context.payload["image_url"] == IMAGE_URL

    (blobs / digest).write_bytes(b"wrong")
    with pytest.raises(SessionMigrateError, match="blob hash"):
        omp.parse(path)


def test_omp_rejects_symlinked_image_blob(tmp_path: Path) -> None:
    root = tmp_path / "agent"
    path = root / omp.session_relative_path(tmp_path, SESSION_ID, "2026-08-25T12:00:00Z")
    path.parent.mkdir(parents=True)
    data, _ = omp.serialize(_source(tmp_path), session_id=SESSION_ID, cwd=tmp_path)
    records = [json.loads(line) for line in data.splitlines()]
    digest = hashlib.sha256(IMAGE_BYTES).hexdigest()
    records[2]["message"]["content"][1]["data"] = f"blob:sha256:{digest}"
    body = b"".join(
        (json.dumps(record, separators=(",", ":")) + "\n").encode() for record in records[1:]
    )
    path.write_bytes(data[: omp.TITLE_SLOT_BYTES] + body)
    blobs = root / "blobs"
    blobs.mkdir()
    target = tmp_path / "outside"
    target.write_bytes(IMAGE_BYTES)
    os.symlink(target, blobs / digest)

    with pytest.raises(SessionMigrateError, match="blob is unavailable"):
        omp.parse(path)


def test_omp_accounts_for_private_and_runtime_entries(tmp_path: Path) -> None:
    path, data = _write_native(tmp_path)
    records = [json.loads(line) for line in data.splitlines()]
    last = records[-1]["id"]
    records.extend(
        [
            {
                "type": "credential_pin",
                "id": "omp-extra-1",
                "parentId": last,
                "timestamp": "2026-08-25T12:00:06Z",
                "provider": "synthetic",
            },
            {
                "type": "message",
                "id": "omp-extra-2",
                "parentId": "omp-extra-1",
                "timestamp": "2026-08-25T12:00:07Z",
                "message": {"role": "developer", "content": "PRIVATE_RUNTIME_CONTEXT"},
            },
        ]
    )
    path.write_bytes(
        data[: omp.TITLE_SLOT_BYTES]
        + b"".join(
            (json.dumps(record, separators=(",", ":")) + "\n").encode() for record in records[1:]
        )
    )

    source = omp.parse_session(path)
    reasons = [
        event.payload.get("reason") for event in source.events if event.kind == EventKind.OPAQUE
    ]
    assert "omp_credential_pin" in reasons
    assert "unknown_omp_message_role" in reasons
    assert all(event.text != "PRIVATE_RUNTIME_CONTEXT" for event in source.events)


def test_omp_reset_boundary_does_not_resurrect_old_model_context(tmp_path: Path) -> None:
    path, data = _write_native(tmp_path)
    records = [json.loads(line) for line in data.splitlines()]
    reset_parent = records[3]["id"]
    records = [
        *records[:4],
        {
            "type": "reset_boundary",
            "id": "omp-reset",
            "parentId": reset_parent,
            "timestamp": "2026-08-25T12:00:02.500Z",
        },
        {
            "type": "message",
            "id": "omp-after-reset",
            "parentId": "omp-reset",
            "timestamp": "2026-08-25T12:00:03Z",
            "message": {"role": "user", "content": "ONLY_POST_RESET_CONTEXT"},
        },
    ]
    path.write_bytes(
        data[: omp.TITLE_SLOT_BYTES]
        + b"".join(
            (json.dumps(record, separators=(",", ":")) + "\n").encode() for record in records[1:]
        )
    )

    source = omp.parse_session(path)
    visible = [event.text for event in source.events if event.kind == EventKind.MESSAGE]
    reasons = [
        event.payload.get("reason") for event in source.events if event.kind == EventKind.OPAQUE
    ]
    assert visible == ["ONLY_POST_RESET_CONTEXT"]
    assert reasons.count("omp_pre_reset_entry") == 2
    assert "omp_reset_boundary" in reasons


def test_omp_rejects_malformed_title_slot_and_tree(tmp_path: Path) -> None:
    path, data = _write_native(tmp_path)
    records = [json.loads(line) for line in data.splitlines()]
    records[0]["v"] = 2
    malformed = b"".join(
        (json.dumps(record, separators=(",", ":")) + "\n").encode() for record in records
    )
    with pytest.raises(SessionMigrateError, match="invalid title slot"):
        omp.validate_native_bytes(malformed, SESSION_ID)

    records = [json.loads(line) for line in data.splitlines()]
    records[2]["parentId"] = "missing"
    path.write_bytes(
        data[: omp.TITLE_SLOT_BYTES]
        + b"".join(
            (json.dumps(record, separators=(",", ":")) + "\n").encode() for record in records[1:]
        )
    )
    with pytest.raises(SessionMigrateError, match="missing parent"):
        omp.parse(path)


def test_omp_session_relative_paths_follow_current_bucket_rules(tmp_path: Path) -> None:
    relative = omp.session_relative_path(tmp_path, SESSION_ID, "2026-08-25T12:34:56.789Z")

    assert relative.parts[0] == "sessions"
    assert relative.parts[1].startswith("-tmp-")
    assert relative.name == f"2026-08-25T12-34-56-789Z_{SESSION_ID}.jsonl"
    assert not relative.is_absolute()
