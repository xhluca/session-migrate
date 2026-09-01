"""Source-native corpus checks for exact Muse Code 0.2.1."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from native_corpus.loader import ALLOWED_MODALITIES, load_standalone_fixture
from native_corpus.route_oracle import assert_source_expectations, parse_native_fixture

from session_migrate.formats import muse
from session_migrate.model import EventKind, Role

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/native_corpus/v1/sources/muse/0.2.1/portable-rich"
SANITIZER = ROOT / "scripts/native-corpus/sanitize-muse.py"
CAPTURE = ROOT / "scripts/native-corpus/capture-muse.py"
SESSION_ID = "74747474-7474-4747-8747-747474747474"
FOLLOWUP = "SANITIZED_NATIVE_CORPUS_RELOAD_FOLLOWUP"
REPLY = "SANITIZED_NATIVE_CORPUS_RELOAD_OK"
MUSE_BYTES = 191_895_736
MUSE_SHA256 = "bfd8660b3a4fce67ab3287b0bd27ea64db1ee8472e8d7cb0f0f9aa8e083c9957"


def _load_script(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _encode(records: tuple[dict[str, Any], ...]) -> bytes:
    return b"".join(
        (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        for record in records
    )


def test_muse_sanitizer_is_idempotent_on_reviewed_public_log() -> None:
    sanitizer = _load_script("sanitize_muse_corpus", SANITIZER)
    native = FIXTURE / "native/session.jsonl"

    result = sanitizer.sanitize(
        native,
        source_root=Path("/fixture"),
        session_id=SESSION_ID,
    )

    assert result.mutations == {}
    assert _encode(result.records) == native.read_bytes()


def test_muse_sanitizer_rejects_unknown_envelope_and_payload_schema(
    tmp_path: Path,
) -> None:
    sanitizer = _load_script("sanitize_muse_corpus_schema", SANITIZER)
    native = FIXTURE / "native/session.jsonl"
    records = [json.loads(line) for line in native.read_text().splitlines()]

    records[0]["unknown_native_field"] = True
    malformed = tmp_path / "unknown-envelope.jsonl"
    malformed.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    with pytest.raises(sanitizer.SanitizationError, match="envelope fields changed"):
        sanitizer.sanitize(
            malformed,
            source_root=Path("/fixture"),
            session_id=SESSION_ID,
        )

    del records[0]["unknown_native_field"]
    records[1]["payload_type"] = "runtime.hologram"
    malformed = tmp_path / "unknown-payload.jsonl"
    malformed.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    with pytest.raises(sanitizer.SanitizationError, match="payload type is unsupported"):
        sanitizer.sanitize(
            malformed,
            source_root=Path("/fixture"),
            session_id=SESSION_ID,
        )


def test_muse_promoted_fixture_matches_reviewed_ir_and_media_evidence(
    tmp_path: Path,
) -> None:
    fixture = load_standalone_fixture(FIXTURE)
    session = parse_native_fixture(fixture, tmp_path / "materialized")

    assert_source_expectations(fixture, session)
    assert session.cwd == Path("/fixture/work")
    assert session.cli_version == muse.PINNED_MUSE_VERSION
    assert set(fixture.provenance.modalities) == ALLOWED_MODALITIES
    assert sum(event.kind == EventKind.TOOL_CALL for event in session.events) == 3
    assert sum(event.kind == EventKind.TOOL_RESULT for event in session.events) == 3
    assert any(
        event.kind == EventKind.TOOL_RESULT
        and event.text == "tool failed: No such file or directory (os error 2)"
        for event in session.events
    )
    for marker in ("SM_CORPUS_7319", "COPPER_4821", "BLUE_TRIANGLE_7319"):
        assert any(marker in (event.text or "") for event in session.events)

    modalities = fixture.provenance.modalities
    assert modalities["user_image"].attempted is True
    assert modalities["user_image"].native_accepted is True
    assert modalities["user_image"].fixture_present is False
    assert modalities["user_image"].portable == "lossy"
    for name in ("document", "audio", "video"):
        assert modalities[name].attempted is True
        assert modalities[name].native_accepted is False
        assert modalities[name].fixture_present is False
        assert modalities[name].portable == "unsupported"


def _exact_binary(variable: str, *, version: str, size: int | None = None) -> Path:
    value = os.environ.get(variable)
    if not value:
        pytest.skip(f"set {variable} to the exact pinned executable")
    binary = Path(value).resolve(strict=True)
    completed = subprocess.run(
        [str(binary), "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode == 0
    assert version in completed.stdout.strip()
    if size is not None:
        assert binary.stat().st_size == size
        assert hashlib.sha256(binary.read_bytes()).hexdigest() == MUSE_SHA256
    return binary


def _unused_port() -> int:
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        return int(handle.getsockname()[1])


def _wait_for_port(process: subprocess.Popen[bytes], port: int) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail("muse-code-openrouter exited before accepting connections")
        with socket.socket() as handle:
            handle.settimeout(0.2)
            if handle.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    pytest.fail("muse-code-openrouter did not start in time")


def _bwrap(root: Path, work: Path) -> list[str]:
    binary = shutil.which("bwrap")
    if binary is None:
        pytest.skip("bubblewrap is required to materialize the canonical /fixture/work CWD")
    arguments = [binary, "--tmpfs", "/", "--dev", "/dev", "--proc", "/proc"]
    for directory in ("/usr", "/lib", "/lib64", "/tmp", "/dev/shm", "/etc"):
        if Path(directory).exists():
            arguments.extend(("--ro-bind", directory, directory))
    arguments.extend(
        (
            "--bind",
            str(root),
            str(root),
            "--dir",
            "/fixture",
            "--bind",
            str(work),
            "/fixture/work",
            "--chdir",
            "/fixture/work",
        )
    )
    return arguments


def test_exact_muse_cold_reloads_and_continues_sanitized_fixture(
    tmp_path: Path,
) -> None:
    muse_binary = _exact_binary(
        "SESSION_MIGRATE_MUSE_BIN",
        version="Muse Code 0.2.1 (0.2.1-R1215.1)",
        size=MUSE_BYTES,
    )
    adapter_binary = _exact_binary(
        "SESSION_MIGRATE_MUSE_OPENROUTER_BIN",
        version="0.3.2",
    )
    capture = _load_script("capture_muse_corpus_reload", CAPTURE)
    root = tmp_path
    home = root / "home"
    data = root / "data"
    config = root / "config"
    cache = root / "cache"
    temporary = root / "tmp"
    work = root / "work"
    for path in (home, data, config, cache, temporary, work):
        path.mkdir(mode=0o700)
    for asset in (ROOT / "tests/native_corpus/v1/assets").iterdir():
        if asset.is_file():
            shutil.copy2(asset, work / asset.name)

    destination = data / "muse/sessions/2026/08/31" / SESSION_ID / "session.jsonl"
    destination.parent.mkdir(parents=True, mode=0o700)
    shutil.copyfile(FIXTURE / "native/session.jsonl", destination)
    destination.chmod(0o600)
    before = destination.read_bytes()

    upstream = capture.Upstream(("127.0.0.1", 0), capture.Handler)
    upstream.requests = []
    upstream.lock = threading.Lock()
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    credential = root / "openrouter-key"
    credential.write_text(f"sk-or-v1-{'0' * 64}\n")
    credential.chmod(0o600)
    adapter_port = _unused_port()
    adapter_environment = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(config),
        "MUSE_CODE_OPENROUTER_CREDENTIAL_FILE": str(credential),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "NO_COLOR": "1",
    }
    adapter = subprocess.Popen(
        [
            str(adapter_binary),
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(adapter_port),
            "--model",
            capture.MODEL,
            "--upstream",
            f"http://127.0.0.1:{upstream.server_port}/v1",
            "--log-level",
            "warning",
        ],
        env=adapter_environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    completed: subprocess.CompletedProcess[str] | None = None
    try:
        _wait_for_port(adapter, adapter_port)
        environment = {
            **adapter_environment,
            "XDG_DATA_HOME": str(data),
            "XDG_CACHE_HOME": str(cache),
            "TMPDIR": str(temporary),
            "MUSE_NO_AUTO_UPDATE": "1",
            "META_API_KEY": "credential-free-loopback",
            "TERM": "dumb",
        }
        completed = subprocess.run(
            _bwrap(root, work)
            + [
                str(muse_binary),
                "exec",
                "--session-id",
                SESSION_ID,
                "--provider",
                "meta",
                "--base-url",
                f"http://127.0.0.1:{adapter_port}/v1",
                "--model",
                capture.MODEL,
                "--reasoning-effort",
                "minimal",
                "--workspace",
                "/fixture/work",
                "--disable-approval",
                "--disable-sandbox",
                "--disable-web-tools",
                "--no-foreign-personal-context",
                "--json",
                FOLLOWUP,
            ],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=90,
        )
    finally:
        adapter.terminate()
        try:
            adapter.wait(timeout=10)
        except subprocess.TimeoutExpired:
            adapter.kill()
            adapter.wait(timeout=5)
        upstream.shutdown()
        upstream.server_close()
        upstream_thread.join(timeout=5)

    assert completed is not None
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    after = destination.read_bytes()
    assert len(after) > len(before) and after.startswith(before)
    replay = json.dumps(upstream.requests, ensure_ascii=False)
    for marker in (
        "SM_CORPUS_7319",
        "COPPER_4821",
        "BLUE_TRIANGLE_7319",
        FOLLOWUP,
    ):
        assert marker in replay
    reparsed = muse.parse_session(destination)
    assert any(
        event.kind == EventKind.MESSAGE and event.role == Role.ASSISTANT and event.text == REPLY
        for event in reparsed.events
    )
