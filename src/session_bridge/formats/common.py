"""Shared format-adapter helpers."""

from __future__ import annotations

import json
from typing import Any


def string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def object_value(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def json_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def content_text(value: Any) -> str:
    """Extract display/model text from common text-block containers."""

    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part for part in parts if part)
    return ""

