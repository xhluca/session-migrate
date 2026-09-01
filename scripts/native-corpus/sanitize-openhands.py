#!/usr/bin/env python3
"""Sanitize an exact OpenHands source capture into transcript-only artifacts.

OpenHands rebuilds ``base_state.json`` on resume.  That runtime snapshot holds
the complete agent/skill configuration and credential-shaped fields, so it is
intentionally excluded.  Native event IDs, timestamps, messages, actions, and
observations are preserved; only capture paths and the generated system/dynamic
prompt bodies are replaced.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

PUBLIC_CWD = "/fixture/work"
PUBLIC_RUNTIME = "/fixture/runtime"
PUBLIC_USERNAME = "fixture-user"
PUBLIC_HOSTNAME = "fixture-host"
SYSTEM_PLACEHOLDER = "SANITIZED_NATIVE_OPENHANDS_SYSTEM_PROMPT"
DYNAMIC_PLACEHOLDER = "SANITIZED_NATIVE_OPENHANDS_DYNAMIC_CONTEXT"
_EVENT_NAME = re.compile(r"event-(\d{5})-([0-9a-f-]{36})\.json\Z")


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-conversation-dir", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--source-cwd", required=True)
    parser.add_argument("--source-runtime", required=True)
    parser.add_argument("--source-username", required=True)
    parser.add_argument("--source-hostname", required=True)
    return parser.parse_args()


def replace_paths(value: Any, replacements: dict[str, str], counts: dict[str, int]) -> Any:
    if isinstance(value, str):
        result = value
        for source, target in replacements.items():
            occurrences = result.count(source)
            if occurrences:
                result = result.replace(source, target)
                counts[source] += occurrences
        return result
    if isinstance(value, list):
        return [replace_paths(item, replacements, counts) for item in value]
    if isinstance(value, dict):
        return {key: replace_paths(item, replacements, counts) for key, item in value.items()}
    return value


def sanitize_conversation(
    source: Path,
    destination_root: Path,
    *,
    source_cwd: str,
    source_runtime: str,
    source_username: str,
    source_hostname: str,
) -> tuple[str, list[Path], dict[str, int]]:
    try:
        session_id = str(uuid.UUID(source.name))
    except ValueError as exc:
        raise RuntimeError("OpenHands conversation directory is not a UUID") from exc
    if not (source / "base_state.json").is_file():
        raise RuntimeError("raw OpenHands capture is missing base_state.json")
    events = source / "events"
    source_files = sorted(events.glob("event-*.json"))
    if not source_files:
        raise RuntimeError("raw OpenHands capture has no event files")
    output = destination_root / "native" / session_id.replace("-", "") / "events"
    if output.exists():
        raise RuntimeError("OpenHands sanitizer output already exists")
    output.mkdir(parents=True, mode=0o700)
    os.chmod(output, 0o700)
    path_counts = {source_cwd: 0, source_runtime: 0}
    identity_counts = {source_username: 0, source_hostname: 0}
    prompt_counts = {"system_prompt": 0, "dynamic_context": 0}
    written: list[Path] = []
    for expected, path in enumerate(source_files):
        match = _EVENT_NAME.fullmatch(path.name)
        if match is None or int(match.group(1)) != expected:
            raise RuntimeError("OpenHands event files are not a contiguous native sequence")
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise RuntimeError(f"OpenHands event is not an object: {path.name}")
        value = replace_paths(
            value,
            {source_cwd: PUBLIC_CWD, source_runtime: PUBLIC_RUNTIME},
            path_counts,
        )
        value = replace_paths(
            value,
            {source_username: PUBLIC_USERNAME, source_hostname: PUBLIC_HOSTNAME},
            identity_counts,
        )
        if value.get("kind") == "SystemPromptEvent":
            for field, placeholder in (
                ("system_prompt", SYSTEM_PLACEHOLDER),
                ("dynamic_context", DYNAMIC_PLACEHOLDER),
            ):
                block = value.get(field)
                if not isinstance(block, dict) or not isinstance(block.get("text"), str):
                    raise RuntimeError(f"SystemPromptEvent has no {field}.text")
                block["text"] = placeholder
                prompt_counts[field] += 1
        destination = output / path.name
        destination.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
        os.chmod(destination, 0o600)
        written.append(destination)
    if prompt_counts != {"system_prompt": 1, "dynamic_context": 1}:
        raise RuntimeError("expected exactly one OpenHands system prompt event")
    if path_counts[source_cwd] < 1 or path_counts[source_runtime] < 1:
        raise RuntimeError("capture paths did not match the expected OpenHands fields")
    if identity_counts[source_username] < 1 or identity_counts[source_hostname] < 1:
        raise RuntimeError("capture identity did not match the expected OpenHands fields")
    mutations = {
        "base_state_excluded": 1,
        "capture_cwd": path_counts[source_cwd],
        "capture_runtime": path_counts[source_runtime],
        "capture_username": identity_counts[source_username],
        "capture_hostname": identity_counts[source_hostname],
        **prompt_counts,
    }
    return session_id, written, mutations


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    args = arguments()
    session_id, files, mutations = sanitize_conversation(
        args.raw_conversation_dir,
        args.output_root,
        source_cwd=args.source_cwd,
        source_runtime=args.source_runtime,
        source_username=args.source_username,
        source_hostname=args.source_hostname,
    )
    print(
        json.dumps(
            {
                "artifacts": {
                    str(path.relative_to(args.output_root)): digest(path) for path in files
                },
                "mutations": mutations,
                "session_id": session_id,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
