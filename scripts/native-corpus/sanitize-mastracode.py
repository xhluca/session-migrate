#!/usr/bin/env python3
"""Sanitize one exact MastraCode thread into a minimal native SQLite DB."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import sqlite3
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from session_migrate.formats import mastracode

THREAD_COLUMNS = ("id", "resourceId", "title", "metadata", "createdAt", "updatedAt")
MESSAGE_COLUMNS = (
    "id",
    "thread_id",
    "content",
    "role",
    "type",
    "createdAt",
    "resourceId",
)
CONTENT_FIELDS = frozenset({"format", "parts", "metadata", "content"})
PART_FIELDS = {
    "text": (frozenset({"type", "text", "createdAt"}), frozenset({"providerMetadata"})),
    "file": (
        frozenset({"type", "data", "mimeType", "createdAt"}),
        frozenset({"providerMetadata", "filename"}),
    ),
    "tool-invocation": (
        frozenset({"type", "toolInvocation"}),
        frozenset({"providerMetadata"}),
    ),
    "data-workspace-metadata": (
        frozenset({"type", "data", "createdAt"}),
        frozenset(),
    ),
    "data-sandbox-exit": (
        frozenset({"type", "data", "createdAt"}),
        frozenset(),
    ),
    "step-start": (
        frozenset({"type", "createdAt", "model"}),
        frozenset({"providerMetadata"}),
    ),
}


class SanitizationError(ValueError):
    """Raised when captured state does not match the reviewed native schema."""


@dataclass(frozen=True, slots=True)
class Result:
    thread: tuple[str, str, str, bytes, str, str]
    messages: tuple[tuple[str, str, str, str, str, str, str], ...]
    mutations: dict[str, int]


def _validate_json(value: Any, *, depth: int = 0, budget: list[int] | None = None) -> None:
    if budget is None:
        budget = [1_000_000]
    if depth > 96 or budget[0] <= 0:
        raise SanitizationError("native JSON exceeds the shape limit")
    budget[0] -= 1
    if value is None or isinstance(value, (bool, int, float, str)):
        return
    if isinstance(value, list):
        for item in value:
            _validate_json(item, depth=depth + 1, budget=budget)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _validate_json(item, depth=depth + 1, budget=budget)
        return
    raise SanitizationError("native JSON contains an unsupported value")


def _scrub(value: Any, source_root: str, replacement: str, mutations: Counter[str]) -> Any:
    if isinstance(value, str):
        updated = value.replace(source_root, replacement)
        if updated != value:
            mutations["private path"] += value.count(source_root)
        return updated
    if isinstance(value, list):
        return [_scrub(item, source_root, replacement, mutations) for item in value]
    if isinstance(value, dict):
        return {
            key: _scrub(item, source_root, replacement, mutations) for key, item in value.items()
        }
    return value


def _validate_tool_invocation(invocation: Any, index: int) -> None:
    if not isinstance(invocation, dict):
        raise SanitizationError(f"message {index} toolInvocation is not an object")
    state = invocation.get("state")
    common = {"state", "toolCallId", "toolName", "args"}
    expected = common | ({"result"} if state == "result" else {"errorText"})
    if state not in {"result", "output-error"} or set(invocation) != expected:
        raise SanitizationError(f"message {index} toolInvocation schema changed")
    if not all(isinstance(invocation.get(key), str) for key in ("toolCallId", "toolName")):
        raise SanitizationError(f"message {index} toolInvocation linkage is invalid")
    if not isinstance(invocation.get("args"), dict):
        raise SanitizationError(f"message {index} toolInvocation args are invalid")
    if state == "output-error" and not isinstance(invocation.get("errorText"), str):
        raise SanitizationError(f"message {index} failed tool text is invalid")


def _validate_content(document: Any, index: int) -> None:
    if not isinstance(document, dict):
        raise SanitizationError(f"message {index} content is not an object")
    if (
        not {"format", "parts", "metadata"}.issubset(document)
        or not set(document) <= CONTENT_FIELDS
    ):
        raise SanitizationError(f"message {index} content fields changed")
    if document.get("format") != 2 or not isinstance(document.get("metadata"), dict):
        raise SanitizationError(f"message {index} content header is invalid")
    parts = document.get("parts")
    if not isinstance(parts, list) or not parts:
        raise SanitizationError(f"message {index} parts are missing")
    for part in parts:
        if not isinstance(part, dict):
            raise SanitizationError(f"message {index} part is not an object")
        part_type = part.get("type")
        if part_type not in PART_FIELDS:
            raise SanitizationError(f"message {index} part type is unsupported: {part_type}")
        required, optional = PART_FIELDS[part_type]
        if not required.issubset(part) or not set(part) <= required | optional:
            raise SanitizationError(f"message {index} {part_type} part fields changed")
        if part_type == "text" and not isinstance(part.get("text"), str):
            raise SanitizationError(f"message {index} text part is invalid")
        if part_type == "file":
            if part.get("mimeType") != "image/png" or not isinstance(part.get("data"), str):
                raise SanitizationError(f"message {index} native file part is not PNG")
            try:
                base64.b64decode(part["data"], validate=True)
            except (binascii.Error, ValueError) as exc:
                raise SanitizationError(f"message {index} native PNG is invalid base64") from exc
        if part_type == "tool-invocation":
            _validate_tool_invocation(part.get("toolInvocation"), index)
    _validate_json(document)


def sanitize_database(source: Path, *, session_id: str, source_root: Path) -> Result:
    """Validate/select one native thread, then sanitize its durable rows."""

    source = source.resolve(strict=True)
    root = os.path.abspath(source_root)
    mutations: Counter[str] = Counter()
    uri = f"file:{source}?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        integrity = db.execute("PRAGMA integrity_check(1)").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise SanitizationError("source database failed SQLite integrity validation")
        for table, expected in (
            ("mastra_threads", THREAD_COLUMNS),
            ("mastra_messages", MESSAGE_COLUMNS),
        ):
            columns = tuple(row[1] for row in db.execute(f'PRAGMA table_info("{table}")'))
            if columns != expected:
                raise SanitizationError(f"native {table} columns changed")
        thread = db.execute(
            "SELECT id,resourceId,title,CAST(metadata AS BLOB),createdAt,updatedAt "
            'FROM "mastra_threads" WHERE id=?',
            (session_id,),
        ).fetchone()
        if thread is None:
            raise SanitizationError("selected native thread does not exist")
        rows = db.execute(
            "SELECT id,thread_id,content,role,type,createdAt,resourceId "
            'FROM "mastra_messages" WHERE thread_id=? ORDER BY createdAt,id',
            (session_id,),
        ).fetchall()
    if not rows:
        raise SanitizationError("selected native thread has no messages")

    metadata = mastracode._decode_metadata(thread[3])
    if not metadata or "projectPath" not in metadata:
        raise SanitizationError("native thread metadata is missing its project path")
    clean_metadata = _scrub(metadata, root, "/fixture", mutations)
    clean_thread = (
        str(thread[0]),
        str(thread[1]),
        str(thread[2]),
        mastracode._jsonb_encode(clean_metadata),
        str(thread[4]),
        str(thread[5]),
    )
    clean_rows = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        message_id = str(row[0])
        if message_id in seen or str(row[1]) != session_id:
            raise SanitizationError("native message linkage is invalid")
        seen.add(message_id)
        if row[3] not in {"user", "assistant", "system", "signal"}:
            raise SanitizationError(f"message {index} role is unsupported")
        content = json.loads(str(row[2]))
        _validate_content(content, index)
        clean_content = _scrub(content, root, "/fixture", mutations)
        clean_rows.append(
            (
                message_id,
                session_id,
                json.dumps(
                    clean_content,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                str(row[3]),
                str(row[4]),
                str(row[5]),
                str(row[6]),
            )
        )
    return Result(clean_thread, tuple(clean_rows), dict(sorted(mutations.items())))


def write_database(result: Result, output: Path) -> None:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise SanitizationError("output already exists")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="session-migrate-mastracode-sanitize-", suffix=".db", dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with sqlite3.connect(temporary) as db:
            db.executescript(
                'CREATE TABLE "mastra_threads" ('
                '"id" TEXT NOT NULL PRIMARY KEY,'
                '"resourceId" TEXT NOT NULL,'
                '"title" TEXT NOT NULL,'
                '"metadata" TEXT,'
                '"createdAt" TEXT NOT NULL,'
                '"updatedAt" TEXT NOT NULL);'
                'CREATE TABLE "mastra_messages" ('
                '"id" TEXT NOT NULL PRIMARY KEY,'
                '"thread_id" TEXT NOT NULL,'
                '"content" TEXT NOT NULL,'
                '"role" TEXT NOT NULL,'
                '"type" TEXT NOT NULL,'
                '"createdAt" TEXT NOT NULL,'
                '"resourceId" TEXT);'
                'CREATE INDEX "mastra_messages_thread_created" '
                'ON "mastra_messages"("thread_id","createdAt","id");'
            )
            db.execute(
                'INSERT INTO "mastra_threads" '
                "(id,resourceId,title,metadata,createdAt,updatedAt) VALUES (?,?,?,?,?,?)",
                result.thread,
            )
            db.executemany(
                'INSERT INTO "mastra_messages" '
                "(id,thread_id,content,role,type,createdAt,resourceId) VALUES (?,?,?,?,?,?,?)",
                result.messages,
            )
            db.commit()
        temporary.replace(output)
        output.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--mutations", type=Path)
    arguments = parser.parse_args()
    result = sanitize_database(
        arguments.input,
        session_id=arguments.session_id,
        source_root=arguments.source_root,
    )
    write_database(result, arguments.output)
    if arguments.mutations:
        arguments.mutations.write_text(json.dumps(result.mutations, indent=2) + "\n")
    print(
        json.dumps(
            {
                "output": str(arguments.output),
                "sha256": hashlib.sha256(arguments.output.read_bytes()).hexdigest(),
                "mutations": result.mutations,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except (OSError, sqlite3.Error, json.JSONDecodeError, SanitizationError) as exc:
        raise SystemExit(str(exc)) from exc
