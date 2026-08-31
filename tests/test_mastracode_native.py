"""Opt-in exact-package native resume trajectory for MastraCode 0.37.1."""

import json
import os
import sqlite3
import subprocess
import threading
from hashlib import file_digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from session_migrate.formats import mastracode
from session_migrate.model import AgentFormat, Event, EventKind, Provenance, Role, Session

SESSION_ID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
RESOURCE_ID = "mastracode-native-resume"
IMPORTED_USER = "MASTRACODE_NATIVE_IMPORTED_USER_ALPHA"
IMPORTED_ASSISTANT = "MASTRACODE_NATIVE_IMPORTED_ASSISTANT_BETA"
IMPORTED_TOOL = "MASTRACODE_NATIVE_IMPORTED_TOOL_GAMMA"
IMPORTED_SUMMARY = "MASTRACODE_NATIVE_IMPORTED_SUMMARY_DELTA"
FOLLOWUP = "MASTRACODE_NATIVE_RESUME_FOLLOWUP_EPSILON"
REPLY = "MASTRACODE_NATIVE_RESUME_REPLY_OMEGA"


class Provider(ThreadingHTTPServer):
    requests: list[dict[str, Any]]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def do_GET(self) -> None:  # noqa: N802
        body = json.dumps(
            {
                "object": "list",
                "data": [
                    {
                        "id": "fixture-model",
                        "object": "model",
                        "created": 0,
                        "owned_by": "session-migrate-loopback",
                    }
                ],
            }
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        request = json.loads(self.rfile.read(length))
        assert isinstance(request, dict)
        self.server.requests.append(request)  # type: ignore[attr-defined]
        chunks = [
            {
                "id": "mastracode-native-resume",
                "object": "chat.completion.chunk",
                "created": 1_788_093_400,
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
                "id": "mastracode-native-resume",
                "object": "chat.completion.chunk",
                "created": 1_788_093_400,
                "model": "fixture-model",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 40,
                    "completion_tokens": 6,
                    "total_tokens": 46,
                },
            },
        ]
        body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        encoded = (body + "data: [DONE]\n\n").encode()
        self.send_response(200)
        self.send_header("content-type", "text/event-stream; charset=utf-8")
        self.send_header("cache-control", "no-cache")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def _source(work: Path) -> Session:
    return Session(
        source_format=AgentFormat.CLAUDE,
        source_path=work / "portable.jsonl",
        source_sha256="0" * 64,
        session_id="11111111-1111-4111-8111-111111111111",
        cwd=work,
        started_at="2026-08-30T12:34:56Z",
        cli_version="2.1.209",
        model="fixture-model",
        title="MastraCode native resume fixture",
        events=(
            Event(
                EventKind.MESSAGE,
                Provenance(0, "user"),
                role=Role.USER,
                timestamp="2026-08-30T12:34:56Z",
                text=IMPORTED_USER,
            ),
            Event(
                EventKind.MESSAGE,
                Provenance(1, "assistant"),
                role=Role.ASSISTANT,
                timestamp="2026-08-30T12:34:57Z",
                text=IMPORTED_ASSISTANT,
            ),
            Event(
                EventKind.TOOL_CALL,
                Provenance(1, "assistant", block_index=1),
                role=Role.ASSISTANT,
                timestamp="2026-08-30T12:34:57Z",
                tool_name="execute_command",
                tool_call_id="call_mastracode_native",
                payload={"input": {"command": f"printf {IMPORTED_TOOL}"}},
            ),
            Event(
                EventKind.TOOL_RESULT,
                Provenance(2, "tool"),
                role=Role.TOOL,
                timestamp="2026-08-30T12:34:58Z",
                text=IMPORTED_TOOL,
                tool_name="execute_command",
                tool_call_id="call_mastracode_native",
                payload={"content": IMPORTED_TOOL},
            ),
            Event(
                EventKind.COMPACTION,
                Provenance(3, "compaction"),
                timestamp="2026-08-30T12:34:59Z",
                text=IMPORTED_SUMMARY,
            ),
        ),
        raw_record_count=5,
        model_provider="loopback",
    )


def _environment(home: Path, app_data: Path, temporary: Path) -> dict[str, str]:
    environment = {
        "HOME": str(home),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "MASTRA_APP_DATA_DIR": str(app_data),
        "TMPDIR": str(temporary),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "TERM": "dumb",
        "NO_COLOR": "1",
        "DO_NOT_TRACK": "1",
    }
    for key in ("LANG", "LC_ALL", "LC_CTYPE"):
        if os.environ.get(key):
            environment[key] = os.environ[key]
    return environment


