"""Strict, bounded JSONL input and atomic private-file output."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from session_bridge.errors import JsonlError

DEFAULT_MAX_RECORD_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class JsonlRecord:
    """One decoded JSON object and its content-free location metadata."""

    index: int
    line_number: int
    value: Mapping[str, Any]


def iter_jsonl(
    path: Path, *, max_record_bytes: int = DEFAULT_MAX_RECORD_BYTES
) -> Iterator[JsonlRecord]:
    """Yield non-empty JSONL objects, rejecting malformed or oversized records."""

    try:
        stream = path.open("rb")
    except OSError as exc:
        raise JsonlError(f"cannot open session file {path}: {exc.strerror or exc}") from exc

    record_index = 0
    with stream:
        for line_number, raw_line in enumerate(stream, start=1):
            if len(raw_line) > max_record_bytes:
                raise JsonlError(
                    f"session record at line {line_number} exceeds "
                    f"the {max_record_bytes}-byte safety limit"
                )
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                value = json.loads(stripped)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise JsonlError(f"invalid JSON session record at line {line_number}") from exc
            if not isinstance(value, dict):
                raise JsonlError(f"session record at line {line_number} is not a JSON object")
            yield JsonlRecord(index=record_index, line_number=line_number, value=value)
            record_index += 1


def file_sha256(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Return a streaming SHA-256 digest without loading conversation data at once."""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(chunk_size):
                digest.update(chunk)
    except OSError as exc:
        raise JsonlError(f"cannot hash session file {path}: {exc.strerror or exc}") from exc
    return digest.hexdigest()


def encode_jsonl(records: Iterable[Mapping[str, Any]]) -> bytes:
    """Encode native records deterministically while preserving Unicode."""

    lines = [json.dumps(record, ensure_ascii=False, separators=(",", ":")) for record in records]
    return (("\n".join(lines) + "\n") if lines else "").encode()


def write_private_atomic(path: Path, data: bytes, *, overwrite: bool = False) -> None:
    """Write mode-0600 bytes atomically without silently replacing a session."""

    path = path.resolve()
    if path.exists() and not overwrite:
        raise JsonlError(f"refusing to overwrite existing target: {path}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if overwrite:
                os.replace(temporary_path, path)
            else:
                # A hard link is an atomic create-if-absent operation. Unlike an
                # exists() check followed by replace(), it cannot clobber a file
                # created by another process during this write.
                try:
                    os.link(temporary_path, path)
                except FileExistsError as exc:
                    raise JsonlError(f"refusing to overwrite existing target: {path}") from exc
                temporary_path.unlink()
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise
    except JsonlError:
        raise
    except OSError as exc:
        raise JsonlError(f"cannot write target session {path}: {exc.strerror or exc}") from exc
