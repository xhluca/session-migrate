import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from session_migrate.formats import opencode
from session_migrate.model import EventKind

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "opencode-source-1.17.20"
    / "comprehensive.json"
)
OPENCODE_FALLBACK = Path.home() / ".opencode/bin/opencode"
LOOPBACK_UUID = "44444444-4444-4444-8444-444444444444"


def exact_opencode() -> str:
    candidate = shutil.which("opencode")
    if candidate is None and OPENCODE_FALLBACK.is_file():
        candidate = str(OPENCODE_FALLBACK)
    if candidate is None:
        pytest.skip("opencode is not installed")
    completed = subprocess.run(
        [candidate, "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if completed.returncode != 0 or completed.stdout.strip() != opencode.PINNED_OPENCODE_VERSION:
        pytest.skip(
            f"native oracle requires opencode {opencode.PINNED_OPENCODE_VERSION}"
        )
    return candidate


def isolated_env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    values = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "TERM": "dumb",
        "NO_COLOR": "1",
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_CACHE_HOME": str(home / "cache"),
        "XDG_STATE_HOME": str(home / "state"),
        "OPENCODE_CONFIG_DIR": str(home / "opencode-config"),
        "OPENCODE_DISABLE_AUTOUPDATE": "true",
        "OPENCODE_DISABLE_PRUNE": "true",
    }
    for value in values.values():
        if value.startswith(str(home)):
            Path(value).mkdir(parents=True, exist_ok=True)
    return values


def invoke(
    binary: str,
    arguments: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [binary, *arguments, "--pure"],
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_opencode_source_official_import_export_and_portable_reimport(
    tmp_path: Path,
) -> None:
    binary = exact_opencode()
    work = tmp_path / "work"
    work.mkdir()
    env = isolated_env(tmp_path)

    imported = invoke(binary, ["import", str(FIXTURE)], cwd=work, env=env)
    assert imported.returncode == 0, imported.stderr
    assert "ses_33333333333343338333333333333333" in imported.stdout

    store = invoke(
        binary,
        [
            "db",
            "SELECT (SELECT count(*) FROM session) AS sessions, "
            "(SELECT count(*) FROM message) AS messages, "
            "(SELECT count(*) FROM part) AS parts",
            "--format",
            "json",
        ],
        cwd=work,
        env=env,
    )
    assert store.returncode == 0, store.stderr
    assert json.loads(store.stdout) == [{"sessions": 1, "messages": 4, "parts": 13}]

    exported = invoke(
        binary,
        ["export", "ses_33333333333343338333333333333333"],
        cwd=work,
        env=env,
    )
    assert exported.returncode == 0, exported.stderr
    official_export = tmp_path / "opencode-source-official-export.json"
    official_export.write_text(exported.stdout)
    source = opencode.parse_session(official_export)
    assert source.event_counts() == {
        "compaction": 1,
        "context": 1,
        "message": 3,
        "opaque": 15,
        "thinking": 1,
        "tool_call": 1,
        "tool_result": 1,
    }

    target_id = opencode.session_id_from_uuid(LOOPBACK_UUID)
    portable_bytes, dropped = opencode.serialize(
        source,
        session_id=target_id,
        cwd=work,
        provider_id="fixture",
        model_id="fixture-model",
        title="SYNTHETIC_OPENCODE_SOURCE_LOOPBACK",
    )
    opencode.validate_native_bytes(portable_bytes, target_id)
    portable_bundle = tmp_path / "opencode-source-portable-loopback.json"
    portable_bundle.write_bytes(portable_bytes)
    assert dropped == {
        "compaction:boundary_metadata": 1,
        "message:privileged_role": 1,
        "opaque:opencode_file_source_metadata": 1,
        "opaque:opencode_ignored_text": 1,
        "opaque:opencode_nonportable_file": 1,
        "opaque:opencode_parent_session": 1,
        "opaque:opencode_patch_part": 1,
        "opaque:opencode_session_metadata": 1,
        "opaque:opencode_session_summary": 1,
        "opaque:opencode_snapshot_part": 1,
        "opaque:opencode_step-finish_part": 1,
        "opaque:opencode_step-start_part": 1,
        "opaque:opencode_tool_metadata": 1,
        "opaque:opencode_tool_result_compacted": 1,
        "opaque:opencode_user_output_format": 1,
        "opaque:opencode_user_summary_metadata": 1,
        "opaque:opencode_user_tool_policy": 1,
        "thinking": 1,
    }

    reimported = invoke(binary, ["import", str(portable_bundle)], cwd=work, env=env)
    assert reimported.returncode == 0, reimported.stderr
    assert target_id in reimported.stdout
    reexported = invoke(binary, ["export", target_id], cwd=work, env=env)
    assert reexported.returncode == 0, reexported.stderr
    reexport_path = tmp_path / "opencode-source-portable-reexport.json"
    reexport_path.write_text(reexported.stdout)
    loopback = opencode.parse_session(reexport_path)

    assert loopback.title == "SYNTHETIC_OPENCODE_SOURCE_LOOPBACK"
    assert loopback.model == "fixture-model"
    assert loopback.model_provider == "fixture"
    assert loopback.event_counts() == {
        "compaction": 1,
        "context": 1,
        "message": 2,
        "tool_call": 1,
        "tool_result": 1,
    }
    replay = json.dumps(json.loads(reexported.stdout), sort_keys=True)
    for marker in (
        "SYNTHETIC_OPENCODE_USER_MARKER",
        "SYNTHETIC_OPENCODE_ASSISTANT_MARKER",
        "synthetic_opencode_call_1",
        "SYNTHETIC_OPENCODE_TOOL_RESULT",
        "SYNTHETIC_OPENCODE_COMPACTION_SUMMARY",
        "c3ludGhldGlj",
        "dG9vbC1pbWFnZQ==",
    ):
        assert marker in replay
    assert "SYNTHETIC_IGNORED_MARKER" not in replay
    assert not any(event.kind == EventKind.OPAQUE for event in loopback.events)
