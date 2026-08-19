"""User-facing failures with content-safe messages."""


class SessionMigrateError(Exception):
    """Base error for expected parse, validation, and installation failures."""


class JsonlError(SessionMigrateError):
    """A JSONL stream is unreadable or structurally invalid."""


class FormatDetectionError(SessionMigrateError):
    """The agent session format cannot be identified safely."""
