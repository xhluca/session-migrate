from __future__ import annotations

from functools import lru_cache
from itertools import product
from pathlib import Path

import pytest
from native_corpus.loader import EXPECTED_FORMATS, NativeCorpus, load_corpus
from native_corpus.route_oracle import (
    TARGET_CONTRACTS,
    assert_source_expectations,
    assert_tool_linkage,
    expected_loss_counters,
    expected_semantic_signature,
    materialize_and_reparse_target,
    parse_native_fixture,
)

from session_migrate.conversion import ConversionOptions, convert_session
from session_migrate.formats import claude
from session_migrate.model import TargetFormat

CORPUS_ROOT = Path(__file__).parent / "native_corpus" / "v1"
FORMAT_NAMES = tuple(sorted(EXPECTED_FORMATS))
ROUTE_CASES = tuple(product(FORMAT_NAMES, FORMAT_NAMES))
TARGET_UUID = "abababab-abab-4bab-8bab-abababababab"


@lru_cache(maxsize=1)
def _load_real_corpus() -> NativeCorpus:
    marker = CORPUS_ROOT / "corpus.json"
    if not marker.is_file():
        pytest.skip("native-produced v1 corpus has not landed yet")
    # A present corpus never skips: malformed metadata, missing fixtures, hash
    # drift, or an incomplete format set must fail loudly in load_corpus.
    return load_corpus(CORPUS_ROOT)


def test_native_corpus_route_cases_are_the_exact_cartesian_product() -> None:
    expected = {(source, target) for source in EXPECTED_FORMATS for target in EXPECTED_FORMATS}

    assert len(EXPECTED_FORMATS) == 18
    assert len(ROUTE_CASES) == 324
    assert len(set(ROUTE_CASES)) == 324
    assert set(ROUTE_CASES) == expected
    assert set(TARGET_CONTRACTS) == EXPECTED_FORMATS
    assert all(TargetFormat(target).value == target for _, target in ROUTE_CASES)


@pytest.mark.parametrize("target_name", FORMAT_NAMES)
def test_target_materialization_helper_covers_every_format(
    target_name: str, tmp_path: Path
) -> None:
    source = claude.parse(Path(__file__).parent / "fixtures" / "claude-2.1.209" / "basic.jsonl")
    artifact = convert_session(
        source,
        ConversionOptions(
            target_format=TargetFormat(target_name),
            session_id=TARGET_UUID,
            cwd=tmp_path,
        ),
    )

    semantics = materialize_and_reparse_target(artifact, tmp_path / "target")

    assert semantics
    assert semantics[0] == {
        "kind": "message",
        "role": "user",
        "text": "Remember synthetic migrator nonce ALPHA-1042.",
    }


@pytest.mark.parametrize(
    ("source_name", "target_name"),
    ROUTE_CASES,
    ids=[f"{source}-to-{target}" for source, target in ROUTE_CASES],
)
def test_native_produced_source_to_every_target_route(
    source_name: str,
    target_name: str,
    tmp_path: Path,
) -> None:
    corpus = _load_real_corpus()
    fixture = corpus.primary(source_name)
    source = parse_native_fixture(fixture, tmp_path / "source")
    assert source.source_format.value == source_name
    assert source.session_id == fixture.provenance.native_session_id
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

    expected_losses = expected_loss_counters(source, target_name)
    assert artifact.dropped == expected_losses
    expected_semantics = expected_semantic_signature(fixture.expected_signature(), target_name)
    actual_semantics = materialize_and_reparse_target(artifact, tmp_path / "target")
    assert actual_semantics == expected_semantics
    assert artifact.target_format.value == target_name
    assert artifact.native_record_count > 0
    if source_name == target_name:
        assert any(
            warning.get("code") == "same_format_portable_rewrite" for warning in artifact.warnings
        )
