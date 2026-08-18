import json
from pathlib import Path

import pytest

from session_bridge import __version__
from session_bridge.cli import build_parser, main
from session_bridge.formats.claude import project_directory_name


def test_parser_exposes_version() -> None:
    assert __version__ == "0.2.0"
    assert build_parser().prog == "session-bridge"


def test_parser_expands_home_in_every_path_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    parser = build_parser()

    global_args = parser.parse_args(["--catalog", "~/catalog.sqlite3", "catalog", "list"])
    assert global_args.catalog == tmp_path / "catalog.sqlite3"

    inspect_args = parser.parse_args(["inspect", "~/source.jsonl"])
    assert inspect_args.path == tmp_path / "source.jsonl"

    convert_args = parser.parse_args(
        [
            "convert",
            "~/source.jsonl",
            "--to",
            "codex",
            "--output",
            "~/output.jsonl",
            "--cwd",
            "~/work",
        ]
    )
    assert convert_args.path == tmp_path / "source.jsonl"
    assert convert_args.output == tmp_path / "output.jsonl"
    assert convert_args.cwd == tmp_path / "work"

    import_args = parser.parse_args(
        ["import", "~/source.jsonl", "--to", "claude", "--home", "~/target"]
    )
    assert import_args.path == tmp_path / "source.jsonl"
    assert import_args.home == tmp_path / "target"

    transfer_args = parser.parse_args(
        [
            "transfer",
            "10000000-0000-4000-8000-000000000000",
            "--from",
            "claude",
            "--source-home",
            "~/source-home",
            "--source-cwd",
            "~/source-work",
            "--home",
            "~/target-home",
            "--cwd",
            "~/target-work",
        ]
    )
    assert transfer_args.source_home == tmp_path / "source-home"
    assert transfer_args.source_cwd == tmp_path / "source-work"
    assert transfer_args.home == tmp_path / "target-home"
    assert transfer_args.cwd == tmp_path / "target-work"

    refresh_args = parser.parse_args(
        [
            "catalog",
            "refresh",
            "--claude-root",
            "~/claude-one",
            "--claude-root",
            "~/claude-two",
            "--codex-root",
            "~/codex",
            "--discover-under",
            "~/workspace",
        ]
    )
    assert refresh_args.claude_root == [
        tmp_path / "claude-one",
        tmp_path / "claude-two",
    ]
    assert refresh_args.codex_root == [tmp_path / "codex"]
    assert refresh_args.discover_under == [tmp_path / "workspace"]


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


def test_convert_expands_quoted_home_in_paths_and_result(
    tmp_path: Path, capsys: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = Path(__file__).parent / "fixtures" / "claude-2.1.209" / "basic.jsonl"
    monkeypatch.setenv("HOME", str(tmp_path))

    status = main(
        [
            "convert",
            str(fixture),
            "--to",
            "codex",
            "--output",
            "~/converted.jsonl",
            "--cwd",
            str(tmp_path),
        ]
    )

    assert status == 0
    result = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    output = tmp_path / "converted.jsonl"
    manifest = tmp_path / "converted.jsonl.session-bridge.json"
    assert output.exists()
    assert manifest.exists()
    assert result["output"] == str(output)
    assert result["manifest"] == str(manifest)


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


def test_transfer_requires_native_session_id_metadata(tmp_path: Path, capsys: object) -> None:
    fixture = Path(__file__).parent / "fixtures" / "claude-2.1.209" / "basic.jsonl"
    source_home = tmp_path / "claude-home"
    source_id = "10000000-0000-4000-8000-000000000000"
    source_path = source_home / "projects" / "-work" / f"{source_id}.jsonl"
    source_path.parent.mkdir(parents=True)
    records = [json.loads(line) for line in fixture.read_text().splitlines()]
    for record in records:
        record.pop("sessionId", None)
    source_path.write_text("\n".join(json.dumps(record) for record in records) + "\n")

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
            str(tmp_path / "codex-home"),
            "--dry-run",
        ]
    )

    assert status == 2
    assert "no native session ID metadata" in capsys.readouterr().err  # type: ignore[attr-defined]


def test_catalog_cli_refresh_search_show_and_root_management(
    tmp_path: Path, capsys: object
) -> None:
    source_home = tmp_path / "claude-home"
    source_id = "10000000-0000-4000-8000-000000000000"
    source_path = source_home / "projects" / "-work" / f"{source_id}.jsonl"
    source_path.parent.mkdir(parents=True)
    records = [
        {
            "type": "user",
            "uuid": "10000000-0000-4000-8000-000000000001",
            "parentUuid": None,
            "sessionId": source_id,
            "timestamp": "2026-08-18T10:00:00Z",
            "cwd": "/synthetic/work",
            "version": "2.1.209",
            "isSidechain": False,
            "message": {"role": "user", "content": "body must not be searchable"},
        },
        {
            "type": "custom-title",
            "customTitle": "Searchable synthetic title",
            "sessionId": source_id,
        },
    ]
    source_path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    database = tmp_path / "catalog.sqlite3"
    prefix = ["--catalog", str(database), "catalog"]

    assert (
        main(
            [
                *prefix,
                "refresh",
                "--claude-root",
                str(source_home),
                "--no-auto-roots",
                "--json",
            ]
        )
        == 0
    )
    refreshed = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert refreshed["files_seen"] == 1
    assert refreshed["statuses"] == {"candidate": 1}

    assert main([*prefix, "search", "searchable synthetic", "--json"]) == 0
    matches = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert len(matches) == 1
    catalog_id = matches[0]["catalog_id"]
    assert matches[0]["title"] == "Searchable synthetic title"
    assert matches[0]["path"] is None

    assert main([*prefix, "search", "body must not", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []  # type: ignore[attr-defined]

    assert main([*prefix, "show", catalog_id, "--include-paths", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert shown["path"] == str(source_path.resolve())

    assert main([*prefix, "roots", "list", "--json"]) == 0
    roots = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert len(roots) == 1
    assert main([*prefix, "roots", "remove", str(roots[0]["id"])]) == 0
    assert "native files were not changed" in capsys.readouterr().out  # type: ignore[attr-defined]
    assert source_path.exists()


def test_transfer_by_catalog_id_authoritatively_loads_and_dry_runs(
    tmp_path: Path, capsys: object
) -> None:
    fixture = Path(__file__).parent / "fixtures" / "claude-2.1.209" / "basic.jsonl"
    source_id = "10000000-0000-4000-8000-000000000000"
    source_home = tmp_path / "claude-home"
    source_path = source_home / "projects" / "-work" / f"{source_id}.jsonl"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(fixture.read_bytes())
    database = tmp_path / "catalog.sqlite3"
    prefix = ["--catalog", str(database)]

    assert (
        main(
            [
                *prefix,
                "catalog",
                "refresh",
                "--claude-root",
                str(source_home),
                "--no-auto-roots",
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()  # type: ignore[attr-defined]
    assert main([*prefix, "catalog", "list", "--json"]) == 0
    catalog_id = json.loads(capsys.readouterr().out)[0]["catalog_id"]  # type: ignore[attr-defined]

    target_home = tmp_path / "codex-home"
    assert (
        main(
            [
                *prefix,
                "transfer",
                "--catalog-id",
                catalog_id,
                "--home",
                str(target_home),
                "--cwd",
                str(tmp_path),
                "--dry-run",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert result["source_format"] == "claude"
    assert result["target_format"] == "codex"
    assert result["dry_run"] is True
    assert not target_home.exists()
