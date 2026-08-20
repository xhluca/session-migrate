import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from session_migrate import cli as cli_module
from session_migrate import conversion
from session_migrate.cli import build_parser, main
from session_migrate.conversion import (
    ConversionOptions,
    convert_session,
    default_migration_state_home,
    default_target_home,
    install_antigravity_artifact,
    install_copilot_artifact,
    install_cursor_artifact,
    install_opencode_artifact,
    opencode_manifest_path,
    target_import_paths,
)
from session_migrate.errors import SessionMigrateError
from session_migrate.formats import antigravity, claude, codex, copilot, cursor, opencode, pi
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
TARGET_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
TARGET_OPENCODE_ID = "ses_aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa"


def source_session() -> Session:
    return claude.parse(FIXTURES / "claude-2.1.209" / "basic.jsonl")


def opencode_artifact(
    tmp_path: Path, *, version: str | None = None
) -> conversion.ConversionArtifact:
    return convert_session(
        source_session(),
        ConversionOptions(
            target_format=TargetFormat.OPENCODE,
            session_id=TARGET_UUID,
            cwd=tmp_path,
            target_cli_version=version,
        ),
    )


def patch_opencode_preflight(
    monkeypatch: pytest.MonkeyPatch,
    session_id_results: list[set[str]],
) -> tuple[Path, list[dict[str, str]]]:
    cli = Path("/synthetic/opencode")
    environments: list[dict[str, str]] = []
    results = iter(session_id_results)
    monkeypatch.setattr(conversion, "_resolve_opencode_cli", lambda path, env: cli)
    monkeypatch.setattr(
        conversion,
        "_opencode_version",
        lambda path, env: opencode.PINNED_OPENCODE_VERSION,
    )

    def session_ids(path: Path, env: dict[str, str]) -> set[str]:
        assert path == cli
        environments.append(dict(env))
        return next(results)

    monkeypatch.setattr(conversion, "_opencode_session_ids", session_ids)
    return cli, environments


def test_load_opencode_session_uses_official_export_and_virtual_source_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = FIXTURES / "opencode-source-1.17.20" / "comprehensive.json"
    source_id = "ses_33333333333343338333333333333333"
    cli = tmp_path / "opencode"
    monkeypatch.setattr(conversion, "_resolve_opencode_cli", lambda path, env: cli)
    monkeypatch.setattr(
        conversion,
        "_opencode_version",
        lambda path, env: opencode.PINNED_OPENCODE_VERSION,
    )

    def export(_cli: Path, session_id: str, output: Path, env: dict[str, str]) -> None:
        assert session_id == source_id
        output.write_bytes(fixture.read_bytes())

    monkeypatch.setattr(conversion, "_invoke_opencode_export", export)

    source = conversion.load_opencode_session(source_id, source_cli=cli, environ={})

    assert source.source_format == AgentFormat.OPENCODE
    assert source.session_id == source_id
    assert str(source.source_path) == f"opencode:{source_id}"
    assert source.source_sha256


def test_source_and_target_enums_are_deliberately_separate() -> None:
    assert tuple(AgentFormat) == (
        AgentFormat.CLAUDE,
        AgentFormat.CODEX,
        AgentFormat.PI,
        AgentFormat.OPENCODE,
        AgentFormat.COPILOT,
        AgentFormat.ANTIGRAVITY,
        AgentFormat.CURSOR,
    )
    assert set(TargetFormat) == {
        TargetFormat.CLAUDE,
        TargetFormat.CODEX,
        TargetFormat.PI,
        TargetFormat.OPENCODE,
        TargetFormat.COPILOT,
        TargetFormat.ANTIGRAVITY,
        TargetFormat.CURSOR,
    }


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (claude.parse(FIXTURES / "claude-2.1.209" / "basic.jsonl"), TargetFormat.CLAUDE),
        (codex.parse(FIXTURES / "codex-0.144.4" / "basic.jsonl"), TargetFormat.CODEX),
        (pi.parse_session(FIXTURES / "pi-0.80.6" / "basic.jsonl"), TargetFormat.PI),
    ],
)
def test_same_format_conversion_creates_a_new_portable_session(
    source: Session, target: TargetFormat, tmp_path: Path
) -> None:
    artifact = convert_session(
        source,
        ConversionOptions(target_format=target, session_id=TARGET_UUID, cwd=tmp_path),
    )

    assert artifact.session_id == TARGET_UUID
    assert artifact.native_bytes
    assert artifact.source.session_id != artifact.session_id
    assert any(warning["code"] == "same_format_portable_rewrite" for warning in artifact.warnings)


