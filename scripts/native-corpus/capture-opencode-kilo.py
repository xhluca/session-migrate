#!/usr/bin/env python3
"""Capture an OpenCode-lineage corpus session through an exact native CLI.

This command is intentionally opt-in and credential-free.  It starts an
OpenAI-compatible loopback provider, creates a session from an empty isolated
home, exercises native tools and every corpus medium through ``run --file``,
then writes the unredacted official export and an evidence report.  Run the
separate sanitizer before committing an export.
"""

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
EXPECTED = {
    "opencode": {
        "version": "1.17.20",
        "size": 189_278_336,
        "sha256": "373af49ceba30c1b64e964463a64f8065103f942f240933a955f6c461e1a67f6",
        "config": "OPENCODE_CONFIG_CONTENT",
    },
    "kilo": {
        "version": "7.5.0",
        "size": 145_118_408,
        "sha256": "ede061eb9178d0158ac66baa81619e2bf66859041d20d0a014798d38ddc7c1ce",
        "config": "KILO_CONFIG_CONTENT",
    },
}


@dataclass(frozen=True, slots=True)
class RunEvidence:
    turn: str
    returncode: int
    stdout: str
    stderr: str


class LoopbackHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []
    work: Path

    def do_GET(self) -> None:  # noqa: N802
        self._send_json(
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
        )

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        request = json.loads(self.rfile.read(length))
        type(self).requests.append(request)
        replay = json.dumps(request, sort_keys=True)
        latest_user = next(
            (
                json.dumps(message.get("content"), sort_keys=True)
                for message in reversed(request.get("messages", []))
                if message.get("role") == "user"
            ),
            "",
        )
        if "missing-corpus-file.txt" in latest_user and "call_missing_corpus_file" not in replay:
            chunks = self._tool_chunks(
                [
                    (
                        "call_missing_corpus_file",
                        "read",
                        {"filePath": str(type(self).work / "missing-corpus-file.txt")},
                    )
                ]
            )
        elif "missing-corpus-file.txt" in latest_user:
            chunks = self._text_chunks(
                "SM_NATIVE_FAILURE_7319: missing-corpus-file.txt was absent; continuing."
            )
        elif "inspect timeline.py" in latest_user and "call_read_timeline" not in replay:
            chunks = self._tool_chunks(
                [
                    (
                        "call_read_timeline",
                        "read",
                        {"filePath": str(type(self).work / "timeline.py")},
                    ),
                    (
                        "call_read_corpus_note",
                        "read",
                        {"filePath": str(type(self).work / "CORPUS_NOTE.txt")},
                    ),
                ]
            )
        elif "inspect timeline.py" in latest_user:
            chunks = self._text_chunks(
                "The boundary check uses <, so exactly touching intervals do not merge. "
                "The native file marker is COPPER_4821."
            )
        elif "Inspect each attached medium" in latest_user:
            chunks = self._text_chunks(
                "The accepted image is a blue triangle labeled BLUE_TRIANGLE_7319. "
                "The accepted PDF reports ORBIT_2048."
            )
        else:
            chunks = self._text_chunks(
                "Recall: SM_CORPUS_7319, COPPER_4821, BLUE_TRIANGLE_7319, ORBIT_2048."
            )
        self._send_sse(chunks)

    @staticmethod
    def _tool_chunks(
        calls: list[tuple[str, str, dict[str, str]]],
    ) -> list[dict[str, Any]]:
        tool_calls = [
            {
                "index": index,
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
            for index, (call_id, name, arguments) in enumerate(calls)
        ]
        return [
            {
                "id": "chatcmpl-native-tool",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "fixture-model",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "tool_calls": tool_calls},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chatcmpl-native-tool",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "fixture-model",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 2, "total_tokens": 22},
            },
        ]

    @staticmethod
    def _text_chunks(text: str) -> list[dict[str, Any]]:
        return [
            {
                "id": "chatcmpl-native-text",
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
                "id": "chatcmpl-native-text",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "fixture-model",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42},
            },
        ]

    def _send_sse(self, chunks: list[dict[str, Any]]) -> None:
        body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        encoded = (body + "data: [DONE]\n\n").encode()
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_json(self, value: object) -> None:
        encoded = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


