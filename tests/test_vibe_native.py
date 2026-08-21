"""Opt-in credential-free native trajectory for Mistral Vibe 2.24.3."""

import json
import os
import subprocess
import threading
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
