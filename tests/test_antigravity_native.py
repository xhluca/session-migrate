import os
import shutil
import subprocess
from pathlib import Path

import pytest

from session_migrate.errors import SessionMigrateError
from session_migrate.formats import antigravity
from session_migrate.model import AgentFormat, Event, EventKind, Provenance, Role, Session

TARGET_ID = "77777777-8888-4999-8aaa-bbbbbbbbbbbb"
TRAJECTORY_ID = "cccccccc-dddd-4eee-8fff-000000000000"


def exact_antigravity() -> Path:
    try:
        return antigravity.verify_pinned_cli()
    except SessionMigrateError as exc:
        pytest.skip(str(exc))


def test_antigravity_1116_loads_adapter_database_and_appends_native_turn(
    tmp_path: Path,
) -> None:
    """Exercise the real runtime without printing or reinterpreting its credential."""

    cli = exact_antigravity()
    credential = Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    if not credential.is_file():
        pytest.skip("Antigravity OAuth state is unavailable for an isolated native resume")

    isolated_home = tmp_path / "home"
    app_home = antigravity.app_data_home(isolated_home)
    app_home.mkdir(parents=True, mode=0o700)
    copied_credential = app_home / credential.name
    shutil.copyfile(credential, copied_credential)
    os.chmod(copied_credential, 0o600)
    workspace = tmp_path / "work"
    workspace.mkdir(mode=0o700)

    events = (
        Event(
            kind=EventKind.MESSAGE,
            role=Role.USER,
            text="AGY_NATIVE_USER_ALPHA",
            provenance=Provenance(0, "user"),
        ),
        Event(
            kind=EventKind.MESSAGE,
            role=Role.ASSISTANT,
            text="AGY_NATIVE_ASSISTANT_OMEGA",
            provenance=Provenance(1, "assistant"),
        ),
        Event(
            kind=EventKind.TOOL_CALL,
            role=Role.ASSISTANT,
            tool_name="echo_marker",
            tool_call_id="agy-native-call-1",
            payload={"input": {"text": "AGY_NATIVE_TOOL_INPUT"}},
            provenance=Provenance(2, "tool_call"),
        ),
        Event(
            kind=EventKind.TOOL_RESULT,
            role=Role.TOOL,
            text="AGY_NATIVE_TOOL_RESULT",
            tool_name="echo_marker",
            tool_call_id="agy-native-call-1",
            payload={"content_blocks": [{"type": "text", "text": "AGY_NATIVE_TOOL_RESULT"}]},
            provenance=Provenance(3, "tool_result"),
        ),
        Event(
            kind=EventKind.THINKING,
            role=Role.ASSISTANT,
            text="AGY_NATIVE_PRIVATE_THINKING_MUST_NOT_SURVIVE",
            provenance=Provenance(4, "thinking"),
        ),
    )
    source = Session(
        source_format=AgentFormat.CLAUDE,
        source_path=tmp_path / "source.jsonl",
        source_sha256="0" * 64,
        session_id=None,
        cwd=workspace,
        started_at="2026-08-20T12:00:00Z",
        cli_version="2.1.209",
        model="fixture-model",
        title="Native Antigravity oracle",
        events=events,
        raw_record_count=len(events),
    )
    data, dropped = antigravity.serialize(
        source,
        session_id=TARGET_ID,
        trajectory_id=TRAJECTORY_ID,
        cwd=workspace,
        timestamp=source.started_at,
    )
    assert dropped == {"thinking:private": 1}
    assert b"AGY_NATIVE_PRIVATE_THINKING_MUST_NOT_SURVIVE" not in data
    installed = antigravity.install_database(
        data,
        session_id=TARGET_ID,
        cwd=workspace,
        timestamp=source.started_at or "",
        title=source.title,
        target_home=app_home,
        target_cli=cli,
    )
    before = antigravity.snapshot_database_bytes(installed.conversation_path)

    env = {
        "HOME": str(isolated_home),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "XDG_CACHE_HOME": str(tmp_path / "xdg-cache"),
        "PATH": os.environ.get("PATH", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": "C.UTF-8",
        "TERM": "dumb",
        "NO_COLOR": "1",
    }
    completed = subprocess.run(
        [
            str(cli),
            f"--conversation={TARGET_ID}",
            "--print-timeout=30s",
            "--print",
            "AGY_NATIVE_APPEND_GAMMA",
        ],
        cwd=workspace,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=60,
        check=False,
    )
    # An account without a model appends an error row and exits 1; an enabled
    # account may complete and exit 0.  Both paths prove native resume/append.
    assert completed.returncode in {0, 1}

    after = antigravity.snapshot_database_bytes(installed.conversation_path)
    assert after != before
    parsed = antigravity.parse(installed.conversation_path)
    assert [(event.kind, event.text) for event in parsed.events[:2]] == [
        (EventKind.MESSAGE, "AGY_NATIVE_USER_ALPHA"),
        (EventKind.MESSAGE, "AGY_NATIVE_ASSISTANT_OMEGA"),
    ]
    assert any(event.text == "AGY_NATIVE_TOOL_RESULT" for event in parsed.events)
    assert any(event.text == "AGY_NATIVE_APPEND_GAMMA" for event in parsed.events)
    assert b"AGY_NATIVE_PRIVATE_THINKING_MUST_NOT_SURVIVE" not in after
