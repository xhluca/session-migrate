#!/usr/bin/env python3
"""Credential-free Claude/Codex native resume checks over readable sessions.

The script prints aggregate anonymous counts only. It imports selected real
sessions into private temporary homes, resumes them in the pinned Docker image
with networking disabled, proves append-only behavior, and removes the homes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from session_migrate.conversion import (
    ConversionOptions,
    convert_session,
    target_import_paths,
)
from session_migrate.errors import SessionMigrateError
from session_migrate.formats import claude, codex, copilot, opencode, pi
from session_migrate.jsonl import write_private_atomic
from session_migrate.model import AgentFormat, EventKind, Role, Session, TargetFormat

PINNED_IMAGE_ID = "sha256:8f170f660813ac358f347fa8a3580139972f3ea7a9fb087834f1da44669d9392"


@dataclass(frozen=True, slots=True)
class SessionSummary:
    ordinal: int
    source_bytes: int
    features: tuple[str, ...]


def main() -> int:
    parser = argparse.ArgumentParser()
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--claude-root", type=Path)
    sources.add_argument("--codex-root", type=Path)
    sources.add_argument("--pi-root", type=Path)
    sources.add_argument("--opencode-export-root", type=Path)
    sources.add_argument("--copilot-root", type=Path)
    parser.add_argument(
        "--include-same-format",
        action="store_true",
        help="also cold-resume the portable same-format rewrite",
    )
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--image", default="basic-claude-uv:latest")
    args = parser.parse_args()

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
    target_formats = tuple(
        target
        for target in (TargetFormat.CLAUDE, TargetFormat.CODEX)
        if args.include_same_format or target.value != source_format.value
    )
    image_id = resolved_image_id(args.image)
    feature_counts: Counter[str] = Counter()
    target_counts: Counter[str] = Counter()
    workspace: Path | None = None
    with tempfile.TemporaryDirectory(prefix="session-migrate-core-native-") as directory:
        workspace = Path(directory)
        os.chmod(workspace, 0o700)
        for summary in selected:
            session = load_source(files[summary.ordinal - 1], source_format)
            feature_counts.update(summary.features)
            for target in target_formats:
                check_one(
                    session,
                    ordinal=summary.ordinal,
                    target=target,
                    image_id=image_id,
                    workspace=workspace,
                )
                target_counts[target.value] += 1

    if workspace is None or workspace.exists():
        raise RuntimeError("private native validation workspace was not removed")
    result: dict[str, Any] = {
        "source_format": source_format.value,
        "source_files": len(files),
        "parsed_sessions": len(inventory),
        "expected_rejections": dict(sorted(rejected.items())),
        "selected_sessions": len(selected),
        "target_checks": dict(sorted(target_counts.items())),
        "native_resumes": sum(target_counts.values()),
        "exact_generated_prefixes": sum(target_counts.values()),
        "claude_append_ancestry_verified": target_counts[TargetFormat.CLAUDE.value],
        "codex_sqlite_indexes_rebuilt": target_counts[TargetFormat.CODEX.value],
        "feature_counts": dict(sorted(feature_counts.items())),
        "docker_image_id": image_id,
        "network_enabled": False,
        "credentials_mounted": False,
        "private_workspace_removed": True,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
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
    assert args.copilot_root
    return AgentFormat.COPILOT, args.copilot_root


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
    return sorted(root.glob("*/events.jsonl"))


def load_source(path: Path, source_format: AgentFormat) -> Session:
    if source_format == AgentFormat.CLAUDE:
        return claude.parse(path)
    if source_format == AgentFormat.CODEX:
        return codex.parse(path)
    if source_format == AgentFormat.PI:
        return pi.parse_session(path)
    if source_format == AgentFormat.OPENCODE:
        return opencode.parse_session(path)
    return copilot.parse_session(path)


def expected_rejection(source_format: AgentFormat, exc: SessionMigrateError) -> str | None:
    if source_format != AgentFormat.CODEX:
        return None
    message = str(exc)
    if "history mode" in message and "not supported" in message:
        return "codex_history_mode"
    if "history_base lineage is not supported" in message:
        return "codex_history_base"
    return None


def classify(session: Session, source_bytes: int) -> tuple[str, ...]:
    kinds = Counter(event.kind for event in session.events)
    features: list[str] = []
    if kinds[EventKind.TOOL_CALL] or kinds[EventKind.TOOL_RESULT]:
        features.append("tools")
    if kinds[EventKind.COMPACTION]:
        features.append("compaction")
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
        features.append("images")
    portable = [
        event
        for event in session.events
        if event.kind in {EventKind.MESSAGE, EventKind.TOOL_CALL, EventKind.TOOL_RESULT}
    ]
    if portable and (portable[-1].role == Role.USER or portable[-1].kind == EventKind.TOOL_CALL):
        features.append("interrupted")
    if source_bytes >= 1024 * 1024:
        features.append("large")
    return tuple(features or ["basic"])


def select_cases(sessions: list[SessionSummary], count: int) -> list[SessionSummary]:
    count = min(max(count, 0), len(sessions))
    selected: list[SessionSummary] = []
    for feature in ("compaction", "images", "interrupted", "tools", "large", "basic"):
        matches = [item for item in sessions if feature in item.features and item not in selected]
        if matches:
            selected.append(min(matches, key=lambda item: (item.source_bytes, item.ordinal)))
    remaining = sorted(
        (item for item in sessions if item not in selected),
        key=lambda item: (item.source_bytes, item.ordinal),
    )
    selected.extend(remaining[: max(0, count - len(selected))])
    return selected[:count]


def resolved_image_id(image: str) -> str:
    completed = subprocess.run(
        ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    image_id = completed.stdout.strip()
    if completed.returncode != 0 or image_id != PINNED_IMAGE_ID:
        raise RuntimeError("native validation requires the pinned Docker image ID")
    return image_id


def check_one(
    session: Session,
    *,
    ordinal: int,
    target: TargetFormat,
    image_id: str,
    workspace: Path,
) -> None:
    case = workspace / f"case-{ordinal}-{target.value}"
    target_home = case / "target"
    os_home = case / "home"
    for directory in (case, target_home, os_home):
        directory.mkdir(mode=0o700)
    target_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"core-native-{target.value}-{ordinal}"))
    artifact = convert_session(
        session,
        ConversionOptions(target_format=target, session_id=target_id, cwd=Path("/work")),
    )
    native_path, _manifest = target_import_paths(artifact, target_home)
    native_path.parent.mkdir(parents=True, mode=0o700)
    write_private_atomic(native_path, artifact.native_bytes)
    before = native_path.read_bytes()
    if before != artifact.native_bytes:
        raise RuntimeError(f"native setup mismatch at anonymous session {ordinal}")

    command = claude_command() if target == TargetFormat.CLAUDE else codex_command()
    completed = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "-e",
            f"TARGET_ID={target_id}",
            "-v",
            f"{case}:/state",
            "-w",
            "/work",
            image_id,
            "bash",
            "-lc",
            command,
        ],
        check=False,
        capture_output=True,
        timeout=40,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"native resume failed at anonymous session {ordinal}")
    after = native_path.read_bytes()
    if len(after) <= len(before) or not after.startswith(before):
        raise RuntimeError(f"native append changed source prefix at anonymous session {ordinal}")
    if hashlib.sha256(after[: len(before)]).digest() != hashlib.sha256(before).digest():
        raise RuntimeError(f"native prefix hash mismatch at anonymous session {ordinal}")
    if target == TargetFormat.CLAUDE:
        assert_claude_append(native_path, len(before), ordinal)
    elif not (target_home / "state_5.sqlite").is_file():
        raise RuntimeError(f"Codex index was not rebuilt at anonymous session {ordinal}")


def claude_command() -> str:
    return """
