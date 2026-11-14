import json
import os
import time
from pathlib import Path

import pytest

from session_migrate.cli import main
from session_migrate.conversion import (
    ConversionOptions,
    convert_session,
    install_claude_artifact,
    write_claude_artifact,
)
from session_migrate.errors import SessionMigrateError
from session_migrate.formats import claude
from session_migrate.model import (
    AgentFormat,
    Event,
    EventKind,
    Provenance,
    Role,
    Session,
    TargetFormat,
)

FIXTURES = Path(__file__).parent / "fixtures"
SOURCE_CLAUDE_FILE = FIXTURES / "claude-2.1.209" / "basic.jsonl"
TEST_SESSION_ID = "22222222-2222-4222-8222-222222222222"


def _sample_session(tmp_path: Path) -> Session:
    return Session(
        source_format=AgentFormat.CLAUDE,
        source_path=tmp_path / "sample.jsonl",
        source_sha256="abc123",
        session_id=TEST_SESSION_ID,
        cwd=tmp_path / "my_project",
        started_at="2026-08-17T12:00:00Z",
        cli_version="2.1.209",
        model="claude-3-5-sonnet-20241022",
        title="Test Desktop Session",
        events=(
            Event(
                kind=EventKind.MESSAGE,
                role=Role.USER,
                text="Hello Claude Desktop",
                timestamp="2026-08-17T12:00:00Z",
                provenance=Provenance(record_index=0, record_type="user", source_id="u1"),
            ),
            Event(
                kind=EventKind.MESSAGE,
                role=Role.ASSISTANT,
                text="Hello human!",
                timestamp="2026-08-17T12:05:00Z",
                provenance=Provenance(record_index=1, record_type="assistant", source_id="a1"),
            ),
        ),
        raw_record_count=2,
    )


def test_resolve_desktop_sessions_dir_explicit(tmp_path: Path) -> None:
    desktop_root = tmp_path / "Claude"
    desktop_root.mkdir()

    # Pass root directory (without trailing claude-code-sessions)
    resolved = claude.resolve_desktop_sessions_dir(desktop_dir=desktop_root)
    assert resolved == (desktop_root / "claude-code-sessions").resolve()

    # Pass directory already ending in claude-code-sessions
    sessions_dir = desktop_root / "claude-code-sessions"
    sessions_dir.mkdir()
    resolved_direct = claude.resolve_desktop_sessions_dir(desktop_dir=sessions_dir)
    assert resolved_direct == sessions_dir.resolve()


def test_resolve_desktop_sessions_dir_environ(tmp_path: Path) -> None:
    custom_dir = tmp_path / "custom_desktop"
    custom_dir.mkdir()

    env = {"CLAUDE_DESKTOP_CONFIG_DIR": str(custom_dir)}
    resolved = claude.resolve_desktop_sessions_dir(environ=env)
    assert resolved == (custom_dir / "claude-code-sessions").resolve()


def test_resolve_desktop_sessions_dir_target_home(tmp_path: Path) -> None:
    home = tmp_path / "target_home"
    sessions = home / "claude-code-sessions"
    sessions.mkdir(parents=True)

    # Isolated environ preventing OS path detection
    env = {"HOME": str(tmp_path / "empty_home"), "CLAUDE_DESKTOP_CONFIG_DIR": ""}
    resolved = claude.resolve_desktop_sessions_dir(environ=env, target_home=home)
    assert resolved == sessions.resolve()


def test_resolve_desktop_sessions_dir_returns_none_when_absent(tmp_path: Path) -> None:
    import sys

    empty_home = tmp_path / "empty_home"
    empty_home.mkdir()
    env = {
        "HOME": str(empty_home),
        "XDG_CONFIG_HOME": str(empty_home),
        "APPDATA": str(empty_home),
    }
    fake_nonexistent = tmp_path / "nonexistent"
    orig_platform = sys.platform
    try:
        sys.platform = "linux"
        res = claude.resolve_desktop_sessions_dir(environ=env, target_home=fake_nonexistent)
        assert res is None
    finally:
        sys.platform = orig_platform


