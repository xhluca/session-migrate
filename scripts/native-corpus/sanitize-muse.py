#!/usr/bin/env python3
"""Sanitize one exact Muse Code 0.2.1 durable session log."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from session_migrate.formats import muse

TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "id",
        "stream",
        "sequence",
        "recorded_at",
        "record_type",
        "durability",
        "causation_id",
        "payload_type",
        "payload_schema_version",
        "payload",
    }
)
PAYLOAD_TYPES = frozenset(
    {
        "runtime.session.metadata",
        "runtime.session.route_facts",
        "session.opened.observed",
        "runtime.session",
        "runtime.command_intake.received",
        "runtime.command_intake.settled",
        "runtime.user_intent.accepted",
        "runtime.user_intent.materialized",
        "run.model.configured",
        "tool_batch.effect.started",
        "tool_batch.effect.terminal",
        "session.end",
        "session.resumed",
    }
)
RUN_CONTEXT_FIELDS = frozenset({"id", "lifecycle", "order", "role", "source", "text"})
SYSTEM_SENTINEL = "[SANITIZED_MUSE_SYSTEM_INSTRUCTIONS]"
CONTEXT_SENTINEL = "[SANITIZED_MUSE_DEVELOPER_CONTEXT]"


class SanitizationError(ValueError):
    """Raised when raw state does not match the reviewed Muse schema."""


@dataclass(frozen=True, slots=True)
class Result:
    records: tuple[dict[str, Any], ...]
    mutations: dict[str, int]


def _load(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for index, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SanitizationError(f"line {index} is not valid JSON") from exc
        if not isinstance(value, dict):
            raise SanitizationError(f"line {index} is not an object")
        records.append(value)
    return records


def _validate_context_messages(value: Any, sequence: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise SanitizationError(f"record {sequence} run_context_messages is not a list")
    messages: list[dict[str, Any]] = []
    for message in value:
        if (
            not isinstance(message, dict)
            or set(message) != RUN_CONTEXT_FIELDS
            or message.get("role") != "developer"
            or not isinstance(message.get("text"), str)
        ):
            raise SanitizationError(f"record {sequence} developer context schema changed")
        messages.append(dict(message))
    return messages


def _scrub_paths(value: Any, source_root: str, mutations: Counter[str]) -> Any:
    if isinstance(value, str):
        updated = value.replace(source_root, "/fixture")
        if updated != value:
            mutations["private path"] += value.count(source_root)
        return updated
    if isinstance(value, list):
        return [_scrub_paths(item, source_root, mutations) for item in value]
    if isinstance(value, dict):
        return {key: _scrub_paths(item, source_root, mutations) for key, item in value.items()}
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise SanitizationError("native JSON contains an unsupported value")


def sanitize(path: Path, *, source_root: Path, session_id: str) -> Result:
    """Validate and sanitize one selected Muse session without inventing events."""

    path = path.resolve(strict=True)
    records = _load(path)
    try:
        muse._validate_records(records, expected_session_id=session_id)
    except Exception as exc:
        raise SanitizationError(f"Muse native validation failed: {exc}") from exc
    source = os.path.abspath(source_root)
    mutations: Counter[str] = Counter()
    sanitized: list[dict[str, Any]] = []
    for record in records:
        sequence = int(record.get("sequence", -1))
        if set(record) != TOP_LEVEL_FIELDS:
            raise SanitizationError(f"record {sequence} envelope fields changed")
        if record.get("payload_type") not in PAYLOAD_TYPES:
            raise SanitizationError(
                f"record {sequence} payload type is unsupported: {record.get('payload_type')}"
            )
        clean = _scrub_paths(record, source, mutations)
        payload = clean["payload"]
        event = payload.get("event") if isinstance(payload, dict) else None
        if isinstance(event, dict) and event.get("kind") == "model_request_configured":
            base = event.get("base_instructions")
            if not isinstance(base, str) or not base:
                raise SanitizationError(f"record {sequence} base instructions schema changed")
            event["base_instructions"] = SYSTEM_SENTINEL
            mutations["base instructions"] += 1
            contexts = _validate_context_messages(event.get("run_context_messages"), sequence)
            for context in contexts:
                context["text"] = CONTEXT_SENTINEL
                mutations["developer context"] += 1
            event["run_context_messages"] = contexts
        sanitized.append(clean)
    encoded = b"".join(
        (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        for record in sanitized
    )
    try:
        muse.validate_native_bytes(encoded, session_id)
    except Exception as exc:
        raise SanitizationError(f"sanitized Muse validation failed: {exc}") from exc
    text = encoded.decode()
    forbidden = (source, "You are Muse Code", "<system-reminder", "sk-or-v1-")
    if any(marker in text for marker in forbidden):
        raise SanitizationError("sanitized Muse log retains private paths, prompts, or credentials")
    return Result(tuple(sanitized), dict(sorted(mutations.items())))


def write_result(result: Result, output: Path) -> None:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise SanitizationError("output already exists")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="session-migrate-muse-sanitize-", suffix=".jsonl", dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with temporary.open("wb") as handle:
            for record in result.records:
                handle.write(
                    (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
                )
        temporary.replace(output)
        output.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mutations", type=Path)
    arguments = parser.parse_args()
    result = sanitize(
        arguments.input,
        source_root=arguments.source_root,
        session_id=arguments.session_id,
    )
    write_result(result, arguments.output)
    if arguments.mutations:
        arguments.mutations.write_text(json.dumps(result.mutations, indent=2) + "\n")
    print(
        json.dumps(
            {
                "output": str(arguments.output),
                "sha256": hashlib.sha256(arguments.output.read_bytes()).hexdigest(),
                "mutations": result.mutations,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except (OSError, SanitizationError) as exc:
        raise SystemExit(str(exc)) from exc
