"""Public OpenCode 2.0 transfer schema support; no private database access.

Legacy bundles are an internal portable projection only. Native v2 files always
use SessionTransfer.Data (flat messages), not legacy message info/parts.
"""

from __future__ import annotations

import base64
import copy
import re
from typing import Any

from session_migrate.errors import SessionMigrateError

_VERSION = re.compile(r"(?:opencode v)?(2\.0\.\d+(?:[-+][0-9A-Za-z.-]+)?)")
VALIDATED_VERSION = "2.0.23"
VALIDATED_VERSIONS = frozenset({"2.0.22", VALIDATED_VERSION})


def version(value: str) -> str | None:
    match = _VERSION.fullmatch(value)
    return match.group(1) if match else None


def is_bundle(value: dict[str, Any]) -> bool:
    return isinstance(value.get("info"), dict) and "location" in value["info"]


def _file(part: dict[str, Any]) -> dict[str, Any]:
    uri = part["url"]
    if not uri.startswith("data:") or ";base64," not in uri:
        raise SessionMigrateError("OpenCode v2 inline images must have a base64 data URI")
    return {"data": uri.split(";base64,", 1)[1], "mime": part["mime"], "source": {"type": "inline"}}


def from_legacy(bundle: dict[str, Any], dropped: Any) -> dict[str, Any]:
    old = bundle["info"]
    ref: dict[str, Any] = {}
    messages: list[dict[str, Any]] = []
    compact: set[str] = set()
    for message in bundle["messages"]:
        info, parts = message["info"], message["parts"]
        common = {"id": info["id"], "time": info["time"]}
        if info["role"] == "user":
            ref = {"id": info["model"]["modelID"], "providerID": info["model"]["providerID"]}
            if any(part["type"] == "compaction" for part in parts):
                compact.add(info["id"])
                continue
            native = {
                **common,
                "type": "user",
                "text": "\n".join(part["text"] for part in parts if part["type"] == "text"),
            }
            files = [_file(part) for part in parts if part["type"] == "file"]
            if files:
                native["files"] = files
        else:
            ref = {"id": info["modelID"], "providerID": info["providerID"]}
            if info.get("summary") and info["parentID"] in compact:
                messages.append(
                    {
                        **common,
                        "type": "compaction",
                        "status": "completed",
                        "reason": "auto",
                        "model": ref,
                        "summary": "\n".join(p["text"] for p in parts if p["type"] == "text"),
                        "recent": "",
                    }
                )
                continue
            content: list[dict[str, Any]] = []
            for part in parts:
                kind = part["type"]
                if kind in {"text", "reasoning"}:
                    content.append({"type": kind, "text": part["text"]})
                elif kind == "tool":
                    state = part["state"]
                    status = state["status"]
                    tool_state: dict[str, Any] = {"status": status, "input": state["input"]}
                    if status == "completed":
                        tool_state["content"] = [{"type": "text", "text": state["output"]}]
                        tool_state["content"].extend(
                            {"type": "file", "uri": p["url"], "mime": p["mime"]}
                            for p in state.get("attachments", [])
                        )
                    elif status == "error":
                        tool_state["error"] = {"type": "ToolError", "message": state["error"]}
                    else:
                        # Native export/import discards unsettled assistant steps. Archive
                        # incomplete calls as explicit errors so their input survives.
                        tool_state = {
                            "status": "error",
                            "input": state["input"],
                            "error": {
                                "type": "ImportedIncompleteTool",
                                "message": "Imported call had no completed result",
                            },
                        }
                        dropped["tool_call:v2_incomplete_archived"] += 1
                    times = state.get("time", {})
                    content.append(
                        {
                            "type": "tool",
                            "id": part["callID"],
                            "name": part["tool"],
                            "state": tool_state,
                            "time": {
                                "created": times.get("start", info["time"]["created"]),
                                **({"completed": times["end"]} if "end" in times else {}),
                            },
                        }
                    )
            native = {
                **common,
                "type": "assistant",
                "agent": info["agent"],
                "model": ref,
                "content": content,
                "finish": info["finish"],
                "cost": info["cost"],
                "tokens": info["tokens"],
            }
        messages.append(native)
    return {
        "info": {
            "id": old["id"],
            "projectID": old["projectID"],
            "title": old["title"],
            "agent": next(
                (m.get("agent", "build") for m in reversed(messages) if m.get("agent")), "build"
            ),
            "model": ref,
            "location": {"directory": old["directory"]},
            "cost": 0,
            "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
            "time": old["time"],
        },
        "messages": messages,
    }


