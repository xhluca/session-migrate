from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path
from types import ModuleType

import pytest
from native_corpus.loader import load_standalone_fixture
from native_corpus.route_oracle import assert_source_expectations, parse_native_fixture

from session_migrate.formats import cursor
from session_migrate.model import AgentFormat, Event, EventKind, Provenance, Role, Session

FIXTURE_ROOT = (
    Path(__file__).parent / "native_corpus/v1/sources/cursor/2026.03.20-44cb435/portable-rich"
)
SOURCE_MARKER = "CURSOR_NATIVE_SOURCE_COMPLETE"
FOLLOWUP_MARKER = "CURSOR_NATIVE_FOLLOWUP_COMPLETE"
RELOAD_MARKER = "CURSOR_SANITIZED_RELOAD_COMPLETE"


def _sanitizer() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts/native-corpus/sanitize-cursor.py"
    spec = importlib.util.spec_from_file_location("sanitize_cursor", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _portable_source(cwd: Path, private_root: str, private_home: str) -> Session:
    events = (
        Event(
            kind=EventKind.MESSAGE,
            role=Role.USER,
            text=(
                f"Inspect {private_root}/work/timeline.py for owner alice and cache {private_home}."
            ),
            provenance=Provenance(0, "user"),
        ),
        Event(
            kind=EventKind.MESSAGE,
            role=Role.ASSISTANT,
            text="COPPER_4821 remains available to alice.",
            provenance=Provenance(1, "assistant"),
        ),
    )
    return Session(
        source_format=AgentFormat.CLAUDE,
        source_path=cwd / "source.jsonl",
        source_sha256="0" * 64,
        session_id=None,
        cwd=cwd,
        started_at="2026-09-01T00:00:00Z",
        cli_version=None,
        model=None,
        title="private Cursor capture",
        events=events,
        raw_record_count=len(events),
    )


def test_cursor_sanitizer_rehashes_private_content_addressed_graph(tmp_path: Path) -> None:
    sanitizer = _sanitizer()
    session_id = "760c3e81-113f-4000-aae9-fbfcefc67ce7"
    private_home = "/private/user-home/alice"
    private_root = f"{private_home}/captures/cursor-source"
    cwd = Path(f"{private_root}/work")
    data, losses = cursor.serialize(
        _portable_source(cwd, private_root, private_home),
        session_id=session_id,
        cwd=cwd,
        timestamp="2026-09-01T00:00:00Z",
    )
    assert losses == {"runtime_metadata:source_format": 1}
    raw_store = tmp_path / "raw/store.db"
    raw_store.parent.mkdir()
    raw_store.write_bytes(data)

    result = sanitizer.sanitize_store(
        raw_store,
        tmp_path / "sanitized",
        source_capture_root=private_root,
        source_user_home=private_home,
        source_username="alice",
    )

    artifact = tmp_path / "sanitized" / result["artifact"]
    assert result["session_id"] == session_id
    assert result["mutations"]["capture_root"] == 1
    assert result["mutations"]["user_home"] == 1
    assert result["mutations"]["username"] == 2
    assert result["mutations"]["content_addressed_blob_ids"] >= 3
    assert result["mutations"]["title"] == 1
    assert result["rehash_rounds"] >= 2
    assert os.stat(artifact).st_mode & 0o777 == 0o600

    parsed = cursor.parse(artifact, cwd=Path(result["native_cwd"]))
    assert parsed.session_id == session_id
    assert parsed.title == sanitizer.PUBLIC_TITLE
    joined = "\n".join(event.text or "" for event in parsed.events)
    assert "COPPER_4821" in joined
    assert private_root not in joined
    assert private_home not in joined
    assert "alice" not in joined
    assert sanitizer.PUBLIC_CAPTURE_PREFIX in joined
    assert sanitizer.PUBLIC_USER_PREFIX in joined
    assert sanitizer.PUBLIC_USERNAME in joined
    assert private_root.encode() in raw_store.read_bytes()


def test_promoted_cursor_fixture_matches_reviewed_ir(tmp_path: Path) -> None:
    fixture = load_standalone_fixture(FIXTURE_ROOT)
    session = parse_native_fixture(fixture, tmp_path / "cursor")

    assert fixture.provenance.producer.version == cursor.PINNED_CURSOR_VERSION
    assert fixture.provenance.capture.created_by_exact_cli is True
    assert fixture.provenance.sanitization.reloaded_by_exact_cli is True
    assert set(fixture.provenance.modalities) == {
        "audio",
        "compaction",
        "document",
        "readable_reasoning",
        "text",
        "tool_call",
        "tool_result",
        "tool_result_image",
        "user_image",
        "video",
    }
    assert_source_expectations(fixture, session)
    joined = "\n".join(event.text or "" for event in session.events)
    assert SOURCE_MARKER in joined
    assert FOLLOWUP_MARKER in joined
    assert "COPPER_4821" in joined
    assert "ORBIT_2048" in joined
    assert "7319" in joined


def _exact_cursor() -> Path:
    if os.environ.get("SESSION_MIGRATE_RUN_CURSOR_CORPUS") != "1":
        pytest.skip("set SESSION_MIGRATE_RUN_CURSOR_CORPUS=1 for the exact Cursor gate")
    configured = os.environ.get("SESSION_MIGRATE_CURSOR_BIN", "").strip()
    if not configured:
        pytest.skip("set SESSION_MIGRATE_CURSOR_BIN to the exact pinned launcher")
    return cursor.verify_pinned_cli(Path(configured))


def _credential_file(variable: str) -> Path:
    value = os.environ.get(variable, "").strip()
    if not value:
        pytest.skip(f"set {variable} to a private Cursor credential/config file")
    path = Path(value).expanduser()
    if not path.is_file() or path.is_symlink():
        pytest.fail(f"{variable} is not a regular file")
    return path


def _exact_environment(tmp_path: Path, config_root: Path) -> dict[str, str]:
    home = tmp_path / "home"
    xdg_config = tmp_path / "xdg-config"
    for path in (
        home,
        config_root,
        xdg_config / "cursor",
        tmp_path / "xdg-cache",
        tmp_path / "xdg-data",
    ):
        path.mkdir(parents=True, mode=0o700)
    shutil.copyfile(
        _credential_file("SESSION_MIGRATE_CURSOR_AUTH_JSON"),
        xdg_config / "cursor/auth.json",
    )
    shutil.copyfile(
        _credential_file("SESSION_MIGRATE_CURSOR_CONFIG_JSON"), config_root / "cli-config.json"
    )
    os.chmod(xdg_config / "cursor/auth.json", 0o600)
    os.chmod(config_root / "cli-config.json", 0o600)
    return {
        "HOME": str(home),
        "CURSOR_CONFIG_DIR": str(config_root),
        "XDG_CONFIG_HOME": str(xdg_config),
        "XDG_CACHE_HOME": str(tmp_path / "xdg-cache"),
        "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TERM": "xterm-256color",
    }


def test_exact_cursor_cold_reloads_sanitized_native_fixture(tmp_path: Path) -> None:
    cli = _exact_cursor()
    fixture = load_standalone_fixture(FIXTURE_ROOT)
    workspace = Path(fixture.provenance.native_cwd)
    if workspace.exists():
        pytest.fail(f"fixed Cursor corpus workspace already exists: {workspace}")
    workspace.mkdir(parents=True, mode=0o700)
    try:
        config_root = tmp_path / "home/.cursor"
        env = _exact_environment(tmp_path, config_root)
        materialized = fixture.materialize(tmp_path / "fixture")
        source = next(path for path in materialized.artifact_paths if path.name == "store.db")
        target = config_root / cursor.session_relative_path(
            fixture.provenance.native_session_id, workspace
        )
        target.parent.mkdir(parents=True, mode=0o700)
        shutil.copyfile(source, target)
        os.chmod(target, 0o600)
        completed = subprocess.run(
            [
                str(cli),
                f"--resume={fixture.provenance.native_session_id}",
                "--print",
                "--trust",
                "--force",
                "--model",
                "auto",
                "--output-format",
                "json",
                (
                    "Without tools, recall COPPER_4821, ORBIT_2048, image digits 7319, "
                    "the audio frequency, video transition, and boundary fix. End exactly "
                    f"with {RELOAD_MARKER}."
                ),
            ],
            cwd=workspace,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr[-2_000:]
        assert RELOAD_MARKER in completed.stdout
        assert "COPPER_4821" in completed.stdout
        assert "ORBIT_2048" in completed.stdout
        assert "7319" in completed.stdout
        assert "440" in completed.stdout
    finally:
        shutil.rmtree(workspace)
