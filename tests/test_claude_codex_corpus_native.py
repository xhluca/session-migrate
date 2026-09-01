"""Native-corpus checks for exact Claude Code 2.1.209 and Codex 0.144.4."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from native_corpus.loader import load_standalone_fixture
from native_corpus.route_oracle import assert_source_expectations, parse_native_fixture

from session_migrate.conversion import (
    ConversionOptions,
    convert_session,
    target_import_paths,
    write_artifact,
)
from session_migrate.formats import claude, codex
from session_migrate.model import EventKind, Role, TargetFormat

CLAUDE_ID = "73fea258-9467-4a17-877b-ef6bcd0898b7"
CODEX_ID = "01a05a3a-c543-7dd3-922d-44935ac19894"
FOLLOWUP = "SANITIZED_NATIVE_CORPUS_RELOAD_FOLLOWUP"
REPLY = "SANITIZED_NATIVE_CORPUS_RELOAD_OK"
FIXTURE_ROOT = Path(__file__).parent / "native_corpus/v1/sources"
ASSETS = Path(__file__).parent / "native_corpus/v1/assets"


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
        if self.path.startswith("/v1/messages"):
            payload = self._claude_events()
        elif self.path.startswith("/v1/responses"):
            payload = self._codex_events()
        else:
            self.send_error(404)
            return
        encoded = payload.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    @staticmethod
    def _claude_events() -> str:
        events = [
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_native_corpus_reload",
                        "type": "message",
                        "role": "assistant",
                        "model": "claude-haiku-4-5-20251001",
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {
                            "input_tokens": 10,
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0,
                            "output_tokens": 1,
                        },
                    },
                },
            ),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": REPLY},
                },
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 3},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ]
        return "".join(f"event: {name}\ndata: {json.dumps(value)}\n\n" for name, value in events)

    @staticmethod
    def _codex_events() -> str:
        item = {
            "id": "msg_native_corpus_reload",
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": REPLY, "annotations": []}],
        }
        response = {
            "id": "resp_native_corpus_reload",
            "object": "response",
            "created_at": 1788218000,
            "status": "completed",
            "completed_at": 1788218001,
            "error": None,
            "incomplete_details": None,
            "instructions": None,
            "max_output_tokens": None,
            "model": "fixture-model",
            "output": [item],
            "parallel_tool_calls": True,
            "previous_response_id": None,
            "reasoning": {"effort": "low", "summary": None},
            "store": False,
            "temperature": None,
            "text": {"format": {"type": "text"}},
            "tool_choice": "auto",
            "tools": [],
            "top_p": None,
            "truncation": "disabled",
            "usage": {
                "input_tokens": 10,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 3,
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": 13,
            },
            "user": None,
            "metadata": {},
        }
        events = [
            {
                "type": "response.created",
                "response": {**response, "status": "in_progress", "output": []},
            },
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {**item, "status": "in_progress", "content": []},
            },
            {
                "type": "response.content_part.added",
                "item_id": item["id"],
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            },
            {
                "type": "response.output_text.delta",
                "item_id": item["id"],
                "output_index": 0,
                "content_index": 0,
                "delta": REPLY,
            },
            {
                "type": "response.output_text.done",
                "item_id": item["id"],
                "output_index": 0,
                "content_index": 0,
                "text": REPLY,
            },
            {
                "type": "response.content_part.done",
                "item_id": item["id"],
                "output_index": 0,
                "content_index": 0,
                "part": item["content"][0],
            },
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {"type": "response.completed", "response": response},
        ]
        return "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"


def _binary(variable: str, version: str, digest: str) -> Path:
    value = os.environ.get(variable)
    if not value:
        pytest.skip(f"set {variable} to the exact pinned native binary")
    binary = Path(value)
    completed = subprocess.run(
        [str(binary), "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode == 0
    assert version in completed.stdout.strip()
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == digest
    return binary


def _directories(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    paths = tuple(tmp_path / name for name in ("client", "work", "system-home"))
    for path in paths:
        path.mkdir(mode=0o700)
    client, work, system_home = paths
    return client, work, tmp_path, system_home


def _bwrap(root: Path, work: Path) -> list[str]:
    binary = shutil.which("bwrap")
    if binary is None:
        pytest.skip("bubblewrap is required to materialize the canonical /fixture/work CWD")
    arguments = [binary, "--tmpfs", "/", "--dev", "/dev", "--proc", "/proc"]
    for directory in ("/usr", "/lib", "/lib64", "/tmp", "/dev/shm", "/etc"):
        if Path(directory).exists():
            arguments.extend(("--ro-bind", directory, directory))
    arguments.extend(
        (
            "--bind",
            str(root),
            str(root),
            "--dir",
            "/fixture",
            "--bind",
            str(work),
            "/fixture/work",
            "--chdir",
            "/fixture/work",
        )
    )
    return arguments


def _provider() -> tuple[Provider, threading.Thread]:
    provider = Provider(("127.0.0.1", 0), Handler)
    provider.requests = []
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    return provider, thread


@pytest.mark.parametrize(("format_name", "version"), (("claude", "2.1.209"), ("codex", "0.144.4")))
def test_sanitized_native_source_matches_reviewed_ir(
    tmp_path: Path, format_name: str, version: str
) -> None:
    fixture = load_standalone_fixture(FIXTURE_ROOT / format_name / version / "portable-rich")
    session = parse_native_fixture(fixture, tmp_path / f"materialized-{format_name}")

    assert_source_expectations(fixture, session)
    assert session.cwd == Path("/fixture/work")
    assert any("SM_CORPUS_7319" in (event.text or "") for event in session.events)
    assert any("COPPER_4821" in (event.text or "") for event in session.events)
    assert fixture.provenance.modalities["user_image"].native_accepted is True
    assert sum(event.kind == EventKind.CONTEXT for event in session.events) >= 2
    if format_name == "claude":
        assert session.title == "repair-event-window-boundary"
        assert sum(event.kind == EventKind.TOOL_CALL for event in session.events) == 3
        assert sum(event.kind == EventKind.TOOL_RESULT for event in session.events) == 3
        assert any(event.payload.get("is_error") is True for event in session.events)
        assert fixture.provenance.modalities["document"].native_accepted is True
        assert "HTTP 400" in fixture.provenance.observations["audio"]
        assert "HTTP 400" in fixture.provenance.observations["video"]
    else:
        assert sum(event.kind == EventKind.TOOL_CALL for event in session.events) == 2
        assert sum(event.kind == EventKind.TOOL_RESULT for event in session.events) == 2
        assert any(
            block.get("type") == "image"
            for event in session.events
            if event.kind == EventKind.TOOL_RESULT
            for block in event.payload.get("content_blocks", [])
        )
        assert fixture.provenance.modalities["document"].native_accepted is False


def test_exact_claude_cold_reloads_sanitized_corpus_source(tmp_path: Path) -> None:
    digest = "b882f4b8b27772f897540df50f24000206f43a9426e8f7d19bd065959b69e9dd"
    binary = _binary("SESSION_MIGRATE_CLAUDE_BIN", claude.PINNED_CLAUDE_VERSION, digest)
    client, work, root, system_home = _directories(tmp_path)
    native = FIXTURE_ROOT / "claude/2.1.209/portable-rich/native" / f"{CLAUDE_ID}.jsonl"
    destination = client / "projects/-fixture-work" / f"{CLAUDE_ID}.jsonl"
    destination.parent.mkdir(parents=True, mode=0o700)
    shutil.copyfile(native, destination)
    os.chmod(destination, 0o600)
    provider, thread = _provider()
    environment = {
        "HOME": str(system_home),
        "CLAUDE_CONFIG_DIR": str(client),
        "ANTHROPIC_API_KEY": "credential-free-loopback",
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{provider.server_address[1]}",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        # Claude creates a per-UID scratch directory. The bubblewrap fixture
        # exposes this test root read-write while the shared host /tmp is
        # intentionally read-only.
        "TMPDIR": str(root),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "TERM": "dumb",
        "NO_COLOR": "1",
    }
    before = destination.read_bytes()
    try:
        completed = subprocess.run(
            _bwrap(root, work)
            + [
                str(binary),
                "--bare",
                "--print",
                "--output-format",
                "stream-json",
                "--verbose",
                "--resume",
                CLAUDE_ID,
                "--model",
                "claude-haiku-4-5-20251001",
                "--disable-slash-commands",
                "--no-chrome",
                FOLLOWUP,
            ],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert completed.returncode == 0, (completed.stdout, completed.stderr)
        after = destination.read_bytes()
        assert len(after) > len(before) and after.startswith(before)
        replay = json.dumps(provider.requests[-1], ensure_ascii=False)
        for marker in ("SM_CORPUS_7319", "COPPER_4821", "ORBIT_2048", FOLLOWUP):
            assert marker in replay
        reparsed = claude.parse(destination)
        assert any(
            event.role == Role.ASSISTANT and event.text == REPLY for event in reparsed.events
        )
    finally:
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=5)


def test_exact_codex_cold_reloads_sanitized_corpus_source(tmp_path: Path) -> None:
    digest = "2b3edc9cdfd1717fba3dbc92817205a8a2c7511d459e456d4817eeff6f78ed7a"
    binary = _binary("SESSION_MIGRATE_CODEX_BIN", codex.PINNED_CODEX_VERSION, digest)
    client, work, root, system_home = _directories(tmp_path)
    native = FIXTURE_ROOT / "codex/0.144.4/portable-rich/native" / f"{CODEX_ID}.jsonl"
    destination = client / "sessions/2026/08/31" / f"rollout-2026-08-31T19-49-56-{CODEX_ID}.jsonl"
    destination.parent.mkdir(parents=True, mode=0o700)
    shutil.copyfile(native, destination)
    os.chmod(destination, 0o600)
    provider, thread = _provider()
    (client / "config.toml").write_text(
        "\n".join(
            (
                'model = "fixture-model"',
                'model_provider = "fixture"',
                "[model_providers.fixture]",
                'name = "Native corpus loopback"',
                f'base_url = "http://127.0.0.1:{provider.server_address[1]}/v1"',
                'env_key = "FIXTURE_API_KEY"',
                'wire_api = "responses"',
                "requires_openai_auth = false",
                "",
            )
        )
    )
    environment = {
        "HOME": str(system_home),
        "CODEX_HOME": str(client),
        "FIXTURE_API_KEY": "credential-free-loopback",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "TERM": "dumb",
        "NO_COLOR": "1",
    }
    before = destination.read_bytes()
    try:
        completed = subprocess.run(
            _bwrap(root, work)
            + [
                str(binary),
                "--disable",
                "apps",
                "--disable",
                "plugins",
                "--disable",
                "remote_plugin",
                "exec",
                "resume",
                CODEX_ID,
                "--ignore-rules",
                "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox",
                "--json",
                FOLLOWUP,
            ],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert completed.returncode == 0, (completed.stdout, completed.stderr)
        after = destination.read_bytes()
        assert len(after) > len(before) and after.startswith(before)
        replay = json.dumps(provider.requests[-1], ensure_ascii=False)
        for marker in ("SM_CORPUS_7319", "COPPER_4821", FOLLOWUP):
            assert marker in replay
        reparsed = codex.parse(destination)
        assert any(
            event.role == Role.ASSISTANT and event.text == REPLY for event in reparsed.events
        )
    finally:
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize(
    ("target_format", "variable", "version", "digest"),
    (
        (
            TargetFormat.CLAUDE,
            "SESSION_MIGRATE_CLAUDE_BIN",
            claude.PINNED_CLAUDE_VERSION,
            "b882f4b8b27772f897540df50f24000206f43a9426e8f7d19bd065959b69e9dd",
        ),
        (
            TargetFormat.CODEX,
            "SESSION_MIGRATE_CODEX_BIN",
            codex.PINNED_CODEX_VERSION,
            "2b3edc9cdfd1717fba3dbc92817205a8a2c7511d459e456d4817eeff6f78ed7a",
        ),
    ),
)
def test_exact_client_resumes_fresh_writer_output(
    tmp_path: Path,
    target_format: TargetFormat,
    variable: str,
    version: str,
    digest: str,
) -> None:
    """Gate target writers separately from cold reloads of captured sources."""

    binary = _binary(variable, version, digest)
    client, work, _root, system_home = _directories(tmp_path)
    source = claude.parse(Path(__file__).parent / "fixtures/claude-2.1.209/basic.jsonl")
    target_id = (
        "82828282-8282-4282-8282-828282828282"
        if target_format == TargetFormat.CLAUDE
        else "01a05b2c-8282-7282-8282-828282828282"
    )
    artifact = convert_session(
        source,
        ConversionOptions(
            target_format=target_format,
            session_id=target_id,
            cwd=work,
            model="claude-haiku-4-5-20251001",
            model_provider="fixture",
        ),
    )
    destination, manifest = target_import_paths(artifact, client)
    write_artifact(artifact, output_path=destination, manifest_path=manifest)
    before = destination.read_bytes()

    provider, thread = _provider()
    try:
        if target_format == TargetFormat.CLAUDE:
            environment = {
                "HOME": str(system_home),
                "CLAUDE_CONFIG_DIR": str(client),
                "ANTHROPIC_API_KEY": "credential-free-loopback",
                "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{provider.server_address[1]}",
                "DISABLE_TELEMETRY": "1",
                "DISABLE_ERROR_REPORTING": "1",
                "DISABLE_AUTOUPDATER": "1",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "TERM": "dumb",
                "NO_COLOR": "1",
            }
            command = [
                str(binary),
                "--bare",
                "--print",
                "--output-format",
                "stream-json",
                "--verbose",
                "--resume",
                target_id,
                "--model",
                "claude-haiku-4-5-20251001",
                "--disable-slash-commands",
                "--no-chrome",
                FOLLOWUP,
            ]
        else:
            (client / "config.toml").write_text(
                "\n".join(
                    (
                        'model = "fixture-model"',
                        'model_provider = "fixture"',
                        "[model_providers.fixture]",
                        'name = "Target writer loopback"',
                        f'base_url = "http://127.0.0.1:{provider.server_address[1]}/v1"',
                        'env_key = "FIXTURE_API_KEY"',
                        'wire_api = "responses"',
                        "requires_openai_auth = false",
                        "",
                    )
                )
            )
            environment = {
                "HOME": str(system_home),
                "CODEX_HOME": str(client),
                "FIXTURE_API_KEY": "credential-free-loopback",
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "TERM": "dumb",
                "NO_COLOR": "1",
            }
            command = [
                str(binary),
                "--disable",
                "apps",
                "--disable",
                "plugins",
                "--disable",
                "remote_plugin",
                "exec",
                "resume",
                target_id,
                "--ignore-rules",
                "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox",
                "--json",
                FOLLOWUP,
            ]

        completed = subprocess.run(
            command,
            cwd=work,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert completed.returncode == 0, (completed.stdout, completed.stderr)
        after = destination.read_bytes()
        assert len(after) > len(before) and after.startswith(before)
        replay = json.dumps(provider.requests[-1], ensure_ascii=False)
        for marker in (
            "Continue after the synthetic compaction.",
            "The synthetic post-compaction fixture is complete.",
            FOLLOWUP,
        ):
            assert marker in replay
        reparsed = (
            claude.parse(destination)
            if target_format == TargetFormat.CLAUDE
            else codex.parse(destination)
        )
        assert any(
            event.role == Role.ASSISTANT and event.text == REPLY for event in reparsed.events
        )
    finally:
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=5)
