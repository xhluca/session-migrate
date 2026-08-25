import json
from pathlib import Path
from typing import Any

import pytest

from session_migrate.conversion import ConversionOptions, convert_session
from session_migrate.formats import (
    antigravity,
    claude,
    codex,
    copilot,
    cursor,
    kimi,
    muse,
    omp,
    opencode,
    pi,
    qwen,
    vibe,
)
from session_migrate.model import (
    AgentFormat,
    Event,
    EventKind,
    Provenance,
    Role,
    Session,
    TargetFormat,
)

FIXTURES = Path(__file__).parent / "fixtures"
TARGET_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def source_sessions(tmp_path: Path) -> dict[str, Session]:
    sessions = {
        "claude": claude.parse(FIXTURES / "claude-2.1.209" / "basic.jsonl"),
        "codex": codex.parse(FIXTURES / "codex-0.144.4" / "basic.jsonl"),
        "pi": pi.parse_session(FIXTURES / "pi-0.80.6" / "basic.jsonl"),
        "omp": omp.parse_session(FIXTURES / "omp-18.0.5" / "basic.jsonl"),
        "opencode": opencode.parse_session(
            FIXTURES / "opencode-source-1.17.20" / "comprehensive.json"
        ),
        "copilot": copilot.parse_session(
            FIXTURES / "copilot-source-1.0.70" / "copilot-source-native-events.jsonl"
        ),
    }
    antigravity_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    antigravity_bytes, _ = antigravity.serialize(
        sessions["claude"],
        session_id=antigravity_id,
        cwd=tmp_path,
        timestamp="2026-08-20T12:00:00Z",
    )
    antigravity_path = tmp_path / f"{antigravity_id}.db"
    antigravity_path.write_bytes(antigravity_bytes)
    sessions["antigravity"] = antigravity.parse_session(antigravity_path)
    cursor_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    cursor_bytes, _ = cursor.serialize(
        sessions["claude"],
        session_id=cursor_id,
        cwd=tmp_path,
        timestamp="2026-08-20T12:00:00Z",
    )
    cursor_path = tmp_path / cursor_id / "store.db"
    cursor_path.parent.mkdir()
    cursor_path.write_bytes(cursor_bytes)
    sessions["cursor"] = cursor.project_session(
        cursor.parse(cursor_path), source_format=AgentFormat.CURSOR
    )
    vibe_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    vibe_bytes, _ = vibe.serialize(
        sessions["claude"],
        session_id=vibe_id,
        cwd=tmp_path,
        timestamp="2026-08-20T12:00:00Z",
    )
    meta_bytes, messages_bytes = vibe.native_files(vibe_bytes, vibe_id)
    vibe_path = tmp_path / "vibe-source"
    vibe_path.mkdir()
    (vibe_path / vibe.META_FILENAME).write_bytes(meta_bytes)
    (vibe_path / vibe.MESSAGES_FILENAME).write_bytes(messages_bytes)
    sessions["vibe"] = vibe.parse_session(vibe_path)
    muse_id = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    muse_bytes, _ = muse.serialize(
        sessions["claude"],
        session_id=muse_id,
        cwd=tmp_path,
        timestamp="2026-08-20T12:00:00Z",
    )
    muse_path = tmp_path / "muse-source.jsonl"
    muse_path.write_bytes(muse_bytes)
    sessions["muse"] = muse.parse_session(muse_path)
    qwen_id = "ffffffff-ffff-4fff-8fff-ffffffffffff"
    qwen_bytes, _ = qwen.serialize(
        sessions["claude"],
        session_id=qwen_id,
        cwd=tmp_path,
        timestamp="2026-08-20T12:00:00Z",
    )
    qwen_path = tmp_path / "qwen-source.jsonl"
    qwen_path.write_bytes(qwen_bytes)
    sessions["qwen"] = qwen.parse_session(qwen_path)
    kimi_id = "12121212-1212-4212-8212-121212121212"
    kimi_native_id = kimi.native_session_id(kimi_id)
    kimi_bytes, _ = kimi.serialize(
        sessions["claude"],
        session_id=kimi_native_id,
        cwd=tmp_path,
        timestamp="2026-08-20T12:00:00Z",
    )
    kimi_path = tmp_path / "kimi-source"
    state_bytes, wire_bytes = kimi.native_files(kimi_bytes, kimi_native_id, kimi_path)
    (kimi_path / "agents/main").mkdir(parents=True)
    (kimi_path / kimi.STATE_FILENAME).write_bytes(state_bytes)
    (kimi_path / "agents/main" / kimi.WIRE_FILENAME).write_bytes(wire_bytes)
    sessions["kimi"] = kimi.parse_session(kimi_path)
    return sessions


