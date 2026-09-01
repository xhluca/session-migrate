import json
import os
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from session_migrate.errors import SessionMigrateError
from session_migrate.formats import antigravity
from session_migrate.model import AgentFormat, Event, EventKind, Provenance, Role, Session

TARGET_ID = "77777777-8888-4999-8aaa-bbbbbbbbbbbb"
TRAJECTORY_ID = "cccccccc-dddd-4eee-8fff-000000000000"
CORPUS_ID = "a29dedb0-f6c6-497c-bad9-08cbdb556747"
CORPUS = Path(__file__).parent / "native_corpus/v1/sources/antigravity/1.1.16/portable-rich/native"


def exact_antigravity() -> Path:
    try:
        value = os.environ.get("SESSION_MIGRATE_ANTIGRAVITY_BIN")
        return antigravity.verify_pinned_cli(Path(value) if value else None)
    except SessionMigrateError as exc:
        pytest.skip(str(exc))


def _credential() -> Path:
    path = Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    if not path.is_file():
        pytest.skip("Antigravity OAuth state is unavailable for an isolated native run")
    return path


def _environment(home: Path, tmp_path: Path) -> dict[str, str]:
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "XDG_CACHE_HOME": str(tmp_path / "xdg-cache"),
        "PATH": os.environ.get("PATH", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": "C.UTF-8",
        "TERM": "dumb",
        "NO_COLOR": "1",
        "AGY_CLI_DISABLE_AUTO_UPDATE": "1",
        "AGY_CLI_HIDE_ACCOUNT_INFO": "1",
    }


def _install_credential(app_home: Path) -> None:
    copied = app_home / "antigravity-oauth-token"
    shutil.copyfile(_credential(), copied)
    os.chmod(copied, 0o600)


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


def test_antigravity_1116_native_corpus_contains_multimodal_tool_trajectory() -> None:
    conversation = CORPUS / "conversations" / f"{CORPUS_ID}.db"
    session = antigravity.parse_session(conversation)

    assert session.session_id == CORPUS_ID
    assert session.title == "inspect-portable-media"
    assert session.raw_record_count == 20
    assert session.event_counts() == {
        "message": 4,
        "opaque": 3,
        "thinking": 2,
        "tool_call": 9,
        "tool_result": 9,
    }
    calls = {
        event.tool_call_id: event for event in session.events if event.kind == EventKind.TOOL_CALL
    }
    results = {
        event.tool_call_id: event for event in session.events if event.kind == EventKind.TOOL_RESULT
    }
    assert calls.keys() == results.keys()
    expected_files = {
        "CORPUS_NOTE.txt",
        "corpus-card.png",
        "corpus-document.pdf",
        "corpus-tone.wav",
        "corpus-transition.mp4",
        "timeline.py",
    }
    observed_files = {
        Path(str(event.payload["input"].get("AbsolutePath"))).name
        for event in calls.values()
        if event.tool_name == "view_file" and isinstance(event.payload.get("input"), dict)
    }
    assert observed_files == expected_files
    assert all(results[call_id].payload.get("is_error") is False for call_id in calls)
    text = "\n".join(event.text or "" for event in session.events)
    for marker in (
        "COPPER_4821",
        "ORBIT_2048",
        "BLUE TRIANGLE",
        "ANTIGRAVITY_NATIVE_SOURCE_COMPLETE",
        "ANTIGRAVITY_FOLLOWUP_COMPLETE",
    ):
        assert marker in text


