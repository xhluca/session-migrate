#!/usr/bin/env python3
"""Content-safe validation for real Claude -> Pi/OpenCode/Copilot conversion.

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
import tempfile
import uuid
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from session_bridge.conversion import (
    ConversionOptions,
    convert_session,
    install_opencode_artifact,
)
from session_bridge.formats import claude, copilot, opencode, pi
from session_bridge.formats.common import portable_data_image, valid_rfc3339
from session_bridge.jsonl import write_private_atomic
from session_bridge.model import Event, EventKind, Role, Session, TargetFormat

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
    targets: dict[str, Projection]
    dropped: dict[str, dict[str, int]]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--claude-root", type=Path, required=True)
    parser.add_argument("--manual-report", type=Path)
    parser.add_argument("--manual-count", type=int, default=20)
    parser.add_argument("--native-pi-bin", type=Path)
    parser.add_argument("--native-opencode-bin", type=Path)
    parser.add_argument("--native-count", type=int, default=0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    files = sorted((args.claude_root / "projects").glob("*/*.jsonl"))
    checked: list[CheckedSession] = []
    aggregate_dropped = {"pi": Counter(), "opencode": Counter(), "copilot": Counter()}
    feature_counts: Counter[str] = Counter()

    with tempfile.TemporaryDirectory(prefix="session-bridge-corpus-") as directory:
        temporary = Path(directory)
        for ordinal, path in enumerate(files, start=1):
            try:
                session = claude.parse(path)
                source_projection = project(session.events, source=True)
                features = classify(session, path.stat().st_size)
                target_projections: dict[str, Projection] = {}
                dropped_by_target: dict[str, dict[str, int]] = {}
                for target in (
                    TargetFormat.PI,
                    TargetFormat.OPENCODE,
                    TargetFormat.COPILOT,
                ):
                    target_uuid = str(
                        uuid.uuid5(uuid.NAMESPACE_URL, f"session-bridge-corpus-{ordinal}")
                    )
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
                    )
                    suffix = "json" if target == TargetFormat.OPENCODE else "jsonl"
                    converted_path = temporary / f"{ordinal}-{target.value}.{suffix}"
                    converted_path.write_bytes(artifact.native_bytes)
                    if target == TargetFormat.PI:
                        pi.validate_native_bytes(artifact.native_bytes, artifact.session_id)
                        parsed = pi.parse(converted_path)
                    elif target == TargetFormat.OPENCODE:
                        opencode.validate_native_bytes(artifact.native_bytes, artifact.session_id)
                        parsed = opencode.parse(converted_path)
                    else:
                        copilot.validate_native_bytes(artifact.native_bytes, artifact.session_id)
                        parsed = copilot.parse(converted_path)
                    target_projection = project(parsed.events, source=False)
                    assert_projection_equal(
                        ordinal,
                        target.value,
                        source_projection,
                        target_projection,
                    )
                    if artifact.dropped != expected_dropped:
                        raise RuntimeError(
                            f"loss counter mismatch at anonymous session {ordinal} "
                            f"for {target.value}"
                        )
                    target_projections[target.value] = target_projection
                    dropped_by_target[target.value] = artifact.dropped
                    aggregate_dropped[target.value].update(artifact.dropped)
                    converted_path.unlink()
                checked.append(
                    CheckedSession(
                        ordinal=ordinal,
                        source_bytes=path.stat().st_size,
                        features=features,
                        source=source_projection,
                        targets=target_projections,
                        dropped=dropped_by_target,
                    )
                )
                feature_counts.update(features)
            except Exception:
                raise RuntimeError(
                    f"additional-target corpus validation failed at anonymous session {ordinal}"
                ) from None

    selected = select_manual(checked, args.manual_count)
    manual_rows = 0
    if args.manual_report:
        report = render_manual_report(selected)
        args.manual_report.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            args.manual_report,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(report)
        manual_rows = report.count(" exact=")

    native_result: dict[str, Any] | None = None
    if args.native_count:
        if not args.native_pi_bin or not args.native_opencode_bin:
            raise RuntimeError("native validation requires both pinned target binaries")
        native_selected = select_native(checked, args.native_count)
        native_result = native_smoke(
            files,
            native_selected,
            args.native_pi_bin,
            args.native_opencode_bin,
        )

    result = {
        "source_files": len(files),
        "parsed_sessions": len(checked),
        "targets": {
            target: {
                "converted": len(checked),
                "byte_validated": len(checked),
                "reparsed": len(checked),
                "semantic_projection_matches": len(checked),
                "loss_counter_matches": len(checked),
                "aggregate_dropped": dict(sorted(counter.items())),
            }
            for target, counter in aggregate_dropped.items()
        },
        "feature_counts": dict(sorted(feature_counts.items())),
        "manual": {
            "anonymous_sessions": len(selected),
            "targets_per_session": 3,
            "side_by_side_target_cases": len(selected) * 3,
            "content_safe_rows": manual_rows,
        },
        "native": native_result,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def project(events: tuple[Event, ...], *, source: bool) -> Projection:
    timeline: list[tuple[Any, ...]] = []
    conversation: list[tuple[Any, ...]] = []
    calls: list[tuple[Any, ...]] = []
    results: list[tuple[Any, ...]] = []
    call_aliases: dict[str, str] = {}
    missing_call_ids: deque[str] = deque()
    orphan_aliases: dict[str, str] = {}
    call_names: dict[str, str] = {}
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
        elif event.kind == EventKind.COMPACTION and event.text:
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
            if alias is None:
                alias = orphan_aliases.setdefault(raw_id, f"orphan-{len(orphan_aliases)}")
            text, images = portable_result(event)
            item = (
                alias,
                call_names.get(alias, event.tool_name or "unknown_tool"),
                event.payload.get("is_error") is True,
                text,
                images,
            )
            results.append(item)
            timeline.append(("result", *item))
    return Projection(tuple(timeline), tuple(conversation), tuple(calls), tuple(results))


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
) -> dict[str, int]:
    dropped: Counter[str] = Counter()
    seen_calls: set[str] = set()
    seen_results: set[str] = set()
    generated_calls: deque[str] = deque()
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
            count_result_losses(event, dropped, target)
            count_bad_time(event, dropped)
            continue
        if (
            event.kind == EventKind.CONTEXT
            and event.payload.get("block_type") == "image"
            and event.role == Role.USER
        ):
            if portable_image(event.payload.get("image_url")):
                count_bad_time(event, dropped)
            else:
                dropped["context:image"] += 1
            continue
        if event.kind == EventKind.COMPACTION and event.text:
            count_bad_time(event, dropped)
            if event.payload.get("has_boundary_metadata") is True:
                dropped["compaction:boundary_metadata"] += 1
            continue
        dropped[omission_key(event)] += 1
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
            current = last_time
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
            dropped["tool_result:malformed_block"] += 1
            continue
        block_type = block.get("type") if isinstance(block.get("type"), str) else None
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


def omission_key(event: Event) -> str:
    if event.kind == EventKind.MESSAGE and event.role not in {Role.USER, Role.ASSISTANT}:
        return "message:privileged_role"
    if event.kind == EventKind.CONTEXT and event.role not in {Role.USER, None}:
        return "context:privileged_image"
    if event.kind == EventKind.OPAQUE:
        reason = event.payload.get("reason")
        return f"opaque:{reason}" if isinstance(reason, str) and reason else "opaque"
    return event.kind.value


def portable_image(value: Any) -> bool:
    image = portable_data_image(value)
    return image is not None and image[0] in MEDIA_TYPES


def assert_projection_equal(
    ordinal: int, target: str, source: Projection, destination: Projection
) -> None:
    for name in ("timeline", "conversation", "calls", "results"):
        if getattr(source, name) != getattr(destination, name):
            raise RuntimeError(
                f"{name} projection mismatch at anonymous session {ordinal} for {target}"
            )


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


def select_manual(sessions: list[CheckedSession], count: int) -> list[CheckedSession]:
    count = min(max(count, 0), len(sessions))
    selected: list[CheckedSession] = []
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


def select_native(sessions: list[CheckedSession], count: int) -> list[CheckedSession]:
    """Choose feature-diverse, reasonably sized native smoke inputs."""

    count = min(max(count, 0), len(sessions))
    selected: list[CheckedSession] = []
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
    selected: list[CheckedSession],
    pi_bin: Path,
    opencode_bin: Path,
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
        require_version(pi_bin, pi.PINNED_PI_VERSION, "Pi", pi_environment)
        require_version(
            opencode_bin,
            opencode.PINNED_OPENCODE_VERSION,
            "OpenCode",
            opencode_environment,
        )
        for item in selected:
            session = claude.parse(files[item.ordinal - 1])
            target_uuid = str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"session-bridge-native-{item.ordinal}")
            )
            feature_counts.update(item.features)

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
            source_records = [json.loads(line) for line in pi_artifact.native_bytes.splitlines()]
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
                item.source,
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


def render_manual_report(sessions: list[CheckedSession]) -> str:
    lines = [
        "CONTENT-SAFE MANUAL SIDE-BY-SIDE REPORT",
        "No paths, IDs, titles, text, tool values, timestamps, CWDs, or hashes.",
        "",
    ]
    for sample_index, checked in enumerate(sessions, start=1):
        size_bucket = (
            ">=10MiB"
            if checked.source_bytes >= 10 * 1024 * 1024
            else ">=1MiB"
            if checked.source_bytes >= 1024 * 1024
            else "<1MiB"
        )
        for target in ("pi", "opencode", "copilot"):
            destination = checked.targets[target]
            lines.append(
                f"sample={sample_index:02d} target={target} size={size_bucket} "
                f"features={','.join(checked.features)}"
            )
            for category in ("conversation", "calls", "results"):
                source_rows = getattr(checked.source, category)
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
