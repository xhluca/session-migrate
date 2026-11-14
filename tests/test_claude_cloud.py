import json
from pathlib import Path

import pytest

from session_migrate.catalog import Catalog
from session_migrate.cli import main
from session_migrate.conversion import (
    ConversionOptions,
    convert_session,
    load_session,
)
from session_migrate.discovery import locate_session
from session_migrate.errors import SessionMigrateError
from session_migrate.formats import claude_cloud
from session_migrate.inspection import detect_format, detect_path_format, inspect_session
from session_migrate.model import (
    AgentFormat,
    EventKind,
    Role,
    TargetFormat,
)

CONV_UUID_1 = "11111111-aaaa-4111-8111-111111111111"
CONV_UUID_2 = "22222222-bbbb-4222-8222-222222222222"


def _sample_cloud_export() -> list[dict[str, object]]:
    return [
        {
            "uuid": CONV_UUID_1,
            "name": "First Cloud Conversation",
            "created_at": "2026-08-17T10:00:00Z",
            "updated_at": "2026-08-17T10:30:00Z",
            "model": "claude-3-5-sonnet-20241022",
            "chat_messages": [
                {
                    "uuid": "msg-001",
                    "sender": "human",
                    "text": "Can you explain recursion?",
                    "created_at": "2026-08-17T10:00:00Z",
                    "updated_at": "2026-08-17T10:00:00Z",
                    "attachments": [],
                    "files": [],
                },
                {
                    "uuid": "msg-002",
                    "sender": "assistant",
                    "text": "Recursion is when a function calls itself.",
                    "created_at": "2026-08-17T10:01:00Z",
                    "updated_at": "2026-08-17T10:01:00Z",
                    "attachments": [],
                    "files": [],
                },
            ],
        },
        {
            "uuid": CONV_UUID_2,
            "name": "Second Cloud Conversation with Image and Attachment",
            "created_at": "2026-08-18T14:00:00Z",
            "updated_at": "2026-08-18T14:15:00Z",
            "chat_messages": [
                {
                    "uuid": "msg-003",
                    "sender": "human",
                    "text": "",
                    "created_at": "2026-08-18T14:00:00Z",
                    "attachments": [
                        {
                            "file_name": "data.csv",
                            "file_type": "text/csv",
                            "extracted_content": "col1,col2\nval1,val2",
                        },
                        {
                            "file_name": "diagram.png",
                            "file_type": "image/png",
                            "image_url": "https://example.com/diagram.png",
                        },
                    ],
                },
                {
                    "uuid": "msg-004",
                    "sender": "assistant",
                    "text": "I received your CSV data and diagram.",
                    "model": "claude-3-opus-20240229",
                    "created_at": "2026-08-18T14:02:00Z",
                },
            ],
        },
    ]


def test_is_claude_cloud_document() -> None:
    data = _sample_cloud_export()
    assert claude_cloud.is_claude_cloud_document(data) is True

    # Single conversation dict
    assert claude_cloud.is_claude_cloud_document(data[0]) is True

    # Wrapped in conversations list
    assert claude_cloud.is_claude_cloud_document({"conversations": data}) is True

    # Negative cases
    assert claude_cloud.is_claude_cloud_document([]) is False
    assert claude_cloud.is_claude_cloud_document({}) is False
    assert claude_cloud.is_claude_cloud_document({"foo": "bar"}) is False
    assert claude_cloud.is_claude_cloud_document("string") is False


def test_is_claude_cloud_path_and_format_detection(tmp_path: Path) -> None:
    export_file = tmp_path / "conversations.json"
    export_file.write_text(json.dumps(_sample_cloud_export()))

    assert claude_cloud.is_claude_cloud_path(export_file, export_file.stat().st_size) is True
    assert detect_path_format(export_file) == AgentFormat.CLAUDE_CLOUD
    assert detect_format(_sample_cloud_export()) == AgentFormat.CLAUDE_CLOUD


