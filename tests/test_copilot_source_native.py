"""Opt-in credential-free checks against the exact GitHub Copilot CLI binary."""

import base64
import hashlib
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from session_migrate.formats import copilot
from session_migrate.model import EventKind, Role

SOURCE_ID = "66666666-6666-4666-8666-666666666666"
REWRITE_ID = "77777777-7777-4777-8777-777777777777"
CORPUS_ID = "89898989-8989-4989-8989-898989898989"
PROMPT = "SYNTHETIC_COPILOT_SOURCE_NATIVE_USER"
TOOL_RESULT = "SYNTHETIC_COPILOT_SOURCE_NATIVE_TOOL_RESULT"
ASSISTANT = "SYNTHETIC_COPILOT_SOURCE_NATIVE_ASSISTANT"
FOLLOWUP = "SYNTHETIC_COPILOT_SOURCE_NATIVE_FOLLOWUP"
FOLLOWUP_REPLY = "SYNTHETIC_COPILOT_SOURCE_NATIVE_FOLLOWUP_REPLY"
CORPUS_PROMPT = (
    "Remember SM_CORPUS_7319, inspect fixture.txt with the native file tool, "
    "and describe the attached image briefly. Do not edit files."
)


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
        wire = json.dumps(value, ensure_ascii=False)
        if FOLLOWUP in wire:
            chunks = _text_chunks(FOLLOWUP_REPLY, "followup")
        elif TOOL_RESULT in wire:
            chunks = _text_chunks(ASSISTANT, "assistant")
        else:
            chunks = _tool_chunks()
        payload = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        encoded = (payload + "data: [DONE]\n\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def _text_chunks(text: str, suffix: str) -> list[dict[str, Any]]:
    return [
        {
            "id": f"copilot-source-{suffix}",
            "object": "chat.completion.chunk",
            "created": 1787215928,
            "model": "fixture-model",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": text},
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": f"copilot-source-{suffix}",
            "object": "chat.completion.chunk",
            "created": 1787215928,
            "model": "fixture-model",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
        },
    ]


def _tool_chunks() -> list[dict[str, Any]]:
    return [
        {
            "id": "copilot-source-tool",
            "object": "chat.completion.chunk",
            "created": 1787215928,
            "model": "fixture-model",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_copilot_source_native",
                                "type": "function",
                                "function": {
                                    "name": "view",
                                    "arguments": '{"path":"fixture.txt"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "copilot-source-tool",
            "object": "chat.completion.chunk",
            "created": 1787215928,
            "model": "fixture-model",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
        },
    ]


def _environment(home: Path, copilot_home: Path, temporary: Path, port: int) -> dict[str, str]:
    environment = {
        "HOME": str(home),
        "COPILOT_HOME": str(copilot_home),
        "TMPDIR": str(temporary),
        "PATH": "/usr/bin:/bin",
        "TERM": "dumb",
        "NO_COLOR": "1",
        "COPILOT_PROVIDER_BASE_URL": f"http://127.0.0.1:{port}/v1",
        "COPILOT_PROVIDER_TYPE": "openai",
        "COPILOT_PROVIDER_WIRE_API": "completions",
        "COPILOT_PROVIDER_MODEL_ID": "gpt-4.1",
        "COPILOT_PROVIDER_WIRE_MODEL": "fixture-model",
        "COPILOT_PROVIDER_MAX_PROMPT_TOKENS": "1000000",
        "COPILOT_PROVIDER_MAX_OUTPUT_TOKENS": "4096",
        "COPILOT_OFFLINE": "true",
    }
    for key in ("LANG", "LC_ALL", "LC_CTYPE"):
        if os.environ.get(key):
            environment[key] = os.environ[key]
    return environment


def _run(
    binary: Path,
    environment: dict[str, str],
    work: Path,
    *arguments: str,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [
            str(binary),
            "--no-auto-update",
            "--no-remote",
            "--no-remote-export",
            "--disable-builtin-mcps",
            "--no-custom-instructions",
            "-C",
            str(work),
            *arguments,
            "--allow-all-tools",
            "--silent",
        ],
        cwd=work,
        env=environment,
        check=False,
        capture_output=True,
        timeout=90,
    )


def test_exact_copilot_source_native_trajectory_and_cold_rewrite(tmp_path: Path) -> None:
    binary_value = os.environ.get("SESSION_MIGRATE_COPILOT_BIN")
    if not binary_value:
        pytest.skip("set SESSION_MIGRATE_COPILOT_BIN to exact Copilot 1.0.70 binary")
    binary = Path(binary_value)
    home = tmp_path / "home"
    copilot_home = tmp_path / "copilot"
    work = tmp_path / "work"
    temporary = tmp_path / "tmp"
    for directory in (home, copilot_home, work, temporary):
        directory.mkdir(mode=0o700)
    (work / "fixture.txt").write_text(f"{TOOL_RESULT}\n")
    provider = Provider(("127.0.0.1", 0), Handler)
    provider.requests = []
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    environment = _environment(home, copilot_home, temporary, provider.server_address[1])
    try:
        version = subprocess.run(
            [str(binary), "--no-auto-update", "--version"],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert version.returncode == 0
        assert copilot.PINNED_COPILOT_VERSION in version.stdout

        created = _run(binary, environment, work, "--session-id", SOURCE_ID, "-p", PROMPT)
        assert created.returncode == 0
        assert ASSISTANT.encode() in created.stdout
        source_path = copilot_home / copilot.session_relative_path(SOURCE_ID)
        source = copilot.parse_session(source_path)
        assert any(
            event.kind == EventKind.MESSAGE and event.role == Role.USER and event.text == PROMPT
            for event in source.events
        )
        assert any(event.kind == EventKind.TOOL_CALL for event in source.events)
        assert any(
            event.kind == EventKind.TOOL_RESULT and TOOL_RESULT in (event.text or "")
            for event in source.events
        )
        assert any(event.text == ASSISTANT for event in source.events)

        native, _ = copilot.serialize(
            source,
            session_id=REWRITE_ID,
            cwd=work,
            timestamp="2026-08-20T09:00:00Z",
        )
        destination = copilot_home / copilot.session_relative_path(REWRITE_ID)
        destination.parent.mkdir(mode=0o700)
        destination.write_bytes(native)
        (destination.parent / "workspace.yaml").write_bytes(
            copilot.workspace_bytes(
                session_id=REWRITE_ID,
                cwd=work,
                timestamp="2026-08-20T09:00:00Z",
                title="SYNTHETIC_COPILOT_SOURCE_REWRITE",
            )
        )
        resumed = _run(binary, environment, work, f"--resume={REWRITE_ID}", "-p", FOLLOWUP)
        assert resumed.returncode == 0
        assert FOLLOWUP_REPLY.encode() in resumed.stdout
        after = destination.read_bytes()
        assert len(after) > len(native)
        assert after.startswith(native)
        reparsed = copilot.parse_session(destination)
        assert any(event.text == FOLLOWUP for event in reparsed.events)
        assert any(event.text == FOLLOWUP_REPLY for event in reparsed.events)
        assert len(provider.requests) == 3
        final_wire = json.dumps(provider.requests[-1], ensure_ascii=False)
        for marker in (PROMPT, TOOL_RESULT, ASSISTANT, FOLLOWUP):
            assert marker in final_wire
    finally:
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=5)


def test_exact_copilot_creates_named_multimodal_native_source(tmp_path: Path) -> None:
    """Create a source through Copilot itself, including its public image input."""

    binary_value = os.environ.get("SESSION_MIGRATE_COPILOT_BIN")
    if not binary_value:
        pytest.skip("set SESSION_MIGRATE_COPILOT_BIN to exact Copilot 1.0.70 binary")
    binary = Path(binary_value)
    home = tmp_path / "home"
    copilot_home = tmp_path / "copilot"
    work = tmp_path / "work"
    temporary = tmp_path / "tmp"
    for directory in (home, copilot_home, work, temporary):
        directory.mkdir(mode=0o700)
    (work / "fixture.txt").write_text(f"{TOOL_RESULT}\nCOPPER_4821\n")
    image_path = Path(__file__).parent / "native_corpus/v1/assets/corpus-card.png"
    image_digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
    provider = Provider(("127.0.0.1", 0), Handler)
    provider.requests = []
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    environment = _environment(home, copilot_home, temporary, provider.server_address[1])
    try:
        created = _run(
            binary,
            environment,
            work,
            "--session-id",
            CORPUS_ID,
            "--name",
            "repair-event-window-boundary",
            "--attachment",
            str(image_path),
            "-p",
            CORPUS_PROMPT,
        )
        assert created.returncode == 0, created.stderr.decode(errors="replace")
        source_path = copilot_home / copilot.session_relative_path(CORPUS_ID)
        source = copilot.parse_session(source_path)

        assert source.session_id == CORPUS_ID
        assert source.title == "repair-event-window-boundary"
        assert any(
            event.kind == EventKind.MESSAGE
            and event.role == Role.USER
            and event.text == CORPUS_PROMPT
            for event in source.events
        )
        image = next(
            event
            for event in source.events
            if event.kind == EventKind.CONTEXT
            and event.role == Role.USER
            and event.payload.get("block_type") == "image"
        )
        image_url = image.payload["image_url"]
        assert isinstance(image_url, str) and image_url.startswith("data:image/png;base64,")
        assert hashlib.sha256(base64.b64decode(image_url.partition(",")[2])).hexdigest() == (
            image_digest
        )
        assert any(event.kind == EventKind.TOOL_CALL for event in source.events)
        assert any(
            event.kind == EventKind.TOOL_RESULT and "COPPER_4821" in (event.text or "")
            for event in source.events
        )
        assert any(event.text == ASSISTANT for event in source.events)
        assert len(provider.requests) == 2
        provider_wire = json.dumps(provider.requests, sort_keys=True)
        assert CORPUS_PROMPT in provider_wire
        assert "COPPER_4821" in provider_wire
    finally:
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=5)
