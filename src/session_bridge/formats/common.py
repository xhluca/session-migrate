"""Shared format-adapter helpers."""

from __future__ import annotations

from typing import Any


def string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def object_value(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


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


def image_url_from_claude_source(value: Any) -> str | None:
    """Normalize a Claude image source into Codex's self-contained URL form."""

    if not isinstance(value, dict):
        return None
    source_type = string(value.get("type"))
    if source_type == "base64":
        media_type = string(value.get("media_type"))
        data = string(value.get("data"))
        if media_type and data:
            return f"data:{media_type};base64,{data}"
    if source_type == "url":
        return string(value.get("url"))
    return None


def claude_source_from_image_url(value: Any) -> dict[str, str] | None:
    """Normalize a Codex image URL into a Claude base64 or URL source."""

    image_url = string(value)
    if not image_url:
        return None
    if image_url.startswith("data:"):
        header, separator, data = image_url.partition(",")
        prefix = "data:"
        suffix = ";base64"
        if separator and header.startswith(prefix) and header.endswith(suffix) and data:
            media_type = header[len(prefix) : -len(suffix)]
            if media_type:
                return {"type": "base64", "media_type": media_type, "data": data}
        return None
    return {"type": "url", "url": image_url}
