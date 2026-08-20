import json
from pathlib import Path
from typing import Any

import pytest

from session_migrate.conversion import ConversionOptions, convert_session
from session_migrate.formats import claude, codex, copilot, opencode, pi
from session_migrate.model import Event, EventKind, Role, Session, TargetFormat

FIXTURES = Path(__file__).parent / "fixtures"
TARGET_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def source_sessions() -> dict[str, Session]:
    return {
        "claude": claude.parse(FIXTURES / "claude-2.1.209" / "basic.jsonl"),
        "codex": codex.parse(FIXTURES / "codex-0.144.4" / "basic.jsonl"),
        "pi": pi.parse_session(FIXTURES / "pi-0.80.6" / "basic.jsonl"),
        "opencode": opencode.parse_session(
            FIXTURES / "opencode-source-1.17.20" / "comprehensive.json"
        ),
        "copilot": copilot.parse_session(
            FIXTURES
            / "copilot-source-1.0.70"
            / "copilot-source-native-events.jsonl"
        ),
    }


def portable_signature(events: tuple[Event, ...], *, include_compaction: bool) -> list[Any]:
    result: list[Any] = []
    for event in events:
        if event.kind == EventKind.MESSAGE and event.role in {Role.USER, Role.ASSISTANT}:
            result.append(("message", event.role.value, event.text))
        elif event.kind == EventKind.CONTEXT and event.role == Role.USER:
            result.append(("image", event.payload.get("image_url")))
        elif event.kind == EventKind.TOOL_CALL:
            result.append(
                (
                    "call",
                    event.tool_call_id,
                    event.tool_name,
                    json.dumps(event.payload.get("input", {}), sort_keys=True),
                )
            )
        elif event.kind == EventKind.TOOL_RESULT:
            blocks = [
                block
                for block in event.payload.get("content_blocks", [])
                if isinstance(block, dict) and block.get("type") in {"text", "image"}
            ]
            result.append(
                (
                    "result",
                    event.tool_call_id,
                    event.text,
                    event.payload.get("is_error") is True,
                    json.dumps(blocks, sort_keys=True),
                )
            )
        elif include_compaction and event.kind == EventKind.COMPACTION and event.text:
            result.append(("compaction", event.text))
    return result


def parse_target(path: Path, target: TargetFormat) -> Session:
    if target == TargetFormat.CLAUDE:
        return claude.parse(path)
    if target == TargetFormat.CODEX:
        return codex.parse(path)
    if target == TargetFormat.PI:
        return pi.parse_session(path)
    if target == TargetFormat.OPENCODE:
        return opencode.parse_session(path)
    return copilot.parse_session(path)


@pytest.mark.parametrize("source_name", tuple(source_sessions()))
@pytest.mark.parametrize(
    "target",
    (
        TargetFormat.CLAUDE,
        TargetFormat.CODEX,
        TargetFormat.PI,
        TargetFormat.OPENCODE,
        TargetFormat.COPILOT,
    ),
)
def test_every_supported_source_to_target_route_preserves_portable_timeline(
    source_name: str, target: TargetFormat, tmp_path: Path
) -> None:
    source = source_sessions()[source_name]
    artifact = convert_session(
        source,
        ConversionOptions(
            target_format=target,
            session_id=TARGET_UUID,
            cwd=tmp_path,
        ),
    )
    if target == TargetFormat.COPILOT:
        output = tmp_path / artifact.session_id / "events.jsonl"
        output.parent.mkdir()
    else:
        output = tmp_path / ("target.json" if target == TargetFormat.OPENCODE else "target.jsonl")
    output.write_bytes(artifact.native_bytes)
    reparsed = parse_target(output, target)

    # Claude accepts compact-summary records on native resume, but its reader
    # intentionally treats generated summaries as transcript metadata rather
    # than re-emitting a second portable compaction event.
    include_compaction = target != TargetFormat.CLAUDE
    assert portable_signature(
        reparsed.events, include_compaction=include_compaction
    ) == portable_signature(source.events, include_compaction=include_compaction)
    if source.source_format.value == target.value:
        assert any(
            warning["code"] == "same_format_portable_rewrite"
            for warning in artifact.warnings
        )
