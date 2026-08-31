"""Opt-in native-from-empty corpus trajectories for Pi 0.80.6 and OMP 18.0.5."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import queue
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from native_corpus.loader import load_standalone_fixture
from native_corpus.route_oracle import (
    assert_source_expectations,
    assert_tool_linkage,
    observed_modality_counts,
    parse_native_fixture,
)

from session_migrate.formats import omp, pi
from session_migrate.model import EventKind, Role, Session

ASSETS = Path(__file__).parent / "native_corpus" / "v1" / "assets"
PI_FIXTURE_ROOT = (
    Path(__file__).parent
    / "native_corpus/v1/sources/pi/0.80.6/portable-rich"
)
OMP_FIXTURE_ROOT = (
    Path(__file__).parent / "native_corpus/v1/sources/omp/18.0.5/portable-rich"
)
PI_FIXTURE = PI_FIXTURE_ROOT / "native/session.jsonl"
OMP_FIXTURE = OMP_FIXTURE_ROOT / "native/sessions/--fixture-work--/session.jsonl"
PI_PUBLIC_ID = "70707070-7070-4070-8070-707070707070"
OMP_PUBLIC_ID = "71717171-7171-4171-8171-717171717171"
PI_CAPTURE_ID = "60606060-6060-4060-8060-606060606060"
PI_CLI_SHA256 = "af302f231437eaf6f37691bce4b34234fcb626bcb5eb3910d4fc3f6519bf78ca"
PI_CLI_BYTES = 681
PROMPT = (
    "We are testing a disposable timeline helper. Remember SM_CORPUS_7319, inspect "
    "timeline.py and CORPUS_NOTE.txt with the native file tool, explain the boundary "
    "bug briefly, and do not edit files."
)
MEDIA_PROMPT = (
    "Inspect each attached medium the harness accepts. For the image, report the "
    "visible shape, color, and nonce. For the document, report its nonce. Acknowledge "
    "accepted audio or video without inventing content."
)
FAILURE_PROMPT = (
    "Use the native file tool to read missing-corpus-file.txt, report the failure, "
    "then continue without creating it."
)
RECALL_PROMPT = (
    "Without tools, recall the conversation, file, image, and document nonces that "
    "were actually available in this session."
)
COLD_PROMPT = "COLD_RELOAD_VERIFY_8421: confirm the earlier tool and media context remains."


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
        chunks = _provider_chunks(value)
        body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        encoded = (body + "data: [DONE]\n\n").encode()
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def _provider_chunks(request: dict[str, Any]) -> list[dict[str, Any]]:
    messages = request.get("messages")
    assert isinstance(messages, list)
    user_index = max(
        index
        for index, message in enumerate(messages)
        if isinstance(message, dict) and message.get("role") == "user"
    )
    active = messages[user_index:]
    user_wire = json.dumps(active[0], sort_keys=True)
    tool_results = [
        message
        for message in active[1:]
        if isinstance(message, dict) and message.get("role") == "tool"
    ]
    if "SM_CORPUS_7319" in user_wire and len(tool_results) < 2:
        path = "timeline.py" if not tool_results else "CORPUS_NOTE.txt"
        return _tool_chunks(f"call_native_{len(tool_results) + 1}", path)
    if "missing-corpus-file.txt" in user_wire and not tool_results:
        return _tool_chunks("call_native_missing", "missing-corpus-file.txt")
    if "SM_CORPUS_7319" in user_wire:
        text = "BOUNDARY_ANSWER_7319 COPPER_4821"
    elif "missing-corpus-file.txt" in user_wire:
        text = "MISSING_FILE_CONFIRMED_7319"
    elif "COLD_RELOAD_VERIFY_8421" in user_wire:
        text = "COLD_RELOAD_OK_8421 SM_CORPUS_7319 COPPER_4821 BLUE_TRIANGLE_7319"
    elif "Without tools" in user_wire:
        text = "SM_CORPUS_7319 COPPER_4821 BLUE_TRIANGLE_7319 ORBIT_2048"
    else:
        text = "MEDIA_ACCEPTED_7319"
    return _text_chunks(text)


def _tool_chunks(call_id: str, path: str) -> list[dict[str, Any]]:
    return [
        {
            "id": f"chatcmpl-{call_id}",
            "object": "chat.completion.chunk",
            "created": 1788210000,
            "model": "fixture-model",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": "read",
                                    "arguments": json.dumps({"path": path}),
                                },
                            }
                        ],
                    },
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": f"chatcmpl-{call_id}",
            "object": "chat.completion.chunk",
            "created": 1788210000,
            "model": "fixture-model",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
        },
    ]


def _text_chunks(text: str) -> list[dict[str, Any]]:
    return [
        {
            "id": "chatcmpl-native-text",
            "object": "chat.completion.chunk",
            "created": 1788210000,
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
            "id": "chatcmpl-native-text",
            "object": "chat.completion.chunk",
            "created": 1788210000,
            "model": "fixture-model",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
        },
    ]


class RpcProcess:
    def __init__(self, process: subprocess.Popen[str]) -> None:
        self.process = process
        self.output: list[str] = []
        self._sequence = 0
        self._lines: queue.Queue[str] = queue.Queue()
        self._reader = threading.Thread(target=self._pump_stdout, daemon=True)
        self._reader.start()

    def command(self, command: dict[str, Any]) -> dict[str, Any]:
        self._sequence += 1
        request_id = f"rpc-{self._sequence}"
        command = {"id": request_id, **command}
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(command) + "\n")
        self.process.stdin.flush()
        return self._read_until(
            lambda value: value.get("type") == "response" and value.get("id") == request_id
        )

    def prompt(self, message: str, images: list[dict[str, str]] | None = None) -> bool:
        response = self.command({"type": "prompt", "message": message, "images": images})
        if response.get("success") is not True:
            return False
        self._read_until(lambda value: value.get("type") == "agent_end", timeout=60)
        return True

    def close(self) -> tuple[str, str]:
        assert self.process.stdin is not None
        self.process.stdin.close()
        self.process.stdin = None
        self.process.wait(timeout=30)
        self._reader.join(timeout=5)
        assert self.process.stderr is not None
        stderr = self.process.stderr.read()
        assert self.process.returncode == 0, ("".join(self.output), stderr)
        return "".join(self.output), stderr

    def _pump_stdout(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.output.append(line)
            self._lines.put(line)

    def _read_until(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        *,
        timeout: float = 30,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = self._lines.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                break
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and predicate(value):
                return value
        stderr = ""
        if self.process.poll() is not None and self.process.stderr is not None:
            stderr = self.process.stderr.read()
        raise AssertionError(
            f"RPC trajectory timed out; returncode={self.process.poll()}; "
            f"output={''.join(self.output)[-4000:]}; stderr={stderr[-4000:]}"
        )


def _environment(home: Path, agent_home: Path, temporary: Path) -> dict[str, str]:
    environment = {
        "HOME": str(home),
        "PI_CODING_AGENT_DIR": str(agent_home),
        "TMPDIR": str(temporary),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "TERM": "dumb",
        "NO_COLOR": "1",
        "PI_OFFLINE": "1",
    }
    for key in ("LANG", "LC_ALL", "LC_CTYPE"):
        if os.environ.get(key):
            environment[key] = os.environ[key]
    return environment


def _write_model_config(format_name: str, agent_home: Path, port: int) -> None:
    if format_name == "pi":
        value = {
            "providers": {
                "session-migrate-loopback": {
                    "baseUrl": f"http://127.0.0.1:{port}/v1",
                    "api": "openai-completions",
                    "apiKey": "fixture-not-a-secret",
                    "models": [
                        {
                            "id": "fixture-model",
                            "name": "Session Migrate Fixture",
                            "input": ["text", "image"],
                            "contextWindow": 32768,
                            "maxTokens": 4096,
                        }
                    ],
                }
            }
        }
        (agent_home / "models.json").write_text(json.dumps(value))
        (agent_home / "models.json").chmod(0o600)
        return
    (agent_home / "models.yml").write_text(
        "\n".join(
            [
                "providers:",
                "  session-migrate-loopback:",
                f"    baseUrl: http://127.0.0.1:{port}/v1",
                "    api: openai-completions",
                "    auth: none",
                "    models:",
                "      - id: fixture-model",
                "        name: Session Migrate Fixture",
                "        contextWindow: 32768",
                "        maxTokens: 4096",
                "        input: [text, image]",
                "",
            ]
        )
    )
    (agent_home / "models.yml").chmod(0o600)


def _launch(
    format_name: str,
    binary: Path,
    environment: dict[str, str],
    work: Path,
    session_dir: Path,
    *,
    session_id: str | None = None,
    session_path: Path | None = None,
) -> RpcProcess:
    common = [
        str(binary),
        "--mode",
        "rpc",
        "--model",
        "session-migrate-loopback/fixture-model",
        "--api-key",
        "fixture-not-a-secret",
        "--session-dir",
        str(session_dir),
        "--no-extensions",
        "--no-skills",
    ]
    if format_name == "pi":
        common.extend(
            ["--no-prompt-templates", "--no-context-files", "--offline", "--tools", "read"]
        )
        if session_path is not None:
            common.extend(["--session", str(session_path)])
        elif session_id is not None:
            common.extend(["--session-id", session_id])
    else:
        common.extend(
            [
                "--cwd",
                str(work),
                "--no-rules",
                "--no-lsp",
                "--no-pty",
                "--no-title",
                "--tools",
                "read",
                "--auto-approve",
            ]
        )
        if session_path is not None:
            common.extend(["--resume", str(session_path)])
    process = subprocess.Popen(
        common,
        cwd=work,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return RpcProcess(process)


def _image(path: Path, media_type: str) -> dict[str, str]:
    return {
        "type": "image",
        "data": base64.b64encode(path.read_bytes()).decode(),
        "mimeType": media_type,
    }


def _exact_binary(format_name: str) -> Path:
    environment_name = (
        "SESSION_MIGRATE_PI_BIN" if format_name == "pi" else "SESSION_MIGRATE_OMP_BIN"
    )
    value = os.environ.get(environment_name)
    if not value:
        pytest.skip(f"set {environment_name} to the exact pinned native client")
    binary = Path(value)
    version = subprocess.run(
        [str(binary), "--version"], check=False, capture_output=True, text=True, timeout=15
    )
    assert version.returncode == 0
    if format_name == "pi":
        assert version.stdout.strip() == pi.PINNED_PI_VERSION
        resolved = binary.resolve()
        assert resolved.stat().st_size == PI_CLI_BYTES
        assert hashlib.sha256(resolved.read_bytes()).hexdigest() == PI_CLI_SHA256
    else:
        assert version.stdout.strip() == f"omp/{omp.PINNED_OMP_VERSION}"
        assert binary.stat().st_size == omp.PINNED_OMP_LINUX_X64_BYTES
        assert hashlib.sha256(binary.read_bytes()).hexdigest() == omp.PINNED_OMP_LINUX_X64_SHA256
    return binary


def _load_sanitizer() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts/native-corpus/sanitize-pi-omp.py"
    spec = importlib.util.spec_from_file_location("sanitize_pi_omp_native", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assert_rich_session(session: Session) -> None:
    wire = json.dumps(
        [
            {
                "kind": event.kind.value,
                "role": event.role.value if event.role else None,
                "text": event.text,
                "tool": event.tool_name,
                "call": event.tool_call_id,
                "payload": event.payload,
            }
            for event in session.events
        ],
        sort_keys=True,
    )
    for marker in (
        "SM_CORPUS_7319",
        "COPPER_4821",
        "BLUE_TRIANGLE_7319",
        "missing-corpus-file.txt",
        "MISSING_FILE_CONFIRMED_7319",
    ):
        assert marker in wire
    assert sum(event.kind == EventKind.TOOL_CALL for event in session.events) == 3
    results = [event for event in session.events if event.kind == EventKind.TOOL_RESULT]
    assert len(results) == 3
    assert sum(event.payload.get("is_error") is True for event in results) == 1
    media_types = []
    for event in session.events:
        if event.kind != EventKind.CONTEXT or event.role != Role.USER:
            continue
        image_url = str(event.payload.get("image_url") or "")
        assert image_url.startswith("data:") and ";base64," in image_url
        media_types.append(image_url[5:].split(";", 1)[0])
    assert media_types == ["image/png", "application/pdf", "audio/wav", "video/mp4"]


def _export_capture(
    format_name: str,
    session_path: Path,
    sanitized_path: Path,
    agent_home: Path,
    *,
    native_session_id: str,
    source_cwd: Path,
    mutation_counts: dict[str, int],
    media_outcomes: dict[str, bool],
) -> None:
    destination_value = os.environ.get("SESSION_MIGRATE_CORPUS_CAPTURE_OUT")
    if not destination_value:
        return
    destination = Path(destination_value) / format_name
    raw = destination / "raw"
    public = destination / "public"
    for directory in (raw, public):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copy2(session_path, raw / "session.jsonl")
    shutil.copy2(sanitized_path, public / "session.jsonl")
    for root in (raw, public):
        (root / "session.jsonl").chmod(0o600)
    if format_name == "omp":
        for blob in (agent_home / "blobs").iterdir():
            if blob.is_file() and len(blob.name) == 64:
                for root in (raw, public):
                    blob_dir = root / "blobs"
                    blob_dir.mkdir(mode=0o700, exist_ok=True)
                    shutil.copy2(blob, blob_dir / blob.name)
                    (blob_dir / blob.name).chmod(0o600)
    metadata = {
        "format": format_name,
        "native_session_id": native_session_id,
        "source_cwd": str(source_cwd),
        "raw_sha256": hashlib.sha256(session_path.read_bytes()).hexdigest(),
        "raw_size": session_path.stat().st_size,
        "public_sha256": hashlib.sha256(sanitized_path.read_bytes()).hexdigest(),
        "public_size": sanitized_path.stat().st_size,
        "mutations": mutation_counts,
        "media_outcomes": media_outcomes,
    }
    (destination / "capture.json").write_text(json.dumps(metadata, indent=2) + "\n")
    (destination / "capture.json").chmod(0o600)


@pytest.mark.parametrize("fixture_root", (PI_FIXTURE_ROOT, OMP_FIXTURE_ROOT))
def test_pi_omp_public_fixture_matches_reviewed_ir(
    fixture_root: Path, tmp_path: Path
) -> None:
    fixture = load_standalone_fixture(fixture_root)
    session = parse_native_fixture(fixture, tmp_path / "materialized")
    assert_source_expectations(fixture, session)
    assert_tool_linkage(session.events)
    observed = observed_modality_counts(session)
    assert {
        modality: observed[modality]
        for modality in ("user_image", "document", "audio", "video")
    } == {"user_image": 1, "document": 1, "audio": 1, "video": 1}


@pytest.mark.parametrize("format_name", ("pi", "omp"))
def test_exact_pi_omp_native_from_empty_media_tools_and_cold_reload(
    format_name: str, tmp_path: Path
) -> None:
    binary = _exact_binary(format_name)
    home = tmp_path / "home"
    agent_home = tmp_path / "agent"
    temporary = tmp_path / "tmp"
    work = tmp_path / "private-capture/work"
    session_dir = tmp_path / "sessions" if format_name == "pi" else agent_home / "sessions"
    for directory in (home, agent_home, temporary, work, session_dir):
        directory.mkdir(parents=True, mode=0o700)
    for name in ("timeline.py", "CORPUS_NOTE.txt"):
        (work / name).write_bytes((ASSETS / name).read_bytes())

    provider = Provider(("127.0.0.1", 0), Handler)
    provider.requests = []
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    environment = _environment(home, agent_home, temporary)
    _write_model_config(format_name, agent_home, provider.server_address[1])
    try:
        rpc = _launch(
            format_name,
            binary,
            environment,
            work,
            session_dir,
            session_id=PI_CAPTURE_ID if format_name == "pi" else None,
        )
        state = rpc.command({"type": "get_state"})
        session_path = Path(state["data"]["sessionFile"])
        native_session_id = state["data"]["sessionId"]
        assert rpc.command(
            {"type": "set_session_name", "name": "repair-event-window-boundary"}
        )["success"]
        assert rpc.prompt(PROMPT)
        media_outcomes = {
            "user_image": rpc.prompt(
                MEDIA_PROMPT,
                [_image(ASSETS / "corpus-card.png", "image/png")],
            ),
            "document": rpc.prompt(
                "MEDIA_ATTEMPT_DOCUMENT_ORBIT_2048",
                [_image(ASSETS / "corpus-document.pdf", "application/pdf")],
            ),
            "audio": rpc.prompt(
                "MEDIA_ATTEMPT_AUDIO_440HZ_250MS",
                [_image(ASSETS / "corpus-tone.wav", "audio/wav")],
            ),
            "video": rpc.prompt(
                "MEDIA_ATTEMPT_VIDEO_BLUE_TO_LIME_1S",
                [_image(ASSETS / "corpus-transition.mp4", "video/mp4")],
            ),
        }
        assert rpc.prompt(FAILURE_PROMPT)
        assert rpc.prompt(RECALL_PROMPT)
        rpc.close()
        assert media_outcomes == {
            "user_image": True,
            "document": True,
            "audio": True,
            "video": True,
        }

        parser = pi.parse_session if format_name == "pi" else omp.parse_session
        captured = parser(session_path)
        _assert_rich_session(captured)
        raw = session_path.read_bytes()
        public_id = PI_PUBLIC_ID if format_name == "pi" else OMP_PUBLIC_ID
        sanitized_path = tmp_path / "sanitized.jsonl"
        counts = _load_sanitizer().sanitize_capture(
            session_path,
            sanitized_path,
            source_cwd=str(work),
            source_session_id=native_session_id,
            public_session_id=public_id,
            format_name=format_name,
        )
        assert counts["cwd"] >= 1
        assert counts["uuid"] == 1
        sanitized = parser(sanitized_path)
        _assert_rich_session(sanitized)
        assert sanitized.session_id == public_id
        assert sanitized.cwd == Path("/fixture/work")
        _export_capture(
            format_name,
            session_path,
            sanitized_path,
            agent_home,
            native_session_id=native_session_id,
            source_cwd=work,
            mutation_counts=counts,
            media_outcomes=media_outcomes,
        )

        reload_path = tmp_path / "cold-reload.jsonl"
        reload_path.write_bytes(
            sanitized_path.read_bytes().replace(b"/fixture/work", str(work).encode())
        )
        reload_path.chmod(0o600)
        before = reload_path.read_bytes()
        cold = _launch(
            format_name,
            binary,
            environment,
            work,
            session_dir,
            session_path=reload_path,
        )
        messages = cold.command({"type": "get_messages"})
        assert messages["success"] is True
        assert "SM_CORPUS_7319" in json.dumps(messages, sort_keys=True)
        assert cold.prompt(COLD_PROMPT)
        cold.close()
        after = reload_path.read_bytes()
        if format_name == "omp":
            assert after[omp.TITLE_SLOT_BYTES :].startswith(before[omp.TITLE_SLOT_BYTES :])
        else:
            assert after.startswith(before)
        reloaded = parser(reload_path)
        assert any(event.text and "COLD_RELOAD_OK_8421" in event.text for event in reloaded.events)
        assert provider.requests
        assert "SM_CORPUS_7319" in json.dumps(provider.requests[-1], sort_keys=True)
        assert raw
    finally:
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize(
    ("format_name", "fixture"),
    (("pi", PI_FIXTURE), ("omp", OMP_FIXTURE)),
)
def test_exact_pi_omp_public_fixture_cold_reload(
    format_name: str, fixture: Path, tmp_path: Path
) -> None:
    binary = _exact_binary(format_name)
    assert fixture.is_file()
    home = tmp_path / "home"
    agent_home = tmp_path / "agent"
    temporary = tmp_path / "tmp"
    work = tmp_path / "work"
    session_dir = tmp_path / "sessions" if format_name == "pi" else agent_home / "sessions"
    for directory in (home, agent_home, temporary, work, session_dir):
        directory.mkdir(parents=True, mode=0o700)
    for name in ("timeline.py", "CORPUS_NOTE.txt"):
        (work / name).write_bytes((ASSETS / name).read_bytes())
    if format_name == "omp":
        fixture_native_root = fixture.parents[2]
        shutil.copytree(fixture_native_root, agent_home, dirs_exist_ok=True)
        native = agent_home / fixture.relative_to(fixture_native_root)
    else:
        native = tmp_path / "native.jsonl"
        native.write_bytes(fixture.read_bytes())
    native.write_bytes(native.read_bytes().replace(b"/fixture/work", str(work).encode()))
    native.chmod(0o600)

    provider = Provider(("127.0.0.1", 0), Handler)
    provider.requests = []
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    environment = _environment(home, agent_home, temporary)
    _write_model_config(format_name, agent_home, provider.server_address[1])
    try:
        before = native.read_bytes()
        rpc = _launch(
            format_name,
            binary,
            environment,
            work,
            session_dir,
            session_path=native,
        )
        messages = rpc.command({"type": "get_messages"})
        assert "SM_CORPUS_7319" in json.dumps(messages, sort_keys=True)
        assert rpc.prompt(COLD_PROMPT)
        rpc.close()
        after = native.read_bytes()
        if format_name == "omp":
            assert after[omp.TITLE_SLOT_BYTES :].startswith(before[omp.TITLE_SLOT_BYTES :])
        else:
            assert after.startswith(before)
        assert "SM_CORPUS_7319" in json.dumps(provider.requests[-1], sort_keys=True)
    finally:
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=5)
