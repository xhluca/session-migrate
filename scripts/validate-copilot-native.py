#!/usr/bin/env python3
"""Credential-free native Copilot cold-resume validation over real sessions.

The harness prints only anonymous counts. Conversation content is sent only to
an in-process loopback provider, retained in memory for equality checks, and
discarded when each check completes.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import threading
import uuid
from collections import Counter
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from session_bridge.conversion import (
    ConversionOptions,
    convert_session,
    install_copilot_artifact,
)
from session_bridge.errors import SessionBridgeError
from session_bridge.formats import claude, codex, copilot, pi
from session_bridge.formats.common import portable_data_image
from session_bridge.model import AgentFormat, Event, EventKind, Role, Session, TargetFormat

FOLLOWUP = "SYNTHETIC_COPILOT_NATIVE_FOLLOWUP"
REPLY = "SYNTHETIC_COPILOT_NATIVE_REPLY"


@dataclass(frozen=True, slots=True)
class SessionSummary:
    ordinal: int
    features: tuple[str, ...]
    source_bytes: int


class _Provider(ThreadingHTTPServer):
    request_value: dict[str, Any] | None = None


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        try:
            value = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_error(400)
            return
        if isinstance(value, dict):
            self.server.request_value = value  # type: ignore[attr-defined]
        chunks = [
            {
                "id": "session-bridge-native",
                "object": "chat.completion.chunk",
                "created": 1787097600,
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
                "id": "session-bridge-native",
                "object": "chat.completion.chunk",
                "created": 1787097600,
                "model": "fixture-model",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 3,
                    "total_tokens": 13,
                },
            },
        ]
        payload = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        encoded = (payload + "data: [DONE]\n\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def main() -> int:
    parser = argparse.ArgumentParser()
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--claude-root", type=Path)
    sources.add_argument("--codex-root", type=Path)
    sources.add_argument("--pi-root", type=Path)
    parser.add_argument("--copilot-bin", type=Path, required=True)
    parser.add_argument("--count", type=int, default=10)
    args = parser.parse_args()

    source_format, source_root = _selected_source(args)
    files = _source_files(source_format, source_root)
    inventory: list[SessionSummary] = []
    rejected: Counter[str] = Counter()
    for ordinal, path in enumerate(files, start=1):
        try:
            session = _load_source(path, source_format)
        except SessionBridgeError as exc:
            rejection = _expected_rejection(source_format, exc)
            if rejection:
                rejected[rejection] += 1
                continue
            raise RuntimeError(f"source parse failed at anonymous session {ordinal}") from None
        size = path.stat().st_size
        inventory.append(SessionSummary(ordinal, _features(session, size), size))
    selected = _select(inventory, args.count)
    features: Counter[str] = Counter()

    with tempfile.TemporaryDirectory(prefix="session-bridge-copilot-native-") as name:
        root = Path(name)
        os.chmod(root, 0o700)
        provider = _Provider(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=provider.serve_forever, daemon=True)
        thread.start()
        try:
            _require_version(args.copilot_bin, root)
            for item in selected:
                session = _load_source(files[item.ordinal - 1], source_format)
                _check_one(
                    args.copilot_bin,
                    provider,
                    root,
                    item.ordinal,
                    session,
                )
                features.update(item.features)
        finally:
            provider.shutdown()
            provider.server_close()
            thread.join(timeout=5)

    result = {
        "source_files": len(files),
        "source_format": source_format.value,
        "parsed_sessions": len(inventory),
        "expected_rejections": dict(sorted(rejected.items())),
        "selected_sessions": len(selected),
        "native_cold_resumes": len(selected),
        "exact_generated_prefixes": len(selected),
        "derived_sqlite_rebuilt": len(selected),
        "portable_replay_value_checks": len(selected),
        "feature_counts": dict(sorted(features.items())),
        "credentials_inherited": False,
        "provider": "loopback_openai_completions",
        "copilot_version": copilot.PINNED_COPILOT_VERSION,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _selected_source(args: argparse.Namespace) -> tuple[AgentFormat, Path]:
    if args.claude_root:
        return AgentFormat.CLAUDE, args.claude_root
    if args.codex_root:
        return AgentFormat.CODEX, args.codex_root
    assert args.pi_root
    return AgentFormat.PI, args.pi_root


def _source_files(source_format: AgentFormat, root: Path) -> list[Path]:
    if source_format == AgentFormat.CLAUDE:
        return sorted((root / "projects").glob("*/*.jsonl"))
    if source_format == AgentFormat.CODEX:
        return sorted(
            [
                *(root / "sessions").glob("*/*/*/rollout-*.jsonl"),
                *(root / "archived_sessions").glob("rollout-*.jsonl"),
            ]
        )
    return sorted((root / "sessions").glob("*/*.jsonl"))


def _load_source(path: Path, source_format: AgentFormat) -> Session:
    if source_format == AgentFormat.CLAUDE:
        return claude.parse(path)
    if source_format == AgentFormat.CODEX:
        return codex.parse(path)
    return pi.parse_session(path)


def _expected_rejection(source_format: AgentFormat, exc: SessionBridgeError) -> str | None:
    if source_format != AgentFormat.CODEX:
        return None
    message = str(exc)
    if "history mode" in message and "not supported" in message:
        return "codex_history_mode"
    if "history_base lineage is not supported" in message:
        return "codex_history_base"
    return None


def _check_one(
    binary: Path,
    provider: _Provider,
    root: Path,
    ordinal: int,
    session: Session,
) -> None:
    case = root / f"case-{ordinal}"
    home = case / "home"
    copilot_home = case / "copilot"
    work = case / "work"
    temporary = case / "tmp"
    for directory in (case, home, copilot_home, work, temporary):
        directory.mkdir(mode=0o700)
    target_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"copilot-native-{ordinal}"))
    artifact = convert_session(
        session,
        ConversionOptions(
            target_format=TargetFormat.COPILOT,
            session_id=target_id,
            cwd=work,
        ),
    )
    events_path, _ = install_copilot_artifact(artifact, target_home=copilot_home)
    before = events_path.read_bytes()
    if before != artifact.native_bytes:
        raise RuntimeError(f"native setup mismatch at anonymous session {ordinal}")
    provider.request_value = None
    environment = _environment(
        home,
        copilot_home,
        temporary,
        provider.server_address[1],
    )
    completed = subprocess.run(
        [
            str(binary),
            "--no-auto-update",
            "--no-remote",
            "--no-remote-export",
            "--disable-builtin-mcps",
            "--no-custom-instructions",
            "-C",
            str(work),
            f"--resume={target_id}",
            "-p",
            FOLLOWUP,
            "--allow-all-tools",
            "--silent",
        ],
        cwd=work,
        env=environment,
        check=False,
        capture_output=True,
        timeout=90,
    )
    if completed.returncode != 0 or REPLY.encode() not in completed.stdout:
        raise RuntimeError(f"native resume failed at anonymous session {ordinal}")
    after = events_path.read_bytes()
    if len(after) <= len(before) or not after.startswith(before):
        raise RuntimeError(f"native append changed source prefix at anonymous session {ordinal}")
    if not (copilot_home / "session-store.db").is_file():
        raise RuntimeError(f"native index was not rebuilt at anonymous session {ordinal}")
    request = provider.request_value
    if not isinstance(request, dict) or FOLLOWUP not in json.dumps(request, ensure_ascii=False):
        raise RuntimeError(f"native provider request missing at anonymous session {ordinal}")
    _assert_expected_values(session.events, request, ordinal)


def _assert_expected_values(
    events: tuple[Event, ...], request: dict[str, Any], ordinal: int
) -> None:
    effective = list(events)
    compact_indexes = [
        index
        for index, event in enumerate(effective)
        if event.kind == EventKind.COMPACTION and event.text
    ]
    if compact_indexes:
        effective = effective[compact_indexes[-1] :]
    wire = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    wire_strings = tuple(_string_values(request))
    canonical_values = set(_canonical_values(request))
    expected: list[tuple[str, str]] = []
    for event in effective:
        if (
            event.kind == EventKind.MESSAGE
            and event.text
            and event.role in {Role.USER, Role.ASSISTANT}
        ) or (event.kind == EventKind.COMPACTION and event.text):
            expected.append((event.kind.value, event.text))
        elif event.kind == EventKind.TOOL_CALL:
            if event.tool_call_id:
                expected.append(("tool_call_id", event.tool_call_id))
            if event.tool_name:
                expected.append(("tool_name", event.tool_name))
            expected.append(
                (
                    "tool_arguments",
                    json.dumps(
                        event.payload.get("input", {}),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
            )
        elif event.kind == EventKind.TOOL_RESULT:
            if event.tool_call_id:
                expected.append(("tool_result_id", event.tool_call_id))
            if event.text:
                expected.append(("tool_result_text", event.text))
            for block in event.payload.get("content_blocks", []):
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    expected.append(("tool_result_text_block", block["text"]))
                elif isinstance(block, dict):
                    image = portable_data_image(block.get("image_url") or block.get("url"))
                    if image:
                        expected.append(("tool_result_image", image[1]))
        elif (
            event.kind == EventKind.CONTEXT
            and event.role == Role.USER
            and event.payload.get("block_type") == "image"
        ):
            image = portable_data_image(event.payload.get("image_url"))
            if image:
                expected.append(("user_image", image[1]))
    missing = [
        (kind, len(value))
        for kind, value in expected
        if kind != "tool_result_image"
        and value
        and value not in wire
        and not any(value in candidate for candidate in wire_strings)
        and value not in canonical_values
    ]
    if missing:
        detail = ",".join(f"{kind}:{length}" for kind, length in missing[:8])
        raise RuntimeError(f"portable replay mismatch at anonymous session {ordinal}: {detail}")


def _string_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for child in value for item in _string_values(child)]
    if isinstance(value, dict):
        return [item for child in value.values() for item in _string_values(child)]
    return []


def _canonical_values(value: Any) -> list[str]:
    result: list[str] = []
    if isinstance(value, dict):
        result.append(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        for child in value.values():
            result.extend(_canonical_values(child))
    elif isinstance(value, list):
        for child in value:
            result.extend(_canonical_values(child))
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return result
        if isinstance(parsed, (dict, list)):
            result.append(
                json.dumps(
                    parsed,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
    return result


def _features(session: Session, size: int) -> tuple[str, ...]:
    kinds = Counter(event.kind for event in session.events)
    result: list[str] = []
    if kinds[EventKind.TOOL_CALL] or kinds[EventKind.TOOL_RESULT]:
        result.append("tools")
    if kinds[EventKind.COMPACTION]:
        result.append("compaction")
    if any(
        event.kind == EventKind.CONTEXT and event.payload.get("block_type") == "image"
        for event in session.events
    ) or any(
        event.kind == EventKind.TOOL_RESULT
        and any(
            isinstance(block, dict) and block.get("type") in {"image", "input_image"}
            for block in event.payload.get("content_blocks", [])
        )
        for event in session.events
    ):
        result.append("images")
    portable = [
        event
        for event in session.events
        if event.kind in {EventKind.MESSAGE, EventKind.TOOL_CALL, EventKind.TOOL_RESULT}
    ]
    if portable and (portable[-1].role == Role.USER or portable[-1].kind == EventKind.TOOL_CALL):
        result.append("interrupted")
    if size >= 1024 * 1024:
        result.append("large")
    return tuple(result or ["basic"])


def _select(inventory: list[SessionSummary], count: int) -> list[SessionSummary]:
    count = min(max(0, count), len(inventory))
    selected: list[SessionSummary] = []
    for feature in ("compaction", "images", "interrupted", "tools", "large", "basic"):
        matches = [item for item in inventory if feature in item.features and item not in selected]
        if matches:
            selected.append(min(matches, key=lambda item: (item.source_bytes, item.ordinal)))
    remaining = sorted(
        (item for item in inventory if item not in selected),
        key=lambda item: (item.source_bytes, item.ordinal),
    )
    selected.extend(remaining[: max(0, count - len(selected))])
    return selected[:count]


def _environment(home: Path, copilot_home: Path, temporary: Path, port: int) -> dict[str, str]:
    values = {
        "HOME": str(home),
        "COPILOT_HOME": str(copilot_home),
        "TMPDIR": str(temporary),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "TERM": "dumb",
        "NO_COLOR": "1",
        "COPILOT_PROVIDER_BASE_URL": f"http://127.0.0.1:{port}/v1",
        "COPILOT_PROVIDER_TYPE": "openai",
        "COPILOT_PROVIDER_WIRE_API": "completions",
        # A recognized vision-capable model ID ensures native resume does not
        # intentionally filter imported images before the loopback request.
        "COPILOT_PROVIDER_MODEL_ID": "gpt-4.1",
        "COPILOT_PROVIDER_WIRE_MODEL": "fixture-model",
        "COPILOT_PROVIDER_MAX_PROMPT_TOKENS": "1000000",
        "COPILOT_PROVIDER_MAX_OUTPUT_TOKENS": "4096",
        "COPILOT_OFFLINE": "true",
    }
    for key in ("LANG", "LC_ALL", "LC_CTYPE"):
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def _require_version(binary: Path, root: Path) -> None:
    completed = subprocess.run(
        [str(binary), "--no-auto-update", "--version"],
        env=_environment(root, root, root, 9),
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if completed.returncode != 0 or copilot.PINNED_COPILOT_VERSION not in completed.stdout:
        raise RuntimeError("Copilot binary does not match the pinned version")


if __name__ == "__main__":
    raise SystemExit(main())
