#!/usr/bin/env python3
"""Capture the native MastraCode 0.37.1 source corpus trajectory.

This is an opt-in developer tool. It uses a credential-free loopback model,
an empty isolated app-data directory, and the exact published client.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import pty
import shutil
import signal
import sqlite3
import struct
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

VERSION = "0.37.1"
SOURCE_COMMIT = "003e75745c5fd6a7af8464ece1d2930f81dd15af"
BINARY_SHA256 = "9921609cd35cb9dc91c8a2ae5d606d937d904404f084b89d7b9739cca260f35b"
BINARY_BYTES = 10526
MODEL = "mastracode/session-migrate-loopback/fixture-model"
TITLE = "repair-event-window-boundary"
SCENARIO = json.loads(
    (Path(__file__).resolve().parents[2] / "tests/native_corpus/v1/scenario.json").read_text()
)


@dataclass(frozen=True, slots=True)
class RunEvidence:
    turn: str
    returncode: int
    stdout: str
    stderr: str
    request_start: int
    request_end: int


class Provider(ThreadingHTTPServer):
    requests: list[dict[str, Any]]
    lock: threading.Lock


def _text_chunks(text: str) -> list[dict[str, Any]]:
    return [
        {
            "id": "mastracode-native-text",
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
            "id": "mastracode-native-text",
            "object": "chat.completion.chunk",
            "created": 1788210000,
            "model": "fixture-model",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
        },
    ]


def _tool_chunks(call_id: str, tool_name: str, arguments: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": f"mastracode-{call_id}",
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
                                    "name": tool_name,
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ],
                    },
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": f"mastracode-{call_id}",
            "object": "chat.completion.chunk",
            "created": 1788210000,
            "model": "fixture-model",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
        },
    ]


def _messages(request: dict[str, Any]) -> list[dict[str, Any]]:
    value = request.get("messages")
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    return ""


def response_chunks(request: dict[str, Any]) -> list[dict[str, Any]]:
    messages = _messages(request)
    last_role = str(messages[-1].get("role")) if messages else ""
    latest_user = next(
        (
            _content_text(message.get("content"))
            for message in reversed(messages)
            if message.get("role") == "user"
        ),
        "",
    )
    if "SM_CORPUS_7319" in latest_user:
        if last_role == "user":
            return _tool_chunks(
                "call_mastracode_inspect",
                "execute_command",
                {"command": "cat timeline.py && cat CORPUS_NOTE.txt"},
            )
        return _text_chunks(
            "The boundary check uses <, so exactly touching intervals do not merge. "
            "The native file marker is COPPER_4821."
        )
    if "missing-corpus-file.txt" in latest_user:
        if last_role == "user":
            return _tool_chunks(
                "call_mastracode_missing",
                "view",
                {"path": "missing-corpus-file.txt"},
            )
        return _text_chunks(
            "SM_NATIVE_FAILURE_7319: missing-corpus-file.txt was absent; continuing."
        )
    if "Without tools, recall" in latest_user:
        return _text_chunks("Recall: SM_CORPUS_7319, COPPER_4821, and BLUE_TRIANGLE_7319.")
    return _text_chunks("The accepted image is a blue triangle labeled BLUE_TRIANGLE_7319.")


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
        if not isinstance(request, dict):
            self.send_error(400)
            return
        with self.server.lock:  # type: ignore[attr-defined]
            self.server.requests.append(request)  # type: ignore[attr-defined]
        chunks = response_chunks(request)
        body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        encoded = (body + "data: [DONE]\n\n").encode()
        self.send_response(200)
        self.send_header("content-type", "text/event-stream; charset=utf-8")
        self.send_header("cache-control", "no-cache")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


@contextmanager
def loopback_provider() -> Iterator[Provider]:
    provider = Provider(("127.0.0.1", 0), Handler)
    provider.requests = []
    provider.lock = threading.Lock()
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    try:
        yield provider
    finally:
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=5)


def verify_client(source: Path, binary: Path) -> dict[str, Any]:
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source.resolve(strict=True),
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    ).stdout.strip()
    if revision != SOURCE_COMMIT:
        raise SystemExit(f"expected MastraCode source {SOURCE_COMMIT}, got {revision}")
    package = json.loads((binary.resolve(strict=True).parent.parent / "package.json").read_text())
    if package.get("name") != "mastracode" or package.get("version") != VERSION:
        raise SystemExit("MastraCode package identity does not match the pinned client")
    size = binary.stat().st_size
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    if size != BINARY_BYTES or digest != BINARY_SHA256:
        raise SystemExit("MastraCode CLI bytes do not match the pinned client")
    return {
        "version": VERSION,
        "source_commit": revision,
        "binary_size": size,
        "binary_sha256": digest,
    }


def environment(home: Path, app_data: Path, temporary: Path) -> dict[str, str]:
    values = {
        "HOME": str(home),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "MASTRA_APP_DATA_DIR": str(app_data),
        "TMPDIR": str(temporary),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "TERM": "xterm-256color",
        "DO_NOT_TRACK": "1",
        "MASTRACODE_MODEL_ID": MODEL,
        "MASTRACODE_THEME": "dark",
    }
    for key in ("LANG", "LC_ALL", "LC_CTYPE"):
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def write_settings(path: Path, port: int) -> None:
    path.write_text(
        json.dumps(
            {
                "onboarding": {
                    "skippedAt": "2026-08-31T00:00:00Z",
                    "version": 1,
                },
                "customProviders": [
                    {
                        "name": "Session Migrate Loopback",
                        "url": f"http://127.0.0.1:{port}/v1",
                        "apiKey": "credential-free-loopback",
                        "models": ["fixture-model"],
                    }
                ],
                "preferences": {
                    "thinkingLevel": "off",
                    "quietMode": True,
                    "theme": "dark",
                },
                "storage": {"backend": "libsql", "libsql": {}, "pg": {}},
                "browser": {"enabled": False},
                "signals": {"unixSocketPubSub": False},
                "mcp": {"claudeCodeGlobal": False, "codexGlobal": False},
                "observability": {"resources": {}, "localTracing": False},
            },
            indent=2,
        )
        + "\n"
    )
    path.chmod(0o600)


def _read_pty(master: int, output: bytearray) -> None:
    while True:
        try:
            output.extend(os.read(master, 65536))
        except BlockingIOError:
            return
        except OSError:
            return


def _wait_for_requests(provider: Provider, minimum: int, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with provider.lock:
            count = len(provider.requests)
        if count >= minimum:
            return
        time.sleep(0.05)
    raise RuntimeError(f"timed out waiting for provider request {minimum}")


def run_media_tui(
    binary: Path,
    work: Path,
    settings: Path,
    env: dict[str, str],
    provider: Provider,
    prompt: str,
) -> RunEvidence:
    start = len(provider.requests)
    master, slave = pty.openpty()
    fcntl.ioctl(slave, 0x5414, struct.pack("HHHH", 42, 140, 0, 0))
    process = subprocess.Popen(
        [str(binary), "--settings", str(settings)],
        cwd=work,
        env=env,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
    )
    os.close(slave)
    os.set_blocking(master, False)
    output = bytearray()
    try:
        ready_deadline = time.monotonic() + 20
        while time.monotonic() < ready_deadline:
            _read_pty(master, output)
            if b"Resource ID:" in output and b"fixture-model" in output:
                break
            if process.poll() is not None:
                raise RuntimeError("MastraCode TUI exited before its editor was ready")
            time.sleep(0.05)
        else:
            raise RuntimeError("MastraCode TUI editor did not become ready")

        os.write(master, prompt.encode())
        for label, name in (
            (" image ", "corpus-card.png"),
            (" document ", "corpus-document.pdf"),
            (" audio ", "corpus-tone.wav"),
            (" video ", "corpus-transition.mp4"),
        ):
            os.write(master, label.encode())
            path = str(work / name).encode()
            os.write(master, b"\x1b[200~" + path + b"\x1b[201~")
        os.write(master, b"\r")
        _wait_for_requests(provider, start + 1, timeout=30)
        # Allow the native persistence callbacks and terminal rendering to finish.
        time.sleep(3)
        _read_pty(master, output)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        _read_pty(master, output)
        os.close(master)
    return RunEvidence(
        "media",
        process.returncode or 0,
        output.decode(errors="replace"),
        "",
        start,
        len(provider.requests),
    )


def select_thread(database: Path) -> tuple[str, str]:
    with sqlite3.connect(database) as db:
        rows = db.execute(
            'SELECT t.id,t.resourceId,COUNT(m.id) FROM "mastra_threads" t '
            'LEFT JOIN "mastra_messages" m ON m.thread_id=t.id '
            "GROUP BY t.id,t.resourceId HAVING COUNT(m.id)>0 "
            "ORDER BY COUNT(m.id) DESC,t.createdAt,t.id"
        ).fetchall()
    if len(rows) != 1:
        raise RuntimeError(f"expected one native MastraCode thread, found {len(rows)}")
    return str(rows[0][0]), str(rows[0][1])


def run_headless(
    binary: Path,
    work: Path,
    settings: Path,
    env: dict[str, str],
    provider: Provider,
    *,
    turn: str,
    prompt: str,
    session_id: str,
    resource_id: str,
    title: str | None = None,
) -> RunEvidence:
    start = len(provider.requests)
    command = [
        str(binary),
        "--settings",
        str(settings),
        "--prompt",
        prompt,
        "--output",
        "json",
        "--model",
        MODEL,
        "--resource-id",
        resource_id,
        "--thread",
        session_id,
        "--permission-mode",
        "auto",
        "--thinking-level",
        "off",
        "--max-turns",
        "4",
        "--timeout",
        "60",
    ]
    if title:
        command.extend(["--title", title])
    completed = subprocess.run(
        command,
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=75,
        check=False,
    )
    return RunEvidence(
        turn,
        completed.returncode,
        completed.stdout,
        completed.stderr,
        start,
        len(provider.requests),
    )


def checkpoint(source: Path, target: Path) -> None:
    with sqlite3.connect(source) as origin, sqlite3.connect(target) as destination:
        origin.backup(destination)
    target.chmod(0o600)


def capture(source: Path, binary: Path, output_dir: Path) -> None:
    client = verify_client(source, binary)
    output_dir.mkdir(parents=True, exist_ok=False)
    turns = {str(turn["id"]): turn for turn in SCENARIO["turns"]}
    with tempfile.TemporaryDirectory(prefix="session-migrate-mastracode-source-") as directory:
        root = Path(directory)
        home = root / "home"
        app_data = root / "app-data"
        temporary = root / "tmp"
        work = root / "work"
        for path in (home, app_data, temporary, work):
            path.mkdir(mode=0o700)
        assets = Path(__file__).resolve().parents[2] / "tests/native_corpus/v1/assets"
        for name in (
            "timeline.py",
            "CORPUS_NOTE.txt",
            "corpus-card.png",
            "corpus-document.pdf",
            "corpus-tone.wav",
            "corpus-transition.mp4",
        ):
            shutil.copyfile(assets / name, work / name)
        env = environment(home, app_data, temporary)
        with loopback_provider() as provider:
            settings = app_data / "settings.json"
            write_settings(settings, int(provider.server_address[1]))
            evidence = [
                run_media_tui(
                    binary,
                    work,
                    settings,
                    env,
                    provider,
                    str(turns["media"]["text"]),
                )
            ]
            state = app_data / "mastra.db"
            session_id, resource_id = select_thread(state)
            evidence.append(
                run_headless(
                    binary,
                    work,
                    settings,
                    env,
                    provider,
                    turn="inspect",
                    prompt=str(turns["inspect"]["text"]),
                    session_id=session_id,
                    resource_id=resource_id,
                    title=TITLE,
                )
            )
            evidence.append(
                run_headless(
                    binary,
                    work,
                    settings,
                    env,
                    provider,
                    turn="failure",
                    prompt=str(turns["failure"]["text"]),
                    session_id=session_id,
                    resource_id=resource_id,
                )
            )
            evidence.append(
                run_headless(
                    binary,
                    work,
                    settings,
                    env,
                    provider,
                    turn="recall",
                    prompt=str(turns["recall"]["text"]),
                    session_id=session_id,
                    resource_id=resource_id,
                )
            )
            requests = tuple(provider.requests)

        raw_db = output_dir / "mastra.db"
        checkpoint(state, raw_db)
        request_wire = "\n".join(json.dumps(item, sort_keys=True) for item in requests)
        media_wire = "\n".join(
            json.dumps(item, sort_keys=True)
            for item in requests[evidence[0].request_start : evidence[0].request_end]
        )
        report = {
            "format": "mastracode",
            "client": client,
            "session_id": session_id,
            "resource_id": resource_id,
            "title": TITLE,
            "source_cwd": str(work),
            "raw_db_sha256": hashlib.sha256(raw_db.read_bytes()).hexdigest(),
            "provider_requests": len(requests),
            "turns": [
                {
                    "id": item.turn,
                    "returncode": item.returncode,
                    "stdout": item.stdout,
                    "stderr": item.stderr,
                    "provider_requests": item.request_end - item.request_start,
                }
                for item in evidence
            ],
            "media": {
                "user_image": {
                    "accepted": "data:image/png" in media_wire,
                    "provider_received_image_bytes": "data:image/png" in media_wire,
                },
                "document": {
                    "accepted": False,
                    "observed_as_literal_path": "corpus-document.pdf" in media_wire,
                    "provider_received_media_bytes": "application/pdf" in media_wire,
                },
                "audio": {
                    "accepted": False,
                    "observed_as_literal_path": "corpus-tone.wav" in media_wire,
                    "provider_received_media_bytes": "audio/wav" in media_wire,
                },
                "video": {
                    "accepted": False,
                    "observed_as_literal_path": "corpus-transition.mp4" in media_wire,
                    "provider_received_media_bytes": "video/mp4" in media_wire,
                },
            },
            "private_path_observed": str(work) in request_wire,
        }
        (output_dir / "capture-report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    arguments = parser.parse_args()
    capture(arguments.source, arguments.binary, arguments.output_dir)


if __name__ == "__main__":
    main()
