#!/usr/bin/env python3
"""Content-safe all-target validation for real Claude, Codex, or Pi sessions.

The script deliberately prints no session path, ID, title, message text, tool
name, argument, result, image data, timestamp, hash, or CWD.  An optional
mode-0600 manual report contains only anonymous ordinals, structural kinds,
content lengths, and equality decisions.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import uuid
from collections import Counter, deque
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from session_bridge.conversion import (
    ConversionOptions,
    convert_session,
    install_opencode_artifact,
)
from session_bridge.errors import SessionBridgeError
from session_bridge.formats import claude, codex, copilot, opencode, pi
from session_bridge.formats.common import portable_data_image, valid_rfc3339
from session_bridge.jsonl import write_private_atomic
from session_bridge.model import AgentFormat, Event, EventKind, Role, Session, TargetFormat

MEDIA_TYPES = {"image/gif", "image/jpeg", "image/png", "image/webp"}


@dataclass(frozen=True, slots=True)
class Projection:
    timeline: tuple[tuple[Any, ...], ...]
    conversation: tuple[tuple[Any, ...], ...]
    calls: tuple[tuple[Any, ...], ...]
    results: tuple[tuple[Any, ...], ...]


@dataclass(frozen=True, slots=True)
class CheckedSession:
    ordinal: int
    source_bytes: int
    features: tuple[str, ...]
    source: Projection
    expected: dict[str, Projection]
    targets: dict[str, Projection]
    dropped: dict[str, dict[str, int]]


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
    parser.add_argument("--manual-report", type=Path)
    parser.add_argument("--manual-count", type=int, default=20)
    parser.add_argument("--native-pi-bin", type=Path)
    parser.add_argument("--native-opencode-bin", type=Path)
    parser.add_argument("--native-count", type=int, default=0)
    parser.add_argument(
        "--max-files",
        type=int,
        default=0,
        help="development-only prefix limit; zero validates the complete selected store",
    )
    parser.add_argument(
        "--start-at",
        type=int,
        default=1,
        help="development-only one-based starting ordinal",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=1000,
        help="emit a content-free progress count to stderr; zero disables progress",
    )
    return parser.parse_args()


def selected_source(args: argparse.Namespace) -> tuple[AgentFormat, Path]:
    if args.claude_root:
        return AgentFormat.CLAUDE, args.claude_root
    if args.codex_root:
        return AgentFormat.CODEX, args.codex_root
    assert args.pi_root
    return AgentFormat.PI, args.pi_root


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
    return sorted((root / "sessions").glob("*/*.jsonl"))


def load_source(path: Path, source_format: AgentFormat) -> Session:
    if source_format == AgentFormat.CLAUDE:
        return claude.parse(path)
    if source_format == AgentFormat.CODEX:
        return codex.parse(path)
    return pi.parse_session(path)


def expected_source_rejection(source_format: AgentFormat, exc: SessionBridgeError) -> str | None:
    if source_format != AgentFormat.CODEX:
        return None
    message = str(exc)
    if "history mode" in message and "not supported" in message:
        return "codex_history_mode"
    if "history_base lineage is not supported" in message:
        return "codex_history_base"
    return None


def check_session_targets(
    session: Session,
    *,
    ordinal: int,
    source_bytes: int,
    features: tuple[str, ...],
    targets: tuple[TargetFormat, ...],
    temporary: Path,
    aggregate_dropped: dict[str, Counter[str]] | None,
    collect: bool,
) -> CheckedSession | None:
    source_projection = project(session.events, source=True) if collect else None
    target_projections: dict[str, Projection] = {}
    expected_projections: dict[str, Projection] = {}
    dropped_by_target: dict[str, dict[str, int]] = {}
    for target in targets:
        target_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"session-bridge-corpus-{ordinal}"))
        artifact = convert_session(
            session,
            ConversionOptions(
                target_format=target,
                session_id=target_uuid,
                cwd=session.cwd or temporary,
            ),
        )
        expected_dropped = independent_dropped(
            session.events,
            target=target,
            fallback_timestamp=artifact.timestamp,
            has_title=bool(session.title),
        )
        suffix = "json" if target == TargetFormat.OPENCODE else "jsonl"
        converted_path = temporary / f"{ordinal}-{target.value}.{suffix}"
        converted_path.write_bytes(artifact.native_bytes)
        try:
            if target == TargetFormat.PI:
                pi.validate_native_bytes(artifact.native_bytes, artifact.session_id)
                parsed = pi.parse(converted_path)
            elif target == TargetFormat.OPENCODE:
                opencode.validate_native_bytes(artifact.native_bytes, artifact.session_id)
                parsed = opencode.parse(converted_path)
            elif target == TargetFormat.COPILOT:
                copilot.validate_native_bytes(artifact.native_bytes, artifact.session_id)
                parsed = copilot.parse(converted_path)
            elif target == TargetFormat.CLAUDE:
                parsed = claude.parse(converted_path)
            else:
                parsed = codex.parse(converted_path)
        finally:
            converted_path.unlink(missing_ok=True)
        target_projection = project(parsed.events, source=False)
        expected_projection = project(session.events, source=True, target=target)
        assert_projection_equal(
            ordinal,
            target.value,
            expected_projection,
            target_projection,
        )
        if artifact.dropped != expected_dropped:
            differences = {
                key: (expected_dropped.get(key, 0), artifact.dropped.get(key, 0))
                for key in sorted(set(expected_dropped) | set(artifact.dropped))
                if expected_dropped.get(key, 0) != artifact.dropped.get(key, 0)
            }
            raise RuntimeError(
                f"loss counter mismatch at anonymous session {ordinal} "
                f"for {target.value}: {differences}"
            )
        if collect:
            target_projections[target.value] = target_projection
            expected_projections[target.value] = expected_projection
            dropped_by_target[target.value] = artifact.dropped
        if aggregate_dropped is not None:
            aggregate_dropped[target.value].update(artifact.dropped)
    if not collect:
        return None
    assert source_projection is not None
    return CheckedSession(
        ordinal=ordinal,
        source_bytes=source_bytes,
        features=features,
        source=source_projection,
        expected=expected_projections,
        targets=target_projections,
        dropped=dropped_by_target,
    )


def main() -> int:
    args = parse_args()
    source_format, source_root = selected_source(args)
    all_files = source_files(source_format, source_root)
    if args.start_at < 1:
        raise RuntimeError("--start-at must be positive")
    files = all_files[args.start_at - 1 :]
    if args.max_files > 0:
        files = files[: args.max_files]
    targets = tuple(
        target
        for target in (
            TargetFormat.CLAUDE,
            TargetFormat.CODEX,
            TargetFormat.PI,
            TargetFormat.OPENCODE,
            TargetFormat.COPILOT,
        )
        if target.value != source_format.value
    )
    inventory: list[SessionSummary] = []
    checked_count = 0
    aggregate_dropped = {target.value: Counter() for target in targets}
    feature_counts: Counter[str] = Counter()
    rejected: Counter[str] = Counter()

    with tempfile.TemporaryDirectory(prefix="session-bridge-corpus-") as directory:
        temporary = Path(directory)
        for ordinal, path in enumerate(files, start=args.start_at):
            try:
                session = load_source(path, source_format)
                source_bytes = path.stat().st_size
                features = classify(session, source_bytes)
                check_session_targets(
                    session,
                    ordinal=ordinal,
                    source_bytes=source_bytes,
                    features=features,
                    targets=targets,
                    temporary=temporary,
                    aggregate_dropped=aggregate_dropped,
                    collect=False,
                )
                inventory.append(SessionSummary(ordinal, source_bytes, features))
                checked_count += 1
                feature_counts.update(features)
            except SessionBridgeError as exc:
                rejection = expected_source_rejection(source_format, exc)
                if rejection:
                    rejected[rejection] += 1
                    continue
                raise RuntimeError(
                    f"source-matrix validation failed at anonymous session {ordinal}"
                ) from None
            except Exception as exc:
                detail = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
                raise RuntimeError(
                    f"source-matrix validation failed at anonymous session {ordinal}: {detail}"
                ) from None
            finally:
                processed = ordinal - args.start_at + 1
                if args.progress_every > 0 and processed % args.progress_every == 0:
                    print(
                        f"validated {processed}/{len(files)} anonymous source files",
                        file=sys.stderr,
                        flush=True,
                    )

    selected = select_manual(inventory, args.manual_count)
    manual_rows = 0
    if args.manual_report:
        manual_rows = write_manual_report(
            args.manual_report,
            files,
            selected,
            first_ordinal=args.start_at,
            source_format=source_format,
            targets=targets,
        )

    native_result: dict[str, Any] | None = None
    if args.native_count:
        if not args.native_opencode_bin:
            raise RuntimeError("native validation requires the pinned OpenCode binary")
        if source_format != AgentFormat.PI and not args.native_pi_bin:
            raise RuntimeError("native validation requires the pinned Pi binary")
        native_selected = select_native(inventory, args.native_count)
        native_result = native_smoke(
            files,
            native_selected,
            args.native_pi_bin,
            args.native_opencode_bin,
            source_format,
            first_ordinal=args.start_at,
        )

    result = {
        "source_files": len(files),
        "source_format": source_format.value,
        "parsed_sessions": checked_count,
        "expected_rejections": dict(sorted(rejected.items())),
        "targets": {
            target: {
                "converted": checked_count,
                "byte_validated": checked_count,
                "reparsed": checked_count,
                "semantic_projection_matches": checked_count,
                "loss_counter_matches": checked_count,
                "aggregate_dropped": dict(sorted(counter.items())),
            }
            for target, counter in aggregate_dropped.items()
        },
        "feature_counts": dict(sorted(feature_counts.items())),
        "manual": {
            "anonymous_sessions": len(selected),
            "targets_per_session": len(targets),
            "side_by_side_target_cases": len(selected) * len(targets),
            "content_safe_rows": manual_rows,
        },
        "native": native_result,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def project(
    events: tuple[Event, ...],
    *,
    source: bool,
    target: TargetFormat | None = None,
) -> Projection:
    if source and target == TargetFormat.COPILOT:
        events = copilot_grouped_source_events(events)
    timeline: list[tuple[Any, ...]] = []
    conversation: list[tuple[Any, ...]] = []
    calls: list[tuple[Any, ...]] = []
    results: list[tuple[Any, ...]] = []
    call_aliases: dict[str, str] = {}
    missing_call_ids: deque[str] = deque()
    orphan_aliases: dict[str, str] = {}
    call_names: dict[str, str] = {}
    available_calls: Counter[str] = Counter()
    generated_count = 0

    for event in events:
        if (
            event.kind == EventKind.MESSAGE
            and event.text
            and event.role
            in {
                Role.USER,
                Role.ASSISTANT,
            }
        ):
            item = (event.kind.value, event.role.value, event.text)
            conversation.append(item)
            timeline.append(("conversation", *item))
        elif (
            event.kind == EventKind.CONTEXT
            and event.role == Role.USER
            and event.payload.get("block_type") == "image"
            and portable_image(event.payload.get("image_url"))
        ):
            item = (event.kind.value, event.role.value, event.payload.get("image_url"))
            conversation.append(item)
            timeline.append(("conversation", *item))
        elif event.kind == EventKind.COMPACTION and event.text and target != TargetFormat.CLAUDE:
            item = (event.kind.value, Role.SYSTEM.value, event.text)
            conversation.append(item)
            timeline.append(("conversation", *item))
        elif event.kind == EventKind.TOOL_CALL:
            if source and not event.tool_call_id:
                raw_id = f"generated-{generated_count}"
                generated_count += 1
                missing_call_ids.append(raw_id)
            else:
                raw_id = event.tool_call_id or f"target-generated-{generated_count}"
                generated_count += int(event.tool_call_id is None)
            alias = call_aliases.setdefault(raw_id, f"call-{len(call_aliases)}")
            name = event.tool_name or "unknown_tool"
            arguments = event.payload.get("input", {})
            if not isinstance(arguments, dict):
                arguments = {"input": arguments}
            call_names.setdefault(alias, name)
            available_calls[raw_id] += 1
            item = (alias, name, canonical_json(arguments))
            calls.append(item)
            timeline.append(("call", *item))
        elif event.kind == EventKind.TOOL_RESULT:
            if source and not event.tool_call_id:
                raw_id = (
                    missing_call_ids.popleft()
                    if missing_call_ids
                    else f"missing-result-{len(orphan_aliases)}"
                )
            else:
                raw_id = event.tool_call_id or f"missing-result-{len(orphan_aliases)}"
            alias = call_aliases.get(raw_id)
            if source and target in {TargetFormat.OPENCODE, TargetFormat.COPILOT}:
                if available_calls[raw_id]:
                    available_calls[raw_id] -= 1
                else:
                    alias = call_aliases.setdefault(raw_id, f"call-{len(call_aliases)}")
                    name = event.tool_name or "unknown_tool"
                    call_names.setdefault(alias, name)
                    synthetic_call = (alias, name, canonical_json({}))
                    calls.append(synthetic_call)
                    timeline.append(("call", *synthetic_call))
            if alias is None:
                alias = orphan_aliases.setdefault(raw_id, f"orphan-{len(orphan_aliases)}")
            text, images = portable_result(event)
            item = (
                alias,
                call_names.get(alias, event.tool_name or "unknown_tool"),
                event.payload.get("is_error") is True and target != TargetFormat.CODEX,
                text,
                images,
            )
            results.append(item)
            timeline.append(("result", *item))
    projected_timeline = tuple(timeline)
    if source and target == TargetFormat.OPENCODE:
        projected_timeline = opencode_associated_timeline(projected_timeline)
    return Projection(projected_timeline, tuple(conversation), tuple(calls), tuple(results))


def copilot_grouped_source_events(events: tuple[Event, ...]) -> tuple[Event, ...]:
    """Model Copilot's one-text-field-per-native-message representation."""

    grouped: list[Event] = []
    index = 0
    while index < len(events):
        event = events[index]
        if (
            event.kind != EventKind.MESSAGE
            or not event.text
            or event.role not in {Role.USER, Role.ASSISTANT}
        ):
            grouped.append(event)
            index += 1
            continue
        texts = [event.text]
        following = index + 1
        while following < len(events):
            candidate = events[following]
            if not (
                candidate.kind == EventKind.MESSAGE
                and candidate.text
                and candidate.role == event.role
                and candidate.provenance.record_index == event.provenance.record_index
            ):
                break
            texts.append(candidate.text)
            following += 1
        grouped.append(replace(event, text="\n".join(texts)))
        index = following
    return tuple(grouped)