def test_cli_parser_accepts_every_target_and_expands_target_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    parser = build_parser()

    for target in TargetFormat:
        args = parser.parse_args(
            [
                "convert",
                "source.jsonl",
                "--to",
                target.value,
                "--output",
                "output",
            ]
        )
        assert args.to == target.value
    imported = parser.parse_args(
        [
            "import",
            "source.jsonl",
            "--to",
            "opencode",
            "--target-cli",
            "~/.opencode/bin/opencode",
        ]
    )
    assert imported.target_cli == tmp_path / ".opencode/bin/opencode"


@pytest.mark.parametrize(
    "target",
    [
        TargetFormat.PI,
        TargetFormat.OPENCODE,
        TargetFormat.COPILOT,
        TargetFormat.ANTIGRAVITY,
        TargetFormat.CURSOR,
    ],
)
def test_shared_conversion_dispatches_additional_targets(
    tmp_path: Path, target: TargetFormat
) -> None:
    artifact = convert_session(
        source_session(),
        ConversionOptions(
            target_format=target,
            session_id=TARGET_UUID,
            cwd=tmp_path,
        ),
    )
    path = tmp_path / (
        "target.json"
        if target == TargetFormat.OPENCODE
        else f"{TARGET_UUID}.db"
        if target in {TargetFormat.ANTIGRAVITY, TargetFormat.CURSOR}
        else "target.jsonl"
    )
    path.write_bytes(artifact.native_bytes)

    assert artifact.target_format == target
    if target == TargetFormat.PI:
        pi.validate_native_bytes(artifact.native_bytes, TARGET_UUID)
        assert pi.parse(path).session_id == TARGET_UUID
    elif target == TargetFormat.OPENCODE:
        opencode.validate_native_bytes(artifact.native_bytes, TARGET_OPENCODE_ID)
        assert opencode.parse(path).session_id == TARGET_OPENCODE_ID
    elif target == TargetFormat.COPILOT:
        copilot.validate_native_bytes(artifact.native_bytes, TARGET_UUID)
        assert copilot.parse(path).session_id == TARGET_UUID
    elif target == TargetFormat.ANTIGRAVITY:
        antigravity.validate_native_bytes(artifact.native_bytes, TARGET_UUID)
        assert antigravity.parse(path).session_id == TARGET_UUID
    else:
        cursor.validate_native_bytes(artifact.native_bytes, TARGET_UUID)
        assert cursor.parse(path).session_id == TARGET_UUID


@pytest.mark.parametrize(
    "target",
    [TargetFormat.CLAUDE, TargetFormat.CODEX, TargetFormat.OPENCODE, TargetFormat.COPILOT],
)
def test_pi_source_dispatches_to_every_other_supported_target(
    tmp_path: Path, target: TargetFormat
) -> None:
    source = pi.parse_session(FIXTURES / "pi-0.80.6" / "basic.jsonl")
    artifact = convert_session(
        source,
        ConversionOptions(target_format=target, session_id=TARGET_UUID, cwd=tmp_path),
    )
    path = tmp_path / ("target.json" if target == TargetFormat.OPENCODE else "target.jsonl")
    path.write_bytes(artifact.native_bytes)

    if target == TargetFormat.CLAUDE:
        parsed = claude.parse(path)
    elif target == TargetFormat.CODEX:
        parsed = codex.parse(path)
    elif target == TargetFormat.OPENCODE:
        parsed = opencode.parse(path)
    else:
        parsed = copilot.parse(path)
    assert parsed.session_id == (
        TARGET_OPENCODE_ID if target == TargetFormat.OPENCODE else TARGET_UUID
    )


