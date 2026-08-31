from dataclasses import dataclass


@dataclass(frozen=True)
class Event:
    start_ms: int
    end_ms: int


def touches(previous: Event, current: Event) -> bool:
    """Return whether two closed intervals overlap or touch."""
    return current.start_ms < previous.end_ms
