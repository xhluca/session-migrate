#!/usr/bin/env python3
"""Sanitize the reviewed Devin CLI 3000.6.7 native corpus capture.

The selected active SQLite message chain, media, tool calls/results, IDs, and
timestamps are preserved.  Other sessions and inactive retry branches are
removed.  Private capture paths, generated runtime instructions, model
reasoning, and session cogs are replaced at their schema locations.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from session_migrate.formats import devin
from session_migrate.model import EventKind

PUBLIC_CWD = "/fixture/work"
PUBLIC_ROOT = "/fixture"
SYSTEM_PLACEHOLDER = "SANITIZED_NATIVE_DEVIN_RUNTIME_CONTEXT"
REASONING_PLACEHOLDER = "SANITIZED_NATIVE_DEVIN_REASONING"
PINNED_VERSION = "3000.6.7"
EXPECTED_ACTIVE_NODES = 14
EXPECTED_TOTAL_NODES = 71
EXPECTED_TOOL_STATES = 3
_SECRET = re.compile(r"(?:sk-or-v1-|sk-[A-Za-z0-9_-]{12,}|api[_-]?key\s*[=:])", re.I)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-database", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--source-cwd", required=True)
    parser.add_argument("--source-root", required=True)
    return parser.parse_args()


def _replace(value: Any, replacements: dict[str, str], counts: dict[str, int]) -> Any:
    if isinstance(value, str):
        result = value
        for source, target in replacements.items():
            occurrences = result.count(source)
            if occurrences:
                result = result.replace(source, target)
                counts["capture_paths"] += occurrences
        return result
    if isinstance(value, list):
        return [_replace(item, replacements, counts) for item in value]
    if isinstance(value, dict):
        return {key: _replace(item, replacements, counts) for key, item in value.items()}
    return value


def _json(value: str | None, label: str) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label} is not valid JSON") from exc


def _render(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _active_node_ids(db: sqlite3.Connection, session_id: str, tip: int) -> list[int]:
    rows = db.execute(
        """
        WITH RECURSIVE chain(node_id, parent_node_id, depth) AS (
          SELECT node_id, parent_node_id, 0
          FROM message_nodes WHERE session_id = ?1 AND node_id = ?2
          UNION ALL
          SELECT m.node_id, m.parent_node_id, chain.depth + 1
          FROM message_nodes m JOIN chain
            ON m.session_id = ?1 AND m.node_id = chain.parent_node_id
          WHERE chain.depth < 100000
        )
        SELECT node_id FROM chain ORDER BY node_id
        """,
        (session_id, tip),
    ).fetchall()
    return [int(row[0]) for row in rows]


def _validate_reviewed_capture(source: Path, session_id: str, source_cwd: str) -> None:
    summaries = {summary.session_id: summary for summary in devin.list_sessions(source)}
    if session_id not in summaries or summaries[session_id].cwd != Path(source_cwd):
        raise RuntimeError("reviewed Devin session or capture CWD is missing")
    session = devin.parse_session(source, session_id)
    if session.cli_version != PINNED_VERSION or session.raw_record_count != EXPECTED_ACTIVE_NODES:
        raise RuntimeError("capture is not the reviewed Devin 3000.6.7 active chain")
    counts = session.event_counts()
    expected = {
        "context": 2,
        "message": 5,
        "opaque": 6,
        "thinking": 3,
        "tool_call": 3,
        "tool_result": 3,
    }
    if counts != expected:
        raise RuntimeError("reviewed Devin modality counts changed")
    calls = [event for event in session.events if event.kind == EventKind.TOOL_CALL]
    if [event.tool_name for event in calls] != ["read", "read", "read"]:
        raise RuntimeError("reviewed Devin native read calls changed")
    text = "\n".join(event.text or "" for event in session.events)
    for marker in ("SM_CORPUS_7319", "COPPER_4821", "[Audio content]"):
        if marker not in text:
            raise RuntimeError(f"reviewed Devin marker is missing: {marker}")


def sanitize_database(
    source: Path,
    destination: Path,
    *,
    session_id: str,
    source_cwd: str,
    source_root: str,
) -> dict[str, int]:
    if not source_cwd.startswith(source_root.rstrip("/") + "/"):
        raise RuntimeError("Devin source CWD must be inside the declared capture root")
    _validate_reviewed_capture(source, session_id, source_cwd)
    counts = {
        "capture_paths": 0,
        "system_messages": 0,
        "reasoning_blocks": 0,
        "session_cogs": 0,
        "other_sessions": 0,
        "inactive_nodes": 0,
        "global_state_rows": 0,
    }
    replacements = {source_cwd: PUBLIC_CWD, source_root: PUBLIC_ROOT}
    destination.parent.mkdir(parents=True, mode=0o700)
    with (
        sqlite3.connect(f"file:{source}?mode=ro", uri=True) as raw,
        sqlite3.connect(destination) as public,
    ):
        raw.backup(public)
        public.execute("PRAGMA secure_delete = ON")
        row = public.execute(
            "SELECT main_chain_id FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None or not isinstance(row[0], int):
            raise RuntimeError("reviewed Devin active tip is missing")
        active = _active_node_ids(public, session_id, row[0])
        if len(active) != EXPECTED_ACTIVE_NODES:
            raise RuntimeError("reviewed Devin active chain changed")
        total_nodes = public.execute(
            "SELECT COUNT(*) FROM message_nodes WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
        if total_nodes != EXPECTED_TOTAL_NODES:
            raise RuntimeError("reviewed Devin forest size changed")
        tool_states = public.execute(
            "SELECT COUNT(*) FROM tool_call_state WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
        if tool_states != EXPECTED_TOOL_STATES:
            raise RuntimeError("reviewed Devin tool-call state changed")

        counts["other_sessions"] = public.execute(
            "SELECT COUNT(*) FROM sessions WHERE id <> ?", (session_id,)
        ).fetchone()[0]
        for table in ("prompt_history", "rendered_commits", "tool_call_state"):
            public.execute(f"DELETE FROM {table} WHERE session_id <> ?", (session_id,))
        public.execute("DELETE FROM message_nodes WHERE session_id <> ?", (session_id,))
        public.execute("DELETE FROM sessions WHERE id <> ?", (session_id,))
        placeholders = ",".join("?" for _ in active)
        counts["inactive_nodes"] = public.execute(
            "SELECT COUNT(*) FROM message_nodes WHERE session_id = ? "
            f"AND node_id NOT IN ({placeholders})",
            (session_id, *active),
        ).fetchone()[0]
        public.execute(
            f"DELETE FROM message_nodes WHERE session_id = ? AND node_id NOT IN ({placeholders})",
            (session_id, *active),
        )
        counts["global_state_rows"] = public.execute("SELECT COUNT(*) FROM app_state").fetchone()[0]
        public.execute("DELETE FROM app_state")

        session_row = public.execute(
            "SELECT workspace_dirs, metadata FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        assert session_row is not None
        workspace_dirs = _replace(_json(session_row[0], "workspace_dirs"), replacements, counts)
        metadata = _replace(_json(session_row[1], "session metadata"), replacements, counts)
        public.execute(
            "UPDATE sessions SET working_directory = ?, workspace_dirs = ?, "
            "cogs_json = '[]', metadata = ? WHERE id = ?",
            (PUBLIC_CWD, _render(workspace_dirs), _render(metadata), session_id),
        )
        counts["session_cogs"] = 1

        for node_id, message_text, metadata_text in public.execute(
            "SELECT node_id, chat_message, metadata FROM message_nodes "
            "WHERE session_id = ? ORDER BY node_id",
            (session_id,),
        ).fetchall():
            message = _replace(_json(message_text, f"message {node_id}"), replacements, counts)
            metadata_value = _replace(
                _json(metadata_text, f"message metadata {node_id}"), replacements, counts
            )
            if message.get("role") == "system":
                if not isinstance(message.get("content"), str):
                    raise RuntimeError("reviewed Devin system node has no text content")
                message["content"] = f"{SYSTEM_PLACEHOLDER}_{node_id}"
                counts["system_messages"] += 1
            thinking = message.get("thinking")
            if isinstance(thinking, dict) and isinstance(thinking.get("thinking"), str):
                thinking["thinking"] = f"{REASONING_PLACEHOLDER}_{node_id}"
                thinking["signature"] = ""
                counts["reasoning_blocks"] += 1
            public.execute(
                "UPDATE message_nodes SET chat_message = ?, metadata = ? "
                "WHERE session_id = ? AND node_id = ?",
                (_render(message), _render(metadata_value), session_id, node_id),
            )

        for row_id, content in public.execute(
            "SELECT id, content FROM prompt_history WHERE session_id = ?", (session_id,)
        ).fetchall():
            public.execute(
                "UPDATE prompt_history SET content = ? WHERE id = ?",
                (_replace(content, replacements, counts), row_id),
            )
        for call_id, call_json, update_json in public.execute(
            "SELECT tool_call_id, tool_call_json, tool_call_update_json "
            "FROM tool_call_state WHERE session_id = ?",
            (session_id,),
        ).fetchall():
            call = _replace(_json(call_json, f"tool call {call_id}"), replacements, counts)
            update = _replace(_json(update_json, f"tool update {call_id}"), replacements, counts)
            public.execute(
                "UPDATE tool_call_state SET tool_call_json = ?, tool_call_update_json = ? "
                "WHERE session_id = ? AND tool_call_id = ?",
                (_render(call), _render(update), session_id, call_id),
            )
        public.commit()
        public.execute("VACUUM")
        public.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    os.chmod(destination, 0o600)

    sanitized = devin.parse_session(destination, session_id)
    if sanitized.cwd != Path(PUBLIC_CWD) or sanitized.raw_record_count != EXPECTED_ACTIVE_NODES:
        raise RuntimeError("sanitized Devin fixture no longer parses as the reviewed chain")
    destination.with_name(f"{destination.name}-wal").unlink(missing_ok=True)
    destination.with_name(f"{destination.name}-shm").unlink(missing_ok=True)
    data = destination.read_bytes()
    for private in (source_cwd.encode(), source_root.encode(), b"/home/"):
        if private in data:
            raise RuntimeError("sanitized Devin database still contains a private path")
    if _SECRET.search(data.decode("latin-1", errors="ignore")):
        raise RuntimeError("sanitized Devin database still contains a secret-like token")
    if counts["system_messages"] != 6 or counts["reasoning_blocks"] != 3:
        raise RuntimeError("reviewed Devin runtime/reasoning redaction counts changed")
    return counts


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    args = arguments()
    destination = args.output_root / "native/sessions.db"
    counts = sanitize_database(
        args.raw_database,
        destination,
        session_id=args.session_id,
        source_cwd=args.source_cwd,
        source_root=args.source_root,
    )
    print(
        json.dumps(
            {
                "artifacts": {destination.name: digest(destination)},
                "mutations": counts,
                "session_id": args.session_id,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