@pytest.mark.parametrize("target", [TargetFormat.PI, TargetFormat.OPENCODE, TargetFormat.COPILOT])
def test_convert_cli_writes_additional_native_target_and_manifest(
    tmp_path: Path, target: TargetFormat, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / ("converted.json" if target == TargetFormat.OPENCODE else "converted.jsonl")

    status = main(
        [
            "convert",
            str(FIXTURES / "claude-2.1.209" / "basic.jsonl"),
            "--to",
            target.value,
            "--output",
            str(output),
            "--session-id",
            TARGET_UUID,
            "--cwd",
            str(tmp_path),
        ]
    )

    assert status == 0
    result = json.loads(capsys.readouterr().out)
    assert result["target_format"] == target.value
    assert Path(result["output"]) == output
    assert output.with_name(f"{output.name}.session-migrate.json").is_file()
    parsed = (
        pi.parse(output)
        if target == TargetFormat.PI
        else opencode.parse(output)
        if target == TargetFormat.OPENCODE
        else copilot.parse(output)
    )
    assert parsed.session_id == (
        TARGET_OPENCODE_ID if target == TargetFormat.OPENCODE else TARGET_UUID
    )


def test_cursor_target_is_a_pinned_text_only_native_store(tmp_path: Path) -> None:
    artifact = convert_session(
        source_session(),
        ConversionOptions(
            target_format=TargetFormat.CURSOR,
            session_id=TARGET_UUID,
            cwd=tmp_path,
        ),
    )

    assert artifact.target_cli_version == cursor.PINNED_CURSOR_VERSION
    assert artifact.dropped
    cursor.validate_native_bytes(artifact.native_bytes, TARGET_UUID)


def test_antigravity_artifact_installs_database_summary_and_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = convert_session(
        source_session(),
        ConversionOptions(
            target_format=TargetFormat.ANTIGRAVITY,
            session_id=TARGET_UUID,
            cwd=tmp_path,
        ),
    )
    home = tmp_path / "antigravity-cli"
    monkeypatch.setattr(antigravity, "verify_pinned_cli", lambda *args, **kwargs: Path("agy"))

    native_path, manifest_path = install_antigravity_artifact(
        artifact,
        target_home=home,
        dry_run=True,
    )
    assert not home.exists()

    installed_native, installed_manifest = install_antigravity_artifact(
        artifact,
        target_home=home,
    )
    assert installed_native == native_path
    assert installed_manifest == manifest_path
    assert antigravity.parse(installed_native).session_id == TARGET_UUID
    assert (home / "conversation_summaries.db").is_file()
    manifest = json.loads(installed_manifest.read_text())
    assert manifest["target"]["format"] == "antigravity"
    assert manifest["target"]["path"] == str(installed_native)

    with pytest.raises(SessionMigrateError, match="already exists|overwrite"):
        install_antigravity_artifact(artifact, target_home=home, dry_run=True)


def test_antigravity_version_label_cannot_bypass_pinned_native_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = convert_session(
        source_session(),
        ConversionOptions(
            target_format=TargetFormat.ANTIGRAVITY,
            session_id=TARGET_UUID,
            cwd=tmp_path,
            target_cli_version="9.9.9",
        ),
    )
    consulted = False

    def verify(*_args: object, **_kwargs: object) -> Path:
        nonlocal consulted
        consulted = True
        return Path("agy")

    monkeypatch.setattr(antigravity, "verify_pinned_cli", verify)
    with pytest.raises(SessionMigrateError, match="requires target metadata version 1.1.16"):
        install_antigravity_artifact(
            artifact,
            target_home=tmp_path / "antigravity-cli",
            dry_run=True,
        )
    assert not consulted


def test_cursor_cli_convert_writes_experimental_native_store(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "cursor.db"

    status = main(
        [
            "convert",
            str(FIXTURES / "claude-2.1.209" / "basic.jsonl"),
            "--to",
            "cursor",
            "--output",
            str(output),
            "--session-id",
            TARGET_UUID,
            "--cwd",
            str(tmp_path),
        ]
    )

    assert status == 0
    result = json.loads(capsys.readouterr().out)
    assert result["target_format"] == "cursor"
    assert cursor.parse(output).session_id == TARGET_UUID
    assert output.with_name("cursor.db.session-migrate.json").is_file()


def test_cursor_artifact_installs_database_and_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = convert_session(
        source_session(),
        ConversionOptions(
            target_format=TargetFormat.CURSOR,
            session_id=TARGET_UUID,
            cwd=tmp_path,
        ),
    )
    home = tmp_path / "cursor-home"
    monkeypatch.setattr(cursor, "verify_pinned_cli", lambda *args, **kwargs: Path("cursor"))

    native_path, manifest_path = install_cursor_artifact(
        artifact, target_home=home, dry_run=True
    )
    assert not home.exists()

    installed_native, installed_manifest = install_cursor_artifact(
        artifact, target_home=home
    )
    assert (installed_native, installed_manifest) == (native_path, manifest_path)
    assert cursor.parse(installed_native).session_id == TARGET_UUID
    manifest = json.loads(installed_manifest.read_text())
    assert manifest["target"]["format"] == "cursor"
    assert manifest["target"]["path"] == str(installed_native)

    with pytest.raises(SessionMigrateError, match="already exists|overwrite"):
        install_cursor_artifact(artifact, target_home=home, dry_run=True)


def test_pi_default_home_and_native_import_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = tmp_path / "pi-home"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(configured))
    artifact = convert_session(
        source_session(),
        ConversionOptions(
            target_format=TargetFormat.PI,
            session_id=TARGET_UUID,
            cwd=tmp_path,
        ),
    )

    native, manifest = target_import_paths(artifact, default_target_home(TargetFormat.PI))

    assert native == configured / pi.session_relative_path(
        tmp_path, TARGET_UUID, artifact.timestamp
    )
    assert manifest == configured / "session-migrate/manifests" / f"{TARGET_UUID}.json"


def test_pi_cli_import_installs_at_native_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target_home = tmp_path / "pi-home"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(target_home))

    status = main(
        [
            "import",
            str(FIXTURES / "claude-2.1.209" / "basic.jsonl"),
            "--to",
            "pi",
            "--session-id",
            TARGET_UUID,
            "--cwd",
            str(tmp_path),
        ]
    )

    assert status == 0
    result = json.loads(capsys.readouterr().out)
    native_path = Path(result["output"])
    assert native_path == target_home / pi.session_relative_path(
        tmp_path, TARGET_UUID, "2026-08-17T12:00:00Z"
    )
    assert native_path.stat().st_mode & 0o777 == 0o600
    assert Path(result["manifest"]).stat().st_mode & 0o777 == 0o600
    assert pi.parse(native_path).session_id == TARGET_UUID