def _to_legacy(bundle: dict[str, Any]) -> dict[str, Any]:
    """Validate the v2 portable content projection and reuse the mature reader."""
    from session_migrate.formats import opencode

    info = bundle["info"]
    _validate_native(bundle)
    if not isinstance(info.get("location"), dict):
        raise SessionMigrateError("OpenCode v2 session is missing location")
    opencode._validate_tokens(info.get("tokens"), "v2 session")
    if not opencode._is_finite_number(info.get("cost")):
        raise SessionMigrateError("OpenCode v2 session has invalid cost")
    model = info.get("model") or {"id": "unknown", "providerID": "unknown"}
    messages = bundle.get("messages")
    if not isinstance(messages, list) or len(messages) > opencode.MAX_NATIVE_MESSAGES:
        raise SessionMigrateError("OpenCode v2 session has invalid messages")
    output: list[dict[str, Any]] = []
    ids: set[str] = set()
    latest_user = "msg_import_root"
    part_index = 0

    def part(data: dict[str, Any], message_id: str) -> dict[str, Any]:
        nonlocal part_index
        part_index += 1
        if part_index > opencode.MAX_NATIVE_PARTS:
            raise SessionMigrateError("OpenCode v2 session contains too much content")
        return {
            **data,
            "id": f"prt_v2_projection_{part_index}",
            "sessionID": info["id"],
            "messageID": message_id,
        }

    for message in messages:
        if not isinstance(message, dict):
            raise SessionMigrateError("OpenCode v2 session contains a malformed message")
        mid = message.get("id")
        if not isinstance(mid, str) or not mid.startswith("msg_") or mid in ids:
            raise SessionMigrateError("OpenCode v2 session has invalid message IDs")
        ids.add(mid)
        clock = message.get("time")
        if not isinstance(clock, dict):
            raise SessionMigrateError("OpenCode v2 message is missing time")
        opencode._iso_from_ms(clock.get("created"))
        kind = message.get("type")
        if kind == "model-switched":
            model = message["model"]
            continue
        if kind == "compaction":
            if message.get("status") != "completed":
                continue
            # A provider-private checkpoint without portable text must not retire
            # earlier readable history when replayed in another harness.
            if not message["summary"].strip() and not message["recent"].strip():
                continue
            synthetic_id = mid + "_compaction"
            output.append(
                {
                    "info": {
                        "id": synthetic_id,
                        "sessionID": info["id"],
                        "role": "user",
                        "time": clock,
                        "agent": "build",
                        "model": {"modelID": model["id"], "providerID": model["providerID"]},
                    },
                    "parts": [
                        part(
                            {"type": "compaction", "auto": message.get("reason") == "auto"},
                            synthetic_id,
                        )
                    ],
                }
            )
            latest_user = synthetic_id
            kind = "assistant"
            message = {
                **message,
                "model": message.get("model") or model,
                "agent": "build",
                "content": [
                    {
                        "type": "text",
                        "text": message.get("summary", "")
                        + (
                            "\n\nRecent context:\n" + message["recent"]
                            if message.get("recent")
                            else ""
                        ),
                    }
                ],
                "_summary": True,
            }
        if kind not in {"user", "assistant"}:
            continue
        parts: list[dict[str, Any]] = []
        common = {"id": mid, "sessionID": info["id"], "role": kind, "time": clock}
        if kind == "user":
            if not isinstance(message.get("text"), str):
                raise SessionMigrateError("OpenCode v2 user message has invalid text")
            parts.append(part({"type": "text", "text": message["text"]}, mid))
            for attachment in message.get("files", []):
                if not isinstance(attachment, dict) or not isinstance(attachment.get("data"), str):
                    raise SessionMigrateError("OpenCode v2 file attachment is invalid")
                try:
                    base64.b64decode(attachment["data"], validate=True)
                except ValueError as exc:
                    raise SessionMigrateError("OpenCode v2 file attachment is not base64") from exc
                parts.append(
                    part(
                        {
                            "type": "file",
                            "mime": attachment["mime"],
                            "url": f"data:{attachment['mime']};base64,{attachment['data']}",
                        },
                        mid,
                    )
                )
            common.update(
                {
                    "agent": "build",
                    "model": {"modelID": model["id"], "providerID": model["providerID"]},
                }
            )
            latest_user = mid
        else:
            model = message.get("model", model)
            if not isinstance(message.get("content"), list):
                raise SessionMigrateError("OpenCode v2 assistant message has invalid content")
            for content in message["content"]:
                if not isinstance(content, dict):
                    raise SessionMigrateError("OpenCode v2 assistant content is malformed")
                ptype = content.get("type")
                if ptype in {"text", "reasoning"}:
                    parts.append(
                        part(
                            {
                                "type": ptype,
                                "text": content.get("text"),
                                **(
                                    {"time": {"start": clock["created"], "end": clock["created"]}}
                                    if ptype == "reasoning"
                                    else {}
                                ),
                            },
                            mid,
                        )
                    )
                elif ptype == "tool":
                    state = copy.deepcopy(content.get("state"))
                    if not isinstance(state, dict):
                        raise SessionMigrateError("OpenCode v2 tool state is invalid")
                    status = state.get("status")
                    if status == "completed":
                        blocks = state.pop("content", None)
                        if not isinstance(blocks, list) or not blocks:
                            raise SessionMigrateError("OpenCode v2 completed tool needs content")
                        state["output"] = "\n".join(
                            b["text"] for b in blocks if b.get("type") == "text"
                        )
                        state["title"] = content["name"]
                        state["metadata"] = state.get("metadata", {})
                        attachments = [
                            part({"type": "file", "mime": b["mime"], "url": b["uri"]}, mid)
                            for b in blocks
                            if b.get("type") == "file"
                        ]
                        if attachments:
                            state["attachments"] = attachments
                    elif status == "error":
                        error = state.get("error")
                        if not isinstance(error, dict) or not isinstance(error.get("message"), str):
                            raise SessionMigrateError("OpenCode v2 tool error is invalid")
                        state["error"] = error["message"]
                    elif status == "streaming":
                        state = {
                            "status": "pending",
                            "input": {"input": state["input"]},
                            "raw": state["input"],
                        }
                    tool_time = content.get("time", clock)
                    state["time"] = {
                        "start": tool_time.get("created", clock["created"]),
                        "end": tool_time.get("completed", clock["created"]),
                    }
                    parts.append(
                        part(
                            {
                                "type": "tool",
                                "callID": content.get("id"),
                                "tool": content.get("name"),
                                "state": state,
                            },
                            mid,
                        )
                    )
                else:
                    raise SessionMigrateError("OpenCode v2 has unsupported assistant content")
            common.update(
                {
                    "parentID": latest_user,
                    "modelID": model["id"],
                    "providerID": model["providerID"],
                    "agent": message.get("agent", "build"),
                    "mode": message.get("agent", "build"),
                    "path": {
                        "cwd": info["location"]["directory"],
                        "root": info["location"]["directory"],
                    },
                    "cost": message.get("cost", 0),
                    "tokens": message.get("tokens", info["tokens"]),
                    "finish": message.get("finish", "stop"),
                }
            )
            if message.get("_summary"):
                common["summary"] = True
        output.append({"info": common, "parts": parts})
    return {
        "info": {
            "id": info["id"],
            "directory": info["location"].get("directory"),
            "title": info.get("title") or "Imported session",
            "slug": "v2-export",
            "projectID": info.get("projectID"),
            "version": "2.0",
            "time": info.get("time"),
            **({"parentID": info["parentID"]} if "parentID" in info else {}),
        },
        "messages": output,
    }