set -eu
log=/state/resume.log
HOME=/state/home CLAUDE_CONFIG_DIR=/state/target timeout 20s \
  claude -p --resume "$TARGET_ID" \
  "Synthetic offline native-resume validation probe." >"$log" 2>&1 || true
grep -q "Not logged in" "$log"
"""


def codex_command() -> str:
    return """
set -eu
log=/state/resume.log
HOME=/state/home CODEX_HOME=/state/target timeout 20s \
  codex exec resume --skip-git-repo-check "$TARGET_ID" \
  "Synthetic offline native-resume validation probe." >"$log" 2>&1 || true
grep -q "session id: $TARGET_ID" "$log"
test -s /state/target/state_5.sqlite
"""


def assert_claude_append(path: Path, before: int, ordinal: int) -> None:
    data = path.read_bytes()
    head = [json.loads(line) for line in data[:before].splitlines() if line.strip()]
    tail = [json.loads(line) for line in data[before:].splitlines() if line.strip()]
    leaf = next(
        (
            record.get("uuid")
            for record in reversed(head)
            if record.get("type") in {"user", "assistant"} and record.get("uuid")
        ),
        None,
    )
    appended = next((record for record in tail if record.get("type") == "user"), None)
    if not leaf or not isinstance(appended, dict):
        raise RuntimeError(f"Claude append missing at anonymous session {ordinal}")
    nodes = {
        record["uuid"]: record
        for record in tail
        if isinstance(record, dict) and isinstance(record.get("uuid"), str)
    }
    cursor = appended.get("parentUuid")
    seen: set[str] = set()
    while isinstance(cursor, str) and cursor in nodes and cursor not in seen:
        seen.add(cursor)
        cursor = nodes[cursor].get("parentUuid")
    if cursor != leaf:
        raise RuntimeError(f"Claude append ancestry mismatch at anonymous session {ordinal}")


if __name__ == "__main__":
    raise SystemExit(main())
