import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from native_corpus.loader import (
    ALLOWED_MODALITIES,
    EXPECTED_FORMATS,
    ArtifactSpec,
    CorpusValidationError,
    artifact_set_sha256,
    load_corpus,
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _update_json(path: Path, update: Callable[[dict[str, Any]], None]) -> None:
    value = json.loads(path.read_text())
    update(value)
    _write_json(path, value)


def _toy_corpus(root: Path) -> Path:
    _write_json(root / "scenario.json", {"schema_version": 1, "name": "temporary toy"})
    sources: list[dict[str, Any]] = []
    for index, format_name in enumerate(sorted(EXPECTED_FORMATS)):
        fixture_dir = PurePosixPath("sources") / format_name / "1.0" / "portable-rich"
        fixture_root = root / fixture_dir
        native_relative = PurePosixPath("native") / "session.jsonl"
        native_data = json.dumps({"format": format_name, "marker": index}).encode() + b"\n"
        native_path = fixture_root / native_relative
        native_path.parent.mkdir(parents=True)
        native_path.write_bytes(native_data)
        os.chmod(native_path, 0o600)

        expected_data = (
            json.dumps(
                {"kind": "message", "role": "user", "text": f"TOY_{format_name.upper()}"},
                sort_keys=True,
            ).encode()
            + b"\n"
        )
        expected_path = fixture_root / "expected-ir.jsonl"
        expected_path.write_bytes(expected_data)
        os.chmod(expected_path, 0o600)

        artifact = ArtifactSpec(
            path=native_relative,
            media_type="application/jsonl",
            size=len(native_data),
            sha256=_sha256(native_data),
            mode=0o600,
            role="transcript",
        )
        modalities = {
            name: {
                "attempted": False,
                "native_accepted": False,
                "fixture_present": False,
                "portable": "unsupported",
            }
            for name in sorted(ALLOWED_MODALITIES)
        }
        modalities["text"] = {
            "attempted": True,
            "native_accepted": True,
            "fixture_present": True,
            "portable": "preserve",
        }
        modalities["audio"] = {
            "attempted": True,
            "native_accepted": False,
            "fixture_present": False,
            "portable": "unsupported",
        }
        provenance = {
            "schema_version": 1,
            "fixture_id": f"{format_name}-1.0-portable-rich",
            "format": format_name,
            "case": "portable-rich",
            "native_session_id": f"native-{index:02d}-{format_name}",
            "native_title": f"Toy {format_name}",
            "native_cwd": "/fixture/work",
            "producer": {
                "name": format_name,
                "version": "1.0",
                "package_or_tag": "toy@1.0",
                "source_commit": "1" * 40,
                "platform": "linux-x86_64",
                "binary_sha256": "2" * 64,
                "binary_size": 123,
            },
            "capture": {
                "created_by_exact_cli": True,
                "capture_kind": "actual_cli_trajectory",
                "scenario_version": 1,
                "provider_kind": "loopback",
                "model_alias": "fixture-model",
                "captured_at": "2026-08-31T12:00:00Z",
                "raw_private_sha256": "3" * 64,
            },
            "artifacts": [
                {
                    "path": native_relative.as_posix(),
                    "media_type": artifact.media_type,
                    "size": artifact.size,
                    "sha256": artifact.sha256,
                    "mode": "0600",
                    "role": artifact.role,
                }
            ],
            "modalities": modalities,
            "sanitization": {
                "required": True,
                "sanitizer": f"scripts/native_corpus/sanitize_{format_name}.py",
                "sanitizer_git_sha": "4" * 40,
                "mutations": [{"selector": "/cwd", "category": "path", "count": 1}],
                "secret_scan": "pass",
                "content_review": "pass",
                "sanitized_artifact_set_sha256": artifact_set_sha256([artifact]),
                "reloaded_by_exact_cli": True,
                "reloaded_version": "1.0",
                "reloaded_binary_sha256": "2" * 64,
            },
            "expectations": {
                "ir": "expected-ir.jsonl",
                "ir_sha256": _sha256(expected_data),
                "event_counts": {"message": 1},
                "opaque_loss_reasons": {},
            },
        }
        _write_json(fixture_root / "provenance.json", provenance)
        sources.append(
            {"format": format_name, "fixture_dir": fixture_dir.as_posix(), "primary": True}
        )

    _write_json(
        root / "corpus.json",
        {
            "schema_version": 1,
            "scenario": "scenario.json",
            "required_formats": sorted(EXPECTED_FORMATS),
            "sources": sources,
        },
    )
    return root


def _provenance(root: Path, format_name: str) -> Path:
    return root / "sources" / format_name / "1.0" / "portable-rich" / "provenance.json"


def test_loads_exact_format_corpus_and_materializes_private_artifacts(tmp_path: Path) -> None:
    root = _toy_corpus(tmp_path / "corpus")

    corpus = load_corpus(root)

    assert corpus.formats() == EXPECTED_FORMATS
    assert len(corpus.fixtures) == len(EXPECTED_FORMATS) == 18
    claude = corpus.primary("claude")
    assert claude.provenance.capture.created_by_exact_cli is True
    assert claude.provenance.modalities["text"].fixture_present is True
    assert claude.expected_signature() == (
        {"kind": "message", "role": "user", "text": "TOY_CLAUDE"},
    )

    materialized = claude.materialize(tmp_path / "materialized")
    assert materialized.root.stat().st_mode & 0o777 == 0o700
    assert len(materialized.artifact_paths) == 1
    target = materialized.artifact_paths[0]
    assert target.relative_to(materialized.root).as_posix() == "native/session.jsonl"
    assert target.read_bytes() == claude.native_entrypoints[0].read_bytes()
    assert target.stat().st_mode & 0o777 == 0o600


def test_rejects_tampered_hash(tmp_path: Path) -> None:
    root = _toy_corpus(tmp_path / "corpus")
    artifact = root / "sources" / "claude" / "1.0" / "portable-rich/native/session.jsonl"
    original_mode = artifact.stat().st_mode & 0o777
    tampered = bytearray(artifact.read_bytes())
    tampered[-2] ^= 1
    artifact.write_bytes(tampered)
    os.chmod(artifact, original_mode)

    with pytest.raises(CorpusValidationError, match="SHA-256 mismatch"):
        load_corpus(root)


def test_rejects_declared_size_mismatch(tmp_path: Path) -> None:
    root = _toy_corpus(tmp_path / "corpus")
    path = _provenance(root, "codex")
    _update_json(path, lambda value: value["artifacts"][0].__setitem__("size", 999))

    with pytest.raises(CorpusValidationError, match="size mismatch"):
        load_corpus(root)


def test_rejects_executable_repository_artifact(tmp_path: Path) -> None:
    root = _toy_corpus(tmp_path / "corpus")
    artifact = root / "sources" / "pi" / "1.0" / "portable-rich/native/session.jsonl"
    os.chmod(artifact, 0o755)

    with pytest.raises(CorpusValidationError, match="repository artifact is executable"):
        load_corpus(root)


def test_rejects_unlisted_native_artifact(tmp_path: Path) -> None:
    root = _toy_corpus(tmp_path / "corpus")
    extra = root / "sources" / "vibe" / "1.0" / "portable-rich/native/unlisted.db"
    extra.write_bytes(b"not declared")
    os.chmod(extra, 0o600)

    with pytest.raises(CorpusValidationError, match="unlisted=.*unlisted.db"):
        load_corpus(root)


def test_rejects_missing_format_from_required_set(tmp_path: Path) -> None:
    root = _toy_corpus(tmp_path / "corpus")
    corpus_path = root / "corpus.json"

    def remove_vibe(value: dict[str, Any]) -> None:
        value["required_formats"].remove("vibe")

    _update_json(corpus_path, remove_vibe)
    with pytest.raises(CorpusValidationError, match="each supported format exactly once"):
        load_corpus(root)


def test_rejects_duplicate_primary_format(tmp_path: Path) -> None:
    root = _toy_corpus(tmp_path / "corpus")
    corpus_path = root / "corpus.json"

    def relabel_qwen_source(value: dict[str, Any]) -> None:
        source = next(source for source in value["sources"] if source["format"] == "qwen")
        source["format"] = "claude"

    _update_json(corpus_path, relabel_qwen_source)
    _update_json(_provenance(root, "qwen"), lambda value: value.__setitem__("format", "claude"))

    with pytest.raises(CorpusValidationError, match="exactly one primary fixture"):
        load_corpus(root)


def test_rejects_duplicate_fixture_and_native_session_ids(tmp_path: Path) -> None:
    root = _toy_corpus(tmp_path / "corpus")
    claude = json.loads(_provenance(root, "claude").read_text())
    qwen_path = _provenance(root, "qwen")
    _update_json(
        qwen_path,
        lambda value: value.__setitem__("fixture_id", claude["fixture_id"]),
    )
    with pytest.raises(CorpusValidationError, match="fixture IDs must be unique"):
        load_corpus(root)

    root = _toy_corpus(tmp_path / "second-corpus")
    claude = json.loads(_provenance(root, "claude").read_text())
    _update_json(
        _provenance(root, "qwen"),
        lambda value: value.__setitem__("native_session_id", claude["native_session_id"]),
    )
    with pytest.raises(CorpusValidationError, match="native session IDs must be unique"):
        load_corpus(root)


def test_accepts_video_and_rejects_unknown_or_internally_inconsistent_modality(
    tmp_path: Path,
) -> None:
    root = _toy_corpus(tmp_path / "corpus")
    path = _provenance(root, "openhands")

    def add_video(value: dict[str, Any]) -> None:
        value["modalities"]["video"] = {
            "attempted": True,
            "native_accepted": True,
            "fixture_present": True,
            "portable": "preserve",
        }

    _update_json(path, add_video)
    corpus = load_corpus(root)
    assert corpus.primary("openhands").provenance.modalities["video"].fixture_present is True

    root = _toy_corpus(tmp_path / "second-corpus")
    path = _provenance(root, "openhands")

    def add_hologram(value: dict[str, Any]) -> None:
        value["modalities"]["hologram"] = {
            "attempted": True,
            "native_accepted": True,
            "fixture_present": True,
            "portable": "preserve",
        }

    _update_json(path, add_hologram)
    with pytest.raises(CorpusValidationError, match="unsupported modalities.*hologram"):
        load_corpus(root)

    root = _toy_corpus(tmp_path / "third-corpus")
    path = _provenance(root, "openhands")
    _update_json(
        path,
        lambda value: value["modalities"]["audio"].__setitem__("fixture_present", True),
    )
    with pytest.raises(CorpusValidationError, match="fixture presence requires native acceptance"):
        load_corpus(root)


def test_rejects_a_fixture_with_an_unreported_modality(tmp_path: Path) -> None:
    root = _toy_corpus(tmp_path / "corpus")
    path = _provenance(root, "claude")

    def omit_video(value: dict[str, Any]) -> None:
        del value["modalities"]["video"]

    _update_json(path, omit_video)
    with pytest.raises(
        CorpusValidationError,
        match=r"every modality must have an explicit status; missing=\['video'\]",
    ):
        load_corpus(root)


def test_rejects_unknown_metadata_field_and_path_traversal(tmp_path: Path) -> None:
    root = _toy_corpus(tmp_path / "corpus")
    path = _provenance(root, "hermes")
    _update_json(path, lambda value: value.__setitem__("unreviewed", True))
    with pytest.raises(CorpusValidationError, match="unknown=.*unreviewed"):
        load_corpus(root)

    root = _toy_corpus(tmp_path / "second-corpus")
    corpus_path = root / "corpus.json"

    def escape_fixture(value: dict[str, Any]) -> None:
        value["sources"][0]["fixture_dir"] = "../outside"

    _update_json(corpus_path, escape_fixture)
    with pytest.raises(CorpusValidationError, match="normalized relative POSIX path"):
        load_corpus(root)


def test_committed_schema_documents_are_valid_json() -> None:
    schema_root = Path(__file__).parent / "native_corpus" / "schema"
    corpus_schema = json.loads((schema_root / "corpus.schema.json").read_text())
    provenance_schema = json.loads((schema_root / "provenance.schema.json").read_text())

    assert corpus_schema["$schema"].endswith("2020-12/schema")
    assert provenance_schema["$schema"].endswith("2020-12/schema")
    assert set(corpus_schema["$defs"]["format"]["enum"]) == EXPECTED_FORMATS
    assert set(provenance_schema["$defs"]["format"]["enum"]) == EXPECTED_FORMATS