def portable_signature(
    events: tuple[Event, ...],
    *,
    include_compaction: bool,
    include_images: bool = True,
    include_tools: bool = True,
    group_messages: bool = False,
) -> list[Any]:
    result: list[Any] = []
    for event in events:
        if event.kind == EventKind.MESSAGE and event.role in {Role.USER, Role.ASSISTANT}:
            value = ("message", event.role.value, event.text)
            if group_messages and result and result[-1][:2] == value[:2]:
                previous = result.pop()
                result.append((*value[:2], f"{previous[2]}\n{event.text}"))
            else:
                result.append(value)
        elif include_images and event.kind == EventKind.CONTEXT and event.role == Role.USER:
            result.append(("image", event.payload.get("image_url")))
        elif include_tools and event.kind == EventKind.TOOL_CALL:
            result.append(
                (
                    "call",
                    event.tool_call_id,
                    event.tool_name,
                    json.dumps(event.payload.get("input", {}), sort_keys=True),
                )
            )
        elif include_tools and event.kind == EventKind.TOOL_RESULT:
            blocks = [
                block
                for block in event.payload.get("content_blocks", [])
                if isinstance(block, dict)
                and block.get("type") in ({"text", "image"} if include_images else {"text"})
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
    if target == TargetFormat.OMP:
        return omp.parse_session(path)
    if target == TargetFormat.OPENCODE:
        return opencode.parse_session(path)
    if target == TargetFormat.COPILOT:
        return copilot.parse_session(path)
    if target == TargetFormat.CURSOR:
        return cursor.project_session(cursor.parse(path), source_format=AgentFormat.CURSOR)
    if target == TargetFormat.VIBE:
        return vibe.parse_session(path)
    if target == TargetFormat.MUSE:
        return muse.parse_session(path)
    if target == TargetFormat.QWEN:
        return qwen.parse_session(path)
    if target == TargetFormat.KIMI:
        return kimi.parse_session(path)
    return antigravity.parse_session(path)


@pytest.mark.parametrize(
    "source_name",
    (
        "claude",
        "codex",
        "pi",
        "omp",
        "opencode",
        "copilot",
        "antigravity",
        "cursor",
        "vibe",
        "muse",
        "qwen",
        "kimi",
    ),
)
@pytest.mark.parametrize(
    "target",
    (
        TargetFormat.CLAUDE,
        TargetFormat.CODEX,
        TargetFormat.PI,
        TargetFormat.OMP,
        TargetFormat.OPENCODE,
        TargetFormat.COPILOT,
        TargetFormat.ANTIGRAVITY,
        TargetFormat.CURSOR,
        TargetFormat.VIBE,
        TargetFormat.MUSE,
        TargetFormat.QWEN,
        TargetFormat.KIMI,
    ),
)
def test_every_supported_source_to_target_route_preserves_portable_timeline(
    source_name: str, target: TargetFormat, tmp_path: Path
) -> None:
    source = source_sessions(tmp_path)[source_name]
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
    elif target == TargetFormat.OPENCODE:
        output = tmp_path / ("target.json" if target == TargetFormat.OPENCODE else "target.jsonl")
    elif target == TargetFormat.ANTIGRAVITY:
        output = tmp_path / f"{artifact.session_id}.db"
    elif target == TargetFormat.CURSOR:
        output = tmp_path / artifact.session_id / "store.db"
        output.parent.mkdir()
    elif target == TargetFormat.VIBE:
        output = tmp_path / "vibe-target"
        output.mkdir()
        meta_bytes, messages_bytes = vibe.native_files(artifact.native_bytes, artifact.session_id)
        (output / vibe.META_FILENAME).write_bytes(meta_bytes)
        (output / vibe.MESSAGES_FILENAME).write_bytes(messages_bytes)
    elif target == TargetFormat.KIMI:
        output = tmp_path / "kimi-target"
        state_bytes, wire_bytes = kimi.native_files(
            artifact.native_bytes, artifact.session_id, output
        )
        (output / "agents/main").mkdir(parents=True)
        (output / kimi.STATE_FILENAME).write_bytes(state_bytes)
        (output / "agents/main" / kimi.WIRE_FILENAME).write_bytes(wire_bytes)
    else:
        output = tmp_path / "target.jsonl"
    if target not in {TargetFormat.VIBE, TargetFormat.KIMI}:
        output.write_bytes(artifact.native_bytes)
    reparsed = parse_target(output, target)

    # Claude accepts compact-summary records on native resume, but its reader
    # intentionally treats generated summaries as transcript metadata rather
    # than re-emitting a second portable compaction event.
    include_compaction = target not in {
        TargetFormat.CLAUDE,
        TargetFormat.ANTIGRAVITY,
        TargetFormat.CURSOR,
        TargetFormat.MUSE,
        TargetFormat.QWEN,
    }
    include_images = target not in {
        TargetFormat.ANTIGRAVITY,
        TargetFormat.CURSOR,
        TargetFormat.MUSE,
    }
    include_tools = target != TargetFormat.CURSOR
    group_messages = target in {TargetFormat.COPILOT, TargetFormat.VIBE}
    assert portable_signature(
        reparsed.events,
        include_compaction=include_compaction,
        include_images=include_images,
        include_tools=include_tools,
        group_messages=group_messages,
    ) == portable_signature(
        source.events,
        include_compaction=include_compaction,
        include_images=include_images,
        include_tools=include_tools,
        group_messages=group_messages,
    )
    if source.source_format.value == target.value:
        assert any(
            warning["code"] == "same_format_portable_rewrite" for warning in artifact.warnings
        )


def test_codex_target_explicitly_counts_nonportable_tool_error_status(tmp_path: Path) -> None:
    source = Session(
        source_format=AgentFormat.OPENCODE,
        source_path=tmp_path / "source.json",
        source_sha256="0" * 64,
        session_id="ses_synthetic",
        cwd=tmp_path,
        started_at="2026-08-20T12:00:00Z",
        cli_version="1.17.20",
        model=None,
        title=None,
        raw_record_count=3,
        events=(
            Event(
                kind=EventKind.MESSAGE,
                role=Role.USER,
                text="run",
                provenance=Provenance(0),
            ),
            Event(
                kind=EventKind.TOOL_CALL,
                role=Role.ASSISTANT,
                tool_call_id="call-1",
                tool_name="read",
                payload={"input": {}},
                provenance=Provenance(1),
            ),
            Event(
                kind=EventKind.TOOL_RESULT,
                role=Role.TOOL,
                text="failed",
                tool_call_id="call-1",
                payload={"is_error": True, "content_blocks": [{"type": "text", "text": "failed"}]},
                provenance=Provenance(2),
            ),
        ),
    )

    artifact = convert_session(
        source,
        ConversionOptions(
            target_format=TargetFormat.CODEX,
            session_id=TARGET_UUID,
            cwd=tmp_path,
        ),
    )

    assert artifact.dropped["tool_result:is_error"] == 1