_MESSAGE_FIELDS = {
    "user": {"text", "files", "agents", "skills"},
    "assistant": {
        "agent",
        "model",
        "content",
        "snapshot",
        "finish",
        "rawFinish",
        "providerState",
        "cost",
        "tokens",
        "error",
        "retry",
    },
    "compaction": {
        "status",
        "reason",
        "summary",
        "recent",
        "model",
        "providerState",
        "providerContext",
        "cost",
        "tokens",
        "error",
    },
    "agent-switched": {"agent", "previous"},
    "model-switched": {"model", "previous"},
    "location-switched": {"location", "projectID", "subpath", "previous"},
    "synthetic": {"text", "description"},
    "system": {"text", "description"},
    "skill": {"skill", "name", "text"},
    "shell": {"shellID", "command", "status", "exit", "output"},
    "idle": {"outcome"},
}


def loss_events(bundle: dict[str, Any]) -> list[Any]:
    """Account for provider-private state and nonportable v2 control records."""
    from session_migrate.formats.opencode import _iso_from_ms
    from session_migrate.model import Event, EventKind, Provenance

    events = []
    if (bundle["info"].get("model") or {}).get("variant"):
        events.append(
            Event(
                kind=EventKind.OPAQUE,
                timestamp=_iso_from_ms(bundle["info"]["time"]["created"]),
                payload={"reason": "opencode_v2_session_model_variant"},
                provenance=Provenance(0, "session"),
            )
        )
    for name in ("metadata", "permissions", "revert", "fork"):
        if bundle["info"].get(name):
            events.append(
                Event(
                    kind=EventKind.OPAQUE,
                    timestamp=_iso_from_ms(bundle["info"]["time"]["created"]),
                    payload={"reason": f"opencode_v2_session_{name}"},
                    provenance=Provenance(0, "session"),
                )
            )
    for index, message in enumerate(bundle["messages"]):
        reasons = []
        kind = message.get("type")
        if kind not in {"user", "assistant", "compaction"}:
            reasons.append(f"opencode_v2_{kind}_record")
        elif kind == "compaction" and message.get("status") != "completed":
            reasons.append(f"opencode_v2_compaction_{message.get('status')}")
        elif (
            kind == "compaction"
            and not message["summary"].strip()
            and not message["recent"].strip()
        ):
            reasons.append("opencode_v2_compaction_empty_portable_summary")
        if isinstance(message.get("model"), dict) and message["model"].get("variant"):
            reasons.append("opencode_v2_message_model_variant")
        for name in ("providerState", "providerContext", "metadata", "snapshot", "retry", "error"):
            if message.get(name):
                reasons.append(f"opencode_v2_{name}")
        for name in ("agents", "skills"):
            if message.get(name):
                reasons.append(f"opencode_v2_user_{name}")
        if kind == "user":
            for attachment in message.get("files", []):
                if (
                    any(attachment.get(name) for name in ("name", "description", "mention"))
                    or attachment.get("source", {}).get("type") == "uri"
                ):
                    reasons.append("opencode_v2_file_metadata")
        allowed = _MESSAGE_FIELDS.get(str(kind), set()) | {"id", "metadata", "time", "type"}
        if set(message) - allowed:
            reasons.append("opencode_v2_unknown_message_fields")
        for content in message["content"] if kind == "assistant" else []:
            allowed_content = (
                {"type", "text", "state", "time"}
                if content.get("type") != "tool"
                else {
                    "type",
                    "id",
                    "name",
                    "executed",
                    "providerState",
                    "providerResultState",
                    "state",
                    "time",
                }
            )
            if set(content) - allowed_content:
                reasons.append("opencode_v2_unknown_assistant_content_fields")
            if content.get("type") == "tool":
                for block in content["state"].get("content", []):
                    if block.get("type") == "file" and block.get("name") is not None:
                        reasons.append("opencode_v2_tool_file_metadata")
            if (
                content.get("type") == "tool"
                and content.get("state", {}).get("status") == "error"
                and content["state"].get("content")
            ):
                reasons.append("opencode_v2_error_tool_content")
            for name in ("state", "providerState", "providerResultState"):
                if content.get(name) and (name != "state" or content.get("type") != "tool"):
                    reasons.append(f"opencode_v2_content_{name}")
        for reason in reasons:
            events.append(
                Event(
                    kind=EventKind.OPAQUE,
                    timestamp=_iso_from_ms(message["time"]["created"]),
                    payload={"reason": reason},
                    provenance=Provenance(index, str(kind)),
                )
            )
    return events


