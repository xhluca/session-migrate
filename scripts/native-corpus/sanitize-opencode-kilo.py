#!/usr/bin/env python3
"""Mechanically sanitize official OpenCode/Kilo export bundles."""

from __future__ import annotations

import argparse
import copy
import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PUBLIC_CWD = "/fixture/work"
EXPECTED_VERSIONS = {"opencode": "1.17.20", "kilo": "7.5.0"}
PART_TYPES = frozenset(
    {
        "agent",
        "compaction",
        "file",
        "patch",
        "reasoning",
        "retry",
        "snapshot",
        "step-finish",
        "step-start",
        "subtask",
        "text",
        "tool",
    }
)


class SanitizationError(ValueError):
    """Raised when input is not a recognized official export bundle."""


@dataclass(frozen=True, slots=True)
class SanitizedBundle:
    document: Mapping[str, Any]
    mutations: Mapping[str, int]


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise SanitizationError(f"{label} must be a JSON object")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise SanitizationError(f"{label} must be a non-empty string")
    return value


def validate_document(value: object, *, format_name: str) -> dict[str, Any]:
    if format_name not in EXPECTED_VERSIONS:
        raise SanitizationError(f"unsupported format: {format_name}")
    document = _object(value, "export")
    if set(document) != {"info", "messages"}:
        raise SanitizationError("export must contain exactly info and messages")
    info = _object(document["info"], "export.info")
    for key in ("id", "directory", "title", "version"):
        _string(info.get(key), f"export.info.{key}")
    if not str(info["id"]).startswith("ses_"):
        raise SanitizationError("export.info.id must use the ses_ native namespace")
    if info["version"] != EXPECTED_VERSIONS[format_name]:
        raise SanitizationError(
            f"expected {format_name} {EXPECTED_VERSIONS[format_name]}, got {info['version']!r}"
        )
    _object(info.get("time"), "export.info.time")
    messages = document["messages"]
    if not isinstance(messages, list) or not messages:
        raise SanitizationError("export.messages must be a non-empty array")
    for message_index, message_value in enumerate(messages):
        message = _object(message_value, f"export.messages[{message_index}]")
        if set(message) != {"info", "parts"}:
            raise SanitizationError(
                f"export.messages[{message_index}] must contain exactly info and parts"
            )
        message_info = _object(message["info"], f"export.messages[{message_index}].info")
        role = _string(message_info.get("role"), f"export.messages[{message_index}].info.role")
        if role not in {"assistant", "user"}:
            raise SanitizationError(f"unsupported native message role: {role}")
        for key in ("id", "sessionID"):
            _string(message_info.get(key), f"export.messages[{message_index}].info.{key}")
        if message_info["sessionID"] != info["id"]:
            raise SanitizationError("message sessionID does not match export.info.id")
        _object(message_info.get("time"), f"export.messages[{message_index}].info.time")
        parts = message["parts"]
        if not isinstance(parts, list) or not parts:
            raise SanitizationError(f"export.messages[{message_index}].parts must be non-empty")
        for part_index, part_value in enumerate(parts):
            label = f"export.messages[{message_index}].parts[{part_index}]"
            part = _object(part_value, label)
            part_type = _string(part.get("type"), f"{label}.type")
            if part_type not in PART_TYPES:
                raise SanitizationError(f"{label}.type is unsupported: {part_type}")
            for key in ("id", "sessionID", "messageID"):
                _string(part.get(key), f"{label}.{key}")
            if part["sessionID"] != info["id"]:
                raise SanitizationError(f"{label}.sessionID does not match export.info.id")
            if part["messageID"] != message_info["id"]:
                raise SanitizationError(f"{label}.messageID does not match its message")
            if part_type == "file":
                _string(part.get("mime"), f"{label}.mime")
                url = _string(part.get("url"), f"{label}.url")
                if not (url.startswith("data:") or url.startswith("file:")):
                    raise SanitizationError(f"{label}.url must be a data: or file: URL")
            if part_type == "tool":
                _string(part.get("callID"), f"{label}.callID")
                _string(part.get("tool"), f"{label}.tool")
                state = _object(part.get("state"), f"{label}.state")
                status = _string(state.get("status"), f"{label}.state.status")
                if status not in {"completed", "error", "pending", "running"}:
                    raise SanitizationError(f"{label}.state.status is unsupported: {status}")
    return document


def sanitize_document(
    value: object,
    *,
    format_name: str,
    source_cwd: Path,
) -> SanitizedBundle:
    document = copy.deepcopy(validate_document(value, format_name=format_name))
    private_cwd = str(source_cwd.resolve())
    if not private_cwd.startswith("/") or private_cwd == "/":
        raise SanitizationError("source cwd must be a specific absolute directory")
    private_relative = private_cwd.lstrip("/")
    mutations: Counter[str] = Counter()

    def replace(value: object, selector: str) -> object:
        if isinstance(value, str):
            updated = value
            absolute_count = updated.count(private_cwd)
            if absolute_count:
                updated = updated.replace(private_cwd, PUBLIC_CWD)
                mutations[selector] += absolute_count
            relative_count = updated.count(private_relative)
            if relative_count:
                updated = updated.replace(private_relative, PUBLIC_CWD.lstrip("/"))
                mutations[selector] += relative_count
            return updated
        if isinstance(value, list):
            return [replace(item, selector) for item in value]
        if isinstance(value, dict):
            return {key: replace(item, selector) for key, item in value.items()}
        return value

    info = document["info"]
    info["directory"] = replace(info["directory"], "session directory")
    if "path" in info:
        info["path"] = replace(info["path"], "session path")
    for message in document["messages"]:
        message["info"] = replace(message["info"], "message metadata path")
        message["parts"] = replace(message["parts"], "message part path")

    serialized = json.dumps(document, sort_keys=True)
    if private_cwd in serialized or private_relative in serialized:
        raise SanitizationError("private capture path remains after sanitization")
    if info["directory"] != PUBLIC_CWD:
        raise SanitizationError("sanitized export did not produce the canonical public cwd")
    validate_document(document, format_name=format_name)
    return SanitizedBundle(document=document, mutations=dict(sorted(mutations.items())))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--format", choices=sorted(EXPECTED_VERSIONS), required=True)
    parser.add_argument("--raw-export", type=Path, required=True)
    parser.add_argument("--source-cwd", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    value = json.loads(args.raw_export.read_text())
    result = sanitize_document(value, format_name=args.format, source_cwd=args.source_cwd)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result.document, indent=2) + "\n")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result.mutations, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
