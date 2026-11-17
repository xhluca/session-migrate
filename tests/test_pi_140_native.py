"""Native Pi 1.4.0 cold-load gate for the >= 1.x system and bashExecution roles.

The fixture-level tests in ``tests/test_pi_v1_message_roles.py`` verify that
``session-migrate`` projects the new Pi 1.x roles correctly. This module asks
a real Pi 1.4.0 binary to cold-load two artifacts via RPC:

1. The synthetic 1.4.0 fixture itself, proving the new roles are accepted by
   the native client.
2. The migrator's own Pi-target output produced from that fixture, proving
   that a round-trip through ``session-migrate`` still cold-loads in 1.4.0
   and that the migrator's ``excludeFromContext`` promise holds end-to-end
   (the excluded payload must not appear in the migrated file bytes).

Pi's RPC ``get_messages`` returns the *raw* AgentMessage view, not the
``convertToLlm`` projection, so this gate does not try to introspect the
native transformer's behavior. The excludeFromContext contract is enforced
by asserting on the migrator's own output bytes.

Skip conditions mirror the other native gates: the test only runs when
``SESSION_MIGRATE_PI_140_BIN`` points at the exact pinned 1.4.0 client, which
``scripts/install-native-test-clis.sh pi-1.4.0`` arranges in CI.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from session_migrate import conversion
from session_migrate.formats import pi
from session_migrate.model import AgentFormat

PI_140_VERSION = "1.4.0"
FIXTURE = Path(__file__).parent / "fixtures" / "pi-1.4.0" / "new-roles.jsonl"
TARGET_UUID = "22222222-2222-4222-8222-222222222222"


def _pi_140_binary() -> Path:
    value = os.environ.get("SESSION_MIGRATE_PI_140_BIN")
    if not value:
        pytest.skip("set SESSION_MIGRATE_PI_140_BIN to the exact pinned Pi 1.4.0 client")
    binary = Path(value)
    if not binary.is_file():
        pytest.skip(f"Pi 1.4.0 binary not present at {binary}")
    completed = subprocess.run(
        [str(binary), "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if completed.returncode != 0 or completed.stdout.strip() != PI_140_VERSION:
        pytest.skip(
            f"native oracle requires pi {PI_140_VERSION}, "
            f"got {completed.stdout.strip()!r} (rc={completed.returncode})"
        )
    return binary


def _isolated_env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "TERM": "dumb",
        "NO_COLOR": "1",
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_CACHE_HOME": str(home / "cache"),
        "XDG_STATE_HOME": str(home / "state"),
        "PI_CODING_AGENT_DIR": str(home / "pi-agent"),
        "PI_OFFLINE": "1",
    }
    for path in (
        home,
        Path(env["XDG_DATA_HOME"]),
        Path(env["XDG_CONFIG_HOME"]),
        Path(env["XDG_CACHE_HOME"]),
        Path(env["XDG_STATE_HOME"]),
        Path(env["PI_CODING_AGENT_DIR"]),
    ):
        path.mkdir(parents=True, exist_ok=True)
    return env


def _rpc_responses(stdout: str) -> dict[str, dict]:
    responses: dict[str, dict] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("type") == "response":
            responses[str(value.get("command"))] = value
    return responses


def _rpc_cold_load(binary: Path, session_file: Path, tmp_path: Path) -> dict[str, dict]:
    """Run ``pi --mode rpc --session <file>`` and return the RPC responses."""

    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    session_dir = tmp_path / "sessions"
    session_dir.mkdir(exist_ok=True)
    fixture_copy = session_dir / session_file.name
    if fixture_copy.resolve() != session_file.resolve():
        shutil.copyfile(session_file, fixture_copy)

    commands = "\n".join(
        [
            json.dumps({"type": "get_messages"}),
            json.dumps({"type": "get_entries"}),
        ]
    )

    completed = subprocess.run(
        [
            str(binary),
            "--mode",
            "rpc",
            "--offline",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-context-files",
            "--session",
            str(fixture_copy),
            "--session-dir",
            str(session_dir),
        ],
        cwd=work,
        env=_isolated_env(tmp_path),
        input=commands + "\n",
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, (
        f"pi 1.4.0 exited {completed.returncode}\n"
        f"stderr:\n{completed.stderr}\n"
        f"stdout head:\n{completed.stdout[:2000]}"
    )
    responses = _rpc_responses(completed.stdout)
    assert "get_messages" in responses, f"missing get_messages response: {list(responses)}"
    assert responses["get_messages"].get("success") is True, responses["get_messages"]
    return responses


def test_pi_140_cold_loads_fixture_with_v1_roles(tmp_path: Path) -> None:
    """A real Pi 1.4.0 binary accepts the synthetic 1.4.0 fixture.

    The fixture contains a sections-form system preamble, a list-form system
    preamble, normal / cancelled+truncated / ``excludeFromContext: true``
    bashExecutions, and regular user/assistant/toolResult history. Cold-load
    success proves the on-disk shape session-migrate parses is the same
    shape the native client reads.
    """

    binary = _pi_140_binary()
    responses = _rpc_cold_load(binary, FIXTURE, tmp_path)
    messages = responses["get_messages"]["data"]["messages"]
    # Cold-load must return a non-empty history with the original roles
    # preserved (RPC exposes the raw AgentMessage view, not convertToLlm).
    assert messages, "Pi 1.4.0 returned an empty message list for the 1.x fixture"
    roles = [message.get("role") for message in messages]
    assert "user" in roles
    assert "assistant" in roles


def test_pi_140_cold_loads_migrated_output_and_excluded_payload_is_absent(
    tmp_path: Path,
) -> None:
    """End-to-end: migrate the 1.x fixture to a fresh Pi file, cold-load it.

    This is the native-side counterpart to the migrator's
    ``excludeFromContext`` promise. The migrator writes its own Pi output
    (which never contains the excluded payload), and the real Pi 1.4.0
    binary must accept it.
    """

    binary = _pi_140_binary()
    artifact = conversion.convert_session(
        pi.parse_session(FIXTURE),
        conversion.ConversionOptions(
            target_format=AgentFormat.PI,
            session_id=TARGET_UUID,
            cwd=tmp_path,
        ),
    )

    # The migrator's promise: the excluded payload never reaches target bytes.
    assert b"EXCLUDED_PRIVATE_MARKER" not in artifact.native_bytes
    assert b"secret-leak-command" not in artifact.native_bytes

    # The migrated file must cold-load in a real Pi 1.4.0 client.
    migrated = tmp_path / "migrated.jsonl"
    migrated.write_bytes(artifact.native_bytes)
    pi.validate_native_bytes(artifact.native_bytes, TARGET_UUID)
    responses = _rpc_cold_load(binary, migrated, tmp_path)
    messages = responses["get_messages"]["data"]["messages"]
    serialized = json.dumps(messages, sort_keys=True)

    # The non-excluded user history must survive the round trip; the excluded
    # payload must not appear in the cold-loaded view either (it was never
    # written into the file in the first place).
    assert "SYNTHETIC_USER_MARKER" in serialized
    assert "EXCLUDED_PRIVATE_MARKER" not in serialized
    assert "secret-leak-command" not in serialized


def test_pi_140_reader_parses_same_fixture() -> None:
    """Sanity: the migrator's own reader still parses the same fixture.

    This test does not require the native binary; it cross-checks that the
    fixture the native gate consumes is also the fixture the migrator's
    reader is tested against, so the two cannot drift.
    """

    parsed = pi.parse(FIXTURE)
    assert parsed.session_id == "33333333-3333-4333-8333-333333333333"
    # 12 records: session header + 11 entries (see fixture).
    assert parsed.raw_record_count == 12