def opencode_associated_timeline(
    rows: tuple[tuple[Any, ...], ...],
) -> tuple[tuple[Any, ...], ...]:
    """Model OpenCode tool results living on their originating tool parts."""

    result_indices: dict[str, deque[int]] = {}
    for index, row in enumerate(rows):
        if row[0] == "result":
            result_indices.setdefault(str(row[1]), deque()).append(index)
    attached: set[int] = set()
    output: list[tuple[Any, ...]] = []
    for index, row in enumerate(rows):
        if index in attached:
            continue
        output.append(row)
        if row[0] != "call":
            continue
        matches = result_indices.setdefault(str(row[1]), deque())
        while matches and matches[0] < index:
            matches.popleft()
        if matches:
            result_index = matches.popleft()
            attached.add(result_index)
            output.append(rows[result_index])
    return tuple(output)


def portable_result(event: Event) -> tuple[str, tuple[str, ...]]:
    blocks = event.payload.get("content_blocks")
    blocks = blocks if isinstance(blocks, list) else []
    texts: list[str] = []
    images: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type in {"text", "input_text", "output_text"}:
            text = block.get("text")
            if isinstance(text, str) and text:
                texts.append(text)
        elif block_type in {"image", "input_image"}:
            image = block.get("image_url") or block.get("url")
            if portable_image(image):
                images.append(image)
    if not texts and event.text:
        texts.append(event.text)
    return "\n".join(texts), tuple(images)