def test_mastracode_0371_import_replay_resume_and_append(tmp_path: Path) -> None:
    binary_value = os.environ.get("SESSION_MIGRATE_MASTRACODE_BIN")
    if not binary_value:
        pytest.skip("set SESSION_MIGRATE_MASTRACODE_BIN to mastracode@0.37.1")
    binary = Path(binary_value).resolve()
    assert binary.is_file()
    assert binary.stat().st_size == mastracode.PINNED_MASTRACODE_CLI_JS_BYTES
    with binary.open("rb") as stream:
        assert (
            file_digest(stream, "sha256").hexdigest() == mastracode.PINNED_MASTRACODE_CLI_JS_SHA256
        )
    package = json.loads((binary.parent.parent / "package.json").read_text())
    assert package["name"] == "mastracode"
    assert package["version"] == mastracode.PINNED_MASTRACODE_VERSION

    home = tmp_path / "home"
    app_data = tmp_path / "app-data"
    temporary = tmp_path / "tmp"
    work = tmp_path / "work"
    for directory in (home, app_data, temporary, work):
        directory.mkdir(mode=0o700)
    target = app_data / "mastra.db"
    artifact, dropped = mastracode.serialize(
        _source(work),
        session_id=SESSION_ID,
        cwd=work,
        resource_id=RESOURCE_ID,
        model="mastracode/session-migrate-loopback/fixture-model",
        timestamp="2026-08-30T12:34:56Z",
    )
    assert dropped == {}
    mastracode.install_native_bytes(artifact, target, session_id=SESSION_ID)
    before = mastracode.parse_session(target, SESSION_ID)

    provider = Provider(("127.0.0.1", 0), Handler)
    provider.requests = []
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    try:
        # Model resolution reloads the global settings path even when the
        # headless bootstrap received --settings, so keep the opt-in fixture at
        # the native app-data location and pass that exact path as well.
        settings = app_data / "settings.json"
        settings.write_text(
            json.dumps(
                {
                    "customProviders": [
                        {
                            "name": "Session Migrate Loopback",
                            "url": f"http://127.0.0.1:{provider.server_address[1]}/v1",
                            "apiKey": "local-test-key",
                            "models": ["fixture-model"],
                        }
                    ],
                    "preferences": {
                        "thinkingLevel": "off",
                        "quietMode": True,
                    },
                    "storage": {"backend": "libsql", "libsql": {}, "pg": {}},
                    "browser": {"enabled": False},
                    "signals": {"unixSocketPubSub": False},
                    "mcp": {"claudeCodeGlobal": False, "codexGlobal": False},
                    "observability": {"resources": {}, "localTracing": False},
                }
            )
        )
        settings.chmod(0o600)
        completed = subprocess.run(
            [
                str(binary),
                "--settings",
                str(settings),
                "--prompt",
                FOLLOWUP,
                "--output",
                "json",
                "--model",
                "mastracode/session-migrate-loopback/fixture-model",
                "--resource-id",
                RESOURCE_ID,
                "--thread",
                SESSION_ID,
                "--permission-mode",
                "deny",
                "--thinking-level",
                "off",
                "--max-turns",
                "2",
                "--timeout",
                "45",
            ],
            cwd=work,
            env=_environment(home, app_data, temporary),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    finally:
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=5)

    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    # 0.37.1 writes terminal-mode reset escapes after its JSON object even in
    # non-interactive mode; decode the first JSON value and inspect the tail.
    result, end = json.JSONDecoder().raw_decode(completed.stdout)
    assert (
        completed.stdout[end:]
        .strip()
        .replace("\x1b[?2004l", "")
        .replace("\x1b[<u", "")
        .replace("\x1b[>4;0m", "")
        .replace("\x1b[?25h", "")
        == ""
    )
    assert result["status"] in {"completed", "done"}
    assert REPLY in json.dumps(result)
    assert provider.requests
    replayed = json.dumps(provider.requests[-1])
    for marker in (IMPORTED_USER, IMPORTED_ASSISTANT, IMPORTED_TOOL, IMPORTED_SUMMARY, FOLLOWUP):
        assert marker in replayed

    after = mastracode.parse_session(target, SESSION_ID)
    assert after.session_id == before.session_id == SESSION_ID
    assert after.raw_record_count > before.raw_record_count
    assert any(event.kind == EventKind.MESSAGE and event.text == FOLLOWUP for event in after.events)
    assert any(event.kind == EventKind.MESSAGE and event.text == REPLY for event in after.events)
    with sqlite3.connect(target) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"mastra_threads", "mastra_messages", "mastra_observational_memory"} <= tables
