import json
import os
import ssl
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest

from session_migrate.formats import antigravity_desktop
from session_migrate.model import AgentFormat, Event, EventKind, Provenance, Role, Session

TARGET_ID = "88888888-9999-4aaa-8bbb-cccccccccccc"
TRAJECTORY_ID = "dddddddd-eeee-4fff-8000-111111111111"


def exact_desktop_binary() -> Path:
    import sys

    if sys.platform != "darwin":
        pytest.skip(
            "Antigravity Desktop native language server test is currently pinned and verified "
            "for macOS; Linux and Windows binary verification is pending"
        )
    binary = antigravity_desktop.default_language_server_path()
    if not binary.is_file():
        pytest.skip("Antigravity Desktop language_server is not installed on this machine")
    try:
        return antigravity_desktop.verify_pinned_desktop(binary)
    except Exception as exc:
        pytest.skip(f"Antigravity Desktop binary verification failed: {exc}")


def test_native_desktop_language_server_loads_adapter_database(tmp_path: Path) -> None:
    tmp_path = tmp_path.resolve()
    binary = exact_desktop_binary()

    # Create a portable test session
    events = (
        Event(
            kind=EventKind.MESSAGE,
            role=Role.USER,
            text="Hello Antigravity Desktop from session-migrate",
            timestamp="2026-09-28T12:00:00Z",
            provenance=Provenance(0, "user"),
        ),
        Event(
            kind=EventKind.MESSAGE,
            role=Role.ASSISTANT,
            text="Hello! Native desktop language server validation passed.",
            timestamp="2026-09-28T12:00:01Z",
            provenance=Provenance(1, "assistant"),
        ),
    )
    session = Session(
        source_format=AgentFormat.ANTIGRAVITY_DESKTOP,
        source_path=tmp_path / "source.db",
        source_sha256="0" * 64,
        session_id=TARGET_ID,
        cwd=tmp_path,
        started_at="2026-09-28T12:00:00Z",
        cli_version=antigravity_desktop.PINNED_ANTIGRAVITY_DESKTOP_VERSION,
        model="gemini-2.5-pro",
        title="Native Integration Test",
        events=events,
        raw_record_count=len(events),
    )

    data, _ = antigravity_desktop.serialize(
        session,
        session_id=TARGET_ID,
        trajectory_id=TRAJECTORY_ID,
        cwd=tmp_path,
        timestamp="2026-09-28T12:00:00Z",
    )

    gemini_dir = tmp_path / ".gemini"
    app_state = gemini_dir / "antigravity"
    antigravity_desktop.install_database(
        data,
        session_id=TARGET_ID,
        cwd=tmp_path,
        timestamp="2026-09-28T12:00:00Z",
        title="Native Integration Test",
        target_home=app_state,
        dry_run=False,
    )

    csrf_token = "01234567-89ab-cdef-0123-456789abcdef"

    # Spawn language_server in the isolated directory
    cmd = [
        str(binary),
        "--standalone",
        "--override_ide_name",
        "antigravity",
        "--subclient_type",
        "hub",
        "--override_ide_version",
        "2.18.1",
        "--override_user_agent_name",
        "antigravity",
        "--https_server_port",
        "0",
        "--csrf_token",
        csrf_token,
        "-gemini_dir",
        str(gemini_dir),
        "-app_data_dir",
        "antigravity",
    ]
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)

    proc = subprocess.Popen(
        cmd,
        cwd=str(app_state),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert proc.stdin is not None
    proc.stdin.close()

    port = None
    try:
        import re

        for _ in range(50):
            line = proc.stdout.readline()
            if not line:
                break
            m = re.search(r"listening on \w+ port at (\d+) for HTTP", line, re.I)
            if m:
                port = int(m.group(1))
                break

        assert port is not None, "Failed to capture language_server port"

        # Query Connect-RPC GetCascadeTrajectorySteps
        url = f"https://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/GetCascadeTrajectorySteps"
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        req_body = json.dumps({"cascadeId": TARGET_ID}).encode()
        req = urllib.request.Request(
            url,
            data=req_body,
            headers={
                "Content-Type": "application/json",
                "X-Codeium-Csrf-Token": csrf_token,
            },
            method="POST",
        )

        res = None
        for _attempt in range(30):
            try:
                with urllib.request.urlopen(req, context=ctx, timeout=5) as resp:
                    if resp.status == 200:
                        data = json.loads(resp.read().decode())
                        s = data.get("steps", [])
                        if len(s) == 2 and "userInput" in s[0] and "plannerResponse" in s[1]:
                            res = data
                            break
            except (urllib.error.HTTPError, urllib.error.URLError):
                pass
            time.sleep(0.2)

        assert res is not None, "Failed to get response from language server"
        assert "steps" in res
        steps = res["steps"]
        assert len(steps) == 2
        # Step 0 is user input
        assert steps[0].get("type") == "CORTEX_STEP_TYPE_USER_INPUT"
        assert steps[0].get("status") == "CORTEX_STEP_STATUS_DONE"
        # Step 1 is planner response
        assert steps[1].get("type") == "CORTEX_STEP_TYPE_PLANNER_RESPONSE"
        assert steps[1].get("status") == "CORTEX_STEP_STATUS_DONE"

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
