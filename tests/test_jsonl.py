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

