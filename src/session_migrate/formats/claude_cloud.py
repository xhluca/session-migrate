"""Claude Cloud (Claude.ai) export reader and session adapter."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from session_migrate.errors import SessionMigrateError
from session_migrate.formats.common import content_text, string, valid_rfc3339
from session_migrate.jsonl import DEFAULT_MAX_TOTAL_BYTES, file_sha256
from session_migrate.model import AgentFormat, Event, EventKind, Provenance, Role, Session

PINNED_CLAUDE_CLOUD_VERSION = "claude-cloud"


def load_conversations(path: Path) -> list[dict[str, Any]]:
    """Load conversation dictionaries from a Claude.ai export JSON file."""
    if not path.is_file() or path.is_symlink():
        raise SessionMigrateError(f"Claude Cloud source path must be a regular file: {path}")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise SessionMigrateError(f"cannot stat Claude Cloud source file: {path}") from exc
    if size > DEFAULT_MAX_TOTAL_BYTES:
        raise SessionMigrateError(
            f"Claude Cloud export file exceeds size limit ({size} > {DEFAULT_MAX_TOTAL_BYTES})"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SessionMigrateError(f"cannot parse Claude Cloud export file {path}: {exc}") from exc

    if isinstance(data, list):
        return [c for c in data if isinstance(c, dict)]
    if isinstance(data, dict):
        if "conversations" in data and isinstance(data["conversations"], list):
            return [c for c in data["conversations"] if isinstance(c, dict)]
        if "chat_messages" in data or "uuid" in data:
            return [data]
    raise SessionMigrateError(f"unrecognized Claude Cloud export structure in {path}")


def is_claude_cloud_document(data: Any) -> bool:
    """Determine if a parsed JSON structure matches Claude Cloud exports."""
    if isinstance(data, list):
        if not data:
            return False
        first = data[0]
        return bool(
            isinstance(first, dict)
            and "chat_messages" in first
            and ("uuid" in first or "name" in first or "created_at" in first)
        )
    if isinstance(data, dict):
        if "chat_messages" in data and (
            "uuid" in data or "name" in data or "created_at" in data
        ):
            return True
        convs = data.get("conversations")
        if isinstance(convs, list) and convs:
            first = convs[0]
            if isinstance(first, dict) and "chat_messages" in first:
                return True
    return False


def is_claude_cloud_path(path: Path, size: int) -> bool:
    """Check whether a file on disk is a Claude Cloud JSON export."""
    if size > DEFAULT_MAX_TOTAL_BYTES or size == 0:
        return False
    try:
        data = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return False
    return is_claude_cloud_document(data)


def has_session(path: Path, session_id: str) -> bool:
    """Return whether the file contains a conversation with the specified session ID."""
    try:
        conversations = load_conversations(path)
    except Exception:
        return False
    norm_id = session_id.lower()
    for conv in conversations:
        conv_id = string(conv.get("uuid")) or string(conv.get("id"))
        if conv_id and conv_id.lower() == norm_id:
            return True
    return False


def list_sessions(path: Path) -> tuple[dict[str, Any], ...]:
    """Return metadata summaries for all conversations in a Claude Cloud export."""
    conversations = load_conversations(path)
    summaries: list[dict[str, Any]] = []
    for conv in conversations:
        conv_id = string(conv.get("uuid")) or string(conv.get("id")) or ""
        title = string(conv.get("name")) or string(conv.get("title")) or "Untitled"
        created_at = valid_rfc3339(conv.get("created_at"))
        messages = conv.get("chat_messages") or conv.get("messages") or []
        summaries.append(
            {
                "session_id": conv_id,
                "title": title,
                "created_at": created_at,
                "records": len(messages) if isinstance(messages, list) else 0,
            }
        )
    return tuple(summaries)


def parse_session(path: Path, session_id: str | None = None) -> Session:
    """Parse one Claude Cloud conversation from an export JSON file into a Session."""
    conversations = load_conversations(path)
    if not conversations:
        raise SessionMigrateError(f"Claude Cloud export contains no conversations: {path}")

    selected: dict[str, Any] | None = None
    if session_id:
        norm_id = session_id.lower()
        for conv in conversations:
            conv_id = string(conv.get("uuid")) or string(conv.get("id"))
            if conv_id and conv_id.lower() == norm_id:
                selected = conv
                break
        if selected is None:
            raise SessionMigrateError(
                f"no Claude Cloud conversation found for UUID {session_id} in {path}"
            )
    else:
        selected = conversations[0]

    conv_uuid = string(selected.get("uuid")) or string(selected.get("id"))
    if not conv_uuid:
        conv_uuid = str(uuid.uuid4())
    title = string(selected.get("name")) or string(selected.get("title"))
    created_at = string(selected.get("created_at"))
    updated_at = string(selected.get("updated_at"))
    started_at = valid_rfc3339(created_at) or valid_rfc3339(updated_at)
    model = string(selected.get("model"))

    chat_messages: list[Any] = (
        selected.get("chat_messages")
        if isinstance(selected.get("chat_messages"), list)
        else selected.get("messages")
        if isinstance(selected.get("messages"), list)
        else []
    )

    events: list[Event] = []
    for idx, msg in enumerate(chat_messages):
        if not isinstance(msg, dict):
            continue
        msg_id = string(msg.get("uuid")) or string(msg.get("id")) or str(uuid.uuid4())
        sender = string(msg.get("sender")) or string(msg.get("role")) or "unknown"
        sender_lower = sender.lower()
        if sender_lower in {"human", "user"}:
            role = Role.USER
        elif sender_lower in {"assistant", "bot"}:
            role = Role.ASSISTANT
        elif sender_lower == "system":
            role = Role.SYSTEM
        else:
            role = (
                Role.USER
                if "human" in sender_lower or "user" in sender_lower
                else Role.ASSISTANT
            )

        if role == Role.ASSISTANT and model is None and msg.get("model"):
            model = string(msg.get("model"))

        msg_ts = (
            valid_rfc3339(msg.get("created_at"))
            or valid_rfc3339(msg.get("updated_at"))
            or started_at
        )
        text = string(msg.get("text")) or content_text(msg.get("content"))
        attachments = msg.get("attachments") if isinstance(msg.get("attachments"), list) else []
        files = msg.get("files") if isinstance(msg.get("files"), list) else []

        if not text:
            extracted = [
                string(a.get("extracted_content"))
                for a in attachments
                if isinstance(a, dict) and string(a.get("extracted_content"))
            ]
            if extracted:
                text = "\n\n".join(extracted)
            else:
                att_names = [
                    string(a.get("file_name"))
                    for a in attachments
                    if isinstance(a, dict) and string(a.get("file_name"))
                ]
                if att_names:
                    text = f"[Attached: {', '.join(att_names)}]"
            if not text and files:
                file_names = [
                    string(f.get("file_name"))
                    for f in files
                    if isinstance(f, dict) and string(f.get("file_name"))
                ]
                if file_names:
                    text = f"[Attached: {', '.join(file_names)}]"

        provenance = Provenance(
            record_index=idx,
            record_type=f"chat_messages:{sender}",
            source_id=msg_id,
        )
        payload: dict[str, Any] = {}
        if attachments:
            payload["attachments"] = attachments
        if files:
            payload["files"] = files

        events.append(
            Event(
                kind=EventKind.MESSAGE,
                role=role,
                text=text or "",
                timestamp=msg_ts,
                provenance=provenance,
                payload=payload,
            )
        )

        for att_idx, att in enumerate(attachments):
            if isinstance(att, dict):
                file_type = string(att.get("file_type")) or ""
                image_url = string(att.get("image_url")) or string(att.get("url"))
                if image_url and file_type.startswith("image/"):
                    events.append(
                        Event(
                            kind=EventKind.CONTEXT,
                            role=role,
                            timestamp=msg_ts,
                            payload={"block_type": "image", "image_url": image_url},
                            provenance=Provenance(
                                record_index=idx,
                                record_type="attachment",
                                source_id=msg_id,
                                block_index=att_idx,
                            ),
                        )
                    )

    return Session(
        source_format=AgentFormat.CLAUDE_CLOUD,
        source_path=path.resolve(),
        source_sha256=file_sha256(path),
        session_id=conv_uuid,
        cwd=None,
        started_at=started_at,
        cli_version=None,
        model=model,
        title=title,
        events=tuple(events),
        raw_record_count=len(chat_messages),
        model_provider="anthropic",
    )