def test_copilot_default_home_and_atomic_native_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target_home = tmp_path / "copilot-home"
    monkeypatch.setenv("COPILOT_HOME", str(target_home))

    status = main(
        [
            "import",
            str(FIXTURES / "claude-2.1.209" / "basic.jsonl"),
            "--to",
            "copilot",
            "--session-id",
            TARGET_UUID,
            "--cwd",
            str(tmp_path),
        ]
    )

    assert status == 0
    result = json.loads(capsys.readouterr().out)
    events = target_home / copilot.session_relative_path(TARGET_UUID)
    workspace = events.parent / "workspace.yaml"
    manifest = target_home / "session-migrate/manifests" / f"{TARGET_UUID}.json"
    assert Path(result["output"]) == events
    assert Path(result["manifest"]) == manifest
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in (events, workspace, manifest))
    assert events.parent.stat().st_mode & 0o777 == 0o700
    assert copilot.parse(events).session_id == TARGET_UUID


def test_copilot_dry_run_and_collision_cover_entire_session_directory(
    tmp_path: Path,
) -> None:
    artifact = convert_session(
        source_session(),
        ConversionOptions(
            target_format=TargetFormat.COPILOT,
            session_id=TARGET_UUID,
            cwd=tmp_path,
        ),
    )
    target_home = tmp_path / "copilot-home"

    events, manifest = install_copilot_artifact(artifact, target_home=target_home, dry_run=True)
    assert not events.exists()
    assert not manifest.exists()
    session_directory = events.parent
    session_directory.mkdir(parents=True)

    with pytest.raises(SessionMigrateError, match="refusing to overwrite"):
        install_copilot_artifact(artifact, target_home=target_home, dry_run=True)


