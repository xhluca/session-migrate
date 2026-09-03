"""Native-corpus checks for exact Devin CLI 3000.6.7."""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, TextIO

import pytest
from native_corpus.loader import load_standalone_fixture
from native_corpus.route_oracle import (
    assert_source_expectations,
    normalize_source_session,
    parse_native_fixture,
)

from session_migrate.formats import devin
from session_migrate.model import EventKind

DEVIN_ID = "pricey-toaster"
FIXTURE_ROOT = Path(__file__).parent / "native_corpus/v1/sources/devin/3000.6.7/portable-rich"
ASSETS = Path(__file__).parent / "native_corpus/v1/assets"


def _binary() -> Path:
    value = os.environ.get("SESSION_MIGRATE_DEVIN_BIN")
    if not value:
        pytest.skip("set SESSION_MIGRATE_DEVIN_BIN to the exact pinned Devin 3000.6.7 binary")
    binary = Path(value)
    devin.verify_pinned_binary(binary)
    completed = subprocess.run(
        [str(binary), "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode == 0
    assert completed.stdout.strip() == "devin 3000.6.7 (260a97c8)"
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == devin.PINNED_DEVIN_LINUX_X64_SHA256
    return binary


def _private(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True)
    path.chmod(0o700)
    return path


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


class _JsonLineReader:
    """Read sequential ACP responses without losing TextIO read-ahead bytes."""

    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self.buffer = bytearray()

    def read_response(self, request_id: int, timeout: float = 30) -> list[dict[str, Any]]:
        selector = selectors.DefaultSelector()
        selector.register(self.stream, selectors.EVENT_READ)
        messages: list[dict[str, Any]] = []
        deadline = time.monotonic() + timeout
        try:
            while True:
                newline = self.buffer.find(b"\n")
                if newline >= 0:
                    raw = bytes(self.buffer[:newline])
                    del self.buffer[: newline + 1]
                    if not raw:
                        continue
                    value = json.loads(raw.decode("utf-8"))
                    messages.append(value)
                    if value.get("id") == request_id:
                        return messages
                    continue

                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    break
                chunk = os.read(self.stream.fileno(), 65_536)
                if not chunk:
                    break
                self.buffer.extend(chunk)
        finally:
            selector.close()
        raise AssertionError(f"exact Devin ACP did not answer request {request_id}")


def _send(stream: TextIO, value: dict[str, Any]) -> None:
    stream.write(json.dumps(value, separators=(",", ":")) + "\n")
    stream.flush()


def test_json_line_reader_retains_responses_from_one_os_read() -> None:
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, encoding="utf-8")
    reader = _JsonLineReader(stream)
    try:
        os.write(write_fd, b'{"id":1,"result":{}}\n{"id":2,"result":{}}\n')
        assert reader.read_response(1, timeout=1)[-1]["id"] == 1
        assert reader.read_response(2, timeout=1)[-1]["id"] == 2
    finally:
        os.close(write_fd)
        stream.close()


def test_sanitized_devin_source_matches_reviewed_ir(tmp_path: Path) -> None:
    fixture = load_standalone_fixture(FIXTURE_ROOT)
    session = parse_native_fixture(fixture, tmp_path / "materialized-devin")

    assert_source_expectations(fixture, session)
    assert session.session_id == DEVIN_ID
    assert session.cwd == Path("/fixture/work")
    assert session.raw_record_count == 14
    assert sum(event.kind == EventKind.TOOL_CALL for event in session.events) == 3
    assert sum(event.kind == EventKind.TOOL_RESULT for event in session.events) == 3
    assert any("SM_CORPUS_7319" in (event.text or "") for event in session.events)
    assert any("COPPER_4821" in (event.text or "") for event in session.events)
    assert any("[Audio content]" in (event.text or "") for event in session.events)
    assert "incorrectly reported 7319" in fixture.provenance.observations["document"]
    assert "advertising audio=false" in fixture.provenance.observations["audio"]
    assert fixture.provenance.modalities["document"].fixture_present is False
    assert fixture.provenance.modalities["audio"].fixture_present is False
    assert fixture.provenance.modalities["video"].fixture_present is False


def test_exact_devin_cold_reloads_sanitized_corpus_source(tmp_path: Path) -> None:
    binary = _binary()
    root = _private(tmp_path / "client-root")
    work = _private(root / "work")
    home = _private(root / "home")
    data = _private(root / "data")
    config = _private(root / "config")
    cache = _private(root / "cache")
    runtime = _private(root / "runtime")
    temporary = _private(root / "tmp")
    for name in ("timeline.py", "CORPUS_NOTE.txt", "corpus-card.png", "corpus-card.jpg"):
        shutil.copyfile(ASSETS / name, work / name)
    database = _private(data / "devin/cli") / "sessions.db"
    shutil.copyfile(FIXTURE_ROOT / "native/sessions.db", database)
    database.chmod(0o600)
    devin_config = _private(config / "devin") / "config.json"
    devin_config.write_text('{"auto_update":false,"proxy":{"mode":"off"}}\n')
    devin_config.chmod(0o600)
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
    command = _bwrap(root, work)
    before = devin.parse_session(database, DEVIN_ID)

    listed = subprocess.run(
        command + [str(binary), "list", "--format", "json"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
    )
    assert listed.returncode == 0, listed.stderr
    entries = json.loads(listed.stdout)
    assert [entry["id"] for entry in entries] == [DEVIN_ID]
    assert entries[0]["working_directory"] == "/fixture/work"

    process = subprocess.Popen(
        command + [str(binary), "acp", "--model", "swe-1-6-slow"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        env=environment,
    )
    assert process.stdin is not None and process.stdout is not None
    reader = _JsonLineReader(process.stdout)
    try:
        _send(
            process.stdin,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": 1,
                    "clientCapabilities": {
                        "fs": {"readTextFile": False, "writeTextFile": False},
                        "terminal": False,
                    },
                    "clientInfo": {"name": "session-migrate-corpus", "version": "1"},
                },
            },
        )
        initialized = reader.read_response(1)
        assert initialized[-1]["result"]["protocolVersion"] == 1
        _send(
            process.stdin,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/load",
                "params": {
                    "cwd": "/fixture/work",
                    "mcpServers": [],
                    "sessionId": DEVIN_ID,
                },
            },
        )
        loaded = reader.read_response(2)
        assert "result" in loaded[-1]
        rendered = json.dumps(loaded, ensure_ascii=False)
        assert "SM_CORPUS_7319" in rendered
        assert "COPPER_4821" in rendered
    finally:
        process.terminate()
        process.wait(timeout=10)

    after = devin.parse_session(database, DEVIN_ID)
    assert normalize_source_session(after) == normalize_source_session(before)
