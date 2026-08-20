"""GitHub Copilot CLI 1.0.70 session-event reader and writer.

Copilot's canonical portable history is the append-only ``events.jsonl`` file
below ``$COPILOT_HOME/session-state/<uuid>``.  The global and per-session SQLite
files are projections/runtime state and are deliberately not synthesized.

The source reader is based on the generated ``session-events.d.ts`` shipped in
the exact 1.0.70 platform package.  It accepts that complete event-name union,
but projects only model-visible conversation semantics.  Unknown schemas fail
closed; lifecycle, privileged, sub-agent, and UI-only records become explicit
opaque loss events rather than being flattened into the main conversation.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from collections import Counter, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from session_migrate.errors import SessionMigrateError
from session_migrate.formats.common import portable_data_image, string, valid_rfc3339
from session_migrate.jsonl import encode_jsonl, file_sha256, iter_jsonl
from session_migrate.model import AgentFormat, Event, EventKind, Provenance, Role, Session

PINNED_COPILOT_VERSION = "1.0.70"
COPILOT_EVENT_VERSION = 1
MAX_NATIVE_BYTES = 256 * 1024 * 1024
MAX_SOURCE_JSON_NODES = 100_000
_EMITTED_TYPES = {
    "session.start",
    "user.message",
    "assistant.message",
    "tool.execution_start",
    "tool.execution_complete",
    "session.compaction_complete",
    "session.binary_asset",
}
_KNOWN_SOURCE_TYPES = {
    "abort",
    "assistant.intent",
    "assistant.message",
    "assistant.message_delta",
    "assistant.message_start",
    "assistant.reasoning",
    "assistant.reasoning_delta",
    "assistant.streaming_delta",
    "assistant.turn_end",
    "assistant.turn_start",
    "assistant.usage",
    "auto_mode_switch.completed",
    "auto_mode_switch.requested",
    "capabilities.changed",
    "command.completed",
    "command.execute",
    "command.queued",
    "commands.changed",
    "elicitation.completed",
    "elicitation.requested",
    "exit_plan_mode.completed",
    "exit_plan_mode.requested",
    "external_tool.completed",
    "external_tool.requested",
    "hook.end",
    "hook.progress",
    "hook.start",
    "mcp.oauth_completed",
    "mcp.oauth_required",
    "mcp_app.tool_call_complete",
    "model.call_failure",
    "pending_messages.modified",
    "permission.completed",
    "permission.requested",
    "sampling.completed",
    "sampling.requested",
    "session.autopilot_objective_changed",
    "session.background_tasks_changed",
    "session.binary_asset",
    "session.canvas.closed",
    "session.canvas.opened",
    "session.canvas.registry_changed",
    "session.compaction_complete",
    "session.compaction_start",
    "session.context_changed",
    "session.custom_agents_updated",
    "session.custom_notification",
    "session.error",
    "session.extensions.attachments_pushed",
    "session.extensions_loaded",
    "session.handoff",
    "session.idle",
    "session.info",
    "session.mcp_server_status_changed",
    "session.mcp_servers_loaded",
    "session.mode_changed",
    "session.model_change",
    "session.permissions_changed",
    "session.plan_changed",
    "session.remote_steerable_changed",
    "session.resume",
    "session.schedule_cancelled",
    "session.schedule_created",
    "session.shutdown",
    "session.skills_loaded",
    "session.snapshot_rewind",
    "session.start",
    "session.task_complete",
    "session.title_changed",
    "session.todos_changed",
    "session.tools_updated",
    "session.truncation",
    "session.usage_info",
    "session.warning",
    "session.workspace_file_changed",
    "skill.invoked",
    "subagent.completed",
    "subagent.deselected",
    "subagent.failed",
    "subagent.selected",
    "subagent.started",
    "system.message",
    "system.notification",
    "tool.execution_complete",
    "tool.execution_partial_result",
    "tool.execution_progress",
    "tool.execution_start",
    "tool.user_requested",
    "user.message",
    "user_input.completed",
    "user_input.requested",
}


@dataclass(frozen=True, slots=True)
class ParsedCopilotSession:
    """Portable projection and source metadata from a Copilot event log."""

    session_id: str
    cwd: Path | None
    started_at: str
    cli_version: str
    model: str | None
    title: str | None
    events: tuple[Event, ...]
    raw_record_count: int


def serialize(
    session: Session,
    *,
    session_id: str,
    cwd: Path,
    cli_version: str = PINNED_COPILOT_VERSION,
    model: str | None = None,
    timestamp: str | None = None,
) -> tuple[bytes, dict[str, int]]:
    """Serialize portable events into Copilot's public session event schema."""

    fallback_timestamp = valid_rfc3339(timestamp) or valid_rfc3339(session.started_at) or _utc_now()
    target_model = model or session.model or "unknown"
    dropped: Counter[str] = Counter()
    records: list[dict[str, Any]] = []
    parent_id: str | None = None
    last_timestamp: datetime | None = None
    generated_tool_ids: deque[str] = deque()
    seen_tool_call_ids: set[str] = set()
    seen_tool_result_ids: set[str] = set()
    available_tool_call_ids: Counter[str] = Counter()
    tool_names: dict[str, str] = {}
    tool_inputs: dict[str, Any] = {}
    emitted_assets: set[str] = set()

    def append_event(event_type: str, data: dict[str, Any], raw_timestamp: str) -> None:
        nonlocal parent_id, last_timestamp
        parsed = _parse_timestamp(raw_timestamp)
        if last_timestamp is not None and parsed < last_timestamp:
            parsed = last_timestamp + timedelta(microseconds=1)
            dropped["timestamp:native_order_adjusted"] += 1
        last_timestamp = parsed
        event_id = str(uuid.uuid4())
        records.append(
            {
                "type": event_type,
                "data": data,
                "id": event_id,
                "timestamp": _format_timestamp(parsed),
                "parentId": parent_id,
            }
        )
        parent_id = event_id

    def asset_reference(
        image_url: Any, event_timestamp: str, description: str
    ) -> dict[str, Any] | None:
        image = portable_data_image(image_url)
        if not image:
            return None
        mime_type, data = image
        decoded = base64.b64decode(data, validate=True)
        asset_id = f"sha256:{hashlib.sha256(decoded).hexdigest()}"
        if asset_id not in emitted_assets:
            append_event(
                "session.binary_asset",
                {
                    "assetId": asset_id,
                    "type": "image",
                    "mimeType": mime_type,
                    "byteLength": len(decoded),
                    "data": data,
                    "description": description,
                },
                event_timestamp,
            )
            emitted_assets.add(asset_id)
        return {
            "type": "image",
            "assetId": asset_id,
            "mimeType": mime_type,
            "byteLength": len(decoded),
            "description": description,
        }

    append_event(
        "session.start",
        {
            "sessionId": session_id,
            "version": COPILOT_EVENT_VERSION,
            "producer": "session-migrate",
            "copilotVersion": cli_version,
            "startTime": fallback_timestamp,
            "selectedModel": target_model,
            "context": {"cwd": str(cwd)},
            "alreadyInUse": False,
            "remoteSteerable": False,
        },
        fallback_timestamp,
    )

    pending_role: Role | None = None
    pending_record_index: int | None = None
    pending_timestamp: str | None = None
    pending_text: list[str] = []
    pending_attachments: list[dict[str, Any]] = []
    pending_tools: list[tuple[str, str, Any]] = []

    def flush_message() -> None:
        nonlocal pending_role, pending_record_index, pending_timestamp
        nonlocal pending_text, pending_attachments, pending_tools
        if pending_role is None:
            return
        if len(pending_text) > 1:
            dropped["message:native_text_blocks_grouped"] += len(pending_text) - 1
        event_timestamp = pending_timestamp or fallback_timestamp
        content = "\n".join(pending_text)
        if pending_role == Role.USER:
            if content or pending_attachments:
                data: dict[str, Any] = {"content": content}
                if pending_attachments:
                    data["attachments"] = pending_attachments
                append_event("user.message", data, event_timestamp)
        else:
            requests = [
                {
                    "toolCallId": call_id,
                    "name": name,
                    "arguments": arguments,
                    "type": "function",
                }
                for call_id, name, arguments in pending_tools
            ]
            if content or requests:
                data = {
                    "messageId": str(uuid.uuid4()),
                    "model": target_model,
                    "content": content,
                }
                if requests:
                    data["toolRequests"] = requests
                append_event("assistant.message", data, event_timestamp)
                for call_id, name, arguments in pending_tools:
                    append_event(
                        "tool.execution_start",
                        {
                            "toolCallId": call_id,
                            "toolName": name,
                            "arguments": arguments,
                            "model": target_model,
                        },
                        event_timestamp,
                    )
        pending_role = None
        pending_record_index = None
        pending_timestamp = None
        pending_text = []
        pending_attachments = []
        pending_tools = []

    def queue(event: Event, role: Role) -> None:
        nonlocal pending_role, pending_record_index, pending_timestamp
        if pending_role is not None and (
            pending_role != role or pending_record_index != event.provenance.record_index
        ):
            flush_message()
        pending_role = role
        pending_record_index = event.provenance.record_index
        pending_timestamp = pending_timestamp or _event_timestamp(
            event, fallback_timestamp, dropped
        )

    for event in session.events:
        if (
            event.kind == EventKind.MESSAGE
            and event.text
            and event.role
            in {
                Role.USER,
                Role.ASSISTANT,
            }
        ):
            if event.payload.get("ui_only_projection") is True:
                dropped["message:ui_only_projection"] += 1
            queue(event, event.role)
            pending_text.append(event.text)
            continue

        if (
            event.kind == EventKind.CONTEXT
            and event.role == Role.USER
            and event.payload.get("block_type") == "image"
        ):
            event_timestamp = _event_timestamp(event, fallback_timestamp, dropped)
            image = asset_reference(
                event.payload.get("image_url"), event_timestamp, "imported user image"
            )
            if image:
                queue(event, Role.USER)
                pending_attachments.append(
                    {
                        "type": "blob",
                        "assetId": image["assetId"],
                        "mimeType": image["mimeType"],
                        "byteLength": image["byteLength"],
                        "displayName": _image_display_name(image["mimeType"]),
                    }
                )
            else:
                dropped["context:image"] += 1
            continue

        if event.kind == EventKind.TOOL_CALL:
            call_id = event.tool_call_id
            if not call_id:
                call_id = f"call_session_migrate_{uuid.uuid4().hex}"
                generated_tool_ids.append(call_id)
                dropped["tool_call:missing_id"] += 1
            name = event.tool_name
            if not name:
                name = "unknown_tool"
                dropped["tool_call:missing_name"] += 1
            arguments = event.payload.get("input", {})
            if not isinstance(arguments, dict):
                arguments = {"input": arguments}
                dropped["tool_call:non_object_input"] += 1
            if call_id in seen_tool_call_ids:
                dropped["tool_call:duplicate_id"] += 1
            seen_tool_call_ids.add(call_id)
            available_tool_call_ids[call_id] += 1
            tool_names.setdefault(call_id, name)
            tool_inputs.setdefault(call_id, arguments)
            if event.payload.get("namespace"):
                dropped["tool_call:namespace"] += 1
            queue(event, Role.ASSISTANT)
            pending_tools.append((call_id, name, arguments))
            continue

        if event.kind == EventKind.TOOL_RESULT:
            flush_message()
            source_call_id = event.tool_call_id
            call_id = source_call_id
            if not call_id:
                call_id = (
                    generated_tool_ids.popleft()
                    if generated_tool_ids
                    else f"call_missing_{uuid.uuid4().hex}"
                )
                dropped["tool_result:missing_id"] += 1
            if available_tool_call_ids[call_id]:
                available_tool_call_ids[call_id] -= 1
            else:
                dropped["tool_result:orphan_id"] += 1
                call_id = f"call_session_migrate_orphan_{uuid.uuid4().hex}"
                name = event.tool_name or "unknown_tool"
                arguments: dict[str, Any] = {}
                append_event(
                    "assistant.message",
                    {
                        "messageId": str(uuid.uuid4()),
                        "model": target_model,
                        "content": "",
                        "toolRequests": [
                            {
                                "toolCallId": call_id,
                                "name": name,
                                "arguments": arguments,
                                "type": "function",
                            }
                        ],
                    },
                    _event_timestamp(event, fallback_timestamp, dropped),
                )
                append_event(
                    "tool.execution_start",
                    {
                        "toolCallId": call_id,
                        "toolName": name,
                        "arguments": arguments,
                        "model": target_model,
                    },
                    _event_timestamp(event, fallback_timestamp, dropped),
                )
                seen_tool_call_ids.add(call_id)
                tool_names[call_id] = name
                tool_inputs[call_id] = arguments
            if source_call_id and source_call_id in seen_tool_result_ids:
                dropped["tool_result:duplicate_id"] += 1
            if source_call_id:
                seen_tool_result_ids.add(source_call_id)
            content, contents, binary, omissions = _tool_result(event)
            dropped.update(omissions)
            is_error = event.payload.get("is_error") is True
            event_timestamp = _event_timestamp(event, fallback_timestamp, dropped)
            binary_references = [
                reference
                for item in binary
                if (
                    reference := asset_reference(
                        f"data:{item['mimeType']};base64,{item['data']}",
                        event_timestamp,
                        "imported tool image",
                    )
                )
            ]
            complete: dict[str, Any] = {
                "toolCallId": call_id,
                "success": not is_error,
                "model": target_model,
            }
            result: dict[str, Any] = {"content": content}
            if contents:
                result["contents"] = contents
            if binary_references:
                result["binaryResultsForLlm"] = binary_references
            if is_error:
                complete["error"] = {"message": content or "tool execution failed"}
                # Copilot's schema permits the original structured result next
                # to the error. Keep it for UI/media fidelity while the error
                # remains the model-facing completion status.
                complete["result"] = result
            else:
                complete["result"] = result
            append_event(
                "tool.execution_complete",
                complete,
                event_timestamp,
            )
            continue

        if event.kind == EventKind.COMPACTION and event.text:
            flush_message()
            append_event(
                "session.compaction_complete",
                {"success": True, "summaryContent": event.text},
                _event_timestamp(event, fallback_timestamp, dropped),
            )
            if event.payload.get("has_boundary_metadata") is True:
                dropped["compaction:boundary_metadata"] += 1
            continue

        dropped[_omission_key(event)] += 1

    flush_message()
    if not any(record["type"] == "user.message" for record in records):
        raise SessionMigrateError("Copilot target has no resumable user conversation history")
    return encode_jsonl(records), dict(sorted(dropped.items()))


