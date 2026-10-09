import os
from pathlib import Path

import pytest

from session_migrate.errors import JsonlError
from session_migrate.jsonl import (
    _fsync_directory,
    encode_jsonl,
    file_sha256,
    iter_jsonl,
    write_private_atomic,
)


def test_jsonl_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    encoded = encode_jsonl([{"type": "message", "text": "héllo"}])
    write_private_atomic(path, encoded)

    records = list(iter_jsonl(path))

    assert records[0].value["text"] == "héllo"
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_atomic_write_refuses_existing_target(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    write_private_atomic(path, b"{}\n")

    with pytest.raises(JsonlError, match="refusing to overwrite"):
        write_private_atomic(path, b'{"changed":true}\n')

    assert path.read_bytes() == b"{}\n"


def test_rejects_oversized_record_without_reading_to_newline(tmp_path: Path) -> None:
    path = tmp_path / "oversized.jsonl"
    path.write_bytes(b'{"value":"' + (b"x" * 128))

    with pytest.raises(JsonlError, match="safety limit"):
        list(iter_jsonl(path, max_record_bytes=32))


def test_rejects_oversized_total_file(tmp_path: Path) -> None:
    path = tmp_path / "too-large.jsonl"
    path.write_bytes(b"{}\n{}\n{}\n")

    with pytest.raises(JsonlError, match="total safety limit"):
        list(iter_jsonl(path, max_total_bytes=8))
    with pytest.raises(JsonlError, match="total safety limit"):
        file_sha256(path, max_total_bytes=8)


def test_rejects_excessive_record_count(tmp_path: Path) -> None:
    path = tmp_path / "too-many.jsonl"
    path.write_bytes(b"{}\n{}\n{}\n")

    with pytest.raises(JsonlError, match="record safety limit"):
        list(iter_jsonl(path, max_records=2))


def test_rejects_nonstandard_json_constants(tmp_path: Path) -> None:
    path = tmp_path / "nan.jsonl"
    path.write_text('{"value":NaN}\n')

    with pytest.raises(JsonlError, match="invalid JSON"):
        list(iter_jsonl(path))


def test_atomic_write_creates_private_directories(tmp_path: Path) -> None:
    first = tmp_path / "private" / "nested"
    path = first / "session.jsonl"

    write_private_atomic(path, b"{}\n")

    assert path.read_bytes() == b"{}\n"
    assert (tmp_path / "private").stat().st_mode & 0o777 == 0o700
    assert first.stat().st_mode & 0o777 == 0o700


def test_atomic_write_refuses_broken_symlink(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    path.symlink_to(tmp_path / "missing.jsonl")

    with pytest.raises(JsonlError, match="refusing to overwrite"):
        write_private_atomic(path, b"{}\n")

    assert path.is_symlink()


def test_atomic_write_does_not_clobber_racing_creator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "session.jsonl"
    original_link = os.link

    def racing_link(source: object, target: object) -> None:
        Path(target).write_bytes(b"racing winner")
        original_link(source, target)

    monkeypatch.setattr(os, "link", racing_link)

    with pytest.raises(JsonlError, match="refusing to overwrite"):
        write_private_atomic(path, b"migrator output\n")

    assert path.read_bytes() == b"racing winner"


def test_atomic_write_when_fchmod_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Windows and WASI have no os.fchmod; writes must still succeed there.
    monkeypatch.delattr(os, "fchmod", raising=False)
    path = tmp_path / "session.jsonl"

    write_private_atomic(path, b"{}\n")

    assert path.read_bytes() == b"{}\n"
    assert not list(tmp_path.glob(".*.tmp"))


def test_directory_fsync_is_skipped_on_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []

    def fail_open(*args: object, **kwargs: object) -> int:
        calls.append("open")
        raise AssertionError("directory os.open must not be called on nt")

    def fail_fsync(descriptor: int) -> None:
        calls.append("fsync")
        raise AssertionError("os.fsync must not be called on nt")

    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(os, "open", fail_open)
    monkeypatch.setattr(os, "fsync", fail_fsync)

    _fsync_directory(tmp_path)

    assert calls == []


def test_atomic_write_cleanup_does_not_mask_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "session.jsonl"

    def failing_fchmod(descriptor: int, mode: int) -> None:
        raise OSError("fchmod failed")

    def failing_unlink(target: object, **kwargs: object) -> None:
        raise OSError("cleanup unlink failed")

    monkeypatch.setattr(os, "fchmod", failing_fchmod)
    monkeypatch.setattr(os, "unlink", failing_unlink)

    with pytest.raises(JsonlError, match="fchmod failed"):
        write_private_atomic(path, b"{}\n")
