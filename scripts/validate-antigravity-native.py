#!/usr/bin/env python3
"""Credential-isolated native Antigravity 1.1.16 resume validation.

The harness prints aggregate counts only. It copies the existing OAuth state
into a mode-0700 temporary HOME, installs selected conversions through the
pinned adapter, asks the exact CLI to append a synthetic prompt, verifies the
portable prefix and appended prompt, and removes the isolated HOME.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import pty
import select
import shutil
import signal
import struct
import subprocess
import tempfile
import termios
import time
import uuid
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from session_migrate.conversion import ConversionOptions, convert_session
from session_migrate.errors import SessionMigrateError
from session_migrate.formats import antigravity, claude, codex, copilot, opencode, pi
from session_migrate.model import AgentFormat, Event, EventKind, Session, TargetFormat

APPEND_PROMPT = "SESSION_MIGRATE_ANTIGRAVITY_NATIVE_APPEND"
TUI_APPEND_PROMPT = "SESSION_MIGRATE_ANTIGRAVITY_TUI_APPEND"


@dataclass(frozen=True, slots=True)
class SessionSummary:
    ordinal: int
    source_bytes: int
    features: tuple[str, ...]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--claude-root", type=Path)
    sources.add_argument("--codex-root", type=Path)
    sources.add_argument("--pi-root", type=Path)
    sources.add_argument("--opencode-export-root", type=Path)
    sources.add_argument("--copilot-root", type=Path)
    sources.add_argument("--antigravity-root", type=Path)
    parser.add_argument("--antigravity-bin", type=Path, required=True)
    parser.add_argument("--credential", type=Path)
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument(
        "--tui-count",
        type=int,
        default=0,
        help="also drive this many selected sessions through the actual interactive TUI",
    )
    parser.add_argument("--timeout", type=int, default=60)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source_format, source_root = selected_source(args)
    files = source_files(source_format, source_root)
    inventory: list[SessionSummary] = []
    rejected: Counter[str] = Counter()
    for ordinal, path in enumerate(files, start=1):
        try:
            session = load_source(path, source_format)
        except SessionMigrateError as exc:
            reason = expected_rejection(source_format, exc)
            if reason:
                rejected[reason] += 1
                continue
            raise RuntimeError(f"source parse failed at anonymous session {ordinal}") from None
        size = path.stat().st_size
        inventory.append(SessionSummary(ordinal, size, classify(session, size)))
    selected = select_cases(inventory, args.count)
    credential = args.credential or (
        Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    )
    if not credential.is_file():
        raise RuntimeError("Antigravity OAuth state is unavailable")

    feature_counts: Counter[str] = Counter()
    root_path: Path | None = None
    with tempfile.TemporaryDirectory(prefix="session-migrate-antigravity-native-") as name:
        root = Path(name)
        root_path = root
        os.chmod(root, 0o700)
        cli = antigravity.verify_pinned_cli(args.antigravity_bin)
        for item in selected:
            session = load_source(files[item.ordinal - 1], source_format)
            check_one(
                cli,
                credential,
                root,
                item.ordinal,
                session,
                timeout=args.timeout,
            )
            feature_counts.update(item.features)
        tui_selected = selected[: min(max(args.tui_count, 0), len(selected))]
        for item in tui_selected:
            session = load_source(files[item.ordinal - 1], source_format)
            check_tui(cli, credential, root, item.ordinal, session, timeout=args.timeout)

    if root_path is None or root_path.exists():
        raise RuntimeError("private native validation workspace was not removed")
    print(
        json.dumps(
            {
                "source_format": source_format.value,
                "source_files": len(files),
                "parsed_sessions": len(inventory),
                "expected_rejections": dict(sorted(rejected.items())),
                "selected_sessions": len(selected),
                "native_loads": len(selected),
                "native_appends": len(selected),
                "portable_prefix_matches": len(selected),
                "actual_tui_loads": len(tui_selected),
                "actual_tui_appends": len(tui_selected),
                "feature_counts": dict(sorted(feature_counts.items())),
                "antigravity_version": antigravity.PINNED_ANTIGRAVITY_VERSION,
                "credential_copied_to_isolated_home": True,
                "private_workspace_removed": True,
                "content_or_identifiers_printed": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def selected_source(args: argparse.Namespace) -> tuple[AgentFormat, Path]:
    if args.claude_root:
        return AgentFormat.CLAUDE, args.claude_root
    if args.codex_root:
        return AgentFormat.CODEX, args.codex_root
    if args.pi_root:
        return AgentFormat.PI, args.pi_root
    if args.opencode_export_root:
        return AgentFormat.OPENCODE, args.opencode_export_root
    if args.copilot_root:
        return AgentFormat.COPILOT, args.copilot_root
    assert args.antigravity_root
    return AgentFormat.ANTIGRAVITY, args.antigravity_root


def source_files(source_format: AgentFormat, root: Path) -> list[Path]:
    if source_format == AgentFormat.CLAUDE:
        return sorted((root / "projects").glob("*/*.jsonl"))
    if source_format == AgentFormat.CODEX:
        return sorted(
            [
                *(root / "sessions").glob("*/*/*/rollout-*.jsonl"),
                *(root / "archived_sessions").glob("rollout-*.jsonl"),
            ]
        )
    if source_format == AgentFormat.PI:
        return sorted((root / "sessions").glob("*/*.jsonl"))
    if source_format == AgentFormat.OPENCODE:
        return sorted(root.glob("*.json"))
    if source_format == AgentFormat.COPILOT:
        return sorted(root.glob("*/events.jsonl"))
    conversations = root / "conversations"
    return sorted((conversations if conversations.is_dir() else root).glob("*.db"))


def load_source(path: Path, source_format: AgentFormat) -> Session:
    if source_format == AgentFormat.CLAUDE:
        return claude.parse(path)
    if source_format == AgentFormat.CODEX:
        return codex.parse(path)
    if source_format == AgentFormat.PI:
        return pi.parse_session(path)
    if source_format == AgentFormat.OPENCODE:
        return opencode.parse_session(path)
    if source_format == AgentFormat.COPILOT:
        return copilot.parse_session(path)
    return antigravity.parse_session(path)


def expected_rejection(source_format: AgentFormat, exc: SessionMigrateError) -> str | None:
    if source_format != AgentFormat.CODEX:
        return None
    message = str(exc)
    if "history mode" in message and "not supported" in message:
        return "codex_history_mode"
    if "history_base lineage is not supported" in message:
        return "codex_history_base"
    return None


def check_one(
    cli: Path,
    credential: Path,
    root: Path,
    ordinal: int,
    session: Session,
    *,
    timeout: int,
) -> None:
    case = root / f"case-{ordinal}"
    isolated_home = case / "home"
    workspace = case / "work"
    for directory in (case, isolated_home, workspace):
        directory.mkdir(mode=0o700)
    app_home = antigravity.app_data_home(isolated_home)
    app_home.mkdir(parents=True, mode=0o700)
    credential_copy = app_home / credential.name
    shutil.copyfile(credential, credential_copy)
    os.chmod(credential_copy, 0o600)

    target_id = deterministic_uuid4(f"antigravity-native-{ordinal}")
    artifact = convert_session(
        session,
        ConversionOptions(
            target_format=TargetFormat.ANTIGRAVITY,
            session_id=target_id,
            cwd=workspace,
        ),
    )
    installed = antigravity.install_database(
        artifact.native_bytes,
        session_id=artifact.session_id,
        cwd=workspace,
        timestamp=artifact.timestamp,
        title=session.title,
        target_home=app_home,
        target_cli=cli,
    )
    before_bytes = antigravity.snapshot_database_bytes(installed.conversation_path)
    before = antigravity.parse_session(installed.conversation_path)
    before_signature = portable_signature(before.events)
    environment = isolated_environment(isolated_home, case)
    completed = subprocess.run(
        [
            str(cli),
            f"--conversation={artifact.session_id}",
            "--print-timeout=30s",
            "--print",
            APPEND_PROMPT,
        ],
        cwd=workspace,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode not in {0, 1}:
        raise RuntimeError(f"native append failed at anonymous session {ordinal}")
    after_bytes = antigravity.snapshot_database_bytes(installed.conversation_path)
    if after_bytes == before_bytes:
        raise RuntimeError(f"native append did not change anonymous session {ordinal}")
    after = antigravity.parse_session(installed.conversation_path)
    after_signature = portable_signature(after.events)
    if after_signature[: len(before_signature)] != before_signature:
        raise RuntimeError(f"native prefix changed at anonymous session {ordinal}")
    if not any(
        event.kind == EventKind.MESSAGE and event.text == APPEND_PROMPT for event in after.events
    ):
        raise RuntimeError(f"native append is missing at anonymous session {ordinal}")


def check_tui(
    cli: Path,
    credential: Path,
    root: Path,
    ordinal: int,
    session: Session,
    *,
    timeout: int,
) -> None:
    """Drive the real full-screen TUI through isolated first-run onboarding."""

    case = root / f"tui-case-{ordinal}"
    isolated_home = case / "home"
    workspace = case / "work"
    for directory in (case, isolated_home, workspace):
        directory.mkdir(mode=0o700)
    app_home = antigravity.app_data_home(isolated_home)
    app_home.mkdir(parents=True, mode=0o700)
    credential_copy = app_home / credential.name
    shutil.copyfile(credential, credential_copy)
    os.chmod(credential_copy, 0o600)

    target_id = deterministic_uuid4(f"antigravity-tui-{ordinal}")
    artifact = convert_session(
        session,
        ConversionOptions(
            target_format=TargetFormat.ANTIGRAVITY,
            session_id=target_id,
            cwd=workspace,
        ),
    )
    installed = antigravity.install_database(
        artifact.native_bytes,
        session_id=artifact.session_id,
        cwd=workspace,
        timestamp=artifact.timestamp,
        title=session.title,
        target_home=app_home,
        target_cli=cli,
    )
    environment = isolated_environment(isolated_home, case)
    environment["TERM"] = "xterm-256color"
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    current_flags = fcntl.fcntl(master, fcntl.F_GETFL)
    fcntl.fcntl(master, fcntl.F_SETFL, current_flags | os.O_NONBLOCK)
    process = subprocess.Popen(
        [str(cli), f"--conversation={artifact.session_id}"],
        cwd=workspace,
        env=environment,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
    )
    os.close(slave)
    output_tail = b""
    output_bytes = 0
    terminal_response_sent = False
    state = 0
    state_changed = time.monotonic()
    prompt_sent = False
    appended = False
    started = time.monotonic()
    deadline = started + timeout
    try:
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.2)[0]:
                try:
                    chunk = os.read(master, 65_536)
                except (BlockingIOError, OSError):
                    chunk = b""
                output_bytes += len(chunk)
                output_tail = (output_tail + chunk)[-1_000_000:]
            elapsed = time.monotonic() - started
            if not terminal_response_sent and elapsed >= 0.5:
                os.write(master, b"\x1b[?2026;0$y\x1b[?2027;0$y\x1b[?0u")
                terminal_response_sent = True
            now = time.monotonic()
            if state == 0 and b"Choose your color scheme" in output_tail:
                os.write(master, b"\r")
                state = 1
                state_changed = now
            elif state == 1 and now - state_changed > 1:
                os.write(master, b"\r")
                state = 2
                state_changed = now
            elif state == 2 and b"Terms of Service" in output_tail and now - state_changed > 1:
                os.write(master, b"\r")
                state = 3
                state_changed = now
            elif state == 3 and now - state_changed > 3:
                os.write(master, (TUI_APPEND_PROMPT + "\r").encode())
                prompt_sent = True
                state = 4
            if prompt_sent:
                try:
                    parsed = antigravity.parse_session(installed.conversation_path)
                except SessionMigrateError:
                    continue
                if any(
                    event.kind == EventKind.MESSAGE and event.text == TUI_APPEND_PROMPT
                    for event in parsed.events
                ):
                    appended = True
                    break
        if not appended:
            raise RuntimeError(
                f"actual TUI did not append at anonymous session {ordinal}; "
                f"process_alive={process.poll() is None}, terminal_bytes={output_bytes}, "
                f"onboarding_state={state}"
            )
    finally:
        if process.poll() is None:
            with suppress(OSError):
                os.write(master, b"\x03\x03")
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
        os.close(master)


def isolated_environment(home: Path, case: Path) -> dict[str, str]:
    values = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(case / "xdg-config"),
        "XDG_CACHE_HOME": str(case / "xdg-cache"),
        "PATH": os.environ.get("PATH", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": "C.UTF-8",
        "TERM": "dumb",
        "NO_COLOR": "1",
    }
    for key in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        Path(values[key]).mkdir(mode=0o700)
    return values


def portable_signature(events: tuple[Event, ...]) -> tuple[tuple[Any, ...], ...]:
    rows: list[tuple[Any, ...]] = []
    aliases: dict[str, str] = {}
    for event in events:
        if event.kind == EventKind.MESSAGE and event.text:
            rows.append(("message", event.role.value if event.role else None, event.text))
        elif event.kind == EventKind.TOOL_CALL:
            call_id = event.tool_call_id or f"missing-{len(aliases)}"
            alias = aliases.setdefault(call_id, f"call-{len(aliases)}")
            rows.append(
                (
                    "call",
                    alias,
                    event.tool_name,
                    json.dumps(event.payload.get("input", {}), sort_keys=True),
                )
            )
        elif event.kind == EventKind.TOOL_RESULT:
            call_id = event.tool_call_id or f"missing-{len(aliases)}"
            alias = aliases.setdefault(call_id, f"call-{len(aliases)}")
            rows.append(
                (
                    "result",
                    alias,
                    event.tool_name,
                    event.text,
                    event.payload.get("is_error") is True,
                )
            )
    return tuple(rows)


def classify(session: Session, source_bytes: int) -> tuple[str, ...]:
    kinds = Counter(event.kind for event in session.events)
    features: list[str] = []
    if kinds[EventKind.TOOL_CALL] or kinds[EventKind.TOOL_RESULT]:
        features.append("tools")
    if kinds[EventKind.COMPACTION]:
        features.append("compaction")
    if kinds[EventKind.CONTEXT]:
        features.append("context")
    if kinds[EventKind.OPAQUE]:
        features.append("native_metadata")
    if source_bytes >= 1024 * 1024:
        features.append("large")
    return tuple(features or ["basic"])


def select_cases(sessions: list[SessionSummary], count: int) -> list[SessionSummary]:
    count = min(max(count, 0), len(sessions))
    selected: list[SessionSummary] = []
    for feature in ("tools", "compaction", "context", "native_metadata", "large", "basic"):
        matches = [item for item in sessions if feature in item.features and item not in selected]
        if matches:
            selected.append(min(matches, key=lambda item: (item.source_bytes, item.ordinal)))
    remaining = sorted(
        (item for item in sessions if item not in selected),
        key=lambda item: (item.source_bytes, item.ordinal),
    )
    selected.extend(remaining[: max(0, count - len(selected))])
    return selected[:count]


def deterministic_uuid4(value: str) -> str:
    data = bytearray(uuid.uuid5(uuid.NAMESPACE_URL, value).bytes)
    data[6] = (data[6] & 0x0F) | 0x40
    data[8] = (data[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(data)))


if __name__ == "__main__":
    raise SystemExit(main())
