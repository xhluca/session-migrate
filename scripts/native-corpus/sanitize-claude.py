#!/usr/bin/env python3
"""Sanitize the reviewed Claude Code 2.1.209 native corpus capture.

The UUID graph, timestamps, user/assistant content, image/document blocks, and
tool activity are preserved.  Only the private capture root and generated
runtime tool-listing prose are replaced.  The latter is client-generated
system context, not conversation history suitable for a public fixture.
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
RUNTIME_PLACEHOLDER = "SANITIZED_NATIVE_CLAUDE_RUNTIME_ATTACHMENT"
PINNED_VERSION = "2.1.209"
EXPECTED_RECORDS = 28
_SECRET = re.compile(r"(?:sk-or-v1-|sk-[A-Za-z0-9_-]{12,})")


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-transcript", type=Path, required=True)
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


def sanitize_transcript(
    source: Path,
    destination: Path,
    *,
    source_cwd: str,
    source_root: str,
) -> tuple[str, dict[str, int]]:
    if not source_cwd.startswith(source_root.rstrip("/") + "/"):
        raise RuntimeError("Claude source CWD must be inside the declared capture root")
    records: list[dict[str, Any]] = []
    session_id: str | None = None
    uuids: set[str] = set()
    tool_names: list[str] = []
    tool_results = 0
    image_blocks = 0
    document_blocks = 0
    counts = {"capture_paths": 0, "runtime_attachment_lines": 0}
    replacements = {source_cwd: PUBLIC_CWD, source_root: PUBLIC_ROOT}

    for line_number, line in enumerate(source.read_text().splitlines(), start=1):
        if not line.strip():
            raise RuntimeError(f"blank native record at line {line_number}")
        value = json.loads(line)
        if not isinstance(value, dict) or not isinstance(value.get("type"), str):
            raise RuntimeError(f"record {line_number} is not a Claude envelope")
        current_session = value.get("sessionId")
        if not isinstance(current_session, str) or not current_session:
            raise RuntimeError(f"record {line_number} has no native session ID")
        session_id = session_id or current_session
        if current_session != session_id:
            raise RuntimeError("capture mixes Claude session IDs")
        if "version" in value and value["version"] != PINNED_VERSION:
            raise RuntimeError(f"record {line_number} was not written by Claude {PINNED_VERSION}")
        if "cwd" in value and value["cwd"] != source_cwd:
            raise RuntimeError(f"record {line_number} has an unexpected native CWD")
        uuid = value.get("uuid")
        if uuid is not None:
            if not isinstance(uuid, str) or uuid in uuids:
                raise RuntimeError(f"record {line_number} has an invalid or duplicate UUID")
            uuids.add(uuid)

        value = _replace(value, replacements, counts)
        attachment = value.get("attachment")
        if isinstance(attachment, dict) and "addedLines" in attachment:
            lines = attachment["addedLines"]
            if not isinstance(lines, list) or not all(isinstance(item, str) for item in lines):
                raise RuntimeError("Claude runtime attachment addedLines has an unknown schema")
            attachment["addedLines"] = [RUNTIME_PLACEHOLDER for _ in lines]
            counts["runtime_attachment_lines"] += len(lines)

        message = value.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        blocks = content if isinstance(content, list) else []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "tool_use":
                name = block.get("name")
                if isinstance(name, str):
                    tool_names.append(name)
            elif block_type == "tool_result":
                tool_results += 1
            elif block_type == "image":
                image_blocks += 1
            elif block_type == "document":
                document_blocks += 1
        records.append(value)

    if session_id is None or len(records) != EXPECTED_RECORDS:
        raise RuntimeError(f"expected the reviewed {EXPECTED_RECORDS}-record Claude capture")
    if tool_names != ["Read", "Read", "Read"] or tool_results != 3:
        raise RuntimeError("expected the reviewed three Claude Read calls/results")
    if image_blocks != 1 or document_blocks != 1:
        raise RuntimeError("expected one native Claude image and one document block")
    if counts["capture_paths"] < 20 or counts["runtime_attachment_lines"] != 21:
        raise RuntimeError("reviewed Claude private/runtime fields were not all found")

    rendered = "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n" for record in records
    )
    if source_cwd in rendered or source_root in rendered or "/home/" in rendered:
        raise RuntimeError("sanitized Claude capture still contains a private path")
    if _SECRET.search(rendered):
        raise RuntimeError("sanitized Claude capture still contains a secret-like token")
    destination.parent.mkdir(parents=True, mode=0o700)
    destination.write_text(rendered)
    os.chmod(destination, 0o600)
    return session_id, counts


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    args = arguments()
    pending = args.output_root / "native/pending.jsonl"
    session_id, counts = sanitize_transcript(
        args.raw_transcript,
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
