#!/usr/bin/env python3
"""Sanitize a Copilot native capture without fabricating conversation events.

The sanitizer keeps the exact native event envelopes, IDs, parent chain,
timestamps, messages, tool activity, and binary assets. It only replaces the
private capture paths and the vendor-generated system prompt. The resulting
files must still be reloaded by the pinned Copilot client before promotion to
the public corpus.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

PUBLIC_CWD = "/fixture/work"
PUBLIC_IMAGE = "/fixture/work/corpus-card.png"
PUBLIC_DOCUMENT = "/fixture/work/corpus-document.pdf"
SYSTEM_PLACEHOLDER = "SANITIZED_NATIVE_COPILOT_SYSTEM_PROMPT"


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-session-dir", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--source-cwd", required=True)
    parser.add_argument("--source-image", required=True)
    parser.add_argument("--source-document", required=True)
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


def sanitize_events(
    source: Path,
    destination: Path,
    *,
    source_cwd: str,
    source_image: str,
    source_document: str,
) -> tuple[str, dict[str, int], int]:
    records: list[dict[str, Any]] = []
    system_count = 0
    path_counts = {source_cwd: 0, source_image: 0, source_document: 0}
    for line_number, line in enumerate(source.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise RuntimeError(f"record {line_number} is not an object")
        value = replace_paths(
            value,
            {
                source_cwd: PUBLIC_CWD,
                source_image: PUBLIC_IMAGE,
                source_document: PUBLIC_DOCUMENT,
            },
            path_counts,
        )
        if value.get("type") == "system.message":
            data = value.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("content"), str):
                raise RuntimeError("system.message has no string content")
            data["content"] = SYSTEM_PLACEHOLDER
            system_count += 1
        records.append(value)
    if not records or records[0].get("type") != "session.start":
        raise RuntimeError("capture does not start with session.start")
    session_id = records[0].get("data", {}).get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        raise RuntimeError("capture has no native session ID")
    if system_count != 1:
        raise RuntimeError(f"expected exactly one system.message, found {system_count}")
    if (
        path_counts[source_cwd] < 1
        or path_counts[source_image] != 1
        or path_counts[source_document] != 2
    ):
        raise RuntimeError("capture paths did not match the expected native fields")
    rendered = "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n" for record in records
    )
    destination.parent.mkdir(parents=True, mode=0o700)
    destination.write_text(rendered)
    os.chmod(destination, 0o600)
    return session_id, path_counts, system_count


def sanitize_workspace(source: Path, destination: Path, *, source_cwd: str) -> None:
    text = source.read_text()
    if text.count(source_cwd) != 1:
        raise RuntimeError("workspace.yaml does not contain exactly one source CWD")
    text = text.replace(source_cwd, PUBLIC_CWD)
    if "name: repair-event-window-boundary\n" not in text:
        raise RuntimeError("workspace.yaml does not retain the corpus session title")
    destination.write_text(text)
    os.chmod(destination, 0o600)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    args = arguments()
    raw_events = args.raw_session_dir / "events.jsonl"
    raw_workspace = args.raw_session_dir / "workspace.yaml"
    if not raw_events.is_file() or not raw_workspace.is_file():
        raise RuntimeError("raw Copilot capture is missing events.jsonl or workspace.yaml")
    output_session = args.output_root / "native/session-state/pending"
    temporary_events = output_session / "events.jsonl"
    session_id, path_counts, system_count = sanitize_events(
        raw_events,
        temporary_events,
        source_cwd=args.source_cwd,
        source_image=args.source_image,
        source_document=args.source_document,
    )
    final_session = output_session.with_name(session_id)
    output_session.rename(final_session)
    sanitize_workspace(
        raw_workspace,
        final_session / "workspace.yaml",
        source_cwd=args.source_cwd,
    )
    print(
        json.dumps(
            {
                "artifacts": {
                    "events.jsonl": digest(final_session / "events.jsonl"),
                    "workspace.yaml": digest(final_session / "workspace.yaml"),
                },
                "mutations": {
                    "capture_cwd": path_counts[args.source_cwd],
                    "capture_image_path": path_counts[args.source_image],
                    "capture_document_path": path_counts[args.source_document],
                    "system_prompt": system_count,
                    "workspace_cwd": 1,
                },
                "session_id": session_id,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
