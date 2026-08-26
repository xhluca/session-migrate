"""Credential-free native gates for Grok, Kilo Code, and OpenHands.

The default suite exercises the adapters without installing vendor binaries.
Set the corresponding ``SESSION_MIGRATE_*_BIN`` variable to run an exact,
version-and-digest-pinned native import/resume trajectory against a local
OpenAI-compatible server.  No provider credential or network model is used.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from hashlib import file_digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from test_additional_formats import TARGET_UUID, portable_session

from session_migrate.conversion import (
    ConversionOptions,
    convert_session,
    install_grok_artifact,
    install_kilo_artifact,
    install_openhands_artifact,
    kilo_manifest_path,
)
from session_migrate.formats import grok, kilo, openhands
from session_migrate.model import EventKind, TargetFormat


class LoopbackHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []

    def do_GET(self) -> None:  # noqa: N802
        if not self.path.endswith("/models"):
            self.send_error(404)
            return
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
        value = json.loads(self.rfile.read(length))
        type(self).requests.append(value)
        if value.get("stream"):
            chunks = [
                {
                    "id": "chatcmpl-session-migrate-test",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "fixture-model",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "content": "SYNTHETIC_NATIVE_REPLY",
                            },
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": "chatcmpl-session-migrate-test",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "fixture-model",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 2,
                        "total_tokens": 12,
                    },
                },
            ]
            body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
            encoded = (body + "data: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(encoded)))
            self.end_headers()
            with suppress(BrokenPipeError):
                self.wfile.write(encoded)
            return
        self._send_json(
            {
                "id": "chatcmpl-session-migrate-test",
                "object": "chat.completion",
                "created": 0,
                "model": "fixture-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "SYNTHETIC_NATIVE_REPLY",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 2,
                    "total_tokens": 12,
                },
            }
        )

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
def loopback_server() -> Iterator[tuple[int, type[LoopbackHandler]]]:
    handler = LoopbackHandler
    handler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], handler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def exact_binary(
    variable: str,
    *,
    expected_bytes: int,
    expected_sha256: str,
    version_command: list[str],
    expected_version: str,
) -> Path:
    value = os.environ.get(variable)
    if not value:
        pytest.skip(f"set {variable} to the exact pinned vendor binary")
    path = Path(value).resolve()
    assert path.is_file() and not path.is_symlink()
    assert path.stat().st_size == expected_bytes
    with path.open("rb") as stream:
        assert file_digest(stream, "sha256").hexdigest() == expected_sha256
    completed = subprocess.run(
        [str(path), *version_command],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
        env={
            "HOME": str(path.parent),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "OPENHANDS_SUPPRESS_BANNER": "1",
        },
    )
    assert completed.returncode == 0, completed.stderr
    assert expected_version in completed.stdout.strip()
    return path


def isolated_env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    values = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "TERM": "dumb",
        "NO_COLOR": "1",
        "LANG": "C.UTF-8",
        "TMPDIR": str(tmp_path / "tmp"),
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_CACHE_HOME": str(home / "cache"),
        "XDG_STATE_HOME": str(home / "state"),
        "OPENCODE_DISABLE_AUTOUPDATE": "true",
        "OPENCODE_DISABLE_PRUNE": "true",
    }
    for value in values.values():
        if value.startswith(str(tmp_path)):
            Path(value).mkdir(parents=True, exist_ok=True)
    return values


def assert_request_markers(requests: list[dict[str, Any]], *markers: str) -> None:
    assert requests
    replay = json.dumps(requests, sort_keys=True)
    for marker in markers:
        assert marker in replay


def test_grok_105_loads_prefix_and_appends_through_loopback(tmp_path: Path) -> None:
    binary = exact_binary(
        "SESSION_MIGRATE_GROK_BIN",
        expected_bytes=grok.PINNED_GROK_LINUX_X64_BYTES,
        expected_sha256=grok.PINNED_GROK_LINUX_X64_SHA256,
        version_command=["--version"],
        expected_version=f"grok {grok.PINNED_GROK_VERSION}",
    )
    work = tmp_path / "work"
    work.mkdir()
    artifact = convert_session(
        portable_session(work, compaction=True),
        ConversionOptions(
            target_format=TargetFormat.GROK,
            session_id=TARGET_UUID,
            cwd=work,
            model="fixture-model",
        ),
    )
    grok_home = tmp_path / "grok"
    session_path, _ = install_grok_artifact(artifact, target_home=grok_home)
    updates = session_path / "updates.jsonl"
    before = updates.read_bytes()

    with loopback_server() as (port, handler):
        (grok_home / "config.toml").write_text(
            "\n".join(
                [
                    "[models]",
                    'default = "fixture-model"',
                    "[model.fixture-model]",
                    'model = "fixture-model"',
                    f'base_url = "http://127.0.0.1:{port}/v1"',
                    'api_key = "synthetic-not-a-secret"',
                    "context_window = 65536",
                    "",
                ]
            )
        )
        completed = subprocess.run(
            [
                str(binary),
                "--resume",
                artifact.session_id,
                "--cwd",
                str(work),
                "--model",
                "fixture-model",
                "-p",
                "SYNTHETIC_GROK_FOLLOWUP",
                "--max-turns",
                "1",
                "--output-format",
                "plain",
            ],
            cwd=work,
            env={**isolated_env(tmp_path), "GROK_HOME": str(grok_home)},
            check=False,
            capture_output=True,
            text=True,
            timeout=45,
        )

    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    assert "SYNTHETIC_NATIVE_REPLY" in completed.stdout
    assert updates.read_bytes().startswith(before)
    assert_request_markers(
        handler.requests,
        "SYNTHETIC_COMPACTION_MARKER",
        "SYNTHETIC_FINAL_MARKER",
        "SYNTHETIC_GROK_FOLLOWUP",
    )
    resumed = grok.parse_session(session_path)
    assert any(
        event.kind == EventKind.MESSAGE and event.text == "SYNTHETIC_NATIVE_REPLY"
        for event in resumed.events
    )


def test_kilo_750_official_import_replay_and_export(tmp_path: Path) -> None:
    binary = exact_binary(
        "SESSION_MIGRATE_KILO_BIN",
        expected_bytes=kilo.PINNED_KILO_LINUX_X64_BYTES,
        expected_sha256=kilo.PINNED_KILO_LINUX_X64_SHA256,
        version_command=["--version"],
        expected_version=kilo.PINNED_KILO_VERSION,
    )
    work = tmp_path / "work"
    work.mkdir()
    env = isolated_env(tmp_path)
    artifact = convert_session(
        portable_session(work, compaction=True),
        ConversionOptions(
            target_format=TargetFormat.KILO,
            session_id=TARGET_UUID,
            cwd=work,
            model="fixture-model",
            model_provider="fixture",
        ),
    )
    manifest = kilo_manifest_path(artifact, state_home=Path(env["XDG_STATE_HOME"]))
    install_kilo_artifact(
        artifact,
        manifest_path=manifest,
        target_cli=binary,
        environ=env,
    )

    with loopback_server() as (port, handler):
        config = {
            "model": "fixture/fixture-model",
            "provider": {
                "fixture": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "Synthetic loopback",
                    "options": {
                        "baseURL": f"http://127.0.0.1:{port}/v1",
                        "apiKey": "synthetic-not-a-secret",
                    },
                    "models": {
                        "fixture-model": {
                            "name": "Synthetic fixture model",
                            "attachment": True,
                            "modalities": {
                                "input": ["text", "image"],
                                "output": ["text"],
                            },
                        }
                    },
                }
            },
        }
        completed = subprocess.run(
            [
                str(binary),
                "run",
                "SYNTHETIC_KILO_FOLLOWUP",
                "--session",
                artifact.session_id,
                "--model",
                "fixture/fixture-model",
                "--format",
                "json",
                "--pure",
            ],
            cwd=work,
            env={**env, "KILO_CONFIG_CONTENT": json.dumps(config)},
            check=False,
            capture_output=True,
            text=True,
            timeout=45,
        )

    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    assert "SYNTHETIC_NATIVE_REPLY" in completed.stdout
    assert_request_markers(
        handler.requests,
        "SYNTHETIC_COMPACTION_MARKER",
        "SYNTHETIC_FINAL_MARKER",
        "SYNTHETIC_KILO_FOLLOWUP",
    )
    exported = subprocess.run(
        [str(binary), "export", artifact.session_id, "--pure"],
        cwd=work,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert exported.returncode == 0, exported.stderr
    export_path = tmp_path / "kilo-export.json"
    export_path.write_text(exported.stdout)
    replay = kilo.parse_session(export_path)
    assert replay.cwd == work
    assert any(event.text == "SYNTHETIC_KILO_FOLLOWUP" for event in replay.events)
    assert any(event.text == "SYNTHETIC_NATIVE_REPLY" for event in replay.events)


def test_openhands_1160_loads_prefix_and_appends_through_loopback(
    tmp_path: Path,
) -> None:
    binary = exact_binary(
        "SESSION_MIGRATE_OPENHANDS_BIN",
        expected_bytes=openhands.PINNED_OPENHANDS_LINUX_X64_BYTES,
        expected_sha256=openhands.PINNED_OPENHANDS_LINUX_X64_SHA256,
        version_command=["--version"],
        expected_version=f"OpenHands CLI {openhands.PINNED_OPENHANDS_VERSION}",
    )
    # OpenHands scans ancestors for skills.  Pytest's /tmp parent can contain
    # unrelated, permission-restricted Unix sockets, so keep the native oracle
    # in the checked-out repository while all writable state remains isolated.
    work = Path.cwd().resolve()
    artifact = convert_session(
        portable_session(work, compaction=True),
        ConversionOptions(
            target_format=TargetFormat.OPENHANDS,
            session_id=TARGET_UUID,
            cwd=work,
            model="openai/fixture-model",
        ),
    )
    conversations = tmp_path / "conversations"
    events_path, _ = install_openhands_artifact(artifact, target_home=conversations)
    bundle = openhands.validate_native_bytes(artifact.native_bytes, artifact.session_id)
    assert bundle.cwd == work
    assert bundle.model == "openai/fixture-model"
    assert bundle.title == "SYNTHETIC_IMPORTED_NAME"
    assert bundle.picker_title == "SYNTHETIC_USER_MARKER"
    assert bundle.cli_version == openhands.PINNED_OPENHANDS_VERSION
    assert bundle.base_state_policy == openhands.OPENHANDS_BASE_STATE_POLICY
    assert not (events_path.parent / "base_state.json").exists()
    prefix = {path.name: path.read_bytes() for path in sorted(events_path.glob("event-*.json"))}

    with loopback_server() as (port, handler):
        completed = subprocess.run(
            [
                str(binary),
                "--resume",
                artifact.session_id,
                "--headless",
                "--json",
                "--override-with-envs",
                "--exit-without-confirmation",
                "-t",
                "SYNTHETIC_OPENHANDS_FOLLOWUP",
            ],
            cwd=work,
            env={
                **isolated_env(tmp_path),
                "OPENHANDS_CONVERSATIONS_DIR": str(conversations),
                "OPENHANDS_SUPPRESS_BANNER": "1",
                "LLM_API_KEY": "synthetic-not-a-secret",
                "LLM_BASE_URL": f"http://127.0.0.1:{port}/v1",
                "LLM_MODEL": "openai/fixture-model",
            },
            check=False,
            capture_output=True,
            text=True,
            timeout=45,
        )

    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    assert "SYNTHETIC_NATIVE_REPLY" in completed.stdout
    for name, value in prefix.items():
        assert (events_path / name).read_bytes() == value
    assert_request_markers(
        handler.requests,
        "SYNTHETIC_USER_MARKER",
        "synthetic_call_1",
        "SYNTHETIC_TOOL_RESULT",
        "SYNTHETIC_OPENHANDS_FOLLOWUP",
    )
    base_state = json.loads((events_path.parent / "base_state.json").read_text())
    assert base_state["id"] == artifact.session_id
    assert base_state["workspace"]["working_dir"] == str(work)
    assert base_state["agent"]["llm"]["model"] == "openai/fixture-model"
    assert "title" not in base_state
    assert "cli_version" not in base_state
    resumed = openhands.parse_session(events_path)
    assert resumed.title == "SYNTHETIC_USER_MARKER"
    assert resumed.cwd == work
    assert resumed.model == "openai/fixture-model"
    assert any(event.text == "SYNTHETIC_OPENHANDS_FOLLOWUP" for event in resumed.events)
    assert any(event.text == "SYNTHETIC_NATIVE_REPLY" for event in resumed.events)
