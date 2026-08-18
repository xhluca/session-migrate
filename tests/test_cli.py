import json
from pathlib import Path

from session_bridge import __version__
from session_bridge.cli import build_parser, main
from session_bridge.formats.claude import project_directory_name


def test_parser_exposes_version() -> None:
    assert __version__ == "0.1.0.dev0"
    assert build_parser().prog == "session-bridge"


def test_convert_cli_writes_native_and_manifest(tmp_path: Path) -> None:
    fixture = Path(__file__).parent / "fixtures" / "claude-2.1.209" / "basic.jsonl"
    output = tmp_path / "rollout.jsonl"

    status = main(
        [
            "convert",
            str(fixture),
            "--to",
            "codex",
            "--output",
            str(output),
            "--session-id",
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "--cwd",
            str(tmp_path),
        ]
    )

    assert status == 0
    assert output.exists()
    assert output.with_name(f"{output.name}.session-bridge.json").exists()


def test_import_dry_run_does_not_write(tmp_path: Path, capsys: object) -> None:
    fixture = Path(__file__).parent / "fixtures" / "codex-0.144.4" / "basic.jsonl"
    target_home = tmp_path / "claude-home"

    status = main(
        [
            "import",
            str(fixture),
            "--to",
            "claude",
            "--home",
            str(target_home),
            "--dry-run",
            "--session-id",
            "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "--cwd",
            str(tmp_path),
        ]
    )

    assert status == 0
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    result = json.loads(captured.out)
    assert result["dry_run"] is True
    assert not target_home.exists()


def test_import_dry_run_rejects_existing_target(tmp_path: Path, capsys: object) -> None:
    fixture = Path(__file__).parent / "fixtures" / "codex-0.144.4" / "basic.jsonl"
    target_home = tmp_path / "claude-home"
    target = (
        target_home
        / "projects"
        / project_directory_name(tmp_path)
        / "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb.jsonl"
    )
    target.parent.mkdir(parents=True)
    target.write_text("existing")

    status = main(
        [
            "import",
            str(fixture),
            "--to",
            "claude",
            "--home",
            str(target_home),
            "--dry-run",
            "--session-id",
            "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "--cwd",
            str(tmp_path),
        ]
    )

    assert status == 2
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert "refusing to overwrite" in captured.err


def test_transfer_finds_claude_uuid_and_dry_runs_codex_import(
    tmp_path: Path, capsys: object
) -> None:
    fixture = Path(__file__).parent / "fixtures" / "claude-2.1.209" / "basic.jsonl"
    source_home = tmp_path / "claude-home"
    source_id = "10000000-0000-4000-8000-000000000000"
    source_path = source_home / "projects" / "-work" / f"{source_id}.jsonl"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(fixture.read_bytes())
    target_home = tmp_path / "codex-home"

    status = main(
        [
            "transfer",
            source_id,
            "--from",
            "claude",
            "--source-home",
            str(source_home),
            "--source-cwd",
            "/work",
            "--home",
            str(target_home),
            "--cwd",
            str(tmp_path),
            "--dry-run",
        ]
    )

    assert status == 0
    result = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert result["source_format"] == "claude"
    assert result["target_format"] == "codex"
    assert result["dry_run"] is True
    assert not target_home.exists()
