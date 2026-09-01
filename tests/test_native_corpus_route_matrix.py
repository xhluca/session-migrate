from __future__ import annotations

from dataclasses import replace
from functools import lru_cache
from itertools import product
from pathlib import Path

import pytest
from native_corpus.loader import EXPECTED_FORMATS, NativeCorpus, load_corpus
from native_corpus.route_oracle import (
    TARGET_CONTRACTS,
    assert_artifact_warning_contract,
    assert_modality_loss_contract,
    assert_source_expectations,
    assert_tool_linkage,
    expected_loss_counters,
    expected_semantic_signature,
    materialize_and_reparse_target,
    normalize_source_session,
    observed_modality_counts,
    parse_native_fixture,
)

from session_migrate.conversion import ConversionOptions, convert_session
from session_migrate.formats import claude
from session_migrate.model import Event, EventKind, Provenance, Role, TargetFormat

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


def test_target_contracts_cannot_declare_a_silent_modality_drop() -> None:
    fields = (
        "user_image",
        "tool_call",
        "tool_result",
        "tool_result_image",
        "compaction",
        "reasoning",
    )
    for target, contract in TARGET_CONTRACTS.items():
        for field in fields:
            rule = getattr(contract, field)
            assert rule.preserve or rule.loss_keys, f"{target}: {field} silently drops"
            assert len(rule.loss_keys) == len(set(rule.loss_keys)), (
                f"{target}: {field} repeats a loss key"
            )


def test_modality_counter_observes_structured_source_evidence() -> None:
    source = claude.parse(Path(__file__).parent / "fixtures" / "claude-2.1.209" / "basic.jsonl")

    assert observed_modality_counts(source) == {
        "compaction": 1,
        "text": 5,
        "tool_call": 1,
        "tool_result": 1,
        "tool_result_image": 1,
        "user_image": 1,
    }


def test_tool_result_normalization_equates_absent_and_empty_text_only() -> None:
    source = claude.parse(Path(__file__).parent / "fixtures" / "claude-2.1.209" / "basic.jsonl")
    result = Event(
        EventKind.TOOL_RESULT,
        Provenance(0, "tool"),
        role=Role.TOOL,
        tool_name="view_image",
        tool_call_id="call-image-only",
        payload={
            "is_error": False,
            "content_blocks": [
                {"type": "text", "text": ""},
                {
                    "type": "image",
                    "image_url": "data:image/png;base64,aW1hZ2Utb25seQ==",
                },
            ],
        },
    )

    normalized = normalize_source_session(replace(source, events=(result,)))[0]

    assert normalized["text"] == ""
    assert normalized["content_blocks"] == [
        {
            "type": "image",
            "media": {
                "transport": "data",
                "media_type": "image/png",
                "sha256": "7e3094cefe74c5c212e9c8bbbf6e8654a25437b8600739379adb5d1fcb9411d4",
            },
        }
    ]


def test_copilot_oracle_accounts_for_grouping_and_timestamp_repair() -> None:
    source = claude.parse(Path(__file__).parent / "fixtures" / "claude-2.1.209" / "basic.jsonl")
    source = replace(
        source,
        started_at="2026-08-31T12:00:00.500000Z",
        title=None,
        events=(
            Event(
                EventKind.MESSAGE,
                Provenance(0, "user", block_index=0),
                role=Role.USER,
                text="first block",
                timestamp="2026-08-31T12:00:00Z",
            ),
            Event(
                EventKind.MESSAGE,
                Provenance(0, "user", block_index=1),
                role=Role.USER,
                text="second block",
                timestamp="2026-08-31T12:00:00Z",
            ),
            Event(
                EventKind.MESSAGE,
                Provenance(1, "assistant"),
                role=Role.ASSISTANT,
                text="reply",
                timestamp="2026-08-31T12:00:00Z",
            ),
        ),
    )

    assert expected_loss_counters(source, "copilot") == {
        "message:native_text_blocks_grouped": 1,
        "timestamp:native_order_adjusted": 2,
    }


def test_grouping_targets_keep_one_message_around_native_image_attachment() -> None:
    events = (
        {"kind": "message", "role": "user", "text": "before"},
        {
            "kind": "user_image",
            "role": "user",
            "media": {"transport": "data", "media_type": "image/png", "sha256": "0" * 64},
        },
        {"kind": "message", "role": "user", "text": "after"},
    )

    assert expected_semantic_signature(events, "vibe") == (
        {"kind": "message", "role": "user", "text": "before\nafter"},
        {
            "kind": "user_image",
            "media": {"transport": "data", "media_type": "image/png", "sha256": "0" * 64},
        },
    )


def test_embedded_result_targets_pair_parallel_calls_without_dropping_events() -> None:
    events = (
        {"kind": "tool_call", "tool_call_id": "call-a", "tool_name": "read", "input": {}},
        {"kind": "tool_call", "tool_call_id": "call-b", "tool_name": "read", "input": {}},
        {
            "kind": "tool_result",
            "tool_call_id": "call-a",
            "text": "a",
            "is_error": False,
            "content_blocks": [],
        },
        {
            "kind": "tool_result",
            "tool_call_id": "call-b",
            "text": "b",
            "is_error": False,
            "content_blocks": [],
        },
    )

    paired = expected_semantic_signature(events, "opencode")
    assert [(event["kind"], event["tool_call_id"]) for event in paired] == [
        ("tool_call", "call-a"),
        ("tool_result", "call-a"),
        ("tool_call", "call-b"),
        ("tool_result", "call-b"),
    ]
    assert len(paired) == len(events)


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

    observation = materialize_and_reparse_target(artifact, tmp_path / "target")

    assert observation.source_format == target_name
    assert observation.session_id == artifact.session_id
    assert observation.native_record_count == artifact.native_record_count
    assert observation.native_record_count > 0
    assert observation.semantics
    assert observation.semantics[0] == {
        "kind": "message",
        "role": "user",
        "text": "Remember synthetic migrator nonce ALPHA-1042.",
    }
    assert_artifact_warning_contract(artifact, same_format=target_name == "claude")


def test_warning_oracle_fails_when_a_loss_warning_is_missing(tmp_path: Path) -> None:
    source = claude.parse(Path(__file__).parent / "fixtures" / "claude-2.1.209" / "basic.jsonl")
    artifact = convert_session(
        source,
        ConversionOptions(
            target_format=TargetFormat.ANTIGRAVITY,
            session_id=TARGET_UUID,
            cwd=tmp_path,
        ),
    )
    warnings = tuple(
        warning
        for warning in artifact.warnings
        if not (
            warning.get("code") == "dropped_event_kind"
            and warning.get("event_kind") == next(iter(artifact.dropped))
        )
    )

    with pytest.raises(AssertionError, match="differ from artifact loss ledger"):
        assert_artifact_warning_contract(replace(artifact, warnings=warnings), same_format=False)


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
    assert_modality_loss_contract(source, target_name, artifact.dropped)
    assert_artifact_warning_contract(artifact, same_format=source_name == target_name)
    expected_semantics = expected_semantic_signature(fixture.expected_signature(), target_name)
    observation = materialize_and_reparse_target(artifact, tmp_path / "target")
    assert observation.semantics == expected_semantics
    assert observation.source_format == target_name
    assert observation.session_id == artifact.session_id
    assert observation.native_record_count == artifact.native_record_count
    assert artifact.target_format.value == target_name
    assert artifact.native_record_count > 0
