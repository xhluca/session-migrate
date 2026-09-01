"""Opt-in credential-free native trajectory for Mistral Vibe 2.24.3."""

import json
import os
import pty
import select
import shutil
import signal
import struct
import subprocess
import termios
import threading
import time
from contextlib import suppress
from fcntl import ioctl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from session_migrate.conversion import (
    ConversionOptions,
    convert_session,
    install_vibe_artifact,
)
from session_migrate.formats import claude, vibe
from session_migrate.model import EventKind, Role, TargetFormat

FIXTURE = Path(__file__).parent / "fixtures/claude-2.1.209/basic.jsonl"
SESSION_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
FOLLOWUP = "VIBE_NATIVE_FOLLOWUP_GAMMA"
REPLY = "VIBE_NATIVE_APPEND_OMEGA"


class Provider(ThreadingHTTPServer):
    requests: list[dict[str, Any]]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        value = json.loads(self.rfile.read(length))
        assert isinstance(value, dict)
        self.server.requests.append(value)  # type: ignore[attr-defined]
        chunks = [
            {
                "id": "vibe-native-fixture",
                "object": "chat.completion.chunk",
                "created": 1787284800,
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
                "id": "vibe-native-fixture",
                "object": "chat.completion.chunk",
                "created": 1787284800,
                "model": "fixture-model",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 4,
                    "total_tokens": 24,
                },
            },
        ]
        body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        encoded = (body + "data: [DONE]\n\n").encode()
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class SourceHandler(Handler):
    """Return a deterministic reply after Vibe performs native mention reads."""

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        value = json.loads(self.rfile.read(length))
        assert isinstance(value, dict)
        self.server.requests.append(value)  # type: ignore[attr-defined]
        chunks = [
            {
                "id": "vibe-native-source",
                "object": "chat.completion.chunk",
                "created": 1788219900,
                "model": "fixture-model",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "content": (
                                "SM_CORPUS_7319: native mention handling retained "
                                "COPPER_4821 and the BLUE_TRIANGLE_7319 image; "
                                "unsupported media failures remain explicit."
                            ),
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "vibe-native-source",
                "object": "chat.completion.chunk",
                "created": 1788219900,
                "model": "fixture-model",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 30,
                    "completion_tokens": 8,
                    "total_tokens": 38,
                },
            },
        ]
        body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        encoded = (body + "data: [DONE]\n\n").encode()
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def _config(home: Path, port: int) -> None:
    value = f'''active_model = "fixture"
enable_telemetry = false
enable_auto_update = false
enable_update_checks = false
show_greeting = false
enable_connectors = false
bypass_tool_permissions = true

[[providers]]
name = "fixture"
api_base = "http://127.0.0.1:{port}/v1"
api_key_env_var = "VIBE_TEST_API_KEY"
backend = "generic"
emits_finish_reason = true

[[models]]
name = "fixture-model"
provider = "fixture"
alias = "fixture"
thinking = "off"
supports_images = true

[experiments]
enable = false

[session_logging]
save_dir = "{home / "logs/session"}"
session_prefix = "session"
enabled = true
'''
    path = home / "config.toml"
    path.write_text(value)
    path.chmod(0o600)


def _environment(home: Path, temporary: Path) -> dict[str, str]:
    environment = {
        "HOME": str(home.parent / "system-home"),
        "VIBE_HOME": str(home),
        "TMPDIR": str(temporary),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "TERM": "dumb",
        "NO_COLOR": "1",
        "VIBE_TEST_API_KEY": "synthetic-loopback-key",
    }
    for key in ("LANG", "LC_ALL", "LC_CTYPE"):
        if os.environ.get(key):
            environment[key] = os.environ[key]
    return environment


def _run_tui_until(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    marker: str,
    timeout: float = 75,
) -> str:
    """Launch Vibe's public TUI and stop only after its reply is rendered."""

    master, slave = pty.openpty()
    ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 48, 160, 0, 0))
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env={**environment, "TERM": "xterm-256color", "COLORTERM": "truecolor"},
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
    )
    os.close(slave)
    output = bytearray()
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    output.extend(os.read(master, 65_536))
                except OSError:
                    break
            if marker.encode() in output:
                time.sleep(1)
                break
            if process.poll() is not None:
                break
    finally:
        if process.poll() is None:
            with suppress(OSError):
                os.write(master, b"\x03")
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=2)
        os.close(master)
    transcript = output.decode(errors="replace")
    assert marker in transcript, transcript[-4000:]
    return transcript