def independent_dropped(
    events: tuple[Event, ...],
    *,
    target: TargetFormat,
    fallback_timestamp: str,
    has_title: bool,
) -> dict[str, int]:
    dropped: Counter[str] = Counter()
    seen_calls: set[str] = set()
    seen_results: set[str] = set()
    generated_calls: deque[str] = deque()
    generated_count = 0
    opencode_boundary = 0
    opencode_call_boundaries: dict[str, deque[int]] = {}
    for event in events:
        if (
            event.kind == EventKind.MESSAGE
            and event.text
            and event.role
            in {
                Role.USER,
                Role.ASSISTANT,
            }
        ):
            if target == TargetFormat.OPENCODE:
                opencode_boundary += 1
            if event.payload.get("ui_only_projection") is True:
                dropped["message:ui_only_projection"] += 1
            count_bad_time(event, dropped)
            continue
        if event.kind == EventKind.TOOL_CALL:
            call_id = event.tool_call_id
            if not call_id:
                call_id = f"generated-{generated_count}"
                generated_count += 1
                generated_calls.append(call_id)
                dropped["tool_call:missing_id"] += 1
            if not event.tool_name:
                dropped["tool_call:missing_name"] += 1
            if not isinstance(event.payload.get("input", {}), dict):
                dropped["tool_call:non_object_input"] += 1
            if call_id in seen_calls:
                dropped["tool_call:duplicate_id"] += 1
            seen_calls.add(call_id)
            if target == TargetFormat.OPENCODE:
                opencode_call_boundaries.setdefault(call_id, deque()).append(opencode_boundary)
            if event.payload.get("namespace"):
                dropped["tool_call:namespace"] += 1
            count_bad_time(event, dropped)
            continue
        if event.kind == EventKind.TOOL_RESULT:
            call_id = event.tool_call_id
            if not call_id:
                call_id = (
                    generated_calls.popleft()
                    if generated_calls
                    else f"missing-result-{generated_count}"
                )
                generated_count += 1
                dropped["tool_result:missing_id"] += 1
            elif call_id not in seen_calls:
                dropped["tool_result:orphan_id"] += 1
            if event.tool_call_id and event.tool_call_id in seen_results:
                dropped["tool_result:duplicate_id"] += 1
            if event.tool_call_id:
                seen_results.add(event.tool_call_id)
            if target == TargetFormat.OPENCODE:
                boundaries = opencode_call_boundaries.setdefault(call_id, deque())
                if boundaries and boundaries.popleft() < opencode_boundary:
                    dropped["tool_result:native_order_associated"] += 1
            count_result_losses(event, dropped, target)
            if target == TargetFormat.CODEX and event.payload.get("is_error") is True:
                dropped["tool_result:is_error"] += 1
            count_bad_time(event, dropped)
            continue
        if (
            event.kind == EventKind.CONTEXT
            and event.payload.get("block_type") == "image"
            and event.role == Role.USER
        ):
            if portable_image(event.payload.get("image_url")):
                if target == TargetFormat.OPENCODE:
                    opencode_boundary += 1
                count_bad_time(event, dropped)
            else:
                dropped["context:image"] += 1
            continue
        if event.kind == EventKind.COMPACTION and event.text:
            if target == TargetFormat.OPENCODE:
                opencode_boundary += 1
            if target == TargetFormat.CLAUDE:
                dropped[omission_key(event, target)] += 1
                continue
            count_bad_time(event, dropped)
            if event.payload.get("has_boundary_metadata") is True:
                dropped["compaction:boundary_metadata"] += 1
            continue
        dropped[omission_key(event, target)] += 1
    if target == TargetFormat.CODEX and has_title:
        dropped["session:title"] += 1
    if target == TargetFormat.OPENCODE:
        dropped.update(opencode_timestamp_adjustments(events, fallback_timestamp))
    elif target == TargetFormat.COPILOT:
        dropped.update(copilot_timestamp_adjustments(events, fallback_timestamp))
    return dict(sorted(dropped.items()))