def parse(path: Path) -> ParsedCopilotSession:
    """Parse a native Copilot event log into the portable projection."""

    records = [dict(item.value) for item in iter_jsonl(path)]
    _validate_source_records(records, path=path)
    first_data = records[0]["data"]
    assets = _asset_inventory(records)
    events: list[Event] = []
    referenced_assets: set[str] = set()
    emitted_calls: Counter[str] = Counter()
    tool_names: dict[str, str] = {}
    model = string(first_data.get("selectedModel"))
    title: str | None = None

    def opaque(index: int, record_type: str, reason: str, timestamp: str | None) -> None:
        events.append(
            Event(
                kind=EventKind.OPAQUE,
                timestamp=timestamp,
                payload={"reason": reason, "source_event_type": record_type},
                provenance=Provenance(index, record_type, string(records[index].get("id"))),
            )
        )

    for index, record in enumerate(records):
        record_type = record["type"]
        data = record["data"]
        timestamp = record["timestamp"]
        provenance = Provenance(index, record_type, record["id"])
        if string(record.get("agentId")):
            opaque(index, record_type, "copilot_subagent_scoped_event", timestamp)
            continue
        if record_type == "user.message":
            content = data["content"]
            if content:
                events.append(
                    Event(
                        kind=EventKind.MESSAGE,
                        role=Role.USER,
                        text=content,
                        timestamp=timestamp,
                        provenance=provenance,
                    )
                )
            transformed = string(data.get("transformedContent"))
            if transformed and transformed != content:
                opaque(index, record_type, "copilot_user_transformed_content", timestamp)
            for block_index, attachment in enumerate(data.get("attachments") or []):
                image_url = _attachment_image_url(attachment, assets)
                asset_id = string(attachment.get("assetId"))
                block_provenance = Provenance(index, record_type, record["id"], block_index)
                if image_url:
                    if asset_id:
                        referenced_assets.add(asset_id)
                    events.append(
                        Event(
                            kind=EventKind.CONTEXT,
                            role=Role.USER,
                            timestamp=timestamp,
                            payload={"block_type": "image", "image_url": image_url},
                            provenance=block_provenance,
                        )
                    )
                else:
                    attachment_type = string(attachment.get("type")) or "unknown"
                    events.append(
                        Event(
                            kind=EventKind.OPAQUE,
                            role=Role.USER,
                            timestamp=timestamp,
                            payload={
                                "reason": f"copilot_attachment_{attachment_type}",
                                "source_block_type": attachment_type,
                            },
                            provenance=block_provenance,
                        )
                    )
        elif record_type == "assistant.message":
            reasoning = string(data.get("reasoningText"))
            if reasoning:
                events.append(
                    Event(
                        kind=EventKind.THINKING,
                        role=Role.ASSISTANT,
                        text=reasoning,
                        timestamp=timestamp,
                        provenance=provenance,
                    )
                )
            if string(data.get("reasoningOpaque")):
                opaque(index, record_type, "copilot_reasoning_opaque", timestamp)
            if string(data.get("encryptedContent")):
                opaque(index, record_type, "copilot_encrypted_content", timestamp)
            if data["content"]:
                events.append(
                    Event(
                        kind=EventKind.MESSAGE,
                        role=Role.ASSISTANT,
                        text=data["content"],
                        timestamp=timestamp,
                        provenance=provenance,
                    )
                )
            for block_index, request in enumerate(data.get("toolRequests") or []):
                call_id = request["toolCallId"]
                tool_name = request["name"]
                emitted_calls[call_id] += 1
                tool_names[call_id] = tool_name
                payload: dict[str, Any] = {"input": request.get("arguments", {})}
                namespace = string(request.get("mcpServerName"))
                if namespace:
                    payload["namespace"] = namespace
                events.append(
                    Event(
                        kind=EventKind.TOOL_CALL,
                        role=Role.ASSISTANT,
                        tool_name=tool_name,
                        tool_call_id=call_id,
                        timestamp=timestamp,
                        payload=payload,
                        provenance=Provenance(
                            index, record_type, record["id"], block_index
                        ),
                    )
                )
            if data.get("citations") is not None:
                opaque(index, record_type, "copilot_assistant_citations", timestamp)
            if data.get("serverTools") is not None:
                opaque(index, record_type, "copilot_assistant_server_tools", timestamp)
        elif record_type == "tool.execution_start":
            call_id = data["toolCallId"]
            tool_name = data["toolName"]
            tool_names[call_id] = tool_name
            if emitted_calls[call_id]:
                emitted_calls[call_id] -= 1
            else:
                payload = {"input": data.get("arguments", {})}
                namespace = string(data.get("mcpServerName"))
                if namespace:
                    payload["namespace"] = namespace
                events.append(
                    Event(
                        kind=EventKind.TOOL_CALL,
                        role=Role.ASSISTANT,
                        tool_name=tool_name,
                        tool_call_id=call_id,
                        timestamp=timestamp,
                        payload=payload,
                        provenance=provenance,
                    )
                )
        elif record_type == "tool.execution_complete":
            call_id = data["toolCallId"]
            if data.get("isUserRequested") is True:
                opaque(index, record_type, "copilot_user_requested_tool_result", timestamp)
                continue
            result = data.get("result") if isinstance(data.get("result"), dict) else {}
            blocks, used_assets, omissions = _portable_source_result_blocks(result, assets)
            referenced_assets.update(used_assets)
            content = string(result.get("content"))
            error = data.get("error") if isinstance(data.get("error"), dict) else {}
            if data["success"] is False:
                content = string(error.get("message")) or content
            events.append(
                Event(
                    kind=EventKind.TOOL_RESULT,
                    role=Role.TOOL,
                    text=content,
                    tool_name=tool_names.get(call_id),
                    tool_call_id=call_id,
                    timestamp=timestamp,
                    payload={
                        "is_error": data["success"] is False,
                        "content_blocks": blocks,
                    },
                    provenance=provenance,
                )
            )
            for reason in omissions:
                opaque(index, record_type, reason, timestamp)
        elif record_type == "session.compaction_complete":
            summary = string(data.get("summaryContent"))
            if data.get("success") is True and summary:
                events.append(
                    Event(
                        kind=EventKind.COMPACTION,
                        role=Role.SYSTEM,
                        text=summary,
                        timestamp=timestamp,
                        provenance=provenance,
                    )
                )
            else:
                opaque(index, record_type, "copilot_compaction_without_summary", timestamp)
        elif record_type == "session.model_change":
            model = string(data.get("newModel")) or model
            opaque(index, record_type, "copilot_model_change_metadata", timestamp)
        elif record_type == "session.title_changed":
            title = string(data.get("title")) or title
        elif record_type == "system.message":
            opaque(index, record_type, "copilot_privileged_system_message", timestamp)
        elif record_type == "session.binary_asset":
            continue
        elif record_type != "session.start":
            opaque(index, record_type, "copilot_native_lifecycle_or_ui_event", timestamp)

    for index, record in enumerate(records):
        if record["type"] == "session.binary_asset":
            asset_id = record["data"]["assetId"]
            if asset_id not in referenced_assets:
                opaque(
                    index,
                    "session.binary_asset",
                    "copilot_unreferenced_binary_asset",
                    record["timestamp"],
                )
    title = title or _workspace_title(path)
    context = first_data.get("context")
    cwd_value = string(context.get("cwd")) if isinstance(context, dict) else None
    return ParsedCopilotSession(
        session_id=first_data["sessionId"],
        cwd=Path(cwd_value) if cwd_value else None,
        started_at=first_data["startTime"],
        cli_version=first_data["copilotVersion"],
        model=model,
        title=title,
        events=tuple(events),
        raw_record_count=len(records),
    )