def test_vibe_2243_native_resume_preserves_prefix_and_appends(tmp_path: Path) -> None:
    binary_value = os.environ.get("SESSION_MIGRATE_VIBE_BIN")
    if not binary_value:
        pytest.skip("set SESSION_MIGRATE_VIBE_BIN to the exact Vibe 2.24.3 binary")
    binary = Path(binary_value)
    home = tmp_path / "vibe"
    work = tmp_path / "work"
    temporary = tmp_path / "tmp"
    system_home = tmp_path / "system-home"
    for directory in (home, work, temporary, system_home):
        directory.mkdir(mode=0o700)

    provider = Provider(("127.0.0.1", 0), Handler)
    provider.requests = []
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    try:
        environment = _environment(home, temporary)
        version = subprocess.run(
            [str(binary), "--version"],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert version.returncode == 0
        assert version.stdout.strip() == f"vibe {vibe.PINNED_VIBE_VERSION}"
        _config(home, provider.server_address[1])

        source = claude.parse(FIXTURE)
        artifact = convert_session(
            source,
            ConversionOptions(
                target_format=TargetFormat.VIBE,
                session_id=SESSION_ID,
                cwd=work,
            ),
        )
        messages_path, _manifest_path = install_vibe_artifact(artifact, target_home=home)
        before = messages_path.read_bytes()

        resumed = subprocess.run(
            [
                str(binary),
                "--resume",
                SESSION_ID[:8],
                "-p",
                FOLLOWUP,
                "--workdir",
                str(work),
                "--trust",
                "--agent",
                "ask",
                "--max-turns",
                "1",
                "--output",
                "text",
            ],
            cwd=work,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert resumed.returncode == 0, resumed.stderr
        assert REPLY in resumed.stdout
        after = messages_path.read_bytes()
        assert after.startswith(before)
        assert len(after) > len(before)

        request_wire = json.dumps(provider.requests, ensure_ascii=False)
        assert "Continue after the synthetic compaction." in request_wire
        assert "The synthetic post-compaction fixture is complete." in request_wire
        assert FOLLOWUP in request_wire

        parsed = vibe.parse_session(messages_path)
        assert any(
            event.kind == EventKind.MESSAGE and event.role == Role.USER and event.text == FOLLOWUP
            for event in parsed.events
        )
        assert any(
            event.kind == EventKind.MESSAGE and event.role == Role.ASSISTANT and event.text == REPLY
            for event in parsed.events
        )
    finally:
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=5)


def test_vibe_2243_creates_native_multimodal_source_from_empty_state(
    tmp_path: Path,
) -> None:
    binary_value = os.environ.get("SESSION_MIGRATE_VIBE_BIN")
    if not binary_value:
        pytest.skip("set SESSION_MIGRATE_VIBE_BIN to the exact Vibe 2.24.3 binary")
    binary = Path(binary_value).resolve()
    home = tmp_path / "vibe"
    work = tmp_path / "work"
    temporary = tmp_path / "tmp"
    system_home = tmp_path / "system-home"
    for directory in (home, work, temporary, system_home):
        directory.mkdir(mode=0o700)
    assets = Path(__file__).parent / "native_corpus/v1/assets"
    for name in (
        "CORPUS_NOTE.txt",
        "corpus-card.png",
        "corpus-document.pdf",
        "corpus-tone.wav",
        "corpus-transition.mp4",
    ):
        shutil.copyfile(assets / name, work / name)

    provider = Provider(("127.0.0.1", 0), SourceHandler)
    provider.requests = []
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    try:
        environment = _environment(home, temporary)
        _config(home, provider.server_address[1])
        prompt = (
            "repair-event-window-boundary: remember SM_CORPUS_7319 and inspect "
            "@CORPUS_NOTE.txt @corpus-card.png @corpus-document.pdf "
            "@corpus-tone.wav @corpus-transition.mp4 using native mention handling; "
            "report only observed evidence and do not edit files."
        )
        _run_tui_until(
            [
                str(binary),
                prompt,
                "--workdir",
                str(work),
                "--trust",
                "--agent",
                "ask",
            ],
            cwd=work,
            environment=environment,
            marker="BLUE_TRIANGLE_7319",
        )
    finally:
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=5)

    session_dirs = tuple(path.parent for path in home.rglob(vibe.META_FILENAME))
    assert len(session_dirs) == 1
    source = vibe.parse_session(session_dirs[0])
    assert source.cwd == work
    assert source.title and source.title.startswith("repair-event-window-boundary")
    assert any("SM_CORPUS_7319" in (event.text or "") for event in source.events)
    assert any("COPPER_4821" in (event.text or "") for event in source.events)
    # The public TUI (unlike Vibe's headless prompt shortcut) snapshots the
    # image and persists the attachment in messages.jsonl.
    assert any(
        event.kind == EventKind.CONTEXT
        and event.role == Role.USER
        and event.payload.get("block_type") == "image"
        for event in source.events
    )
    calls = [event for event in source.events if event.kind == EventKind.TOOL_CALL]
    results = [event for event in source.events if event.kind == EventKind.TOOL_RESULT]
    assert len(calls) == 4
    assert len(results) == 4
    assert all(event.payload.get("is_error") is not True for event in results)
    replay = json.dumps(provider.requests, ensure_ascii=False, sort_keys=True)
    for marker in ("SM_CORPUS_7319", "COPPER_4821", "image/png"):
        assert marker in replay


