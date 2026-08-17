"""Shared format-adapter helpers."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

_PORTABLE_IMAGE_MEDIA_TYPES = {
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
}
_BASE64 = re.compile(r"[A-Za-z0-9+/]*={0,2}")


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
        if media_type in _PORTABLE_IMAGE_MEDIA_TYPES and _valid_base64(data):
            return f"data:{media_type};base64,{data}"
    if source_type == "url":
        url = string(value.get("url"))
        return url if url and _valid_remote_image_url(url) else None
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
            if media_type in _PORTABLE_IMAGE_MEDIA_TYPES and _valid_base64(data):
                return {"type": "base64", "media_type": media_type, "data": data}
        return None
    if _valid_remote_image_url(image_url):
        return {"type": "url", "url": image_url}
    return None


def valid_rfc3339(value: Any) -> str | None:
    """Return a timezone-aware RFC 3339-like timestamp or None."""

    timestamp = string(value)
    if not timestamp or "T" not in timestamp:
        return None
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return timestamp if parsed.tzinfo is not None else None


def _valid_base64(value: str | None) -> bool:
    return bool(value and len(value) % 4 == 0 and _BASE64.fullmatch(value))


def _valid_remote_image_url(value: str) -> bool:
    return value.startswith(("https://", "http://"))