def parse_session(path: Path) -> Session:
    """Parse Copilot's canonical ``events.jsonl`` as a first-class source."""

    parsed = parse(path)
    return Session(
        source_format=AgentFormat.COPILOT,
        source_path=path.resolve(),
        source_sha256=file_sha256(path),
        session_id=parsed.session_id,
        cwd=parsed.cwd,
        started_at=parsed.started_at,
        cli_version=parsed.cli_version,
        model=parsed.model,
        title=parsed.title,
        events=parsed.events,
        raw_record_count=parsed.raw_record_count,
        model_provider="github-copilot",
    )


def validate_native_bytes(data: bytes, session_id: str) -> None:
    """Validate a generated Copilot event log before installation."""

    _validate_generated_records(_decode_native_records(data), expected_session_id=session_id)


def workspace_bytes(
    *, session_id: str, cwd: Path, timestamp: str, title: str | None = None
) -> bytes:
    """Return Copilot's small picker/workspace sidecar as conservative YAML."""

    values: list[tuple[str, Any]] = [
        ("id", session_id),
        ("cwd", str(cwd)),
        ("client_name", "github/cli"),
    ]
    if title:
        values.extend((("name", title), ("user_named", True)))
    values.extend(
        (
            ("summary_count", 0),
            ("created_at", timestamp),
            ("updated_at", timestamp),
        )
    )
    lines = []
    for key, value in values:
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, int):
            rendered = str(value)
        else:
            rendered = json.dumps(value, ensure_ascii=False)
        lines.append(f"{key}: {rendered}")
    return ("\n".join(lines) + "\n").encode()