def copilot_timestamp_adjustments(
    events: tuple[Event, ...], fallback_timestamp: str
) -> Counter[str]:
    """Independently model Copilot's nondecreasing event timestamps."""

    requested = [fallback_timestamp]
    pending_role: Role | None = None
    pending_record: int | None = None
    pending_timestamp: str | None = None
    seen_calls: set[str] = set()
    generated_calls: deque[str] = deque()
    generated_count = 0
    emitted_assets: set[str] = set()

    def record_asset(image_url: Any, event_time: str) -> None:
        image = portable_data_image(image_url)
        if image is None or image[0] not in MEDIA_TYPES:
            return
        asset_key = image[1]
        if asset_key not in emitted_assets:
            emitted_assets.add(asset_key)
            requested.append(event_time)

    def flush() -> None:
        nonlocal pending_role, pending_record, pending_timestamp
        if pending_role is not None:
            event_time = pending_timestamp or fallback_timestamp
            requested.append(event_time)
            if pending_role == Role.ASSISTANT:
                # Each queued call emits one execution_start after the
                # containing assistant message at the same time.
                requested.extend([event_time] * pending_call_count[0])
        pending_role = None
        pending_record = None
        pending_timestamp = None
        pending_call_count[0] = 0

    pending_call_count = [0]

    def queue(event: Event, role: Role) -> None:
        nonlocal pending_role, pending_record, pending_timestamp
        if pending_role is not None and (
            pending_role != role or pending_record != event.provenance.record_index
        ):
            flush()
        pending_role = role
        pending_record = event.provenance.record_index
        pending_timestamp = pending_timestamp or (
            valid_rfc3339(event.timestamp) or fallback_timestamp
        )

    for event in events:
        if (
            event.kind == EventKind.MESSAGE
            and event.text
            and event.role
            in {
                Role.USER,
                Role.ASSISTANT,
            }
        ):
            queue(event, event.role)
            continue
        if (
            event.kind == EventKind.CONTEXT
            and event.role == Role.USER
            and event.payload.get("block_type") == "image"
            and portable_image(event.payload.get("image_url"))
        ):
            event_time = valid_rfc3339(event.timestamp) or fallback_timestamp
            record_asset(event.payload.get("image_url"), event_time)
            queue(event, Role.USER)
            continue
        if event.kind == EventKind.TOOL_CALL:
            call_id = event.tool_call_id
            if not call_id:
                call_id = f"generated-{generated_count}"
                generated_count += 1
                generated_calls.append(call_id)
            seen_calls.add(call_id)
            queue(event, Role.ASSISTANT)
            pending_call_count[0] += 1
            continue
        if event.kind == EventKind.TOOL_RESULT:
            flush()
            call_id = event.tool_call_id
            if not call_id:
                call_id = (
                    generated_calls.popleft()
                    if generated_calls
                    else f"missing-result-{generated_count}"
                )
                generated_count += 1
            event_time = valid_rfc3339(event.timestamp) or fallback_timestamp
            if call_id not in seen_calls:
                requested.extend((event_time, event_time))
                seen_calls.add(call_id)
            for image_url in portable_result(event)[1]:
                record_asset(image_url, event_time)
            requested.append(event_time)
            continue
        if event.kind == EventKind.COMPACTION and event.text:
            flush()
            requested.append(valid_rfc3339(event.timestamp) or fallback_timestamp)
    flush()

    result: Counter[str] = Counter()
    last_time: datetime | None = None
    for timestamp in requested:
        current = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if last_time is not None and current < last_time:
            result["timestamp:native_order_adjusted"] += 1
            current = last_time + timedelta(microseconds=1)
        last_time = current
    return result


