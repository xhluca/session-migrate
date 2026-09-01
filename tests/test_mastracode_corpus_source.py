from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from hashlib import file_digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from native_corpus.loader import load_standalone_fixture
from native_corpus.route_oracle import assert_source_expectations, parse_native_fixture

from session_migrate.formats import mastracode
from session_migrate.model import EventKind, Role

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/native_corpus/v1/sources/mastracode/0.37.1/portable-rich"
SCRIPT = ROOT / "scripts/native-corpus/sanitize-mastracode.py"
SESSION_ID = "8987a893-12cb-47e0-acdc-2d45a1c920f0"
RESOURCE_ID = "work-d4a79df42af4"
FOLLOWUP = "MASTRACODE_CORPUS_COLD_RELOAD_FOLLOWUP_7319"
REPLY = "MASTRACODE_CORPUS_COLD_RELOAD_REPLY_7319"


def load_sanitizer() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sanitize_mastracode_corpus", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_mastracode_sanitizer_is_idempotent_on_reviewed_public_rows(
    tmp_path: Path,
) -> None:
    sanitizer = load_sanitizer()
    source = FIXTURE / "native/mastra.db"

    result = sanitizer.sanitize_database(
        source,
        session_id=SESSION_ID,
        source_root=Path("/fixture"),
    )
    output = tmp_path / "mastra.db"
    sanitizer.write_database(result, output)

    assert result.mutations == {}
    parsed = mastracode.parse_session(output, SESSION_ID)
    assert parsed.cwd == Path("/fixture/work")
    assert parsed.event_counts() == {
        "context": 1,
        "message": 8,
        "opaque": 5,
        "tool_call": 2,
        "tool_result": 2,
    }


def test_mastracode_sanitizer_preserves_png_and_rejects_unknown_part(
    tmp_path: Path,
) -> None:
    sanitizer = load_sanitizer()
    source = FIXTURE / "native/mastra.db"
    with sqlite3.connect(source) as db:
        content = json.loads(
            db.execute(
                'SELECT content FROM "mastra_messages" WHERE thread_id=? '
                "ORDER BY createdAt,id LIMIT 1",
                (SESSION_ID,),
            ).fetchone()[0]
        )
    image = content["parts"][1]
    assert image["type"] == "file"
    assert image["mimeType"] == "image/png"
    assert not image["data"].startswith("data:")

    malformed = tmp_path / "malformed.db"
    shutil.copyfile(source, malformed)
    with sqlite3.connect(malformed) as db:
        row = db.execute(
            'SELECT id,content FROM "mastra_messages" WHERE thread_id=? '
            "ORDER BY createdAt,id LIMIT 1",
            (SESSION_ID,),
        ).fetchone()
        document = json.loads(row[1])
        document["parts"][1]["type"] = "hologram"
        db.execute(
            'UPDATE "mastra_messages" SET content=? WHERE id=?',
            (json.dumps(document), row[0]),
        )
        db.commit()
    with pytest.raises(sanitizer.SanitizationError, match="unsupported: hologram"):
        sanitizer.sanitize_database(
            malformed,
            session_id=SESSION_ID,
            source_root=Path("/fixture"),
        )


def test_mastracode_promoted_fixture_matches_reviewed_ir_and_media_evidence(
    tmp_path: Path,
) -> None:
    fixture = load_standalone_fixture(FIXTURE)
    session = parse_native_fixture(fixture, tmp_path / "materialized")

    assert_source_expectations(fixture, session)
    assert any(
        event.kind == EventKind.TOOL_RESULT and event.payload.get("is_error") is True
        for event in session.events
    )
    modalities = fixture.provenance.modalities
    assert modalities["user_image"].native_accepted is True
    assert modalities["user_image"].fixture_present is True
    for name in ("document", "audio", "video"):
        assert modalities[name].attempted is True
        assert modalities[name].native_accepted is False


