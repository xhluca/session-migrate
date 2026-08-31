"""Opt-in credential-free native gate for the exact Devin CLI 3000.6.7 binary.

The gate never copies a user's Devin credentials and never sends a model
request.  Point ``SESSION_MIGRATE_DEVIN_BIN`` at the pinned Linux x86-64
binary to exercise the real CLI against an isolated native store.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from session_migrate.formats import devin
from session_migrate.model import AgentFormat, Event, EventKind, Provenance, Role, Session


def _native_binary() -> Path:
    configured = os.environ.get("SESSION_MIGRATE_DEVIN_BIN")
    if not configured:
        pytest.skip("set SESSION_MIGRATE_DEVIN_BIN to the exact pinned Devin CLI 3000.6.7 binary")
    binary = Path(configured)
    devin.verify_pinned_binary(binary)
    return binary


def _private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True)
    path.chmod(0o700)
    return path


def _isolated_environment(tmp_path: Path) -> tuple[dict[str, str], Path]:
    home = _private_directory(tmp_path / "home")
    data = _private_directory(tmp_path / "data")
    config = _private_directory(tmp_path / "config")
    cache = _private_directory(tmp_path / "cache")
    runtime = _private_directory(tmp_path / "runtime")
    temporary = _private_directory(tmp_path / "tmp")
    devin_config = _private_directory(config / "devin") / "config.json"
    devin_config.write_text('{"auto_update":false,"proxy":{"mode":"off"}}\n')
    devin_config.chmod(0o600)
    # Deliberately construct a small environment rather than inheriting API
    # keys, browser login state, or any Devin credential path from the runner.
    environment = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "XDG_CACHE_HOME": str(cache),
        "XDG_CONFIG_HOME": str(config),
        "XDG_DATA_HOME": str(data),
        "XDG_RUNTIME_DIR": str(runtime),
        "TMPDIR": str(temporary),
        "NO_COLOR": "1",
        "TERM": "dumb",
    }
    return environment, data / "devin/cli/sessions.db"


def _session(cwd: Path, marker: str, *, with_tool: bool) -> Session:
    events: list[Event] = [
        Event(
            EventKind.MESSAGE,
            Provenance(0, "user"),
            role=Role.USER,
            text=f"USER_{marker}",
            timestamp="2026-08-30T12:00:00Z",
        ),
        Event(
            EventKind.MESSAGE,
            Provenance(1, "assistant"),
            role=Role.ASSISTANT,
            text=f"ASSISTANT_{marker}",
            timestamp="2026-08-30T12:00:01Z",
        ),
    ]
    if with_tool:
        events.extend(
            [
                Event(
                    EventKind.TOOL_CALL,
                    Provenance(1, "assistant", block_index=1),
                    role=Role.ASSISTANT,
                    timestamp="2026-08-30T12:00:01Z",
                    tool_name="read_file",
                    tool_call_id=f"call-{marker.lower()}",
                    payload={"input": {"path": "README.md"}},
                ),
                Event(
                    EventKind.TOOL_RESULT,
                    Provenance(2, "tool"),
                    role=Role.TOOL,
                    text=f"RESULT_{marker}",
                    timestamp="2026-08-30T12:00:02Z",
                    tool_call_id=f"call-{marker.lower()}",
                    payload={"is_error": False},
                ),
            ]
        )
    return Session(
        source_format=AgentFormat.CLAUDE,
        source_path=cwd / "source.jsonl",
        source_sha256="0" * 64,
        session_id=f"source-{marker.lower()}",
        cwd=cwd,
        started_at="2026-08-30T12:00:00Z",
        cli_version="fixture",
        model="swe-1-6-fast",
        title=f"Native {marker} trajectory",
        events=tuple(events),
        raw_record_count=len(events),
    )


def _run(
    binary: Path,
    arguments: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(binary), *arguments],
        cwd=cwd,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_devin_300067_native_lists_multiple_imported_trajectories_and_gates_resume(
    tmp_path: Path,
) -> None:
    binary = _native_binary()
    environment, database = _isolated_environment(tmp_path)
    workspaces = [
        _private_directory(tmp_path / "workspace-alpha"),
        _private_directory(tmp_path / "workspace-beta"),
        _private_directory(tmp_path / "workspace-gamma"),
    ]

    version = _run(binary, ["--version"], cwd=workspaces[0], environment=environment)
    assert version.returncode == 0
    assert version.stdout.strip() == "devin 3000.6.7 (260a97c8)"

    # Let the real binary create its exact empty v16 database first.
    initialized = _run(
        binary,
        ["list", "--format", "json"],
        cwd=workspaces[0],
        environment=environment,
    )
    assert initialized.returncode == 0, initialized.stderr
    assert json.loads(initialized.stdout) == []
    assert database.is_file()

    identities = ("native-alpha", "native-beta", "native-gamma")
    for index, (identity, workspace) in enumerate(zip(identities, workspaces, strict=True)):
        artifact, dropped = devin.serialize(
            _session(workspace, identity.replace("native-", "").upper(), with_tool=index != 1),
            session_id=identity,
            cwd=workspace,
        )
        assert dropped == {}
        devin.install_database(artifact, database, identity)

    # `devin list` is cwd-filtered, whereas session-migrate intentionally
    # catalogs every visible row in the shared database.
    for identity, workspace in zip(identities, workspaces, strict=True):
        result = _run(
            binary,
            ["list", "--format", "json"],
            cwd=workspace,
            environment=environment,
        )
        assert result.returncode == 0, result.stderr
        listed = json.loads(result.stdout)
        assert [entry["id"] for entry in listed] == [identity]
        assert listed[0]["working_directory"] == str(workspace)
        assert listed[0]["title"] == f"Native {identity.removeprefix('native-').upper()} trajectory"

    summaries = devin.list_sessions(database)
    assert {summary.session_id for summary in summaries} == set(identities)
    assert {summary.cwd for summary in summaries} == set(workspaces)
    assert {summary.records for summary in summaries} == {3, 4}
    for identity in identities:
        parsed = devin.parse_session(database, identity)
        marker = identity.removeprefix("native-").upper()
        assert marker in " ".join(event.text or "" for event in parsed.events)
        if identity != "native-beta":
            assert any(event.kind == EventKind.TOOL_CALL for event in parsed.events)
            assert any(event.kind == EventKind.TOOL_RESULT for event in parsed.events)

    # With no credential file or inherited key, the current binary rejects
    # resume at authentication.  The imported native chain remains unchanged.
    before = devin.parse_session(database, identities[0]).source_sha256
    resumed = _run(
        binary,
        [
            "--resume",
            identities[0],
            "--print",
            "Do not call tools; acknowledge the imported marker.",
            "--respect-workspace-trust",
            "false",
        ],
        cwd=workspaces[0],
        environment=environment,
    )
    assert resumed.returncode != 0
    combined = f"{resumed.stdout}\n{resumed.stderr}".lower()
    assert "login canceled" in combined or "authentication" in combined or "log in" in combined
    assert devin.parse_session(database, identities[0]).source_sha256 == before
