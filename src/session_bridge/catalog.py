"""Private, filesystem-authoritative catalog of native agent sessions.

The catalog stores operational metadata and native session titles only.  It
never stores message bodies, tool arguments/results, previews, or first-user
messages.  Native JSONL files remain authoritative; Claude/Codex indexes are
optional sources of title and lineage metadata.
"""

from __future__ import annotations

import os
import re
import sqlite3
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from session_bridge.conversion import ConversionOptions, convert_session, load_session
from session_bridge.errors import JsonlError, SessionBridgeError
from session_bridge.jsonl import (
    DEFAULT_MAX_TOTAL_BYTES,
    ensure_file_unchanged,
    file_snapshot,
    iter_jsonl,
)
from session_bridge.model import AgentFormat

SCHEMA_VERSION = 2
LABEL_LIMIT = 512
PATH_VALUE_LIMIT = 32_768
_UUID_SUFFIX = re.compile(
    r"(?P<id>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12})$"
)
_STATE_DATABASE = re.compile(r"state_(?P<version>[0-9]+)\.sqlite$")


@dataclass(frozen=True, slots=True)
class CatalogRoot:
    id: int
    format: str
    path: str
    source: str
    enabled: bool
    last_scan_at: str | None
    last_scan_status: str | None
    last_error: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    catalog_id: str
    format: str
    session_id: str | None
    filename_session_id: str | None
    title: str | None
    title_kind: str | None
    kind: str
    lifecycle: str
    status: str
    reason: str | None
    duplicate: bool
    started_at: str | None
    cli_version: str | None
    history_mode: str | None
    records: int | None
    bytes: int
    cwd: str | None = None
    path: str | None = None
    root: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RefreshResult:
    roots: int
    files_seen: int
    scanned: int
    unchanged: int
    missing: int
    statuses: dict[str, int]
    root_errors: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _Label:
    kind: str
    value: str
    ordinal: int
    priority: int


@dataclass(frozen=True, slots=True)
class _Scan:
    session_id: str | None
    filename_session_id: str | None
    cwd: str | None
    started_at: str | None
    cli_version: str | None
    history_mode: str | None
    kind: str
    lifecycle: str
    parent_session_id: str | None
    status: str
    reason: str | None
    records: int | None
    labels: tuple[_Label, ...]


@dataclass(slots=True)
class _NativeMetadata:
    by_path: dict[str, tuple[_Label, ...]]
    by_id: dict[str, tuple[_Label, ...]]
    parent_by_id: dict[str, str]
    loaded: bool


def default_catalog_path(
    *, environ: Mapping[str, str] | None = None, home: Path | None = None
) -> Path:
    """Return the private catalog path without creating it."""

    values = os.environ if environ is None else environ
    configured = values.get("SESSION_BRIDGE_CATALOG")
    if configured:
        return _absolute(Path(configured))
    state_home = values.get("XDG_STATE_HOME")
    base = _absolute(Path(state_home)) if state_home else (home or Path.home()) / ".local/state"
    return _absolute(base / "session-bridge/catalog.sqlite3")


