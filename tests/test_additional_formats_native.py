import json
import os
import shutil
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from test_additional_formats import (
    IMAGE_URL,
    TARGET_OPENCODE_ID,
    TARGET_UUID,
    TOOL_IMAGE_URL,
    portable_session,
)

from session_bridge.cli import main
from session_bridge.formats import opencode, pi
from session_bridge.model import Event, EventKind, Provenance, Role, Session

OPENCODE_FALLBACK = Path("/home/nlp/users/xlu41/.opencode/bin/opencode")


def exact_binary(name: str, version: str, fallback: Path | None = None) -> str:
    candidate = shutil.which(name)
    if candidate is None and fallback is not None and fallback.is_file():
        candidate = str(fallback)
    if candidate is None:
        pytest.skip(f"{name} is not installed")
    completed = subprocess.run(
        [candidate, "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if completed.returncode != 0 or completed.stdout.strip() != version:
        pytest.skip(f"native oracle requires {name} {version}")
    return candidate


def isolated_env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "TERM": "dumb",
        "NO_COLOR": "1",
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_CACHE_HOME": str(home / "cache"),
        "XDG_STATE_HOME": str(home / "state"),
        "OPENCODE_CONFIG_DIR": str(home / "opencode-config"),
        "OPENCODE_DISABLE_AUTOUPDATE": "true",
        "OPENCODE_DISABLE_PRUNE": "true",
        "PI_CODING_AGENT_DIR": str(home / "pi-agent"),
        "PI_OFFLINE": "1",
    }
    for path in (
        home,
        Path(env["XDG_DATA_HOME"]),
        Path(env["XDG_CONFIG_HOME"]),
        Path(env["XDG_CACHE_HOME"]),
        Path(env["XDG_STATE_HOME"]),
        Path(env["OPENCODE_CONFIG_DIR"]),
        Path(env["PI_CODING_AGENT_DIR"]),
    ):
        path.mkdir(parents=True, exist_ok=True)
    return env


def native_pi_session(tmp_path: Path) -> Session:
    base = portable_session(tmp_path)
    compact = Event(
        kind=EventKind.COMPACTION,
        role=Role.SYSTEM,
        text="SYNTHETIC_COMPACTION_MARKER",
        timestamp="2026-08-18T12:00:03Z",
        provenance=Provenance(3, "compact"),
    )
    active_events = (
        Event(
            kind=EventKind.MESSAGE,
            role=Role.USER,
            text="SYNTHETIC_POST_COMPACTION_USER",
            timestamp="2026-08-18T12:00:04Z",
            provenance=Provenance(4, "user", block_index=0),
        ),
        Event(
            kind=EventKind.CONTEXT,
            role=Role.USER,
            timestamp="2026-08-18T12:00:04Z",
            payload={"block_type": "image", "image_url": IMAGE_URL},
            provenance=Provenance(4, "user", block_index=1),
        ),
        Event(
            kind=EventKind.MESSAGE,
            role=Role.ASSISTANT,
            text="SYNTHETIC_POST_COMPACTION_ASSISTANT",
            timestamp="2026-08-18T12:00:05Z",
            provenance=Provenance(5, "assistant", block_index=0),
        ),
        Event(
            kind=EventKind.TOOL_CALL,
            role=Role.ASSISTANT,
            tool_name="read",
            tool_call_id="synthetic_call_2",
            timestamp="2026-08-18T12:00:05Z",
            payload={"input": {"path": "post-compaction.txt"}},
            provenance=Provenance(5, "assistant", block_index=1),
        ),
        Event(
            kind=EventKind.TOOL_RESULT,
            role=Role.TOOL,
            text="SYNTHETIC_POST_COMPACTION_RESULT",
            tool_name="read",
            tool_call_id="synthetic_call_2",
            timestamp="2026-08-18T12:00:06Z",
            payload={
                "is_error": False,
                "content_blocks": [
                    {"type": "text", "text": "SYNTHETIC_POST_COMPACTION_RESULT"},
                    {"type": "image", "image_url": TOOL_IMAGE_URL},
                ],
            },
            provenance=Provenance(6, "user", block_index=0),
        ),
        Event(
            kind=EventKind.MESSAGE,
            role=Role.ASSISTANT,
            text="SYNTHETIC_POST_COMPACTION_FINAL",
            timestamp="2026-08-18T12:00:07Z",
            provenance=Provenance(7, "assistant"),
        ),
    )
    return Session(
        source_format=base.source_format,
        source_path=base.source_path,
        source_sha256=base.source_sha256,
        session_id=base.session_id,
        cwd=base.cwd,
        started_at=base.started_at,
        cli_version=base.cli_version,
        model=base.model,
        title=base.title,
        events=(*base.events[:-1], compact, *active_events),
        raw_record_count=12,
    )


def rpc_responses(stdout: str) -> dict[str, dict[str, Any]]:
    responses: dict[str, dict[str, Any]] = {}
    for line in stdout.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("type") == "response":
            responses[str(value.get("command"))] = value
    return responses


def test_pi_0806_loads_compaction_images_and_tools_via_offline_rpc(
    tmp_path: Path,
) -> None:
    binary = exact_binary("pi", pi.PINNED_PI_VERSION)
    work = tmp_path / "work"
    work.mkdir()
    source = native_pi_session(work)
    data, dropped = pi.serialize(
        source,
        session_id=TARGET_UUID,
        cwd=work,
        timestamp="2026-08-18T12:00:00Z",
    )
    pi.validate_native_bytes(data, TARGET_UUID)
    session_path = tmp_path / "native-pi.jsonl"
    session_path.write_bytes(data)
    source_records = [json.loads(line) for line in data.decode().splitlines()]
    commands = "\n".join(
        [
            json.dumps({"type": "get_messages"}),
            json.dumps({"type": "get_entries"}),
            json.dumps({"type": "set_session_name", "name": "SYNTHETIC_NATIVE_NAME"}),
        ]
    )

    completed = subprocess.run(
        [
            binary,
            "--mode",
            "rpc",
            "--offline",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-context-files",
            "--session",
            str(session_path),
            "--session-dir",
            str(tmp_path / "sessions"),
        ],
        cwd=work,
        env=isolated_env(tmp_path),
        input=commands + "\n",
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert dropped == {}
    responses = rpc_responses(completed.stdout)
    assert set(responses) >= {"get_messages", "get_entries", "set_session_name"}
    assert all(response["success"] is True for response in responses.values())

    messages = responses["get_messages"]["data"]["messages"]
    assert [message["role"] for message in messages] == [
        "compactionSummary",
        "user",
        "assistant",
        "toolResult",
        "assistant",
    ]
    serialized_messages = json.dumps(messages, sort_keys=True)
    for marker in (
        "SYNTHETIC_COMPACTION_MARKER",
        "SYNTHETIC_POST_COMPACTION_USER",
        "SYNTHETIC_POST_COMPACTION_ASSISTANT",
        "synthetic_call_2",
        "post-compaction.txt",
        "SYNTHETIC_POST_COMPACTION_RESULT",
        "SYNTHETIC_POST_COMPACTION_FINAL",
        "c3ludGhldGlj",
        "dG9vbC1pbWFnZQ==",
    ):
        assert marker in serialized_messages

    entries = responses["get_entries"]["data"]["entries"]
    assert entries[: len(source_records) - 1] == source_records[1:]
    on_disk = [json.loads(line) for line in session_path.read_text().splitlines()]
    assert on_disk[: len(source_records)] == source_records
    assert {entry["type"] for entry in on_disk[len(source_records) :]} <= {
        "thinking_level_change",
        "session_info",
    }
    assert on_disk[-1]["type"] == "session_info"
    assert on_disk[-1]["name"] == "SYNTHETIC_NATIVE_NAME"


class LoopbackHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        value = json.loads(self.rfile.read(length))
        type(self).requests.append(value)
        chunks = [
            {
                "id": "chatcmpl-synthetic",
                "object": "chat.completion.chunk",
                "created": 1787054408,
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
                "id": "chatcmpl-synthetic",
                "object": "chat.completion.chunk",
                "created": 1787054408,
                "model": "fixture-model",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            },
        ]
        body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        body += "data: [DONE]\n\n"
        encoded = body.encode()
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


@contextmanager
def loopback_server() -> Iterator[tuple[ThreadingHTTPServer, type[LoopbackHandler]]]:
    handler = LoopbackHandler
    handler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, handler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_opencode_11720_official_import_and_loopback_resume(tmp_path: Path) -> None:
    binary = exact_binary("opencode", opencode.PINNED_OPENCODE_VERSION, OPENCODE_FALLBACK)
    work = tmp_path / "work"
    work.mkdir()
    env = isolated_env(tmp_path)
    data, dropped = opencode.serialize(
        portable_session(work),
        session_id=TARGET_OPENCODE_ID,
        cwd=work,
        timestamp="2026-08-18T12:00:00Z",
        provider_id="fixture",
    )
    opencode.validate_native_bytes(data, TARGET_OPENCODE_ID)
    bundle = tmp_path / "opencode-import.json"
    bundle.write_bytes(data)

    imported = subprocess.run(
        [binary, "import", str(bundle), "--pure"],
        cwd=work,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert imported.returncode == 0, imported.stderr
    assert dropped == {}

    listed = subprocess.run(
        [binary, "session", "list", "--format", "json", "--pure"],
        cwd=work,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert listed.returncode == 0, listed.stderr
    assert TARGET_OPENCODE_ID in {item["id"] for item in json.loads(listed.stdout)}

    exported_before = subprocess.run(
        [binary, "export", TARGET_OPENCODE_ID, "--pure"],
        cwd=work,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert exported_before.returncode == 0, exported_before.stderr
    before = json.loads(exported_before.stdout)
    opencode.validate_native_bytes(
        (json.dumps(before) + "\n").encode(), TARGET_OPENCODE_ID
    )
    assert "SYNTHETIC_TOOL_RESULT" in json.dumps(before)

    with loopback_server() as (server, handler):
        port = server.server_address[1]
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
        resume_env = {**env, "OPENCODE_CONFIG_CONTENT": json.dumps(config)}
        resumed = subprocess.run(
            [
                binary,
                "run",
                "SYNTHETIC_FOLLOWUP_MARKER",
                "--session",
                TARGET_OPENCODE_ID,
                "--model",
                "fixture/fixture-model",
                "--format",
                "json",
                "--pure",
            ],
            cwd=work,
            env=resume_env,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )

    assert resumed.returncode == 0, (
        resumed.stdout,
        resumed.stderr,
        handler.requests,
    )
    assert handler.requests, (resumed.stdout, resumed.stderr)
    replay = json.dumps(handler.requests, sort_keys=True)
    for marker in (
        "SYNTHETIC_USER_MARKER",
        "SYNTHETIC_ASSISTANT_MARKER",
        "synthetic_call_1",
        "fixture.txt",
        "SYNTHETIC_TOOL_RESULT",
        "SYNTHETIC_FINAL_MARKER",
        "SYNTHETIC_FOLLOWUP_MARKER",
        "c3ludGhldGlj",
        "dG9vbC1pbWFnZQ==",
    ):
        assert marker in replay

    exported_after = subprocess.run(
        [binary, "export", TARGET_OPENCODE_ID, "--pure"],
        cwd=work,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert exported_after.returncode == 0, exported_after.stderr
    after = json.loads(exported_after.stdout)
    assert [message["info"]["role"] for message in after["messages"]] == [
        "user",
        "assistant",
        "assistant",
        "user",
        "assistant",
    ]
    after_text = json.dumps(after, sort_keys=True)
    assert "SYNTHETIC_FOLLOWUP_MARKER" in after_text
    assert "SYNTHETIC_NATIVE_REPLY" in after_text


def test_opencode_cli_import_uses_official_importer_and_rejects_native_collision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    binary = exact_binary("opencode", opencode.PINNED_OPENCODE_VERSION, OPENCODE_FALLBACK)
    work = tmp_path / "work"
    work.mkdir()
    env = isolated_env(tmp_path)
    temporary_root = tmp_path / "temporary"
    temporary_root.mkdir()
    env["TMPDIR"] = str(temporary_root)
    for key in tuple(os.environ):
        if key.startswith("OPENCODE_"):
            monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    fixture = Path(__file__).parent / "fixtures" / "claude-2.1.209" / "basic.jsonl"
    command = [
        "import",
        str(fixture),
        "--to",
        "opencode",
        "--target-cli",
        binary,
        "--session-id",
        TARGET_UUID,
        "--cwd",
        str(work),
    ]
    bridge_manifest = (
        Path(env["XDG_STATE_HOME"])
        / "session-bridge/manifests/opencode"
        / f"{TARGET_OPENCODE_ID}.json"
    )

    assert main([*command, "--dry-run"]) == 0
    dry_result = json.loads(capsys.readouterr().out)
    assert dry_result["output"] == f"opencode:{TARGET_OPENCODE_ID}"
    assert dry_result["dry_run"] is True
    assert not bridge_manifest.exists()
    assert not list(temporary_root.glob("session-bridge-opencode-*"))

    listed_before = subprocess.run(
        [binary, "session", "list", "--format", "json", "--pure"],
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert listed_before.returncode == 0, listed_before.stderr
    listed_before_value = (
        json.loads(listed_before.stdout) if listed_before.stdout.strip() else []
    )
    assert TARGET_OPENCODE_ID not in {
        item["id"] for item in listed_before_value
    }

    assert main(command) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["output"] == f"opencode:{TARGET_OPENCODE_ID}"
    assert result["manifest"] == str(bridge_manifest)
    assert result["dry_run"] is False
    assert bridge_manifest.stat().st_mode & 0o777 == 0o600
    manifest = json.loads(bridge_manifest.read_text())
    assert manifest["target"]["path"] == f"opencode:{TARGET_OPENCODE_ID}"
    assert manifest["target"]["session_id"] == TARGET_OPENCODE_ID
    assert set(manifest) == {
        "bridge_version",
        "created_at",
        "dropped_events",
        "schema_version",
        "source",
        "target",
        "warnings",
    }
    assert not list(temporary_root.glob("session-bridge-opencode-*"))

    exported = subprocess.run(
        [binary, "export", TARGET_OPENCODE_ID, "--pure"],
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert exported.returncode == 0, exported.stderr
    exported_bytes = (json.dumps(json.loads(exported.stdout)) + "\n").encode()
    opencode.validate_native_bytes(exported_bytes, TARGET_OPENCODE_ID)

    assert main([*command, "--dry-run"]) == 2
    assert "refusing to overwrite native session" in capsys.readouterr().err
    assert not list(temporary_root.glob("session-bridge-opencode-*"))


def test_opencode_native_replay_preserves_source_order_with_decreasing_timestamps(
    tmp_path: Path,
) -> None:
    binary = exact_binary("opencode", opencode.PINNED_OPENCODE_VERSION, OPENCODE_FALLBACK)
    work = tmp_path / "work"
    work.mkdir()
    env = isolated_env(tmp_path)
    base = portable_session(work)
    source = replace(
        base,
        events=(
            Event(
                kind=EventKind.MESSAGE,
                role=Role.USER,
                text="SYNTHETIC_ORDER_FIRST",
                timestamp="2026-08-18T12:00:02Z",
                provenance=Provenance(0, "user"),
            ),
            Event(
                kind=EventKind.MESSAGE,
                role=Role.ASSISTANT,
                text="SYNTHETIC_ORDER_SECOND",
                timestamp="2026-08-18T12:00:01Z",
                provenance=Provenance(1, "assistant"),
            ),
        ),
        raw_record_count=2,
    )
    data, dropped = opencode.serialize(
        source,
        session_id=TARGET_OPENCODE_ID,
        cwd=work,
        provider_id="fixture",
    )
    bundle = tmp_path / "ordered-import.json"
    bundle.write_bytes(data)

    imported = subprocess.run(
        [binary, "import", str(bundle), "--pure"],
        cwd=work,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert imported.returncode == 0, imported.stderr
    assert dropped == {"timestamp:native_order_adjusted": 1}

    with loopback_server() as (server, handler):
        port = server.server_address[1]
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
                    "models": {"fixture-model": {"name": "Synthetic fixture model"}},
                }
            },
        }
        resumed = subprocess.run(
            [
                binary,
                "run",
                "SYNTHETIC_ORDER_FOLLOWUP",
                "--session",
                TARGET_OPENCODE_ID,
                "--model",
                "fixture/fixture-model",
                "--format",
                "json",
                "--pure",
            ],
            cwd=work,
            env={**env, "OPENCODE_CONFIG_CONTENT": json.dumps(config)},
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )

    assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
    assert handler.requests
    replay = json.dumps(handler.requests)
    assert replay.index("SYNTHETIC_ORDER_FIRST") < replay.index(
        "SYNTHETIC_ORDER_SECOND"
    ) < replay.index("SYNTHETIC_ORDER_FOLLOWUP")
