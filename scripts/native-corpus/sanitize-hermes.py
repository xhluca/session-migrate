#!/usr/bin/env python3
"""Sanitize an exact Hermes Agent official export and rebuild its native DB."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SESSION_FIELDS = frozenset(
    {
        "actual_cost_usd",
        "api_call_count",
        "archived",
        "billing_base_url",
        "billing_mode",
        "billing_provider",
        "cache_read_tokens",
        "cache_write_tokens",
        "chat_id",
        "chat_type",
        "compression_failure_cooldown_until",
        "compression_failure_error",
        "compression_fallback_streak",
        "compression_ineffective_count",
        "cost_source",
        "cost_status",
        "cwd",
        "display_name",
        "end_reason",
        "ended_at",
        "estimated_cost_usd",
        "expiry_finalized",
        "git_branch",
        "git_metadata_generation",
        "git_repo_root",
        "handoff_error",
        "handoff_platform",
        "handoff_state",
        "hidden",
        "id",
        "input_tokens",
        "last_activity_at",
        "last_activity_description",
        "last_activity_provenance",
        "last_read_at",
        "message_count",
        "messages",
        "model",
        "model_config",
        "origin_json",
        "output_tokens",
        "parent_session_id",
        "pinned",
        "pricing_version",
        "profile_name",
        "reasoning_tokens",
        "rewind_count",
        "session_key",
        "source",
        "started_at",
        "system_prompt",
        "system_prompt_hash",
        "thread_id",
        "title",
        "title_source",
        "tool_call_count",
        "user_id",
    }
)
MESSAGE_FIELDS = frozenset(
    {
        "active",
        "api_content",
        "codex_message_items",
        "codex_reasoning_items",
        "compacted",
        "content",
        "display_kind",
        "display_metadata",
        "effect_disposition",
        "finish_reason",
        "id",
        "observed",
        "platform_message_id",
        "reasoning",
        "reasoning_content",
        "reasoning_details",
        "role",
        "session_id",
        "timestamp",
        "token_count",
        "tool_call_id",
        "tool_calls",
        "tool_name",
    }
)
PRIVATE_SESSION_FIELDS = (
    "billing_base_url",
    "billing_mode",
    "billing_provider",
    "chat_id",
    "chat_type",
    "display_name",
    "git_branch",
    "git_repo_root",
    "handoff_error",
    "handoff_platform",
    "handoff_state",
    "model_config",
    "origin_json",
    "parent_session_id",
    "profile_name",
    "session_key",
    "system_prompt",
    "system_prompt_hash",
    "thread_id",
    "user_id",
)
PUBLIC_CWD = "/fixture/work"


class SanitizationError(ValueError):
    """Raised when private capture data does not match the reviewed schema."""


@dataclass(frozen=True, slots=True)
class Result:
    document: dict[str, Any]
    mutations: dict[str, int]


def _scrub(value: Any, private_cwd: str, mutations: Counter[str]) -> Any:
    if isinstance(value, str):
        replaced = value.replace(private_cwd, PUBLIC_CWD)
        if replaced != value:
            mutations["message path"] += value.count(private_cwd)
        return replaced
    if isinstance(value, list):
        return [_scrub(item, private_cwd, mutations) for item in value]
    if isinstance(value, dict):
        return {key: _scrub(item, private_cwd, mutations) for key, item in value.items()}
    return value


def _validate_tool_calls(value: Any, index: int) -> None:
    if value is None:
        return
    if not isinstance(value, list) or not value:
        raise SanitizationError(f"messages[{index}].tool_calls must be a non-empty list")
    for call in value:
        if not isinstance(call, dict) or set(call) != {
            "id",
            "call_id",
            "response_item_id",
            "type",
            "function",
        }:
            raise SanitizationError(f"messages[{index}].tool_calls fields changed")
        function = call.get("function")
        if (
            call.get("type") != "function"
            or call.get("id") != call.get("call_id")
            or not isinstance(function, dict)
            or set(function) != {"name", "arguments"}
            or not isinstance(function.get("name"), str)
            or not isinstance(function.get("arguments"), str)
        ):
            raise SanitizationError(f"messages[{index}] has invalid native tool linkage")
        try:
            arguments = json.loads(function["arguments"])
        except json.JSONDecodeError as exc:
            raise SanitizationError(
                f"messages[{index}] tool arguments are not strict JSON"
            ) from exc
        if not isinstance(arguments, dict):
            raise SanitizationError(f"messages[{index}] tool arguments are not an object")


def sanitize_document(document: object, *, source_cwd: Path) -> Result:
    """Validate and sanitize one official ``SessionDB.export_session`` object."""

    if not isinstance(document, dict) or set(document) != SESSION_FIELDS:
        raise SanitizationError("official Hermes session fields changed")
    messages = document.get("messages")
    session_id = document.get("id")
    if not isinstance(session_id, str) or not session_id:
        raise SanitizationError("official Hermes session id is missing")
    if not isinstance(messages, list) or not messages:
        raise SanitizationError("official Hermes messages are missing")
    seen_results: set[str] = set()
    seen_calls: set[str] = set()
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or set(message) != MESSAGE_FIELDS:
            raise SanitizationError(f"messages[{index}] fields changed")
        if message.get("session_id") != session_id:
            raise SanitizationError(f"messages[{index}] session linkage changed")
        if message.get("role") not in {"user", "assistant", "tool"}:
            raise SanitizationError(f"messages[{index}] role is unsupported")
        _validate_tool_calls(message.get("tool_calls"), index)
        for call in message.get("tool_calls") or []:
            call_id = call["id"]
            if call_id in seen_calls:
                raise SanitizationError("native tool call id is duplicated")
            seen_calls.add(call_id)
        if message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            if call_id not in seen_calls or call_id in seen_results:
                raise SanitizationError("native tool result linkage changed")
            seen_results.add(call_id)

    private_cwd = str(source_cwd.resolve())
    mutations: Counter[str] = Counter()
    sanitized = _scrub(document, private_cwd, mutations)
    assert isinstance(sanitized, dict)
    for field in PRIVATE_SESSION_FIELDS:
        if sanitized.get(field) is not None:
            sanitized[field] = None
            mutations[f"session {field}"] += 1
    if sanitized.get("cwd") != PUBLIC_CWD:
        sanitized["cwd"] = PUBLIC_CWD
        mutations["session cwd"] += 1
    sanitized["source"] = "session-migrate-native-corpus"
    sanitized["last_activity_description"] = None
    sanitized["last_activity_provenance"] = None
    sanitized["last_activity_at"] = None
    sanitized["last_read_at"] = None
    sanitized["archived"] = 0
    sanitized["hidden"] = 0
    sanitized["pinned"] = 0
    return Result(sanitized, dict(sorted(mutations.items())))


def import_exact_client(
    document: dict[str, Any], *, source: Path, binary: Path, output: Path
) -> None:
    """Use the pinned client's official importer to materialize a schema-26 DB."""

    interpreter = binary.resolve(strict=True).parent / "python"
    program = (
        "import json,sys; from pathlib import Path; from hermes_state import SessionDB; "
        "value=json.load(open(sys.argv[2], encoding='utf-8')); "
        "db=SessionDB(db_path=Path(sys.argv[1])); result=db.import_sessions([value]); "
        "db.close(); print(json.dumps(result, sort_keys=True))"
    )
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="session-migrate-hermes-sanitize-") as directory:
        payload = Path(directory) / "session.json"
        payload.write_text(json.dumps(document, ensure_ascii=False) + "\n")
        completed = subprocess.run(
            [str(interpreter), "-c", program, str(output), str(payload)],
            cwd=source.resolve(strict=True),
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    if completed.returncode:
        raise SanitizationError(f"exact Hermes import failed: {completed.stderr.strip()}")
    try:
        result = json.loads(completed.stdout.splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise SanitizationError("exact Hermes import emitted no result") from exc
    if result.get("ok") is not True or result.get("imported") != 1:
        raise SanitizationError(f"exact Hermes import rejected the fixture: {result!r}")
    os.chmod(output, 0o600)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--official-export", required=True, type=Path)
    parser.add_argument("--source-cwd", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--mutations", type=Path)
    arguments = parser.parse_args()
    source_document = json.loads(arguments.official_export.read_text())
    result = sanitize_document(source_document, source_cwd=arguments.source_cwd)
    import_exact_client(
        result.document,
        source=arguments.source,
        binary=arguments.binary,
        output=arguments.output,
    )
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
    except (OSError, SanitizationError, json.JSONDecodeError) as exc:
        raise SystemExit(str(exc)) from exc
