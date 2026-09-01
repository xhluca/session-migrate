from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from native_corpus.loader import load_standalone_fixture
from native_corpus.route_oracle import assert_source_expectations, parse_native_fixture

from session_migrate.formats import hermes
from session_migrate.model import EventKind

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/native_corpus/v1/sources/hermes/0.20.6/portable-rich"
SCRIPT = ROOT / "scripts/native-corpus/sanitize-hermes.py"
SESSION_ID = "20260831_195650_1783a3"
FOLLOWUP = "HERMES_CORPUS_COLD_RELOAD_FOLLOWUP_7319"
REPLY = "HERMES_CORPUS_COLD_RELOAD_REPLY_7319"


def load_sanitizer() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sanitize_hermes_corpus", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _message(module: ModuleType, **updates: Any) -> dict[str, Any]:
    value = dict.fromkeys(module.MESSAGE_FIELDS)
    value.update(
        {
            "active": 1,
            "compacted": 0,
            "observed": False,
            "timestamp": 1788210000.0,
        }
    )
    value.update(updates)
    return value


def private_export(module: ModuleType, private: Path) -> dict[str, Any]:
    session_id = "20260831_123456_fixture"
    call_id = "call_fixture"
    messages = [
        _message(
            module,
            id=1,
            session_id=session_id,
            role="user",
            content=f"Read {private}/timeline.py",
        ),
        _message(
            module,
            id=2,
            session_id=session_id,
            role="assistant",
            content=None,
            tool_calls=[
                {
                    "id": call_id,
                    "call_id": call_id,
                    "response_item_id": "fc_fixture",
                    "type": "function",
                    "function": {
                        "name": "terminal",
                        "arguments": json.dumps({"command": f"cat {private}/timeline.py"}),
                    },
                }
            ],
        ),
        _message(
            module,
            id=3,
            session_id=session_id,
            role="tool",
            content='{"output":"ok","exit_code":0,"error":null}',
            tool_call_id=call_id,
            tool_name="terminal",
        ),
    ]
    value = dict.fromkeys(module.SESSION_FIELDS)
    value.update(
        {
            "id": session_id,
            "source": "cli",
            "started_at": 1788210000.0,
            "ended_at": 1788210001.0,
            "message_count": len(messages),
            "tool_call_count": 1,
            "input_tokens": 1,
            "output_tokens": 1,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
            "api_call_count": 1,
            "title": "fixture",
            "messages": messages,
            "system_prompt": "private runtime prompt",
            "system_prompt_hash": "f" * 64,
        }
    )
    return value


def test_hermes_sanitizer_is_schema_aware_and_removes_runtime_ownership(
    tmp_path: Path,
) -> None:
    sanitizer = load_sanitizer()
    private = tmp_path / "private/work"
    document = private_export(sanitizer, private)

    result = sanitizer.sanitize_document(document, source_cwd=private)

    wire = json.dumps(result.document)
    assert str(private) not in wire
    assert "/fixture/work/timeline.py" in wire
    assert result.document["cwd"] == "/fixture/work"
    assert result.document["system_prompt"] is None
    assert result.document["system_prompt_hash"] is None
    assert result.mutations == {
        "message path": 2,
        "session cwd": 1,
        "session system_prompt": 1,
        "session system_prompt_hash": 1,
    }


def test_hermes_sanitizer_rejects_unknown_schema_and_broken_tool_linkage(
    tmp_path: Path,
) -> None:
    sanitizer = load_sanitizer()
    document = private_export(sanitizer, tmp_path / "private/work")
    document["unknown_native_field"] = True
    with pytest.raises(sanitizer.SanitizationError, match="fields changed"):
        sanitizer.sanitize_document(document, source_cwd=tmp_path)

    document.pop("unknown_native_field")
    document["messages"][2]["tool_call_id"] = "missing_call"
    with pytest.raises(sanitizer.SanitizationError, match="tool result linkage"):
        sanitizer.sanitize_document(document, source_cwd=tmp_path)


def test_hermes_promoted_fixture_matches_reviewed_ir_and_media_evidence(
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
    assert modalities["user_image"].fixture_present is False
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
                "id": "fixture-model",
                "object": "model",
                "created": 1788210000,
                "owned_by": "session-migrate-loopback",
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
        if not isinstance(request, dict) or not isinstance(request.get("messages"), list):
            self.do_GET()
            return
        self.server.requests.append(request)  # type: ignore[attr-defined]
        chunks = [
            {
                "id": "hermes-corpus-reload",
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
                "id": "hermes-corpus-reload",
                "object": "chat.completion.chunk",
                "created": 1788210000,
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


def test_hermes_fixture_cold_reloads_and_continues_in_exact_client(tmp_path: Path) -> None:
    source_value = os.environ.get("SESSION_MIGRATE_HERMES_SOURCE")
    if not source_value:
        pytest.skip("set SESSION_MIGRATE_HERMES_SOURCE to exact v2026.8.27 source")
    source = Path(source_value).resolve()
    binary = Path(
        os.environ.get("SESSION_MIGRATE_HERMES_BIN", str(source / ".venv/bin/hermes"))
    ).resolve()
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert revision.stdout.strip() == hermes.PINNED_HERMES_SOURCE_COMMIT

    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir(mode=0o700)
    work.mkdir(mode=0o700)
    shutil.copyfile(FIXTURE / "native/state.db", home / "state.db")
    shutil.copyfile(ROOT / "tests/native_corpus/v1/assets/timeline.py", work / "timeline.py")
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
                    "    api_key: credential-free-loopback",
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
        env = {
            "HOME": str(home),
            "HERMES_HOME": str(home),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "TERM": "dumb",
            "NO_COLOR": "1",
            "HERMES_NO_UPDATE_CHECK": "1",
            "SESSION_MIGRATE_HERMES_SOURCE": str(source),
        }
        completed = subprocess.run(
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
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    finally:
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=5)

    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    assert REPLY in completed.stdout
    replay = json.dumps(provider.requests[-1])
    for marker in ("SM_CORPUS_7319", "COPPER_4821", "call_hermes_missing", FOLLOWUP):
        assert marker in replay
    reloaded = hermes.parse_session(home / "state.db", SESSION_ID)
    assert [
        event.text for event in reloaded.events if event.kind == EventKind.MESSAGE and event.text
    ][-2:] == [FOLLOWUP, REPLY]
