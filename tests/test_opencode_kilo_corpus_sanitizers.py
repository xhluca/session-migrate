from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from session_migrate.formats import kilo, opencode

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/native-corpus/sanitize-opencode-kilo.py"
SOURCES = ROOT / "tests/native_corpus/v1/sources"


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sanitize_opencode_kilo", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def fixture_path(format_name: str) -> Path:
    version = "1.17.20" if format_name == "opencode" else "7.5.0"
    return SOURCES / format_name / version / "portable-rich/native/export.json"


def private_copy(value: object, private_cwd: str) -> object:
    if isinstance(value, str):
        return value.replace("/fixture/work", private_cwd).replace(
            "fixture/work", private_cwd.lstrip("/")
        )
    if isinstance(value, list):
        return [private_copy(item, private_cwd) for item in value]
    if isinstance(value, dict):
        return {key: private_copy(item, private_cwd) for key, item in value.items()}
    return value


@pytest.mark.parametrize(
    ("format_name", "adapter"),
    [("opencode", opencode), ("kilo", kilo)],
)
def test_sanitizer_round_trips_a_schema_valid_private_export(
    tmp_path: Path,
    format_name: str,
    adapter: Any,
) -> None:
    sanitizer = load_script()
    public = json.loads(fixture_path(format_name).read_text())
    private_cwd = str(tmp_path / "private-capture/work")
    private = private_copy(public, private_cwd)

    result = sanitizer.sanitize_document(
        private,
        format_name=format_name,
        source_cwd=Path(private_cwd),
    )

    assert result.document == public
    assert result.mutations == {
        "message metadata path": 6,
        "message part path": 18,
        "session directory": 1,
        "session path": 1,
    }
    data = (json.dumps(result.document, indent=2) + "\n").encode()
    adapter.validate_native_bytes(data, public["info"]["id"])


@pytest.mark.parametrize("format_name", ["opencode", "kilo"])
def test_sanitizer_is_idempotent_and_preserves_native_media_evidence(
    format_name: str,
) -> None:
    sanitizer = load_script()
    document = json.loads(fixture_path(format_name).read_text())

    result = sanitizer.sanitize_document(
        document,
        format_name=format_name,
        source_cwd=Path("/capture/that/is/not/present"),
    )

    assert result.document == document
    assert result.mutations == {}
    parts = [part for message in document["messages"] for part in message["parts"]]
    assert {(part.get("type"), part.get("mime")) for part in parts} >= {
        ("file", "image/png"),
        ("file", "application/pdf"),
    }
    assert not any(
        part.get("type") == "file" and part.get("mime") in {"audio/wav", "video/mp4"}
        for part in parts
    )
    replay = json.dumps(parts)
    assert "corpus-tone.wav" in replay and "Cannot read binary file" in replay
    assert "corpus-transition.mp4" in replay and "Cannot read binary file" in replay


@pytest.mark.parametrize("format_name", ["opencode", "kilo"])
def test_sanitizer_rejects_unknown_native_part_schema(format_name: str) -> None:
    sanitizer = load_script()
    document = json.loads(fixture_path(format_name).read_text())
    malformed = copy.deepcopy(document)
    malformed["messages"][0]["parts"][0]["type"] = "hologram"

    with pytest.raises(sanitizer.SanitizationError, match="type is unsupported: hologram"):
        sanitizer.sanitize_document(
            malformed,
            format_name=format_name,
            source_cwd=Path("/private/capture/work"),
        )


@pytest.mark.parametrize("format_name", ["opencode", "kilo"])
def test_sanitizer_rejects_cross_session_part_linkage(format_name: str) -> None:
    sanitizer = load_script()
    document = json.loads(fixture_path(format_name).read_text())
    malformed = copy.deepcopy(document)
    malformed["messages"][0]["parts"][0]["sessionID"] = "ses_wrong"

    with pytest.raises(sanitizer.SanitizationError, match="sessionID does not match"):
        sanitizer.sanitize_document(
            malformed,
            format_name=format_name,
            source_cwd=Path("/private/capture/work"),
        )