class Provider(ThreadingHTTPServer):
    requests: list[dict[str, Any]]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def do_GET(self) -> None:  # noqa: N802
        encoded = json.dumps(
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
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        request = json.loads(self.rfile.read(length))
        assert isinstance(request, dict)
        self.server.requests.append(request)  # type: ignore[attr-defined]
        chunks = [
            {
                "id": "mastracode-corpus-reload",
                "object": "chat.completion.chunk",
                "created": 1788210000,
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
                "id": "mastracode-corpus-reload",
                "object": "chat.completion.chunk",
                "created": 1788210000,
                "model": "fixture-model",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 6, "total_tokens": 46},
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


def test_mastracode_fixture_cold_reloads_and_continues_in_exact_client(
    tmp_path: Path,
) -> None:
    binary_value = os.environ.get("SESSION_MIGRATE_MASTRACODE_BIN")
    if not binary_value:
        pytest.skip("set SESSION_MIGRATE_MASTRACODE_BIN to mastracode@0.37.1")
    binary = Path(binary_value).resolve()
    assert binary.stat().st_size == mastracode.PINNED_MASTRACODE_CLI_JS_BYTES
    with binary.open("rb") as stream:
        assert file_digest(stream, "sha256").hexdigest() == (
            mastracode.PINNED_MASTRACODE_CLI_JS_SHA256
        )

    home = tmp_path / "home"
    app_data = tmp_path / "app-data"
    temporary = tmp_path / "tmp"
    work = tmp_path / "work"
    for path in (home, app_data, temporary, work):
        path.mkdir(mode=0o700)
    target = app_data / "mastra.db"
    shutil.copyfile(FIXTURE / "native/mastra.db", target)
    provider = Provider(("127.0.0.1", 0), Handler)
    provider.requests = []
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    try:
        settings = app_data / "settings.json"
        settings.write_text(
            json.dumps(
                {
                    "onboarding": {
                        "skippedAt": "2026-08-31T00:00:00Z",
                        "version": 1,
                    },
                    "customProviders": [
                        {
                            "name": "Session Migrate Loopback",
                            "url": f"http://127.0.0.1:{provider.server_address[1]}/v1",
                            "apiKey": "credential-free-loopback",
                            "models": ["fixture-model"],
                        }
                    ],
                    "preferences": {"thinkingLevel": "off", "quietMode": True},
                    "storage": {"backend": "libsql", "libsql": {}, "pg": {}},
                    "browser": {"enabled": False},
                    "signals": {"unixSocketPubSub": False},
                    "mcp": {"claudeCodeGlobal": False, "codexGlobal": False},
                    "observability": {"resources": {}, "localTracing": False},
                }
            )
        )
        env = {
            "HOME": str(home),
            "XDG_DATA_HOME": str(home / ".local/share"),
            "MASTRA_APP_DATA_DIR": str(app_data),
            "TMPDIR": str(temporary),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "TERM": "dumb",
            "NO_COLOR": "1",
            "DO_NOT_TRACK": "1",
        }
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
            env=env,
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
    assert REPLY in completed.stdout
    replay = json.dumps(provider.requests[-1])
    for marker in (
        "SM_CORPUS_7319",
        "COPPER_4821",
        "call_mastracode_missing",
        "File not found: missing-corpus-file.txt",
        FOLLOWUP,
    ):
        assert marker in replay
    reloaded = mastracode.parse_session(target, SESSION_ID)
    messages = [event for event in reloaded.events if event.kind == EventKind.MESSAGE]
    followup_index = next(
        index
        for index, event in enumerate(messages)
        if event.role == Role.USER and event.text == FOLLOWUP
    )
    reply_index = next(
        index
        for index, event in enumerate(messages)
        if event.role == Role.ASSISTANT and event.text == REPLY
    )
    # MastraCode inserts a native temporal-gap reminder between an old imported
    # prefix and the new turn. Preserve it and assert ordering instead of
    # pretending the user/assistant rows must be adjacent.
    assert followup_index < reply_index
