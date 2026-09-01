"""Focused tests for the exact Devin native-corpus sanitizer."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest


def _load() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts/native-corpus/sanitize-devin.py"
    spec = importlib.util.spec_from_file_location("sanitize_devin", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_devin_sanitizer_recursive_replacement_is_schema_preserving() -> None:
    sanitizer = _load()
    counts = {"capture_paths": 0}
    value = {
        "content": "/private/capture/work/timeline.py",
        "nested": ["/private/capture/tmp/value", {"safe": True}],
    }
    result = sanitizer._replace(
        value,
        {
            "/private/capture/work": sanitizer.PUBLIC_CWD,
            "/private/capture": sanitizer.PUBLIC_ROOT,
        },
        counts,
    )
    assert result == {
        "content": "/fixture/work/timeline.py",
        "nested": ["/fixture/tmp/value", {"safe": True}],
    }
    assert counts == {"capture_paths": 2}


def test_devin_sanitizer_active_chain_ignores_inactive_forest(tmp_path: Path) -> None:
    sanitizer = _load()
    database = tmp_path / "forest.db"
    import sqlite3

    with sqlite3.connect(database) as db:
        db.execute(
            "CREATE TABLE message_nodes(session_id TEXT, node_id INTEGER, parent_node_id INTEGER)"
        )
        db.executemany(
            "INSERT INTO message_nodes VALUES('session', ?, ?)",
            [(0, None), (1, 0), (2, 1), (7, 0)],
        )
        assert sanitizer._active_node_ids(db, "session", 2) == [0, 1, 2]


def test_devin_sanitizer_rejects_malformed_json() -> None:
    sanitizer = _load()
    with pytest.raises(RuntimeError, match="not valid JSON"):
        sanitizer._json("{", "native field")
    assert sanitizer._json(json.dumps({"safe": True}), "native field") == {"safe": True}