def test_load_conversations_and_listing(tmp_path: Path) -> None:
    export_file = tmp_path / "conversations.json"
    export_file.write_text(json.dumps(_sample_cloud_export()))

    convs = claude_cloud.load_conversations(export_file)
    assert len(convs) == 2

    assert claude_cloud.has_session(export_file, CONV_UUID_1) is True
    assert claude_cloud.has_session(export_file, CONV_UUID_2) is True
    assert claude_cloud.has_session(export_file, "nonexistent-uuid") is False

    summaries = claude_cloud.list_sessions(export_file)
    assert len(summaries) == 2
    assert summaries[0]["session_id"] == CONV_UUID_1
    assert summaries[0]["title"] == "First Cloud Conversation"
    assert summaries[0]["records"] == 2
    assert summaries[1]["session_id"] == CONV_UUID_2


def test_parse_session_rejects_ambiguous_export(tmp_path: Path) -> None:
    export_file = tmp_path / "conversations.json"
    export_file.write_text(json.dumps(_sample_cloud_export()))

    with pytest.raises(SessionMigrateError, match="ambiguous file conversion without a session ID"):
        claude_cloud.parse_session(export_file)


def test_parse_session_by_uuid_and_attachments(tmp_path: Path) -> None:
    export_file = tmp_path / "conversations.json"
    export_file.write_text(json.dumps(_sample_cloud_export()))

    session = claude_cloud.parse_session(export_file, session_id=CONV_UUID_2)
    assert session.session_id == CONV_UUID_2
    assert session.title == "Second Cloud Conversation with Image and Attachment"
    assert session.model == "claude-3-opus-20240229"

    # Message 1 had extracted text from CSV and an image attachment (emitted as CONTEXT event)
    # Message 2 was assistant response
    assert len(session.events) == 3
    event_user_msg = session.events[0]
    assert event_user_msg.kind == EventKind.MESSAGE
    assert event_user_msg.role == Role.USER
    assert "col1,col2" in event_user_msg.text

    event_image = session.events[1]
    assert event_image.kind == EventKind.CONTEXT
    assert event_image.payload.get("block_type") == "image"
    assert event_image.payload.get("image_url") == "https://example.com/diagram.png"

    event_asst_msg = session.events[2]
    assert event_asst_msg.kind == EventKind.MESSAGE
    assert event_asst_msg.role == Role.ASSISTANT
    assert event_asst_msg.text == "I received your CSV data and diagram."


def test_parse_session_single_conversation_object(tmp_path: Path) -> None:
    single = _sample_cloud_export()[0]
    export_file = tmp_path / "single_conversation.json"
    export_file.write_text(json.dumps(single))

    session = claude_cloud.parse_session(export_file)
    assert session.session_id == CONV_UUID_1
    assert len(session.events) == 2


def test_parse_session_nonexistent_raises(tmp_path: Path) -> None:
    export_file = tmp_path / "conversations.json"
    export_file.write_text(json.dumps(_sample_cloud_export()))

    with pytest.raises(SessionMigrateError, match="no Claude Cloud conversation found"):
        claude_cloud.parse_session(export_file, session_id="missing-uuid")


def test_load_session_and_inspection(tmp_path: Path) -> None:
    export_file = tmp_path / "conversations.json"
    export_file.write_text(json.dumps(_sample_cloud_export()))

    session = load_session(
        export_file,
        source_format=AgentFormat.CLAUDE_CLOUD,
        session_id=CONV_UUID_2,
    )
    assert session.session_id == CONV_UUID_2

    # Multiple conversations without session_id raises ambiguous conversion error
    with pytest.raises(SessionMigrateError, match="ambiguous file conversion without a session ID"):
        inspect_session(export_file)

    # Single conversation inspects cleanly
    single_file = tmp_path / "single.json"
    single_file.write_text(json.dumps(_sample_cloud_export()[0]))
    inspection = inspect_session(single_file)
    assert inspection.format == AgentFormat.CLAUDE_CLOUD.value
    assert inspection.session_id == CONV_UUID_1
    assert inspection.records == 2