def opencode_timestamp_adjustments(
    events: tuple[Event, ...], fallback_timestamp: str
) -> Counter[str]:
    """Independently model OpenCode's native-message timestamp ordering."""

    requested_timestamps: list[str] = []
    pending_role: Role | None = None
    pending_record: int | None = None
    pending_timestamp: str | None = None
    call_parts: Counter[str] = Counter()
    generated_calls: deque[str] = deque()
    generated_count = 0

    def flush() -> None:
        nonlocal pending_role, pending_record, pending_timestamp
        if pending_role is not None:
            requested_timestamps.append(pending_timestamp or fallback_timestamp)
        pending_role = None
        pending_record = None
        pending_timestamp = None

    def queue(event: Event, role: Role) -> None:
        nonlocal pending_role, pending_record, pending_timestamp
        if pending_role is not None and (
            pending_role != role or pending_record != event.provenance.record_index
        ):
            flush()
        pending_role = role
        pending_record = event.provenance.record_index
        pending_timestamp = pending_timestamp or (
            valid_rfc3339(event.timestamp) or fallback_timestamp
        )

    for event in events:
        if (
            event.kind == EventKind.MESSAGE
            and event.text
            and event.role
            in {
                Role.USER,
                Role.ASSISTANT,
            }
        ):
            queue(event, event.role)
            continue
        if event.kind == EventKind.TOOL_CALL:
            call_id = event.tool_call_id
            if not call_id:
                call_id = f"generated-{generated_count}"
                generated_count += 1
                generated_calls.append(call_id)
            call_parts[call_id] += 1
            queue(event, Role.ASSISTANT)
            continue
        if event.kind == EventKind.TOOL_RESULT:
            flush()
            call_id = event.tool_call_id
            if not call_id:
                call_id = (
                    generated_calls.popleft()
                    if generated_calls
                    else f"missing-result-{generated_count}"
                )
                generated_count += 1
            if call_parts[call_id]:
                call_parts[call_id] -= 1
            else:
                requested_timestamps.append(valid_rfc3339(event.timestamp) or fallback_timestamp)
            continue
        if (
            event.kind == EventKind.CONTEXT
            and event.payload.get("block_type") == "image"
            and event.role == Role.USER
            and portable_image(event.payload.get("image_url"))
        ):
            queue(event, Role.USER)
            continue
        if event.kind == EventKind.COMPACTION and event.text:
            flush()
            timestamp = valid_rfc3339(event.timestamp) or fallback_timestamp
            requested_timestamps.extend((timestamp, timestamp))
    flush()

    result: Counter[str] = Counter()
    last_created = -1
    for timestamp in requested_timestamps:
        requested_ms = timestamp_ms(timestamp)
        ordered_ms = max(requested_ms, last_created)
        if ordered_ms != requested_ms:
            result["timestamp:native_order_adjusted"] += 1
        last_created = ordered_ms
    return result


def count_result_losses(event: Event, dropped: Counter[str], target: TargetFormat) -> None:
    blocks = event.payload.get("content_blocks")
    blocks = blocks if isinstance(blocks, list) else []
    for block in blocks:
        if not isinstance(block, dict):
            dropped[
                "tool_result:block"
                if target in {TargetFormat.CLAUDE, TargetFormat.CODEX}
                else "tool_result:malformed_block"
            ] += 1
            continue
        block_type = block.get("type") if isinstance(block.get("type"), str) else None
        if target == TargetFormat.CLAUDE:
            if block_type == "text" and isinstance(block.get("text"), str):
                continue
            if block_type == "image" and portable_image(block.get("image_url")):
                continue
            if block_type == "tool_reference" and isinstance(block.get("tool_name"), str):
                continue
            dropped[f"tool_result:{block_type or 'block'}"] += 1
            continue
        if target == TargetFormat.CODEX:
            if block_type == "text" and isinstance(block.get("text"), str):
                continue
            if block_type == "image" and isinstance(block.get("image_url"), str):
                continue
            dropped[f"tool_result:{block_type or 'block'}"] += 1
            continue
        if block_type in {"text", "input_text", "output_text"}:
            if not isinstance(block.get("text"), str) or not block.get("text"):
                dropped["tool_result:malformed_text"] += 1
        elif block_type in {"image", "input_image"}:
            if not portable_image(block.get("image_url") or block.get("url")):
                dropped["tool_result:image"] += 1
            elif target == TargetFormat.COPILOT:
                dropped["tool_result:image_provider_dependent"] += 1
        else:
            dropped[f"tool_result:{block_type or 'unknown_block'}"] += 1


def count_bad_time(event: Event, dropped: Counter[str]) -> None:
    if event.timestamp and not valid_rfc3339(event.timestamp):
        dropped["timestamp:invalid"] += 1


def omission_key(event: Event, target: TargetFormat) -> str:
    if target in {TargetFormat.PI, TargetFormat.OPENCODE, TargetFormat.COPILOT}:
        if event.kind == EventKind.MESSAGE and event.role not in {
            Role.USER,
            Role.ASSISTANT,
        }:
            return "message:privileged_role"
        if event.kind == EventKind.CONTEXT and event.role not in {Role.USER, None}:
            return "context:privileged_image"
        if event.kind == EventKind.OPAQUE:
            reason = event.payload.get("reason")
            return f"opaque:{reason}" if isinstance(reason, str) and reason else "opaque"
        return event.kind.value
    if event.kind == EventKind.MESSAGE and event.role == Role.SYSTEM:
        return "message:privileged_role"
    if event.kind == EventKind.COMPACTION and event.payload.get("replacement_history_expanded"):
        return "compaction:replacement_history_expanded"
    if event.kind == EventKind.CONTEXT:
        if event.role == Role.SYSTEM and event.payload.get("block_type") == "image":
            return "context:privileged_image"
        if event.payload.get("source_record_type"):
            return f"context:{event.payload['source_record_type']}"
        return f"context:{event.payload.get('block_type', 'unknown')}"
    if event.kind == EventKind.OPAQUE:
        detail = next(
            (
                event.payload.get(key)
                for key in (
                    "reason",
                    "source_record_type",
                    "source_event_type",
                    "source_block_type",
                    "source_item_type",
                )
                if event.payload.get(key)
            ),
            "unknown",
        )
        return f"opaque:{detail}"
    return event.kind.value


def portable_image(value: Any) -> bool:
    image = portable_data_image(value)
    return image is not None and image[0] in MEDIA_TYPES


def assert_projection_equal(
    ordinal: int, target: str, source: Projection, destination: Projection
) -> None:
    for name in ("conversation", "calls", "results", "timeline"):
        source_rows = getattr(source, name)
        destination_rows = getattr(destination, name)
        if name == "timeline" and target in {
            TargetFormat.OPENCODE.value,
            TargetFormat.COPILOT.value,
            "opencode-native-export",
        }:
            assert_tool_results_follow_calls(ordinal, target, destination_rows)
            source_rows = normalized_tool_segments(source_rows)
            destination_rows = normalized_tool_segments(destination_rows)
        if source_rows != destination_rows:
            mismatch = next(
                (
                    index
                    for index, (left, right) in enumerate(
                        zip(source_rows, destination_rows, strict=False)
                    )
                    if left != right
                ),
                min(len(source_rows), len(destination_rows)),
            )
            source_shape = (
                projection_row_shape(name, source_rows[mismatch])
                if mismatch < len(source_rows)
                else "<missing>"
            )
            destination_shape = (
                projection_row_shape(name, destination_rows[mismatch])
                if mismatch < len(destination_rows)
                else "<missing>"
            )
            start = max(0, mismatch - 3)
            end = mismatch + 4
            source_kinds = [str(row[0]) for row in source_rows[start:end]]
            destination_kinds = [str(row[0]) for row in destination_rows[start:end]]
            raise RuntimeError(
                f"{name} projection mismatch at anonymous session {ordinal} for {target}; "
                f"row={mismatch}, source_count={len(source_rows)}, "
                f"target_count={len(destination_rows)}, source={source_shape}, "
                f"target={destination_shape}, source_kinds={source_kinds}, "
                f"target_kinds={destination_kinds}"
            )


