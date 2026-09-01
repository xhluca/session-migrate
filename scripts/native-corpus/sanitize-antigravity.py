#!/usr/bin/env python3
"""Sanitize one exact Antigravity CLI 1.1.16 native conversation.

The input is a transactionally consistent conversation snapshot plus the
native picker database.  The sanitizer preserves the producer's SQLite and
protobuf records, replaces private paths without changing protobuf byte
lengths, removes runtime-only auxiliary metadata, and removes the duplicated
plan/runtime context carried beside each visible user message.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from session_migrate.formats import antigravity

PUBLIC_CWD = "/fixture/antigravity-work"
PUBLIC_REPOSITORY = "/fixture/session-migrate"
PUBLIC_TITLE = "inspect-portable-media"


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-conversation", type=Path, required=True)
    parser.add_argument("--raw-summaries", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--source-capture-root", required=True)
    parser.add_argument("--source-cwd", required=True)
    parser.add_argument("--source-repository", required=True)
    return parser.parse_args()


def _same_length_path(public: str, source: str) -> str:
    """Return a non-private absolute path with exactly ``len(source)`` bytes."""

    if not source.startswith("/") or len(public) > len(source):
        raise RuntimeError("path replacement cannot preserve the native byte length")
    return public + "/" + "x" * (len(source) - len(public) - 1)


def _replace_bytes(value: bytes, replacements: dict[bytes, bytes]) -> tuple[bytes, int]:
    result = value
    count = 0
    for source, target in replacements.items():
        if len(source) != len(target):
            raise RuntimeError("binary replacement changes protobuf byte length")
        occurrences = result.count(source)
        if occurrences:
            result = result.replace(source, target)
            count += occurrences
    return result, count


def _encode_fields(fields: tuple[Any, ...]) -> bytes:
    result = bytearray()
    for field in fields:
        if field.wire_type == 0:
            result.extend(antigravity._field_varint(field.number, field.value))
        elif field.wire_type == 1:
            result.extend(antigravity._varint((field.number << 3) | 1))
            result.extend(field.value)
        elif field.wire_type == 2:
            result.extend(antigravity._field_bytes(field.number, field.value))
        elif field.wire_type == 5:
            result.extend(antigravity._varint((field.number << 3) | 5))
            result.extend(field.value)
        else:  # pragma: no cover - the pinned decoder rejects this first
            raise RuntimeError("unsupported protobuf wire type")
    return bytes(result)


def _without_runtime_user_context(payload: bytes) -> tuple[bytes, int]:
    """Remove native plan/workspace context while retaining the visible prompt."""

    outer = antigravity._decode_message(payload)
    cleaned_outer: list[Any] = []
    removed = 0
    for field in outer:
        if field.number != 19 or field.wire_type != 2:
            cleaned_outer.append(field)
            continue
        user = antigravity._decode_message(field.value)
        # Fields 3/9 contain duplicated command expansion/runtime context;
        # fields 12/13 contain private workspace/runtime snapshots.  None is
        # the visible user message (field 2), and the source parser already
        # treats all four as non-portable native context.
        retained = tuple(item for item in user if item.number not in {3, 9, 12, 13})
        removed += len(user) - len(retained)
        cleaned_outer.append(type(field)(field.number, field.wire_type, _encode_fields(retained)))
    if removed == 0:
        raise RuntimeError("Antigravity capture has no runtime user context to sanitize")
    return _encode_fields(tuple(cleaned_outer)), removed


def _backup(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise RuntimeError(f"missing Antigravity database: {source}")
    if destination.exists():
        raise RuntimeError(f"Antigravity sanitizer output already exists: {destination}")
    destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    source_uri = f"file:{source.resolve()}?mode=ro"
    try:
        with sqlite3.connect(source_uri, uri=True, timeout=5) as origin:
            origin.execute("PRAGMA trusted_schema=OFF")
            with sqlite3.connect(destination) as target:
                origin.backup(target)
    except sqlite3.Error as exc:
        raise RuntimeError("cannot snapshot Antigravity database") from exc
    os.chmod(destination, 0o600)


def _sanitize_conversation(
    path: Path, replacements: dict[bytes, bytes]
) -> tuple[str, Counter[str]]:
    counts: Counter[str] = Counter()
    try:
        with sqlite3.connect(path, timeout=5) as db:
            db.execute("PRAGMA trusted_schema=OFF")
            row = db.execute("SELECT cascade_id FROM trajectory_meta WHERE source=17").fetchone()
            if row is None or not isinstance(row[0], str):
                raise RuntimeError("Antigravity capture has no CLI conversation ID")
            session_id = str(uuid.UUID(row[0]))
            rows = db.execute(
                "SELECT idx,step_type,metadata,error_details,permissions,task_details,"
                "render_info,step_payload FROM steps ORDER BY idx"
            ).fetchall()
            if not rows:
                raise RuntimeError("Antigravity capture has no native steps")
            for row in rows:
                index, step_type, *values = row
                cleaned: list[bytes | None] = []
                for value in values:
                    if value is None:
                        cleaned.append(None)
                        continue
                    if not isinstance(value, bytes):
                        raise RuntimeError("Antigravity step BLOB has an invalid storage type")
                    replaced, number = _replace_bytes(value, replacements)
                    counts["private_path"] += number
                    cleaned.append(replaced)
                if step_type == antigravity.STEP_TYPE_USER_INPUT:
                    cleaned[-1], number = _without_runtime_user_context(cleaned[-1] or b"")
                    counts["runtime_user_context"] += number
                db.execute(
                    "UPDATE steps SET metadata=?,error_details=?,permissions=?,task_details=?,"
                    "render_info=?,step_payload=? WHERE idx=?",
                    (*cleaned, index),
                )
            for metadata_id, value in db.execute(
                "SELECT id,data FROM trajectory_metadata_blob ORDER BY id"
            ).fetchall():
                if not isinstance(value, bytes):
                    raise RuntimeError("Antigravity trajectory metadata has an invalid type")
                replaced, number = _replace_bytes(value, replacements)
                counts["private_path"] += number
                if number:
                    db.execute(
                        "UPDATE trajectory_metadata_blob SET data=? WHERE id=?",
                        (replaced, metadata_id),
                    )
            for table in (
                "gen_metadata",
                "executor_metadata",
                "parent_references",
                "battle_mode_infos",
            ):
                number = int(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
                if number:
                    db.execute(f"DELETE FROM {table}")
                    counts[f"runtime_table:{table}"] += number
            # SQLite otherwise keeps deleted/replaced content in free pages.
            # A sanitized public artifact must not retain those stale bytes.
            db.commit()
            db.execute("VACUUM")
            if db.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
                raise RuntimeError("cannot checkpoint sanitized Antigravity conversation")
            if db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise RuntimeError("sanitized Antigravity conversation failed integrity_check")
    except sqlite3.Error as exc:
        raise RuntimeError("cannot sanitize Antigravity conversation") from exc
    return session_id, counts


def _sanitize_summaries(
    path: Path,
    *,
    session_id: str,
    public_cwd: str,
    step_count: int,
) -> Counter[str]:
    counts: Counter[str] = Counter()
    try:
        with sqlite3.connect(path, timeout=5) as db:
            db.execute("PRAGMA trusted_schema=OFF")
            row = db.execute(
                "SELECT title,preview,workspace_uris,step_count FROM conversation_summaries "
                "WHERE conversation_id=?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("Antigravity picker row is missing")
            counts["summary_title"] += 1
            counts["summary_preview"] += 1
            counts["summary_workspace"] += 1
            if row[3] != step_count:
                counts["summary_step_count"] += 1
            db.execute(
                "UPDATE conversation_summaries SET title=?,preview=?,workspace_uris=?,"
                "step_count=? WHERE conversation_id=?",
                (
                    PUBLIC_TITLE,
                    "Portable media and timeline inspection",
                    json.dumps([f"file://{public_cwd}"], separators=(",", ":")),
                    step_count,
                    session_id,
                ),
            )
            db.commit()
            db.execute("VACUUM")
            if db.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
                raise RuntimeError("cannot checkpoint sanitized Antigravity summaries")
            if db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise RuntimeError("sanitized Antigravity summaries failed integrity_check")
    except sqlite3.Error as exc:
        raise RuntimeError("cannot sanitize Antigravity summaries") from exc
    return counts


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact_set_digest(root: Path, files: tuple[Path, ...]) -> str:
    digest = hashlib.sha256()
    for path in sorted(files):
        relative = str(path.relative_to(root)).encode()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        data = path.read_bytes()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def sanitize(args: argparse.Namespace) -> dict[str, Any]:
    source_cwd = str(Path(args.source_cwd).resolve())
    source_root = str(Path(args.source_capture_root).resolve())
    source_repository = str(Path(args.source_repository).resolve())
    public_root = _same_length_path("/fixture/antigravity-capture", source_root)
    public_cwd = _same_length_path(PUBLIC_CWD, source_cwd)
    public_repository = _same_length_path(PUBLIC_REPOSITORY, source_repository)
    replacements = {
        source_cwd.encode(): public_cwd.encode(),
        source_repository.encode(): public_repository.encode(),
        source_root.encode(): public_root.encode(),
    }

    output_native = args.output_root / "native"
    staged_conversation = output_native / "conversation.db"
    summaries = output_native / "conversation_summaries.db"
    _backup(args.raw_conversation, staged_conversation)
    _backup(args.raw_summaries, summaries)
    session_id, mutations = _sanitize_conversation(staged_conversation, replacements)
    conversation = output_native / "conversations" / f"{session_id}.db"
    conversation.parent.mkdir(mode=0o700)
    shutil.move(staged_conversation, conversation)
    os.chmod(conversation, 0o600)
    with sqlite3.connect(conversation) as db:
        step_count = int(db.execute("SELECT count(*) FROM steps").fetchone()[0])
    mutations.update(
        _sanitize_summaries(
            summaries,
            session_id=session_id,
            public_cwd=public_cwd,
            step_count=step_count,
        )
    )

    # The production parser takes a consistent snapshot and validates every
    # native row.  This catches accidental protobuf or schema corruption before
    # anything can be promoted into the public corpus.
    parsed = antigravity.parse_session(conversation)
    if parsed.session_id != session_id or parsed.raw_record_count != step_count:
        raise RuntimeError("sanitized Antigravity session does not reparse exactly")

    files = (conversation, summaries)
    return {
        "artifact_set_sha256": _artifact_set_digest(args.output_root, files),
        "artifacts": {str(path.relative_to(args.output_root)): _digest(path) for path in files},
        "mutations": dict(sorted(mutations.items())),
        "public_cwd": public_cwd,
        "session_id": session_id,
    }


def main() -> int:
    result = sanitize(arguments())
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