def session_relative_path(session_id: str) -> Path:
    return Path("session-state") / session_id / "events.jsonl"


def _decode_native_records(data: bytes) -> list[dict[str, Any]]:
    if len(data) > MAX_NATIVE_BYTES:
        raise SessionMigrateError("Copilot session exceeds the native artifact safety limit")
    records: list[dict[str, Any]] = []
    try:
        text = data.decode("utf-8")
        # JSONL records are delimited by LF. ``str.splitlines()`` also splits on
        # valid JSON string characters such as U+2028 and U+2029.
        for line_number, line in enumerate(text.split("\n"), start=1):
            if not line.strip():
                continue
            value = json.loads(line, parse_constant=_reject_json_constant)
            if not isinstance(value, dict):
                raise SessionMigrateError(f"Copilot record {line_number} is not a JSON object")
            records.append(value)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SessionMigrateError("generated Copilot session is not valid JSONL") from exc
    return records


def _validate_source_records(records: list[dict[str, Any]], *, path: Path) -> None:
    """Validate persisted 1.0.70 source envelopes and projected payloads."""

    if not records or records[0].get("type") != "session.start":
        raise SessionMigrateError("Copilot session must begin with session.start")
    first_data = records[0].get("data")
    if not isinstance(first_data, dict):
        raise SessionMigrateError("Copilot session.start data is invalid")
    session_id = string(first_data.get("sessionId"))
    if not session_id or not _is_uuid(session_id):
        raise SessionMigrateError("Copilot session ID is not a UUID")
    parent_name = path.parent.name
    if _is_uuid(parent_name) and parent_name != session_id:
        raise SessionMigrateError("Copilot session directory does not match session.start")
    if first_data.get("version") != COPILOT_EVENT_VERSION:
        raise SessionMigrateError("Copilot session event version is unsupported")
    if not all(string(first_data.get(key)) for key in ("producer", "copilotVersion")):
        raise SessionMigrateError("Copilot session.start is missing required metadata")
    if not valid_rfc3339(first_data.get("startTime")):
        raise SessionMigrateError("Copilot session start time is invalid")
    context = first_data.get("context")
    if context is not None:
        if not isinstance(context, dict):
            raise SessionMigrateError("Copilot working directory context is invalid")
        cwd = string(context.get("cwd"))
        if not cwd or "\0" in cwd:
            raise SessionMigrateError("Copilot working directory context is invalid")

    prior_id: str | None = None
    seen_ids: set[str] = set()
    last_time: datetime | None = None
    start_count = 0
    for index, record in enumerate(records):
        _ensure_json_bounds(record)
        record_type = string(record.get("type"))
        if record_type not in _KNOWN_SOURCE_TYPES:
            raise SessionMigrateError(f"unsupported Copilot source event: {record_type}")
        if record_type == "session.start":
            start_count += 1
            if index != 0:
                raise SessionMigrateError("Copilot session contains multiple session.start events")
        if record.get("ephemeral") is True:
            raise SessionMigrateError("Copilot persisted session contains an ephemeral event")
        agent_id = record.get("agentId")
        if agent_id is not None and not string(agent_id):
            raise SessionMigrateError("Copilot event agentId is invalid")
        event_id = string(record.get("id"))
        if not event_id or not _is_uuid4(event_id) or event_id in seen_ids:
            raise SessionMigrateError("Copilot event IDs must be unique UUIDv4 values")
        seen_ids.add(event_id)
        if record.get("parentId") != prior_id:
            raise SessionMigrateError("Copilot event parent chain is not linear")
        prior_id = event_id
        timestamp = valid_rfc3339(record.get("timestamp"))
        if not timestamp:
            raise SessionMigrateError("Copilot event timestamp is invalid")
        parsed_time = _parse_timestamp(timestamp)
        if last_time is not None and parsed_time < last_time:
            raise SessionMigrateError("Copilot event timestamps are not ordered")
        last_time = parsed_time
        data = record.get("data")
        if not isinstance(data, dict):
            raise SessionMigrateError("Copilot event data must be an object")
        _validate_source_payload(record_type, data)
    if start_count != 1:
        raise SessionMigrateError("Copilot session must contain one session.start event")
    _validate_source_references(records)


