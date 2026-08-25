"""Opt-in exact-binary native trajectory for Oh My Pi 18.0.5."""

import json
import os
import selectors
import subprocess
import threading
import time
from collections.abc import Callable
from hashlib import file_digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from session_migrate.conversion import ConversionOptions, convert_session
from session_migrate.formats import claude, omp
from session_migrate.model import EventKind, Role, TargetFormat

FIXTURE = Path(__file__).parent / "fixtures/claude-2.1.209/basic.jsonl"
SESSION_ID = "19191919-1919-4919-8919-191919191919"
FOLLOWUP = "OMP_NATIVE_FOLLOWUP_GAMMA"
REPLY = "OMP_NATIVE_REPLY_OMEGA"


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
                "id": "omp-native-fixture",
                "object": "chat.completion.chunk",
                "created": 1787673600,
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
                "id": "omp-native-fixture",
                "object": "chat.completion.chunk",
                "created": 1787673600,
                "model": "fixture-model",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 24},
            },
        ]
        body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        encoded = (body + "data: [DONE]\n\n").encode()
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def _environment(home: Path, agent_home: Path, temporary: Path) -> dict[str, str]:
    environment = {
        "HOME": str(home),
        "PI_CODING_AGENT_DIR": str(agent_home),
        "TMPDIR": str(temporary),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "TERM": "dumb",
        "NO_COLOR": "1",
    }
    for key in ("LANG", "LC_ALL", "LC_CTYPE"):
        if os.environ.get(key):
            environment[key] = os.environ[key]
    return environment


def _response(stdout: str, command: str) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(value, dict)
            and value.get("type") == "response"
            and value.get("command") == command
        ):
            matches.append(value)
    assert matches
    return matches[-1]


def _read_until(
    process: subprocess.Popen[str],
    output: list[str],
    predicate: Callable[[dict[str, Any]], bool],
    *,
    timeout: float = 30,
) -> dict[str, Any]:
    selector = selectors.DefaultSelector()
    assert process.stdout is not None
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            ready = selector.select(max(0, deadline - time.monotonic()))
            if not ready:
                break
            line = process.stdout.readline()
            if not line:
                break
            output.append(line)
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and predicate(value):
                return value
    finally:
        selector.close()
    raise AssertionError("OMP RPC trajectory timed out before the expected event")


def test_omp_1805_native_load_model_replay_append_and_rename(tmp_path: Path) -> None:
    binary_value = os.environ.get("SESSION_MIGRATE_OMP_BIN")
    if not binary_value:
        pytest.skip("set SESSION_MIGRATE_OMP_BIN to the exact OMP 18.0.5 binary")
    binary = Path(binary_value)
    assert binary.stat().st_size == omp.PINNED_OMP_LINUX_X64_BYTES
    with binary.open("rb") as stream:
        assert file_digest(stream, "sha256").hexdigest() == omp.PINNED_OMP_LINUX_X64_SHA256
    home = tmp_path / "home"
    agent_home = tmp_path / "agent"
    temporary = tmp_path / "tmp"
    work = tmp_path / "work"
    for directory in (home, agent_home, temporary, work):
        directory.mkdir(mode=0o700)
    environment = _environment(home, agent_home, temporary)

    version = subprocess.run(
        [str(binary), "--version"],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert version.returncode == 0
    assert version.stdout.strip() == f"omp/{omp.PINNED_OMP_VERSION}"

    source = claude.parse(FIXTURE)
    artifact = convert_session(
        source,
        ConversionOptions(
            target_format=TargetFormat.OMP,
            session_id=SESSION_ID,
            cwd=work,
        ),
    )
    session_path = agent_home / omp.session_relative_path(work, SESSION_ID, artifact.timestamp)
    session_path.parent.mkdir(parents=True)
    session_path.write_bytes(artifact.native_bytes)
    before = session_path.read_bytes()

    provider = Provider(("127.0.0.1", 0), Handler)
    provider.requests = []
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    try:
        models = agent_home / "models.yml"
        models.write_text(
            "\n".join(
                [
                    "providers:",
                    "  session-migrate-loopback:",
                    f"    baseUrl: http://127.0.0.1:{provider.server_address[1]}/v1",
                    "    api: openai-completions",
                    "    auth: none",
                    "    models:",
                    "      - id: fixture-model",
                    "        name: Session Migrate Fixture",
                    "        contextWindow: 32768",
                    "        maxTokens: 4096",
                    "",
                ]
            )
        )
        models.chmod(0o600)
        process = subprocess.Popen(
            [
                str(binary),
                "--mode",
                "rpc",
                "--model",
                "session-migrate-loopback/fixture-model",
                "--cwd",
                str(work),
                "--no-extensions",
                "--no-skills",
                "--no-rules",
                "--no-lsp",
                "--no-pty",
                "--no-title",
                "--resume",
                str(session_path),
            ],
            cwd=work,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        output: list[str] = []
        _read_until(process, output, lambda value: value.get("type") == "ready")
        assert process.stdin is not None

        process.stdin.write(json.dumps({"id": "initial", "type": "get_messages"}) + "\n")
        process.stdin.flush()
        _read_until(
            process,
            output,
            lambda value: value.get("type") == "response" and value.get("id") == "initial",
        )

        process.stdin.write(
            json.dumps({"id": "followup", "type": "prompt", "message": FOLLOWUP}) + "\n"
        )
        process.stdin.flush()
        _read_until(
            process,
            output,
            lambda value: value.get("type") == "response" and value.get("id") == "followup",
        )
        _read_until(process, output, lambda value: value.get("type") == "agent_end")

        for value in (
            {"id": "after", "type": "get_messages"},
            {"id": "rename", "type": "set_session_name", "name": "OMP native proof"},
        ):
            process.stdin.write(json.dumps(value) + "\n")
            process.stdin.flush()
            _read_until(
                process,
                output,
                lambda response, request_id=value["id"]: (
                    response.get("type") == "response" and response.get("id") == request_id
                ),
            )
        process.stdin.close()
        process.stdin = None
        remaining, stderr = process.communicate(timeout=30)
        output.append(remaining)
        returncode = process.returncode
    finally:
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=5)

    stdout = "".join(output)
    assert returncode == 0, (stdout, stderr)
    assert _response(stdout, "prompt")["success"] is True
    assert _response(stdout, "set_session_name")["success"] is True
    messages = _response(stdout, "get_messages")["data"]["messages"]
    serialized_messages = json.dumps(messages, sort_keys=True)
    for marker in (
        "Continue after the synthetic compaction.",
        "The synthetic post-compaction fixture is complete.",
    ):
        assert marker in serialized_messages

    assert provider.requests
    replay = json.dumps(provider.requests, sort_keys=True)
    assert "Continue after the synthetic compaction." in replay
    assert "The synthetic post-compaction fixture is complete." in replay
    assert FOLLOWUP in replay

    after = session_path.read_bytes()
    assert after[omp.TITLE_SLOT_BYTES :].startswith(before[omp.TITLE_SLOT_BYTES :])
    assert len(after) > len(before)
    parsed = omp.parse_session(session_path)
    assert parsed.title == "OMP native proof"
    assert any(
        event.kind == EventKind.MESSAGE and event.role == Role.USER and event.text == FOLLOWUP
        for event in parsed.events
    )
    assert any(
        event.kind == EventKind.MESSAGE and event.role == Role.ASSISTANT and event.text == REPLY
        for event in parsed.events
    )
