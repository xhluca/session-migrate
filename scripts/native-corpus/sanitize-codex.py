#!/usr/bin/env python3
"""Sanitize the reviewed Codex CLI 0.144.4 native corpus rollout.

Conversation items, image input, encrypted reasoning, tool calls/results, and
their native envelopes are retained. Generated base/developer/runtime
instructions, account rate-limit state, and private capture paths are replaced
at their schema-defined locations.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

PUBLIC_CWD = "/fixture/work"
PUBLIC_ROOT = "/fixture"
BASE_PLACEHOLDER = "SANITIZED_NATIVE_CODEX_BASE_INSTRUCTIONS"
DEVELOPER_PLACEHOLDER = "SANITIZED_NATIVE_CODEX_DEVELOPER_INSTRUCTIONS"
RUNTIME_USER_PLACEHOLDER = "SANITIZED_NATIVE_CODEX_RUNTIME_USER_CONTEXT"
ACCOUNT_PLACEHOLDER = "SANITIZED_ACCOUNT_METADATA"
PINNED_VERSION = "0.144.4"
EXPECTED_RECORDS = 34
_SECRET = re.compile(r"(?:sk-or-v1-|sk-[A-Za-z0-9_-]{12,})")


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-rollout", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--source-cwd", required=True)
    parser.add_argument("--source-root", required=True)
    return parser.parse_args()


def _replace(value: Any, replacements: dict[str, str], counts: dict[str, int]) -> Any:
    if isinstance(value, str):
        result = value
        for source, target in replacements.items():
            occurrences = result.count(source)
            if occurrences:
                result = result.replace(source, target)
                counts["capture_paths"] += occurrences
        return result
    if isinstance(value, list):
        return [_replace(item, replacements, counts) for item in value]
    if isinstance(value, dict):
        return {key: _replace(item, replacements, counts) for key, item in value.items()}
    return value


def _redact_rate_limits(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _redact_rate_limits(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_rate_limits(item) for item in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return 0
    return ACCOUNT_PLACEHOLDER


def _replace_text_blocks(message: dict[str, Any], placeholder: str) -> int:
    content = message.get("content")
    if not isinstance(content, list):
        raise RuntimeError("Codex instruction message has no content array")
    replaced = 0
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "input_text":
            raise RuntimeError("Codex instruction message contains an unexpected block")
        if not isinstance(block.get("text"), str):
            raise RuntimeError("Codex instruction block has no text")
        block["text"] = placeholder
        replaced += 1
    return replaced


def sanitize_rollout(
    source: Path,
    destination: Path,
    *,
    source_cwd: str,
    source_root: str,
) -> tuple[str, dict[str, int]]:
    if not source_cwd.startswith(source_root.rstrip("/") + "/"):
        raise RuntimeError("Codex source CWD must be inside the declared capture root")
    raw_records = source.read_text().splitlines()
    if len(raw_records) != EXPECTED_RECORDS:
        raise RuntimeError(f"expected the reviewed {EXPECTED_RECORDS}-record Codex rollout")

    counts = {
        "capture_paths": 0,
        "base_instructions": 0,
        "developer_instruction_blocks": 0,
        "runtime_user_blocks": 0,
        "account_rate_limits": 0,
    }
    replacements = {source_cwd: PUBLIC_CWD, source_root: PUBLIC_ROOT}
    records: list[dict[str, Any]] = []
    session_id: str | None = None
    seen_world_state = False
    tool_calls: list[str] = []
    tool_results = 0
    image_blocks = 0

    for line_number, line in enumerate(raw_records, start=1):
        if not line.strip():
            raise RuntimeError(f"blank native record at line {line_number}")
        value = json.loads(line)
        if not isinstance(value, dict) or not isinstance(value.get("type"), str):
            raise RuntimeError(f"record {line_number} is not a Codex envelope")
        payload = value.get("payload")
        if not isinstance(payload, dict):
            raise RuntimeError(f"record {line_number} has no Codex payload")
        if value["type"] == "session_meta":
            if line_number != 1 or payload.get("cli_version") != PINNED_VERSION:
                raise RuntimeError("unexpected Codex session metadata")
            candidate = payload.get("id") or payload.get("session_id")
            if not isinstance(candidate, str) or not candidate:
                raise RuntimeError("Codex session metadata has no native ID")
            session_id = candidate
            base = payload.get("base_instructions")
            if not isinstance(base, dict) or not isinstance(base.get("text"), str):
                raise RuntimeError("Codex session metadata has no base instructions")
            base["text"] = BASE_PLACEHOLDER
            counts["base_instructions"] += 1

        value = _replace(value, replacements, counts)
        payload = value["payload"]
        if value["type"] == "world_state":
            seen_world_state = True
            state = payload.get("state")
            if not isinstance(state, dict) or not isinstance(state.get("apps_instructions"), bool):
                raise RuntimeError("Codex world state has no reviewed apps-instructions flag")

        if value["type"] == "response_item" and payload.get("type") == "message":
            role = payload.get("role")
            if role == "developer":
                counts["developer_instruction_blocks"] += _replace_text_blocks(
                    payload, DEVELOPER_PLACEHOLDER
                )
            elif role == "user" and not seen_world_state:
                counts["runtime_user_blocks"] += _replace_text_blocks(
                    payload, RUNTIME_USER_PLACEHOLDER
                )
            content = payload.get("content")
            if isinstance(content, list):
                image_blocks += sum(
                    isinstance(block, dict) and block.get("type") == "input_image"
                    for block in content
                )

        if value["type"] == "response_item" and payload.get("type") == "custom_tool_call":
            name = payload.get("name")
            if isinstance(name, str):
                tool_calls.append(name)
        if value["type"] == "response_item" and payload.get("type") == "custom_tool_call_output":
            tool_results += 1
        if isinstance(payload.get("rate_limits"), dict):
            payload["rate_limits"] = _redact_rate_limits(payload["rate_limits"])
            counts["account_rate_limits"] += 1
        records.append(value)

    if session_id is None:
        raise RuntimeError("Codex rollout has no session metadata")
    if tool_calls != ["exec", "exec"] or tool_results != 2:
        raise RuntimeError("expected the reviewed two Codex exec calls/results")
    if image_blocks != 1:
        raise RuntimeError("expected one native Codex image block")
    if counts != {
        "capture_paths": 20,
        "base_instructions": 1,
        "developer_instruction_blocks": 5,
        "runtime_user_blocks": 2,
        "account_rate_limits": 4,
    }:
        raise RuntimeError(f"reviewed Codex mutation shape changed: {counts}")

    rendered = "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n" for record in records
    )
    if source_cwd in rendered or source_root in rendered or "/home/" in rendered:
        raise RuntimeError("sanitized Codex rollout still contains a private path")
    if _SECRET.search(rendered):
        raise RuntimeError("sanitized Codex rollout still contains a secret-like token")
    destination.parent.mkdir(parents=True, mode=0o700)
    destination.write_text(rendered)
    os.chmod(destination, 0o600)
    return session_id, counts


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    args = arguments()
    pending = args.output_root / "native/pending.jsonl"
    session_id, counts = sanitize_rollout(
        args.raw_rollout,
        pending,
        source_cwd=args.source_cwd,
        source_root=args.source_root,
    )
    destination = pending.with_name(f"{session_id}.jsonl")
    pending.rename(destination)
    print(
        json.dumps(
            {
                "artifacts": {destination.name: digest(destination)},
                "mutations": counts,
                "session_id": session_id,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
