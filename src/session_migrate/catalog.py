"""Private, filesystem-authoritative catalog of native agent sessions.

The catalog stores operational metadata and native session titles only.  It
never stores message bodies, tool arguments/results, previews, or first-user
messages. Native JSONL files remain authoritative; agent-owned indexes are
optional sources of title and lineage metadata.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from session_migrate.conversion import ConversionOptions, convert_session, load_session
from session_migrate.errors import JsonlError, SessionMigrateError
from session_migrate.formats import antigravity, kimi, omp, openhands, vibe
from session_migrate.formats import cursor as cursor_format
from session_migrate.jsonl import (
    DEFAULT_MAX_TOTAL_BYTES,
    ensure_file_unchanged,
    file_snapshot,
    iter_jsonl,
)
from session_migrate.model import AgentFormat, EventKind, Role

SCHEMA_VERSION = 4
LABEL_LIMIT = 512
PATH_VALUE_LIMIT = 32_768
_UUID_SUFFIX = re.compile(
    r"(?P<id>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12})$"
)
_STATE_DATABASE = re.compile(r"state_(?P<version>[0-9]+)\.sqlite$")
_OPENCODE_SESSION_ID = re.compile(r"ses_[0-9A-Za-z]{20,64}$")


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
class CatalogTransferSource:
    """Authoritative source selected by an opaque catalog ID.

    Filesystem formats expose ``path``. OpenCode is deliberately virtual: its
    official exporter consumes a native session ID from the store rooted at
    ``root``, so the catalog never represents ``opencode.db`` as a transcript.
    """

    format: AgentFormat
    session_id: str | None
    root: Path
    path: Path | None

    @property
    def is_virtual(self) -> bool:
        return self.path is None


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


@dataclass(frozen=True, slots=True)
class _VirtualSnapshot:
    device: int
    inode: int
    size: int
    modified_ns: int
    fingerprint: str


class _OpenCodeInventoryError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def default_catalog_path(
    *, environ: Mapping[str, str] | None = None, home: Path | None = None
) -> Path:
    """Return the private catalog path without creating it."""

    values = os.environ if environ is None else environ
    configured = values.get("SESSION_MIGRATE_CATALOG")
    if configured:
        return _absolute(Path(configured))
    state_home = values.get("XDG_STATE_HOME")
    base = _absolute(Path(state_home)) if state_home else (home or Path.home()) / ".local/state"
    return _absolute(base / "session-migrate/catalog.sqlite3")


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
    data_home_value = values.get("XDG_DATA_HOME")
    opencode_home = (
        _absolute(Path(data_home_value)) / "opencode"
        if data_home_value
        else user_home / ".local" / "share" / "opencode"
    )
    kilo_home = (
        _absolute(Path(data_home_value)) / "kilo"
        if data_home_value
        else user_home / ".local" / "share" / "kilo"
    )
    cursor_home = _absolute(cursor_format.config_home(user_home, environ=values))
    candidates: list[tuple[AgentFormat, Path, str]] = [
        (AgentFormat.CLAUDE, user_home / ".claude", "default"),
        (AgentFormat.CODEX, user_home / ".codex", "default"),
        (AgentFormat.PI, user_home / ".pi" / "agent", "default"),
        (AgentFormat.OMP, user_home / ".omp" / "agent", "default"),
        (
            AgentFormat.OPENCODE,
            opencode_home,
            "environment" if data_home_value else "default",
        ),
        (
            AgentFormat.KILO,
            kilo_home,
            "environment" if data_home_value else "default",
        ),
        (AgentFormat.COPILOT, user_home / ".copilot", "default"),
        (
            AgentFormat.ANTIGRAVITY,
            user_home / ".gemini" / "antigravity-cli",
            "default",
        ),
        (
            AgentFormat.CURSOR,
            cursor_home,
            "environment"
            if values.get("CURSOR_CONFIG_DIR") or values.get("XDG_CONFIG_HOME")
            else "default",
        ),
        (
            AgentFormat.VIBE,
            _absolute(Path(values["VIBE_HOME"]))
            if values.get("VIBE_HOME")
            else user_home / ".vibe",
            "environment" if values.get("VIBE_HOME") else "default",
        ),
        (
            AgentFormat.MUSE,
            (_absolute(Path(data_home_value)) if data_home_value else user_home / ".local/share")
            / "muse",
            "environment" if data_home_value else "default",
        ),
        (
            AgentFormat.QWEN,
            _absolute(Path(values["QWEN_HOME"]))
            if values.get("QWEN_HOME")
            else user_home / ".qwen",
            "environment" if values.get("QWEN_HOME") else "default",
        ),
        (
            AgentFormat.KIMI,
            _absolute(Path(values["KIMI_CODE_HOME"]))
            if values.get("KIMI_CODE_HOME")
            else user_home / ".kimi-code",
            "environment" if values.get("KIMI_CODE_HOME") else "default",
        ),
        (
            AgentFormat.GROK,
            _absolute(Path(values["GROK_HOME"]))
            if values.get("GROK_HOME")
            else user_home / ".grok",
            "environment" if values.get("GROK_HOME") else "default",
        ),
        (
            AgentFormat.OPENHANDS,
            _absolute(Path(values["OPENHANDS_CONVERSATIONS_DIR"]))
            if values.get("OPENHANDS_CONVERSATIONS_DIR")
            else user_home / ".openhands" / "conversations",
            "environment" if values.get("OPENHANDS_CONVERSATIONS_DIR") else "default",
        ),
    ]
    configured = (
        (AgentFormat.CLAUDE, values.get("CLAUDE_CONFIG_DIR")),
        (AgentFormat.CODEX, values.get("CODEX_HOME")),
        (AgentFormat.COPILOT, values.get("COPILOT_HOME")),
    )
    for agent_format, value in configured:
        if value:
            candidates.append((agent_format, _absolute(Path(value)), "environment"))
    pi_family_home = values.get("PI_CODING_AGENT_DIR")
    if pi_family_home:
        path = _absolute(Path(pi_family_home))
        candidates.append((_pi_family_root_format(path), path, "environment"))

    cursor = _absolute(cwd or Path.cwd())
    for directory in (cursor, *cursor.parents):
        claude_home = directory / ".claude"
        if (claude_home / "projects").is_dir():
            candidates.append((AgentFormat.CLAUDE, claude_home, "project"))
        codex_home = directory / ".codex"
        if (codex_home / "sessions").is_dir() or (codex_home / "archived_sessions").is_dir():
            candidates.append((AgentFormat.CODEX, codex_home, "project"))
        pi_home = directory / ".pi" / "agent"
        if (pi_home / "sessions").is_dir():
            candidates.append((AgentFormat.PI, pi_home, "project"))
        omp_home = directory / ".omp" / "agent"
        if (omp_home / "sessions").is_dir():
            candidates.append((AgentFormat.OMP, omp_home, "project"))
        copilot_home = directory / ".copilot"
        if (copilot_home / "session-state").is_dir():
            candidates.append((AgentFormat.COPILOT, copilot_home, "project"))
        antigravity_home = directory / ".gemini" / "antigravity-cli"
        if (antigravity_home / "conversations").is_dir():
            candidates.append((AgentFormat.ANTIGRAVITY, antigravity_home, "project"))
        cursor_home = directory / ".cursor"
        if (cursor_home / "chats").is_dir():
            candidates.append((AgentFormat.CURSOR, cursor_home, "project"))
        vibe_home = directory / ".vibe"
        if (vibe_home / "logs/session").is_dir():
            candidates.append((AgentFormat.VIBE, vibe_home, "project"))
        qwen_home = directory / ".qwen"
        if (qwen_home / "projects").is_dir():
            candidates.append((AgentFormat.QWEN, qwen_home, "project"))
        kimi_home = directory / ".kimi-code"
        if (kimi_home / "sessions").is_dir():
            candidates.append((AgentFormat.KIMI, kimi_home, "project"))
        grok_home = directory / ".grok"
        if (grok_home / "sessions").is_dir():
            candidates.append((AgentFormat.GROK, grok_home, "project"))
        openhands_home = directory / ".openhands" / "conversations"
        if openhands_home.is_dir():
            candidates.append((AgentFormat.OPENHANDS, openhands_home, "project"))

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


def _pi_family_root_format(path: Path) -> AgentFormat:
    """Classify the shared ``PI_CODING_AGENT_DIR`` without double indexing it."""

    sessions = path / "sessions"
    if sessions.is_dir():
        for candidate in sorted(sessions.glob("*/*.jsonl")):
            if candidate.is_symlink() or not candidate.is_file():
                continue
            try:
                descriptor = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                try:
                    prefix = os.read(descriptor, omp.TITLE_SLOT_BYTES)
                finally:
                    os.close(descriptor)
                first = json.loads(prefix.split(b"\n", 1)[0])
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                continue
            if isinstance(first, dict) and first.get("type") == "title" and first.get("v") == 1:
                return AgentFormat.OMP
            if isinstance(first, dict) and first.get("type") == "session":
                return AgentFormat.PI
    return AgentFormat.OMP if ".omp" in path.parts else AgentFormat.PI


def discover_roots(search_paths: Sequence[Path]) -> list[tuple[AgentFormat, Path, str]]:
    """Find project-local agent homes below explicit subtrees.

    Symlinked directories are never followed, and only directories with native
    store markers are returned.  This function never widens the caller's
    supplied search boundaries.
    """

    found: list[tuple[AgentFormat, Path, str]] = []
    seen: set[tuple[AgentFormat, str]] = set()
    for search_path in search_paths:
        boundary = _absolute(search_path)
        if not boundary.is_dir():
            raise SessionMigrateError(f"catalog discovery path is not a directory: {boundary}")
        try:
            walker = os.walk(boundary, followlinks=False, onerror=_raise_walk_error)
            for current, subdirectories, _filenames in walker:
                current_path = Path(current)
                subdirectories[:] = [
                    name for name in subdirectories if not (current_path / name).is_symlink()
                ]
                candidates: list[tuple[AgentFormat, Path]] = []
                if current_path.name == ".claude" and (current_path / "projects").is_dir():
                    candidates.append((AgentFormat.CLAUDE, current_path))
                if current_path.name == ".codex" and (
                    (current_path / "sessions").is_dir()
                    or (current_path / "archived_sessions").is_dir()
                ):
                    candidates.append((AgentFormat.CODEX, current_path))
                if current_path.name == ".pi" and (current_path / "agent" / "sessions").is_dir():
                    candidates.append((AgentFormat.PI, current_path / "agent"))
                if current_path.name == ".omp" and (current_path / "agent" / "sessions").is_dir():
                    candidates.append((AgentFormat.OMP, current_path / "agent"))
                if current_path.name == ".copilot" and (current_path / "session-state").is_dir():
                    candidates.append((AgentFormat.COPILOT, current_path))
                if (
                    current_path.name == "antigravity-cli"
                    and current_path.parent.name == ".gemini"
                    and (current_path / "conversations").is_dir()
                ):
                    candidates.append((AgentFormat.ANTIGRAVITY, current_path))
                if current_path.name == ".cursor" and (current_path / "chats").is_dir():
                    candidates.append((AgentFormat.CURSOR, current_path))
                if current_path.name == ".vibe" and (current_path / "logs/session").is_dir():
                    candidates.append((AgentFormat.VIBE, current_path))
                if current_path.name == ".qwen" and (current_path / "projects").is_dir():
                    candidates.append((AgentFormat.QWEN, current_path))
                if current_path.name == ".kimi-code" and (current_path / "sessions").is_dir():
                    candidates.append((AgentFormat.KIMI, current_path))
                if current_path.name == ".grok" and (current_path / "sessions").is_dir():
                    candidates.append((AgentFormat.GROK, current_path))
                if (
                    current_path.name == "conversations"
                    and current_path.parent.name == ".openhands"
                ):
                    candidates.append((AgentFormat.OPENHANDS, current_path))
                if current_path.name == "kilo" and (current_path / "kilo.db").is_file():
                    candidates.append((AgentFormat.KILO, current_path))
                for agent_format, path in candidates:
                    key = (agent_format, str(path))
                    if key not in seen:
                        seen.add(key)
                        found.append((agent_format, path, "discovered"))
                if candidates:
                    # Native homes can be very large and cannot contain another
                    # project-local home without an explicit, unusual nesting.
                    subdirectories.clear()
        except OSError as exc:
            raise SessionMigrateError(
                f"catalog could not completely scan discovery boundary: {boundary}"
            ) from exc
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
        except SessionMigrateError:
            if connection is not None:
                connection.close()
            raise
        except (OSError, sqlite3.Error, ValueError) as exc:
            if connection is not None:
                connection.close()
            raise SessionMigrateError(
                "cannot open the private session catalog; move the disposable "
                "database aside and run `session-migrate catalog refresh`"
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
                format TEXT NOT NULL,
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
                format TEXT NOT NULL,
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
                source_fingerprint TEXT,
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
            self._migrate_v2_to_v3()
            self._migrate_v3_to_v4()
        elif observed == 2:
            self._migrate_v2_to_v3()
            self._migrate_v3_to_v4()
        elif observed == 3:
            self._migrate_v3_to_v4()
        elif observed is not None and observed != SCHEMA_VERSION:
            raise SessionMigrateError(
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
                self._connection.execute("ALTER TABLE sessions ADD COLUMN started_at_epoch REAL")
            label_columns = {
                str(row[1])
                for row in self._connection.execute("PRAGMA table_info(session_labels)").fetchall()
            }
            if "normalized" in label_columns:
                self._connection.execute("ALTER TABLE session_labels RENAME TO session_labels_v1")
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
                ("2",),
            )
            self._connection.execute("PRAGMA user_version = 2")
            self._connection.commit()
        except (sqlite3.Error, ValueError) as exc:
            self._connection.rollback()
            raise SessionMigrateError(
                "catalog schema migration failed; preserve registered roots or rebuild the "
                "disposable catalog with `session-migrate catalog refresh`"
            ) from exc

    def _migrate_v3_to_v4(self) -> None:
        """Add OpenCode/Copilot roots and virtual-source fingerprints losslessly."""

        try:
            self._connection.commit()
            self._connection.execute("PRAGMA foreign_keys = OFF")
            self._connection.executescript(
                """
                BEGIN IMMEDIATE;
                DROP INDEX IF EXISTS sessions_uuid_idx;
                DROP INDEX IF EXISTS sessions_status_idx;
                DROP INDEX IF EXISTS sessions_modified_idx;
                ALTER TABLE session_labels RENAME TO session_labels_v3;
                ALTER TABLE sessions RENAME TO sessions_v3;
                ALTER TABLE roots RENAME TO roots_v3;
                CREATE TABLE roots (
                    id INTEGER PRIMARY KEY,
                    format TEXT NOT NULL,
                    path TEXT NOT NULL, source TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    last_scan_at TEXT, last_scan_status TEXT, last_error TEXT,
                    UNIQUE(format, path)
                );
                CREATE TABLE sessions (
                    id INTEGER PRIMARY KEY,
                    catalog_id TEXT NOT NULL UNIQUE,
                    root_id INTEGER NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
                    relative_path TEXT NOT NULL, canonical_path TEXT NOT NULL,
                    format TEXT NOT NULL,
                    session_id TEXT, filename_session_id TEXT,
                    display_title TEXT, display_title_kind TEXT, cwd TEXT,
                    started_at TEXT, started_at_epoch REAL, cli_version TEXT,
                    history_mode TEXT, kind TEXT NOT NULL, lifecycle TEXT NOT NULL,
                    parent_session_id TEXT, status TEXT NOT NULL, reason TEXT,
                    records INTEGER, device INTEGER NOT NULL, inode INTEGER NOT NULL,
                    bytes INTEGER NOT NULL, modified_ns INTEGER NOT NULL,
                    source_fingerprint TEXT,
                    indexed_at TEXT NOT NULL, validated_at TEXT, missing_since TEXT,
                    UNIQUE(root_id, relative_path)
                );
                CREATE TABLE session_labels (
                    id INTEGER PRIMARY KEY,
                    session_row_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL, value TEXT NOT NULL,
                    ordinal INTEGER NOT NULL, priority INTEGER NOT NULL,
                    UNIQUE(session_row_id, kind, value)
                );
                INSERT INTO roots SELECT * FROM roots_v3;
                INSERT INTO sessions(
                    id, catalog_id, root_id, relative_path, canonical_path, format,
                    session_id, filename_session_id, display_title, display_title_kind,
                    cwd, started_at, started_at_epoch, cli_version, history_mode, kind,
                    lifecycle, parent_session_id, status, reason, records, device, inode,
                    bytes, modified_ns, source_fingerprint, indexed_at, validated_at,
                    missing_since
                )
                SELECT
                    id, catalog_id, root_id, relative_path, canonical_path, format,
                    session_id, filename_session_id, display_title, display_title_kind,
                    cwd, started_at, started_at_epoch, cli_version, history_mode, kind,
                    lifecycle, parent_session_id, status, reason, records, device, inode,
                    bytes, modified_ns, NULL, indexed_at, validated_at, missing_since
                FROM sessions_v3;
                INSERT INTO session_labels SELECT * FROM session_labels_v3;
                DROP TABLE session_labels_v3;
                DROP TABLE sessions_v3;
                DROP TABLE roots_v3;
                CREATE INDEX sessions_uuid_idx ON sessions(format, session_id);
                CREATE INDEX sessions_status_idx ON sessions(status);
                CREATE INDEX sessions_modified_idx ON sessions(modified_ns DESC);
                """
            )
            self._connection.execute(
                "UPDATE catalog_meta SET value = ? WHERE key = 'schema_version'", ("4",)
            )
            self._connection.execute("PRAGMA user_version = 4")
            self._connection.commit()
            self._connection.execute("PRAGMA foreign_keys = ON")
            if self._connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise sqlite3.IntegrityError("foreign key check failed")
        except (sqlite3.Error, ValueError) as exc:
            self._connection.rollback()
            self._connection.execute("PRAGMA foreign_keys = ON")
            raise SessionMigrateError(
                "catalog schema migration failed; preserve registered roots or rebuild the "
                "disposable catalog with `session-migrate catalog refresh`"
            ) from exc

    def _migrate_v2_to_v3(self) -> None:
        """Expand the native-format constraints to include Pi without losing rows."""

        try:
            self._connection.commit()
            self._connection.execute("PRAGMA foreign_keys = OFF")
            self._connection.executescript(
                """
                BEGIN IMMEDIATE;
                DROP INDEX IF EXISTS sessions_uuid_idx;
                DROP INDEX IF EXISTS sessions_status_idx;
                DROP INDEX IF EXISTS sessions_modified_idx;
                ALTER TABLE session_labels RENAME TO session_labels_v2;
                ALTER TABLE sessions RENAME TO sessions_v2;
                ALTER TABLE roots RENAME TO roots_v2;
                CREATE TABLE roots (
                    id INTEGER PRIMARY KEY,
                    format TEXT NOT NULL CHECK (format IN ('claude', 'codex', 'pi')),
                    path TEXT NOT NULL, source TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    last_scan_at TEXT, last_scan_status TEXT, last_error TEXT,
                    UNIQUE(format, path)
                );
                CREATE TABLE sessions (
                    id INTEGER PRIMARY KEY,
                    catalog_id TEXT NOT NULL UNIQUE,
                    root_id INTEGER NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
                    relative_path TEXT NOT NULL, canonical_path TEXT NOT NULL,
                    format TEXT NOT NULL CHECK (format IN ('claude', 'codex', 'pi')),
                    session_id TEXT, filename_session_id TEXT,
                    display_title TEXT, display_title_kind TEXT, cwd TEXT,
                    started_at TEXT, started_at_epoch REAL, cli_version TEXT,
                    history_mode TEXT, kind TEXT NOT NULL, lifecycle TEXT NOT NULL,
                    parent_session_id TEXT, status TEXT NOT NULL, reason TEXT,
                    records INTEGER, device INTEGER NOT NULL, inode INTEGER NOT NULL,
                    bytes INTEGER NOT NULL, modified_ns INTEGER NOT NULL,
                    indexed_at TEXT NOT NULL, validated_at TEXT, missing_since TEXT,
                    UNIQUE(root_id, relative_path)
                );
                CREATE TABLE session_labels (
                    id INTEGER PRIMARY KEY,
                    session_row_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL, value TEXT NOT NULL,
                    ordinal INTEGER NOT NULL, priority INTEGER NOT NULL,
                    UNIQUE(session_row_id, kind, value)
                );
                INSERT INTO roots SELECT * FROM roots_v2;
                INSERT INTO sessions(
                    id, catalog_id, root_id, relative_path, canonical_path, format,
                    session_id, filename_session_id, display_title, display_title_kind,
                    cwd, started_at, started_at_epoch, cli_version, history_mode, kind,
                    lifecycle, parent_session_id, status, reason, records, device, inode,
                    bytes, modified_ns, indexed_at, validated_at, missing_since
                )
                SELECT
                    id, catalog_id, root_id, relative_path, canonical_path, format,
                    session_id, filename_session_id, display_title, display_title_kind,
                    cwd, started_at, started_at_epoch, cli_version, history_mode, kind,
                    lifecycle, parent_session_id, status, reason, records, device, inode,
                    bytes, modified_ns, indexed_at, validated_at, missing_since
                FROM sessions_v2;
                INSERT INTO session_labels SELECT * FROM session_labels_v2;
                DROP TABLE session_labels_v2;
                DROP TABLE sessions_v2;
                DROP TABLE roots_v2;
                CREATE INDEX sessions_uuid_idx ON sessions(format, session_id);
                CREATE INDEX sessions_status_idx ON sessions(status);
                CREATE INDEX sessions_modified_idx ON sessions(modified_ns DESC);
                """
            )
            self._connection.execute(
                "UPDATE catalog_meta SET value = ? WHERE key = 'schema_version'", ("3",)
            )
            self._connection.execute("PRAGMA user_version = 3")
            self._connection.commit()
            self._connection.execute("PRAGMA foreign_keys = ON")
            if self._connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise sqlite3.IntegrityError("foreign key check failed")
        except (sqlite3.Error, ValueError) as exc:
            self._connection.rollback()
            self._connection.execute("PRAGMA foreign_keys = ON")
            raise SessionMigrateError(
                "catalog schema migration failed; preserve registered roots or rebuild the "
                "disposable catalog with `session-migrate catalog refresh`"
            ) from exc

    def add_root(
        self, agent_format: AgentFormat, path: Path, *, source: str = "registered"
    ) -> CatalogRoot:
        if agent_format not in {
            AgentFormat.CLAUDE,
            AgentFormat.CODEX,
            AgentFormat.PI,
            AgentFormat.OMP,
            AgentFormat.OPENCODE,
            AgentFormat.COPILOT,
            AgentFormat.ANTIGRAVITY,
            AgentFormat.CURSOR,
            AgentFormat.VIBE,
            AgentFormat.MUSE,
            AgentFormat.QWEN,
            AgentFormat.KIMI,
            AgentFormat.GROK,
            AgentFormat.KILO,
            AgentFormat.OPENHANDS,
        }:
            raise SessionMigrateError("catalog root format is unsupported")
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
        pi_roots: Sequence[Path] = (),
        omp_roots: Sequence[Path] = (),
        opencode_roots: Sequence[Path] = (),
        copilot_roots: Sequence[Path] = (),
        antigravity_roots: Sequence[Path] = (),
        cursor_roots: Sequence[Path] = (),
        vibe_roots: Sequence[Path] = (),
        muse_roots: Sequence[Path] = (),
        qwen_roots: Sequence[Path] = (),
        kimi_roots: Sequence[Path] = (),
        grok_roots: Sequence[Path] = (),
        kilo_roots: Sequence[Path] = (),
        openhands_roots: Sequence[Path] = (),
        discover_under: Sequence[Path] = (),
        include_auto: bool = True,
        validate: bool = False,
        cwd: Path | None = None,
        environ: Mapping[str, str] | None = None,
        home: Path | None = None,
    ) -> RefreshResult:
        if include_auto:
            for agent_format, path, source in auto_roots(cwd=cwd, environ=environ, home=home):
                self.add_root(agent_format, path, source=source)
        for path in claude_roots:
            self.add_root(AgentFormat.CLAUDE, path)
        for path in codex_roots:
            self.add_root(AgentFormat.CODEX, path)
        for path in pi_roots:
            self.add_root(AgentFormat.PI, path)
        for path in omp_roots:
            self.add_root(AgentFormat.OMP, path)
        for path in opencode_roots:
            self.add_root(AgentFormat.OPENCODE, path)
        for path in copilot_roots:
            self.add_root(AgentFormat.COPILOT, path)
        for path in antigravity_roots:
            self.add_root(AgentFormat.ANTIGRAVITY, path)
        for path in cursor_roots:
            self.add_root(AgentFormat.CURSOR, path)
        for path in vibe_roots:
            self.add_root(AgentFormat.VIBE, path)
        for path in muse_roots:
            self.add_root(AgentFormat.MUSE, path)
        for path in qwen_roots:
            self.add_root(AgentFormat.QWEN, path)
        for path in kimi_roots:
            self.add_root(AgentFormat.KIMI, path)
        for path in grok_roots:
            self.add_root(AgentFormat.GROK, path)
        for path in kilo_roots:
            self.add_root(AgentFormat.KILO, path)
        for path in openhands_roots:
            self.add_root(AgentFormat.OPENHANDS, path)
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
        if root.format in {AgentFormat.OPENCODE.value, AgentFormat.KILO.value}:
            return self._refresh_opencode_root(root)
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

        if root.format == AgentFormat.CODEX.value:
            metadata = _codex_native_metadata(root_path)
        elif root.format == AgentFormat.COPILOT.value:
            metadata = _copilot_native_metadata(root_path)
        elif root.format == AgentFormat.ANTIGRAVITY.value:
            metadata = _antigravity_native_metadata(root_path)
        else:
            metadata = _NativeMetadata({}, {}, {}, False)
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
                if root.format == AgentFormat.COPILOT.value and (
                    path.is_symlink() or not path.is_file()
                ):
                    before = _copilot_unavailable_snapshot(path)
                elif root.format == AgentFormat.CURSOR.value and (
                    path.is_symlink() or not path.is_file()
                ):
                    before = _cursor_unavailable_snapshot(path)
                elif root.format == AgentFormat.VIBE.value:
                    try:
                        before = _vibe_session_snapshot(path)
                    except JsonlError:
                        continue
                elif root.format == AgentFormat.KIMI.value:
                    try:
                        before = _kimi_session_snapshot(path)
                    except JsonlError:
                        continue
                elif root.format in {
                    AgentFormat.GROK.value,
                    AgentFormat.OPENHANDS.value,
                }:
                    try:
                        before = _directory_session_snapshot(path, root.format)
                    except JsonlError:
                        continue
                elif root.format in {
                    AgentFormat.ANTIGRAVITY.value,
                    AgentFormat.CURSOR.value,
                }:
                    try:
                        before = _sqlite_session_snapshot(path, root.format)
                    except JsonlError:
                        continue
                else:
                    try:
                        before = file_snapshot(path)
                    except JsonlError:
                        if root.format != AgentFormat.COPILOT.value:
                            # A candidate can vanish between enumeration and stat.  It
                            # is retried on the next refresh and not invented here.
                            continue
                        before = _copilot_unavailable_snapshot(path)
                previous = existing.get(relative)
                unchanged = bool(
                    previous
                    and previous["status"] != "missing"
                    and int(previous["device"]) == before.device
                    and int(previous["inode"]) == before.inode
                    and int(previous["bytes"]) == before.size
                    and int(previous["modified_ns"]) == before.modified_ns
                    and previous["source_fingerprint"] == getattr(before, "fingerprint", None)
                    and (not validate or previous["status"] == "validated")
                )
                if unchanged:
                    counts["unchanged"] += 1
                    row_id = int(previous["id"])
                    self._replace_native_labels(row_id, previous, metadata)
                    continue

                if root.format == AgentFormat.COPILOT.value and (
                    not path.is_file() or path.is_symlink()
                ):
                    scan = _copilot_unavailable_scan(path, root_path)
                    if scan.status == "missing" and (
                        previous is None or previous["status"] != "missing"
                    ):
                        counts["missing"] += 1
                elif root.format == AgentFormat.CURSOR.value and (
                    not path.is_file() or path.is_symlink()
                ):
                    scan = _cursor_unavailable_scan(path, root_path)
                    if scan.status == "missing" and (
                        previous is None or previous["status"] != "missing"
                    ):
                        counts["missing"] += 1
                else:
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

    def _refresh_opencode_root(self, root: CatalogRoot) -> dict[str, int]:
        """Index OpenCode's authoritative session table without exporting content."""

        counts = {
            "files_seen": 0,
            "scanned": 0,
            "unchanged": 0,
            "missing": 0,
            "root_errors": 0,
        }
        root_path = Path(root.path)
        if not root_path.is_dir():
            self._record_root_failure(root.id, "root_unavailable")
            counts["root_errors"] = 1
            return counts
        existing = {
            str(row["relative_path"]): row
            for row in self._connection.execute(
                "SELECT * FROM sessions WHERE root_id = ?", (root.id,)
            ).fetchall()
        }
        seen: set[str] = set()
        now = _utc_now()
        try:
            database_name = (
                "opencode.db" if root.format == AgentFormat.OPENCODE.value else "kilo.db"
            )
            with _opencode_inventory(root_path, database_name=database_name) as (
                database_snapshot,
                rows,
            ):
                self._connection.execute("BEGIN")
                for native_row in rows:
                    scan, snapshot = _scan_opencode_row(
                        native_row,
                        database_snapshot,
                        AgentFormat(root.format),
                    )
                    native_id = _string(native_row["id"])
                    relative_key = (
                        native_id
                        if native_id and _OPENCODE_SESSION_ID.fullmatch(native_id)
                        else _anonymous_row_key(native_row)
                    )
                    relative = f"session/{relative_key}"
                    seen.add(relative)
                    counts["files_seen"] += 1
                    previous = existing.get(relative)
                    unchanged = bool(
                        previous
                        and previous["status"] != "missing"
                        and previous["source_fingerprint"] == snapshot.fingerprint
                    )
                    if unchanged:
                        counts["unchanged"] += 1
                        continue
                    counts["scanned"] += 1
                    row_id = self._upsert_virtual_session(
                        root,
                        relative,
                        snapshot,
                        scan,
                        now,
                        previous,
                    )
                    self._replace_labels(row_id, scan.labels)

                for relative in set(existing) - seen:
                    previous = existing[relative]
                    if previous["status"] != "missing":
                        counts["missing"] += 1
                        self._connection.execute(
                            """
                            UPDATE sessions SET status = 'missing', reason = 'session_missing',
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
        except _OpenCodeInventoryError as exc:
            self._connection.rollback()
            self._record_root_failure(root.id, exc.code)
            counts["root_errors"] = 1
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
                records, device, inode, bytes, modified_ns, source_fingerprint, indexed_at,
                validated_at, missing_since
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL
            )
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
                source_fingerprint = excluded.source_fingerprint,
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
                getattr(snapshot, "fingerprint", None),
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

    def _upsert_virtual_session(
        self,
        root: CatalogRoot,
        relative: str,
        snapshot: _VirtualSnapshot,
        scan: _Scan,
        now: str,
        previous: sqlite3.Row | None,
    ) -> int:
        """Persist an ID-addressed native source without inventing a file path."""

        catalog_id = str(previous["catalog_id"]) if previous else uuid.uuid4().hex[:16]
        canonical_source = f"{root.format}:{scan.session_id or relative.removeprefix('session/')}"
        self._connection.execute(
            """
            INSERT INTO sessions(
                catalog_id, root_id, relative_path, canonical_path, format,
                session_id, filename_session_id, cwd, started_at, started_at_epoch,
                cli_version, history_mode, kind, lifecycle, parent_session_id,
                status, reason, records, device, inode, bytes, modified_ns,
                source_fingerprint, indexed_at, validated_at, missing_since
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL,
                      ?, ?, ?, ?, ?, ?, NULL, NULL)
            ON CONFLICT(root_id, relative_path) DO UPDATE SET
                canonical_path = excluded.canonical_path,
                format = excluded.format,
                session_id = excluded.session_id,
                filename_session_id = NULL,
                cwd = excluded.cwd,
                started_at = excluded.started_at,
                started_at_epoch = excluded.started_at_epoch,
                cli_version = excluded.cli_version,
                history_mode = NULL,
                kind = excluded.kind,
                lifecycle = excluded.lifecycle,
                parent_session_id = excluded.parent_session_id,
                status = excluded.status,
                reason = excluded.reason,
                records = NULL,
                device = excluded.device,
                inode = excluded.inode,
                bytes = excluded.bytes,
                modified_ns = excluded.modified_ns,
                source_fingerprint = excluded.source_fingerprint,
                indexed_at = excluded.indexed_at,
                validated_at = NULL,
                missing_since = NULL
            """,
            (
                catalog_id,
                root.id,
                relative,
                canonical_source,
                root.format,
                scan.session_id,
                scan.cwd,
                scan.started_at,
                _timestamp_epoch(scan.started_at),
                scan.cli_version,
                scan.kind,
                scan.lifecycle,
                scan.parent_session_id,
                scan.status,
                scan.reason,
                snapshot.device,
                snapshot.inode,
                snapshot.size,
                snapshot.modified_ns,
                snapshot.fingerprint,
                now,
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
        if str(row["format"]) in {
            AgentFormat.CODEX.value,
            AgentFormat.ANTIGRAVITY.value,
        }:
            parent = (
                metadata.parent_by_id.get(str(row["session_id"])) if row["session_id"] else None
            )
            current_kind = str(row["kind"])
            desired_kind = (
                "subagent" if parent else ("main" if current_kind == "subagent" else current_kind)
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
            raise SessionMigrateError("catalog limit must be between 1 and 10000")
        if offset < 0:
            raise SessionMigrateError("catalog offset cannot be negative")
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
                raise SessionMigrateError("catalog search query cannot be empty")
            for term in normalized.split():
                search = [
                    "instr(lower(COALESCE(s.session_id, '')), ?) > 0",
                    "instr(lower(COALESCE(s.filename_session_id, '')), ?) > 0",
                    "EXISTS (SELECT 1 FROM session_labels l "
                    "WHERE l.session_row_id = s.id "
                    "AND instr(session_casefold(l.value), ?) > 0)",
                ]
                parameters.extend((term, term, term))
                if include_paths:
                    search.extend(
                        (
                            "instr(lower(COALESCE(s.cwd, '')), ?) > 0",
                            "instr(lower(s.canonical_path), ?) > 0",
                        )
                    )
                    parameters.extend((term, term))
                where.append(f"({' OR '.join(search)})")
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        rows = self._connection.execute(
            f"""
            WITH duplicate_counts AS (
                SELECT format,
                    COALESCE(session_id, filename_session_id) AS native_id,
                    count(*) AS duplicate_count
                FROM sessions
                WHERE status != 'missing'
                  AND COALESCE(session_id, filename_session_id) IS NOT NULL
                GROUP BY format, COALESCE(session_id, filename_session_id)
            )
            SELECT s.*, r.path AS root_path,
                COALESCE(duplicates.duplicate_count, 0) AS duplicate_count
            FROM sessions s JOIN roots r ON r.id = s.root_id
            LEFT JOIN duplicate_counts duplicates
              ON duplicates.format = s.format
             AND duplicates.native_id = COALESCE(s.session_id, s.filename_session_id)
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
            WITH duplicate_counts AS (
                SELECT format,
                    COALESCE(session_id, filename_session_id) AS native_id,
                    count(*) AS duplicate_count
                FROM sessions
                WHERE status != 'missing'
                  AND COALESCE(session_id, filename_session_id) IS NOT NULL
                GROUP BY format, COALESCE(session_id, filename_session_id)
            )
            SELECT s.*, r.path AS root_path,
                COALESCE(duplicates.duplicate_count, 0) AS duplicate_count
            FROM sessions s JOIN roots r ON r.id = s.root_id
            LEFT JOIN duplicate_counts duplicates
              ON duplicates.format = s.format
             AND duplicates.native_id = COALESCE(s.session_id, s.filename_session_id)
            WHERE s.catalog_id = ?
            """,
            (catalog_id,),
        ).fetchone()
        if row is None:
            raise SessionMigrateError("catalog session ID was not found")
        return _entry_from_row(row, include_paths=include_paths)

    def session_source_for_transfer(self, catalog_id: str) -> CatalogTransferSource:
        entry = self.get_session(catalog_id, include_paths=True)
        if entry.status == "missing":
            raise SessionMigrateError("catalog session source is missing; refresh the catalog")
        if entry.status in {"unsupported", "corrupt", "oversized", "busy", "unreadable"}:
            raise SessionMigrateError(
                f"catalog session is not transferable: {entry.status}"
                + (f" ({entry.reason})" if entry.reason else "")
            )
        assert entry.root is not None
        agent_format = AgentFormat(entry.format)
        path = (
            None
            if agent_format in {AgentFormat.OPENCODE, AgentFormat.KILO}
            else Path(entry.path or "")
        )
        if agent_format not in {AgentFormat.OPENCODE, AgentFormat.KILO} and not entry.path:
            raise SessionMigrateError("catalog session has no physical source path")
        return CatalogTransferSource(
            format=agent_format,
            session_id=entry.session_id,
            root=Path(entry.root),
            path=path,
        )

    def session_path_for_transfer(self, catalog_id: str) -> tuple[AgentFormat, Path]:
        """Return the legacy physical-source tuple.

        Callers that support OpenCode must use :meth:`session_source_for_transfer`
        because the database is an inventory, not an export transcript.
        """

        source = self.session_source_for_transfer(catalog_id)
        if source.path is None:
            raise SessionMigrateError(
                "cataloged OpenCode/Kilo sources are native IDs, not transcript files"
            )
        return source.format, source.path


def _candidate_files(agent_format: AgentFormat, root: Path) -> Iterable[Path]:
    if not root.is_dir():
        raise OSError("root is not available")
    if agent_format == AgentFormat.COPILOT:
        session_state = root / "session-state"
        if not session_state.is_dir():
            return
        with os.scandir(session_state) as entries:
            candidates = [
                Path(entry.path) / "events.jsonl"
                for entry in entries
                if entry.is_dir(follow_symlinks=False)
            ]
        yield from sorted(candidates)
        return
    if agent_format == AgentFormat.ANTIGRAVITY:
        conversations = root / "conversations"
        if not conversations.is_dir():
            return
        with os.scandir(conversations) as entries:
            candidates = [
                Path(entry.path)
                for entry in entries
                if entry.is_file(follow_symlinks=False) and entry.name.endswith(".db")
            ]
        yield from sorted(candidates)
        return
    if agent_format == AgentFormat.CURSOR:
        chats = root / "chats"
        if not chats.is_dir():
            return
        candidates: list[Path] = []
        with os.scandir(chats) as workspaces:
            workspace_paths = [
                Path(entry.path) for entry in workspaces if entry.is_dir(follow_symlinks=False)
            ]
        for workspace in sorted(workspace_paths):
            with os.scandir(workspace) as sessions:
                candidates.extend(
                    Path(entry.path) / "store.db"
                    for entry in sessions
                    if entry.is_dir(follow_symlinks=False)
                )
        yield from sorted(candidates)
        return
    if agent_format == AgentFormat.VIBE:
        sessions = root / "logs/session"
        if not sessions.is_dir():
            return
        with os.scandir(sessions) as entries:
            candidates = [
                Path(entry.path) / vibe.MESSAGES_FILENAME
                for entry in entries
                if entry.is_dir(follow_symlinks=False) and entry.name.startswith("session_")
            ]
        yield from sorted(candidates)
        return
    if agent_format == AgentFormat.QWEN:
        projects = root / "projects"
        if not projects.is_dir():
            return
        yield from sorted(
            path
            for path in projects.glob("*/chats/*.jsonl")
            if path.is_file() and not path.is_symlink()
        )
        return
    if agent_format == AgentFormat.KIMI:
        sessions = root / "sessions"
        if not sessions.is_dir():
            return
        yield from sorted(
            path
            for path in sessions.glob(f"*/session_*/agents/main/{kimi.WIRE_FILENAME}")
            if path.is_file() and not path.is_symlink()
        )
        return
    if agent_format == AgentFormat.MUSE:
        yield from sorted(
            path
            for path in (root / "sessions").glob("*/*/*/*/session.jsonl")
            if path.is_file() and not path.is_symlink()
        )
        return
    if agent_format == AgentFormat.GROK:
        yield from sorted(
            path.parent
            for path in (root / "sessions").glob("*/*/summary.json")
            if path.is_file()
            and not path.is_symlink()
            and (path.parent / "updates.jsonl").is_file()
        )
        return
    if agent_format == AgentFormat.OPENHANDS:
        yield from sorted(
            path for path in root.glob("*/events") if path.is_dir() and not path.is_symlink()
        )
        return
    if agent_format == AgentFormat.CLAUDE:
        directories = [root / "projects"]
    elif agent_format == AgentFormat.CODEX:
        directories = [root / "sessions", root / "archived_sessions"]
    else:
        directories = [root / "sessions"]
    candidates: list[Path] = []
    for directory in directories:
        if not directory.is_dir():
            continue
        for current, subdirectories, filenames in os.walk(
            directory,
            followlinks=False,
            onerror=_raise_walk_error,
        ):
            subdirectories[:] = [
                name for name in subdirectories if not (Path(current) / name).is_symlink()
            ]
            for filename in filenames:
                path = Path(current) / filename
                if path.suffix == ".jsonl" and not path.is_symlink():
                    candidates.append(path)
    yield from sorted(candidates)


def _raise_walk_error(error: OSError) -> None:
    """Make an incomplete filesystem walk fail instead of looking exhaustive."""

    raise error


def _scan_file(path: Path, agent_format: AgentFormat, root: Path) -> _Scan:
    if agent_format == AgentFormat.ANTIGRAVITY:
        return _scan_antigravity_file(path, root)
    if agent_format == AgentFormat.CURSOR:
        return _scan_cursor_file(path, root)
    if agent_format == AgentFormat.VIBE:
        return _scan_vibe_file(path, root)
    if agent_format in {
        AgentFormat.MUSE,
        AgentFormat.QWEN,
        AgentFormat.KIMI,
        AgentFormat.GROK,
        AgentFormat.OPENHANDS,
    }:
        return _scan_new_portable_file(path, agent_format, root)
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
    session_record_index: int | None = None
    pi_version: int | None = None
    first_record_type: str | None = None
    parent_session_id: str | None = None
    wrong_format = False
    try:
        for record in iter_jsonl(path):
            records += 1
            value = record.value
            record_type = _string(value.get("type"))
            if records == 1:
                first_record_type = record_type
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
            elif agent_format == AgentFormat.CODEX:
                if record_type in {"user", "assistant"} and isinstance(value.get("message"), dict):
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
                    started_at = (
                        started_at
                        or _string(payload.get("timestamp"))
                        or _string(value.get("timestamp"))
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
                        _string(payload.get("name")) or _string(payload.get("thread_name")),
                        LABEL_LIMIT,
                    )
                    if title:
                        labels.append(_Label("thread_name", title, record.index, 110))
            elif agent_format in {AgentFormat.PI, AgentFormat.OMP}:
                if record_type in {"user", "assistant", "session_meta", "response_item"}:
                    wrong_format = True
                if agent_format == AgentFormat.PI and records == 1 and record_type == "title":
                    wrong_format = True
                if agent_format == AgentFormat.OMP and records == 1:
                    if record_type != "title" or value.get("v") != 1:
                        wrong_format = True
                    title = _bounded(_string(value.get("title")), LABEL_LIMIT)
                    if title:
                        labels.append(_Label("session_title", title, record.index, 120))
                if record_type == "session":
                    has_session_meta = True
                    session_record_index = records
                    session_id = session_id or _normalized_uuid(_string(value.get("id")))
                    cwd = cwd or _bounded(_string(value.get("cwd")), PATH_VALUE_LIMIT)
                    started_at = started_at or _string(value.get("timestamp"))
                    version = value.get("version")
                    pi_version = version if isinstance(version, int) else None
                    parent = _string(value.get("parentSession"))
                    if parent:
                        parent_session_id = _filename_uuid(Path(parent))
                    if agent_format == AgentFormat.OMP:
                        title = _bounded(_string(value.get("title")), LABEL_LIMIT)
                        if title:
                            labels.append(_Label("session_title", title, record.index, 90))
                elif record_type == "message":
                    message = value.get("message")
                    has_conversation = has_conversation or (
                        isinstance(message, dict)
                        and message.get("role") in {"user", "assistant", "toolResult"}
                    )
                elif record_type in {"compaction", "branch_summary", "custom_message"}:
                    has_conversation = True
                elif record_type == "session_info":
                    title = _bounded(_string(value.get("name")), LABEL_LIMIT)
                    if title:
                        labels.append(_Label("session_name", title, record.index, 110))
                elif record_type == "title_change" and agent_format == AgentFormat.OMP:
                    title = _bounded(_string(value.get("title")), LABEL_LIMIT)
                    if title:
                        labels.append(_Label("session_title", title, record.index, 110))
            else:
                if record_type in {
                    "user",
                    "assistant",
                    "session_meta",
                    "response_item",
                    "session",
                    "message",
                }:
                    wrong_format = True
                data = value.get("data")
                if not isinstance(data, dict):
                    data = {}
                if record_type == "session.start":
                    has_session_meta = True
                    session_id = session_id or _normalized_uuid(_string(data.get("sessionId")))
                    started_at = started_at or _string(data.get("startTime"))
                    cli_version = cli_version or _string(data.get("copilotVersion"))
                    version = data.get("version")
                    pi_version = version if isinstance(version, int) else None
                    context = data.get("context")
                    if isinstance(context, dict):
                        cwd = cwd or _bounded(_string(context.get("cwd")), PATH_VALUE_LIMIT)
                elif record_type == "session.title_changed":
                    title = _bounded(_string(data.get("title")), LABEL_LIMIT)
                    if title:
                        labels.append(_Label("session_title", title, record.index, 110))
                if record_type in {
                    "user.message",
                    "assistant.message",
                    "tool.execution_start",
                    "tool.execution_complete",
                    "session.compaction_start",
                    "session.compaction_complete",
                }:
                    has_conversation = True
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
    elif agent_format == AgentFormat.CODEX:
        if not has_session_meta:
            status, reason = "corrupt", "missing_session_meta"
        elif history_mode and history_mode != "legacy":
            status, reason = "unsupported", "codex_history_mode"
        elif history_base:
            status, reason = "unsupported", "codex_history_base"
        elif not has_conversation:
            status, reason = "corrupt", "no_conversation_records"
    elif agent_format == AgentFormat.PI:
        if first_record_type != "session" or session_record_index != 1 or not has_session_meta:
            status, reason = "corrupt", "missing_pi_session_header"
        elif pi_version != 3:
            status, reason = "unsupported", "pi_session_version"
        elif not session_id:
            status, reason = "corrupt", "missing_session_id"
        elif not has_conversation:
            status, reason = "corrupt", "no_conversation_records"
    elif agent_format == AgentFormat.OMP:
        if first_record_type != "title" or session_record_index != 2 or not has_session_meta:
            status, reason = "corrupt", "missing_omp_title_or_session_header"
        elif pi_version != omp.OMP_SESSION_VERSION:
            status, reason = "unsupported", "omp_session_version"
        elif not session_id:
            status, reason = "corrupt", "missing_session_id"
        elif not has_conversation:
            status, reason = "corrupt", "no_conversation_records"
    else:
        if first_record_type != "session.start" or not has_session_meta:
            status, reason = "corrupt", "missing_copilot_session_start"
        elif pi_version != 1:
            status, reason = "unsupported", "copilot_event_version"
        elif not session_id:
            status, reason = "corrupt", "missing_session_id"
        elif base.filename_session_id and session_id != base.filename_session_id:
            status, reason = "corrupt", "session_directory_mismatch"
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
        parent_session_id=parent_session_id or base.parent_session_id,
        status=status,
        reason=reason,
        records=records,
        labels=tuple(labels),
    )


def _validated_scan(path: Path, agent_format: AgentFormat, scan: _Scan) -> _Scan:
    try:
        session = load_session(path, agent_format)
        target = AgentFormat.CODEX if agent_format == AgentFormat.CLAUDE else AgentFormat.CLAUDE
        convert_session(session, ConversionOptions(target_format=target))
    except (SessionMigrateError, JsonlError) as exc:
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


def _native_key_labels(path: Path, agent_format: AgentFormat, root: Path) -> tuple[_Label, ...]:
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
    if agent_format in {AgentFormat.COPILOT, AgentFormat.CURSOR, AgentFormat.MUSE}:
        filename_id = _normalized_uuid(path.parent.name)
    elif agent_format == AgentFormat.KIMI:
        filename_id = _normalized_uuid(path.parent.parent.parent.name.removeprefix("session_"))
    elif agent_format == AgentFormat.GROK:
        filename_id = _normalized_uuid(path.name)
    elif agent_format == AgentFormat.OPENHANDS:
        filename_id = _normalized_uuid(path.parent.name)
    else:
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
    elif agent_format == AgentFormat.CODEX:
        kind = "main"
        lifecycle = "archived" if relative.parts[0] == "archived_sessions" else "active"
    elif agent_format in {AgentFormat.PI, AgentFormat.OMP}:
        kind = "main"
        lifecycle = "project"
    elif agent_format in {
        AgentFormat.ANTIGRAVITY,
        AgentFormat.CURSOR,
        AgentFormat.VIBE,
        AgentFormat.MUSE,
        AgentFormat.QWEN,
        AgentFormat.KIMI,
        AgentFormat.GROK,
        AgentFormat.OPENHANDS,
    }:
        kind = "main"
        lifecycle = "active"
    else:
        kind = "main"
        lifecycle = "active"
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


def _scan_antigravity_file(path: Path, root: Path) -> _Scan:
    base = _base_scan(path, AgentFormat.ANTIGRAVITY, root, "candidate", None)
    try:
        parsed = antigravity.parse(path)
    except SessionMigrateError:
        return _replace_scan_status(base, "corrupt", "invalid_antigravity_database")
    labels: list[_Label] = []
    if parsed.title:
        title = _bounded(parsed.title, LABEL_LIMIT)
        if title:
            labels.append(_Label("native_title", title, 0, 110))
    has_conversation = any(
        event.kind.value in {"message", "tool_call", "tool_result"} for event in parsed.events
    )
    status = "candidate" if has_conversation else "corrupt"
    reason = None if has_conversation else "no_conversation_records"
    return _Scan(
        session_id=parsed.session_id,
        filename_session_id=base.filename_session_id,
        cwd=_bounded(str(parsed.cwd), PATH_VALUE_LIMIT) if parsed.cwd else None,
        started_at=parsed.started_at,
        cli_version=parsed.cli_version,
        history_mode=None,
        kind=base.kind,
        lifecycle=base.lifecycle,
        parent_session_id=None,
        status=status,
        reason=reason,
        records=parsed.raw_record_count,
        labels=tuple(labels),
    )


def _scan_cursor_file(path: Path, root: Path) -> _Scan:
    base = _base_scan(path, AgentFormat.CURSOR, root, "candidate", None)
    try:
        parsed = cursor_format.parse(path)
    except SessionMigrateError:
        return _replace_scan_status(base, "corrupt", "invalid_cursor_database")
    labels: list[_Label] = []
    title = _bounded(parsed.title, LABEL_LIMIT)
    if title:
        labels.append(_Label("native_title", title, 0, 110))
    has_conversation = any(event.kind.value == "message" for event in parsed.events)
    status = "candidate" if has_conversation else "corrupt"
    reason = None if has_conversation else "no_conversation_records"
    return _Scan(
        session_id=parsed.session_id,
        filename_session_id=base.filename_session_id,
        cwd=None,
        started_at=parsed.started_at,
        cli_version=parsed.cli_version,
        history_mode=None,
        kind=base.kind,
        lifecycle=base.lifecycle,
        parent_session_id=None,
        status=status,
        reason=reason,
        records=parsed.raw_record_count,
        labels=tuple(labels),
    )


def _scan_vibe_file(path: Path, root: Path) -> _Scan:
    base = _base_scan(path, AgentFormat.VIBE, root, "candidate", None)
    try:
        parsed = vibe.parse_session(path)
    except (SessionMigrateError, JsonlError):
        return _replace_scan_status(base, "corrupt", "invalid_vibe_session")
    labels: list[_Label] = []
    title = _bounded(parsed.title, LABEL_LIMIT)
    if title:
        labels.append(_Label("native_title", title, 0, 110))
    has_conversation = any(
        event.kind in {EventKind.MESSAGE, EventKind.TOOL_CALL, EventKind.TOOL_RESULT}
        and event.role in {Role.USER, Role.ASSISTANT, Role.TOOL}
        for event in parsed.events
    )
    status = "candidate" if has_conversation else "corrupt"
    reason = None if has_conversation else "no_conversation_records"
    return _Scan(
        session_id=parsed.session_id,
        filename_session_id=None,
        cwd=_bounded(str(parsed.cwd), PATH_VALUE_LIMIT) if parsed.cwd else None,
        started_at=parsed.started_at,
        cli_version=parsed.cli_version,
        history_mode=None,
        kind=base.kind,
        lifecycle=base.lifecycle,
        parent_session_id=None,
        status=status,
        reason=reason,
        records=parsed.raw_record_count,
        labels=tuple(labels),
    )


def _scan_new_portable_file(path: Path, agent_format: AgentFormat, root: Path) -> _Scan:
    base = _base_scan(path, agent_format, root, "candidate", None)
    try:
        parsed = load_session(path, agent_format)
    except (SessionMigrateError, JsonlError):
        return _replace_scan_status(base, "corrupt", f"invalid_{agent_format.value}_session")
    labels: list[_Label] = []
    title = _bounded(parsed.title, LABEL_LIMIT)
    if title:
        labels.append(_Label("native_title", title, 0, 110))
    has_conversation = any(
        event.kind in {EventKind.MESSAGE, EventKind.TOOL_CALL, EventKind.TOOL_RESULT}
        and event.role in {Role.USER, Role.ASSISTANT, Role.TOOL}
        for event in parsed.events
    )
    return _Scan(
        session_id=parsed.session_id,
        filename_session_id=base.filename_session_id,
        cwd=_bounded(str(parsed.cwd), PATH_VALUE_LIMIT) if parsed.cwd else None,
        started_at=parsed.started_at,
        cli_version=parsed.cli_version,
        history_mode=None,
        kind=base.kind,
        lifecycle=base.lifecycle,
        parent_session_id=None,
        status="candidate" if has_conversation else "corrupt",
        reason=None if has_conversation else "no_conversation_records",
        records=parsed.raw_record_count,
        labels=tuple(labels),
    )


def _sqlite_session_snapshot(path: Path, format_name: str) -> _VirtualSnapshot:
    """Track a native SQLite file plus live WAL/SHM state incrementally."""

    try:
        main = path.lstat()
    except OSError as exc:
        raise JsonlError(f"{format_name} database is unavailable") from exc
    if path.is_symlink() or not path.is_file():
        raise JsonlError(f"{format_name} database is not a regular file")
    components: list[str] = []
    total_size = 0
    newest = main.st_mtime_ns
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            components.append(f"{candidate.name}:missing")
            continue
        except OSError as exc:
            raise JsonlError(f"{format_name} database state is unavailable") from exc
        if candidate.is_symlink() or not candidate.is_file():
            raise JsonlError(f"{format_name} database state is not a regular file")
        total_size += info.st_size
        newest = max(newest, info.st_mtime_ns)
        components.append(
            f"{candidate.name}:{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}"
        )
    fingerprint = sha256("\0".join(components).encode()).hexdigest()
    return _VirtualSnapshot(main.st_dev, main.st_ino, total_size, newest, fingerprint)


def _directory_session_snapshot(path: Path, format_name: str) -> _VirtualSnapshot:
    """Track all authoritative files of a directory-backed native session."""

    if format_name == AgentFormat.OPENHANDS.value:
        snapshot = openhands.session_snapshot(path)
        return _VirtualSnapshot(
            snapshot.device,
            snapshot.inode,
            snapshot.size,
            snapshot.modified_ns,
            snapshot.fingerprint,
        )

    try:
        directory = path.lstat()
    except OSError as exc:
        raise JsonlError(f"{format_name} session directory is unavailable") from exc
    if path.is_symlink() or not path.is_dir():
        raise JsonlError(f"{format_name} session directory is invalid")
    if format_name == AgentFormat.GROK.value:
        candidates = (path / "summary.json", path / "updates.jsonl")
    else:
        candidates = tuple(sorted(path.glob("event-*.json")))
    if not candidates:
        raise JsonlError(f"{format_name} session has no native records")
    components: list[str] = []
    total_size = 0
    newest = directory.st_mtime_ns
    for candidate in candidates:
        try:
            info = candidate.lstat()
        except OSError as exc:
            raise JsonlError(f"{format_name} session state is unavailable") from exc
        if candidate.is_symlink() or not candidate.is_file():
            raise JsonlError(f"{format_name} session state is not a regular file")
        total_size += info.st_size
        if total_size > DEFAULT_MAX_TOTAL_BYTES:
            raise JsonlError(f"{format_name} session exceeds the input safety limit")
        newest = max(newest, info.st_mtime_ns)
        components.append(
            f"{candidate.name}:{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}"
        )
    fingerprint = sha256("\0".join(components).encode()).hexdigest()
    return _VirtualSnapshot(
        directory.st_dev,
        directory.st_ino,
        total_size,
        newest,
        fingerprint,
    )


def _vibe_session_snapshot(path: Path) -> _VirtualSnapshot:
    """Track Vibe's messages and metadata files as one incremental source."""

    if path.is_symlink() or not path.is_file():
        raise JsonlError("Vibe messages file is not a regular file")
    meta_path = path.parent / vibe.META_FILENAME
    if meta_path.is_symlink() or not meta_path.is_file():
        raise JsonlError("Vibe metadata file is not a regular file")
    components: list[str] = []
    total_size = 0
    newest = 0
    primary = path.stat()
    for candidate in (path, meta_path):
        try:
            info = candidate.stat()
        except OSError as exc:
            raise JsonlError("Vibe session state is unavailable") from exc
        total_size += info.st_size
        newest = max(newest, info.st_mtime_ns)
        components.append(
            f"{candidate.name}:{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}"
        )
    fingerprint = sha256("\0".join(components).encode()).hexdigest()
    return _VirtualSnapshot(primary.st_dev, primary.st_ino, total_size, newest, fingerprint)


def _kimi_session_snapshot(path: Path) -> _VirtualSnapshot:
    """Track Kimi's main wire journal and state document together."""

    if path.is_symlink() or not path.is_file():
        raise JsonlError("Kimi wire journal is not a regular file")
    state_path = path.parent.parent.parent / kimi.STATE_FILENAME
    if state_path.is_symlink() or not state_path.is_file():
        raise JsonlError("Kimi state document is not a regular file")
    components: list[str] = []
    total_size = 0
    newest = 0
    primary = path.stat()
    for candidate in (path, state_path):
        try:
            info = candidate.stat()
        except OSError as exc:
            raise JsonlError("Kimi session state is unavailable") from exc
        total_size += info.st_size
        newest = max(newest, info.st_mtime_ns)
        components.append(
            f"{candidate.name}:{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}"
        )
    fingerprint = sha256("\0".join(components).encode()).hexdigest()
    return _VirtualSnapshot(primary.st_dev, primary.st_ino, total_size, newest, fingerprint)


def _antigravity_snapshot(path: Path) -> _VirtualSnapshot:
    """Compatibility wrapper for existing callers and tests."""

    return _sqlite_session_snapshot(path, AgentFormat.ANTIGRAVITY.value)


def _antigravity_native_metadata(root: Path) -> _NativeMetadata:
    """Read only picker titles/lineage; never inspect message or tool bodies."""

    database = root / "conversation_summaries.db"
    if database.is_symlink() or not database.is_file():
        return _NativeMetadata({}, {}, {}, False)
    by_id: dict[str, tuple[_Label, ...]] = {}
    parent_by_id: dict[str, str] = {}
    try:
        uri = f"{database.absolute().as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=1) as connection:
            connection.execute("PRAGMA query_only = ON")
            rows = connection.execute(
                "SELECT conversation_id,title,parent_conversation_id FROM conversation_summaries"
            ).fetchall()
        for session_id, title_value, parent_value in rows:
            normalized_id = _normalized_uuid(_string(session_id))
            if not normalized_id:
                continue
            title = _bounded(_string(title_value), LABEL_LIMIT)
            if title:
                by_id[normalized_id] = (_Label("native_title", title, 0, 120),)
            parent = _normalized_uuid(_string(parent_value))
            if parent:
                parent_by_id[normalized_id] = parent
    except sqlite3.Error:
        return _NativeMetadata({}, {}, {}, False)
    return _NativeMetadata({}, by_id, parent_by_id, True)


def _copilot_unavailable_snapshot(path: Path) -> _VirtualSnapshot:
    """Represent a declared Copilot session whose event log cannot be opened."""

    try:
        stat_result = path.lstat() if os.path.lexists(path) else path.parent.lstat()
        device = stat_result.st_dev
        inode = stat_result.st_ino
        modified_ns = stat_result.st_mtime_ns
    except OSError:
        device = inode = modified_ns = 0
    state = "symlink" if path.is_symlink() else "missing"
    fingerprint = sha256(f"{state}\0{device}\0{inode}\0{modified_ns}".encode()).hexdigest()
    return _VirtualSnapshot(device, inode, 0, modified_ns, fingerprint)


def _copilot_unavailable_scan(path: Path, root: Path) -> _Scan:
    if path.is_symlink():
        return _base_scan(path, AgentFormat.COPILOT, root, "unreadable", "symlink_not_allowed")
    if os.path.lexists(path):
        return _base_scan(
            path,
            AgentFormat.COPILOT,
            root,
            "unreadable",
            "events_file_not_regular",
        )
    return _base_scan(path, AgentFormat.COPILOT, root, "missing", "events_file_missing")


def _cursor_unavailable_snapshot(path: Path) -> _VirtualSnapshot:
    """Represent a declared Cursor chat whose SQLite store cannot be opened."""

    try:
        stat_result = path.lstat() if os.path.lexists(path) else path.parent.lstat()
        device = stat_result.st_dev
        inode = stat_result.st_ino
        modified_ns = stat_result.st_mtime_ns
    except OSError:
        device = inode = modified_ns = 0
    if path.is_symlink():
        state = "symlink"
    elif os.path.lexists(path):
        state = "not_regular"
    else:
        state = "missing"
    fingerprint = sha256(f"{state}\0{device}\0{inode}\0{modified_ns}".encode()).hexdigest()
    return _VirtualSnapshot(device, inode, 0, modified_ns, fingerprint)


def _cursor_unavailable_scan(path: Path, root: Path) -> _Scan:
    if path.is_symlink():
        return _base_scan(path, AgentFormat.CURSOR, root, "unreadable", "symlink_not_allowed")
    if os.path.lexists(path):
        return _base_scan(
            path,
            AgentFormat.CURSOR,
            root,
            "unreadable",
            "store_database_not_regular",
        )
    return _base_scan(path, AgentFormat.CURSOR, root, "missing", "store_database_missing")


@contextmanager
def _opencode_inventory(
    root: Path,
    *,
    database_name: str = "opencode.db",
) -> Iterator[tuple[_VirtualSnapshot, Iterator[sqlite3.Row]]]:
    """Yield a coherent, read-only OpenCode-lineage session inventory."""

    database = root / database_name
    label = "kilo" if database_name == "kilo.db" else "opencode"
    if database.is_symlink():
        raise _OpenCodeInventoryError(f"{label}_database_symlink")
    try:
        stat_result = database.stat()
    except OSError as exc:
        raise _OpenCodeInventoryError(f"{label}_database_unavailable") from exc
    if not database.is_file():
        raise _OpenCodeInventoryError(f"{label}_database_unavailable")
    base_snapshot = _VirtualSnapshot(
        stat_result.st_dev,
        stat_result.st_ino,
        0,
        stat_result.st_mtime_ns,
        "",
    )
    connection: sqlite3.Connection | None = None
    try:
        uri = f"{database.absolute().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=1)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 1000")
        connection.execute("BEGIN")
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(session)").fetchall()
        }
        required = {
            "id",
            "directory",
            "title",
            "version",
            "time_created",
            "time_updated",
        }
        if not required.issubset(columns):
            raise _OpenCodeInventoryError(f"{label}_schema_unsupported")
        selected = [
            "id",
            f"substr(directory, 1, {PATH_VALUE_LIMIT}) AS directory",
            f"substr(title, 1, {LABEL_LIMIT}) AS title",
            f"substr(version, 1, {LABEL_LIMIT}) AS version",
            "time_created",
            "time_updated",
            (
                f"substr(parent_id, 1, {LABEL_LIMIT}) AS parent_id"
                if "parent_id" in columns
                else "NULL AS parent_id"
            ),
            "time_archived" if "time_archived" in columns else "NULL AS time_archived",
        ]
        rows = connection.execute(f"SELECT {', '.join(selected)} FROM session ORDER BY id")  # noqa: S608
    except _OpenCodeInventoryError:
        if connection is not None:
            connection.close()
        raise
    except (OSError, sqlite3.Error) as exc:
        if connection is not None:
            connection.close()
        raise _OpenCodeInventoryError(f"{label}_database_unreadable") from exc
    try:
        yield base_snapshot, iter(rows)
    finally:
        assert connection is not None
        connection.close()