def projection_row_shape(name: str, row: tuple[Any, ...]) -> str:
    if name == "timeline":
        if row[0] == "tool_segment":
            return f"tool_segment:calls={len(row[1])}:results={len(row[2])}"
        return f"{row[0]}:fields={len(row)}"
    return safe_shape(row)


def normalized_tool_segments(
    rows: tuple[tuple[Any, ...], ...],
) -> tuple[tuple[Any, ...], ...]:
    normalized: list[tuple[Any, ...]] = []
    calls: list[tuple[Any, ...]] = []
    results: list[tuple[Any, ...]] = []

    def flush() -> None:
        if calls or results:
            normalized.append(("tool_segment", tuple(calls), tuple(results)))
            calls.clear()
            results.clear()

    for row in rows:
        if row[0] == "call":
            calls.append(row)
        elif row[0] == "result":
            results.append(row)
        else:
            flush()
            normalized.append(row)
    flush()
    return tuple(normalized)


def assert_tool_results_follow_calls(
    ordinal: int, target: str, rows: tuple[tuple[Any, ...], ...]
) -> None:
    seen: Counter[str] = Counter()
    for row in rows:
        if row[0] == "call":
            seen[str(row[1])] += 1
        elif row[0] == "result":
            alias = str(row[1])
            if not seen[alias]:
                raise RuntimeError(
                    f"tool result precedes its call at anonymous session {ordinal} for {target}"
                )
            seen[alias] -= 1


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
    ) or any(portable_result(event)[1] for event in session.events):
        features.append("images")
    if kinds[EventKind.OPAQUE]:
        features.append("branch_or_meta")
    portable = [
        event
        for event in session.events
        if event.kind in {EventKind.MESSAGE, EventKind.TOOL_CALL, EventKind.TOOL_RESULT}
    ]
    if (
        portable
        and portable[-1].kind in {EventKind.MESSAGE, EventKind.TOOL_CALL}
        and (portable[-1].role == Role.USER or portable[-1].kind == EventKind.TOOL_CALL)
    ):
        features.append("interrupted")
    if source_bytes >= 10 * 1024 * 1024:
        features.append("large_10m")
    elif source_bytes >= 1024 * 1024:
        features.append("large_1m")
    if not features:
        features.append("basic")
    return tuple(features)


def select_manual(sessions: list[SessionSummary], count: int) -> list[SessionSummary]:
    count = min(max(count, 0), len(sessions))
    selected: list[SessionSummary] = []
    covered: set[str] = set()
    candidates = sorted(
        sessions,
        key=lambda item: (-len(item.features), -item.source_bytes, item.ordinal),
    )
    while len(selected) < count:
        remaining = [item for item in candidates if item not in selected]
        if not remaining:
            break
        choice = max(
            remaining,
            key=lambda item: (
                len(set(item.features) - covered),
                item.source_bytes,
                -item.ordinal,
            ),
        )
        selected.append(choice)
        covered.update(choice.features)
    return selected


def select_native(sessions: list[SessionSummary], count: int) -> list[SessionSummary]:
    """Choose feature-diverse, reasonably sized native smoke inputs."""

    count = min(max(count, 0), len(sessions))
    selected: list[SessionSummary] = []
    for feature in ("compaction", "images", "interrupted", "tools", "large_1m"):
        candidates = [
            item for item in sessions if feature in item.features and item not in selected
        ]
        if candidates:
            selected.append(min(candidates, key=lambda item: item.source_bytes))
    remaining = sorted(
        (item for item in sessions if item not in selected),
        key=lambda item: (item.source_bytes, item.ordinal),
    )
    selected.extend(remaining[: max(0, count - len(selected))])
    return selected[:count]