def test_discovery_and_catalog(tmp_path: Path) -> None:
    export_file = tmp_path / "conversations.json"
    export_file.write_text(json.dumps(_sample_cloud_export()))

    located = locate_session(AgentFormat.CLAUDE_CLOUD, CONV_UUID_1, tmp_path)
    assert located.resolve() == export_file.resolve()
    located_2 = locate_session(AgentFormat.CLAUDE_CLOUD, CONV_UUID_2, tmp_path)
    assert located_2.resolve() == export_file.resolve()

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    result = catalog.refresh(claude_cloud_roots=[tmp_path], include_auto=False)
    assert result.root_errors == 0
    assert result.files_seen == 2

    # Both conversations indexed with their native titles
    sessions_1 = catalog.list_sessions(query="First Cloud Conversation")
    assert len(sessions_1) == 1
    assert sessions_1[0].session_id == CONV_UUID_1

    sessions_2 = catalog.list_sessions(query="Second Cloud Conversation")
    assert len(sessions_2) == 1
    assert sessions_2[0].session_id == CONV_UUID_2


def test_convert_claude_cloud_to_codex_and_claude(tmp_path: Path) -> None:
    export_file = tmp_path / "conversations.json"
    export_file.write_text(json.dumps(_sample_cloud_export()))

    session = claude_cloud.parse_session(export_file, session_id=CONV_UUID_1)

    # Convert to CODEX
    codex_artifact = convert_session(
        session,
        ConversionOptions(
            target_format=TargetFormat.CODEX,
            session_id="33333333-3333-4333-8333-333333333333",
            cwd=tmp_path,
        ),
    )
    assert codex_artifact.target_format == TargetFormat.CODEX
    assert codex_artifact.native_record_count > 0

    # Convert to CLAUDE
    claude_artifact = convert_session(
        session,
        ConversionOptions(
            target_format=TargetFormat.CLAUDE,
            session_id="44444444-4444-4444-8444-444444444444",
            cwd=tmp_path,
        ),
    )
    assert claude_artifact.target_format == TargetFormat.CLAUDE
    assert claude_artifact.native_record_count > 0


