from __future__ import annotations

from itertools import product
from pathlib import Path

import pytest
from native_corpus.loader import EXPECTED_FORMATS, load_standalone_fixture
from native_corpus.route_oracle import (
    assert_artifact_warning_contract,
    assert_modality_loss_contract,
    assert_source_expectations,
    assert_tool_linkage,
    expected_loss_counters,
    expected_semantic_signature,
    materialize_and_reparse_target,
    parse_native_fixture,
)

from session_migrate.conversion import ConversionOptions, convert_session
from session_migrate.model import TargetFormat

SOURCES = Path(__file__).parent / "native_corpus/v1/sources"
PROVENANCE_FILES = tuple(sorted(SOURCES.glob("*/*/*/provenance.json")))
PROMOTED_ROUTE_CASES = tuple(product(PROVENANCE_FILES, sorted(EXPECTED_FORMATS)))
TARGET_UUID = "acacacac-acac-4cac-8cac-acacacacacac"


def test_partial_native_corpus_contains_at_least_one_promoted_source() -> None:
    assert PROVENANCE_FILES


@pytest.mark.parametrize("provenance_path", PROVENANCE_FILES, ids=lambda path: path.parts[-4])
def test_promoted_native_source_matches_reviewed_ir(
    provenance_path: Path, tmp_path: Path
) -> None:
    fixture = load_standalone_fixture(provenance_path.parent)

    assert provenance_path.parts[-4] == fixture.format
    assert provenance_path.parts[-2] == fixture.provenance.case
    session = parse_native_fixture(fixture, tmp_path / fixture.format)
    assert_source_expectations(fixture, session)


@pytest.mark.parametrize(
    ("provenance_path", "target_name"),
    PROMOTED_ROUTE_CASES,
    ids=lambda value: value.parts[-4] if isinstance(value, Path) else str(value),
)
def test_each_promoted_native_source_converts_to_every_target(
    provenance_path: Path, target_name: str, tmp_path: Path
) -> None:
    """Run the strict route oracle before the complete 18-source manifest lands."""

    fixture = load_standalone_fixture(provenance_path.parent)
    source = parse_native_fixture(fixture, tmp_path / "source")
    assert_source_expectations(fixture, source)
    assert_tool_linkage(source.events)
    artifact = convert_session(
        source,
        ConversionOptions(
            target_format=TargetFormat(target_name),
            session_id=TARGET_UUID,
            cwd=tmp_path,
        ),
    )

    assert artifact.dropped == expected_loss_counters(source, target_name)
    assert_modality_loss_contract(source, target_name, artifact.dropped)
    assert_artifact_warning_contract(artifact, same_format=fixture.format == target_name)
    observation = materialize_and_reparse_target(artifact, tmp_path / "target")
    assert observation.semantics == expected_semantic_signature(
        fixture.expected_signature(), target_name
    )
    assert observation.source_format == target_name
    assert observation.session_id == artifact.session_id
    assert observation.native_record_count == artifact.native_record_count
    assert artifact.native_record_count > 0
