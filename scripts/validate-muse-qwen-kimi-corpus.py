#!/usr/bin/env python3
"""Content-safe corpus validation for the Muse, Qwen, and Kimi writers.

The command prints aggregate counts only.  It never prints session paths, IDs,
titles, message text, tool names, arguments, results, media, timestamps, hashes,
or working directories.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from session_migrate.conversion import ConversionOptions, convert_session
from session_migrate.errors import SessionMigrateError
from session_migrate.formats import claude, codex, kimi, muse, qwen
from session_migrate.formats.common import portable_data_image
from session_migrate.model import AgentFormat, Event, EventKind, Role, Session, TargetFormat

TARGETS = (TargetFormat.MUSE, TargetFormat.QWEN, TargetFormat.KIMI)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--claude-root", type=Path)
    sources.add_argument("--codex-root", type=Path)
    parser.add_argument(
        "--sample",
        type=int,
        default=0,
        help="select this many files evenly across the store; zero means all",
    )
    parser.add_argument("--progress-every", type=int, default=25)
    return parser.parse_args()


def source_inventory(args: argparse.Namespace) -> tuple[AgentFormat, list[Path]]:
    if args.claude_root:
        paths = sorted((args.claude_root / "projects").glob("*/*.jsonl"))
        source_format = AgentFormat.CLAUDE
    else:
        assert args.codex_root
        paths = sorted(
            [
                *(args.codex_root / "sessions").glob("*/*/*/rollout-*.jsonl"),
                *(args.codex_root / "archived_sessions").glob("rollout-*.jsonl"),
            ]
        )
        source_format = AgentFormat.CODEX
    if args.sample < 0:
        raise RuntimeError("--sample cannot be negative")
    if args.sample and args.sample < len(paths):
        denominator = args.sample - 1
        paths = (
            [paths[len(paths) // 2]]
            if denominator == 0
            else [paths[index * (len(paths) - 1) // denominator] for index in range(args.sample)]
        )
    return source_format, paths


def parse_source(path: Path, source_format: AgentFormat) -> Session:
    return claude.parse(path) if source_format == AgentFormat.CLAUDE else codex.parse(path)


def portable_signature(events: tuple[Event, ...], target: TargetFormat) -> list[Any]:
    """Project the target's documented portable subset, independent of its parser."""

    rows: list[Any] = []
    active_muse_turn = False
    for event in events:
        if event.kind == EventKind.MESSAGE and event.role == Role.USER and event.text:
            active_muse_turn = True
            rows.append(("message", "user", event.text))
        elif event.kind == EventKind.MESSAGE and event.role == Role.ASSISTANT and event.text:
            if target != TargetFormat.MUSE or active_muse_turn:
                rows.append(("message", "assistant", event.text))
        elif event.kind == EventKind.CONTEXT and event.role == Role.USER:
            if target != TargetFormat.MUSE and event.payload.get("block_type") == "image":
                rows.append(("image", event.payload.get("image_url")))
        elif event.kind == EventKind.TOOL_CALL:
            if target != TargetFormat.MUSE or active_muse_turn:
                arguments = event.payload.get("input", {})
                if not isinstance(arguments, dict):
                    arguments = {"input": arguments}
                rows.append(
                    (
                        "call",
                        event.tool_call_id,
                        event.tool_name,
                        json.dumps(arguments, ensure_ascii=False, sort_keys=True),
                    )
                )
        elif event.kind == EventKind.TOOL_RESULT:
            if target != TargetFormat.MUSE or active_muse_turn:
                blocks = event.payload.get("content_blocks", [])
                blocks = blocks if isinstance(blocks, list) else []
                portable: list[dict[str, Any]] = []
                for block in blocks:
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get("type")
                    if (
                        block_type in {"text", "input_text", "output_text"}
                        and isinstance(block.get("text"), str)
                        and block.get("text")
                    ):
                        portable.append({"type": "text", "text": block["text"]})
                    elif block_type in {"image", "input_image"}:
                        image = portable_data_image(block.get("image_url") or block.get("url"))
                        if image:
                            media_type, data = image
                            portable.append(
                                {
                                    "type": "image",
                                    "image_url": f"data:{media_type};base64,{data}",
                                }
                            )
                text = event.text or None
                if target == TargetFormat.MUSE:
                    texts = [
                        block.get("text")
                        for block in blocks
                        if isinstance(block, dict)
                        and block.get("type") in {"text", "input_text", "output_text"}
                        and isinstance(block.get("text"), str)
                        and block.get("text")
                    ]
                    text = "\n".join(texts) or text
                    portable = []
                rows.append(
                    (
                        "result",
                        event.tool_call_id,
                        text,
                        target != TargetFormat.MUSE and event.payload.get("is_error") is True,
                        json.dumps(portable, ensure_ascii=False, sort_keys=True),
                    )
                )
        elif event.kind == EventKind.COMPACTION and event.text and target == TargetFormat.KIMI:
            rows.append(("compaction", event.text))
    return rows


