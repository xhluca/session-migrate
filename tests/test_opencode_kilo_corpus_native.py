"""Native-corpus gates for OpenCode 1.17.20 and Kilo Code 7.5.0."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from native_corpus.loader import NativeFixture, load_standalone_fixture
from native_corpus.route_oracle import (
    assert_source_expectations,
    normalize_source_session,
    parse_native_fixture,
)

from session_migrate.formats import kilo, opencode
from session_migrate.model import EventKind

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "tests/native_corpus/v1/sources"
ASSETS = ROOT / "tests/native_corpus/v1/assets"
CAPTURE_SCRIPT = ROOT / "scripts/native-corpus/capture-opencode-kilo.py"
SANITIZER_SCRIPT = ROOT / "scripts/native-corpus/sanitize-opencode-kilo.py"
COLD_PROMPT = "COLD_RELOAD_VERIFY_8421: confirm the earlier tool and media context remains."
FORMAT_CASES = {
    "opencode": {
        "adapter": opencode,
        "version": "1.17.20",
        "variable": "SESSION_MIGRATE_OPENCODE_BIN",
    },
    "kilo": {
        "adapter": kilo,
        "version": "7.5.0",
        "variable": "SESSION_MIGRATE_KILO_BIN",
    },
}


def _load_script(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fixture(format_name: str) -> NativeFixture:
    version = str(FORMAT_CASES[format_name]["version"])
    return load_standalone_fixture(SOURCES / format_name / version / "portable-rich")


def _exact_binary(format_name: str, capture: ModuleType) -> Path:
    variable = str(FORMAT_CASES[format_name]["variable"])
    value = os.environ.get(variable)
    if not value:
        pytest.skip(f"set {variable} to the exact pinned native binary")
    binary = Path(value)
    capture.verify_binary(format_name, binary)
    return binary


def _configuration(port: int) -> dict[str, Any]:
    return {
        "model": "fixture/fixture-model",
        "provider": {
            "fixture": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "session-migrate loopback",
                "options": {
                    "baseURL": f"http://127.0.0.1:{port}/v1",
                    "apiKey": "synthetic-not-a-secret",
                },
                "models": {
                    "fixture-model": {
                        "name": "Synthetic fixture model",
                        "attachment": True,
                        "tool_call": True,
                        "limit": {"context": 64_000, "output": 4_096},
                        "modalities": {
                            "input": ["text", "image"],
                            "output": ["text"],
                        },
                    }
                },
            }
        },
    }


def _export(
    binary: Path,
    session_id: str,
    work: Path,
    environment: dict[str, str],
    path: Path,
) -> None:
    # Bun/OpenCode truncates this >64 KiB export at exactly 64 KiB when stdout
    # is a subprocess pipe.  A direct regular-file stdout is the public CLI's
    # complete export and is independently parsed below.
    with path.open("wb") as output:
        completed = subprocess.run(
            [str(binary), "export", session_id, "--pure"],
            cwd=work,
            env=environment,
            check=False,
            stdout=output,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")


def _native_file_parts(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    parts = [part for message in document["messages"] for part in message["parts"]]
    return {str(part["mime"]): part for part in parts if part.get("type") == "file"}


@pytest.mark.parametrize("format_name", tuple(FORMAT_CASES))
def test_public_fixture_is_exact_native_media_and_tool_trajectory(
    format_name: str, tmp_path: Path
) -> None:
    fixture = _fixture(format_name)
    session = parse_native_fixture(fixture, tmp_path / format_name)
    assert_source_expectations(fixture, session)

    assert session.cwd == Path("/fixture/work")
    assert session.title == "repair-event-window-boundary"
    assert session.cli_version == FORMAT_CASES[format_name]["version"]
    replay = json.dumps(
        [
            {
                "kind": event.kind.value,
                "text": event.text,
                "tool": event.tool_name,
                "payload": event.payload,
            }
            for event in session.events
        ],
        sort_keys=True,
    )
    for marker in (
        "SM_CORPUS_7319",
        "COPPER_4821",
        "BLUE_TRIANGLE_7319",
        "ORBIT_2048",
        "missing-corpus-file.txt",
        "SM_NATIVE_FAILURE_7319",
    ):
        assert marker in replay
    assert sum(event.kind == EventKind.TOOL_CALL for event in session.events) == 3
    results = [event for event in session.events if event.kind == EventKind.TOOL_RESULT]
    assert len(results) == 3
    assert sum(event.payload.get("is_error") is True for event in results) == 1

    export = json.loads((fixture.root / "native/export.json").read_text())
    file_parts = _native_file_parts(export)
    assert set(file_parts) == {"application/pdf", "image/png"}
    for mime, asset in (
        ("image/png", "corpus-card.png"),
        ("application/pdf", "corpus-document.pdf"),
    ):
        prefix, encoded = str(file_parts[mime]["url"]).split(",", 1)
        assert prefix == f"data:{mime};base64"
        assert (
            hashlib.sha256(base64.b64decode(encoded)).hexdigest()
            == hashlib.sha256((ASSETS / asset).read_bytes()).hexdigest()
        )
    native_replay = json.dumps(export, sort_keys=True)
    for name in ("corpus-tone.wav", "corpus-transition.mp4"):
        assert name in native_replay
    assert native_replay.count("Cannot read binary file") >= 2

    modalities = fixture.provenance.modalities
    assert modalities["user_image"].fixture_present is True
    assert modalities["document"].portable == "lossy"
    for name in ("audio", "video"):
        assert modalities[name].attempted is True
        assert modalities[name].native_accepted is False
        assert modalities[name].fixture_present is False


@pytest.mark.parametrize("format_name", tuple(FORMAT_CASES))
def test_exact_client_captures_from_empty_through_public_surfaces(
    format_name: str, tmp_path: Path
) -> None:
    capture = _load_script(CAPTURE_SCRIPT, f"capture_{format_name}_native")
    sanitizer = _load_script(SANITIZER_SCRIPT, f"sanitize_{format_name}_native")
    binary = _exact_binary(format_name, capture)
    fixture = _fixture(format_name)
    raw_dir = tmp_path / "raw"

    capture.capture(format_name, binary, raw_dir)
    report = json.loads((raw_dir / "capture-report.json").read_text())
    assert report["requests"] == 6
    assert report["media"] == {
        "application/pdf": {"accepted": True},
        "audio/wav": {
            "accepted": False,
            "error": "Cannot read binary file",
            "observed": True,
        },
        "image/png": {"accepted": True},
        "video/mp4": {
            "accepted": False,
            "error": "Cannot read binary file",
            "observed": True,
        },
    }
    turns = {item["id"]: item for item in report["turns"]}
    assert turns["inspect"]["returncode"] == 0
    assert turns["media"]["returncode"] == 1
    assert turns["failure"]["returncode"] == 0
    assert turns["recall"]["returncode"] == 0

    private = json.loads((raw_dir / "export.json").read_text())
    sanitized = sanitizer.sanitize_document(
        private,
        format_name=format_name,
        source_cwd=Path(report["source_cwd"]),
    )
    assert sanitized.mutations == {
        "message metadata path": 6,
        "message part path": 18,
        "session directory": 1,
        "session path": 1,
    }
    public = tmp_path / "sanitized-export.json"
    public.write_text(json.dumps(sanitized.document, indent=2) + "\n")
    adapter = FORMAT_CASES[format_name]["adapter"]
    adapter.validate_native_bytes(public.read_bytes(), private["info"]["id"])
    regenerated = adapter.parse_session(public)
    assert_source_expectations(fixture, regenerated)


@pytest.mark.parametrize("format_name", tuple(FORMAT_CASES))
def test_exact_client_cold_import_export_and_continuation_preserve_prefix(
    format_name: str, tmp_path: Path
) -> None:
    capture = _load_script(CAPTURE_SCRIPT, f"reload_{format_name}_native")
    binary = _exact_binary(format_name, capture)
    fixture = _fixture(format_name)
    native = fixture.root / "native/export.json"
    original = json.loads(native.read_text())
    work = tmp_path / "work"
    work.mkdir()
    for name in ("timeline.py", "CORPUS_NOTE.txt"):
        shutil.copy2(ASSETS / name, work / name)

    with capture.loopback(work) as (port, handler):
        config_name = str(capture.EXPECTED[format_name]["config"])
        environment = capture.isolated_env(tmp_path, config_name, _configuration(port))
        imported = subprocess.run(
            [str(binary), "import", str(native), "--pure"],
            cwd=work,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert imported.returncode == 0, imported.stderr
        session_id = fixture.provenance.native_session_id
        assert session_id in imported.stdout

        immediate = tmp_path / "immediate-export.json"
        _export(binary, session_id, work, environment, immediate)
        adapter = FORMAT_CASES[format_name]["adapter"]
        immediate_session = adapter.parse_session(immediate)
        assert normalize_source_session(immediate_session) == fixture.expected_signature()

        # The exact client intentionally rebases imported cwd metadata and
        # strips its transient `synthetic` marker from accepted file parts.
        # Neither normalization changes the parsed portable transcript.
        immediate_value = json.loads(immediate.read_text())
        assert immediate_value["info"]["directory"] == str(work)
        assert all(part.get("synthetic") is True for part in _native_file_parts(original).values())
        assert all("synthetic" not in part for part in _native_file_parts(immediate_value).values())

        evidence = capture.run_turn(
            binary,
            work,
            environment,
            session_id,
            {"id": "cold-reload", "text": COLD_PROMPT},
        )
        assert evidence.returncode == 0, evidence.stderr
        after = tmp_path / "continued-export.json"
        _export(binary, session_id, work, environment, after)
        continued = normalize_source_session(adapter.parse_session(after))
        expected = fixture.expected_signature()
        assert len(continued) > len(expected)
        assert continued[: len(expected)] == expected
        replay = json.dumps(handler.requests[-1], sort_keys=True)
        for marker in (
            "SM_CORPUS_7319",
            "COPPER_4821",
            "BLUE_TRIANGLE_7319",
            "ORBIT_2048",
            COLD_PROMPT,
        ):
            assert marker in replay