def _validate_source_payload(record_type: str, data: dict[str, Any]) -> None:
    if record_type == "user.message":
        if not isinstance(data.get("content"), str):
            raise SessionMigrateError("Copilot user message content is invalid")
        if data.get("transformedContent") is not None and not isinstance(
            data.get("transformedContent"), str
        ):
            raise SessionMigrateError("Copilot transformed user content is invalid")
        attachments = data.get("attachments", [])
        if not isinstance(attachments, list) or len(attachments) > 10_000:
            raise SessionMigrateError("Copilot attachments must be a bounded array")
        for attachment in attachments:
            if not isinstance(attachment, dict) or not string(attachment.get("type")):
                raise SessionMigrateError("Copilot attachment is invalid")
    elif record_type == "assistant.message":
        if not isinstance(data.get("content"), str) or not string(data.get("messageId")):
            raise SessionMigrateError("Copilot assistant message is invalid")
        for key in ("reasoningText", "reasoningOpaque", "encryptedContent"):
            if data.get(key) is not None and not isinstance(data.get(key), str):
                raise SessionMigrateError(f"Copilot assistant {key} is invalid")
        requests = data.get("toolRequests", [])
        if not isinstance(requests, list) or len(requests) > 10_000:
            raise SessionMigrateError("Copilot toolRequests must be a bounded array")
        for request in requests:
            if (
                not isinstance(request, dict)
                or not string(request.get("toolCallId"))
                or not string(request.get("name"))
            ):
                raise SessionMigrateError("Copilot tool request is missing linkage")
            arguments = request.get("arguments", {})
            if not isinstance(arguments, dict):
                raise SessionMigrateError("Copilot tool request arguments must be an object")
    elif record_type == "tool.execution_start":
        if not string(data.get("toolCallId")) or not string(data.get("toolName")):
            raise SessionMigrateError("Copilot tool start is missing linkage")
        if not isinstance(data.get("arguments", {}), dict):
            raise SessionMigrateError("Copilot tool start arguments must be an object")
    elif record_type == "tool.execution_complete":
        if not string(data.get("toolCallId")) or not isinstance(data.get("success"), bool):
            raise SessionMigrateError("Copilot tool result is missing linkage")
        if data["success"]:
            result = data.get("result")
            if not isinstance(result, dict) or not isinstance(result.get("content"), str):
                raise SessionMigrateError("successful Copilot tool result has no content")
        else:
            error = data.get("error")
            if not isinstance(error, dict) or not isinstance(error.get("message"), str):
                raise SessionMigrateError("failed Copilot tool result has no error message")
    elif record_type == "session.binary_asset":
        _validated_asset(data)
    elif record_type == "session.compaction_complete":
        if not isinstance(data.get("success"), bool):
            raise SessionMigrateError("Copilot compaction status is invalid")
        if data.get("summaryContent") is not None and not isinstance(
            data.get("summaryContent"), str
        ):
            raise SessionMigrateError("Copilot compaction summary is invalid")
    elif record_type == "session.model_change":
        if not string(data.get("newModel")):
            raise SessionMigrateError("Copilot model change is invalid")
    elif record_type == "session.title_changed":
        if not string(data.get("title")):
            raise SessionMigrateError("Copilot title change is invalid")
    elif record_type == "system.message":
        if not isinstance(data.get("content"), str) or data.get("role") not in {
            "system",
            "developer",
        }:
            raise SessionMigrateError("Copilot system message is invalid")


