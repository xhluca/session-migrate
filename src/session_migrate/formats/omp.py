"""Oh My Pi 18.0.5 native v3 session adapter.

OMP is a fork of Pi, but its current journal is not a Pi alias: it lives below
``~/.omp/agent``, uses different project buckets, and reserves a fixed-width
title record at the start of each JSONL file. This adapter writes that current
shape and reads both current title-slot journals and legacy slotless v3 files.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from session_migrate.errors import SessionMigrateError
from session_migrate.formats import pi
from session_migrate.formats.common import string, valid_rfc3339
from session_migrate.jsonl import file_sha256, iter_jsonl
from session_migrate.model import AgentFormat, Event, EventKind, Provenance, Session

PINNED_OMP_VERSION = "18.0.5"
PINNED_OMP_LINUX_X64_BYTES = 183_420_104
PINNED_OMP_LINUX_X64_SHA256 = "d5a322af241cebe2662b3b792ff29d3ea6e61364328e916c9429065f346391ed"
OMP_SESSION_VERSION = 3
OMP_NATIVE_IMPORT_SUPPORTED = True
TITLE_SLOT_BYTES = 256
MAX_NATIVE_BYTES = 256 * 1024 * 1024
MAX_BLOB_BYTES = 64 * 1024 * 1024
_BLOB_PREFIX = "blob:sha256:"


@dataclass(frozen=True, slots=True)
class ParsedOmpSession:
    """Portable projection of one OMP journal's active branch."""

    session_id: str
    cwd: Path
    started_at: str
    name: str | None
    model: str | None
    provider: str | None
    parent_session: str | None
    events: tuple[Event, ...]
    raw_record_count: int