@pytest.mark.skipif(
    os.environ.get("SESSION_MIGRATE_RUN_ANTIGRAVITY_SOURCE") != "1",
    reason="set SESSION_MIGRATE_RUN_ANTIGRAVITY_SOURCE=1 for the live vendor source gate",
)
def test_antigravity_1116_creates_native_multimodal_source_from_empty_state(
    tmp_path: Path,
) -> None:
    cli = exact_antigravity()
    isolated_home = tmp_path / "home"
    app_home = antigravity.app_data_home(isolated_home)
    workspace = tmp_path / "work"
    app_home.mkdir(parents=True, mode=0o700)
    workspace.mkdir(mode=0o700)
    _install_credential(app_home)
    assets = Path(__file__).parent / "native_corpus/v1/assets"
    names = (
        "CORPUS_NOTE.txt",
        "corpus-card.png",
        "corpus-document.pdf",
        "corpus-tone.wav",
        "corpus-transition.mp4",
    )
    for name in names:
        shutil.copyfile(assets / name, workspace / name)

    completed = subprocess.run(
        [
            str(cli),
            "--new-project",
            "--add-dir",
            str(workspace),
            "--mode",
            "plan",
            "--effort",
            "low",
            "--dangerously-skip-permissions",
            "--print-timeout=2m",
            "--output-format",
            "json",
            "--print",
            (
                "Read and inspect @CORPUS_NOTE.txt, @corpus-card.png, "
                "@corpus-document.pdf, @corpus-tone.wav, and "
                "@corpus-transition.mp4 using native tools. Do not edit files. "
                "State the text markers, describe the image, and report the "
                "audio/video details. End with ANTIGRAVITY_NATIVE_SOURCE_COMPLETE."
            ),
        ],
        cwd=workspace,
        env=_environment(isolated_home, tmp_path),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    response = json.loads(completed.stdout)
    assert response["status"] == "SUCCESS"
    assert "ANTIGRAVITY_NATIVE_SOURCE_COMPLETE" in response["response"]
    session_id = response["conversation_id"]
    session = antigravity.parse_session(app_home / "conversations" / f"{session_id}.db")
    assert session.raw_record_count >= 10
    assert Counter(event.kind for event in session.events)[EventKind.TOOL_CALL] >= 5
    assert Counter(event.kind for event in session.events)[EventKind.TOOL_RESULT] >= 5


@pytest.mark.skipif(
    os.environ.get("SESSION_MIGRATE_RUN_ANTIGRAVITY_RELOAD") != "1",
    reason="set SESSION_MIGRATE_RUN_ANTIGRAVITY_RELOAD=1 for the exact-client cold reload",
)
def test_antigravity_1116_cold_reloads_sanitized_native_corpus_source(
    tmp_path: Path,
) -> None:
    cli = exact_antigravity()
    isolated_home = tmp_path / "home"
    app_home = antigravity.app_data_home(isolated_home)
    workspace = tmp_path / "work"
    shutil.copytree(CORPUS, app_home)
    workspace.mkdir(mode=0o700)
    _install_credential(app_home)
    conversation = app_home / "conversations" / f"{CORPUS_ID}.db"
    before = antigravity.parse_session(conversation)
    before_steps = tuple(
        event
        for event in before.events
        if event.provenance is not None and event.provenance.record_index < before.raw_record_count
    )

    followup = (
        "Continue this exact sanitized session without reading files. Name COPPER_4821 "
        "and ORBIT_2048, then end exactly with ANTIGRAVITY_SANITIZED_RELOAD_COMPLETE."
    )
    completed = subprocess.run(
        [
            str(cli),
            "--conversation",
            CORPUS_ID,
            "--add-dir",
            str(workspace),
            "--mode",
            "plan",
            "--effort",
            "low",
            "--dangerously-skip-permissions",
            "--print-timeout=2m",
            "--output-format",
            "json",
            "--print",
            followup,
        ],
        cwd=workspace,
        env=_environment(isolated_home, tmp_path),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    response = json.loads(completed.stdout)
    assert response["status"] == "SUCCESS"
    assert "ANTIGRAVITY_SANITIZED_RELOAD_COMPLETE" in response["response"]

    after = antigravity.parse_session(conversation)
    after_original_steps = tuple(
        event
        for event in after.events
        if event.provenance is not None and event.provenance.record_index < before.raw_record_count
    )
    assert after_original_steps == before_steps
    assert after.raw_record_count > before.raw_record_count
    assert any(followup in (event.text or "") for event in after.events)
    assert any(
        "ANTIGRAVITY_SANITIZED_RELOAD_COMPLETE" in (event.text or "") for event in after.events
    )