def test_find_desktop_project_dir_matching_cwd(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "claude-code-sessions"
    org1_proj1 = sessions_dir / "org1" / "proj1"
    org1_proj1.mkdir(parents=True)
    org2_proj2 = sessions_dir / "org2" / "proj2"
    org2_proj2.mkdir(parents=True)

    target_cwd = (tmp_path / "my_project").resolve()
    target_cwd.mkdir(parents=True)

    pointer1 = org1_proj1 / "local_111.json"
    pointer1.write_text(json.dumps({"cwd": str(target_cwd)}))

    pointer2 = org2_proj2 / "local_222.json"
    pointer2.write_text(json.dumps({"cwd": "/some/other/repo"}))

    selected = claude.find_desktop_project_dir(sessions_dir, target_cwd)
    assert selected == org1_proj1


def test_find_desktop_project_dir_no_guess_by_mtime(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "claude-code-sessions"
    org1_proj1 = sessions_dir / "org1" / "proj1"
    org1_proj1.mkdir(parents=True)
    org2_proj2 = sessions_dir / "org2" / "proj2"
    org2_proj2.mkdir(parents=True)

    p1 = org1_proj1 / "local_111.json"
    p1.write_text(json.dumps({"cwd": "/old/repo"}))
    os.utime(p1, (time.time() - 100, time.time() - 100))

    p2 = org2_proj2 / "local_222.json"
    p2.write_text(json.dumps({"cwd": "/recent/repo"}))
    os.utime(p2, (time.time(), time.time()))

    # Looking for a cwd that doesn't match any existing pointer:
    # do NOT guess the account/organization from the newest pointer!
    unmatched_cwd = tmp_path / "unmatched"
    selected = claude.find_desktop_project_dir(sessions_dir, unmatched_cwd)
    assert selected == (
        sessions_dir / claude.DEFAULT_DESKTOP_ORG_ID / claude.DEFAULT_DESKTOP_PROJECT_ID
    )


def test_find_desktop_project_dir_empty_initializes_defaults(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "claude-code-sessions"
    sessions_dir.mkdir()

    selected = claude.find_desktop_project_dir(sessions_dir, tmp_path / "any")
    expected = sessions_dir / claude.DEFAULT_DESKTOP_ORG_ID / claude.DEFAULT_DESKTOP_PROJECT_ID
    assert selected == expected


def test_desktop_pointer_data_structure(tmp_path: Path) -> None:
    session = _sample_session(tmp_path)
    data = claude.desktop_pointer_data(
        session_id=session.session_id,
        cwd=session.cwd,
        timestamp=session.started_at,
        model=session.model,
        title=session.title,
        events=session.events,
    )

    assert data["sessionId"] == f"local_{TEST_SESSION_ID}"
    assert data["cliSessionId"] == TEST_SESSION_ID
    assert data["cwd"] == str(session.cwd.resolve())
    assert data["originCwd"] == str(session.cwd.resolve())
    assert data["model"] == "claude-3-5-sonnet-20241022"
    assert data["title"] == "Test Desktop Session"
    assert data["isArchived"] is False
    assert data["titleSource"] == "custom"
    assert data["permissionMode"] == "default"
    assert data["importedFrom"] == "local-1p-code"
    assert data["alwaysAllowedReasons"] == []
    assert data["sessionPermissionUpdates"] == []

    # Timestamp in ms: 2026-08-17T12:00:00Z = 1786968000000
    assert isinstance(data["createdAt"], int)
    assert isinstance(data["lastActivityAt"], int)
    # The second event is at 12:05:00Z, so lastActivityAt should be greater than createdAt
    assert data["lastActivityAt"] > data["createdAt"]
    assert data["lastFocusedAt"] == data["lastActivityAt"]


def test_install_claude_artifact_with_desktop_pointer(tmp_path: Path) -> None:
    session = _sample_session(tmp_path)
    options = ConversionOptions(
        target_format=TargetFormat.CLAUDE,
        session_id=TEST_SESSION_ID,
        cwd=session.cwd,
    )
    artifact = convert_session(session, options)

    target_home = tmp_path / "claude_home"
    desktop_dir = tmp_path / "claude_desktop" / "claude-code-sessions"
    desktop_dir.mkdir(parents=True)

    native_path, manifest_path, pointer_path = install_claude_artifact(
        artifact,
        target_home=target_home,
        desktop_dir=desktop_dir,
    )

    assert native_path.is_file()
    assert manifest_path.is_file()
    assert pointer_path is not None
    assert pointer_path.is_file()
    assert pointer_path.name == f"local_{TEST_SESSION_ID}.json"

    # Verify pointer contents
    data = json.loads(pointer_path.read_text(encoding="utf-8"))
    assert data["cliSessionId"] == TEST_SESSION_ID
    assert data["sessionId"] == f"local_{TEST_SESSION_ID}"
    assert data["cwd"] == str(session.cwd.resolve())


def test_install_claude_artifact_dry_run(tmp_path: Path) -> None:
    session = _sample_session(tmp_path)
    options = ConversionOptions(
        target_format=TargetFormat.CLAUDE,
        session_id=TEST_SESSION_ID,
        cwd=session.cwd,
    )
    artifact = convert_session(session, options)

    target_home = tmp_path / "claude_home"
    desktop_dir = tmp_path / "claude_desktop" / "claude-code-sessions"
    desktop_dir.mkdir(parents=True)

    native_path, manifest_path, pointer_path = install_claude_artifact(
        artifact,
        target_home=target_home,
        desktop_dir=desktop_dir,
        dry_run=True,
    )

    assert not native_path.exists()
    assert not manifest_path.exists()
    assert pointer_path is not None
    assert not pointer_path.exists()


def test_install_claude_artifact_collision_fails(tmp_path: Path) -> None:
    session = _sample_session(tmp_path)
    options = ConversionOptions(
        target_format=TargetFormat.CLAUDE,
        session_id=TEST_SESSION_ID,
        cwd=session.cwd,
    )
    artifact = convert_session(session, options)

    target_home = tmp_path / "claude_home"
    desktop_dir = tmp_path / "claude_desktop" / "claude-code-sessions"
    desktop_dir.mkdir(parents=True)

    # First install succeeds
    install_claude_artifact(artifact, target_home=target_home, desktop_dir=desktop_dir)

    # Second install should raise error because files exist
    with pytest.raises(SessionMigrateError, match="refusing to overwrite"):
        install_claude_artifact(artifact, target_home=target_home, desktop_dir=desktop_dir)


def test_write_claude_artifact_with_desktop_dir(tmp_path: Path) -> None:
    session = _sample_session(tmp_path)
    options = ConversionOptions(
        target_format=TargetFormat.CLAUDE,
        session_id=TEST_SESSION_ID,
        cwd=session.cwd,
    )
    artifact = convert_session(session, options)

    output_path = tmp_path / "out.jsonl"
    manifest_path = tmp_path / "manifest.json"
    desktop_dir = tmp_path / "claude_desktop" / "claude-code-sessions"
    desktop_dir.mkdir(parents=True)

    out_p, man_p, point_p = write_claude_artifact(
        artifact,
        output_path=output_path,
        manifest_path=manifest_path,
        desktop_dir=desktop_dir,
    )

    assert out_p.is_file()
    assert man_p.is_file()
    assert point_p is not None
    assert point_p.is_file()
    data = json.loads(point_p.read_text(encoding="utf-8"))
    assert data["cliSessionId"] == TEST_SESSION_ID


def test_cli_import_to_claude_with_desktop_flag(tmp_path: Path) -> None:
    claude_home = tmp_path / "target_claude"
    desktop_dir = tmp_path / "desktop" / "claude-code-sessions"
    desktop_dir.mkdir(parents=True)
    target_sid = "11111111-2222-3333-4444-555555555555"

    code = main(
        [
            "import",
            str(SOURCE_CLAUDE_FILE),
            "--to",
            "claude",
            "--home",
            str(claude_home),
            "--session-id",
            target_sid,
            "--claude-desktop-dir",
            str(desktop_dir),
        ]
    )
    assert code == 0

    pointer = (
        desktop_dir
        / claude.DEFAULT_DESKTOP_ORG_ID
        / claude.DEFAULT_DESKTOP_PROJECT_ID
        / f"local_{target_sid}.json"
    )
    assert pointer.is_file()
    data = json.loads(pointer.read_text(encoding="utf-8"))
    assert data["cliSessionId"] == target_sid


def test_cli_import_to_claude_no_desktop_option_leaves_global_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_global = tmp_path / "global_home"
    global_desktop = fake_global / ".config" / "Claude" / "claude-code-sessions"
    global_desktop.mkdir(parents=True)

    monkeypatch.setenv("HOME", str(fake_global))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_global / ".config"))
    monkeypatch.delenv("CLAUDE_DESKTOP_CONFIG_DIR", raising=False)

    target_home = tmp_path / "isolated_claude_home"
    target_sid = "99999999-9999-4999-8999-999999999999"

    code = main(
        [
            "import",
            str(SOURCE_CLAUDE_FILE),
            "--to",
            "claude",
            "--home",
            str(target_home),
            "--session-id",
            target_sid,
        ]
    )
    assert code == 0

    # Imported transcript exists under target_home
    manifest = target_home / "session-migrate" / "manifests" / f"{target_sid}.json"
    assert manifest.is_file()

    # Crucial: NO pointer was created in the global Desktop tree
    assert list(global_desktop.rglob("*.json")) == []


def test_read_desktop_pointer_valid_and_bounds(tmp_path: Path) -> None:
    # Valid pointer under 64 KiB
    valid = tmp_path / "valid.json"
    valid.write_text(json.dumps({"cwd": str(tmp_path), "originCwd": str(tmp_path)}))
    parsed = claude.read_desktop_pointer(valid)
    assert parsed == {"cwd": str(tmp_path), "originCwd": str(tmp_path)}

    # Oversized pointer (> 64 KiB)
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b'{"cwd": "' + b"a" * (65 * 1024) + b'"}')
    assert claude.read_desktop_pointer(oversized) is None

    # Directory is not a regular file
    sub_dir = tmp_path / "subdir.json"
    sub_dir.mkdir()
    assert claude.read_desktop_pointer(sub_dir) is None

    # Symlink is rejected
    symlink_p = tmp_path / "symlinked.json"
    symlink_p.symlink_to(valid)
    assert claude.read_desktop_pointer(symlink_p) is None

    # Malformed JSON
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{not-valid-json")
    assert claude.read_desktop_pointer(malformed) is None

    # Non-dict JSON
    non_dict = tmp_path / "list.json"
    non_dict.write_text("[1, 2, 3]")
    assert claude.read_desktop_pointer(non_dict) is None


def test_find_desktop_project_dir_skips_fifo_and_oversized(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "claude-code-sessions"
    org = sessions_dir / "test-org"
    proj = org / "test-proj"
    proj.mkdir(parents=True)

    session_cwd = tmp_path / "target_workspace"
    session_cwd.mkdir()

    # Oversized file matching cwd must be skipped
    oversized = proj / "local_oversized.json"
    oversized.write_bytes(
        b'{"cwd": "' + str(session_cwd).encode() + b'", "pad": "' + b"x" * (65 * 1024) + b'"}'
    )

    # Symlink matching cwd must be skipped
    real = tmp_path / "external_pointer.json"
    real.write_text(json.dumps({"cwd": str(session_cwd)}))
    symlinked = proj / "local_symlinked.json"
    symlinked.symlink_to(real)

    # FIFO matching cwd (if platform supports mkfifo) must not block and must be skipped
    if hasattr(os, "mkfifo"):
        fifo = proj / "local_blocking.json"
        os.mkfifo(fifo)

    # Because all candidates in test-proj are skipped (oversized, symlinked, fifo),
    # find_desktop_project_dir must NOT match test-proj and must fall back to default
    found = claude.find_desktop_project_dir(sessions_dir, session_cwd)
    assert found == sessions_dir / claude.DEFAULT_DESKTOP_ORG_ID / claude.DEFAULT_DESKTOP_PROJECT_ID

    # Now add a valid bounded pointer in test-proj
    valid_ptr = proj / "local_valid.json"
    valid_ptr.write_text(json.dumps({"cwd": str(session_cwd)}))
    found_valid = claude.find_desktop_project_dir(sessions_dir, session_cwd)
    assert found_valid == proj


def test_install_claude_artifact_rejects_symlinked_org(tmp_path: Path) -> None:
    session = _sample_session(tmp_path)
    options = ConversionOptions(target_format=TargetFormat.CLAUDE, cwd=tmp_path / "my_project")
    artifact = convert_session(session, options)

    target_home = tmp_path / "target_home"
    desktop_dir = tmp_path / "desktop" / "claude-code-sessions"
    desktop_dir.mkdir(parents=True)

    outside = tmp_path / "escaped_org"
    outside.mkdir()

    # Create default org as a symlink pointing outside desktop_dir
    org_link = desktop_dir / claude.DEFAULT_DESKTOP_ORG_ID
    org_link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(SessionMigrateError, match="symlink|escapes"):
        install_claude_artifact(
            artifact,
            target_home=target_home,
            desktop_dir=desktop_dir,
        )

    # Verify dry_run also rejects
    with pytest.raises(SessionMigrateError, match="symlink|escapes"):
        install_claude_artifact(
            artifact,
            target_home=target_home,
            desktop_dir=desktop_dir,
            dry_run=True,
        )

    # Verify write_claude_artifact also rejects
    output_path = target_home / "session.jsonl"
    manifest_path = target_home / "manifest.json"
    with pytest.raises(SessionMigrateError, match="symlink|escapes"):
        write_claude_artifact(
            artifact,
            output_path=output_path,
            manifest_path=manifest_path,
            desktop_dir=desktop_dir,
        )

    # Crucial check: No file created inside escaped directory
    assert list(outside.rglob("*.json")) == []


def test_install_claude_artifact_rejects_symlinked_proj(tmp_path: Path) -> None:
    session = _sample_session(tmp_path)
    options = ConversionOptions(target_format=TargetFormat.CLAUDE, cwd=tmp_path / "my_project")
    artifact = convert_session(session, options)

    target_home = tmp_path / "target_home"
    desktop_dir = tmp_path / "desktop" / "claude-code-sessions"
    org_dir = desktop_dir / claude.DEFAULT_DESKTOP_ORG_ID
    org_dir.mkdir(parents=True)

    outside = tmp_path / "escaped_proj"
    outside.mkdir()

    # Create default project as a symlink pointing outside desktop_dir
    proj_link = org_dir / claude.DEFAULT_DESKTOP_PROJECT_ID
    proj_link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(SessionMigrateError, match="symlink|escapes"):
        install_claude_artifact(
            artifact,
            target_home=target_home,
            desktop_dir=desktop_dir,
        )

    assert list(outside.rglob("*.json")) == []


def test_claude_desktop_consumer_acceptance_oracle(tmp_path: Path) -> None:
    """Validate full end-to-end Claude Desktop consumer acceptance contract.

    Acts as an independent Claude Desktop consumer oracle verifying that:
    1. The companion pointer generated by `install_claude_artifact` conforms to the
       exact JSON schema required by Claude Desktop frontend stores.
    2. Every mandatory consumer key is present, non-null, and of the expected type.
    3. `cliSessionId` maps directly to an active, valid native Claude Code JSONL
       session in the companion CLI store (`target_home / .claude/projects/`).
    4. The native session parses back losslessly via `claude.parse_session`.
    """
    session = _sample_session(tmp_path)
    options = ConversionOptions(
        target_format=TargetFormat.CLAUDE,
        session_id=TEST_SESSION_ID,
        cwd=session.cwd,
    )
    artifact = convert_session(session, options)

    target_home = tmp_path / "claude_home"
    desktop_dir = tmp_path / "claude_desktop" / "claude-code-sessions"
    desktop_dir.mkdir(parents=True)

    native_path, manifest_path, pointer_path = install_claude_artifact(
        artifact,
        target_home=target_home,
        desktop_dir=desktop_dir,
    )

    assert native_path.is_file()
    assert pointer_path is not None and pointer_path.is_file()

    # --- Simulated Claude Desktop Consumer Oracle ---
    # 1. Discover all local_*.json pointers in desktop_dir
    pointers = list(desktop_dir.rglob("local_*.json"))
    assert len(pointers) == 1
    pointer_file = pointers[0]

    # 2. Parse pointer using bounded, safe reader
    payload = claude.read_desktop_pointer(pointer_file)
    assert isinstance(payload, dict)

    # 3. Validate Claude Desktop mandatory schema invariants
    required_string_fields = {
        "sessionId": f"local_{TEST_SESSION_ID}",
        "cliSessionId": TEST_SESSION_ID,
        "cwd": str(session.cwd.resolve()),
        "originCwd": str(session.cwd.resolve()),
        "model": "claude-3-5-sonnet-20241022",
        "title": "Test Desktop Session",
        "titleSource": "custom",
        "permissionMode": "default",
        "importedFrom": "local-1p-code",
    }
    for key, expected_val in required_string_fields.items():
        assert key in payload, f"Missing required Desktop consumer field: {key}"
        assert payload[key] == expected_val, (
            f"Field {key} mismatch: {payload[key]!r} != {expected_val!r}"
        )

    required_int_fields = ["createdAt", "lastActivityAt", "lastFocusedAt"]
    for key in required_int_fields:
        assert key in payload and isinstance(payload[key], int), f"Field {key} must be int"
        assert payload[key] > 0, f"Field {key} must be positive timestamp"

    assert payload["lastActivityAt"] >= payload["createdAt"]
    assert payload["lastFocusedAt"] == payload["lastActivityAt"]
    assert payload["isArchived"] is False
    assert isinstance(payload["alwaysAllowedReasons"], list)
    assert isinstance(payload["sessionPermissionUpdates"], list)

    # 4. Consumer linkage: verify pointer resolves to valid native Claude Code CLI session
    companion_cli_path = native_path
    assert companion_cli_path.is_file()
    assert companion_cli_path.stem == TEST_SESSION_ID

    # 5. Parse companion session through native Claude Code parser
    cli_session = claude.parse(companion_cli_path)
    assert cli_session.session_id == TEST_SESSION_ID
    assert cli_session.cwd == session.cwd.resolve()
    assert len(cli_session.events) == len(session.events)
    assert cli_session.title == payload["title"]