def serialize(
    session: Session,
    *,
    session_id: str,
    cwd: Path,
    cli_version: str = PINNED_OMP_VERSION,
    provider: str = "anthropic",
    model: str | None = None,
    timestamp: str | None = None,
    name: str | None = None,
) -> tuple[bytes, dict[str, int]]:
    """Serialize portable history as a current OMP v3 title-slot journal."""

    del cli_version  # OMP records schema v3 in the header, not its CLI build.
    native, dropped = pi.serialize(
        session,
        session_id=session_id,
        cwd=cwd,
        provider=provider,
        model=model,
        timestamp=timestamp,
        name=name,
    )
    records = pi._decode_native_records(native)
    if not records or records[0].get("type") != "session":
        raise SessionMigrateError("OMP conversion did not produce a session header")

    # Pi's picker label is a trailing session_info entry. OMP instead folds its
    # mutable title into both the header and a 256-byte first-line slot.
    records = [record for record in records if record.get("type") != "session_info"]
    requested_title = name or session.title or ""
    slot, native_title = _serialize_title_slot(requested_title, records[0]["timestamp"])
    if native_title:
        records[0]["title"] = native_title
        records[0]["titleSource"] = "user"
    body = b"".join(
        (json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
        for record in records
    )
    return slot + body, dropped


def parse(path: Path) -> ParsedOmpSession:
    """Parse the active, compaction-aware branch of an OMP v3 journal."""

    raw = list(iter_jsonl(path))
    physical_records = [dict(record.value) for record in raw]
    title_slot: dict[str, Any] | None = None
    if physical_records and physical_records[0].get("type") == "title":
        title_slot = physical_records.pop(0)
    _validate_records(physical_records, title_slot=title_slot)
    header = physical_records[0]
    session_id = string(header.get("id"))
    cwd = string(header.get("cwd"))
    started_at = string(header.get("timestamp"))
    if not session_id or not cwd or not started_at:
        raise SessionMigrateError("OMP session header is missing required metadata")

    entries = physical_records[1:]
    indexed: dict[str, dict[str, Any]] = {}
    record_indices: dict[str, int] = {}
    offset = 2 if title_slot else 1
    for record_index, entry in enumerate(entries, start=offset):
        entry_id = string(entry.get("id"))
        if not entry_id:
            raise SessionMigrateError("OMP session entry is missing an id")
        indexed[entry_id] = entry
        record_indices[entry_id] = record_index
    path_entries = pi._active_path(entries, indexed)
    selected_ids = {string(entry.get("id")) for entry in path_entries}
    events: list[Event] = []
    model = None
    provider = None
    reset_index = max(
        (
            index
            for index, entry in enumerate(path_entries)
            if entry.get("type") == "reset_boundary"
        ),
        default=-1,
    )
    for path_index, entry in enumerate(path_entries):
        entry_id = string(entry.get("id")) or ""
        if path_index < reset_index:
            entry_events = [_opaque_entry(entry, record_indices[entry_id], "omp_pre_reset_entry")]
        else:
            entry_events = _entry_events(entry, record_indices[entry_id])
        events.extend(_resolve_blob_images(entry_events, path))
        if entry.get("type") == "model_change":
            selector = string(entry.get("model"))
            if selector and "/" in selector:
                provider, model = selector.split("/", 1)
        elif entry.get("type") == "message" and isinstance(entry.get("message"), dict):
            message = entry["message"]
            if message.get("role") == "assistant":
                model = string(message.get("model")) or model
                provider = string(message.get("provider")) or provider
    for entry in entries:
        entry_id = string(entry.get("id"))
        if entry_id not in selected_ids:
            events.append(
                Event(
                    kind=EventKind.OPAQUE,
                    timestamp=string(entry.get("timestamp")),
                    payload={"reason": "inactive_omp_branch_entry"},
                    provenance=Provenance(
                        record_indices[entry_id or ""], string(entry.get("type"))
                    ),
                )
            )

    title = _title_from_records(header, entries, title_slot)
    return ParsedOmpSession(
        session_id=session_id,
        cwd=Path(cwd),
        started_at=started_at,
        name=title,
        model=model,
        provider=provider,
        parent_session=string(header.get("parentSession")),
        events=tuple(events),
        raw_record_count=len(raw),
    )


def _entry_events(entry: dict[str, Any], record_index: int) -> list[Event]:
    entry_type = string(entry.get("type"))
    if entry_type == "title_change":
        return []
    if entry_type in {
        "credential_pin",
        "mode_change",
        "reset_boundary",
        "service_tier_change",
        "session_init",
        "ttsr_injection",
    }:
        return [_opaque_entry(entry, record_index, f"omp_{entry_type}")]
    return pi._entry_events(entry, record_index, reason_prefix="omp")


def _opaque_entry(entry: dict[str, Any], record_index: int, reason: str) -> Event:
    return Event(
        kind=EventKind.OPAQUE,
        timestamp=string(entry.get("timestamp")),
        payload={"reason": reason},
        provenance=Provenance(record_index, string(entry.get("type"))),
    )


def parse_session(path: Path) -> Session:
    """Parse OMP v3 into the migrator's source-session model."""

    parsed = parse(path)
    events = list(parsed.events)
    if parsed.parent_session:
        events.insert(
            0,
            Event(
                kind=EventKind.OPAQUE,
                timestamp=parsed.started_at,
                payload={"reason": "omp_parent_session"},
                provenance=Provenance(0, "session"),
            ),
        )
    return Session(
        source_format=AgentFormat.OMP,
        source_path=path.resolve(),
        source_sha256=file_sha256(path),
        session_id=parsed.session_id,
        cwd=parsed.cwd,
        started_at=parsed.started_at,
        cli_version=None,
        model=parsed.model,
        title=parsed.name,
        events=tuple(events),
        raw_record_count=parsed.raw_record_count,
        model_provider=parsed.provider,
    )


def validate_native_bytes(data: bytes, session_id: str) -> None:
    """Validate a generated current OMP title-slot journal."""

    records, title_slot = _decode_native_records(data)
    if title_slot is None:
        raise SessionMigrateError("OMP target is missing its fixed-width title slot")
    first_newline = data.find(b"\n")
    if first_newline + 1 != TITLE_SLOT_BYTES:
        raise SessionMigrateError("OMP title slot is not exactly 256 bytes")
    _validate_records(records, title_slot=title_slot, expected_session_id=session_id)


def native_record_count(data: bytes) -> int:
    """Count physical OMP records, including its title slot."""

    records, title_slot = _decode_native_records(data)
    return len(records) + (1 if title_slot else 0)


def session_relative_path(cwd: Path, session_id: str, timestamp: str) -> Path:
    """Return OMP's current path below its active agent directory."""

    date = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(UTC)
    stamp = date.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    stamp = stamp.replace(":", "-").replace(".", "-")
    return Path("sessions") / session_directory_name(cwd) / f"{stamp}_{session_id}.jsonl"


def session_directory_name(cwd: Path) -> str:
    """Encode a cwd using OMP 18.0.5's home/tmp/absolute bucket rules."""

    resolved = cwd.resolve()
    home = Path.home().resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    try:
        relative = resolved.relative_to(home)
        return _relative_bucket("-", relative)
    except ValueError:
        pass
    try:
        relative = resolved.relative_to(temp_root)
        return _relative_bucket("-tmp", relative)
    except ValueError:
        pass
    escaped = str(resolved).lstrip("/\\").replace("/", "-").replace("\\", "-").replace(":", "-")
    return f"--{escaped}--"


def _relative_bucket(prefix: str, relative: Path) -> str:
    encoded = str(relative).replace("/", "-").replace("\\", "-").replace(":", "-")
    if encoded in {"", "."}:
        return prefix
    return f"{prefix}{encoded}" if prefix.endswith("-") else f"{prefix}-{encoded}"


def _serialize_title_slot(title: str, updated_at: str) -> tuple[bytes, str]:
    source = "user" if title else None
    codepoints = list(title)

    def encoded(candidate: str, pad: str = "") -> bytes:
        record: dict[str, Any] = {
            "type": "title",
            "v": 1,
            "title": candidate,
        }
        if source:
            record["source"] = source
        record["updatedAt"] = updated_at
        record["pad"] = pad
        return (json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n").encode(
            "utf-8"
        )

    low, high, native_title = 0, len(codepoints), ""
    while low <= high:
        middle = (low + high) // 2
        candidate = "".join(codepoints[:middle])
        if len(encoded(candidate)) <= TITLE_SLOT_BYTES:
            native_title = candidate
            low = middle + 1
        else:
            high = middle - 1
    unpadded = encoded(native_title)
    slot = encoded(native_title, " " * (TITLE_SLOT_BYTES - len(unpadded)))
    if len(slot) != TITLE_SLOT_BYTES:
        raise SessionMigrateError("failed to serialize OMP's fixed-width title slot")
    return slot, native_title


def _decode_native_records(
    data: bytes,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if len(data) > MAX_NATIVE_BYTES:
        raise SessionMigrateError("OMP session exceeds the native artifact safety limit")
    try:
        text = data.decode("utf-8")
        values: list[dict[str, Any]] = []
        for line_number, line in enumerate(text.split("\n"), start=1):
            if not line.strip():
                continue
            value = json.loads(line, parse_constant=pi._reject_json_constant)
            if not isinstance(value, dict):
                raise SessionMigrateError(
                    f"OMP session record at line {line_number} is not an object"
                )
            values.append(value)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SessionMigrateError("OMP session is not valid UTF-8 JSONL") from exc
    title_slot = values.pop(0) if values and values[0].get("type") == "title" else None
    return values, title_slot


def _validate_records(
    records: list[dict[str, Any]],
    *,
    title_slot: dict[str, Any] | None,
    expected_session_id: str | None = None,
) -> None:
    if not records:
        raise SessionMigrateError("OMP session is empty")
    if title_slot is not None and (
        title_slot.get("v") != 1
        or not isinstance(title_slot.get("title"), str)
        or not isinstance(title_slot.get("updatedAt"), str)
        or not isinstance(title_slot.get("pad"), str)
        or title_slot.get("source") not in {None, "auto", "user"}
    ):
        raise SessionMigrateError("OMP session has an invalid title slot")
    header = records[0]
    if header.get("type") != "session" or header.get("version") != OMP_SESSION_VERSION:
        raise SessionMigrateError("OMP session does not have a supported v3 header")
    session_id = string(header.get("id"))
    if expected_session_id is not None and session_id != expected_session_id:
        raise SessionMigrateError("OMP session header ID does not match the target ID")
    if (
        not session_id
        or not string(header.get("cwd"))
        or not valid_rfc3339(header.get("timestamp"))
    ):
        raise SessionMigrateError("OMP session header is missing required metadata")

    known_ids: set[str] = set()
    has_context = False
    entries = records[1:]
    for entry in entries:
        entry_id = string(entry.get("id"))
        if not entry_id:
            raise SessionMigrateError("OMP session entry is missing an id")
        if entry_id in known_ids:
            raise SessionMigrateError("OMP session contains a duplicate entry id")
        parent_id = entry.get("parentId")
        if parent_id is not None and (not isinstance(parent_id, str) or parent_id not in known_ids):
            raise SessionMigrateError("OMP session tree references a missing parent")
        if not valid_rfc3339(entry.get("timestamp")):
            raise SessionMigrateError("OMP session entry has an invalid timestamp")
        known_ids.add(entry_id)
        entry_type = string(entry.get("type"))
        if entry_type == "message":
            _validate_message(entry.get("message"))
            has_context = True
        elif entry_type == "compaction":
            if not string(entry.get("summary")) or not string(entry.get("firstKeptEntryId")):
                raise SessionMigrateError("OMP compaction entry is missing required metadata")
            if not isinstance(entry.get("tokensBefore"), int) or entry["tokensBefore"] < 0:
                raise SessionMigrateError("OMP compaction entry has invalid token metadata")
            has_context = True
        elif entry_type == "title_change":
            if not string(entry.get("title")) or entry.get("source") not in {"auto", "user"}:
                raise SessionMigrateError("OMP title change entry is invalid")
    if entries:
        pi._active_path(entries, {str(entry["id"]): entry for entry in entries})
    if not has_context:
        raise SessionMigrateError("OMP session has no resumable conversation context")


def _validate_message(value: Any) -> None:
    if not isinstance(value, dict):
        raise SessionMigrateError("OMP message entry is missing its message")
    role = string(value.get("role"))
    if role not in {"user", "assistant", "developer", "toolResult", "custom", "bashExecution"}:
        raise SessionMigrateError("OMP message entry has an unsupported role")
    if not isinstance(value.get("content"), (str, list)):
        raise SessionMigrateError("OMP message entry has invalid content")
    if role == "toolResult" and not string(value.get("toolCallId")):
        raise SessionMigrateError("OMP tool result is missing its call ID")


def _title_from_records(
    header: dict[str, Any],
    entries: list[dict[str, Any]],
    title_slot: dict[str, Any] | None,
) -> str | None:
    if title_slot and string(title_slot.get("title")):
        return string(title_slot.get("title"))
    for entry in reversed(entries):
        if entry.get("type") == "title_change" and string(entry.get("title")):
            return string(entry.get("title"))
    return string(header.get("title"))


def _resolve_blob_images(events: list[Event], session_path: Path) -> list[Event]:
    root = next(
        (parent.parent for parent in session_path.parents if parent.name == "sessions"), None
    )
    if root is None:
        return events
    result: list[Event] = []
    for event in events:
        if event.kind != EventKind.CONTEXT:
            result.append(event)
            continue
        url = event.payload.get("image_url")
        if not isinstance(url, str) or not url.startswith("data:") or _BLOB_PREFIX not in url:
            result.append(event)
            continue
        prefix, ref = url.split(",", 1)
        if not ref.startswith(_BLOB_PREFIX):
            result.append(event)
            continue
        digest = ref.removeprefix(_BLOB_PREFIX)
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise SessionMigrateError("OMP image contains an invalid blob reference")
        blob_path = root / "blobs" / digest
        try:
            descriptor = os.open(blob_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            raise SessionMigrateError("OMP image blob is unavailable") from exc
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BLOB_BYTES:
                raise SessionMigrateError("OMP image blob is not a bounded regular file")
            content = bytearray()
            while len(content) <= MAX_BLOB_BYTES:
                chunk = os.read(descriptor, min(1024 * 1024, MAX_BLOB_BYTES + 1 - len(content)))
                if not chunk:
                    break
                content.extend(chunk)
        finally:
            os.close(descriptor)
        data = bytes(content)
        if hashlib.sha256(data).hexdigest() != digest:
            raise SessionMigrateError("OMP image blob hash does not match its reference")
        payload = dict(event.payload)
        payload["image_url"] = f"{prefix},{base64.b64encode(data).decode('ascii')}"
        result.append(replace(event, payload=payload))
    return result
