import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from session_migrate.errors import SessionMigrateError
from session_migrate.formats import kilo, opencode
from session_migrate.model import AgentFormat

FIXTURE = Path(__file__).parent / "fixtures" / "opencode-source-1.17.20" / "comprehensive.json"


def test_kilo_source_projects_official_export_bundle() -> None:
    source = kilo.parse_session(FIXTURE)

    assert source.source_format == AgentFormat.KILO
    assert source.source_sha256 == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert source.session_id == "ses_33333333333343338333333333333333"
    assert source.event_counts()["tool_call"] == 1
    assert source.event_counts()["tool_result"] == 1


def test_kilo_writer_round_trips_portable_history() -> None:
    source = kilo.parse_session(FIXTURE)
    target_id = kilo.session_id_from_uuid("44444444-4444-4444-8444-444444444444")

    data, dropped = kilo.serialize(
        source,
        session_id=target_id,
        cwd=Path("/tmp/session-migrate-kilo"),
        provider_id="fixture",
        model_id="fixture-model",
    )

    kilo.validate_native_bytes(data, target_id)
    value = json.loads(data)
    assert value["info"]["version"] == kilo.PINNED_KILO_VERSION
    assert kilo.native_record_count(data) > len(value["messages"])
    assert dropped


def test_kilo_validator_rejects_wrong_session_id_and_empty_history() -> None:
    source = kilo.parse_session(FIXTURE)
    target_id = kilo.session_id_from_uuid("55555555-5555-4555-8555-555555555555")
    data, _ = kilo.serialize(source, session_id=target_id, cwd=Path("/tmp/kilo"))

    with pytest.raises(SessionMigrateError, match="does not match"):
        wrong_id = kilo.session_id_from_uuid("66666666-6666-4666-8666-666666666666")
        kilo.validate_native_bytes(data, wrong_id)

    value = json.loads(data)
    value["messages"] = []
    with pytest.raises(SessionMigrateError, match="no resumable"):
        kilo.validate_native_bytes(json.dumps(value).encode(), target_id)


def test_kilo_source_identity_is_not_an_opencode_alias() -> None:
    source = opencode.parse_session(FIXTURE)
    kilo_source = replace(source, source_format=AgentFormat.KILO)

    assert source.source_format == AgentFormat.OPENCODE
    assert kilo_source.source_format == AgentFormat.KILO
