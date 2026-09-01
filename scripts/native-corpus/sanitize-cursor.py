#!/usr/bin/env python3
"""Sanitize an exact Cursor Agent content-addressed native store.

Cursor blob IDs are SHA-256 digests of their protobuf payloads.  Replacing a
private path therefore changes its containing blob and every ancestor that
references that blob.  This script rewrites equal-length path strings, then
recomputes the complete digest graph to a fixed point before publishing a
fresh SQLite database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
import uuid
from pathlib import Path
from typing import Any

PUBLIC_CAPTURE_PREFIX = "/tmp/sm-cursor-capture/"
PUBLIC_USER_PREFIX = "/tmp/sm-cursor-user/"
PUBLIC_USERNAME = "userx"
PUBLIC_TITLE = "inspect-native-media-boundaries"
MAX_BLOBS = 500_000
MAX_ROUNDS = 1_024


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-store", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--source-capture-root", required=True)
    parser.add_argument("--source-user-home", required=True)
    parser.add_argument("--source-username", required=True)
    parser.add_argument("--title", default=PUBLIC_TITLE)
    return parser.parse_args()


def _padded_public_path(prefix: str, source: str) -> str:
    if not source.startswith("/"):
        raise RuntimeError("Cursor source path must be absolute")
    if len(prefix) > len(source):
        raise RuntimeError("Cursor source path is too short for its public replacement")
    return prefix + "x" * (len(source) - len(prefix))


def _snapshot(source: Path, destination: Path) -> None:
    if not source.is_file() or source.is_symlink():
        raise RuntimeError("raw Cursor store must be a regular file")
    uri = f"file:{source.resolve()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=5) as origin:
            origin.execute("PRAGMA trusted_schema=OFF")
            with sqlite3.connect(destination) as target:
                origin.backup(target)
    except sqlite3.Error as exc:
        raise RuntimeError("cannot snapshot raw Cursor store") from exc


def _read_snapshot(path: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    try:
        with sqlite3.connect(path) as db:
            if db.execute("PRAGMA integrity_check(1)").fetchone() != ("ok",):
                raise RuntimeError("raw Cursor store failed integrity validation")
            if int(db.execute("PRAGMA user_version").fetchone()[0]) != 1:
                raise RuntimeError("raw Cursor store has an unsupported schema version")
            objects = set(
                db.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")
            )
            if objects != {("table", "blobs"), ("table", "meta")}:
                raise RuntimeError("raw Cursor store has an unsupported schema")
            meta_rows = db.execute("SELECT key,value FROM meta ORDER BY key").fetchall()
            if len(meta_rows) != 1 or meta_rows[0][0] != "0":
                raise RuntimeError("raw Cursor store has invalid metadata")
            metadata = json.loads(bytes.fromhex(meta_rows[0][1]))
            rows = db.execute("SELECT id,data FROM blobs ORDER BY id").fetchall()
    except (sqlite3.Error, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("cannot read raw Cursor store") from exc
    if not isinstance(metadata, dict):
        raise RuntimeError("raw Cursor metadata is not an object")
    if not rows or len(rows) > MAX_BLOBS:
        raise RuntimeError("raw Cursor blob count is invalid")
    blobs: dict[str, bytes] = {}
    for blob_id, data in rows:
        if not isinstance(blob_id, str) or not isinstance(data, bytes):
            raise RuntimeError("raw Cursor blob row is malformed")
        if hashlib.sha256(data).hexdigest() != blob_id:
            raise RuntimeError("raw Cursor blob digest is invalid")
        blobs[blob_id] = data
    return metadata, blobs


def _replace_all(data: bytes, replacements: list[tuple[bytes, bytes]]) -> bytes:
    result = data
    for source, target in replacements:
        result = result.replace(source, target)
    return result


def _rehash_graph(
    blobs: dict[str, bytes], path_replacements: list[tuple[bytes, bytes]]
) -> tuple[dict[str, str], dict[str, bytes], int]:
    path_rewritten = {
        blob_id: _replace_all(data, path_replacements) for blob_id, data in blobs.items()
    }
    mapping = {
        blob_id: hashlib.sha256(data).hexdigest() for blob_id, data in path_rewritten.items()
    }
    for round_number in range(1, min(MAX_ROUNDS, len(blobs) + 2) + 1):
        reference_replacements: list[tuple[bytes, bytes]] = []
        for old_id, new_id in mapping.items():
            if old_id == new_id:
                continue
            reference_replacements.append((bytes.fromhex(old_id), bytes.fromhex(new_id)))
            reference_replacements.append((old_id.encode(), new_id.encode()))
        transformed = {
            old_id: _replace_all(data, reference_replacements)
            for old_id, data in path_rewritten.items()
        }
        next_mapping = {
            old_id: hashlib.sha256(data).hexdigest() for old_id, data in transformed.items()
        }
        if next_mapping == mapping:
            rewritten: dict[str, bytes] = {}
            for old_id, data in transformed.items():
                new_id = next_mapping[old_id]
                if new_id in rewritten and rewritten[new_id] != data:
                    raise RuntimeError("Cursor sanitizer produced a blob digest collision")
                rewritten[new_id] = data
            return next_mapping, rewritten, round_number
        mapping = next_mapping
    raise RuntimeError("Cursor blob reference graph did not converge")


def _write_store(path: Path, metadata: dict[str, Any], blobs: dict[str, bytes]) -> None:
    path.parent.mkdir(parents=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    if path.exists():
        raise RuntimeError("Cursor sanitizer output already exists")
    try:
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA user_version=1")
            db.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
            db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
            db.executemany("INSERT INTO blobs(id,data) VALUES(?,?)", sorted(blobs.items()))
            encoded = json.dumps(
                metadata, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode()
            db.execute("INSERT INTO meta(key,value) VALUES('0',?)", (encoded.hex(),))
            db.commit()
            db.execute("VACUUM")
    except (sqlite3.Error, TypeError, ValueError) as exc:
        raise RuntimeError("cannot write sanitized Cursor store") from exc
    os.chmod(path, 0o600)


def sanitize_store(
    raw_store: Path,
    output_root: Path,
    *,
    source_capture_root: str,
    source_user_home: str,
    source_username: str,
    title: str = PUBLIC_TITLE,
) -> dict[str, Any]:
    capture_public = _padded_public_path(PUBLIC_CAPTURE_PREFIX, source_capture_root)
    user_public = _padded_public_path(PUBLIC_USER_PREFIX, source_user_home)
    if not source_username or len(source_username) != len(PUBLIC_USERNAME):
        raise RuntimeError("Cursor source username must match the public replacement length")
    ordered_replacements = list(
        (
            ("capture_root", source_capture_root, capture_public),
            ("user_home", source_user_home, user_public),
            ("username", source_username, PUBLIC_USERNAME),
        )
    )
    ordered_replacements.sort(key=lambda item: len(item[1]), reverse=True)
    replacements = [
        (source.encode(), target.encode()) for _, source, target in ordered_replacements
    ]
    counts: dict[str, int] = {
        "capture_root": 0,
        "user_home": 0,
        "username": 0,
    }

    with tempfile.TemporaryDirectory(prefix="session-migrate-cursor-sanitize-") as directory:
        snapshot = Path(directory) / "snapshot.db"
        _snapshot(raw_store, snapshot)
        raw_sha256 = hashlib.sha256(snapshot.read_bytes()).hexdigest()
        metadata, blobs = _read_snapshot(snapshot)

    for data in blobs.values():
        working = data
        for label, source, target in ordered_replacements:
            source_bytes = source.encode()
            counts[label] += working.count(source_bytes)
            working = working.replace(source_bytes, target.encode())
    if counts["capture_root"] < 1 or counts["username"] < 1:
        raise RuntimeError("raw Cursor capture does not contain its expected private identity")

    try:
        session_id = str(uuid.UUID(str(metadata["agentId"])))
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError("raw Cursor metadata has an invalid session ID") from exc
    old_root = metadata.get("latestRootBlobId")
    if not isinstance(old_root, str) or old_root not in blobs:
        raise RuntimeError("raw Cursor metadata has an invalid root blob")

    mapping, rewritten, rounds = _rehash_graph(blobs, replacements)
    metadata = dict(metadata)
    metadata["latestRootBlobId"] = mapping[old_root]
    old_title = metadata.get("name")
    metadata["name"] = title
    public_cwd = f"{capture_public}/work"
    workspace_key = hashlib.md5(public_cwd.encode(), usedforsecurity=False).hexdigest()
    destination = output_root / "native/chats" / workspace_key / session_id / "store.db"
    _write_store(destination, metadata, rewritten)

    data = destination.read_bytes()
    forbidden = (
        source_capture_root.encode(),
        source_user_home.encode(),
        source_username.encode(),
    )
    if any(value in data for value in forbidden):
        raise RuntimeError("sanitized Cursor store still contains a private path")
    with tempfile.TemporaryDirectory(prefix="session-migrate-cursor-verify-") as directory:
        snapshot = Path(directory) / "snapshot.db"
        _snapshot(destination, snapshot)
        check_metadata, check_blobs = _read_snapshot(snapshot)
    if check_metadata != metadata or check_blobs != rewritten:
        raise RuntimeError("sanitized Cursor store failed round-trip verification")

    return {
        "artifact": str(destination.relative_to(output_root)),
        "artifact_sha256": hashlib.sha256(data).hexdigest(),
        "artifact_size": len(data),
        "raw_private_sha256": raw_sha256,
        "session_id": session_id,
        "native_cwd": public_cwd,
        "mutations": {
            **counts,
            "title": int(old_title != title),
            "content_addressed_blob_ids": sum(
                1 for old_id, new_id in mapping.items() if old_id != new_id
            ),
        },
        "rehash_rounds": rounds,
    }


def main() -> int:
    args = arguments()
    result = sanitize_store(
        args.raw_store,
        args.output_root,
        source_capture_root=args.source_capture_root,
        source_user_home=args.source_user_home,
        source_username=args.source_username,
        title=args.title,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
