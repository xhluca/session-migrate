#!/usr/bin/env python3
"""Sanitize one exact Mistral Vibe 2.24.3 native session directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

PUBLIC_CWD = "/fixture/work"
PUBLIC_HOME = "/fixture/vibe-home"
PUBLIC_USERNAME = "fixture-user"
PUBLIC_CONFIG = {
    "writer": "sanitized-native-vibe-2.24.3",
    "target_cli_version": "2.24.3",
    "model": "fixture-model",
}


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-session-dir", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--source-cwd", required=True)
    parser.add_argument("--source-home", required=True)
    parser.add_argument("--source-username", required=True)
    return parser.parse_args()


def _replace_paths(value: Any, replacements: dict[str, str], counts: dict[str, int]) -> Any:
    if isinstance(value, str):
        result = value
        for source, target in replacements.items():
            occurrences = result.count(source)
            if occurrences:
                result = result.replace(source, target)
                counts[source] += occurrences
        return result
    if isinstance(value, list):
        return [_replace_paths(item, replacements, counts) for item in value]
    if isinstance(value, dict):
        return {key: _replace_paths(item, replacements, counts) for key, item in value.items()}
    return value


def sanitize_session(
    source: Path,
    destination_root: Path,
    *,
    source_cwd: str,
    source_home: str,
    source_username: str,
) -> tuple[str, tuple[Path, ...], dict[str, int]]:
    meta_path = source / "meta.json"
    messages_path = source / "messages.jsonl"
    if not meta_path.is_file() or not messages_path.is_file():
        raise RuntimeError("raw Vibe capture is missing meta.json or messages.jsonl")
    meta = json.loads(meta_path.read_text())
    if not isinstance(meta, dict):
        raise RuntimeError("raw Vibe metadata is malformed")
    session_id = str(uuid.UUID(str(meta.get("session_id"))))
    if meta.get("username") != source_username:
        raise RuntimeError("raw Vibe username does not match the expected capture account")
    messages: list[dict[str, Any]] = []
    for line_number, line in enumerate(messages_path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise RuntimeError(f"Vibe message {line_number} is not an object")
        messages.append(value)
    if not messages or meta.get("total_messages") != len(messages):
        raise RuntimeError("raw Vibe message count does not match metadata")

    replacements = {source_cwd: PUBLIC_CWD, source_home: PUBLIC_HOME}
    counts = {source_cwd: 0, source_home: 0}
    meta = _replace_paths(meta, replacements, counts)
    messages = _replace_paths(messages, replacements, counts)
    meta["username"] = PUBLIC_USERNAME
    system_prompt = meta.get("system_prompt")
    if not isinstance(system_prompt, dict) or not isinstance(system_prompt.get("content"), str):
        raise RuntimeError("raw Vibe metadata has no structured runtime system prompt")
    if not isinstance(meta.get("config"), dict) or not isinstance(
        meta.get("tools_available"), list
    ):
        raise RuntimeError("raw Vibe metadata has no runtime config/tool inventory")
    meta["system_prompt"] = None
    meta["config"] = dict(PUBLIC_CONFIG)
    meta["tools_available"] = []

    attachment_sources: dict[str, Path] = {}
    image_paths_rewritten = 0
    for message in messages:
        images = message.get("images")
        if not isinstance(images, list):
            continue
        for image in images:
            image_source = image.get("source") if isinstance(image, dict) else None
            if not isinstance(image_source, dict) or image_source.get("kind") != "file":
                continue
            raw_path = image_source.get("path")
            if not isinstance(raw_path, str):
                raise RuntimeError("raw Vibe image attachment has no file path")
            original = Path(raw_path.replace(PUBLIC_HOME, source_home)).resolve()
            attachment_root = (source / "attachments").resolve()
            if original.parent != attachment_root or not original.is_file():
                raise RuntimeError("raw Vibe image attachment escapes the session directory")
            relative = f"attachments/{original.name}"
            image_source["path"] = relative
            attachment_sources[relative] = original
            image_paths_rewritten += 1
    if image_paths_rewritten != 1:
        raise RuntimeError("expected exactly one persisted Vibe image attachment")
    if counts[source_cwd] < 1 or counts[source_home] < 1:
        raise RuntimeError("capture paths did not match the expected Vibe fields")

    destination = destination_root / "native" / source.name
    if destination.exists():
        raise RuntimeError("Vibe sanitizer output already exists")
    destination.mkdir(parents=True, mode=0o700)
    output_meta = destination / "meta.json"
    output_messages = destination / "messages.jsonl"
    output_meta.write_text(json.dumps(meta, ensure_ascii=False, separators=(",", ":")) + "\n")
    output_messages.write_text(
        "".join(
            json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
            for message in messages
        )
    )
    outputs = [output_meta, output_messages]
    for relative, original in sorted(attachment_sources.items()):
        output = destination / relative
        output.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        shutil.copyfile(original, output)
        outputs.append(output)
    for output in outputs:
        os.chmod(output, 0o600)
    return session_id, tuple(outputs), {
        "capture_cwd": counts[source_cwd],
        "capture_home": counts[source_home],
        "image_path": image_paths_rewritten,
        "runtime_config": 1,
        "system_prompt": 1,
        "tool_inventory": 1,
        "username": 1,
    }


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    args = arguments()
    session_id, files, mutations = sanitize_session(
        args.raw_session_dir,
        args.output_root,
        source_cwd=args.source_cwd,
        source_home=args.source_home,
        source_username=args.source_username,
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