def to_legacy(bundle: dict[str, Any]) -> dict[str, Any]:
    try:
        return _to_legacy(bundle)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise SessionMigrateError(
            "OpenCode v2 transfer has malformed required metadata/content"
        ) from exc


def _validate_native(bundle: dict[str, Any]) -> None:
    """Check portable v2 invariants before projection can erase native fields."""
    from session_migrate.formats.opencode import (
        _is_finite_number,
        _is_non_negative_int,
        _validate_tokens,
    )

    def ref(value: Any) -> None:
        if not isinstance(value, dict) or not all(
            isinstance(value.get(k), str) for k in ("id", "providerID")
        ):
            raise SessionMigrateError("OpenCode v2 has invalid model reference")
        if value.get("variant") is not None and not isinstance(value["variant"], str):
            raise SessionMigrateError("OpenCode v2 has invalid model variant")

    def error(value: Any) -> None:
        if not isinstance(value, dict) or not all(
            isinstance(value.get(key), str) for key in ("type", "message")
        ):
            raise SessionMigrateError("OpenCode v2 has invalid structured error")
        status = value.get("status")
        if status is not None and (not _is_non_negative_int(status) or not 100 <= status <= 599):
            raise SessionMigrateError("OpenCode v2 has invalid error status")
        response = value.get("response")
        if response is not None and (
            not isinstance(response, dict) or not isinstance(response.get("body"), str)
        ):
            raise SessionMigrateError("OpenCode v2 has invalid error response")

    info = bundle["info"]
    location = info.get("location")
    directory = location.get("directory") if isinstance(location, dict) else None
    if not isinstance(directory, str) or not directory or "\x00" in directory:
        raise SessionMigrateError("OpenCode v2 has invalid location directory")
    if location.get("workspaceID") is not None and not isinstance(location["workspaceID"], str):
        raise SessionMigrateError("OpenCode v2 has invalid location workspace")
    if info.get("metadata") is not None and not isinstance(info["metadata"], dict):
        raise SessionMigrateError("OpenCode v2 has invalid session metadata")
    if info.get("outcome") is not None and info["outcome"] not in {
        "succeeded",
        "failed",
        "interrupted",
    }:
        raise SessionMigrateError("OpenCode v2 has invalid session outcome")
    permissions = info.get("permissions")
    if permissions is not None and (
        not isinstance(permissions, list)
        or any(
            not isinstance(rule, dict)
            or not isinstance(rule.get("action"), str)
            or not isinstance(rule.get("resource"), str)
            or rule.get("effect") not in {"allow", "deny", "ask"}
            for rule in permissions
        )
    ):
        raise SessionMigrateError("OpenCode v2 has invalid session permissions")
    fork = info.get("fork")
    if fork is not None:
        boundary = fork.get("boundary") if isinstance(fork, dict) else None
        if (
            not isinstance(fork, dict)
            or not isinstance(fork.get("sessionID"), str)
            or not fork["sessionID"].startswith("ses_")
            or not isinstance(boundary, dict)
            or boundary.get("type") not in {"before", "through"}
            or not isinstance(boundary.get("messageID"), str)
            or not boundary["messageID"].startswith("msg_")
        ):
            raise SessionMigrateError("OpenCode v2 has invalid session fork")
    revert = info.get("revert")
    if revert is not None and (
        not isinstance(revert, dict)
        or not isinstance(revert.get("messageID"), str)
        or not revert["messageID"].startswith("msg_")
    ):
        raise SessionMigrateError("OpenCode v2 has invalid session revert")
    if info.get("model") is not None:
        ref(info["model"])
    if info.get("agent") is not None and not isinstance(info["agent"], str):
        raise SessionMigrateError("OpenCode v2 has invalid session agent")
    for m in bundle.get("messages", []):
        if not isinstance(m, dict):
            raise SessionMigrateError("OpenCode v2 has malformed message")
        if m.get("metadata") is not None and not isinstance(m["metadata"], dict):
            raise SessionMigrateError("OpenCode v2 has invalid message metadata")
        if m.get("cost") is not None and not _is_finite_number(m["cost"]):
            raise SessionMigrateError("OpenCode v2 has invalid message cost")
        if m.get("tokens") is not None:
            _validate_tokens(m["tokens"], "v2 message")
        if m.get("error") is not None:
            error(m["error"])
        kind = m.get("type")
        if kind not in {
            "user",
            "assistant",
            "compaction",
            "agent-switched",
            "model-switched",
            "location-switched",
            "synthetic",
            "system",
            "skill",
            "shell",
            "idle",
        }:
            raise SessionMigrateError("OpenCode v2 has unsupported message type")
        clock = m.get("time")
        if not isinstance(clock, dict) or not _is_non_negative_int(clock.get("created")):
            raise SessionMigrateError("OpenCode v2 has invalid message time")
        if clock.get("completed") is not None and not _is_non_negative_int(clock["completed"]):
            raise SessionMigrateError("OpenCode v2 has invalid completion time")
        if kind in {"assistant", "model-switched"} or (
            kind == "compaction" and m.get("model") is not None
        ):
            ref(m.get("model"))
        if kind == "user":
            files = m.get("files", [])
            if not isinstance(files, list):
                raise SessionMigrateError("OpenCode v2 user has invalid file attachments")
            for attachment in files:
                source = attachment.get("source") if isinstance(attachment, dict) else None
                if not isinstance(source, dict) or source.get("type") not in {"inline", "uri"}:
                    raise SessionMigrateError("OpenCode v2 file has invalid source")
                if source["type"] == "uri" and not isinstance(source.get("uri"), str):
                    raise SessionMigrateError("OpenCode v2 file has invalid source URI")
        if kind == "assistant":
            if not isinstance(m.get("agent"), str) or not isinstance(m.get("content"), list):
                raise SessionMigrateError("OpenCode v2 assistant has invalid runtime metadata")
            for c in m["content"]:
                if not isinstance(c, dict):
                    raise SessionMigrateError("OpenCode v2 has malformed assistant content")
                if c.get("type") != "tool":
                    continue
                if not isinstance(c.get("time"), dict) or not _is_non_negative_int(
                    c["time"].get("created")
                ):
                    raise SessionMigrateError("OpenCode v2 tool has invalid time")
                state = c.get("state")
                if not isinstance(state, dict):
                    raise SessionMigrateError("OpenCode v2 tool has invalid state")
                if state.get("status") == "running" and not isinstance(state.get("metadata"), dict):
                    raise SessionMigrateError("OpenCode v2 running tool has invalid metadata")
                if state.get("status") == "error":
                    error(state.get("error"))
                for block in state.get("content", []):
                    if not isinstance(block, dict) or block.get("type") not in {"text", "file"}:
                        raise SessionMigrateError("OpenCode v2 tool has invalid content")
                    fields = ("text",) if block["type"] == "text" else ("uri", "mime")
                    if not all(isinstance(block.get(k), str) for k in fields):
                        raise SessionMigrateError("OpenCode v2 tool has invalid content fields")
        if kind == "compaction":
            if m.get("status") not in {"running", "completed", "failed"}:
                raise SessionMigrateError("OpenCode v2 compaction has invalid status")
            if m.get("reason") not in {"auto", "manual"}:
                raise SessionMigrateError("OpenCode v2 compaction has invalid reason")
            if m["status"] in {"running", "completed"} and not all(
                isinstance(m.get(k), str) for k in ("summary", "recent")
            ):
                raise SessionMigrateError("OpenCode v2 compaction has invalid summary")


def record_count(bundle: dict[str, Any]) -> int:
    return (
        1
        + len(bundle["messages"])
        + sum(
            len(m["content"])
            if m["type"] == "assistant"
            else len(m.get("files", []))
            if m["type"] == "user"
            else 0
            for m in bundle["messages"]
        )
    )
