"""Independent test-only route oracle for native-captured corpus fixtures.

This module deliberately keeps the reviewed target contract outside production
adapters.  A writer and its reader must not be able to weaken the expected
portable projection or loss accounting together.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from native_corpus.loader import CorpusValidationError, NativeFixture
from session_migrate.conversion import ConversionArtifact
from session_migrate.formats import (
    antigravity,
    claude,
    codex,
    copilot,
    cursor,
    devin,
    grok,
    hermes,
    kilo,
    kimi,
    mastracode,
    muse,
    omp,
    opencode,
    openhands,
    pi,
    qwen,
    vibe,
)
from session_migrate.model import AgentFormat, Event, EventKind, Role, Session


@dataclass(frozen=True, slots=True)
class CapabilityRule:
    """One reviewed target mapping, independent from writer constants."""

    preserve: bool
    loss_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TargetContract:
    user_image: CapabilityRule
    tool_call: CapabilityRule
    tool_result: CapabilityRule
    tool_result_image: CapabilityRule
    compaction: CapabilityRule
    reasoning: CapabilityRule
    result_content_blocks: bool = True
    group_adjacent_messages: bool = False
    opaque_style: str = "reason_prefixed"
    system_loss: str = "message:privileged_role"
    other_context_style: str = "typed"
    preserve_non_image_context: bool = False
    tool_result_error_loss: str | None = None


@dataclass(frozen=True, slots=True)
class TargetObservation:
    """Independently parsed identity, count, and portable target semantics."""

    semantics: tuple[Mapping[str, Any], ...]
    source_format: str
    session_id: str
    native_record_count: int


KEEP = CapabilityRule(True)


# Reviewed compatibility contract.  These literals must change only with a
# documented compatibility decision and matching native evidence.  They do not
# import writer capability sets or loss-key constants from production code.
TARGET_CONTRACTS: Mapping[str, TargetContract] = {
    "claude": TargetContract(
        KEEP,
        KEEP,
        KEEP,
        KEEP,
        CapabilityRule(False, ("compaction",)),
        CapabilityRule(False, ("thinking",)),
    ),
    "codex": TargetContract(
        KEEP,
        KEEP,
        KEEP,
        KEEP,
        KEEP,
        CapabilityRule(False, ("thinking",)),
        preserve_non_image_context=True,
        tool_result_error_loss="tool_result:is_error",
    ),
    "pi": TargetContract(
        KEEP,
        KEEP,
        KEEP,
        KEEP,
        KEEP,
        CapabilityRule(False, ("thinking",)),
    ),
    "omp": TargetContract(
        KEEP,
        KEEP,
        KEEP,
        KEEP,
        KEEP,
        CapabilityRule(False, ("thinking",)),
    ),
    "opencode": TargetContract(
        KEEP,
        KEEP,
        KEEP,
        KEEP,
        KEEP,
        CapabilityRule(False, ("thinking",)),
    ),
    "copilot": TargetContract(
        KEEP,
        KEEP,
        KEEP,
        CapabilityRule(True, ("tool_result:image_provider_dependent",)),
        KEEP,
        CapabilityRule(False, ("thinking",)),
        group_adjacent_messages=True,
    ),
    "antigravity": TargetContract(
        CapabilityRule(False, ("context:image",)),
        KEEP,
        KEEP,
        CapabilityRule(False, ("tool_result:non_text_block",)),
        CapabilityRule(False, ("compaction:no_stored_native_equivalent",)),
        CapabilityRule(False, ("thinking:private",)),
    ),
    "cursor": TargetContract(
        CapabilityRule(False, ("context:unsupported", "image:unsupported")),
        CapabilityRule(False, ("tool_call:unsupported",)),
        CapabilityRule(False, ("tool_result:unsupported",)),
        CapabilityRule(False, ("image:unsupported",)),
        CapabilityRule(False, ("compaction:unsupported",)),
        CapabilityRule(False, ("thinking:unsupported",)),
        result_content_blocks=False,
        opaque_style="cursor_runtime",
        system_loss="system:unsupported",
        other_context_style="cursor",
    ),
    "vibe": TargetContract(
        KEEP,
        KEEP,
        KEEP,
        KEEP,
        KEEP,
        CapabilityRule(False, ("thinking:private",)),
        group_adjacent_messages=True,
    ),
    "muse": TargetContract(
        CapabilityRule(False, ("context",)),
        KEEP,
        KEEP,
        CapabilityRule(False, ("tool_result:image",)),
        CapabilityRule(False, ("compaction",)),
        CapabilityRule(False, ("thinking:private",)),
        other_context_style="generic",
        tool_result_error_loss="tool_result:error_flag",
    ),
    "qwen": TargetContract(
        KEEP,
        KEEP,
        KEEP,
        KEEP,
        CapabilityRule(False, ("compaction",)),
        CapabilityRule(False, ("thinking:private",)),
    ),
    "kimi": TargetContract(
        KEEP,
        KEEP,
        KEEP,
        KEEP,
        KEEP,
        CapabilityRule(False, ("thinking:private",)),
    ),
    "grok": TargetContract(
        KEEP,
        KEEP,
        KEEP,
        CapabilityRule(False, ("tool_result:non_text_content",)),
        CapabilityRule(True, ("compaction:flattened",)),
        CapabilityRule(False, ("thinking:private",)),
        opaque_style="reason_only",
    ),
    "kilo": TargetContract(
        KEEP,
        KEEP,
        KEEP,
        KEEP,
        KEEP,
        CapabilityRule(False, ("thinking",)),
    ),
    "openhands": TargetContract(
        KEEP,
        KEEP,
        KEEP,
        KEEP,
        KEEP,
        CapabilityRule(False, ("thinking:private",)),
        opaque_style="reason_only",
    ),
    "hermes": TargetContract(
        KEEP,
        KEEP,
        KEEP,
        CapabilityRule(False, ("tool_result:non_text_content",)),
        KEEP,
        CapabilityRule(False, ("thinking:private",)),
        result_content_blocks=False,
        opaque_style="role",
        system_loss="message:system",
    ),
    "mastracode": TargetContract(
        KEEP,
        KEEP,
        KEEP,
        # The reviewed contract requires every omitted non-text result block to
        # be counted, even though this exposes a current adapter gap once the
        # native corpus lands.
        CapabilityRule(False, ("tool_result:non_text_content",)),
        KEEP,
        CapabilityRule(False, ("thinking:private",)),
        result_content_blocks=False,
    ),
    "devin": TargetContract(
        KEEP,
        KEEP,
        KEEP,
        CapabilityRule(False, ("tool_result:non_text_content",)),
        CapabilityRule(True, ("compaction:flattened",)),
        CapabilityRule(False, ("thinking:private",)),
        result_content_blocks=False,
        opaque_style="generic",
        system_loss="system:runtime",
    ),
}

# Reviewed evidence that a native modality survives source parsing only as an
# opaque IR record.  Missing structured evidence is never accepted merely
# because some unrelated opaque event exists in the same capture.
OPAQUE_MODALITY_REASONS: Mapping[tuple[str, str], frozenset[str]] = {
    ("copilot", "document"): frozenset({"copilot_attachment_file"}),
    ("kilo", "document"): frozenset({"opencode_nonportable_file"}),
    ("opencode", "document"): frozenset({"opencode_nonportable_file"}),
}


def parse_native_fixture(fixture: NativeFixture, destination: Path) -> Session:
    """Materialize and parse one exact native-corpus source fixture."""

    materialized = fixture.materialize(destination)
    root = materialized.root / "native"
    format_name = fixture.format
    session_id = fixture.provenance.native_session_id

    if format_name == "claude":
        return claude.parse(_single_transcript(materialized.artifact_paths, ".jsonl"))
    if format_name == "codex":
        return codex.parse(_single_transcript(materialized.artifact_paths, ".jsonl"))
    if format_name == "pi":
        return pi.parse_session(_single_transcript(materialized.artifact_paths, ".jsonl"))
    if format_name == "omp":
        return omp.parse_session(_single_transcript(materialized.artifact_paths, ".jsonl"))
    if format_name == "opencode":
        return opencode.parse_session(_single_transcript(materialized.artifact_paths, ".json"))
    if format_name == "copilot":
        return copilot.parse_session(_named(materialized.artifact_paths, "events.jsonl"))
    if format_name == "antigravity":
        return antigravity.parse_session(_single_transcript(materialized.artifact_paths, ".db"))
    if format_name == "cursor":
        parsed = cursor.parse(
            _named(materialized.artifact_paths, "store.db"),
            cwd=Path(fixture.provenance.native_cwd),
        )
        return cursor.project_session(parsed, source_format=AgentFormat.CURSOR)
    if format_name == "vibe":
        return vibe.parse(_named(materialized.artifact_paths, vibe.META_FILENAME).parent)
    if format_name == "muse":
        return muse.parse_session(_single_transcript(materialized.artifact_paths, ".jsonl"))
    if format_name == "qwen":
        return qwen.parse_session(_single_transcript(materialized.artifact_paths, ".jsonl"))
    if format_name == "kimi":
        return kimi.parse_session(_named(materialized.artifact_paths, kimi.STATE_FILENAME).parent)
    if format_name == "grok":
        return grok.parse_session(_named(materialized.artifact_paths, "summary.json").parent)
    if format_name == "kilo":
        return kilo.parse_session(_single_transcript(materialized.artifact_paths, ".json"))
    if format_name == "openhands":
        return openhands.parse_session(_openhands_entrypoint(root, materialized.artifact_paths))
    if format_name == "hermes":
        return hermes.parse_session(
            _single_transcript(materialized.artifact_paths, ".db"), session_id
        )
    if format_name == "mastracode":
        return mastracode.parse_session(
            _single_transcript(materialized.artifact_paths, ".db"), session_id
        )
    if format_name == "devin":
        return devin.parse_session(
            _single_transcript(materialized.artifact_paths, ".db"), session_id
        )
    raise AssertionError(f"unhandled native corpus format: {format_name}")


def normalize_source_session(session: Session) -> tuple[Mapping[str, Any], ...]:
    """Return the strict, content-safe expected-IR representation."""

    return tuple(_normalize_event(event) for event in session.events)


def assert_source_expectations(fixture: NativeFixture, session: Session) -> None:
    """Check independently authored IR and counters before route conversion."""

    expected = tuple(_validate_expected_event(item) for item in fixture.expected_signature())
    actual = normalize_source_session(session)
    if actual != expected:
        raise AssertionError(
            f"{fixture.provenance.fixture_id}: parsed source IR differs from reviewed expected IR"
        )
    if session.event_counts() != dict(fixture.provenance.expectations.event_counts):
        raise AssertionError(
            f"{fixture.provenance.fixture_id}: parsed source event counts differ from provenance"
        )
    opaque = Counter(
        str(event.payload.get("reason") or "unknown")
        for event in session.events
        if event.kind == EventKind.OPAQUE
    )
    if dict(sorted(opaque.items())) != dict(
        sorted(fixture.provenance.expectations.opaque_loss_reasons.items())
    ):
        raise AssertionError(
            f"{fixture.provenance.fixture_id}: opaque loss reasons differ from provenance"
        )
    _assert_fixture_modality_presence(fixture, session)


def observed_modality_counts(session: Session) -> dict[str, int]:
    """Count portable modality evidence produced by a native source parser."""

    counts: Counter[str] = Counter()
    for event in session.events:
        if event.kind == EventKind.MESSAGE and event.role in {Role.USER, Role.ASSISTANT}:
            counts["text"] += 1
        elif event.kind == EventKind.CONTEXT:
            block_type = str(event.payload.get("block_type") or "")
            if event.role == Role.USER and block_type == "image":
                counts[_context_media_modality(event)] += 1
            elif block_type in {"audio", "document", "video"}:
                counts[block_type] += 1
        elif event.kind == EventKind.TOOL_CALL:
            counts["tool_call"] += 1
        elif event.kind == EventKind.TOOL_RESULT:
            counts["tool_result"] += 1
            result_images = sum(
                block.get("type") == "image" for block in _result_blocks(event)
            )
            if result_images:
                counts["tool_result_image"] += result_images
        elif event.kind == EventKind.THINKING:
            counts["readable_reasoning"] += 1
        elif event.kind == EventKind.COMPACTION:
            counts["compaction"] += 1
        elif event.kind == EventKind.OPAQUE:
            counts["opaque"] += 1
    return dict(sorted(counts.items()))


def assert_modality_loss_contract(
    session: Session, target: str, dropped: Mapping[str, int]
) -> None:
    """Require every present structured modality to follow its reviewed rule."""

    contract = TARGET_CONTRACTS[target]
    observed = observed_modality_counts(session)
    rules = {
        "user_image": contract.user_image,
        "document": _context_media_rule(contract, "document"),
        "audio": _context_media_rule(contract, "audio"),
        "video": _context_media_rule(contract, "video"),
        "tool_call": contract.tool_call,
        "tool_result": contract.tool_result,
        "tool_result_image": contract.tool_result_image,
        "compaction": contract.compaction,
        "readable_reasoning": contract.reasoning,
    }
    for modality, rule in rules.items():
        count = observed.get(modality, 0)
        if not count:
            continue
        if not rule.preserve and not rule.loss_keys:
            raise AssertionError(f"{target}: {modality} is silently dropped")
        for loss_key in rule.loss_keys:
            if dropped.get(loss_key, 0) < count:
                raise AssertionError(
                    f"{target}: {modality} requires at least {count} {loss_key!r} "
                    f"losses, got {dropped.get(loss_key, 0)}"
                )


def assert_artifact_warning_contract(
    artifact: ConversionArtifact, *, same_format: bool
) -> None:
    """Require warnings to surface the exact loss ledger and no surprise state."""

    dropped_warnings: dict[str, int] = {}
    other_codes: list[str] = []
    for warning in artifact.warnings:
        code = warning.get("code")
        message = warning.get("message")
        if not isinstance(code, str) or not code:
            raise AssertionError("conversion warning has no non-empty code")
        if not isinstance(message, str) or not message:
            raise AssertionError(f"conversion warning {code!r} has no non-empty message")
        if code != "dropped_event_kind":
            other_codes.append(code)
            continue
        event_kind = warning.get("event_kind")
        count = warning.get("count")
        if not isinstance(event_kind, str) or not event_kind:
            raise AssertionError("dropped-event warning has no event_kind")
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
            raise AssertionError(f"dropped-event warning {event_kind!r} has invalid count")
        if event_kind in dropped_warnings:
            raise AssertionError(f"duplicate dropped-event warning: {event_kind}")
        dropped_warnings[event_kind] = count

    if dropped_warnings != artifact.dropped:
        raise AssertionError(
            "dropped-event warnings differ from artifact loss ledger: "
            f"warnings={dropped_warnings!r}, dropped={artifact.dropped!r}"
        )
    expected_other = ["same_format_portable_rewrite"] if same_format else []
    if other_codes != expected_other:
        raise AssertionError(
            f"unexpected non-loss warning codes: expected={expected_other!r}, "
            f"actual={other_codes!r}"
        )


def expected_loss_counters(session: Session, target: str) -> dict[str, int]:
    """Project exact expected loss counters from the static target contract."""

    contract = TARGET_CONTRACTS[target]
    losses: Counter[str] = Counter()
    for event in session.events:
        if event.kind == EventKind.MESSAGE:
            if event.role == Role.SYSTEM:
                losses[contract.system_loss] += 1
                continue
            if event.payload.get("ui_only_projection") is True:
                losses["message:ui_only_projection"] += 1
            if target == "cursor" and event.timestamp:
                losses["runtime_metadata:event_timestamp"] += 1
            if target == "cursor" and event.payload:
                losses["runtime_metadata:message_payload"] += 1
            continue
        if event.kind == EventKind.CONTEXT:
            if event.role == Role.USER and event.payload.get("block_type") == "image":
                _apply_rule(losses, _context_media_rule(contract, _context_media_modality(event)))
            else:
                losses[_context_loss_key(event, contract)] += 1
            continue
        if event.kind == EventKind.TOOL_CALL:
            _apply_rule(losses, contract.tool_call)
            continue
        if event.kind == EventKind.TOOL_RESULT:
            _apply_rule(losses, contract.tool_result)
            for block in _result_blocks(event):
                if block.get("type") == "image":
                    _apply_rule(losses, contract.tool_result_image)
            if event.payload.get("is_error") is True and contract.tool_result_error_loss:
                losses[contract.tool_result_error_loss] += 1
            continue
        if event.kind == EventKind.COMPACTION:
            _apply_rule(losses, contract.compaction)
            if event.payload.get("has_boundary_metadata") is True and target in {
                "codex",
                "pi",
                "omp",
                "opencode",
                "copilot",
                "vibe",
                "kimi",
                "grok",
                "kilo",
                "openhands",
                "hermes",
                "mastracode",
                "devin",
            }:
                losses["compaction:boundary_metadata"] += 1
            if event.payload.get("replacement_history_expanded") is True and target in {
                "codex",
                "pi",
                "omp",
                "opencode",
                "copilot",
                "vibe",
                "kimi",
                "grok",
                "kilo",
                "openhands",
                "hermes",
                "mastracode",
                "devin",
            }:
                losses["compaction:replacement_history_expanded"] += 1
            continue
        if event.kind == EventKind.THINKING:
            _apply_rule(losses, contract.reasoning)
            if (
                event.payload.get("signature") or event.payload.get("encrypted_content")
            ) and target in {
                "vibe",
                "grok",
                "openhands",
                "hermes",
                "mastracode",
                "devin",
            }:
                losses["thinking:provider_payload"] += 1
            continue
        if event.kind == EventKind.OPAQUE:
            losses[_opaque_loss_key(event, contract)] += 1
            continue
        losses[event.kind.value] += 1

    if target == "codex" and session.title:
        losses["session:title"] += 1
    if target == "cursor":
        losses["runtime_metadata:source_format"] += 1
        if session.cli_version:
            losses["runtime_metadata:source_cli_version"] += 1
        if session.model:
            losses["runtime_metadata:model"] += 1
        if session.model_provider:
            losses["runtime_metadata:model_provider"] += 1

    return dict(sorted((key, count) for key, count in losses.items() if count))


def expected_semantic_signature(
    expected_events: Iterable[Mapping[str, Any]], target: str
) -> tuple[Mapping[str, Any], ...]:
    """Project reviewed source IR through the static target contract."""

    return _semantic_signature(tuple(expected_events), TARGET_CONTRACTS[target])


def materialize_and_reparse_target(
    artifact: ConversionArtifact, destination: Path
) -> TargetObservation:
    """Materialize and independently reparse/validate any of the 18 targets."""

    destination.mkdir(parents=True)
    target = artifact.target_format.value
    session: Session | None = None
    if target == "claude":
        path = destination / "target.jsonl"
        path.write_bytes(artifact.native_bytes)
        session = claude.parse(path)
    elif target == "codex":
        path = destination / "target.jsonl"
        path.write_bytes(artifact.native_bytes)
        session = codex.parse(path)
    elif target == "pi":
        path = destination / "target.jsonl"
        path.write_bytes(artifact.native_bytes)
        session = pi.parse_session(path)
    elif target == "omp":
        path = destination / "target.jsonl"
        path.write_bytes(artifact.native_bytes)
        session = omp.parse_session(path)
    elif target == "opencode":
        path = destination / "target.json"
        path.write_bytes(artifact.native_bytes)
        session = opencode.parse_session(path)
    elif target == "copilot":
        path = destination / artifact.session_id / "events.jsonl"
        path.parent.mkdir(parents=True)
        path.write_bytes(artifact.native_bytes)
        session = copilot.parse_session(path)
    elif target == "antigravity":
        path = destination / f"{artifact.session_id}.db"
        path.write_bytes(artifact.native_bytes)
        session = antigravity.parse_session(path)
    elif target == "cursor":
        path = destination / cursor.workspace_key(artifact.cwd) / artifact.session_id / "store.db"
        path.parent.mkdir(parents=True)
        path.write_bytes(artifact.native_bytes)
        session = cursor.project_session(
            cursor.parse(path, cwd=artifact.cwd), source_format=AgentFormat.CURSOR
        )
    elif target == "vibe":
        path = destination / "vibe"
        path.mkdir()
        metadata, messages = vibe.native_files(artifact.native_bytes, artifact.session_id)
        (path / vibe.META_FILENAME).write_bytes(metadata)
        (path / vibe.MESSAGES_FILENAME).write_bytes(messages)
        session = vibe.parse(path)
    elif target == "muse":
        path = destination / "target.jsonl"
        path.write_bytes(artifact.native_bytes)
        session = muse.parse_session(path)
    elif target == "qwen":
        path = destination / "target.jsonl"
        path.write_bytes(artifact.native_bytes)
        session = qwen.parse_session(path)
    elif target == "kimi":
        path = destination / "kimi"
        state, wire = kimi.native_files(artifact.native_bytes, artifact.session_id, path)
        (path / "agents/main").mkdir(parents=True)
        (path / kimi.STATE_FILENAME).write_bytes(state)
        (path / "agents/main" / kimi.WIRE_FILENAME).write_bytes(wire)
        session = kimi.parse_session(path)
    elif target == "grok":
        path = destination / "grok"
        path.mkdir()
        summary, updates = grok.native_files(artifact.native_bytes, artifact.session_id)
        (path / "summary.json").write_bytes(summary)
        (path / "updates.jsonl").write_bytes(updates)
        session = grok.parse_session(path)
    elif target == "kilo":
        path = destination / "target.json"
        path.write_bytes(artifact.native_bytes)
        session = kilo.parse_session(path)
    elif target == "openhands":
        conversation = destination / artifact.session_id.replace("-", "")
        events = conversation / "events"
        events.mkdir(parents=True)
        for name, data in openhands.native_files(artifact.native_bytes, artifact.session_id):
            (events / name).write_bytes(data)
        session = openhands.parse_session(conversation)
    elif target == "hermes":
        path = destination / "target.json"
        path.write_bytes(artifact.native_bytes)
        parsed = hermes.validate_native_bytes(path.read_bytes(), artifact.session_id)
        return TargetObservation(
            semantics=_hermes_bundle_events(path.read_bytes(), artifact.session_id),
            source_format=target,
            session_id=parsed.session_id,
            native_record_count=1 + len(parsed.messages),
        )
    elif target == "mastracode":
        path = destination / "target.db"
        path.write_bytes(artifact.native_bytes)
        session = mastracode.parse_session(path, artifact.session_id)
    elif target == "devin":
        path = devin.install_database(
            artifact.native_bytes, destination / "devin-home", artifact.session_id
        )
        session = devin.parse_session(path, artifact.session_id)
    else:
        raise AssertionError(f"unhandled target format: {target}")

    normalized = normalize_source_session(session)
    return TargetObservation(
        semantics=_semantic_signature(normalized, TARGET_CONTRACTS[target]),
        source_format=session.source_format.value,
        session_id=session.session_id,
        native_record_count=session.raw_record_count,
    )


def _normalize_event(event: Event) -> Mapping[str, Any]:
    common: dict[str, Any] = {
        "timestamp_category": "present" if event.timestamp else "absent",
        "provenance": {
            "record_type": event.provenance.record_type,
            "block_index": event.provenance.block_index,
        },
    }
    if event.kind == EventKind.MESSAGE:
        return {
            "kind": "message",
            "role": event.role.value if event.role else None,
            "text": event.text,
            **common,
        }
    if (
        event.kind == EventKind.CONTEXT
        and event.role == Role.USER
        and event.payload.get("block_type") == "image"
    ):
        return {
            "kind": "user_image",
            "role": "user",
            "media": _media_descriptor(event.payload.get("image_url")),
            **common,
        }
    if event.kind == EventKind.TOOL_CALL:
        return {
            "kind": "tool_call",
            "role": event.role.value if event.role else None,
            "tool_call_id": event.tool_call_id,
            "tool_name": event.tool_name,
            "input": _canonical_json_value(event.payload.get("input", {})),
            **common,
        }
    if event.kind == EventKind.TOOL_RESULT:
        return {
            "kind": "tool_result",
            "role": event.role.value if event.role else None,
            "text": event.text,
            "tool_call_id": event.tool_call_id,
            "tool_name": event.tool_name,
            "is_error": event.payload.get("is_error") is True,
            "content_blocks": [_normalize_block(block) for block in _result_blocks(event)],
            **common,
        }
    if event.kind == EventKind.THINKING:
        return {
            "kind": "readable_reasoning",
            "role": event.role.value if event.role else None,
            "text": event.text,
            **common,
        }
    if event.kind == EventKind.COMPACTION:
        return {
            "kind": "compaction",
            "role": event.role.value if event.role else None,
            "text": event.text,
            **common,
        }
    if event.kind == EventKind.OPAQUE:
        return {
            "kind": "opaque",
            "opaque_reason": str(event.payload.get("reason") or "unknown"),
            **common,
        }
    return {
        "kind": "context",
        "role": event.role.value if event.role else None,
        "context_type": str(event.payload.get("block_type") or event.kind.value),
        **common,
    }


def _validate_expected_event(value: Mapping[str, Any]) -> Mapping[str, Any]:
    kind = value.get("kind")
    required_by_kind = {
        "message": {"kind", "role", "text", "timestamp_category", "provenance"},
        "user_image": {"kind", "role", "media", "timestamp_category", "provenance"},
        "tool_call": {
            "kind",
            "role",
            "tool_call_id",
            "tool_name",
            "input",
            "timestamp_category",
            "provenance",
        },
        "tool_result": {
            "kind",
            "role",
            "text",
            "tool_call_id",
            "tool_name",
            "is_error",
            "content_blocks",
            "timestamp_category",
            "provenance",
        },
        "readable_reasoning": {
            "kind",
            "role",
            "text",
            "timestamp_category",
            "provenance",
        },
        "compaction": {"kind", "role", "text", "timestamp_category", "provenance"},
        "opaque": {"kind", "opaque_reason", "timestamp_category", "provenance"},
        "context": {
            "kind",
            "role",
            "context_type",
            "timestamp_category",
            "provenance",
        },
    }
    required = required_by_kind.get(kind)
    if required is None or set(value) != required:
        raise CorpusValidationError(
            f"expected IR event fields do not match the reviewed {kind!r} schema"
        )
    if value.get("timestamp_category") not in {"present", "absent"}:
        raise CorpusValidationError("expected IR timestamp_category must be present or absent")
    provenance = value.get("provenance")
    if not isinstance(provenance, dict) or set(provenance) != {"record_type", "block_index"}:
        raise CorpusValidationError("expected IR provenance has an invalid shape")
    return value


def _semantic_signature(
    events: tuple[Mapping[str, Any], ...], contract: TargetContract
) -> tuple[Mapping[str, Any], ...]:
    result: list[Mapping[str, Any]] = []
    for event in events:
        kind = event["kind"]
        if kind == "message" and event.get("role") in {"user", "assistant"}:
            value = {"kind": kind, "role": event["role"], "text": event.get("text")}
            if (
                contract.group_adjacent_messages
                and result
                and result[-1].get("kind") == "message"
                and result[-1].get("role") == value["role"]
            ):
                previous = result.pop()
                value["text"] = f"{previous.get('text')}\n{value.get('text')}"
            result.append(value)
        elif kind == "user_image":
            media = event.get("media")
            modality = _media_descriptor_modality(media)
            if _context_media_rule(contract, modality).preserve:
                result.append({"kind": kind, "media": media})
        elif kind == "tool_call" and contract.tool_call.preserve:
            result.append(
                {
                    "kind": kind,
                    "tool_call_id": event.get("tool_call_id"),
                    "tool_name": event.get("tool_name"),
                    "input": event.get("input"),
                }
            )
        elif kind == "tool_result" and contract.tool_result.preserve:
            blocks = []
            if contract.result_content_blocks:
                blocks = [
                    block
                    for block in event.get("content_blocks", [])
                    if block.get("type") != "image" or contract.tool_result_image.preserve
                ]
            result.append(
                {
                    "kind": kind,
                    "tool_call_id": event.get("tool_call_id"),
                    "text": event.get("text"),
                    "is_error": (
                        event.get("is_error") is True
                        and contract.tool_result_error_loss is None
                    ),
                    "content_blocks": blocks,
                }
            )
        elif kind == "compaction" and contract.compaction.preserve:
            result.append({"kind": kind, "text": event.get("text")})
    return tuple(result)


def _hermes_bundle_events(data: bytes, session_id: str) -> tuple[Mapping[str, Any], ...]:
    parsed = hermes.validate_native_bytes(data, session_id)
    events: list[Mapping[str, Any]] = []
    for message in parsed.messages:
        role = message.get("role")
        content = message.get("content")
        if message.get("_compressed_summary") is True and isinstance(content, str):
            events.append(
                {"kind": "compaction", "text": content.removeprefix("[CONTEXT SUMMARY]:\n")}
            )
            continue
        if role in {"user", "assistant"} and isinstance(content, str) and content:
            events.append({"kind": "message", "role": role, "text": content})
        if role == "user" and isinstance(content, list):
            for block in content:
                image = block.get("image_url") if isinstance(block, dict) else None
                if isinstance(image, dict) and isinstance(image.get("url"), str):
                    events.append({"kind": "user_image", "media": _media_descriptor(image["url"])})
        calls = message.get("tool_calls")
        if role == "assistant" and isinstance(calls, list):
            for call in calls:
                function = call.get("function") if isinstance(call, dict) else None
                if isinstance(function, dict):
                    events.append(
                        {
                            "kind": "tool_call",
                            "tool_call_id": call.get("id"),
                            "tool_name": function.get("name"),
                            "input": json.loads(function.get("arguments", "{}")),
                        }
                    )
        if role == "tool":
            tool_text, is_error = _hermes_bundle_tool_output(content)
            events.append(
                {
                    "kind": "tool_result",
                    "tool_call_id": message.get("tool_call_id"),
                    "text": tool_text,
                    "is_error": is_error,
                    "content_blocks": [],
                }
            )
    return tuple(events)


def _hermes_bundle_tool_output(value: Any) -> tuple[str, bool]:
    """Independently read the native Hermes tool-output envelope."""

    if not isinstance(value, str):
        return "", False
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value, False
    if not isinstance(parsed, dict):
        return value, False
    output = parsed.get("output")
    error = bool(parsed.get("error"))
    exit_code = parsed.get("exit_code")
    return (output if isinstance(output, str) else value), error or (
        isinstance(exit_code, int) and exit_code != 0
    )


def _assert_fixture_modality_presence(fixture: NativeFixture, session: Session) -> None:
    observed = observed_modality_counts(session)
    opaque_reasons = {
        str(event.payload.get("reason") or "unknown")
        for event in session.events
        if event.kind == EventKind.OPAQUE
    }
    for modality, specification in fixture.provenance.modalities.items():
        if not specification.fixture_present:
            continue
        if observed.get(modality, 0):
            continue
        # A native artifact can contain a lossy modality that its parser cannot
        # project structurally.  Accept that only through a reviewed,
        # modality-specific opaque reason; an unrelated opaque event is not
        # evidence for this modality.
        if specification.portable in {"drop", "lossy", "same_format_only"}:
            reasons = OPAQUE_MODALITY_REASONS.get((fixture.format, modality))
            if reasons and opaque_reasons.intersection(reasons):
                continue
        raise AssertionError(
            f"{fixture.provenance.fixture_id}: declared fixture modality "
            f"{modality!r} has no parsed IR evidence"
        )


def _apply_rule(losses: Counter[str], rule: CapabilityRule) -> None:
    for key in rule.loss_keys:
        losses[key] += 1


def _opaque_loss_key(event: Event, contract: TargetContract) -> str:
    reason = str(event.payload.get("reason") or "unknown")
    if contract.opaque_style == "reason_prefixed":
        return f"opaque:{reason}"
    if contract.opaque_style == "reason_only":
        return reason
    if contract.opaque_style == "role":
        return f"opaque:{event.role.value if event.role else 'none'}"
    if contract.opaque_style == "cursor_runtime":
        return "runtime_metadata:opaque_event"
    return "opaque"


def _context_loss_key(event: Event, contract: TargetContract) -> str:
    if contract.other_context_style == "cursor":
        return "context:unsupported"
    if contract.other_context_style == "generic":
        return "context"
    return f"context:{event.payload.get('block_type') or 'unknown'}"


def _result_blocks(event: Event) -> list[Mapping[str, Any]]:
    value = event.payload.get("content_blocks")
    if not isinstance(value, list):
        return []
    return [block for block in value if isinstance(block, dict)]


def _normalize_block(block: Mapping[str, Any]) -> Mapping[str, Any]:
    if block.get("type") == "text":
        return {"type": "text", "text": block.get("text")}
    if block.get("type") == "image":
        return {
            "type": "image",
            "media": _media_descriptor(block.get("image_url") or block.get("url")),
        }
    encoded = json.dumps(block, sort_keys=True, separators=(",", ":"), default=str).encode()
    return {"type": "opaque", "sha256": hashlib.sha256(encoded).hexdigest()}


def _media_descriptor(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, str):
        return {"transport": "invalid", "sha256": hashlib.sha256(b"").hexdigest()}
    if value.startswith("data:"):
        header, separator, encoded = value.partition(",")
        media_type = header[5:].split(";", 1)[0]
        try:
            decoded = base64.b64decode(encoded, validate=True) if separator else b""
        except (binascii.Error, ValueError):
            decoded = value.encode()
            return {
                "transport": "invalid-data-url",
                "media_type": media_type,
                "sha256": hashlib.sha256(decoded).hexdigest(),
            }
        return {
            "transport": "data",
            "media_type": media_type,
            "sha256": hashlib.sha256(decoded).hexdigest(),
        }
    return {
        "transport": "url",
        "sha256": hashlib.sha256(value.encode()).hexdigest(),
    }


def _context_media_modality(event: Event) -> str:
    """Classify a generic native image block by its preserved MIME type."""

    value = event.payload.get("image_url")
    if not isinstance(value, str) or not value.startswith("data:"):
        return "user_image"
    media_type = value[5:].split(";", 1)[0].lower()
    if media_type == "application/pdf":
        return "document"
    if media_type.startswith("audio/"):
        return "audio"
    if media_type.startswith("video/"):
        return "video"
    return "user_image"


def _media_descriptor_modality(value: Any) -> str:
    if not isinstance(value, Mapping):
        return "user_image"
    media_type = str(value.get("media_type") or "").lower()
    if media_type == "application/pdf":
        return "document"
    if media_type.startswith("audio/"):
        return "audio"
    if media_type.startswith("video/"):
        return "video"
    return "user_image"


def _context_media_rule(contract: TargetContract, modality: str) -> CapabilityRule:
    """Return the reviewed target rule for a MIME-specialized context block."""

    if (
        modality == "user_image"
        or not contract.user_image.preserve
        or contract.preserve_non_image_context
    ):
        return contract.user_image
    # Writers currently accept only actual image/* data in the generic image
    # context representation.  A PDF, audio, or video retains exact bytes in
    # source IR but must be reported as a target loss, never silently omitted.
    return CapabilityRule(False, ("context:image",))


def _canonical_json_value(value: Any) -> Any:
    return json.loads(json.dumps(value, sort_keys=True, separators=(",", ":")))


def _single_transcript(paths: tuple[Path, ...], suffix: str) -> Path:
    candidates = [path for path in paths if path.suffix == suffix]
    if len(candidates) != 1:
        raise CorpusValidationError(
            f"expected exactly one {suffix} transcript artifact, found {len(candidates)}"
        )
    return candidates[0]


def _named(paths: tuple[Path, ...], name: str) -> Path:
    candidates = [path for path in paths if path.name == name]
    if len(candidates) != 1:
        raise CorpusValidationError(
            f"expected exactly one native artifact named {name}, found {len(candidates)}"
        )
    return candidates[0]


def _openhands_entrypoint(root: Path, paths: tuple[Path, ...]) -> Path:
    for path in paths:
        if "events" in path.relative_to(root).parts:
            parts = path.relative_to(root).parts
            event_index = parts.index("events")
            return root.joinpath(*parts[:event_index])
    raise CorpusValidationError("OpenHands fixture has no native events directory")


def assert_tool_linkage(events: Iterable[Event]) -> None:
    """Require every result to link to one earlier call in the parsed source."""

    calls: set[str] = set()
    results: set[str] = set()
    for event in events:
        if event.kind == EventKind.TOOL_CALL and event.tool_call_id:
            if event.tool_call_id in calls:
                raise AssertionError(f"duplicate tool call ID: {event.tool_call_id}")
            calls.add(event.tool_call_id)
        elif event.kind == EventKind.TOOL_RESULT and event.tool_call_id:
            if event.tool_call_id not in calls:
                raise AssertionError(f"tool result precedes or lacks call: {event.tool_call_id}")
            if event.tool_call_id in results:
                raise AssertionError(f"duplicate tool result ID: {event.tool_call_id}")
            results.add(event.tool_call_id)
