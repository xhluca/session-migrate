"""Strict, bounded JSONL input and atomic private-file output."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from session_migrate.errors import JsonlError

DEFAULT_MAX_RECORD_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_RECORDS = 100_000


@dataclass(frozen=True, slots=True)
class JsonlRecord:
    """One decoded JSON object and its content-free location metadata."""

    index: int
    line_number: int
    value: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    """Content-free identity used to detect a changing source transcript."""

    device: int
    inode: int
    size: int
    modified_ns: int


def iter_jsonl(
    path: Path,
    *,
    max_record_bytes: int = DEFAULT_MAX_RECORD_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_records: int = DEFAULT_MAX_RECORDS,
) -> Iterator[JsonlRecord]:
    """Yield non-empty JSONL objects, rejecting malformed or oversized records."""

    try:
        stream = path.open("rb")
    except OSError as exc:
        raise JsonlError(f"cannot open session file {path}: {exc.strerror or exc}") from exc

    record_index = 0
    line_number = 0
    total_bytes = 0
    with stream:
        try:
            file_size = os.fstat(stream.fileno()).st_size
        except OSError as exc:
            raise JsonlError(f"cannot inspect session file: {exc.strerror or exc}") from exc
        if file_size > max_total_bytes:
            raise JsonlError(f"session file exceeds the {max_total_bytes}-byte total safety limit")
        while raw_line := stream.readline(max_record_bytes + 1):
            line_number += 1
            total_bytes += len(raw_line)
            if total_bytes > max_total_bytes:
                raise JsonlError(
                    f"session file exceeds the {max_total_bytes}-byte total safety limit"
                )
            if len(raw_line) > max_record_bytes:
                raise JsonlError(
                    f"session record at line {line_number} exceeds "
                    f"the {max_record_bytes}-byte safety limit"
                )
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                value = json.loads(stripped, parse_constant=_reject_json_constant)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                raise JsonlError(f"invalid JSON session record at line {line_number}") from exc
            if not isinstance(value, dict):
                raise JsonlError(f"session record at line {line_number} is not a JSON object")
            if record_index >= max_records:
                raise JsonlError(f"session file exceeds the {max_records}-record safety limit")
            yield JsonlRecord(index=record_index, line_number=line_number, value=value)
            record_index += 1


def file_sha256(
    path: Path,
    *,
    chunk_size: int = 1024 * 1024,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
) -> str:
    """Return a streaming SHA-256 digest without loading conversation data at once."""

    digest = hashlib.sha256()
    total_bytes = 0
    try:
        with path.open("rb") as stream:
            if os.fstat(stream.fileno()).st_size > max_total_bytes:
                raise JsonlError(
                    f"session file exceeds the {max_total_bytes}-byte total safety limit"
                )
            while chunk := stream.read(chunk_size):
                total_bytes += len(chunk)
                if total_bytes > max_total_bytes:
                    raise JsonlError(
                        f"session file exceeds the {max_total_bytes}-byte total safety limit"
                    )
                digest.update(chunk)
    except JsonlError:
        raise
    except OSError as exc:
        raise JsonlError(f"cannot hash session file {path}: {exc.strerror or exc}") from exc
    return digest.hexdigest()


def file_snapshot(path: Path) -> FileSnapshot:
    try:
        current = path.stat()
    except OSError as exc:
        raise JsonlError(f"cannot stat session file {path}: {exc.strerror or exc}") from exc
    return FileSnapshot(
        device=current.st_dev,
        inode=current.st_ino,
        size=current.st_size,
        modified_ns=current.st_mtime_ns,
    )


def ensure_file_unchanged(path: Path, before: FileSnapshot) -> None:
    """Fail when a source changed while it was being detected, parsed, or hashed."""

    if file_snapshot(path) != before:
        raise JsonlError("source session changed while it was being read; retry")


def encode_jsonl(records: Iterable[Mapping[str, Any]]) -> bytes:
    """Encode native records deterministically while preserving Unicode."""

    lines = [json.dumps(record, ensure_ascii=False, separators=(",", ":")) for record in records]
    return (("\n".join(lines) + "\n") if lines else "").encode()


def write_private_atomic(path: Path, data: bytes) -> tuple[int, int]:
    """Write mode-0600 bytes atomically without silently replacing a session."""

    path = Path(os.path.abspath(path.expanduser()))
    if os.path.lexists(path):
        raise JsonlError(f"refusing to overwrite existing target: {path}")
    try:
        _mkdir_private(path.parent)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(temporary_name)
        published_identity: tuple[int, int] | None = None
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            # A hard link is an atomic create-if-absent operation. Unlike an
            # exists() check followed by replace(), it cannot clobber a file
            # created by another process during this write.
            try:
                os.link(temporary_path, path)
            except FileExistsError as exc:
                raise JsonlError(f"refusing to overwrite existing target: {path}") from exc
            temporary_path.unlink()
            target_stat = path.lstat()
            published_identity = (target_stat.st_dev, target_stat.st_ino)
            _fsync_directory(path.parent)
            return published_identity
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            if published_identity is not None:
                _unlink_if_same_file(path, published_identity)
            raise
    except JsonlError:
        raise
    except OSError as exc:
        raise JsonlError(f"cannot write target session {path}: {exc.strerror or exc}") from exc


def _mkdir_private(path: Path) -> None:
    missing: list[Path] = []
    cursor = path
    while not os.path.lexists(cursor):
        missing.append(cursor)
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            if not directory.is_dir():
                raise


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _unlink_if_same_file(path: Path, identity: tuple[int, int]) -> None:
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != identity:
        return
    path.unlink()
    _fsync_directory(path.parent)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")
