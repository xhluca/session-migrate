#!/usr/bin/env python3
"""Sanitize a Grok 1.0.5 native summary/update pair."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

PUBLIC_CWD = "/fixture/work"
PUBLIC_HOME = "/fixture/grok-home"


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-session-dir", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--source-cwd", required=True)
    parser.add_argument("--source-home", required=True)
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


def sanitize_session(
    source: Path,
    destination_root: Path,
    *,
    source_cwd: str,
    source_home: str,
) -> tuple[str, tuple[Path, Path], dict[str, int]]:
    raw_summary = source / "summary.json"
    raw_updates = source / "updates.jsonl"
    if not raw_summary.is_file() or not raw_updates.is_file():
        raise RuntimeError("raw Grok capture is missing summary.json or updates.jsonl")
    summary = json.loads(raw_summary.read_text())
    if not isinstance(summary, dict) or not isinstance(summary.get("info"), dict):
        raise RuntimeError("raw Grok summary is malformed")
    session_id = str(uuid.UUID(str(summary["info"].get("id"))))
    counts = {source_cwd: 0, source_home: 0}
    replacements = {source_cwd: PUBLIC_CWD, source_home: PUBLIC_HOME}
    summary = replace_paths(summary, replacements, counts)
    updates: list[dict[str, Any]] = []
    for line_number, line in enumerate(raw_updates.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise RuntimeError(f"Grok update {line_number} is not an object")
        params = value.get("params")
        if not isinstance(params, dict) or params.get("sessionId") != session_id:
            raise RuntimeError(f"Grok update {line_number} has invalid session linkage")
        updates.append(replace_paths(value, replacements, counts))
    if not updates:
        raise RuntimeError("raw Grok capture has no updates")
    if counts[source_cwd] < 1 or counts[source_home] < 1:
        raise RuntimeError("capture paths did not match the expected Grok fields")
    destination = destination_root / "native" / session_id
    if destination.exists():
        raise RuntimeError("Grok sanitizer output already exists")
    destination.mkdir(parents=True, mode=0o700)
    summary_path = destination / "summary.json"
    updates_path = destination / "updates.jsonl"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, separators=(",", ":")) + "\n")
    updates_path.write_text(
        "".join(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n" for value in updates
        )
    )
    os.chmod(summary_path, 0o600)
    os.chmod(updates_path, 0o600)
    return (
        session_id,
        (summary_path, updates_path),
        {
            "capture_cwd": counts[source_cwd],
            "capture_home": counts[source_home],
        },
    )


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    args = arguments()
    session_id, files, mutations = sanitize_session(
        args.raw_session_dir,
        args.output_root,
        source_cwd=args.source_cwd,
        source_home=args.source_home,
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
