from __future__ import annotations

from pathlib import Path

import pytest
from native_corpus.loader import load_standalone_fixture
from native_corpus.route_oracle import assert_source_expectations, parse_native_fixture

SOURCES = Path(__file__).parent / "native_corpus/v1/sources"
PROVENANCE_FILES = tuple(sorted(SOURCES.glob("*/*/*/provenance.json")))


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