def _validate_source_references(records: list[dict[str, Any]]) -> None:
    assets: dict[str, tuple[str, str, int]] = {}
    for record in records:
        if record["type"] != "session.binary_asset":
            continue
        data = record["data"]
        asset_id, mime_type, encoded, byte_length = _validated_asset(data)
        if asset_id in assets:
            raise SessionMigrateError("Copilot binary asset ID is duplicated")
        assets[asset_id] = (mime_type, encoded, byte_length)

    for record in records:
        data = record["data"]
        references: list[dict[str, Any]] = []
        if record["type"] == "user.message":
            references.extend(data.get("attachments") or [])
        elif record["type"] == "tool.execution_complete":
            result = data.get("result")
            if isinstance(result, dict):
                binary = result.get("binaryResultsForLlm", [])
                if not isinstance(binary, list) or len(binary) > 10_000:
                    raise SessionMigrateError("Copilot binary results must be a bounded array")
                if any(not isinstance(item, dict) for item in binary):
                    raise SessionMigrateError("Copilot binary result is invalid")
                references.extend(binary)
        for reference in references:
            asset_id = string(reference.get("assetId"))
            if not asset_id:
                continue
            stored = assets.get(asset_id)
            if stored is None:
                raise SessionMigrateError("Copilot binary reference has no matching asset")
            mime_type, _, byte_length = stored
            reference_mime = reference.get("mimeType")
            reference_length = reference.get("byteLength")
            if (
                reference_mime is not None
                and reference_mime != mime_type
                or reference_length is not None
                and reference_length != byte_length
            ):
                raise SessionMigrateError("Copilot binary reference metadata does not match asset")


def _validated_asset(data: dict[str, Any]) -> tuple[str, str, str, int]:
    asset_id = string(data.get("assetId"))
    mime_type = string(data.get("mimeType"))
    encoded = string(data.get("data"))
    byte_length = data.get("byteLength")
    if not asset_id or not mime_type or not encoded or not isinstance(byte_length, int):
        raise SessionMigrateError("Copilot binary asset is invalid")
    if byte_length < 0:
        raise SessionMigrateError("Copilot binary asset has a negative length")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise SessionMigrateError("Copilot binary asset is not base64") from exc
    expected_id = f"sha256:{hashlib.sha256(decoded).hexdigest()}"
    if asset_id != expected_id or len(decoded) != byte_length:
        raise SessionMigrateError("Copilot binary asset integrity check failed")
    return asset_id, mime_type, encoded, byte_length


def _ensure_json_bounds(value: Any) -> None:
    stack: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_SOURCE_JSON_NODES or depth > 64:
            raise SessionMigrateError("Copilot record exceeds structural safety limits")
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)


