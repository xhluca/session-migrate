"""Opt-in pinned-source native trajectory for Hermes Agent 0.20.6."""

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from session_migrate.errors import SessionMigrateError
from session_migrate.formats import hermes
from session_migrate.model import AgentFormat, Event, EventKind, Provenance, Role, Session

SESSION_ID = "20260830_123456_111111"
FOLLOWUP = "HERMES_NATIVE_RESUME_FOLLOWUP_ZETA"
REPLY = "HERMES_NATIVE_RESUME_REPLY_OMEGA"


class Provider(ThreadingHTTPServer):
    requests: list[dict[str, Any]]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def do_GET(self) -> None:  # noqa: N802
        self._model_response()

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        value = json.loads(self.rfile.read(length))
        assert isinstance(value, dict)
        messages = value.get("messages")
        if not isinstance(messages, list):
            self._model_response()
            return
        self.server.requests.append(value)  # type: ignore[attr-defined]
        chunks = [
            {
                "id": "hermes-native-resume",
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
                "id": "hermes-native-resume",
                "object": "chat.completion.chunk",
                "created": 1_788_093_400,
                "model": "fixture-model",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
        ]
        body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        encoded = (body + "data: [DONE]\n\n").encode()
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _model_response(self) -> None:
        encoded = json.dumps(
            {
                "id": "fixture-model",
                "object": "model",
                "created": 1_788_093_400,
                "owned_by": "session-migrate-loopback",
            }
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def _source(tmp_path: Path) -> Session:
    return Session(
        source_format=AgentFormat.CLAUDE,
        source_path=tmp_path / "portable-source.jsonl",
        source_sha256="0" * 64,
        session_id="11111111-1111-4111-8111-111111111111",
        cwd=tmp_path,
        started_at="2026-08-30T12:34:56Z",
        cli_version="2.1.209",
        model="fixture-model",
        title="Hermes native resume fixture",
        events=(
            Event(
                EventKind.MESSAGE,
                Provenance(0, "user"),
                role=Role.USER,
                timestamp="2026-08-30T12:34:56Z",
                text="HERMES_IMPORTED_USER_ALPHA",
            ),
            Event(
                EventKind.TOOL_CALL,
                Provenance(1, "assistant"),
                role=Role.ASSISTANT,
                timestamp="2026-08-30T12:34:57Z",
                tool_name="terminal",
                tool_call_id="call_hermes_native",
                payload={"input": {"command": "printf HERMES_IMPORTED_TOOL_GAMMA"}},
            ),
            Event(
                EventKind.TOOL_RESULT,
                Provenance(2, "tool"),
                role=Role.TOOL,
                timestamp="2026-08-30T12:34:58Z",
                text="HERMES_IMPORTED_TOOL_GAMMA",
                tool_name="terminal",
                tool_call_id="call_hermes_native",
                payload={
                    "is_error": False,
                    "content_blocks": [{"type": "text", "text": "HERMES_IMPORTED_TOOL_GAMMA"}],
                },
            ),
            Event(
                EventKind.COMPACTION,
                Provenance(3, "compaction"),
                role=Role.SYSTEM,
                timestamp="2026-08-30T12:34:59Z",
                text="HERMES_IMPORTED_SUMMARY_DELTA",
            ),
            Event(
                EventKind.MESSAGE,
                Provenance(4, "user"),
                role=Role.USER,
                timestamp="2026-08-30T12:35:00Z",
                text="HERMES_IMPORTED_POST_SUMMARY_EPSILON",
            ),
        ),
        raw_record_count=5,
        model_provider="loopback",
    )


def _environment(home: Path, source: Path) -> dict[str, str]:
    environment = {
        "HOME": str(home),
        "HERMES_HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "TERM": "dumb",
        "NO_COLOR": "1",
        "HERMES_NO_UPDATE_CHECK": "1",
        "SESSION_MIGRATE_HERMES_SOURCE": str(source),
    }
    for key in ("LANG", "LC_ALL", "LC_CTYPE", "TMPDIR"):
        if os.environ.get(key):
            environment[key] = os.environ[key]
    return environment


def test_hermes_0206_import_resume_replay_append_and_compaction(tmp_path: Path) -> None:
    source_value = os.environ.get("SESSION_MIGRATE_HERMES_SOURCE")
    if not source_value:
        pytest.skip("set SESSION_MIGRATE_HERMES_SOURCE to the exact v2026.8.27 source checkout")
    source = Path(source_value).resolve()
    binary_value = os.environ.get("SESSION_MIGRATE_HERMES_BIN")
    binary = Path(binary_value).resolve() if binary_value else source / ".venv/bin/hermes"
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source,
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    assert revision.stdout.strip() == hermes.PINNED_HERMES_SOURCE_COMMIT

    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir(mode=0o700)
    work.mkdir(mode=0o700)
    environment = _environment(home, source)
    provider = Provider(("127.0.0.1", 0), Handler)
    provider.requests = []
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    try:
        (home / "config.yaml").write_text(
            "\n".join(
                [
                    "model:",
                    "  provider: loopback",
                    "  default: fixture-model",
                    "providers:",
                    "  loopback:",
                    f"    api: http://127.0.0.1:{provider.server_address[1]}/v1",
                    "    api_key: local-test-key",
                    "    transport: chat_completions",
                    "    default_model: fixture-model",
                    "    models: [fixture-model]",
                    "    discover_models: false",
                    "    context_length: 32768",
                    "display:",
                    "  interface: cli",
                    "  resume_display: minimal",
                    "compression:",
                    "  enabled: false",
                    "agent:",
                    "  max_turns: 3",
                    "",
                ]
            )
        )
        (home / "config.yaml").chmod(0o600)
        data, dropped = hermes.serialize(
            _source(work),
            session_id=SESSION_ID,
            cwd=work,
            timestamp="2026-08-30T12:34:56Z",
        )
        assert dropped == {}
        installed = hermes.install_bundle(
            data,
            session_id=SESSION_ID,
            target_home=home,
            target_cli=binary,
            environ=environment,
        )
        assert installed.path == home / "state.db"
        before = hermes.parse_session(installed.path, SESSION_ID)
        assert before.title == "Hermes native resume fixture"
        assert before.cwd == work
        assert before.event_counts() == {
            "compaction": 1,
            "message": 2,
            "tool_call": 1,
            "tool_result": 1,
        }

        before_collision = hermes.database_snapshot(installed.path)
        with pytest.raises(SessionMigrateError, match="already exists"):
            hermes.install_bundle(
                data,
                session_id=SESSION_ID,
                target_home=home,
                target_cli=binary,
                environ=environment,
            )
        assert hermes.database_snapshot(installed.path).sha256 == before_collision.sha256

        malformed_id = "20260830_123457_badbad"
        interpreter = hermes._script_interpreter(binary)
        malformed = subprocess.run(
            [
                str(interpreter),
                "-c",
                (
                    "import json,sys; from pathlib import Path; "
                    "from hermes_state import SessionDB; "
                    "db=SessionDB(db_path=Path(sys.argv[1])); "
                    "print(json.dumps(db.import_sessions(json.load(sys.stdin)))); "
                    "db.close()"
                ),
                str(installed.path),
            ],
            env=environment,
            input=json.dumps([{"id": malformed_id, "messages": [{"role": ""}]}]),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert malformed.returncode == 0, (malformed.stdout, malformed.stderr)
        malformed_result = json.loads(malformed.stdout.splitlines()[-1])
        assert malformed_result["ok"] is False
        assert malformed_result["imported"] == 0
        assert "role must be a non-empty string" in malformed_result["errors"][0]["error"]
        assert malformed_id not in {
            item.session_id for item in hermes.list_sessions(installed.path)
        }

        resumed = subprocess.run(
            [
                str(binary),
                "chat",
                "--quiet",
                "--provider",
                "loopback",
                "--model",
                "fixture-model",
                "--toolsets",
                "terminal",
                "--resume",
                SESSION_ID,
                "--in",
                str(work),
                "--max-turns",
                "3",
                "--query",
                FOLLOWUP,
            ],
            cwd=source,
            env=environment,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
        assert REPLY in resumed.stdout
        assert provider.requests
        request_text = json.dumps(provider.requests[-1], ensure_ascii=False)
        for marker in (
            "HERMES_IMPORTED_USER_ALPHA",
            "call_hermes_native",
            "HERMES_IMPORTED_TOOL_GAMMA",
            "HERMES_IMPORTED_SUMMARY_DELTA",
            "HERMES_IMPORTED_POST_SUMMARY_EPSILON",
            FOLLOWUP,
        ):
            assert marker in request_text

        appended = hermes.parse_session(installed.path, SESSION_ID)
        messages = [
            event.text
            for event in appended.events
            if event.kind == EventKind.MESSAGE and event.text
        ]
        assert messages[-2:] == [FOLLOWUP, REPLY]

        compacted_messages = [
            {
                "role": "user",
                "content": "[CONTEXT SUMMARY]:\nHERMES_NATIVE_COMPACTION_SIGMA",
                "_compressed_summary": True,
                "timestamp": 1_788_093_500.0,
            },
            {"role": "user", "content": FOLLOWUP, "timestamp": 1_788_093_501.0},
            {"role": "assistant", "content": REPLY, "timestamp": 1_788_093_502.0},
        ]
        compact = subprocess.run(
            [
                str(interpreter),
                "-c",
                (
                    "import json,sys; from pathlib import Path; "
                    "from hermes_state import SessionDB; "
                    "db=SessionDB(db_path=Path(sys.argv[1])); "
                    "print(db.archive_and_compact(sys.argv[2],json.load(sys.stdin))); "
                    "db.close()"
                ),
                str(installed.path),
                SESSION_ID,
            ],
            env=environment,
            input=json.dumps(compacted_messages),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert compact.returncode == 0, (compact.stdout, compact.stderr)
        compacted = hermes.parse_session(installed.path, SESSION_ID)
        assert any(
            event.kind == EventKind.COMPACTION and event.text == "HERMES_NATIVE_COMPACTION_SIGMA"
            for event in compacted.events
        )
        assert any(
            event.kind == EventKind.OPAQUE
            and event.payload.get("reason") == "hermes_compacted_history"
            for event in compacted.events
        )
    finally:
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=5)