def _scan_opencode_row(
    row: sqlite3.Row,
    database: _VirtualSnapshot,
    agent_format: AgentFormat = AgentFormat.OPENCODE,
) -> tuple[_Scan, _VirtualSnapshot]:
    raw_id = _string(row["id"])
    session_id = raw_id if raw_id and _OPENCODE_SESSION_ID.fullmatch(raw_id) else None
    cwd = _bounded(_string(row["directory"]), PATH_VALUE_LIMIT)
    title = _bounded(_string(row["title"]), LABEL_LIMIT)
    cli_version = _bounded(_string(row["version"]), LABEL_LIMIT)
    parent = _bounded(_string(row["parent_id"]), LABEL_LIMIT)
    created = row["time_created"]
    updated = row["time_updated"]
    archived = row["time_archived"]
    started_at = _iso_from_milliseconds(created)
    updated_at = _iso_from_milliseconds(updated)
    archived_at = _iso_from_milliseconds(archived) if archived is not None else None
    status = "candidate"
    reason = None
    prefix = "kilo" if agent_format == AgentFormat.KILO else "opencode"
    if session_id is None:
        status, reason = "corrupt", f"invalid_{prefix}_session_id"
    elif not cwd or "\0" in cwd or not title or not cli_version:
        status, reason = "corrupt", f"invalid_{prefix}_metadata"
    elif started_at is None or updated_at is None or int(updated) < int(created):
        status, reason = "corrupt", f"invalid_{prefix}_time"
    elif archived is not None and archived_at is None:
        status, reason = "corrupt", f"invalid_{prefix}_archive_time"
    elif parent is not None and not _OPENCODE_SESSION_ID.fullmatch(parent):
        status, reason = "corrupt", f"invalid_{prefix}_parent_id"
    labels = (_Label("native_title", title, 0, 110),) if title else ()
    scan = _Scan(
        session_id=session_id,
        filename_session_id=None,
        cwd=cwd,
        started_at=started_at,
        cli_version=cli_version,
        history_mode=None,
        kind="subagent" if parent else "main",
        lifecycle="archived" if archived is not None else "active",
        parent_session_id=parent,
        status=status,
        reason=reason,
        records=None,
        labels=labels,
    )
    fingerprint = _opencode_row_fingerprint(row)
    modified_ns = (
        int(updated) * 1_000_000
        if _is_non_negative_sqlite_int(updated) and int(updated) <= (2**63 - 1) // 1_000_000
        else database.modified_ns
    )
    snapshot = _VirtualSnapshot(
        database.device,
        database.inode,
        0,
        modified_ns,
        fingerprint,
    )
    return scan, snapshot