def _validate_generated_records(
    records: list[dict[str, Any]], *, expected_session_id: str | None = None
) -> None:
    if not records or records[0].get("type") != "session.start":
        raise SessionMigrateError("Copilot session must begin with session.start")
    first_data = records[0].get("data")
    if not isinstance(first_data, dict):
        raise SessionMigrateError("Copilot session.start data is invalid")
    session_id = string(first_data.get("sessionId"))
    if not session_id or (expected_session_id and session_id != expected_session_id):
        raise SessionMigrateError("Copilot session ID does not match the target")
    try:
        uuid.UUID(session_id)
    except ValueError as exc:
        raise SessionMigrateError("Copilot session ID is not a UUID") from exc
    if first_data.get("version") != COPILOT_EVENT_VERSION:
        raise SessionMigrateError("Copilot session event version is unsupported")
    if not all(string(first_data.get(key)) for key in ("producer", "copilotVersion", "startTime")):
        raise SessionMigrateError("Copilot session.start is missing required metadata")
    context = first_data.get("context")
    if not isinstance(context, dict) or not string(context.get("cwd")):
        raise SessionMigrateError("Copilot session.start has no working directory")

    prior_id: str | None = None
    seen_ids: set[str] = set()
    last_time: datetime | None = None
    calls: Counter[str] = Counter()
    results: Counter[str] = Counter()
    assets: dict[str, tuple[str, str, int]] = {}
    user_count = 0
    for index, record in enumerate(records):
        record_type = string(record.get("type"))
        if record_type not in _EMITTED_TYPES:
            raise SessionMigrateError(f"unsupported generated Copilot event: {record_type}")
        event_id = string(record.get("id"))
        try:
            parsed_id = uuid.UUID(event_id or "")
        except ValueError as exc:
            raise SessionMigrateError("Copilot event ID is not a UUID") from exc
        if parsed_id.version != 4 or event_id in seen_ids:
            raise SessionMigrateError("Copilot event IDs must be unique UUIDv4 values")
        seen_ids.add(event_id)
        if record.get("parentId") != prior_id:
            raise SessionMigrateError("Copilot event parent chain is not linear")
        prior_id = event_id
        timestamp = valid_rfc3339(record.get("timestamp"))
        if not timestamp:
            raise SessionMigrateError("Copilot event timestamp is invalid")
        parsed_time = _parse_timestamp(timestamp)
        if last_time is not None and parsed_time < last_time:
            raise SessionMigrateError("Copilot event timestamps are not ordered")
        last_time = parsed_time
        data = record.get("data")
        if not isinstance(data, dict):
            raise SessionMigrateError("Copilot event data must be an object")
        if index == 0:
            continue
        if record_type == "session.binary_asset":
            asset_id = string(data.get("assetId"))
            mime_type = string(data.get("mimeType"))
            encoded = string(data.get("data"))
            byte_length = data.get("byteLength")
            if not asset_id or not mime_type or not encoded or not isinstance(byte_length, int):
                raise SessionMigrateError("Copilot binary asset is invalid")
            try:
                decoded = base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise SessionMigrateError("Copilot binary asset is not base64") from exc
            expected_id = f"sha256:{hashlib.sha256(decoded).hexdigest()}"
            if asset_id != expected_id or len(decoded) != byte_length or asset_id in assets:
                raise SessionMigrateError("Copilot binary asset integrity check failed")
            assets[asset_id] = (mime_type, encoded, byte_length)
        elif record_type == "user.message":
            if not isinstance(data.get("content"), str):
                raise SessionMigrateError("Copilot user message content is invalid")
            user_count += 1
            _validate_attachments(data.get("attachments", []), assets)
        elif record_type == "assistant.message":
            if not string(data.get("messageId")) or not isinstance(data.get("content"), str):
                raise SessionMigrateError("Copilot assistant message is invalid")
            requests = data.get("toolRequests", [])
            if not isinstance(requests, list):
                raise SessionMigrateError("Copilot toolRequests must be an array")
            for request in requests:
                if not isinstance(request, dict):
                    raise SessionMigrateError("Copilot tool request is invalid")
                call_id = string(request.get("toolCallId"))
                if not call_id or not string(request.get("name")):
                    raise SessionMigrateError("Copilot tool request is missing linkage")
                calls[call_id] += 1
        elif record_type == "tool.execution_start":
            if not string(data.get("toolCallId")) or not string(data.get("toolName")):
                raise SessionMigrateError("Copilot tool start is missing linkage")
        elif record_type == "tool.execution_complete":
            call_id = string(data.get("toolCallId"))
            if not call_id or not isinstance(data.get("success"), bool):
                raise SessionMigrateError("Copilot tool result is missing linkage")
            results[call_id] += 1
            if data["success"] and not isinstance(data.get("result"), dict):
                raise SessionMigrateError("successful Copilot tool result has no result data")
            if not data["success"] and not isinstance(data.get("error"), dict):
                raise SessionMigrateError("failed Copilot tool result has no error data")
            result = data.get("result")
            if isinstance(result, dict):
                references = result.get("binaryResultsForLlm", [])
                if not isinstance(references, list):
                    raise SessionMigrateError("Copilot binary results must be an array")
                for reference in references:
                    if (
                        not isinstance(reference, dict)
                        or string(reference.get("assetId")) not in assets
                    ):
                        raise SessionMigrateError("Copilot binary result reference is invalid")
        elif record_type == "session.compaction_complete":
            if data.get("success") is not True or not string(data.get("summaryContent")):
                raise SessionMigrateError("Copilot compaction summary is invalid")
    if not user_count:
        raise SessionMigrateError("Copilot session has no resumable conversation history")
    for call_id, count in results.items():
        if count > calls[call_id]:
            raise SessionMigrateError("Copilot tool result has no preceding tool request")


def _validate_attachments(value: Any, assets: dict[str, tuple[str, str, int]]) -> None:
    if not isinstance(value, list):
        raise SessionMigrateError("Copilot attachments must be an array")
    for attachment in value:
        if not isinstance(attachment, dict) or attachment.get("type") != "blob":
            raise SessionMigrateError("generated Copilot attachment is unsupported")
        image = _attachment_image_url(attachment, assets)
        if not image:
            raise SessionMigrateError("generated Copilot image attachment is invalid")


def _image_display_name(mime_type: str) -> str:
    extension = {"image/jpeg": "jpg"}.get(mime_type, mime_type.split("/", 1)[1])
    return f"imported-image.{extension}"


def _attachment_image_url(
    value: dict[str, Any], assets: dict[str, tuple[str, str, int]]
) -> str | None:
    data = string(value.get("data"))
    mime_type = string(value.get("mimeType"))
    asset_id = string(value.get("assetId"))
    if asset_id and asset_id in assets:
        stored_mime, stored_data, _ = assets[asset_id]
        if mime_type and mime_type != stored_mime:
            return None
        mime_type = stored_mime
        data = stored_data
    if not data or not mime_type:
        return None
    candidate = f"data:{mime_type};base64,{data}"
    return candidate if portable_data_image(candidate) else None


def _asset_inventory(records: list[dict[str, Any]]) -> dict[str, tuple[str, str, int]]:
    result: dict[str, tuple[str, str, int]] = {}
    for record in records:
        if record.get("type") != "session.binary_asset":
            continue
        data = record.get("data")
        if not isinstance(data, dict):
            continue
        asset_id = string(data.get("assetId"))
        mime_type = string(data.get("mimeType"))
        encoded = string(data.get("data"))
        byte_length = data.get("byteLength")
        if asset_id and mime_type and encoded and isinstance(byte_length, int):
            result[asset_id] = (mime_type, encoded, byte_length)
    return result


