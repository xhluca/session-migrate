import os
from pathlib import Path

import pytest

from session_bridge.errors import JsonlError
from session_bridge.jsonl import encode_jsonl, iter_jsonl, write_private_atomic


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
        write_private_atomic(path, b"bridge output\n")

    assert path.read_bytes() == b"racing winner"
