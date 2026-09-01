"""Native-corpus checks for exact Qwen Code 0.22.1 and Kimi Code 0.38.0."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from native_corpus.loader import load_standalone_fixture
from native_corpus.route_oracle import assert_source_expectations, parse_native_fixture

from session_migrate.formats import kimi, qwen
from session_migrate.model import EventKind, Role

QWEN_ID = "44c47f3e-ccfe-4fb7-aa06-0217dc950456"
KIMI_ID = "session_0e9113f5-23d9-4ad4-8a94-a3f44c175d14"
FOLLOWUP = "SANITIZED_NATIVE_CORPUS_RELOAD_FOLLOWUP"
REPLY = "SANITIZED_NATIVE_CORPUS_RELOAD_OK"
FIXTURE_ROOT = Path(__file__).parent / "native_corpus/v1/sources"
ASSETS = Path(__file__).parent / "native_corpus/v1/assets"


class Provider(ThreadingHTTPServer):
    requests: list[dict[str, Any]]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        try:
            value = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_error(400)
            return
        if not isinstance(value, dict):
            self.send_error(400)
            return
        self.server.requests.append(value)  # type: ignore[attr-defined]
        chunks = [
            {
                "id": "native-corpus-reload",
                "object": "chat.completion.chunk",
                "created": 1788218000,
                "model": "fixture-model",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": REPLY},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "native-corpus-reload",
                "object": "chat.completion.chunk",
                "created": 1788218000,
                "model": "fixture-model",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
            },
        ]
        payload = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        encoded = (payload + "data: [DONE]\n\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def _binary(variable: str, version: str, digest: str) -> Path:
    value = os.environ.get(variable)
    if not value:
        pytest.skip(f"set {variable} to the exact pinned native binary")
    binary = Path(value)
    completed = subprocess.run(
        [str(binary), "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode == 0
    assert version in completed.stdout.strip()
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == digest
    return binary


def _directories(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    paths = tuple(tmp_path / name for name in ("client", "work", "system-home"))
    for path in paths:
        path.mkdir(mode=0o700)
    client, work, system_home = paths
    return client, work, tmp_path, system_home


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


def _provider() -> tuple[Provider, threading.Thread]:
    provider = Provider(("127.0.0.1", 0), Handler)
    provider.requests = []
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    return provider, thread


@pytest.mark.parametrize(
    ("format_name", "version"),
    (("qwen", "0.22.1"), ("kimi", "0.38.0")),
)
def test_sanitized_native_source_matches_reviewed_ir(
    tmp_path: Path, format_name: str, version: str
) -> None:
    fixture = load_standalone_fixture(FIXTURE_ROOT / format_name / version / "portable-rich")
    session = parse_native_fixture(fixture, tmp_path / f"materialized-{format_name}")

    assert_source_expectations(fixture, session)
    assert session.cwd == Path("/fixture/work")
    assert any("SM_CORPUS_7319" in (event.text or "") for event in session.events)
    assert any("COPPER_4821" in (event.text or "") for event in session.events)
    assert sum(event.kind == EventKind.TOOL_CALL for event in session.events) == 2
    assert sum(event.kind == EventKind.TOOL_RESULT for event in session.events) == 2

    image = fixture.provenance.modalities["user_image"]
    assert image.attempted is True
    assert image.fixture_present is False
    if format_name == "qwen":
        assert image.native_accepted is False
        assert "unsupported-image rejection" in fixture.provenance.observations["user_image"]
        assert any(
            "This model does not support image input" in (event.text or "")
            for event in session.events
        )
    else:
        assert image.native_accepted is True
        assert "hung for more than six minutes" in fixture.provenance.observations["user_image"]
        assert sum(event.kind == EventKind.THINKING for event in session.events) == 2


def test_exact_qwen_cold_reloads_sanitized_corpus_source(tmp_path: Path) -> None:
    digest = "68cb29eb7ccc936d78ece5564ef55cae41a55b630e6657dc417c1f2e561cf4c9"
    binary = _binary("SESSION_MIGRATE_QWEN_BIN", qwen.PINNED_QWEN_VERSION, digest)
    client, work, root, system_home = _directories(tmp_path)
    native = FIXTURE_ROOT / "qwen/0.22.1/portable-rich/native" / f"{QWEN_ID}.jsonl"
    destination = client / qwen.session_relative_path(Path("/fixture/work"), QWEN_ID)
    destination.parent.mkdir(parents=True, mode=0o700)
    shutil.copyfile(native, destination)
    os.chmod(destination, 0o600)
    provider, thread = _provider()
    settings = {
        "$version": 4,
        "modelProviders": {
            "openai": [
                {
                    "id": "fixture-model",
                    "name": "Native corpus loopback",
                    "envKey": "FIXTURE_API_KEY",
                    "baseUrl": f"http://127.0.0.1:{provider.server_address[1]}/v1",
                }
            ]
        },
        "security": {"auth": {"selectedType": "openai"}},
        "model": {"name": "fixture-model"},
    }
    (client / "settings.json").write_text(json.dumps(settings))
    environment = {
        "HOME": str(system_home),
        "QWEN_HOME": str(client),
        "FIXTURE_API_KEY": "credential-free-loopback",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "TERM": "dumb",
        "NO_COLOR": "1",
    }
    before = destination.read_bytes()
    try:
        completed = subprocess.run(
            _bwrap(root, work)
            + [
                str(binary),
                "--safe-mode",
                "--resume",
                QWEN_ID,
                "--model",
                "fixture-model",
                "--prompt",
                FOLLOWUP,
                "--output-format",
                "json",
            ],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert completed.returncode == 0, completed.stderr
        after = destination.read_bytes()
        assert len(after) > len(before) and after.startswith(before)
        replay = json.dumps(provider.requests[-1], ensure_ascii=False)
        for marker in (
            "SM_CORPUS_7319",
            "COPPER_4821",
            "This model does not support image input",
            FOLLOWUP,
        ):
            assert marker in replay
        reparsed = qwen.parse_session(destination)
        assert any(
            event.role == Role.ASSISTANT and event.text == REPLY for event in reparsed.events
        )
    finally:
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=5)


def test_exact_kimi_cold_reloads_sanitized_corpus_source(tmp_path: Path) -> None:
    digest = "16be7e507dcb161e6535876bda67a0b99b056d7b1cf779db89dd9447edc7ce76"
    binary = _binary("SESSION_MIGRATE_KIMI_BIN", kimi.PINNED_KIMI_VERSION, digest)
    client, work, root, system_home = _directories(tmp_path)
    native = FIXTURE_ROOT / "kimi/0.38.0/portable-rich/native" / KIMI_ID
    destination = client / "sessions" / kimi.workdir_key(Path("/fixture/work")) / KIMI_ID
    shutil.copytree(native, destination)
    for name in ("timeline.py", "CORPUS_NOTE.txt"):
        shutil.copyfile(ASSETS / name, work / name)
    state_path = destination / "state.json"
    state = json.loads(state_path.read_text())
    state["agents"]["main"]["homedir"] = str(destination / "agents/main")
    state_path.write_text(json.dumps(state))
    os.chmod(state_path, 0o600)
    wire = destination / "agents/main/wire.jsonl"
    before = wire.read_bytes()
    provider, thread = _provider()
    environment = {
        "HOME": str(system_home),
        "KIMI_CODE_HOME": str(client),
        "KIMI_MODEL_NAME": "fixture-model",
        "KIMI_MODEL_PROVIDER_TYPE": "openai",
        "KIMI_MODEL_BASE_URL": f"http://127.0.0.1:{provider.server_address[1]}/v1",
        "KIMI_MODEL_API_KEY": "credential-free-loopback",
        "CHOKIDAR_USEPOLLING": "1",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "TERM": "dumb",
        "NO_COLOR": "1",
    }
    try:
        completed = subprocess.run(
            _bwrap(root, work)
            + [
                str(binary),
                "--session",
                KIMI_ID,
                "--model",
                "__kimi_env_model__",
                "--prompt",
                FOLLOWUP,
                "--output-format",
                "stream-json",
            ],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert completed.returncode == 0, completed.stderr
        after = wire.read_bytes()
        assert len(after) > len(before) and after.startswith(before)
        replay = json.dumps(provider.requests[-1], ensure_ascii=False)
        for marker in ("SM_CORPUS_7319", "COPPER_4821", FOLLOWUP):
            assert marker in replay
        reparsed = kimi.parse_session(wire)
        assert any(
            event.role == Role.ASSISTANT and event.text == REPLY for event in reparsed.events
        )
    finally:
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=5)