def native_smoke(
    files: list[Path],
    selected: list[SessionSummary],
    pi_bin: Path | None,
    opencode_bin: Path,
    source_format: AgentFormat,
    *,
    first_ordinal: int,
) -> dict[str, Any]:
    feature_counts: Counter[str] = Counter()
    temp_path: Path | None = None
    pi_loaded = 0
    opencode_imported = 0
    with tempfile.TemporaryDirectory(prefix="session-bridge-native-corpus-") as directory:
        temporary = Path(directory)
        temp_path = temporary
        os.chmod(temporary, 0o700)
        work = temporary / "work"
        work.mkdir(mode=0o700)
        pi_home = temporary / "pi-home"
        pi_home.mkdir(mode=0o700)
        pi_files = temporary / "pi-files"
        pi_files.mkdir(mode=0o700)
        pi_session_directory = pi_home / "sessions"
        pi_session_directory.mkdir(mode=0o700)
        pi_environment = isolated_pi_env(temporary, pi_home)
        opencode_environment = isolated_opencode_env(temporary)
        if source_format != AgentFormat.PI:
            assert pi_bin is not None
            require_version(pi_bin, pi.PINNED_PI_VERSION, "Pi", pi_environment)
        require_version(
            opencode_bin,
            opencode.PINNED_OPENCODE_VERSION,
            "OpenCode",
            opencode_environment,
        )
        for item in selected:
            session = load_source(files[item.ordinal - first_ordinal], source_format)
            target_uuid = str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"session-bridge-native-{item.ordinal}")
            )
            feature_counts.update(item.features)

            if source_format != AgentFormat.PI:
                assert pi_bin is not None
                pi_artifact = convert_session(
                    session,
                    ConversionOptions(
                        target_format=TargetFormat.PI,
                        session_id=target_uuid,
                        cwd=work,
                    ),
                )
                pi_path = pi_files / f"{item.ordinal}.jsonl"
                write_private_atomic(pi_path, pi_artifact.native_bytes)
                completed = subprocess.run(
                    [
                        str(pi_bin),
                        "--mode",
                        "rpc",
                        "--offline",
                        "--no-extensions",
                        "--no-skills",
                        "--no-prompt-templates",
                        "--no-context-files",
                        "--session",
                        str(pi_path),
                        "--session-dir",
                        str(pi_session_directory),
                    ],
                    cwd=work,
                    env=pi_environment,
                    input='{"type":"get_entries"}\n',
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if completed.returncode != 0:
                    raise RuntimeError(f"Pi native load failed at anonymous session {item.ordinal}")
                response = next(
                    (
                        json.loads(line)
                        for line in completed.stdout.splitlines()
                        if line.startswith("{") and '"command":"get_entries"' in line
                    ),
                    None,
                )
                if not isinstance(response, dict) or response.get("success") is not True:
                    raise RuntimeError(f"Pi native RPC failed at anonymous session {item.ordinal}")
                source_records = [
                    json.loads(line) for line in pi_artifact.native_bytes.splitlines()
                ]
                entries = response.get("data", {}).get("entries")
                if (
                    not isinstance(entries, list)
                    or entries[: len(source_records) - 1] != (source_records[1:])
                ):
                    expected_entries = source_records[1:]
                    mismatch_index = next(
                        (
                            index
                            for index, (expected, actual) in enumerate(
                                zip(
                                    expected_entries,
                                    entries if isinstance(entries, list) else [],
                                    strict=False,
                                )
                            )
                            if expected != actual
                        ),
                        min(
                            len(expected_entries),
                            len(entries) if isinstance(entries, list) else 0,
                        ),
                    )
                    expected_type = (
                        expected_entries[mismatch_index].get("type")
                        if mismatch_index < len(expected_entries)
                        else "<missing>"
                    )
                    actual_type = (
                        entries[mismatch_index].get("type")
                        if isinstance(entries, list)
                        and mismatch_index < len(entries)
                        and isinstance(entries[mismatch_index], dict)
                        else "<missing>"
                    )
                    on_disk = pi_path.read_bytes()
                    raise RuntimeError(
                        f"Pi native prefix mismatch at anonymous session {item.ordinal}; "
                        f"entry={mismatch_index}, expected_type={expected_type}, "
                        f"actual_type={actual_type}, expected_count={len(expected_entries)}, "
                        f"actual_count={len(entries) if isinstance(entries, list) else -1}, "
                        f"disk_prefix={on_disk.startswith(pi_artifact.native_bytes)}, "
                        f"disk_delta={len(on_disk) - len(pi_artifact.native_bytes)}, "
                        f"stderr_bytes={len(completed.stderr.encode())}"
                    )
                if not pi_path.read_bytes().startswith(pi_artifact.native_bytes):
                    raise RuntimeError(
                        f"Pi on-disk prefix mismatch at anonymous session {item.ordinal}"
                    )
                pi_loaded += 1

            opencode_artifact = convert_session(
                session,
                ConversionOptions(
                    target_format=TargetFormat.OPENCODE,
                    session_id=target_uuid,
                    cwd=work,
                ),
            )
            manifest_path = temporary / "manifests" / f"{item.ordinal}.json"
            install_opencode_artifact(
                opencode_artifact,
                manifest_path=manifest_path,
                target_cli=opencode_bin,
                environ=opencode_environment,
            )
            exported_path = temporary / f"exported-{item.ordinal}.json"
            exported_descriptor = os.open(
                exported_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(exported_descriptor, "wb") as exported_stream:
                exported = subprocess.run(
                    [
                        str(opencode_bin),
                        "export",
                        opencode_artifact.session_id,
                        "--pure",
                    ],
                    cwd=work,
                    env=opencode_environment,
                    check=False,
                    stdout=exported_stream,
                    stderr=subprocess.PIPE,
                    timeout=60,
                )
            if exported.returncode != 0:
                raise RuntimeError(
                    f"OpenCode native export failed at anonymous session {item.ordinal}"
                )
            exported_raw = exported_path.read_bytes()
            try:
                exported_value = json.loads(exported_raw)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"OpenCode native export returned malformed JSON at anonymous session "
                    f"{item.ordinal}; bytes={len(exported_raw)}, "
                    f"lines={len(exported_raw.splitlines())}, error_offset={exc.pos}, "
                    f"stderr_bytes={len(exported.stderr)}"
                ) from None
            exported_bytes = (json.dumps(exported_value) + "\n").encode()
            try:
                opencode.validate_native_bytes(exported_bytes, opencode_artifact.session_id)
            except Exception:
                exported_messages = (
                    exported_value.get("messages", []) if isinstance(exported_value, dict) else []
                )
                exported_ids = [
                    message.get("info", {}).get("id")
                    for message in exported_messages
                    if isinstance(message, dict) and isinstance(message.get("info"), dict)
                ]
                inversion = next(
                    (
                        index
                        for index in range(1, len(exported_ids))
                        if isinstance(exported_ids[index - 1], str)
                        and isinstance(exported_ids[index], str)
                        and exported_ids[index] <= exported_ids[index - 1]
                    ),
                    -1,
                )
                previous_info = (
                    exported_messages[inversion - 1].get("info", {}) if inversion > 0 else {}
                )
                current_info = (
                    exported_messages[inversion].get("info", {}) if inversion >= 0 else {}
                )
                raise RuntimeError(
                    "OpenCode native export reordered ascending messages at anonymous "
                    f"session {item.ordinal}; messages={len(exported_messages)}, "
                    f"inversion={inversion}, previous_role={previous_info.get('role')}, "
                    f"previous_summary={previous_info.get('summary') is True}, "
                    f"current_role={current_info.get('role')}, "
                    f"current_summary={current_info.get('summary') is True}"
                ) from None
            normalized_export_path = temporary / f"normalized-{item.ordinal}.json"
            write_private_atomic(normalized_export_path, exported_bytes)
            exported_projection = project(
                opencode.parse(normalized_export_path).events, source=False
            )
            assert_projection_equal(
                item.ordinal,
                "opencode-native-export",
                project(session.events, source=True, target=TargetFormat.OPENCODE),
                exported_projection,
            )
            opencode_imported += 1
    if temp_path is None or temp_path.exists():
        raise RuntimeError("private native validation workspace was not removed")
    return {
        "anonymous_sessions": len(selected),
        "pi_loaded_via_offline_rpc": pi_loaded,
        "pi_exact_prefixes": pi_loaded,
        "opencode_official_import_export": opencode_imported,
        "opencode_semantic_projection_matches": opencode_imported,
        "feature_case_counts": dict(sorted(feature_counts.items())),
        "private_workspace_removed": True,
    }


def require_version(
    binary: Path,
    expected: str,
    label: str,
    environment: dict[str, str],
) -> None:
    completed = subprocess.run(
        [str(binary), "--version"],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if completed.returncode != 0 or completed.stdout.strip() != expected:
        raise RuntimeError(f"{label} native validation requires version {expected}")


def minimal_environment(home: Path, temporary: Path) -> dict[str, str]:
    values = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "TERM": "dumb",
        "NO_COLOR": "1",
        "TMPDIR": str(temporary / "tmp"),
    }
    for key in ("LANG", "LC_ALL", "LC_CTYPE"):
        if value := os.environ.get(key):
            values[key] = value
    return values


def isolated_pi_env(temporary: Path, pi_home: Path) -> dict[str, str]:
    home = temporary / "pi-os-home"
    values = {
        **minimal_environment(home, temporary),
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_CACHE_HOME": str(home / "cache"),
        "XDG_STATE_HOME": str(home / "state"),
        "PI_CODING_AGENT_DIR": str(pi_home),
        "PI_OFFLINE": "1",
    }
    for key in (
        "HOME",
        "XDG_DATA_HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "PI_CODING_AGENT_DIR",
        "TMPDIR",
    ):
        Path(values[key]).mkdir(parents=True, mode=0o700, exist_ok=True)
    return values


def isolated_opencode_env(temporary: Path) -> dict[str, str]:
    home = temporary / "opencode-home"
    values = {
        **minimal_environment(home, temporary),
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_CACHE_HOME": str(home / "cache"),
        "XDG_STATE_HOME": str(home / "state"),
        "OPENCODE_CONFIG_DIR": str(home / "opencode-config"),
        "OPENCODE_DISABLE_AUTOUPDATE": "true",
        "OPENCODE_DISABLE_PRUNE": "true",
    }
    for key in (
        "HOME",
        "XDG_DATA_HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "OPENCODE_CONFIG_DIR",
        "TMPDIR",
    ):
        Path(values[key]).mkdir(parents=True, mode=0o700, exist_ok=True)
    return values


def write_manual_report(
    report_path: Path,
    files: list[Path],
    sessions: list[SessionSummary],
    *,
    first_ordinal: int,
    source_format: AgentFormat,
    targets: tuple[TargetFormat, ...],
) -> int:
    """Reread selected sessions and stream a private, content-safe report."""

    report_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(report_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    exact_rows = 0
    with tempfile.TemporaryDirectory(prefix="session-bridge-manual-corpus-") as directory:
        temporary = Path(directory)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(manual_report_header())
            for sample_index, summary in enumerate(sessions, start=1):
                session = load_source(files[summary.ordinal - first_ordinal], source_format)
                checked = check_session_targets(
                    session,
                    ordinal=summary.ordinal,
                    source_bytes=summary.source_bytes,
                    features=summary.features,
                    targets=targets,
                    temporary=temporary,
                    aggregate_dropped=None,
                    collect=True,
                )
                assert checked is not None
                rendered = render_manual_session(checked, sample_index)
                stream.write(rendered)
                exact_rows += rendered.count(" exact=")
    return exact_rows


def manual_report_header() -> str:
    return "\n".join(
        [
            "CONTENT-SAFE MANUAL SIDE-BY-SIDE REPORT",
            "No paths, IDs, titles, text, tool values, timestamps, CWDs, or hashes.",
            "",
        ]
    )


def render_manual_session(checked: CheckedSession, sample_index: int) -> str:
    lines: list[str] = []
    size_bucket = (
        ">=10MiB"
        if checked.source_bytes >= 10 * 1024 * 1024
        else ">=1MiB"
        if checked.source_bytes >= 1024 * 1024
        else "<1MiB"
    )
    for target in checked.targets:
        destination = checked.targets[target]
        expected = checked.expected[target]
        lines.append(
            f"sample={sample_index:02d} target={target} size={size_bucket} "
            f"features={','.join(checked.features)}"
        )
        for category in ("conversation", "calls", "results"):
            source_rows = getattr(expected, category)
            target_rows = getattr(destination, category)
            row_count = max(len(source_rows), len(target_rows))
            for row_index in range(row_count):
                source_row = source_rows[row_index] if row_index < len(source_rows) else None
                target_row = target_rows[row_index] if row_index < len(target_rows) else None
                lines.append(
                    f"  {category[0].upper()}{row_index:04d} "
                    f"source={safe_shape(source_row)} target={safe_shape(target_row)} "
                    f"exact={source_row == target_row}"
                )
        lines.append(
            "  losses "
            f"keys={len(checked.dropped[target])} "
            f"count={sum(checked.dropped[target].values())} exact=True"
        )
        lines.append("")
    return "\n".join(lines) + "\n"


def safe_shape(row: tuple[Any, ...] | None) -> str:
    if row is None:
        return "<missing>"
    if row[0] in {EventKind.MESSAGE.value, EventKind.COMPACTION.value}:
        return f"{row[0]}:{row[1]}:chars={len(row[2])}"
    if row[0] == EventKind.CONTEXT.value:
        value = row[2]
        media = value.partition(";")[0].removeprefix("data:")
        return f"context:{row[1]}:media={media}:chars={len(value)}"
    if len(row) == 3:
        return f"tool_call:args_json_chars={len(row[2])}"
    text = row[3]
    images = row[4]
    return (
        f"tool_result:error={row[2]}:text_chars={len(text)}:images={len(images)}:"
        f"image_chars={sum(len(image) for image in images)}"
    )


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def timestamp_ms(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


if __name__ == "__main__":
    raise SystemExit(main())