def _opencode_row_fingerprint(row: sqlite3.Row) -> str:
    metadata = tuple(
        row[key]
        for key in (
            "id",
            "directory",
            "title",
            "version",
            "time_created",
            "time_updated",
            "parent_id",
            "time_archived",
        )
    )
    return sha256(repr(metadata).encode("utf-8", errors="backslashreplace")).hexdigest()


def _anonymous_row_key(row: sqlite3.Row) -> str:
    return f"invalid-{_opencode_row_fingerprint(row)[:24]}"


def _iso_from_milliseconds(value: Any) -> str | None:
    if not _is_non_negative_sqlite_int(value):
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC).isoformat().replace("+00:00", "Z")
    except (OSError, OverflowError, ValueError):
        return None


def _is_non_negative_sqlite_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _copilot_native_metadata(root: Path) -> _NativeMetadata:
    """Read only bounded picker names, never event/message content."""

    try:
        paths = list(_candidate_files(AgentFormat.COPILOT, root))
    except OSError:
        return _NativeMetadata({}, {}, {}, False)
    by_path: dict[str, tuple[_Label, ...]] = {}
    for event_path in paths:
        workspace = event_path.parent / "workspace.yaml"
        if workspace.is_symlink() or not workspace.is_file():
            continue
        try:
            raw = workspace.read_bytes()
        except OSError:
            return _NativeMetadata({}, {}, {}, False)
        if len(raw) > 64 * 1024:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        title = _copilot_workspace_name(text)
        if title:
            by_path[str(_absolute(event_path))] = (_Label("native_name", title, 0, 100),)
    return _NativeMetadata(by_path, {}, {}, True)


def _copilot_workspace_name(text: str) -> str | None:
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if not separator or key.strip() != "name":
            continue
        candidate = value.strip()
        if candidate.startswith('"'):
            try:
                decoded = json.loads(candidate)
            except json.JSONDecodeError:
                return None
            candidate = decoded if isinstance(decoded, str) else ""
        if not candidate or "\0" in candidate:
            return None
        return _bounded(candidate, LABEL_LIMIT)
    return None


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
        raise SessionMigrateError(f"{option} must be a timezone-aware RFC-3339 timestamp")
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