def _tool_result(
    event: Event,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], Counter[str]]:
    source = event.payload.get("content_blocks")
    blocks = source if isinstance(source, list) else []
    contents: list[dict[str, Any]] = []
    binary: list[dict[str, Any]] = []
    text_parts: list[str] = []
    omitted: Counter[str] = Counter()
    for block in blocks:
        if not isinstance(block, dict):
            omitted["tool_result:malformed_block"] += 1
            continue
        block_type = string(block.get("type"))
        if block_type in {"text", "input_text", "output_text"}:
            text = string(block.get("text"))
            if text:
                text_parts.append(text)
                contents.append({"type": "text", "text": text})
            else:
                omitted["tool_result:malformed_text"] += 1
        elif block_type in {"image", "input_image"}:
            image = portable_data_image(block.get("image_url") or block.get("url"))
            if not image:
                omitted["tool_result:image"] += 1
                continue
            mime_type, data = image
            contents.append({"type": "image", "data": data, "mimeType": mime_type})
            binary.append(
                {
                    "type": "image",
                    "data": data,
                    "mimeType": mime_type,
                    "description": "imported tool image",
                }
            )
            # The native timeline retains the exact asset. Whether it is
            # supplied back to the model depends on the selected provider's
            # tool-result media protocol (OpenAI completions omits it).
            omitted["tool_result:image_provider_dependent"] += 1
        else:
            omitted[f"tool_result:{block_type or 'unknown_block'}"] += 1
    if not text_parts and event.text:
        text_parts.append(event.text)
        contents.insert(0, {"type": "text", "text": event.text})
    return "\n".join(text_parts), contents, binary, omitted


def _portable_source_result_blocks(
    result: dict[str, Any], assets: dict[str, tuple[str, str, int]]
) -> tuple[list[dict[str, Any]], set[str], list[str]]:
    blocks: list[dict[str, Any]] = []
    used_assets: set[str] = set()
    omissions: list[str] = []
    seen_images: set[str] = set()
    content = string(result.get("content"))
    if content:
        blocks.append({"type": "text", "text": content})

    binary = result.get("binaryResultsForLlm")
    if isinstance(binary, list):
        for item in binary:
            assert isinstance(item, dict)
            image_url, asset_id = _binary_image_url(item, assets)
            if image_url:
                if image_url not in seen_images:
                    blocks.append({"type": "image", "image_url": image_url})
                    seen_images.add(image_url)
                if asset_id:
                    used_assets.add(asset_id)
            else:
                reason = (
                    "copilot_tool_binary_omitted"
                    if string(item.get("omittedReason"))
                    else "copilot_tool_binary_unsupported"
                )
                omissions.append(reason)

    contents = result.get("contents")
    if isinstance(contents, list):
        for item in contents:
            if not isinstance(item, dict):
                omissions.append("copilot_tool_content_malformed")
                continue
            item_type = string(item.get("type"))
            if not content and item_type in {"text", "terminal"}:
                text = string(item.get("text"))
                if text:
                    blocks.append({"type": "text", "text": text})
                else:
                    omissions.append("copilot_tool_content_malformed")
            elif item_type == "image":
                image_url, _ = _binary_image_url(item, assets)
                if image_url and image_url not in seen_images:
                    blocks.append({"type": "image", "image_url": image_url})
                    seen_images.add(image_url)
                elif not image_url:
                    omissions.append("copilot_tool_image_malformed")
            elif item_type not in {"text", "terminal"}:
                omissions.append(f"copilot_tool_content_{item_type or 'unknown'}")
    for key, reason in (
        ("citableSources", "copilot_tool_citable_sources"),
        ("structuredContent", "copilot_tool_structured_content"),
        ("uiResource", "copilot_tool_ui_resource"),
    ):
        if result.get(key) is not None:
            omissions.append(reason)
    detailed = string(result.get("detailedContent"))
    if detailed and detailed != content:
        omissions.append("copilot_tool_detailed_content")
    return blocks, used_assets, omissions


def _binary_image_url(
    value: dict[str, Any], assets: dict[str, tuple[str, str, int]]
) -> tuple[str | None, str | None]:
    if value.get("type") != "image":
        return None, None
    asset_id = string(value.get("assetId"))
    image_url = _attachment_image_url(value, assets)
    return image_url, asset_id if image_url and asset_id else None


def _workspace_title(path: Path) -> str | None:
    workspace = path.parent / "workspace.yaml"
    if not workspace.is_file():
        return None
    try:
        raw = workspace.read_bytes()
    except OSError as exc:
        raise SessionMigrateError("cannot read Copilot workspace metadata") from exc
    if len(raw) > 64 * 1024:
        raise SessionMigrateError("Copilot workspace metadata exceeds the safety limit")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SessionMigrateError("Copilot workspace metadata is not UTF-8") from exc
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() == "name":
            candidate = value.strip()
            if candidate.startswith('"'):
                try:
                    decoded = json.loads(candidate)
                except json.JSONDecodeError as exc:
                    raise SessionMigrateError("Copilot workspace title is invalid") from exc
                candidate = decoded if isinstance(decoded, str) else ""
            if not candidate or len(candidate) > 4096 or "\0" in candidate:
                raise SessionMigrateError("Copilot workspace title is invalid")
            return candidate
    return None


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _is_uuid4(value: str) -> bool:
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return parsed.version == 4


def _event_timestamp(event: Event, fallback: str, dropped: Counter[str]) -> str:
    timestamp = valid_rfc3339(event.timestamp)
    if event.timestamp and not timestamp:
        dropped["timestamp:invalid"] += 1
    return timestamp or fallback


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(UTC)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _omission_key(event: Event) -> str:
    if event.kind == EventKind.MESSAGE and event.role not in {Role.USER, Role.ASSISTANT}:
        return "message:privileged_role"
    if event.kind == EventKind.CONTEXT and event.role not in {Role.USER, None}:
        return "context:privileged_image"
    if event.kind == EventKind.OPAQUE:
        reason = string(event.payload.get("reason"))
        return f"opaque:{reason}" if reason else "opaque"
    return event.kind.value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
