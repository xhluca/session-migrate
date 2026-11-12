"""Pi >= 1.x message roles (system, bashExecution) migrate without sanitization."""

import json
import tempfile
from pathlib import Path

import pytest

from session_migrate import conversion
from session_migrate.errors import SessionMigrateError
from session_migrate.formats import claude, pi
from session_migrate.model import AgentFormat, EventKind, Role

FIXTURES = Path(__file__).parent / "fixtures"
NEW_ROLES_FIXTURE = FIXTURES / "pi-1.4.0" / "new-roles.jsonl"
TARGET_UUID = "22222222-2222-4222-8222-222222222222"


def test_pi_v1_fixture_parses_system_and_bash_execution_roles() -> None:
    parsed = pi.parse(NEW_ROLES_FIXTURE)

    assert parsed.session_id == "33333333-3333-4333-8333-333333333333"
    assert parsed.raw_record_count == 12
    assert [(event.kind, event.role) for event in parsed.events] == [
        (EventKind.OPAQUE, None),  # model_change
        (EventKind.OPAQUE, None),  # thinking_level_change
        (EventKind.MESSAGE, Role.SYSTEM),
        (EventKind.MESSAGE, Role.USER),
        (EventKind.MESSAGE, Role.ASSISTANT),
        (EventKind.TOOL_CALL, Role.ASSISTANT),
        (EventKind.TOOL_RESULT, Role.TOOL),
        (EventKind.MESSAGE, Role.USER),  # bashExecution projection (code ./)
        (EventKind.MESSAGE, Role.USER),
        (EventKind.MESSAGE, Role.SYSTEM),  # list-content system preamble
        (EventKind.OPAQUE, None),  # excludeFromContext bashExecution (dropped)
        (EventKind.MESSAGE, Role.USER),  # cancelled/truncated bashExecution
    ]

    system_event = parsed.events[2]
    assert "expert coding assistant" in (system_event.text or "")
    assert "Be concise." in (system_event.text or "")

    bash_event = parsed.events[7]
    assert bash_event.text == "[bash] code ./ (exit 0)"

    list_system_event = parsed.events[9]
    assert list_system_event.text == "Preamble section A.\n\nSection B."

    cancelled_event = parsed.events[11]
    assert cancelled_event.text is not None
    assert cancelled_event.text.startswith(
        "[bash] long-running-task (exit 1) [cancelled, truncated]"
    )
    assert "partial output" in cancelled_event.text


def test_pi_v1_bash_execution_with_output_and_nonzero_exit() -> None:
    path = NEW_ROLES_FIXTURE.read_text().splitlines()
    records = [json.loads(line) for line in path]
    for record in records:
        message = record.get("message") or {}
        if message.get("role") == "bashExecution":
            message["command"] = "npm test"
            message["output"] = "1 failed, 2 passed"
            message["exitCode"] = 1
    with tempfile.TemporaryDirectory() as tmp:
        modified = Path(tmp) / "modified.jsonl"
        modified.write_text("".join(json.dumps(record) + "\n" for record in records))
        parsed = pi.parse(modified)

    bash_event = next(
        event
        for event in parsed.events
        if event.kind == EventKind.MESSAGE
        and event.role == Role.USER
        and event.text
        and "[bash]" in event.text
    )
    assert bash_event.text == "[bash] npm test (exit 1)\n1 failed, 2 passed"


def test_pi_v1_unknown_role_is_still_rejected(tmp_path: Path) -> None:
    records = [
        {
            "type": "session",
            "version": 3,
            "id": "33333333-3333-4333-8333-333333333333",
            "timestamp": "2026-10-08T12:06:49.789Z",
            "cwd": "/tmp/session-migrate-fixture",
        },
        {
            "type": "message",
            "id": "00000001",
            "parentId": None,
            "timestamp": "2026-10-08T12:06:50.000Z",
            "message": {"role": "mystery", "content": "boom"},
        },
    ]
    path = tmp_path / "unknown-role.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records))

    with pytest.raises(SessionMigrateError, match="unsupported role"):
        pi.parse(path)


def test_pi_v1_fixture_converts_to_claude_with_bash_history_preserved(
    tmp_path: Path,
) -> None:
    artifact = conversion.convert_session(
        pi.parse_session(NEW_ROLES_FIXTURE),
        conversion.ConversionOptions(
            target_format=AgentFormat.CLAUDE,
            session_id=TARGET_UUID,
            cwd=tmp_path,
        ),
    )

    # The privileged system preambles (sections-form and list-form) are
    # deliberately dropped; the user-run bash commands must survive as
    # ordinary user history. The excludeFromContext bashExecution must never
    # appear in target bytes.
    assert artifact.dropped.get("message:privileged_role") == 2
    transcript = artifact.native_bytes.decode("utf-8")
    assert "[bash] code ./ (exit 0)" in transcript
    assert "SYNTHETIC_USER_MARKER" in transcript
    assert "EXCLUDED_PRIVATE_MARKER" not in transcript
    assert "secret-leak-command" not in transcript

    out_path = tmp_path / "converted.jsonl"
    out_path.write_bytes(artifact.native_bytes)
    reparsed = claude.parse(out_path)
    assert reparsed.session_id == TARGET_UUID


