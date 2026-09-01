#!/usr/bin/env python3
"""Capture an exact Hermes 0.20.6 source trajectory without credentials."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
ASSETS = REPO_ROOT / "tests/native_corpus/v1/assets"
SCENARIO = json.loads((REPO_ROOT / "tests/native_corpus/v1/scenario.json").read_text())
TITLE = str(SCENARIO["session_title"])
VERSION = "0.20.6"
RELEASE = "2026.8.27"
SOURCE_COMMIT = "5fc308a70719a83cccdbba4c0e39c23f5a8239d5"
BINARY_BYTES = 339
BINARY_SHA256 = "060277e4ac1caa59ea6e0c551c7627f8fea0989a4aace9f73cdc1f09425f34c2"


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


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def do_GET(self) -> None:  # noqa: N802
        self._models()

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        request = json.loads(self.rfile.read(length))
        if not isinstance(request, dict):
            self.send_error(400)
            return
        with self.server.lock:  # type: ignore[attr-defined]
            self.server.requests.append(request)  # type: ignore[attr-defined]
        messages = request.get("messages")
        if not isinstance(messages, list):
            self._models()
            return
        replay = json.dumps(messages, sort_keys=True)
        latest_user = next(
            (
                json.dumps(message.get("content"), sort_keys=True)
                for message in reversed(messages)
                if isinstance(message, dict) and message.get("role") == "user"
            ),
            "",
        )
        if "inspect timeline.py" in latest_user and "call_hermes_inspect" not in replay:
            chunks = self._tool_chunks(
                "call_hermes_inspect",
                "terminal",
                {"command": "cat timeline.py && cat CORPUS_NOTE.txt"},
            )
        elif "missing-corpus-file.txt" in latest_user and "call_hermes_missing" not in replay:
            chunks = self._tool_chunks(
                "call_hermes_missing",
                "terminal",
                {"command": "cat missing-corpus-file.txt"},
            )
        elif "Describe everything visible" in latest_user:
            chunks = self._text_chunks(
                "A blue triangle labeled BLUE_TRIANGLE_7319 appears on a white card."
            )
        elif "inspect timeline.py" in latest_user:
            chunks = self._text_chunks(
                "The boundary check uses <, so exactly touching intervals do not merge. "
                "The native file marker is COPPER_4821."
            )
        elif "missing-corpus-file.txt" in latest_user:
            chunks = self._text_chunks(
                "SM_NATIVE_FAILURE_7319: missing-corpus-file.txt was absent; continuing."
            )
        elif "attached medium" in latest_user:
            chunks = self._text_chunks(
                "The accepted image is a blue triangle labeled BLUE_TRIANGLE_7319."
            )
        else:
            chunks = self._text_chunks(
                "Recall: SM_CORPUS_7319, COPPER_4821, and BLUE_TRIANGLE_7319."
            )
        self._stream(chunks)

    @staticmethod
    def _tool_chunks(call_id: str, name: str, arguments: dict[str, str]) -> list[dict[str, Any]]:
        return [
            {
                "id": "chatcmpl-hermes-tool",
                "object": "chat.completion.chunk",
                "created": 0,
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
                                        "name": name,
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
                "id": "chatcmpl-hermes-tool",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "fixture-model",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            },
        ]

    @staticmethod
    def _text_chunks(text: str) -> list[dict[str, Any]]:
        return [
            {
                "id": "chatcmpl-hermes-text",
                "object": "chat.completion.chunk",
                "created": 0,
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
                "id": "chatcmpl-hermes-text",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "fixture-model",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
        ]

    def _stream(self, chunks: list[dict[str, Any]]) -> None:
        body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        encoded = (body + "data: [DONE]\n\n").encode()
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _models(self) -> None:
        encoded = json.dumps(
            {
                "object": "list",
                "data": [
                    {
                        "id": "fixture-model",
                        "object": "model",
                        "created": 0,
                        "owned_by": "session-migrate-test",
                    }
                ],
            }
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


@contextmanager
def loopback() -> Iterator[Provider]:
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
    source = source.resolve(strict=True)
    binary = binary.resolve(strict=True)
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    ).stdout.strip()
    if revision != SOURCE_COMMIT:
        raise SystemExit(f"expected Hermes source {SOURCE_COMMIT}, got {revision}")
    version = subprocess.run(
        [str(binary), "--version"],
        cwd=source,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    expected_line = f"Hermes Agent v{VERSION} ({RELEASE})"
    if version.returncode or expected_line not in version.stdout:
        raise SystemExit(f"expected {expected_line!r}, got {version.stdout!r}")
    size = binary.stat().st_size
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    if size != BINARY_BYTES or digest != BINARY_SHA256:
        raise SystemExit("Hermes entry-point wrapper does not match the pinned bytes")
    return {
        "version": VERSION,
        "release": RELEASE,
        "source_commit": revision,
        "binary_size": size,
        "binary_sha256": digest,
    }


def environment(home: Path, source: Path) -> dict[str, str]:
    values = {
        "HOME": str(home),
        "HERMES_HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "TERM": "dumb",
        "NO_COLOR": "1",
        "HERMES_NO_UPDATE_CHECK": "1",
        "SESSION_MIGRATE_HERMES_SOURCE": str(source),
    }
    for key in ("LANG", "LC_ALL", "LC_CTYPE"):
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def write_config(home: Path, port: int) -> None:
    (home / "config.yaml").write_text(
        "\n".join(
            [
                "model:",
                "  provider: loopback",
                "  default: fixture-model",
                "providers:",
                "  loopback:",
                f"    api: http://127.0.0.1:{port}/v1",
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
    (home / "config.yaml").chmod(0o600)


def run_turn(
    source: Path,
    binary: Path,
    home: Path,
    work: Path,
    provider: Provider,
    *,
    turn: str,
    prompt: str,
    session_id: str | None,
    image: Path | None = None,
) -> RunEvidence:
    command = [
        str(binary),
        "chat",
        "--quiet",
        "--provider",
        "loopback",
        "--model",
        "fixture-model",
        "--toolsets",
        "terminal",
        "--in",
        str(work),
        "--max-turns",
        "3",
        "--yolo",
    ]
    if session_id is None:
        command.extend(["--continue", TITLE, "--create-if-missing"])
    else:
        command.extend(["--resume", session_id])
    if image is not None:
        command.extend(["--image", str(image)])
    command.extend(["--query", prompt])
    start = len(provider.requests)
    completed = subprocess.run(
        command,
        cwd=source,
        env=environment(home, source),
        check=False,
        capture_output=True,
        text=True,
        timeout=90,
    )
    return RunEvidence(
        turn,
        completed.returncode,
        completed.stdout,
        completed.stderr,
        start,
        len(provider.requests),
    )


def export_session(source: Path, binary: Path, state_db: Path, session_id: str) -> dict[str, Any]:
    interpreter = binary.parent / "python"
    program = (
        "import json,sys; from pathlib import Path; from hermes_state import SessionDB; "
        "db=SessionDB(db_path=Path(sys.argv[1])); value=db.export_session(sys.argv[2]); "
        "db.close(); print(json.dumps(value, ensure_ascii=False))"
    )
    completed = subprocess.run(
        [str(interpreter), "-c", program, str(state_db), session_id],
        cwd=source,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if completed.returncode:
        raise SystemExit(completed.stderr)
    return json.loads(completed.stdout.splitlines()[-1])


def capture(source: Path, binary: Path, output_dir: Path) -> None:
    client = verify_client(source, binary)
    output_dir.mkdir(parents=True, exist_ok=False)
    turns = {str(turn["id"]): turn for turn in SCENARIO["turns"]}
    with tempfile.TemporaryDirectory(prefix="session-migrate-hermes-source-") as directory:
        root = Path(directory)
        home, work = root / "home", root / "work"
        home.mkdir(mode=0o700)
        work.mkdir(mode=0o700)
        for name in (
            "timeline.py",
            "CORPUS_NOTE.txt",
            "corpus-card.png",
            "corpus-document.pdf",
            "corpus-tone.wav",
            "corpus-transition.mp4",
        ):
            shutil.copy2(ASSETS / name, work / name)
        with loopback() as provider:
            write_config(home, provider.server_address[1])
            evidence = [
                run_turn(
                    source,
                    binary,
                    home,
                    work,
                    provider,
                    turn="inspect",
                    prompt=str(turns["inspect"]["text"]),
                    session_id=None,
                )
            ]
            from session_migrate.formats import hermes

            sessions = hermes.list_sessions(home / "state.db")
            if len(sessions) != 1:
                raise SystemExit(f"expected one Hermes session, found {len(sessions)}")
            session_id = sessions[0].session_id
            evidence.append(
                run_turn(
                    source,
                    binary,
                    home,
                    work,
                    provider,
                    turn="user_image",
                    prompt="Inspect the attached medium and report its visible marker.",
                    session_id=session_id,
                    image=work / "corpus-card.png",
                )
            )
            for turn, name in (
                ("document", "corpus-document.pdf"),
                ("audio", "corpus-tone.wav"),
                ("video", "corpus-transition.mp4"),
            ):
                evidence.append(
                    run_turn(
                        source,
                        binary,
                        home,
                        work,
                        provider,
                        turn=turn,
                        prompt=f"Inspect attached medium {name}.",
                        session_id=session_id,
                        image=work / name,
                    )
                )
            evidence.append(
                run_turn(
                    source,
                    binary,
                    home,
                    work,
                    provider,
                    turn="failure",
                    prompt=str(turns["failure"]["text"]),
                    session_id=session_id,
                )
            )
            evidence.append(
                run_turn(
                    source,
                    binary,
                    home,
                    work,
                    provider,
                    turn="recall",
                    prompt=str(turns["recall"]["text"]),
                    session_id=session_id,
                )
            )
            request_documents = tuple(provider.requests)

        state = home / "state.db"
        official = export_session(source, binary, state, session_id)
        raw_db = output_dir / "state.db"
        shutil.copy2(state, raw_db)
        raw_db.chmod(0o600)
        (output_dir / "official-export.json").write_text(
            json.dumps(official, indent=2, ensure_ascii=False) + "\n"
        )
        serialized_requests = [json.dumps(request, sort_keys=True) for request in request_documents]
        media_names = {
            "document": "corpus-document.pdf",
            "audio": "corpus-tone.wav",
            "video": "corpus-transition.mp4",
        }
        media = {}
        for item in evidence:
            if item.turn not in {"user_image", "document", "audio", "video"}:
                continue
            request_wire = "\n".join(serialized_requests[item.request_start : item.request_end])
            if item.turn == "user_image":
                media[item.turn] = {
                    "accepted": item.returncode == 0 and "data:image/png" in request_wire,
                    "persisted_as": "textual image fallback; native DB omits image bytes",
                }
            else:
                rejected_path = work / media_names[item.turn]
                media[item.turn] = {
                    "accepted": False,
                    "error": f"Not a supported image file: {rejected_path}",
                    "observed": item.returncode == 1
                    and "Not a supported image file" in item.stdout,
                }
        report = {
            "format": "hermes",
            "client": client,
            "session_id": session_id,
            "title": TITLE,
            "source_cwd": str(work),
            "raw_db_sha256": hashlib.sha256(raw_db.read_bytes()).hexdigest(),
            "official_export_sha256": hashlib.sha256(
                (output_dir / "official-export.json").read_bytes()
            ).hexdigest(),
            "provider_requests": len(request_documents),
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
            "media": media,
        }
        (output_dir / "capture-report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    capture(args.source, args.binary, args.output_dir)


if __name__ == "__main__":
    main()
