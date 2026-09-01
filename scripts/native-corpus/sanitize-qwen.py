#!/usr/bin/env python3
"""Sanitize a Qwen Code 0.22.1 native capture for the public corpus.

The sanitizer preserves the native ChatRecord graph, UUIDs, timestamps,
messages, native image rejection, tool calls, and tool results. It only
rewrites the private capture workspace to the corpus workspace. It refuses
captures that do not match the reviewed Qwen schema and trajectory shape.
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
PINNED_VERSION = "0.22.1"
IMAGE_REJECTION = "This model does not support image input"
_SECRET = re.compile(r"(?:sk-or-v1-|sk-[A-Za-z0-9_-]{12,})")


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-chat", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--source-cwd", required=True)
    return parser.parse_args()


def _replace(value: Any, source: str, counts: dict[str, int]) -> Any:
    if isinstance(value, str):
        occurrences = value.count(source)
        if occurrences:
            counts["capture_cwd"] += occurrences
            return value.replace(source, PUBLIC_CWD)
        return value
    if isinstance(value, list):
        return [_replace(item, source, counts) for item in value]
    if isinstance(value, dict):
        return {key: _replace(item, source, counts) for key, item in value.items()}
    return value


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    return []


def sanitize_chat(
    source: Path,
    destination: Path,
    *,
    source_cwd: str,
) -> tuple[str, dict[str, int]]:
    records: list[dict[str, Any]] = []
    counts = {"capture_cwd": 0}
    session_id: str | None = None
    prior_uuid: str | None = None
    image_rejections = 0
    tool_names: list[str] = []

    for line_number, line in enumerate(source.read_text().splitlines(), start=1):
        if not line.strip():
            raise RuntimeError(f"blank native record at line {line_number}")
        value = json.loads(line)
        if not isinstance(value, dict):
            raise RuntimeError(f"record {line_number} is not an object")
        required = {"uuid", "parentUuid", "sessionId", "timestamp", "type", "cwd", "version"}
        if not required.issubset(value):
            raise RuntimeError(f"record {line_number} is missing Qwen envelope fields")
        if value["version"] != PINNED_VERSION:
            raise RuntimeError(f"record {line_number} was not written by Qwen {PINNED_VERSION}")
        if not isinstance(value["uuid"], str) or value["parentUuid"] != prior_uuid:
            raise RuntimeError(f"record {line_number} breaks the linear Qwen parent graph")
        current_session = value["sessionId"]
        if not isinstance(current_session, str) or not current_session:
            raise RuntimeError(f"record {line_number} has no session ID")
        session_id = session_id or current_session
        if current_session != session_id:
            raise RuntimeError("capture mixes Qwen session IDs")
        if value["cwd"] != source_cwd:
            raise RuntimeError(f"record {line_number} has an unexpected native CWD")

        value = _replace(value, source_cwd, counts)
        for text in _strings(value.get("message")):
            image_rejections += text.count(IMAGE_REJECTION)
        message = value.get("message")
        if value["type"] == "assistant" and isinstance(message, dict):
            for part in message.get("parts", []):
                if isinstance(part, dict) and isinstance(part.get("functionCall"), dict):
                    name = part["functionCall"].get("name")
                    if isinstance(name, str):
                        tool_names.append(name)
        records.append(value)
        prior_uuid = value["uuid"]

    if session_id is None or len(records) != 11:
        raise RuntimeError("expected the reviewed 11-record Qwen native capture")
    if image_rejections != 1:
        raise RuntimeError("expected exactly one native Qwen image rejection")
    if tool_names != ["read_file", "read_file"]:
        raise RuntimeError("expected the two reviewed native read_file calls")
    if counts["capture_cwd"] < len(records) + 2:
        raise RuntimeError("capture CWD did not occur in all records and tool arguments")

    rendered = "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n" for record in records
    )
    if source_cwd in rendered or "/home/" in rendered or _SECRET.search(rendered):
        raise RuntimeError("sanitized Qwen capture still contains a private path or secret")
    destination.parent.mkdir(parents=True, mode=0o700)
    destination.write_text(rendered)
    os.chmod(destination, 0o600)
    return session_id, counts


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    args = arguments()
    if not args.raw_chat.is_file():
        raise RuntimeError("raw Qwen capture is missing its chat JSONL")
    pending = args.output_root / "native/pending.jsonl"
    session_id, counts = sanitize_chat(
        args.raw_chat,
        pending,
        source_cwd=args.source_cwd,
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