def test_pi_v1_events_round_trip_through_pi_writer(tmp_path: Path) -> None:
    session = pi.parse_session(NEW_ROLES_FIXTURE)

    data, dropped = pi.serialize(
        session,
        session_id=TARGET_UUID,
        cwd=tmp_path,
        timestamp="2026-10-08T12:06:49.789Z",
    )
    pi.validate_native_bytes(data, TARGET_UUID)

    assert dropped.get("message:privileged_role") == 2
    out_path = tmp_path / "round-trip.jsonl"
    out_path.write_bytes(data)
    reparsed = pi.parse(out_path)
    assert reparsed.session_id == TARGET_UUID
    assert "SYNTHETIC_FINAL_MARKER" in "\n".join(event.text or "" for event in reparsed.events)


def test_pi_v1_exclude_from_context_never_leaks_to_any_target(tmp_path: Path) -> None:
    """A bashExecution with excludeFromContext=true must not enter target bytes.

    Regression for the P1 review finding: Pi's ``!!`` private shell syntax
    sets ``excludeFromContext: true`` and the native transformer omits those
    records from model context. The migration must not copy them into the
    target's ordinary user history either.
    """
    session = pi.parse_session(NEW_ROLES_FIXTURE)
    for target in (
        AgentFormat.CLAUDE,
        AgentFormat.CODEX,
        AgentFormat.CURSOR,
        AgentFormat.PI,
    ):
        artifact = conversion.convert_session(
            session,
            conversion.ConversionOptions(
                target_format=target,
                session_id=TARGET_UUID,
                cwd=tmp_path,
            ),
        )
        target_bytes = artifact.native_bytes
        assert b"EXCLUDED_PRIVATE_MARKER" not in target_bytes, target
        assert b"secret-leak-command" not in target_bytes, target


def test_pi_v1_exclude_from_context_is_accounted_not_silent() -> None:
    """Excluded bashExecutions surface as opaque events, not silent drops."""
    parsed = pi.parse(NEW_ROLES_FIXTURE)
    opaque_reasons = [
        (event.payload or {}).get("reason")
        for event in parsed.events
        if event.kind == EventKind.OPAQUE
    ]
    assert "pi_bash_execution_excluded" in opaque_reasons


def test_pi_v1_system_list_content_text_blocks_are_preserved() -> None:
    """List-content system messages must not silently vanish."""
    parsed = pi.parse(NEW_ROLES_FIXTURE)
    system_texts = [
        event.text
        for event in parsed.events
        if event.kind == EventKind.MESSAGE and event.role == Role.SYSTEM
    ]
    assert any("Preamble section A." in (text or "") for text in system_texts)
    assert any("Section B." in (text or "") for text in system_texts)


def test_pi_v1_bash_execution_requires_command(tmp_path: Path) -> None:
    """bashExecution without a string command must be rejected at parse time."""
    bad = {
        "type": "message",
        "id": "00000001",
        "parentId": None,
        "timestamp": "2026-10-08T12:06:50.000Z",
        "message": {"role": "bashExecution", "output": "orphan"},
    }
    head = {
        "type": "session",
        "version": 3,
        "id": "33333333-3333-4333-8333-333333333333",
        "timestamp": "2026-10-08T12:06:49.789Z",
        "cwd": "/tmp/session-migrate-fixture",
    }
    path = tmp_path / "bad-bash.jsonl"
    path.write_text(json.dumps(head) + "\n" + json.dumps(bad) + "\n")

    with pytest.raises(SessionMigrateError, match="missing its command"):
        pi.parse(path)


def test_pi_v1_system_rejects_scalar_content(tmp_path: Path) -> None:
    """A system message with non-string/non-list content must be rejected."""
    bad = {
        "type": "message",
        "id": "00000001",
        "parentId": None,
        "timestamp": "2026-10-08T12:06:50.000Z",
        "message": {"role": "system", "content": 12345},
    }
    head = {
        "type": "session",
        "version": 3,
        "id": "33333333-3333-4333-8333-333333333333",
        "timestamp": "2026-10-08T12:06:49.789Z",
        "cwd": "/tmp/session-migrate-fixture",
    }
    path = tmp_path / "bad-system.jsonl"
    path.write_text(json.dumps(head) + "\n" + json.dumps(bad) + "\n")

    with pytest.raises(SessionMigrateError, match="invalid content"):
        pi.parse(path)