def test_cli_inspect_and_convert(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    export_file = tmp_path / "conversations.json"
    export_file.write_text(json.dumps(_sample_cloud_export()))

    single_file = tmp_path / "single.json"
    single_file.write_text(json.dumps(_sample_cloud_export()[0]))

    # CLI inspect on ambiguous multi-session export fails with code 2
    code_ambiguous = main(["inspect", str(export_file)])
    assert code_ambiguous == 2

    # CLI inspect on single conversation succeeds
    code = main(["inspect", str(single_file)])
    assert code == 0
    captured = capsys.readouterr()
    assert "claude-cloud" in captured.out
    assert CONV_UUID_1 in captured.out

    # CLI inspect with --from alias
    code_from = main(["inspect", str(single_file), "--from", "claude-cloud"])
    assert code_from == 0

    # Converting ambiguous multi-conversation file fails with code 2
    out_ambiguous = tmp_path / "out_ambiguous.jsonl"
    convert_ambiguous = main(
        [
            "convert",
            str(export_file),
            "--from",
            "claude-cloud",
            "--to",
            "codex",
            "--output",
            str(out_ambiguous),
            "--cwd",
            str(tmp_path),
        ]
    )
    assert convert_ambiguous == 2

    # CLI convert on single conversation succeeds
    out_file = tmp_path / "converted.jsonl"
    convert_code = main(
        [
            "convert",
            str(single_file),
            "--from",
            "claude-cloud",
            "--to",
            "codex",
            "--output",
            str(out_file),
            "--cwd",
            str(tmp_path),
            "--session-id",
            "55555555-5555-4555-8555-555555555555",
        ]
    )
    assert convert_code == 0
    assert out_file.is_file()
    assert (tmp_path / "converted.jsonl.session-migrate.json").is_file()


def test_cli_transfer_second_uuid_by_id(tmp_path: Path) -> None:
    export_file = tmp_path / "conversations.json"
    export_file.write_text(json.dumps(_sample_cloud_export()))
    codex_home = tmp_path / "codex_home"

    # Transfer second UUID directly using --source-home pointing to conversations.json
    code = main(
        [
            "transfer",
            CONV_UUID_2,
            "--from",
            "claude-cloud",
            "--source-home",
            str(export_file),
            "--to",
            "codex",
            "--home",
            str(codex_home),
            "--cwd",
            str(tmp_path),
        ]
    )
    assert code == 0

    # Ensure Codex transcript was created
    codex_files = list(codex_home.rglob("*.jsonl"))
    assert len(codex_files) == 1
    content = codex_files[0].read_text(encoding="utf-8")
    assert "col1,col2" in content
    assert "diagram.png" in content


def test_catalog_file_root_indexes_all_conversations(tmp_path: Path) -> None:
    export_file = tmp_path / "conversations.json"
    export_file.write_text(json.dumps(_sample_cloud_export()))

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    # Add root as the file itself
    catalog.add_root(AgentFormat.CLAUDE_CLOUD, export_file)
    result = catalog.refresh(include_auto=False)

    assert result.root_errors == 0
    assert result.files_seen == 2
    assert result.scanned == 2

    # Check both conversations are registered in catalog
    all_sessions = catalog.list_sessions(agent_format=AgentFormat.CLAUDE_CLOUD)
    assert len(all_sessions) == 2
    session_ids = {s.session_id for s in all_sessions}
    assert session_ids == {CONV_UUID_1, CONV_UUID_2}

    # Verify session_source_for_transfer returns the file path and exact conversation ID
    entry_2 = next(s for s in all_sessions if s.session_id == CONV_UUID_2)
    source = catalog.session_source_for_transfer(entry_2.catalog_id)
    assert source.format == AgentFormat.CLAUDE_CLOUD
    assert source.path == export_file.resolve()
    assert source.session_id == CONV_UUID_2


def test_catalog_transfer_by_title_and_by_catalog_id(tmp_path: Path) -> None:
    export_file = tmp_path / "conversations.json"
    export_file.write_text(json.dumps(_sample_cloud_export()))
    catalog_path = tmp_path / "catalog.sqlite3"

    catalog = Catalog(catalog_path)
    catalog.add_root(AgentFormat.CLAUDE_CLOUD, tmp_path)
    catalog.refresh(include_auto=False)

    codex_home_title = tmp_path / "codex_title"
    # Transfer by exact title
    code_title = main(
        [
            "--catalog",
            str(catalog_path),
            "transfer",
            "--title",
            "Second Cloud Conversation with Image and Attachment",
            "--to",
            "codex",
            "--home",
            str(codex_home_title),
            "--cwd",
            str(tmp_path),
        ]
    )
    assert code_title == 0
    codex_files = list(codex_home_title.rglob("*.jsonl"))
    assert len(codex_files) == 1
    content = codex_files[0].read_text(encoding="utf-8")
    assert "col1,col2" in content

    # Transfer by catalog ID
    session_1 = catalog.list_sessions(query="First Cloud Conversation")[0]
    codex_home_cat = tmp_path / "codex_catalog_id"
    code_cat = main(
        [
            "--catalog",
            str(catalog_path),
            "transfer",
            "--catalog-id",
            session_1.catalog_id,
            "--to",
            "codex",
            "--home",
            str(codex_home_cat),
            "--cwd",
            str(tmp_path),
        ]
    )
    assert code_cat == 0
    codex_files_cat = list(codex_home_cat.rglob("*.jsonl"))
    assert len(codex_files_cat) == 1
    content_cat = codex_files_cat[0].read_text(encoding="utf-8")
    assert "Can you explain recursion?" in content_cat


def test_cli_catalog_refresh_claude_cloud_root(tmp_path: Path) -> None:
    export_file = tmp_path / "conversations.json"
    export_file.write_text(json.dumps(_sample_cloud_export()))
    catalog_path = tmp_path / "catalog.sqlite3"

    code = main(
        [
            "--catalog",
            str(catalog_path),
            "catalog",
            "refresh",
            "--claude-cloud-root",
            str(export_file),
            "--no-auto-roots",
        ]
    )
    assert code == 0

    catalog = Catalog(catalog_path)
    sessions = catalog.list_sessions(agent_format=AgentFormat.CLAUDE_CLOUD)
    assert len(sessions) == 2
    assert {s.session_id for s in sessions} == {CONV_UUID_1, CONV_UUID_2}
