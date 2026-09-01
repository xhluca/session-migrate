#!/usr/bin/env python3
"""Sanitize a Kimi Code 0.38.0 session bundle for the public corpus.

The sanitizer preserves native state, wire envelopes, ordering, timestamps,
messages, thinking, and tool activity. Private paths, the generated system
prompt, and the generated injection message are replaced at their exact schema
locations. It refuses captures outside the reviewed 31-record trajectory.
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
PUBLIC_AGENTS_MD = "/fixture/AGENTS.md"
PUBLIC_WORKSPACE_ID = "wd_work_84f4a13a9723"
SYSTEM_PLACEHOLDER = "SANITIZED_NATIVE_KIMI_SYSTEM_PROMPT"
INJECTION_PLACEHOLDER = "SANITIZED_NATIVE_KIMI_INJECTION_MESSAGE"
PINNED_PROTOCOL = "1.5"
_SECRET = re.compile(r"(?:sk-or-v1-|sk-[A-Za-z0-9_-]{12,})")


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-session-dir", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--source-cwd", required=True)
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


def _render_jsonl(records: list[dict[str, Any]]) -> str:
    return "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n" for record in records
    )


def _assert_public(text: str, private_roots: tuple[str, ...]) -> None:
    if any(root in text for root in private_roots) or "/home/" in text or _SECRET.search(text):
        raise RuntimeError("sanitized Kimi capture still contains a private path or secret")


def sanitize_bundle(
    source_dir: Path,
    destination_dir: Path,
    *,
    source_cwd: str,
) -> tuple[str, dict[str, int]]:
    state_path = source_dir / "state.json"
    wire_path = source_dir / "agents/main/wire.jsonl"
    if not state_path.is_file() or not wire_path.is_file():
        raise RuntimeError("raw Kimi capture is missing state.json or agents/main/wire.jsonl")
    state = json.loads(state_path.read_text())
    if not isinstance(state, dict) or state.get("version") != 2 or state.get("cwd") != source_cwd:
        raise RuntimeError("unexpected Kimi state schema or capture CWD")
    session_id = state.get("id")
    if not isinstance(session_id, str) or not session_id.startswith("session_"):
        raise RuntimeError("Kimi state has no native session ID")

    records = [json.loads(line) for line in wire_path.read_text().splitlines() if line.strip()]
    if len(records) != 31:
        raise RuntimeError("expected the reviewed 31-record Kimi native capture")
    if records[0] != {
        "type": "metadata",
        "protocol_version": PINNED_PROTOCOL,
        "created_at": records[0].get("created_at"),
    }:
        raise RuntimeError("unexpected Kimi wire metadata header")
    if not all(isinstance(record, dict) for record in records):
        raise RuntimeError("Kimi wire record is not an object")

    counts = {
        "capture_paths": 0,
        "main_agent_homedir": 0,
        "workspace_id": 0,
        "system_prompt": 0,
        "system_prompt_hash": 0,
        "injection_message": 0,
    }
    raw_homedir = state.get("agents", {}).get("main", {}).get("homedir")
    if not isinstance(raw_homedir, str) or not raw_homedir.startswith("/"):
        raise RuntimeError("Kimi main-agent homedir is not the reviewed absolute path")
    replacements = {source_cwd: PUBLIC_CWD, "/tmp/AGENTS.md": PUBLIC_AGENTS_MD}
    state = _replace(state, replacements, counts)
    state["cwd"] = PUBLIC_CWD
    state["agents"]["main"]["homedir"] = "agents/main"
    counts["main_agent_homedir"] = 1

    sanitized: list[dict[str, Any]] = []
    tool_calls: list[str] = []
    for index, raw_record in enumerate(records):
        record = _replace(raw_record, replacements, counts)
        if record.get("type") == "runtime.set_binding":
            workspace_id = record.get("workspaceId")
            if not isinstance(workspace_id, str) or not workspace_id.startswith("wd_work_"):
                raise RuntimeError("runtime.set_binding has no reviewed workspace ID")
            record["workspaceId"] = PUBLIC_WORKSPACE_ID
            counts["workspace_id"] += 1
        if record.get("type") == "profile.bind":
            if not isinstance(record.get("systemPrompt"), str):
                raise RuntimeError("profile.bind has no generated system prompt")
            record["systemPrompt"] = SYSTEM_PLACEHOLDER
            counts["system_prompt"] += 1
        if record.get("type") == "llm.request":
            current_hash = record.get("systemPromptHash")
            if not isinstance(current_hash, str) or len(current_hash) != 64:
                raise RuntimeError("llm.request has no generated system-prompt hash")
            record["systemPromptHash"] = hashlib.sha256(SYSTEM_PLACEHOLDER.encode()).hexdigest()
            counts["system_prompt_hash"] += 1
        if record.get("type") == "context.append_message":
            message = record.get("message")
            origin = message.get("origin") if isinstance(message, dict) else None
            origin_kind = origin.get("kind") if isinstance(origin, dict) else origin
            if (
                isinstance(message, dict)
                and message.get("role") == "user"
                and origin_kind
                not in {
                    None,
                    "user",
                }
            ):
                content = message.get("content")
                if (
                    not isinstance(content, list)
                    or len(content) != 1
                    or content[0].get("type") != "text"
                ):
                    raise RuntimeError("unexpected generated Kimi injection message shape")
                content[0]["text"] = INJECTION_PLACEHOLDER
                counts["injection_message"] += 1
        event = record.get("event")
        if (
            record.get("type") == "context.append_loop_event"
            and isinstance(event, dict)
            and event.get("type") == "tool.call"
            and isinstance(event.get("name"), str)
        ):
            tool_calls.append(event["name"])
        if index and not isinstance(record.get("time"), int):
            raise RuntimeError(f"Kimi wire record {index} has no native timestamp")
        sanitized.append(record)

    if (
        counts["system_prompt"] != 1
        or counts["system_prompt_hash"] != 2
        or counts["injection_message"] != 1
        or counts["workspace_id"] != 1
    ):
        raise RuntimeError(
            "expected one workspace binding, one system prompt, two prompt hashes, "
            "and one injection message"
        )
    if counts["capture_paths"] < 5:
        raise RuntimeError("expected Kimi capture paths were not found")
    if tool_calls != ["Read", "Read"]:
        raise RuntimeError("expected the two reviewed native Read calls")

    state_text = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    wire_text = _render_jsonl(sanitized)
    _assert_public(state_text + wire_text, (source_cwd, raw_homedir))
    destination_dir.mkdir(parents=True, mode=0o700)
    wire_destination = destination_dir / "agents/main/wire.jsonl"
    wire_destination.parent.mkdir(parents=True, mode=0o700)
    state_destination = destination_dir / "state.json"
    state_destination.write_text(state_text)
    wire_destination.write_text(wire_text)
    os.chmod(state_destination, 0o600)
    os.chmod(wire_destination, 0o600)
    return session_id, counts


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    args = arguments()
    pending = args.output_root / "native/pending"
    session_id, counts = sanitize_bundle(
        args.raw_session_dir,
        pending,
        source_cwd=args.source_cwd,
    )
    destination = pending.with_name(session_id)
    pending.rename(destination)
    print(
        json.dumps(
            {
                "artifacts": {
                    "state.json": digest(destination / "state.json"),
                    "wire.jsonl": digest(destination / "agents/main/wire.jsonl"),
                },
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