def reparse(data: bytes, target: TargetFormat, session_id: str, root: Path) -> Session:
    if target == TargetFormat.MUSE:
        muse.validate_native_bytes(data, session_id)
        path = root / "target.jsonl"
        path.write_bytes(data)
        return muse.parse_session(path)
    if target == TargetFormat.QWEN:
        qwen.validate_native_bytes(data, session_id)
        path = root / "target.jsonl"
        path.write_bytes(data)
        return qwen.parse_session(path)

    native_id = kimi.native_session_id(session_id)
    kimi.validate_native_bytes(data, native_id)
    path = root / "target"
    state, wire = kimi.native_files(data, native_id, path)
    (path / "agents/main").mkdir(parents=True)
    (path / kimi.STATE_FILENAME).write_bytes(state)
    (path / "agents/main" / kimi.WIRE_FILENAME).write_bytes(wire)
    return kimi.parse_session(path)


def expected_rejection(source_format: AgentFormat, exc: SessionMigrateError) -> str | None:
    if source_format != AgentFormat.CODEX:
        return None
    message = str(exc)
    if "history mode" in message and "not supported" in message:
        return "codex_history_mode"
    if "history_base lineage is not supported" in message:
        return "codex_history_base"
    return None


def main() -> int:
    args = parse_args()
    source_format, paths = source_inventory(args)
    converted = Counter()
    feature_counts = Counter()
    rejections = Counter()

    for ordinal, path in enumerate(paths, start=1):
        try:
            session = parse_source(path, source_format)
            kinds = {event.kind for event in session.events}
            feature_counts.update(
                {
                    "tools": int(EventKind.TOOL_CALL in kinds),
                    "images": int(
                        any(
                            event.kind == EventKind.CONTEXT
                            and event.payload.get("block_type") == "image"
                            for event in session.events
                        )
                    ),
                    "compaction": int(EventKind.COMPACTION in kinds),
                    "thinking": int(EventKind.THINKING in kinds),
                }
            )
            for target in TARGETS:
                with tempfile.TemporaryDirectory(prefix="session-migrate-new-target-") as name:
                    temporary = Path(name)
                    session_id = str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"session-migrate-corpus:{source_format.value}:{ordinal}:{target.value}",
                        )
                    )
                    artifact = convert_session(
                        session,
                        ConversionOptions(
                            target_format=target,
                            session_id=session_id,
                            cwd=session.cwd or temporary,
                        ),
                    )
                    parsed = reparse(artifact.native_bytes, target, artifact.session_id, temporary)
                    expected = portable_signature(session.events, target)
                    actual = portable_signature(parsed.events, target)
                    if actual != expected:
                        mismatch = next(
                            (
                                index
                                for index, (left, right) in enumerate(
                                    zip(expected, actual, strict=False)
                                )
                                if left != right
                            ),
                            min(len(expected), len(actual)),
                        )
                        raise RuntimeError(
                            "semantic mismatch at anonymous source "
                            f"{ordinal}, target {target.value}, row {mismatch}; "
                            f"expected_rows={len(expected)}, actual_rows={len(actual)}"
                        )
                    converted[target.value] += 1
        except SessionMigrateError as exc:
            reason = expected_rejection(source_format, exc)
            if reason is None:
                raise RuntimeError(
                    f"unexpected native rejection at anonymous source {ordinal}"
                ) from None
            rejections[reason] += 1
        if args.progress_every and ordinal % args.progress_every == 0:
            print(
                f"validated {ordinal}/{len(paths)} anonymous sessions",
                file=__import__("sys").stderr,
            )

    print(
        json.dumps(
            {
                "source_format": source_format.value,
                "selected_sessions": len(paths),
                "supported_sessions": next(iter(converted.values()), 0),
                "expected_rejections": dict(sorted(rejections.items())),
                "targets": {
                    target.value: {
                        "converted": converted[target.value],
                        "byte_validated": converted[target.value],
                        "reparsed": converted[target.value],
                        "semantic_projection_matches": converted[target.value],
                    }
                    for target in TARGETS
                },
                "feature_sessions": dict(sorted(feature_counts.items())),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