@contextmanager
def loopback(work: Path) -> Iterator[tuple[int, type[LoopbackHandler]]]:
    handler = LoopbackHandler
    handler.requests = []
    handler.work = work
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], handler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def verify_binary(format_name: str, binary: Path) -> dict[str, Any]:
    expected = EXPECTED[format_name]
    resolved = binary.resolve(strict=True)
    if resolved.is_symlink() or not resolved.is_file():
        raise SystemExit(f"binary must be a regular, non-symlink file: {resolved}")
    size = resolved.stat().st_size
    digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
    completed = subprocess.run(
        [str(resolved), "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    version = completed.stdout.strip()
    if completed.returncode or version != expected["version"]:
        raise SystemExit(f"expected {format_name} {expected['version']}, got {version!r}")
    if size != expected["size"] or digest != expected["sha256"]:
        raise SystemExit(f"{format_name} binary does not match the pinned size and SHA-256")
    return {"version": version, "size": size, "sha256": digest}


def isolated_env(root: Path, config_name: str, config: dict[str, Any]) -> dict[str, str]:
    home = root / "home"
    values = {
        "HOME": str(home),
        "PWD": str(root / "work"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "TERM": "dumb",
        "NO_COLOR": "1",
        "LANG": "C.UTF-8",
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_CACHE_HOME": str(home / "cache"),
        "XDG_STATE_HOME": str(home / "state"),
        "OPENCODE_DISABLE_AUTOUPDATE": "true",
        "OPENCODE_DISABLE_PRUNE": "true",
        config_name: json.dumps(config),
    }
    for value in values.values():
        if value.startswith(str(root)):
            Path(value).mkdir(parents=True, exist_ok=True)
    return values


def run_turn(
    binary: Path,
    work: Path,
    env: dict[str, str],
    session_id: str | None,
    turn: dict[str, Any],
    files: list[Path] | None = None,
) -> RunEvidence:
    command = [
        str(binary),
        "run",
        str(turn["text"]),
        "--model",
        "fixture/fixture-model",
        "--format",
        "json",
        "--pure",
    ]
    if session_id is None:
        command.extend(["--title", TITLE])
    else:
        command.extend(["--session", session_id])
    for path in files or []:
        command.extend(["--file", str(path)])
    completed = subprocess.run(
        command,
        cwd=work,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return RunEvidence(str(turn["id"]), completed.returncode, completed.stdout, completed.stderr)


def capture(format_name: str, binary: Path, output_dir: Path) -> None:
    binary_info = verify_binary(format_name, binary)
    output_dir.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix=f"session-migrate-{format_name}-") as temporary:
        root = Path(temporary)
        work = root / "work"
        work.mkdir()
        for name in (
            "timeline.py",
            "CORPUS_NOTE.txt",
            "corpus-card.png",
            "corpus-document.pdf",
            "corpus-tone.wav",
            "corpus-transition.mp4",
        ):
            shutil.copy2(ASSETS / name, work / name)
        with loopback(work) as (port, handler):
            config = {
                "model": "fixture/fixture-model",
                "provider": {
                    "fixture": {
                        "npm": "@ai-sdk/openai-compatible",
                        "name": "session-migrate loopback",
                        "options": {
                            "baseURL": f"http://127.0.0.1:{port}/v1",
                            "apiKey": "synthetic-not-a-secret",
                        },
                        "models": {
                            "fixture-model": {
                                "name": "Synthetic fixture model",
                                "attachment": True,
                                "tool_call": True,
                                "limit": {"context": 64_000, "output": 4_096},
                                "modalities": {"input": ["text", "image"], "output": ["text"]},
                            }
                        },
                    }
                },
            }
            env = isolated_env(root, str(EXPECTED[format_name]["config"]), config)
            turns = {str(turn["id"]): turn for turn in SCENARIO["turns"]}
            evidence = [run_turn(binary, work, env, None, turns["inspect"])]
            listed = subprocess.run(
                [str(binary), "session", "list", "--format", "json", "--pure"],
                cwd=work,
                env=env,
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            sessions = json.loads(listed.stdout)
            session_id = str(sessions[0]["id"])
            evidence.append(
                run_turn(
                    binary,
                    work,
                    env,
                    session_id,
                    turns["media"],
                    [
                        work / "corpus-card.png",
                        work / "corpus-document.pdf",
                        work / "corpus-tone.wav",
                        work / "corpus-transition.mp4",
                    ],
                )
            )
            evidence.append(run_turn(binary, work, env, session_id, turns["failure"]))
            evidence.append(run_turn(binary, work, env, session_id, turns["recall"]))

        export_path = output_dir / "export.json"
        with export_path.open("wb") as output:
            exported = subprocess.run(
                [str(binary), "export", session_id, "--pure"],
                cwd=work,
                env=env,
                check=False,
                stdout=output,
                stderr=subprocess.PIPE,
                timeout=30,
            )
        if exported.returncode:
            raise SystemExit(exported.stderr.decode(errors="replace"))
        exported_value = json.loads(export_path.read_text())
        native_parts = [part for message in exported_value["messages"] for part in message["parts"]]
        serialized_parts = json.dumps(native_parts, sort_keys=True)
        report = {
            "format": format_name,
            "binary": binary_info,
            "session_id": session_id,
            "title": TITLE,
            "source_cwd": str(work),
            "export_sha256": hashlib.sha256(export_path.read_bytes()).hexdigest(),
            "requests": len(handler.requests),
            "turns": [
                {
                    "id": item.turn,
                    "returncode": item.returncode,
                    "stdout_sha256": hashlib.sha256(item.stdout.encode()).hexdigest(),
                    "stderr": item.stderr,
                }
                for item in evidence
            ],
            "media": {
                "image/png": {
                    "accepted": any(
                        part.get("type") == "file" and part.get("mime") == "image/png"
                        for part in native_parts
                    )
                },
                "application/pdf": {
                    "accepted": any(
                        part.get("type") == "file" and part.get("mime") == "application/pdf"
                        for part in native_parts
                    )
                },
                "audio/wav": {
                    "accepted": False,
                    "error": "Cannot read binary file",
                    "observed": "corpus-tone.wav" in serialized_parts
                    and "Cannot read binary file" in serialized_parts,
                },
                "video/mp4": {
                    "accepted": False,
                    "error": "Cannot read binary file",
                    "observed": "corpus-transition.mp4" in serialized_parts
                    and "Cannot read binary file" in serialized_parts,
                },
            },
        }
        (output_dir / "capture-report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--format", choices=sorted(EXPECTED), required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    capture(args.format, args.binary, args.output_dir)


if __name__ == "__main__":
    main()