def test_vibe_2243_cold_reloads_sanitized_native_corpus_source(tmp_path: Path) -> None:
    binary_value = os.environ.get("SESSION_MIGRATE_VIBE_BIN")
    if not binary_value:
        pytest.skip("set SESSION_MIGRATE_VIBE_BIN to the exact Vibe 2.24.3 binary")
    binary = Path(binary_value).resolve()
    source_id = "76f1b367-a336-6b84-96cb-66ccf903b3d5"
    fixture = (
        Path(__file__).parent
        / "native_corpus/v1/sources/vibe/2.24.3/portable-rich/native"
        / "session_20260831_235533_76f1b367"
    )
    home = tmp_path / "vibe"
    work = tmp_path / "work"
    temporary = tmp_path / "tmp"
    system_home = tmp_path / "system-home"
    destination = home / "logs/session" / fixture.name
    for directory in (home, work, temporary, system_home, destination):
        directory.mkdir(parents=True, mode=0o700)
    shutil.copytree(fixture / "attachments", destination / "attachments")
    attachment = next((destination / "attachments").glob("*.png"))
    replacements = {
        b"/fixture/work": str(work).encode(),
        b"attachments/c777cb87fcdbee8700fbe5b029801028541556b0.png": str(attachment).encode(),
    }
    for name in (vibe.META_FILENAME, vibe.MESSAGES_FILENAME):
        data = (fixture / name).read_bytes()
        for source, target in replacements.items():
            data = data.replace(source, target)
        path = destination / name
        path.write_bytes(data)
        path.chmod(0o600)
    messages = destination / vibe.MESSAGES_FILENAME
    before = messages.read_bytes()

    provider = Provider(("127.0.0.1", 0), Handler)
    provider.requests = []
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    try:
        environment = _environment(home, temporary)
        _config(home, provider.server_address[1])
        completed = subprocess.run(
            [
                str(binary),
                "--resume",
                source_id[:8],
                "-p",
                "COLD_RELOAD_VIBE_8421",
                "--workdir",
                str(work),
                "--trust",
                "--agent",
                "ask",
                "--max-turns",
                "20",
                "--output",
                "text",
            ],
            cwd=work,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=90,
        )
    finally:
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=5)

    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    assert messages.read_bytes().startswith(before)
    replay = json.dumps(provider.requests, ensure_ascii=False, sort_keys=True)
    for marker in (
        "SM_CORPUS_7319",
        "COPPER_4821",
        "image/png",
        "COLD_RELOAD_VIBE_8421",
    ):
        assert marker in replay
    resumed = vibe.parse_session(destination)
    assert any(event.text == "COLD_RELOAD_VIBE_8421" for event in resumed.events)
    assert any(event.text == REPLY for event in resumed.events)