def test_transfer_can_explicitly_target_pi(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_home = tmp_path / "claude-home"
    source_id = "10000000-0000-4000-8000-000000000000"
    source_path = source_home / "projects" / "-work" / f"{source_id}.jsonl"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes((FIXTURES / "claude-2.1.209" / "basic.jsonl").read_bytes())

    status = main(
        [
            "transfer",
            source_id,
            "--from",
            "claude",
            "--to",
            "pi",
            "--source-home",
            str(source_home),
            "--source-cwd",
            "/work",
            "--home",
            str(tmp_path / "pi-home"),
            "--cwd",
            str(tmp_path),
            "--session-id",
            TARGET_UUID,
            "--dry-run",
        ]
    )

    assert status == 0
    result = json.loads(capsys.readouterr().out)
    assert result["source_format"] == "claude"
    assert result["target_format"] == "pi"
    assert result["dry_run"] is True
    assert not (tmp_path / "pi-home").exists()


def test_migration_state_home_uses_xdg_without_creating_it(tmp_path: Path) -> None:
    state = tmp_path / "state"
    result = default_migration_state_home(environ={"XDG_STATE_HOME": str(state)})

    assert result == state / "session-migrate"
    assert not state.exists()


def test_opencode_install_reserves_manifest_and_uses_private_temporary_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = opencode_artifact(tmp_path)
    assert artifact.session_id == TARGET_OPENCODE_ID
    cli, environments = patch_opencode_preflight(monkeypatch, [set(), set(), {TARGET_OPENCODE_ID}])
    temporary_root = tmp_path / "temporary"
    temporary_root.mkdir()
    manifest_path = opencode_manifest_path(artifact, state_home=tmp_path / "migrator-state")
    observed: dict[str, object] = {}

    def invoke(path: Path, bundle_path: Path, env: dict[str, str]) -> None:
        assert path == cli
        observed["bytes"] = bundle_path.read_bytes()
        observed["file_mode"] = bundle_path.stat().st_mode & 0o777
        observed["directory_mode"] = bundle_path.parent.stat().st_mode & 0o777
        observed["bundle_path"] = bundle_path
        assert manifest_path.exists()
        assert manifest_path.stat().st_size == 0
        assert env["OPENCODE_DISABLE_AUTOUPDATE"] == "true"
        assert env["OPENCODE_DISABLE_PRUNE"] == "true"

    monkeypatch.setattr(conversion, "_invoke_opencode_import", invoke)

    installed_cli = install_opencode_artifact(
        artifact,
        manifest_path=manifest_path,
        environ={"TMPDIR": str(temporary_root)},
    )

    assert installed_cli == cli
    assert observed["bytes"] == artifact.native_bytes
    assert observed["file_mode"] == 0o600
    assert observed["directory_mode"] == 0o700
    bundle_path = observed["bundle_path"]
    assert isinstance(bundle_path, Path)
    assert not bundle_path.exists()
    assert list(temporary_root.iterdir()) == []
    assert manifest_path.stat().st_mode & 0o777 == 0o600
    manifest = json.loads(manifest_path.read_text())
    assert manifest["target"]["path"] == f"opencode:{TARGET_OPENCODE_ID}"
    assert manifest["target"]["session_id"] == TARGET_OPENCODE_ID
    assert all(env["OPENCODE_DISABLE_AUTOUPDATE"] == "true" for env in environments)


def test_opencode_dry_run_checks_collision_without_temp_or_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = opencode_artifact(tmp_path)
    patch_opencode_preflight(monkeypatch, [set()])
    invoked = False

    def invoke(path: Path, bundle_path: Path, env: dict[str, str]) -> None:
        nonlocal invoked
        invoked = True

    monkeypatch.setattr(conversion, "_invoke_opencode_import", invoke)
    manifest_path = tmp_path / "state/manifest.json"

    install_opencode_artifact(
        artifact,
        manifest_path=manifest_path,
        dry_run=True,
        environ={},
    )

    assert invoked is False
    assert not manifest_path.exists()
    assert not manifest_path.parent.exists()


def test_opencode_empty_official_session_list_means_no_collisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    monkeypatch.setattr(conversion, "_run_opencode", lambda command, env: completed)

    assert conversion._opencode_session_ids(Path("/synthetic/opencode"), {}) == set()


def test_opencode_native_collision_fails_before_manifest_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = opencode_artifact(tmp_path)
    patch_opencode_preflight(monkeypatch, [{TARGET_OPENCODE_ID}])
    manifest_path = tmp_path / "state/manifest.json"

    with pytest.raises(SessionMigrateError, match="refusing to overwrite native session"):
        install_opencode_artifact(
            artifact,
            manifest_path=manifest_path,
            environ={},
        )

    assert not manifest_path.exists()


def test_opencode_unvalidated_metadata_version_cannot_bypass_pinned_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = opencode_artifact(tmp_path, version="9.9.9")
    resolved = False

    def resolve(path: Path | None, env: dict[str, str]) -> Path:
        nonlocal resolved
        resolved = True
        return Path("/synthetic/opencode")

    monkeypatch.setattr(conversion, "_resolve_opencode_cli", resolve)

    with pytest.raises(SessionMigrateError, match="requires target metadata version 1.17.20"):
        install_opencode_artifact(
            artifact,
            manifest_path=tmp_path / "manifest.json",
            environ={},
        )

    assert resolved is False


def test_opencode_cli_rejects_home_before_native_import(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = main(
        [
            "import",
            str(FIXTURES / "claude-2.1.209" / "basic.jsonl"),
            "--to",
            "opencode",
            "--home",
            str(tmp_path / "opencode-home"),
            "--cwd",
            str(tmp_path),
        ]
    )

    assert status == 2
    assert "control OpenCode's normal HOME/XDG environment" in capsys.readouterr().err


def test_opencode_cli_target_version_override_cannot_bypass_pinned_import(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = main(
        [
            "import",
            str(FIXTURES / "claude-2.1.209" / "basic.jsonl"),
            "--to",
            "opencode",
            "--target-cli-version",
            "9.9.9",
            "--target-cli",
            str(tmp_path / "not-consulted"),
            "--cwd",
            str(tmp_path),
        ]
    )

    assert status == 2
    error = capsys.readouterr().err
    assert "requires target metadata version 1.17.20" in error
    assert "was not found" not in error


def test_opencode_cli_dry_run_delegates_to_official_preflight_without_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = tmp_path / "state"
    target_cli = tmp_path / "bin/opencode"
    observed: dict[str, object] = {}

    def install(
        artifact: conversion.ConversionArtifact,
        *,
        manifest_path: Path,
        target_cli: Path | None,
        dry_run: bool,
    ) -> Path:
        observed.update(
            artifact=artifact,
            manifest_path=manifest_path,
            target_cli=target_cli,
            dry_run=dry_run,
        )
        return target_cli or Path("opencode")

    monkeypatch.setattr(cli_module, "install_opencode_artifact", install)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))

    status = main(
        [
            "import",
            str(FIXTURES / "claude-2.1.209" / "basic.jsonl"),
            "--to",
            "opencode",
            "--target-cli",
            str(target_cli),
            "--session-id",
            TARGET_UUID,
            "--cwd",
            str(tmp_path),
            "--dry-run",
        ]
    )

    assert status == 0
    result = json.loads(capsys.readouterr().out)
    assert result["output"] == f"opencode:{TARGET_OPENCODE_ID}"
    assert observed["target_cli"] == target_cli
    assert observed["dry_run"] is True
    assert observed["manifest_path"] == (
        state / "session-migrate/manifests/opencode" / f"{TARGET_OPENCODE_ID}.json"
    )
    assert not state.exists()


def test_opencode_import_failure_removes_reservation_and_temporary_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = opencode_artifact(tmp_path)
    patch_opencode_preflight(monkeypatch, [set(), set()])
    temporary_root = tmp_path / "temporary"
    temporary_root.mkdir()
    manifest_path = tmp_path / "state/manifest.json"

    def fail(path: Path, bundle_path: Path, env: dict[str, str]) -> None:
        assert bundle_path.exists()
        raise SessionMigrateError("synthetic importer failure")

    monkeypatch.setattr(conversion, "_invoke_opencode_import", fail)

    with pytest.raises(SessionMigrateError, match="synthetic importer failure"):
        install_opencode_artifact(
            artifact,
            manifest_path=manifest_path,
            environ={"TMPDIR": str(temporary_root)},
        )

    assert not manifest_path.exists()
    assert list(temporary_root.iterdir()) == []


def test_opencode_temp_cleanup_failure_after_import_warns_native_may_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = opencode_artifact(tmp_path)
    patch_opencode_preflight(monkeypatch, [set(), set()])
    temporary_directory = tmp_path / "synthetic-temporary-directory"
    manifest_path = tmp_path / "state/manifest.json"
    invoked = False

    class FailingCleanup:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def __enter__(self) -> str:
            temporary_directory.mkdir()
            return str(temporary_directory)

        def __exit__(self, *args: object) -> None:
            del args
            raise OSError("synthetic cleanup failure")

    def invoke(path: Path, bundle_path: Path, env: dict[str, str]) -> None:
        nonlocal invoked
        invoked = True

    monkeypatch.setattr(conversion.tempfile, "TemporaryDirectory", FailingCleanup)
    monkeypatch.setattr(conversion, "_invoke_opencode_import", invoke)

    with pytest.raises(SessionMigrateError, match="native session may already exist"):
        install_opencode_artifact(
            artifact,
            manifest_path=manifest_path,
            environ={},
        )

    assert invoked is True
    assert not manifest_path.exists()


def test_codex_default_provider_remains_openai(tmp_path: Path) -> None:
    artifact = convert_session(
        source_session(),
        ConversionOptions(
            target_format=TargetFormat.CODEX,
            session_id=TARGET_UUID,
            cwd=tmp_path,
        ),
    )
    first = json.loads(artifact.native_bytes.splitlines()[0])

    assert first["payload"]["model_provider"] == "openai"


@pytest.mark.parametrize("target", [TargetFormat.PI, TargetFormat.OPENCODE])
def test_conversion_rejects_thinking_and_opaque_only_history(
    tmp_path: Path, target: TargetFormat
) -> None:
    source = replace(
        source_session(),
        title="metadata does not make history resumable",
        events=(
            Event(
                kind=EventKind.THINKING,
                role=Role.ASSISTANT,
                text="SYNTHETIC_PRIVATE_REASONING",
                provenance=Provenance(0, "assistant"),
            ),
            Event(
                kind=EventKind.OPAQUE,
                payload={"reason": "synthetic_metadata"},
                provenance=Provenance(1, "system"),
            ),
        ),
        raw_record_count=2,
    )

    with pytest.raises(SessionMigrateError, match="no resumable conversation context"):
        convert_session(
            source,
            ConversionOptions(
                target_format=target,
                session_id=TARGET_UUID,
                cwd=tmp_path,
            ),
        )


def test_pi_validator_rejects_header_and_session_info_only() -> None:
    data = (
        '{"type":"session","version":3,"id":"'
        + TARGET_UUID
        + '","timestamp":"2026-08-18T12:00:00Z","cwd":"/synthetic"}\n'
        '{"type":"session_info","id":"info","parentId":null,'
        '"timestamp":"2026-08-18T12:00:00Z","name":"metadata only"}\n'
    ).encode()

    with pytest.raises(SessionMigrateError, match="no resumable conversation context"):
        pi.validate_native_bytes(data, TARGET_UUID)


def test_opencode_validator_rejects_metadata_only_bundle() -> None:
    data = json.dumps(
        {
            "info": {
                "id": TARGET_OPENCODE_ID,
                "directory": "/synthetic",
                "title": "metadata only",
                "time": {"created": 1, "updated": 1},
            },
            "messages": [],
        }
    ).encode()

    with pytest.raises(SessionMigrateError, match="no resumable conversation context"):
        opencode.validate_native_bytes(data, TARGET_OPENCODE_ID)