def auto_roots(
    *,
    cwd: Path | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> list[tuple[AgentFormat, Path, str]]:
    """Find default, environment-selected, and ancestor-local native homes.

    This deliberately does not recursively search a home directory or disk.
    Arbitrary homes must be registered or passed explicitly to refresh.
    """

    values = os.environ if environ is None else environ
    user_home = _absolute(home or Path.home())
    candidates: list[tuple[AgentFormat, Path, str]] = [
        (AgentFormat.CLAUDE, user_home / ".claude", "default"),
        (AgentFormat.CODEX, user_home / ".codex", "default"),
    ]
    configured = (
        (AgentFormat.CLAUDE, values.get("CLAUDE_CONFIG_DIR")),
        (AgentFormat.CODEX, values.get("CODEX_HOME")),
    )
    for agent_format, value in configured:
        if value:
            candidates.append((agent_format, _absolute(Path(value)), "environment"))

    cursor = _absolute(cwd or Path.cwd())
    for directory in (cursor, *cursor.parents):
        claude_home = directory / ".claude"
        if (claude_home / "projects").is_dir():
            candidates.append((AgentFormat.CLAUDE, claude_home, "project"))
        codex_home = directory / ".codex"
        if (codex_home / "sessions").is_dir() or (
            codex_home / "archived_sessions"
        ).is_dir():
            candidates.append((AgentFormat.CODEX, codex_home, "project"))

    result: list[tuple[AgentFormat, Path, str]] = []
    seen: set[tuple[AgentFormat, str]] = set()
    for agent_format, path, source in candidates:
        normalized = _absolute(path)
        key = (agent_format, str(normalized))
        if key in seen or not normalized.is_dir():
            continue
        seen.add(key)
        result.append((agent_format, normalized, source))
    return result


def discover_roots(search_paths: Sequence[Path]) -> list[tuple[AgentFormat, Path, str]]:
    """Find project-local `.claude`/`.codex` homes below explicit subtrees.

    Symlinked directories are never followed, and only directories with native
    store markers are returned.  This function never widens the caller's
    supplied search boundaries.
    """

    found: list[tuple[AgentFormat, Path, str]] = []
    seen: set[tuple[AgentFormat, str]] = set()
    for search_path in search_paths:
        boundary = _absolute(search_path)
        if not boundary.is_dir():
            raise SessionBridgeError(f"catalog discovery path is not a directory: {boundary}")
        for current, subdirectories, _filenames in os.walk(boundary, followlinks=False):
            current_path = Path(current)
            subdirectories[:] = [
                name
                for name in subdirectories
                if not (current_path / name).is_symlink()
            ]
            candidates: list[tuple[AgentFormat, Path]] = []
            if current_path.name == ".claude" and (current_path / "projects").is_dir():
                candidates.append((AgentFormat.CLAUDE, current_path))
            if current_path.name == ".codex" and (
                (current_path / "sessions").is_dir()
                or (current_path / "archived_sessions").is_dir()
            ):
                candidates.append((AgentFormat.CODEX, current_path))
            for agent_format, path in candidates:
                key = (agent_format, str(path))
                if key not in seen:
                    seen.add(key)
                    found.append((agent_format, path, "discovered"))
            if candidates:
                # Native homes can be very large and cannot contain another
                # project-local home without an explicit, unusual nesting.
                subdirectories.clear()
    return found


class Catalog:
    """SQLite-backed native session catalog."""

    def __init__(self, path: Path) -> None:
        self.path = _absolute(path)
        connection: sqlite3.Connection | None = None
        try:
            _make_private_parent(self.path.parent)
            connection = sqlite3.connect(self.path)
            self._connection = connection
            self._connection.row_factory = sqlite3.Row
            self._connection.create_function(
                "session_casefold",
                1,
                lambda value: value.casefold() if isinstance(value, str) else "",
                deterministic=True,
            )
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA journal_mode = DELETE")
            self._initialize()
            os.chmod(self.path, 0o600)
        except SessionBridgeError:
            if connection is not None:
                connection.close()
            raise
        except (OSError, sqlite3.Error, ValueError) as exc:
            if connection is not None:
                connection.close()
            raise SessionBridgeError(
                "cannot open the private session catalog; move the disposable "
                "database aside and run `session-bridge catalog refresh`"
            ) from exc

    def __enter__(self) -> Catalog:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def _initialize(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS catalog_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS roots (
                id INTEGER PRIMARY KEY,
                format TEXT NOT NULL CHECK (format IN ('claude', 'codex')),
                path TEXT NOT NULL,
                source TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_scan_at TEXT,
                last_scan_status TEXT,
                last_error TEXT,
                UNIQUE(format, path)
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY,
                catalog_id TEXT NOT NULL UNIQUE,
                root_id INTEGER NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
                relative_path TEXT NOT NULL,
                canonical_path TEXT NOT NULL,
                format TEXT NOT NULL CHECK (format IN ('claude', 'codex')),
                session_id TEXT,
                filename_session_id TEXT,
                display_title TEXT,
                display_title_kind TEXT,
                cwd TEXT,
                started_at TEXT,
                started_at_epoch REAL,
                cli_version TEXT,
                history_mode TEXT,
                kind TEXT NOT NULL,
                lifecycle TEXT NOT NULL,
                parent_session_id TEXT,
                status TEXT NOT NULL,
                reason TEXT,
                records INTEGER,
                device INTEGER NOT NULL,
                inode INTEGER NOT NULL,
                bytes INTEGER NOT NULL,
                modified_ns INTEGER NOT NULL,
                indexed_at TEXT NOT NULL,
                validated_at TEXT,
                missing_since TEXT,
                UNIQUE(root_id, relative_path)
            );
            CREATE INDEX IF NOT EXISTS sessions_uuid_idx ON sessions(format, session_id);
            CREATE INDEX IF NOT EXISTS sessions_status_idx ON sessions(status);
            CREATE INDEX IF NOT EXISTS sessions_modified_idx ON sessions(modified_ns DESC);
            CREATE TABLE IF NOT EXISTS session_labels (
                id INTEGER PRIMARY KEY,
                session_row_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                value TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                priority INTEGER NOT NULL,
                UNIQUE(session_row_id, kind, value)
            );
            CREATE TABLE IF NOT EXISTS scan_runs (
                id INTEGER PRIMARY KEY,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                validate INTEGER NOT NULL,
                files_seen INTEGER NOT NULL DEFAULT 0,
                scanned INTEGER NOT NULL DEFAULT 0,
                unchanged INTEGER NOT NULL DEFAULT 0,
                missing INTEGER NOT NULL DEFAULT 0,
                root_errors INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        row = self._connection.execute(
            "SELECT value FROM catalog_meta WHERE key = 'schema_version'"
        ).fetchone()
        observed = int(row["value"]) if row is not None else None
        if observed == 1:
            self._migrate_v1_to_v2()
        elif observed is not None and observed != SCHEMA_VERSION:
            raise SessionBridgeError(
                f"catalog schema {row['value']} is not supported; expected {SCHEMA_VERSION}"
            )
        else:
            self._connection.execute(
                """
                INSERT OR REPLACE INTO catalog_meta(key, value)
                VALUES ('schema_version', ?)
                """,
                (str(SCHEMA_VERSION),),
            )
            self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._connection.commit()

    def _migrate_v1_to_v2(self) -> None:
        """Bound title storage and add timestamp filtering without losing roots."""

        try:
            self._connection.execute("BEGIN IMMEDIATE")
            session_columns = {
                str(row[1])
                for row in self._connection.execute("PRAGMA table_info(sessions)").fetchall()
            }
            if "started_at_epoch" not in session_columns:
                self._connection.execute(
                    "ALTER TABLE sessions ADD COLUMN started_at_epoch REAL"
                )
            label_columns = {
                str(row[1])
                for row in self._connection.execute(
                    "PRAGMA table_info(session_labels)"
                ).fetchall()
            }
            if "normalized" in label_columns:
                self._connection.execute(
                    "ALTER TABLE session_labels RENAME TO session_labels_v1"
                )
                self._connection.execute(
                    """
                    CREATE TABLE session_labels (
                        id INTEGER PRIMARY KEY,
                        session_row_id INTEGER NOT NULL
                            REFERENCES sessions(id) ON DELETE CASCADE,
                        kind TEXT NOT NULL,
                        value TEXT NOT NULL,
                        ordinal INTEGER NOT NULL,
                        priority INTEGER NOT NULL,
                        UNIQUE(session_row_id, kind, value)
                    )
                    """
                )
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO session_labels(
                        id, session_row_id, kind, value, ordinal, priority
                    )
                    SELECT id, session_row_id, kind, substr(value, 1, 512),
                           ordinal, priority
                    FROM session_labels_v1
                    """
                )
                self._connection.execute("DROP TABLE session_labels_v1")
            timestamps = self._connection.execute(
                "SELECT id, started_at FROM sessions WHERE started_at IS NOT NULL"
            ).fetchall()
            self._connection.execute(
                "UPDATE sessions SET display_title = substr(display_title, 1, ?)",
                (LABEL_LIMIT,),
            )
            self._connection.executemany(
                "UPDATE sessions SET started_at_epoch = ? WHERE id = ?",
                (
                    (_timestamp_epoch(_string(row["started_at"])), int(row["id"]))
                    for row in timestamps
                ),
            )
            self._connection.execute(
                "UPDATE catalog_meta SET value = ? WHERE key = 'schema_version'",
                (str(SCHEMA_VERSION),),
            )
            self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._connection.commit()
        except (sqlite3.Error, ValueError) as exc:
            self._connection.rollback()
            raise SessionBridgeError(
                "catalog schema migration failed; preserve registered roots or rebuild the "
                "disposable catalog with `session-bridge catalog refresh`"
            ) from exc

    def add_root(
        self, agent_format: AgentFormat, path: Path, *, source: str = "registered"
    ) -> CatalogRoot:
        if agent_format not in {AgentFormat.CLAUDE, AgentFormat.CODEX}:
            raise SessionBridgeError("catalog roots support only Claude and Codex native homes")
        normalized = str(_absolute(path))
        now = _utc_now()
        self._connection.execute(
            """
            INSERT INTO roots(format, path, source, enabled, created_at, updated_at)
            VALUES (?, ?, ?, 1, ?, ?)
            ON CONFLICT(format, path) DO UPDATE SET
                source = CASE
                    WHEN roots.source = 'registered' THEN roots.source
                    ELSE excluded.source
                END,
                enabled = 1,
                updated_at = excluded.updated_at
            """,
            (agent_format.value, normalized, source, now, now),
        )
        self._connection.commit()
        row = self._connection.execute(
            "SELECT * FROM roots WHERE format = ? AND path = ?",
            (agent_format.value, normalized),
        ).fetchone()
        assert row is not None
        return _root_from_row(row)

    def remove_root(self, root_id: int) -> bool:
        cursor = self._connection.execute("DELETE FROM roots WHERE id = ?", (root_id,))
        self._connection.commit()
        return cursor.rowcount > 0

    def roots(self, *, enabled_only: bool = False) -> list[CatalogRoot]:
        where = "WHERE enabled = 1" if enabled_only else ""
        rows = self._connection.execute(
            f"SELECT * FROM roots {where} ORDER BY format, path"  # noqa: S608
        ).fetchall()
        return [_root_from_row(row) for row in rows]

    def refresh(
        self,
        *,
        claude_roots: Sequence[Path] = (),
        codex_roots: Sequence[Path] = (),
        discover_under: Sequence[Path] = (),
        include_auto: bool = True,
        validate: bool = False,
        cwd: Path | None = None,
        environ: Mapping[str, str] | None = None,
        home: Path | None = None,
    ) -> RefreshResult:
        if include_auto:
            for agent_format, path, source in auto_roots(
                cwd=cwd, environ=environ, home=home
            ):
                self.add_root(agent_format, path, source=source)
        for path in claude_roots:
            self.add_root(AgentFormat.CLAUDE, path)
        for path in codex_roots:
            self.add_root(AgentFormat.CODEX, path)
        for agent_format, path, source in discover_roots(discover_under):
            self.add_root(agent_format, path, source=source)

        started = _utc_now()
        run = self._connection.execute(
            "INSERT INTO scan_runs(started_at, validate) VALUES (?, ?)",
            (started, int(validate)),
        )
        run_id = int(run.lastrowid)
        self._connection.commit()
        totals = {
            "files_seen": 0,
            "scanned": 0,
            "unchanged": 0,
            "missing": 0,
            "root_errors": 0,
        }
        for root in self.roots(enabled_only=True):
            result = self._refresh_root(root, validate=validate)
            for key in totals:
                totals[key] += result[key]

        finished = _utc_now()
        self._connection.execute(
            """
            UPDATE scan_runs SET finished_at = ?, files_seen = ?, scanned = ?,
                unchanged = ?, missing = ?, root_errors = ? WHERE id = ?
            """,
            (
                finished,
                totals["files_seen"],
                totals["scanned"],
                totals["unchanged"],
                totals["missing"],
                totals["root_errors"],
                run_id,
            ),
        )
        self._connection.commit()
        status_rows = self._connection.execute(
            "SELECT status, count(*) AS count FROM sessions GROUP BY status ORDER BY status"
        ).fetchall()
        statuses = {str(row["status"]): int(row["count"]) for row in status_rows}
        return RefreshResult(
            roots=len(self.roots(enabled_only=True)),
            files_seen=totals["files_seen"],
            scanned=totals["scanned"],
            unchanged=totals["unchanged"],
            missing=totals["missing"],
            statuses=statuses,
            root_errors=totals["root_errors"],
        )

    def _refresh_root(self, root: CatalogRoot, *, validate: bool) -> dict[str, int]:
        counts = {
            "files_seen": 0,
            "scanned": 0,
            "unchanged": 0,
            "missing": 0,
            "root_errors": 0,
        }
        root_path = Path(root.path)
        try:
            candidates = list(_candidate_files(AgentFormat(root.format), root_path))
        except OSError:
            self._record_root_failure(root.id, "root_unavailable")
            counts["root_errors"] = 1
            return counts
        if not root_path.is_dir():
            self._record_root_failure(root.id, "root_unavailable")
            counts["root_errors"] = 1
            return counts

        metadata = (
            _codex_native_metadata(root_path)
            if root.format == AgentFormat.CODEX.value
            else _NativeMetadata({}, {}, {}, False)
        )
        existing = {
            str(row["relative_path"]): row
            for row in self._connection.execute(
                "SELECT * FROM sessions WHERE root_id = ?", (root.id,)
            ).fetchall()
        }
        seen: set[str] = set()
        now = _utc_now()
        try:
            self._connection.execute("BEGIN")
            for path in candidates:
                relative = path.relative_to(root_path).as_posix()
                seen.add(relative)
                counts["files_seen"] += 1
                try:
                    before = file_snapshot(path)
                except JsonlError:
                    # A candidate can vanish between enumeration and stat.  It
                    # is retried on the next refresh and not invented here.
                    continue
                previous = existing.get(relative)
                unchanged = bool(
                    previous
                    and previous["status"] != "missing"
                    and int(previous["device"]) == before.device
                    and int(previous["inode"]) == before.inode
                    and int(previous["bytes"]) == before.size
                    and int(previous["modified_ns"]) == before.modified_ns
                    and (not validate or previous["status"] == "validated")
                )
                if unchanged:
                    counts["unchanged"] += 1
                    row_id = int(previous["id"])
                    self._replace_native_labels(row_id, previous, metadata)
                    continue

                scan = _scan_file(path, AgentFormat(root.format), root_path)
                if validate and scan.status == "candidate":
                    scan = _validated_scan(path, AgentFormat(root.format), scan)
                counts["scanned"] += 1
                row_id = self._upsert_session(
                    root,
                    path,
                    relative,
                    before,
                    scan,
                    now,
                    previous,
                )
                self._replace_labels(row_id, scan.labels)
                current = self._connection.execute(
                    "SELECT * FROM sessions WHERE id = ?", (row_id,)
                ).fetchone()
                assert current is not None
                self._replace_native_labels(row_id, current, metadata)

            missing_relatives = set(existing) - seen
            for relative in missing_relatives:
                previous = existing[relative]
                if previous["status"] != "missing":
                    counts["missing"] += 1
                    self._connection.execute(
                        """
                        UPDATE sessions SET status = 'missing', reason = 'file_missing',
                            missing_since = ?, indexed_at = ? WHERE id = ?
                        """,
                        (now, now, previous["id"]),
                    )
            self._refresh_display_titles(root.id)
            self._connection.execute(
                """
                UPDATE roots SET last_scan_at = ?, last_scan_status = 'ok',
                    last_error = NULL, updated_at = ? WHERE id = ?
                """,
                (now, now, root.id),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            self._record_root_failure(root.id, "scan_failed")
            counts["root_errors"] = 1
        return counts

    def _record_root_failure(self, root_id: int, code: str) -> None:
        now = _utc_now()
        self._connection.execute(
            """
            UPDATE roots SET last_scan_at = ?, last_scan_status = 'error',
                last_error = ?, updated_at = ? WHERE id = ?
            """,
            (now, code, now, root_id),
        )
        self._connection.commit()

    def _upsert_session(
        self,
        root: CatalogRoot,
        path: Path,
        relative: str,
        snapshot: Any,
        scan: _Scan,
        now: str,
        previous: sqlite3.Row | None,
    ) -> int:
        catalog_id = str(previous["catalog_id"]) if previous else uuid.uuid4().hex[:16]
        validated_at = now if scan.status == "validated" else None
        self._connection.execute(
            """
            INSERT INTO sessions(
                catalog_id, root_id, relative_path, canonical_path, format,
                session_id, filename_session_id, cwd, started_at, started_at_epoch,
                cli_version, history_mode, kind, lifecycle, parent_session_id,
                status, reason,
                records, device, inode, bytes, modified_ns, indexed_at,
                validated_at, missing_since
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(root_id, relative_path) DO UPDATE SET
                canonical_path = excluded.canonical_path,
                format = excluded.format,
                session_id = excluded.session_id,
                filename_session_id = excluded.filename_session_id,
                cwd = excluded.cwd,
                started_at = excluded.started_at,
                started_at_epoch = excluded.started_at_epoch,
                cli_version = excluded.cli_version,
                history_mode = excluded.history_mode,
                kind = excluded.kind,
                lifecycle = excluded.lifecycle,
                parent_session_id = excluded.parent_session_id,
                status = excluded.status,
                reason = excluded.reason,
                records = excluded.records,
                device = excluded.device,
                inode = excluded.inode,
                bytes = excluded.bytes,
                modified_ns = excluded.modified_ns,
                indexed_at = excluded.indexed_at,
                validated_at = excluded.validated_at,
                missing_since = NULL
            """,
            (
                catalog_id,
                root.id,
                relative,
                str(_absolute(path)),
                root.format,
                scan.session_id,
                scan.filename_session_id,
                scan.cwd,
                scan.started_at,
                _timestamp_epoch(scan.started_at),
                scan.cli_version,
                scan.history_mode,
                scan.kind,
                scan.lifecycle,
                scan.parent_session_id,
                scan.status,
                scan.reason,
                scan.records,
                snapshot.device,
                snapshot.inode,
                snapshot.size,
                snapshot.modified_ns,
                now,
                validated_at,
            ),
        )
        row = self._connection.execute(
            "SELECT id FROM sessions WHERE root_id = ? AND relative_path = ?",
            (root.id, relative),
        ).fetchone()
        assert row is not None
        return int(row["id"])

    def _replace_labels(self, row_id: int, labels: Iterable[_Label]) -> None:
        self._connection.execute(
            """
            DELETE FROM session_labels WHERE session_row_id = ?
            AND kind NOT IN ('native_name', 'native_title')
            """,
            (row_id,),
        )
        self._insert_labels(row_id, labels)

    def _replace_native_labels(
        self, row_id: int, row: sqlite3.Row, metadata: _NativeMetadata
    ) -> None:
        if not metadata.loaded:
            return
        path_labels = metadata.by_path.get(str(row["canonical_path"]), ())
        id_labels = metadata.by_id.get(str(row["session_id"]), ()) if row["session_id"] else ()
        desired_labels = {
            (label.kind, label.value, label.ordinal, label.priority)
            for label in (*path_labels, *id_labels)
        }
        existing_labels = {
            (str(label["kind"]), str(label["value"]), int(label["ordinal"]), int(label["priority"]))
            for label in self._connection.execute(
                """
                SELECT kind, value, ordinal, priority FROM session_labels
                WHERE session_row_id = ? AND kind IN ('native_name', 'native_title')
                """,
                (row_id,),
            ).fetchall()
        }
        if desired_labels != existing_labels:
            self._connection.execute(
                """
                DELETE FROM session_labels
                WHERE session_row_id = ? AND kind IN ('native_name', 'native_title')
                """,
                (row_id,),
            )
            self._insert_labels(row_id, (*path_labels, *id_labels))
        if str(row["format"]) == AgentFormat.CODEX.value:
            parent = (
                metadata.parent_by_id.get(str(row["session_id"]))
                if row["session_id"]
                else None
            )
            current_kind = str(row["kind"])
            desired_kind = "subagent" if parent else (
                "main" if current_kind == "subagent" else current_kind
            )
            if parent != row["parent_session_id"] or desired_kind != current_kind:
                self._connection.execute(
                    "UPDATE sessions SET parent_session_id = ?, kind = ? WHERE id = ?",
                    (parent, desired_kind, row_id),
                )

    def _insert_labels(self, row_id: int, labels: Iterable[_Label]) -> None:
        seen: set[tuple[str, str]] = set()
        for label in labels:
            key = (label.kind, label.value)
            if key in seen:
                continue
            seen.add(key)
            self._connection.execute(
                """
                INSERT OR IGNORE INTO session_labels(
                    session_row_id, kind, value, ordinal, priority
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    row_id,
                    label.kind,
                    label.value,
                    label.ordinal,
                    label.priority,
                ),
            )

    def _refresh_display_titles(self, root_id: int) -> None:
        rows = self._connection.execute(
            "SELECT id FROM sessions WHERE root_id = ?", (root_id,)
        ).fetchall()
        for row in rows:
            label = self._connection.execute(
                """
                SELECT kind, value FROM session_labels WHERE session_row_id = ?
                ORDER BY priority DESC, ordinal DESC, id DESC LIMIT 1
                """,
                (row["id"],),
            ).fetchone()
            value = label["value"] if label else None
            kind = label["kind"] if label else None
            current = self._connection.execute(
                "SELECT display_title, display_title_kind FROM sessions WHERE id = ?",
                (row["id"],),
            ).fetchone()
            assert current is not None
            if current["display_title"] != value or current["display_title_kind"] != kind:
                self._connection.execute(
                    "UPDATE sessions SET display_title = ?, display_title_kind = ? WHERE id = ?",
                    (value, kind, row["id"]),
                )

    def list_sessions(
        self,
        *,
        query: str | None = None,
        include_paths: bool = False,
        include_missing: bool = False,
        agent_format: AgentFormat | None = None,
        statuses: Sequence[str] = (),
        kinds: Sequence[str] = (),
        lifecycles: Sequence[str] = (),
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[CatalogEntry]:
        if limit < 1 or limit > 10_000:
            raise SessionBridgeError("catalog limit must be between 1 and 10000")
        if offset < 0:
            raise SessionBridgeError("catalog offset cannot be negative")
        where: list[str] = []
        parameters: list[Any] = []
        if not include_missing:
            where.append("s.status != 'missing'")
        if agent_format:
            where.append("s.format = ?")
            parameters.append(agent_format.value)
        if statuses:
            where.append(f"s.status IN ({','.join('?' for _ in statuses)})")
            parameters.extend(statuses)
        if kinds:
            where.append(f"s.kind IN ({','.join('?' for _ in kinds)})")
            parameters.extend(kinds)
        if lifecycles:
            where.append(f"s.lifecycle IN ({','.join('?' for _ in lifecycles)})")
            parameters.extend(lifecycles)
        if since:
            where.append("s.started_at_epoch >= ?")
            parameters.append(_required_timestamp_epoch(since, "--since"))
        if until:
            where.append("s.started_at_epoch <= ?")
            parameters.append(_required_timestamp_epoch(until, "--until"))
        if query is not None:
            normalized = query.casefold().strip()
            if not normalized:
                raise SessionBridgeError("catalog search query cannot be empty")
            search = [
                "instr(lower(COALESCE(s.session_id, '')), ?) > 0",
                "instr(lower(COALESCE(s.filename_session_id, '')), ?) > 0",
                "EXISTS (SELECT 1 FROM session_labels l "
                "WHERE l.session_row_id = s.id "
                "AND instr(session_casefold(l.value), ?) > 0)",
            ]
            parameters.extend((normalized, normalized, normalized))
            if include_paths:
                search.extend(
                    (
                        "instr(lower(COALESCE(s.cwd, '')), ?) > 0",
                        "instr(lower(s.canonical_path), ?) > 0",
                    )
                )
                parameters.extend((normalized, normalized))
            where.append(f"({' OR '.join(search)})")
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        rows = self._connection.execute(
            f"""
            SELECT s.*, r.path AS root_path,
                (SELECT count(*) FROM sessions duplicate
                 WHERE duplicate.format = s.format
                   AND COALESCE(duplicate.session_id, duplicate.filename_session_id) =
                       COALESCE(s.session_id, s.filename_session_id)
                   AND COALESCE(duplicate.session_id, duplicate.filename_session_id) IS NOT NULL
                   AND duplicate.status != 'missing') AS duplicate_count
            FROM sessions s JOIN roots r ON r.id = s.root_id
            {clause}
            ORDER BY s.modified_ns DESC, s.catalog_id
            LIMIT ? OFFSET ?
            """,  # noqa: S608
            (*parameters, limit, offset),
        ).fetchall()
        return [_entry_from_row(row, include_paths=include_paths) for row in rows]

    def get_session(self, catalog_id: str, *, include_paths: bool = False) -> CatalogEntry:
        row = self._connection.execute(
            """
            SELECT s.*, r.path AS root_path,
                (SELECT count(*) FROM sessions duplicate
                 WHERE duplicate.format = s.format
                   AND COALESCE(duplicate.session_id, duplicate.filename_session_id) =
                       COALESCE(s.session_id, s.filename_session_id)
                   AND COALESCE(duplicate.session_id, duplicate.filename_session_id) IS NOT NULL
                   AND duplicate.status != 'missing') AS duplicate_count
            FROM sessions s JOIN roots r ON r.id = s.root_id
            WHERE s.catalog_id = ?
            """,
            (catalog_id,),
        ).fetchone()
        if row is None:
            raise SessionBridgeError("catalog session ID was not found")
        return _entry_from_row(row, include_paths=include_paths)

    def session_path_for_transfer(self, catalog_id: str) -> tuple[AgentFormat, Path]:
        entry = self.get_session(catalog_id, include_paths=True)
        if entry.status == "missing":
            raise SessionBridgeError("catalog session source file is missing; refresh the catalog")
        if entry.status in {"unsupported", "corrupt", "oversized", "busy", "unreadable"}:
            raise SessionBridgeError(
                f"catalog session is not transferable: {entry.status}"
                + (f" ({entry.reason})" if entry.reason else "")
            )
        assert entry.path is not None
        return AgentFormat(entry.format), Path(entry.path)


def _candidate_files(agent_format: AgentFormat, root: Path) -> Iterable[Path]:
    if not root.is_dir():
        raise OSError("root is not available")
    directories = [root / "projects"] if agent_format == AgentFormat.CLAUDE else [
        root / "sessions",
        root / "archived_sessions",
    ]
    candidates: list[Path] = []
    for directory in directories:
        if not directory.is_dir():
            continue
        for current, subdirectories, filenames in os.walk(directory, followlinks=False):
            subdirectories[:] = [
                name
                for name in subdirectories
                if not (Path(current) / name).is_symlink()
            ]
            for filename in filenames:
                path = Path(current) / filename
                if path.suffix == ".jsonl" and not path.is_symlink():
                    candidates.append(path)
    yield from sorted(candidates)


def _scan_file(path: Path, agent_format: AgentFormat, root: Path) -> _Scan:
    identity_labels = _native_key_labels(path, agent_format, root)
    try:
        size = path.stat().st_size
    except OSError:
        return _replace_scan_labels(
            _base_scan(path, agent_format, root, "unreadable", "file_unreadable"),
            identity_labels,
        )
    if size > DEFAULT_MAX_TOTAL_BYTES:
        return _replace_scan_labels(
            _base_scan(path, agent_format, root, "oversized", "file_size_limit"),
            identity_labels,
        )
    try:
        before = file_snapshot(path)
    except JsonlError:
        return _replace_scan_labels(
            _base_scan(path, agent_format, root, "unreadable", "file_unreadable"),
            identity_labels,
        )
    labels: list[_Label] = list(identity_labels)
    session_id = None
    cwd = None
    started_at = None
    cli_version = None
    history_mode = None
    history_base = False
    sidechain = False
    records = 0
    has_conversation = False
    has_session_meta = False
    wrong_format = False
    try:
        for record in iter_jsonl(path):
            records += 1
            value = record.value
            record_type = _string(value.get("type"))
            if agent_format == AgentFormat.CLAUDE:
                if record_type == "session_meta" and isinstance(value.get("payload"), dict):
                    wrong_format = True
                session_id = session_id or _normalized_uuid(_string(value.get("sessionId")))
                cwd = cwd or _bounded(_string(value.get("cwd")), PATH_VALUE_LIMIT)
                started_at = started_at or _string(value.get("timestamp"))
                cli_version = cli_version or _string(value.get("version"))
                sidechain = sidechain or value.get("isSidechain") is True
                agent_id = _bounded(_string(value.get("agentId")), LABEL_LIMIT)
                if agent_id:
                    labels.append(_Label("agent_id", agent_id, record.index, 10))
                has_conversation = has_conversation or record_type in {"user", "assistant"}
                if record_type == "custom-title":
                    title = _bounded(_string(value.get("customTitle")), LABEL_LIMIT)
                    if title:
                        labels.append(_Label("custom_title", title, record.index, 100))
                elif record_type == "ai-title":
                    title = _bounded(_string(value.get("aiTitle")), LABEL_LIMIT)
                    if title:
                        labels.append(_Label("ai_title", title, record.index, 90))
            else:
                if record_type in {"user", "assistant"} and isinstance(
                    value.get("message"), dict
                ):
                    wrong_format = True
                payload = value.get("payload")
                if not isinstance(payload, dict):
                    payload = {}
                if record_type == "session_meta":
                    has_session_meta = True
                    session_id = session_id or _normalized_uuid(
                        _string(payload.get("id")) or _string(payload.get("session_id"))
                    )
                    cwd = cwd or _bounded(_string(payload.get("cwd")), PATH_VALUE_LIMIT)
                    started_at = started_at or _string(payload.get("timestamp")) or _string(
                        value.get("timestamp")
                    )
                    cli_version = cli_version or _string(payload.get("cli_version"))
                    history_mode = history_mode or _string(payload.get("history_mode"))
                    history_base = history_base or payload.get("history_base") is not None
                elif record_type == "response_item":
                    has_conversation = has_conversation or payload.get("type") in {
                        "message",
                        "function_call",
                        "custom_tool_call",
                        "function_call_output",
                        "custom_tool_call_output",
                    }
                elif record_type == "event_msg" and payload.get("type") == "thread_name_updated":
                    title = _bounded(
                        _string(payload.get("name"))
                        or _string(payload.get("thread_name")),
                        LABEL_LIMIT,
                    )
                    if title:
                        labels.append(_Label("thread_name", title, record.index, 110))
        ensure_file_unchanged(path, before)
    except JsonlError as exc:
        code = "file_changed" if "changed while" in str(exc) else "invalid_jsonl"
        status = "busy" if code == "file_changed" else "corrupt"
        base = _base_scan(path, agent_format, root, status, code)
        return _Scan(
            session_id=session_id,
            filename_session_id=base.filename_session_id,
            cwd=cwd,
            started_at=started_at,
            cli_version=cli_version,
            history_mode=history_mode,
            kind=base.kind,
            lifecycle=base.lifecycle,
            parent_session_id=base.parent_session_id,
            status=status,
            reason=code,
            records=records,
            labels=tuple(labels),
        )
    except OSError:
        return _base_scan(path, agent_format, root, "unreadable", "file_unreadable")

    base = _base_scan(path, agent_format, root, "candidate", None)
    status = "candidate"
    reason = None
    if records == 0:
        status, reason = "corrupt", "empty_jsonl"
    elif wrong_format:
        status, reason = "corrupt", "format_mismatch"
    elif agent_format == AgentFormat.CLAUDE:
        if base.kind == "sidechain" or sidechain:
            status, reason = "unsupported", "claude_sidechain"
        elif not has_conversation:
            status, reason = "corrupt", "no_conversation_records"
    else:
        if not has_session_meta:
            status, reason = "corrupt", "missing_session_meta"
        elif history_mode and history_mode != "legacy":
            status, reason = "unsupported", "codex_history_mode"
        elif history_base:
            status, reason = "unsupported", "codex_history_base"
        elif not has_conversation:
            status, reason = "corrupt", "no_conversation_records"
    return _Scan(
        session_id=session_id,
        filename_session_id=base.filename_session_id,
        cwd=cwd,
        started_at=started_at,
        cli_version=cli_version,
        history_mode=history_mode,
        kind=base.kind,
        lifecycle=base.lifecycle,
        parent_session_id=base.parent_session_id,
        status=status,
        reason=reason,
        records=records,
        labels=tuple(labels),
    )


def _validated_scan(path: Path, agent_format: AgentFormat, scan: _Scan) -> _Scan:
    try:
        session = load_session(path, agent_format)
        target = (
            AgentFormat.CODEX
            if agent_format == AgentFormat.CLAUDE
            else AgentFormat.CLAUDE
        )
        convert_session(session, ConversionOptions(target_format=target))
    except (SessionBridgeError, JsonlError) as exc:
        code = "file_changed" if "changed while" in str(exc) else "conversion_validation_failed"
        status = "busy" if code == "file_changed" else "corrupt"
        return _replace_scan_status(scan, status, code)
    return _replace_scan_status(scan, "validated", None)


def _replace_scan_status(scan: _Scan, status: str, reason: str | None) -> _Scan:
    return _Scan(
        session_id=scan.session_id,
        filename_session_id=scan.filename_session_id,
        cwd=scan.cwd,
        started_at=scan.started_at,
        cli_version=scan.cli_version,
        history_mode=scan.history_mode,
        kind=scan.kind,
        lifecycle=scan.lifecycle,
        parent_session_id=scan.parent_session_id,
        status=status,
        reason=reason,
        records=scan.records,
        labels=scan.labels,
    )


def _replace_scan_labels(scan: _Scan, labels: Iterable[_Label]) -> _Scan:
    return _Scan(
        session_id=scan.session_id,
        filename_session_id=scan.filename_session_id,
        cwd=scan.cwd,
        started_at=scan.started_at,
        cli_version=scan.cli_version,
        history_mode=scan.history_mode,
        kind=scan.kind,
        lifecycle=scan.lifecycle,
        parent_session_id=scan.parent_session_id,
        status=scan.status,
        reason=scan.reason,
        records=scan.records,
        labels=tuple(labels),
    )


def _native_key_labels(
    path: Path, agent_format: AgentFormat, root: Path
) -> tuple[_Label, ...]:
    base = _base_scan(path, agent_format, root, "candidate", None)
    if agent_format != AgentFormat.CLAUDE or base.kind != "sidechain":
        return ()
    native_key = _bounded(path.stem, LABEL_LIMIT)
    return (_Label("native_key", native_key, -1, 5),) if native_key else ()


def _base_scan(
    path: Path,
    agent_format: AgentFormat,
    root: Path,
    status: str,
    reason: str | None,
) -> _Scan:
    relative = path.relative_to(root)
    filename_id = _filename_uuid(path)
    parent_id = None
    if agent_format == AgentFormat.CLAUDE:
        parts = relative.parts
        sidechain = "subagents" in parts
        if sidechain:
            index = parts.index("subagents")
            if index > 0:
                parent_id = _normalized_uuid(parts[index - 1])
        kind = "sidechain" if sidechain else "main"
        lifecycle = "project"
    else:
        kind = "main"
        lifecycle = "archived" if relative.parts[0] == "archived_sessions" else "active"
    return _Scan(
        session_id=None,
        filename_session_id=filename_id,
        cwd=None,
        started_at=None,
        cli_version=None,
        history_mode=None,
        kind=kind,
        lifecycle=lifecycle,
        parent_session_id=parent_id,
        status=status,
        reason=reason,
        records=None,
        labels=(),
    )


def _codex_native_metadata(root: Path) -> _NativeMetadata:
    databases: list[tuple[int, Path]] = []
    try:
        for path in root.glob("state_*.sqlite"):
            match = _STATE_DATABASE.match(path.name)
            if match and path.is_file():
                databases.append((int(match.group("version")), path))
    except OSError:
        return _NativeMetadata({}, {}, {}, False)
    if not databases:
        return _NativeMetadata({}, {}, {}, False)
    database = max(databases)[1]
    by_path: dict[str, tuple[_Label, ...]] = {}
    by_id: dict[str, tuple[_Label, ...]] = {}
    parents: dict[str, str] = {}
    connection: sqlite3.Connection | None = None
    try:
        uri = f"{database.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=1)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(threads)").fetchall()
        }
        required = {"id", "rollout_path"}
        if not required.issubset(columns):
            return _NativeMetadata({}, {}, {}, False)
        selected = ["id", "rollout_path"]
        selected.extend(
            f"substr({name}, 1, {LABEL_LIMIT}) AS {name}"
            for name in ("name", "title")
            if name in columns
        )
        query = f"SELECT {', '.join(selected)} FROM threads"  # noqa: S608
        for row in connection.execute(query):
            labels: list[_Label] = []
            if "name" in columns:
                value = _bounded(_string(row["name"]), LABEL_LIMIT)
                if value:
                    labels.append(_Label("native_name", value, 0, 100))
            if "title" in columns:
                value = _bounded(_string(row["title"]), LABEL_LIMIT)
                if value:
                    labels.append(_Label("native_title", value, 0, 80))
            if not labels:
                continue
            native_id = _normalized_uuid(_string(row["id"]))
            rollout_path = _string(row["rollout_path"])
            frozen = tuple(labels)
            if native_id:
                by_id[native_id] = frozen
            if rollout_path:
                by_path[str(_absolute(Path(rollout_path)))] = frozen
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "thread_spawn_edges" in tables:
            for row in connection.execute(
                "SELECT parent_thread_id, child_thread_id FROM thread_spawn_edges"
            ):
                parent = _normalized_uuid(_string(row[0]))
                child = _normalized_uuid(_string(row[1]))
                if parent and child:
                    parents[child] = parent
    except sqlite3.Error:
        return _NativeMetadata({}, {}, {}, False)
    finally:
        if connection is not None:
            connection.close()
    return _NativeMetadata(by_path, by_id, parents, True)


def _entry_from_row(row: sqlite3.Row, *, include_paths: bool) -> CatalogEntry:
    return CatalogEntry(
        catalog_id=str(row["catalog_id"]),
        format=str(row["format"]),
        session_id=_string(row["session_id"]),
        filename_session_id=_string(row["filename_session_id"]),
        title=_string(row["display_title"]),
        title_kind=_string(row["display_title_kind"]),
        kind=str(row["kind"]),
        lifecycle=str(row["lifecycle"]),
        status=str(row["status"]),
        reason=_string(row["reason"]),
        duplicate=int(row["duplicate_count"]) > 1,
        started_at=_string(row["started_at"]),
        cli_version=_string(row["cli_version"]),
        history_mode=_string(row["history_mode"]),
        records=int(row["records"]) if row["records"] is not None else None,
        bytes=int(row["bytes"]),
        cwd=_string(row["cwd"]) if include_paths else None,
        path=str(row["canonical_path"]) if include_paths else None,
        root=str(row["root_path"]) if include_paths else None,
    )


def _root_from_row(row: sqlite3.Row) -> CatalogRoot:
    return CatalogRoot(
        id=int(row["id"]),
        format=str(row["format"]),
        path=str(row["path"]),
        source=str(row["source"]),
        enabled=bool(row["enabled"]),
        last_scan_at=_string(row["last_scan_at"]),
        last_scan_status=_string(row["last_scan_status"]),
        last_error=_string(row["last_error"]),
    )


def _filename_uuid(path: Path) -> str | None:
    match = _UUID_SUFFIX.search(path.stem)
    return _normalized_uuid(match.group("id")) if match else None


def _normalized_uuid(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _bounded(value: str | None, limit: int) -> str | None:
    return value[:limit] if value else None


def _timestamp_epoch(value: str | None) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def _required_timestamp_epoch(value: str, option: str) -> float:
    parsed = _timestamp_epoch(value)
    if parsed is None:
        raise SessionBridgeError(f"{option} must be a timezone-aware RFC-3339 timestamp")
    return parsed


def _absolute(path: Path) -> Path:
    return Path(os.path.realpath(os.path.abspath(path.expanduser())))


def _make_private_parent(path: Path) -> None:
    missing: list[Path] = []
    cursor = path
    while not os.path.lexists(cursor):
        missing.append(cursor)
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
