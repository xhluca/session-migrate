#!/usr/bin/env python3
"""Mechanically sanitize exact Pi/OMP JSONL captures for the public corpus.

The sanitizer changes only the native session UUID and occurrences of the
private capture working directory.  It preserves entry IDs, parent linkage,
timestamps, message/tool/media payloads, and every other native field.  OMP's
fixed-width title slot is rebuilt to exactly 256 bytes after substitution.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

PUBLIC_CWD = "/fixture/work"
OMP_TITLE_SLOT_BYTES = 256
_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z",
    re.IGNORECASE,
)


def sanitize_capture(
    source: Path,
    destination: Path,
    *,
    source_cwd: str,
    source_session_id: str,
    public_session_id: str,
    format_name: str,
) -> dict[str, int]:
    """Rewrite the two approved private values and return exact mutation counts."""

    if format_name not in {"pi", "omp"}:
        raise RuntimeError(f"unsupported sanitizer format: {format_name}")
    if not source_cwd or source_cwd == PUBLIC_CWD:
        raise RuntimeError("source cwd must be a non-public absolute capture path")
    if not Path(source_cwd).is_absolute():
        raise RuntimeError("source cwd must be absolute")
    if not _UUID.fullmatch(source_session_id) or not _UUID.fullmatch(public_session_id):
        raise RuntimeError("source and public session IDs must be UUIDs")
    if source_session_id == public_session_id:
        raise RuntimeError("source and public session IDs must differ")

    try:
        values = [json.loads(line) for line in source.read_text().splitlines() if line.strip()]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("capture is not valid UTF-8 JSONL") from exc
    if not values or any(not isinstance(value, dict) for value in values):
        raise RuntimeError("capture must contain JSON object records")

    title: dict[str, Any] | None = None
    if values[0].get("type") == "title":
        title = values.pop(0)
    if format_name == "omp" and title is None:
        raise RuntimeError("OMP capture is missing its fixed-width title slot")
    if format_name == "pi" and title is not None:
        raise RuntimeError("Pi capture unexpectedly contains an OMP title slot")
    if not values or values[0].get("type") != "session":
        raise RuntimeError("capture does not start with a native session header")
    if values[0].get("id") != source_session_id:
        raise RuntimeError("capture header does not match the source session ID")

    counts = {"cwd": 0, "uuid": 0}
    sanitized = [
        _replace_values(
            value,
            source_cwd=source_cwd,
            source_session_id=source_session_id,
            public_session_id=public_session_id,
            counts=counts,
        )
        for value in values
    ]
    if counts["cwd"] < 1 or counts["uuid"] != 1:
        raise RuntimeError(
            f"capture values did not match the expected cwd/session ID exactly: {counts!r}"
        )

    output = bytearray()
    if title is not None:
        output.extend(_omp_title_slot(title))
    for value in sanitized:
        output.extend(
            (json.dumps(value, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(bytes(output))
    os.chmod(destination, 0o600)
    return counts


def _replace_values(
    value: Any,
    *,
    source_cwd: str,
    source_session_id: str,
    public_session_id: str,
    counts: dict[str, int],
) -> Any:
    if isinstance(value, dict):
        return {
            key: _replace_values(
                item,
                source_cwd=source_cwd,
                source_session_id=source_session_id,
                public_session_id=public_session_id,
                counts=counts,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _replace_values(
                item,
                source_cwd=source_cwd,
                source_session_id=source_session_id,
                public_session_id=public_session_id,
                counts=counts,
            )
            for item in value
        ]
    if not isinstance(value, str):
        return value
    replaced = value
    cwd_occurrences = replaced.count(source_cwd)
    if cwd_occurrences:
        replaced = replaced.replace(source_cwd, PUBLIC_CWD)
        counts["cwd"] += cwd_occurrences
    uuid_occurrences = replaced.count(source_session_id)
    if uuid_occurrences:
        replaced = replaced.replace(source_session_id, public_session_id)
        counts["uuid"] += uuid_occurrences
    return replaced


def _omp_title_slot(title: dict[str, Any]) -> bytes:
    if (
        title.get("v") != 1
        or not isinstance(title.get("title"), str)
        or not isinstance(title.get("updatedAt"), str)
    ):
        raise RuntimeError("OMP title slot has an invalid shape")
    record = {key: value for key, value in title.items() if key != "pad"}
    record["pad"] = ""

    def encode() -> bytes:
        return (json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n").encode()

    unpadded = encode()
    remaining = OMP_TITLE_SLOT_BYTES - len(unpadded)
    if remaining < 0:
        raise RuntimeError("OMP title slot cannot fit in 256 bytes")
    record["pad"] = " " * remaining
    encoded = encode()
    if len(encoded) != OMP_TITLE_SLOT_BYTES:
        raise RuntimeError("failed to rebuild OMP's 256-byte title slot")
    return encoded


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("format", choices=("pi", "omp"))
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--source-cwd", required=True)
    parser.add_argument("--source-session-id", required=True)
    parser.add_argument("--public-session-id", required=True)
    arguments = parser.parse_args()
    counts = sanitize_capture(
        arguments.source,
        arguments.destination,
        source_cwd=arguments.source_cwd,
        source_session_id=arguments.source_session_id,
        public_session_id=arguments.public_session_id,
        format_name=arguments.format,
    )
    print(json.dumps(counts, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
