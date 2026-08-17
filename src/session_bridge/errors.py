"""User-facing failures with content-safe messages."""


class SessionBridgeError(Exception):
    """Base error for expected parse, validation, and installation failures."""


class JsonlError(SessionBridgeError):
    """A JSONL stream is unreadable or structurally invalid."""


class FormatDetectionError(SessionBridgeError):
    """The agent session format cannot be identified safely."""

