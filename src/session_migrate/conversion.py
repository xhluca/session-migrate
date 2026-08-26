"""Conversion orchestration, manifests, target paths, and atomic installation."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import uuid
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from session_migrate import __version__
from session_migrate.errors import FormatDetectionError, JsonlError, SessionMigrateError
from session_migrate.formats import (
    antigravity,
    claude,
    codex,
    copilot,
    cursor,
    grok,
    kilo,
    kimi,
    muse,
    omp,
    opencode,
    openhands,
    pi,
    qwen,
    vibe,
)
from session_migrate.formats.common import valid_rfc3339
from session_migrate.inspection import detect_path_format
from session_migrate.jsonl import (
    ensure_file_unchanged,
    file_snapshot,
    write_private_atomic,
)
from session_migrate.model import AgentFormat, Session, TargetFormat

OPENCODE_HOME_UNSUPPORTED = (
    "--home is not supported for OpenCode imports; control OpenCode's normal HOME/XDG "
    "environment instead"
)
KILO_HOME_UNSUPPORTED = (
    "--home is not supported for Kilo imports; control Kilo's normal HOME/XDG environment instead"
)
OPENCODE_COMMAND_TIMEOUT_SECONDS = 30
OPENCODE_EXPORT_TIMEOUT_SECONDS = 120


@dataclass(frozen=True, slots=True)
class ConversionOptions:
    target_format: TargetFormat | AgentFormat
    session_id: str | None = None
    cwd: Path | None = None
    target_cli_version: str | None = None
    model_provider: str | None = None
    model: str | None = None

    def __post_init__(self) -> None:
        value = (
            self.target_format.value
            if isinstance(self.target_format, (TargetFormat, AgentFormat))
            else self.target_format
        )
        object.__setattr__(self, "target_format", TargetFormat(value))


@dataclass(frozen=True, slots=True)
class ConversionArtifact:
    source: Session
    target_format: TargetFormat
    session_id: str
    cwd: Path
    target_cli_version: str
    timestamp: str
    native_bytes: bytes
    native_record_count: int
    dropped: dict[str, int]
    warnings: tuple[dict[str, Any], ...]

    @property
    def target_sha256(self) -> str:
        return hashlib.sha256(self.native_bytes).hexdigest()

    def manifest(self, *, output_path: Path | str) -> dict[str, Any]:
        target_location = (
            str(output_path.resolve()) if isinstance(output_path, Path) else output_path
        )
        return {
            "schema_version": 2,
            "created_at": _utc_now(),
            "migration_version": __version__,
            "source": {
                "format": self.source.source_format.value,
                "path": str(self.source.source_path),
                "sha256": self.source.source_sha256,
                "session_id": self.source.session_id,
                "cli_version": self.source.cli_version,
                "records": self.source.raw_record_count,
                "events": self.source.event_counts(),
            },
            "target": {
                "format": self.target_format.value,
                "path": target_location,
                "sha256": self.target_sha256,
                "session_id": self.session_id,
                "cli_version": self.target_cli_version,
                "cwd": str(self.cwd),
                "timestamp": self.timestamp,
                "records": self.native_record_count,
            },
            "dropped_events": self.dropped,
            "warnings": list(self.warnings),
        }


def load_session(path: Path, source_format: AgentFormat | None = None) -> Session:
    source_format = source_format or detect_path_format(path)
    if source_format == AgentFormat.ANTIGRAVITY:
        # The adapter makes a transactionally consistent SQLite backup that
        # includes live WAL state; a plain file stat/hash check would not.
        return antigravity.parse_session(path)
    if source_format == AgentFormat.CURSOR:
        # Cursor is also a live SQLite store. Its adapter takes a consistent
        # backup including committed WAL state before projecting the graph.
        return cursor.project_session(cursor.parse(path), source_format=AgentFormat.CURSOR)
    if source_format == AgentFormat.VIBE:
        # Vibe sessions span meta.json and messages.jsonl; the adapter snapshots
        # and hashes both files as one source transaction.
        return vibe.parse_session(path)
    if source_format == AgentFormat.KIMI:
        # Kimi sessions span state.json and the main-agent wire journal.
        return kimi.parse_session(path)
    if source_format == AgentFormat.GROK:
        return grok.parse_session(path)
    if source_format == AgentFormat.OPENHANDS:
        return openhands.parse_session(path)
    before = file_snapshot(path)
    if source_format == AgentFormat.CLAUDE:
        session = claude.parse(path)
    elif source_format == AgentFormat.CODEX:
        session = codex.parse(path)
    elif source_format == AgentFormat.PI:
        session = pi.parse_session(path)
    elif source_format == AgentFormat.OMP:
        session = omp.parse_session(path)
    elif source_format == AgentFormat.OPENCODE:
        session = opencode.parse_session(path)
    elif source_format == AgentFormat.KILO:
        session = kilo.parse_session(path)
    elif source_format == AgentFormat.COPILOT:
        session = copilot.parse_session(path)
    elif source_format == AgentFormat.MUSE:
        session = muse.parse_session(path)
    elif source_format == AgentFormat.QWEN:
        session = qwen.parse_session(path)
    else:
        raise FormatDetectionError(f"unsupported source format: {source_format}")
    ensure_file_unchanged(path, before)
    return session


def load_opencode_session(
    session_id: str,
    *,
    source_cli: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Session:
    """Export and parse one native OpenCode session through its official CLI."""

    if not session_id.startswith("ses_"):
        raise SessionMigrateError("source OpenCode session ID is invalid")
    values = dict(os.environ if environ is None else environ)
    values.setdefault("OPENCODE_DISABLE_AUTOUPDATE", "true")
    values.setdefault("OPENCODE_DISABLE_PRUNE", "true")
    cli = _resolve_opencode_cli(source_cli, values)
    observed_version = _opencode_version(cli, values)
    if observed_version != opencode.PINNED_OPENCODE_VERSION:
        raise SessionMigrateError(
            "OpenCode source CLI version mismatch: expected "
            f"{opencode.PINNED_OPENCODE_VERSION}, observed {observed_version}"
        )
    temporary_root = values.get("TMPDIR")
    with tempfile.TemporaryDirectory(
        prefix="session-migrate-opencode-source-", dir=temporary_root
    ) as directory_name:
        directory = Path(directory_name)
        os.chmod(directory, 0o700)
        export_path = directory / "export.json"
        _invoke_opencode_export(cli, session_id, export_path, values)
        session = load_session(export_path, AgentFormat.OPENCODE)
    if session.session_id != session_id:
        raise SessionMigrateError("OpenCode export metadata does not match the requested session")
    return replace(session, source_path=Path(f"opencode:{session_id}"))


def load_kilo_session(
    session_id: str,
    *,
    source_cli: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Session:
    """Export and parse one native Kilo session through its official CLI."""

    if not session_id.startswith("ses_"):
        raise SessionMigrateError("source Kilo session ID is invalid")
    values = dict(os.environ if environ is None else environ)
    values.setdefault("OPENCODE_DISABLE_AUTOUPDATE", "true")
    values.setdefault("OPENCODE_DISABLE_PRUNE", "true")
    cli = _resolve_kilo_cli(source_cli, values)
    observed_version = _kilo_version(cli, values)
    if observed_version != kilo.PINNED_KILO_VERSION:
        raise SessionMigrateError(
            "Kilo source CLI version mismatch: expected "
            f"{kilo.PINNED_KILO_VERSION}, observed {observed_version}"
        )
    temporary_root = values.get("TMPDIR")
    with tempfile.TemporaryDirectory(
        prefix="session-migrate-kilo-source-", dir=temporary_root
    ) as directory_name:
        directory = Path(directory_name)
        os.chmod(directory, 0o700)
        export_path = directory / "export.json"
        _invoke_kilo_export(cli, session_id, export_path, values)
        session = load_session(export_path, AgentFormat.KILO)
    if session.session_id != session_id:
        raise SessionMigrateError("Kilo export metadata does not match the requested session")
    return replace(session, source_path=Path(f"kilo:{session_id}"))


def convert_session(session: Session, options: ConversionOptions) -> ConversionArtifact:
    target_format = TargetFormat(options.target_format.value)
    same_format_rewrite = session.source_format.value == target_format.value
    portable_id = _validated_uuid(options.session_id) if options.session_id else str(uuid.uuid4())
    if target_format in {TargetFormat.OPENCODE, TargetFormat.KILO}:
        target_id = opencode.session_id_from_uuid(portable_id)
    elif target_format == TargetFormat.KIMI:
        target_id = kimi.native_session_id(portable_id)
    else:
        target_id = portable_id
    target_cwd = (options.cwd or session.cwd or Path.cwd()).resolve()
    timestamp = valid_rfc3339(session.started_at) or _utc_now()
    warnings: list[dict[str, Any]] = []
    if same_format_rewrite:
        warnings.append(
            {
                "code": "same_format_portable_rewrite",
                "message": (
                    "same-format migration creates a new portable native session; "
                    "source-only runtime metadata may be omitted and is counted below"
                ),
            }
        )
    if session.started_at and not valid_rfc3339(session.started_at):
        warnings.append(
            {
                "code": "invalid_session_timestamp",
                "message": "source session timestamp was invalid; used the current time",
            }
        )
    if session.cwd is None and options.cwd is None:
        warnings.append(
            {
                "code": "synthesized_cwd",
                "message": "source had no working directory; used the current directory",
            }
        )
    if not target_cwd.is_dir():
        warnings.append(
            {
                "code": "cwd_not_directory",
                "message": "target working directory does not exist on this machine",
            }
        )

    provider = options.model_provider or (
        "openai"
        if target_format == TargetFormat.CODEX
        else "meta"
        if target_format == TargetFormat.MUSE
        else session.model_provider
        or ("anthropic" if session.source_format == AgentFormat.CLAUDE else "openai")
    )
    if target_format == TargetFormat.CLAUDE:
        target_version = options.target_cli_version or claude.PINNED_CLAUDE_VERSION
        native_bytes, dropped = claude.serialize(
            session,
            session_id=target_id,
            cwd=target_cwd,
            cli_version=target_version,
            model=options.model,
            timestamp=timestamp,
        )
    elif target_format == TargetFormat.CODEX:
        target_version = options.target_cli_version or codex.PINNED_CODEX_VERSION
        native_bytes, dropped = codex.serialize(
            session,
            session_id=target_id,
            cwd=target_cwd,
            cli_version=target_version,
            model_provider=provider,
            timestamp=timestamp,
        )
    elif target_format == TargetFormat.PI:
        target_version = options.target_cli_version or pi.PINNED_PI_VERSION
        native_bytes, dropped = pi.serialize(
            session,
            session_id=target_id,
            cwd=target_cwd,
            cli_version=target_version,
            provider=provider,
            model=options.model,
            timestamp=timestamp,
        )
    elif target_format == TargetFormat.OMP:
        target_version = options.target_cli_version or omp.PINNED_OMP_VERSION
        native_bytes, dropped = omp.serialize(
            session,
            session_id=target_id,
            cwd=target_cwd,
            cli_version=target_version,
            provider=provider,
            model=options.model,
            timestamp=timestamp,
            name=session.title,
        )
    elif target_format == TargetFormat.COPILOT:
        target_version = options.target_cli_version or copilot.PINNED_COPILOT_VERSION
        native_bytes, dropped = copilot.serialize(
            session,
            session_id=target_id,
            cwd=target_cwd,
            cli_version=target_version,
            model=options.model,
            timestamp=timestamp,
        )
    elif target_format == TargetFormat.ANTIGRAVITY:
        target_version = options.target_cli_version or antigravity.PINNED_ANTIGRAVITY_VERSION
        native_bytes, dropped = antigravity.serialize(
            session,
            session_id=target_id,
            cwd=target_cwd,
            cli_version=target_version,
            model=options.model,
            timestamp=timestamp,
        )
    elif target_format == TargetFormat.CURSOR:
        target_version = options.target_cli_version or cursor.PINNED_CURSOR_VERSION
        native_bytes, dropped = cursor.serialize(
            session,
            session_id=target_id,
            cwd=target_cwd,
            timestamp=timestamp,
            title=session.title,
        )
    elif target_format == TargetFormat.VIBE:
        target_version = options.target_cli_version or vibe.PINNED_VIBE_VERSION
        native_bytes, dropped = vibe.serialize(
            session,
            session_id=target_id,
            cwd=target_cwd,
            cli_version=target_version,
            model=options.model,
            timestamp=timestamp,
            title=session.title,
        )
    elif target_format == TargetFormat.MUSE:
        target_version = options.target_cli_version or muse.PINNED_MUSE_VERSION
        native_bytes, dropped = muse.serialize(
            session,
            session_id=target_id,
            cwd=target_cwd,
            cli_version=target_version,
            model=options.model,
            provider=provider,
            timestamp=timestamp,
        )
    elif target_format == TargetFormat.QWEN:
        target_version = options.target_cli_version or qwen.PINNED_QWEN_VERSION
        native_bytes, dropped = qwen.serialize(
            session,
            session_id=target_id,
            cwd=target_cwd,
            cli_version=target_version,
            model=options.model,
            timestamp=timestamp,
            title=session.title,
        )
    elif target_format == TargetFormat.KIMI:
        target_version = options.target_cli_version or kimi.PINNED_KIMI_VERSION
        native_bytes, dropped = kimi.serialize(
            session,
            session_id=target_id,
            cwd=target_cwd,
            cli_version=target_version,
            model=options.model,
            timestamp=timestamp,
            title=session.title,
        )
    elif target_format == TargetFormat.GROK:
        target_version = options.target_cli_version or grok.PINNED_GROK_VERSION
        native_bytes, dropped = grok.serialize(
            session,
            session_id=target_id,
            cwd=target_cwd,
            cli_version=target_version,
            model=options.model,
            timestamp=timestamp,
            title=session.title,
        )
    elif target_format == TargetFormat.OPENHANDS:
        target_version = options.target_cli_version or openhands.PINNED_OPENHANDS_VERSION
        native_bytes, dropped = openhands.serialize(
            session,
            session_id=target_id,
            cwd=target_cwd,
            cli_version=target_version,
            model=options.model,
            timestamp=timestamp,
            title=session.title,
        )
    elif target_format == TargetFormat.KILO:
        target_version = options.target_cli_version or kilo.PINNED_KILO_VERSION
        native_bytes, dropped = kilo.serialize(
            session,
            session_id=target_id,
            cwd=target_cwd,
            cli_version=target_version,
            provider_id=provider,
            model_id=options.model,
            timestamp=timestamp,
            title=session.title,
        )
    else:
        target_version = options.target_cli_version or opencode.PINNED_OPENCODE_VERSION
        native_bytes, dropped = opencode.serialize(
            session,
            session_id=target_id,
            cwd=target_cwd,
            cli_version=target_version,
            provider_id=provider,
            model_id=options.model,
            timestamp=timestamp,
        )
    pinned_target = _pinned_target_version(target_format)
    if target_version != pinned_target:
        warnings.append(
            {
                "code": "unvalidated_target_version",
                "observed": target_version,
                "validated": pinned_target,
                "message": "target metadata version differs but the writer schema remains pinned",
            }
        )
    if not native_bytes:
        raise SessionMigrateError("conversion produced no resumable conversation history")
    _validate_native_bytes(native_bytes, target_format, target_id)
    for kind, count in dropped.items():
        message = "target conversion omitted or transformed this source detail"
        if kind == "compaction:replacement_history_expanded":
            message = (
                "Codex encrypted compaction state cannot be decoded by Claude; "
                "the visible pre-compaction transcript was retained instead"
            )
        elif kind == "message:ui_only_projection":
            message = (
                "a Codex UI-only message had no exact canonical response-item match; "
                "it was retained as visible conversation history"
            )
        elif kind in {
            "tool_call:duplicate_id",
            "tool_result:duplicate_id",
            "tool_result:orphan_id",
        }:
            message = (
                "source tool linkage is inconsistent; the record was retained, "
                "but the target CLI may diagnose or normalize it"
            )
        elif kind == "tool_result:image_provider_dependent":
            message = (
                "the exact image remains in Copilot's native content-addressed asset "
                "store, but replay into model context depends on the selected provider "
                "and wire protocol"
            )
        warnings.append(
            {
                "code": "dropped_event_kind",
                "event_kind": kind,
                "count": count,
                "message": message,
            }
        )
    if session.cli_version:
        pinned_source = {
            AgentFormat.CLAUDE: claude.PINNED_CLAUDE_VERSION,
            AgentFormat.CODEX: codex.PINNED_CODEX_VERSION,
            AgentFormat.PI: pi.PINNED_PI_VERSION,
            AgentFormat.OMP: omp.PINNED_OMP_VERSION,
            AgentFormat.OPENCODE: opencode.PINNED_OPENCODE_VERSION,
            AgentFormat.COPILOT: copilot.PINNED_COPILOT_VERSION,
            AgentFormat.ANTIGRAVITY: antigravity.PINNED_ANTIGRAVITY_VERSION,
            AgentFormat.CURSOR: cursor.PINNED_CURSOR_VERSION,
            AgentFormat.VIBE: vibe.PINNED_VIBE_VERSION,
            AgentFormat.MUSE: muse.PINNED_MUSE_VERSION,
            AgentFormat.QWEN: qwen.PINNED_QWEN_VERSION,
            AgentFormat.KIMI: kimi.PINNED_KIMI_VERSION,
            AgentFormat.GROK: grok.PINNED_GROK_VERSION,
            AgentFormat.KILO: kilo.PINNED_KILO_VERSION,
            AgentFormat.OPENHANDS: openhands.PINNED_OPENHANDS_VERSION,
        }[session.source_format]
        if session.cli_version != pinned_source:
            warnings.append(
                {
                    "code": "unvalidated_source_version",
                    "observed": session.cli_version,
                    "validated": pinned_source,
                    "message": "source version differs from the pinned integration target",
                }
            )
    return ConversionArtifact(
        source=session,
        target_format=target_format,
        session_id=target_id,
        cwd=target_cwd,
        target_cli_version=target_version,
        timestamp=timestamp,
        native_bytes=native_bytes,
        native_record_count=_native_record_count(native_bytes, target_format),
        dropped=dropped,
        warnings=tuple(warnings),
    )


def target_import_paths(artifact: ConversionArtifact, target_home: Path) -> tuple[Path, Path]:
    target_home = target_home.expanduser().resolve()
    if artifact.target_format == TargetFormat.CLAUDE:
        native_path = (
            target_home
            / "projects"
            / claude.project_directory_name(artifact.cwd)
            / f"{artifact.session_id}.jsonl"
        )
    elif artifact.target_format == TargetFormat.CODEX:
        native_path = target_home / codex.rollout_relative_path(
            artifact.session_id, artifact.timestamp
        )
    elif artifact.target_format == TargetFormat.PI:
        native_path = target_home / pi.session_relative_path(
            artifact.cwd, artifact.session_id, artifact.timestamp
        )
    elif artifact.target_format == TargetFormat.OMP:
        native_path = target_home / omp.session_relative_path(
            artifact.cwd, artifact.session_id, artifact.timestamp
        )
    elif artifact.target_format == TargetFormat.COPILOT:
        native_path = target_home / copilot.session_relative_path(artifact.session_id)
    elif artifact.target_format == TargetFormat.ANTIGRAVITY:
        native_path = target_home / antigravity.session_relative_path(artifact.session_id)
    elif artifact.target_format == TargetFormat.CURSOR:
        native_path = target_home / cursor.session_relative_path(artifact.session_id, artifact.cwd)
    elif artifact.target_format == TargetFormat.VIBE:
        native_path = target_home / vibe.session_relative_path(
            artifact.session_id, artifact.timestamp
        )
    elif artifact.target_format == TargetFormat.MUSE:
        native_path = target_home / muse.session_relative_path(
            artifact.session_id, artifact.timestamp
        )
    elif artifact.target_format == TargetFormat.QWEN:
        native_path = target_home / qwen.session_relative_path(artifact.cwd, artifact.session_id)
    elif artifact.target_format == TargetFormat.KIMI:
        native_path = target_home / kimi.session_relative_path(artifact.cwd, artifact.session_id)
    elif artifact.target_format == TargetFormat.GROK:
        native_path = target_home / grok.session_relative_path(artifact.cwd, artifact.session_id)
    elif artifact.target_format == TargetFormat.OPENHANDS:
        native_path = target_home / openhands.session_relative_path(artifact.session_id)
    else:
        raise SessionMigrateError(
            f"{artifact.target_format.value} does not use filesystem target import paths"
        )
    manifest_path = target_home / "session-migrate" / "manifests" / (f"{artifact.session_id}.json")
    return native_path, manifest_path


def default_target_home(target_format: TargetFormat | AgentFormat) -> Path:
    if target_format.value == TargetFormat.CLAUDE.value:
        configured = os.environ.get("CLAUDE_CONFIG_DIR")
        return Path(configured).expanduser() if configured else Path.home() / ".claude"
    if target_format.value == TargetFormat.CODEX.value:
        configured = os.environ.get("CODEX_HOME")
        return Path(configured).expanduser() if configured else Path.home() / ".codex"
    if target_format.value == TargetFormat.PI.value:
        configured = os.environ.get("PI_CODING_AGENT_DIR")
        return Path(configured).expanduser() if configured else Path.home() / ".pi" / "agent"
    if target_format.value == TargetFormat.OMP.value:
        configured = os.environ.get("PI_CODING_AGENT_DIR")
        return Path(configured).expanduser() if configured else Path.home() / ".omp" / "agent"
    if target_format.value == TargetFormat.COPILOT.value:
        configured = os.environ.get("COPILOT_HOME")
        return Path(configured).expanduser() if configured else Path.home() / ".copilot"
    if target_format.value == TargetFormat.ANTIGRAVITY.value:
        return antigravity.app_data_home()
    if target_format.value == TargetFormat.CURSOR.value:
        return cursor.config_home()
    if target_format.value == TargetFormat.VIBE.value:
        return vibe.vibe_home()
    if target_format.value == TargetFormat.MUSE.value:
        xdg_data = os.environ.get("XDG_DATA_HOME")
        data_home = Path(xdg_data).expanduser() if xdg_data else Path.home() / ".local/share"
        return data_home / "muse"
    if target_format.value == TargetFormat.QWEN.value:
        configured = os.environ.get("QWEN_HOME")
        return Path(configured).expanduser() if configured else Path.home() / ".qwen"
    if target_format.value == TargetFormat.KIMI.value:
        configured = os.environ.get("KIMI_CODE_HOME")
        return Path(configured).expanduser() if configured else Path.home() / ".kimi-code"
    if target_format.value == TargetFormat.GROK.value:
        return grok.grok_home()
    if target_format.value == TargetFormat.OPENHANDS.value:
        return openhands.conversations_home()
    raise SessionMigrateError(f"{target_format.value} does not expose a filesystem target home")


def default_migration_state_home(
    *, environ: Mapping[str, str] | None = None, home: Path | None = None
) -> Path:
    """Return the private migrator state directory without creating it."""

    values = os.environ if environ is None else environ
    configured = values.get("XDG_STATE_HOME")
    base = Path(configured).expanduser() if configured else (home or Path.home()) / ".local/state"
    return _absolute_no_follow(base / "session-migrate")


def opencode_manifest_path(artifact: ConversionArtifact, *, state_home: Path | None = None) -> Path:
    if artifact.target_format != TargetFormat.OPENCODE:
        raise SessionMigrateError("OpenCode manifest paths require an OpenCode artifact")
    base = _absolute_no_follow(state_home) if state_home else default_migration_state_home()
    return base / "manifests" / "opencode" / f"{artifact.session_id}.json"


def kilo_manifest_path(artifact: ConversionArtifact, *, state_home: Path | None = None) -> Path:
    if artifact.target_format != TargetFormat.KILO:
        raise SessionMigrateError("Kilo manifest paths require a Kilo artifact")
    base = _absolute_no_follow(state_home) if state_home else default_migration_state_home()
    return base / "manifests" / "kilo" / f"{artifact.session_id}.json"


def write_artifact(artifact: ConversionArtifact, *, output_path: Path, manifest_path: Path) -> None:
    output_path = _absolute_no_follow(output_path)
    manifest_path = _absolute_no_follow(manifest_path)
    ensure_target_paths_available(output_path, manifest_path)
    manifest_bytes = (
        json.dumps(artifact.manifest(output_path=output_path), indent=2, sort_keys=True) + "\n"
    ).encode()
    output_identity: tuple[int, int] | None = None
    manifest_identity: tuple[int, int] | None = None
    output_guard: int | None = None
    manifest_guard: int | None = None
    try:
        output_identity = write_private_atomic(output_path, artifact.native_bytes)
        output_guard = _open_identity_guard(output_path, output_identity)
        manifest_identity = write_private_atomic(manifest_path, manifest_bytes)
        manifest_guard = _open_identity_guard(manifest_path, manifest_identity)
        if not _path_matches_identity(output_path, output_identity) or not _path_matches_identity(
            manifest_path, manifest_identity
        ):
            raise JsonlError("conversion artifact changed during installation")
    except BaseException:
        if output_identity is not None:
            _unlink_if_identity_matches(output_path, output_identity)
        if manifest_identity is not None:
            _unlink_if_identity_matches(manifest_path, manifest_identity)
        raise
    finally:
        if output_guard is not None:
            os.close(output_guard)
        if manifest_guard is not None:
            os.close(manifest_guard)


def install_copilot_artifact(
    artifact: ConversionArtifact,
    *,
    target_home: Path,
    dry_run: bool = False,
) -> tuple[Path, Path]:
    """Install Copilot's canonical event log and workspace sidecar privately."""

    if artifact.target_format != TargetFormat.COPILOT:
        raise SessionMigrateError("Copilot installation requires a Copilot artifact")
    copilot.validate_native_bytes(artifact.native_bytes, artifact.session_id)
    events_path, manifest_path = target_import_paths(artifact, target_home)
    session_directory = events_path.parent
    workspace_path = session_directory / "workspace.yaml"
    ensure_target_paths_available(session_directory, manifest_path)
    if dry_run:
        return events_path, manifest_path

    manifest_bytes = (
        json.dumps(artifact.manifest(output_path=events_path), indent=2, sort_keys=True) + "\n"
    ).encode()
    workspace_data = copilot.workspace_bytes(
        session_id=artifact.session_id,
        cwd=artifact.cwd,
        timestamp=artifact.timestamp,
        title=artifact.source.title,
    )
    created_directory = False
    identities: list[tuple[Path, tuple[int, int]]] = []
    guards: list[int] = []
    try:
        _mkdir_private_tree(session_directory.parent)
        try:
            session_directory.mkdir(mode=0o700)
            created_directory = True
        except FileExistsError as exc:
            raise JsonlError(
                f"refusing to overwrite existing Copilot session: {session_directory}"
            ) from exc
        for path, data in (
            (events_path, artifact.native_bytes),
            (workspace_path, workspace_data),
            (manifest_path, manifest_bytes),
        ):
            identity = write_private_atomic(path, data)
            identities.append((path, identity))
            guards.append(_open_identity_guard(path, identity))
        if not all(_path_matches_identity(path, identity) for path, identity in identities):
            raise JsonlError("Copilot artifact changed during installation")
    except BaseException:
        for path, identity in reversed(identities):
            _unlink_if_identity_matches(path, identity)
        if created_directory:
            with suppress(OSError):
                session_directory.rmdir()
        raise
    finally:
        for descriptor in guards:
            os.close(descriptor)
    return events_path, manifest_path


def install_antigravity_artifact(
    artifact: ConversionArtifact,
    *,
    target_home: Path,
    target_cli: Path | None = None,
    dry_run: bool = False,
    environ: Mapping[str, str] | None = None,
) -> tuple[Path, Path]:
    """Install a pinned Antigravity DB and finalize a content-free manifest."""

    if artifact.target_format != TargetFormat.ANTIGRAVITY:
        raise SessionMigrateError("Antigravity installation requires an Antigravity artifact")
    if artifact.target_cli_version != antigravity.PINNED_ANTIGRAVITY_VERSION:
        raise SessionMigrateError(
            "automatic Antigravity import requires target metadata version "
            f"{antigravity.PINNED_ANTIGRAVITY_VERSION}; convert-only artifacts may opt "
            "into unvalidated metadata versions"
        )
    native_path, manifest_path = target_import_paths(artifact, target_home)
    ensure_target_paths_available(manifest_path)
    if dry_run:
        installed = antigravity.install_database(
            artifact.native_bytes,
            session_id=artifact.session_id,
            cwd=artifact.cwd,
            timestamp=artifact.timestamp,
            title=artifact.source.title,
            target_home=target_home,
            target_cli=target_cli,
            dry_run=True,
            environ=environ,
        )
        if installed.conversation_path != native_path:
            raise SessionMigrateError("Antigravity installer resolved an unexpected target path")
        return native_path, manifest_path

    manifest_bytes = (
        json.dumps(artifact.manifest(output_path=native_path), indent=2, sort_keys=True) + "\n"
    ).encode()
    reservation_identity: tuple[int, int] | None = None
    reservation_guard: int | None = None
    install_succeeded = False
    try:
        reservation_identity = write_private_atomic(manifest_path, b"")
        reservation_guard = _open_identity_guard(manifest_path, reservation_identity, writable=True)
        installed = antigravity.install_database(
            artifact.native_bytes,
            session_id=artifact.session_id,
            cwd=artifact.cwd,
            timestamp=artifact.timestamp,
            title=artifact.source.title,
            target_home=target_home,
            target_cli=target_cli,
            environ=environ,
        )
        install_succeeded = True
        if installed.conversation_path != native_path:
            raise SessionMigrateError("Antigravity installer resolved an unexpected target path")
        _write_reserved_file(
            reservation_guard,
            manifest_path,
            reservation_identity,
            manifest_bytes,
        )
    except BaseException as exc:
        if reservation_identity is not None:
            _unlink_if_identity_matches(manifest_path, reservation_identity)
        if install_succeeded:
            raise SessionMigrateError(
                "Antigravity import succeeded but migrator manifest finalization failed; "
                f"the native session may already exist as {artifact.session_id}"
            ) from exc
        raise
    finally:
        if reservation_guard is not None:
            os.close(reservation_guard)
    return native_path, manifest_path


def install_cursor_artifact(
    artifact: ConversionArtifact,
    *,
    target_home: Path,
    target_cli: Path | None = None,
    dry_run: bool = False,
    environ: Mapping[str, str] | None = None,
) -> tuple[Path, Path]:
    """Install a pinned experimental Cursor DB and finalize its manifest."""

    if artifact.target_format != TargetFormat.CURSOR:
        raise SessionMigrateError("Cursor installation requires a Cursor artifact")
    if artifact.target_cli_version != cursor.PINNED_CURSOR_VERSION:
        raise SessionMigrateError(
            "automatic Cursor import requires target metadata version "
            f"{cursor.PINNED_CURSOR_VERSION}; convert-only artifacts may opt into "
            "unvalidated metadata versions"
        )
    native_path, manifest_path = target_import_paths(artifact, target_home)
    ensure_target_paths_available(manifest_path)
    if dry_run:
        installed = cursor.install_database(
            artifact.native_bytes,
            session_id=artifact.session_id,
            cwd=artifact.cwd,
            target_home=target_home,
            target_cli=target_cli,
            dry_run=True,
            environ=environ,
        )
        if installed.conversation_path != native_path:
            raise SessionMigrateError("Cursor installer resolved an unexpected target path")
        return native_path, manifest_path

    manifest_bytes = (
        json.dumps(artifact.manifest(output_path=native_path), indent=2, sort_keys=True) + "\n"
    ).encode()
    reservation_identity: tuple[int, int] | None = None
    reservation_guard: int | None = None
    install_succeeded = False
    try:
        reservation_identity = write_private_atomic(manifest_path, b"")
        reservation_guard = _open_identity_guard(manifest_path, reservation_identity, writable=True)
        installed = cursor.install_database(
            artifact.native_bytes,
            session_id=artifact.session_id,
            cwd=artifact.cwd,
            target_home=target_home,
            target_cli=target_cli,
            environ=environ,
        )
        install_succeeded = True
        if installed.conversation_path != native_path:
            raise SessionMigrateError("Cursor installer resolved an unexpected target path")
        _write_reserved_file(
            reservation_guard,
            manifest_path,
            reservation_identity,
            manifest_bytes,
        )
    except BaseException as exc:
        if reservation_identity is not None:
            _unlink_if_identity_matches(manifest_path, reservation_identity)
        if install_succeeded:
            raise SessionMigrateError(
                "Cursor import succeeded but migrator manifest finalization failed; "
                f"the native session may already exist as {artifact.session_id}"
            ) from exc
        raise
    finally:
        if reservation_guard is not None:
            os.close(reservation_guard)
    return native_path, manifest_path


def install_vibe_artifact(
    artifact: ConversionArtifact,
    *,
    target_home: Path,
    dry_run: bool = False,
) -> tuple[Path, Path]:
    """Install Vibe's native meta.json + messages.jsonl session directory."""

    if artifact.target_format != TargetFormat.VIBE:
        raise SessionMigrateError("Vibe installation requires a Vibe artifact")
    meta_bytes, messages_bytes = vibe.native_files(artifact.native_bytes, artifact.session_id)
    messages_path, manifest_path = target_import_paths(artifact, target_home)
    session_directory = messages_path.parent
    meta_path = session_directory / vibe.META_FILENAME
    short_id_collisions = tuple(
        (target_home.expanduser().resolve() / "logs/session").glob(
            f"session_*_{artifact.session_id[:8]}"
        )
    )
    ensure_target_paths_available(session_directory, manifest_path, *short_id_collisions)
    if dry_run:
        return messages_path, manifest_path

    manifest_bytes = (
        json.dumps(artifact.manifest(output_path=messages_path), indent=2, sort_keys=True) + "\n"
    ).encode()
    created_directory = False
    identities: list[tuple[Path, tuple[int, int]]] = []
    guards: list[int] = []
    try:
        _mkdir_private_tree(session_directory.parent)
        try:
            session_directory.mkdir(mode=0o700)
            created_directory = True
        except FileExistsError as exc:
            raise JsonlError(
                f"refusing to overwrite existing Vibe session: {session_directory}"
            ) from exc
        for path, data in (
            (meta_path, meta_bytes),
            (messages_path, messages_bytes),
            (manifest_path, manifest_bytes),
        ):
            identity = write_private_atomic(path, data)
            identities.append((path, identity))
            guards.append(_open_identity_guard(path, identity))
        if not all(_path_matches_identity(path, identity) for path, identity in identities):
            raise JsonlError("Vibe artifact changed during installation")
    except BaseException:
        for path, identity in reversed(identities):
            _unlink_if_identity_matches(path, identity)
        if created_directory:
            with suppress(OSError):
                session_directory.rmdir()
        raise
    finally:
        for descriptor in guards:
            os.close(descriptor)
    return messages_path, manifest_path


def install_kimi_artifact(
    artifact: ConversionArtifact,
    *,
    target_home: Path,
    dry_run: bool = False,
) -> tuple[Path, Path]:
    """Install Kimi's state document and main-agent wire journal."""

    if artifact.target_format != TargetFormat.KIMI:
        raise SessionMigrateError("Kimi installation requires a Kimi artifact")
    wire_path, manifest_path = target_import_paths(artifact, target_home)
    session_directory = wire_path.parent.parent.parent
    state_path = session_directory / kimi.STATE_FILENAME
    ensure_target_paths_available(session_directory, manifest_path)
    if dry_run:
        return wire_path, manifest_path

    state_bytes, wire_bytes = kimi.native_files(
        artifact.native_bytes, artifact.session_id, session_directory
    )
    manifest_bytes = (
        json.dumps(artifact.manifest(output_path=wire_path), indent=2, sort_keys=True) + "\n"
    ).encode()
    created_directory = False
    identities: list[tuple[Path, tuple[int, int]]] = []
    guards: list[int] = []
    try:
        _mkdir_private_tree(session_directory.parent)
        try:
            session_directory.mkdir(mode=0o700)
            created_directory = True
        except FileExistsError as exc:
            raise JsonlError(
                f"refusing to overwrite existing Kimi session: {session_directory}"
            ) from exc
        _mkdir_private_tree(wire_path.parent)
        for path, data in (
            (state_path, state_bytes),
            (wire_path, wire_bytes),
            (manifest_path, manifest_bytes),
        ):
            identity = write_private_atomic(path, data)
            identities.append((path, identity))
            guards.append(_open_identity_guard(path, identity))
        if not all(_path_matches_identity(path, identity) for path, identity in identities):
            raise JsonlError("Kimi artifact changed during installation")
    except BaseException:
        for path, identity in reversed(identities):
            _unlink_if_identity_matches(path, identity)
        if created_directory:
            with suppress(OSError):
                wire_path.parent.rmdir()
            with suppress(OSError):
                wire_path.parent.parent.rmdir()
            with suppress(OSError):
                session_directory.rmdir()
        raise
    finally:
        for descriptor in guards:
            os.close(descriptor)
    return wire_path, manifest_path


def install_grok_artifact(
    artifact: ConversionArtifact,
    *,
    target_home: Path,
    dry_run: bool = False,
) -> tuple[Path, Path]:
    """Install Grok's summary and ACP update log as one private session directory."""

    if artifact.target_format != TargetFormat.GROK:
        raise SessionMigrateError("Grok installation requires a Grok artifact")
    summary_bytes, updates_bytes = grok.native_files(artifact.native_bytes, artifact.session_id)
    session_directory, manifest_path = target_import_paths(artifact, target_home)
    summary_path = session_directory / "summary.json"
    updates_path = session_directory / "updates.jsonl"
    ensure_target_paths_available(session_directory, manifest_path)
    if dry_run:
        return session_directory, manifest_path

    manifest_bytes = (
        json.dumps(artifact.manifest(output_path=session_directory), indent=2, sort_keys=True)
        + "\n"
    ).encode()
    created_directory = False
    identities: list[tuple[Path, tuple[int, int]]] = []
    guards: list[int] = []
    try:
        _mkdir_private_tree(session_directory.parent)
        try:
            session_directory.mkdir(mode=0o700)
            created_directory = True
        except FileExistsError as exc:
            raise JsonlError(
                f"refusing to overwrite existing Grok session: {session_directory}"
            ) from exc
        for path, data in (
            (summary_path, summary_bytes),
            (updates_path, updates_bytes),
            (manifest_path, manifest_bytes),
        ):
            identity = write_private_atomic(path, data)
            identities.append((path, identity))
            guards.append(_open_identity_guard(path, identity))
        if not all(_path_matches_identity(path, identity) for path, identity in identities):
            raise JsonlError("Grok artifact changed during installation")
    except BaseException:
        for path, identity in reversed(identities):
            _unlink_if_identity_matches(path, identity)
        if created_directory:
            with suppress(OSError):
                session_directory.rmdir()
        raise
    finally:
        for descriptor in guards:
            os.close(descriptor)
    return session_directory, manifest_path


def install_openhands_artifact(
    artifact: ConversionArtifact,
    *,
    target_home: Path,
    dry_run: bool = False,
) -> tuple[Path, Path]:
    """Install the canonical OpenHands event log; runtime state is rebuilt on resume."""

    if artifact.target_format != TargetFormat.OPENHANDS:
        raise SessionMigrateError("OpenHands installation requires an OpenHands artifact")
    event_files = openhands.native_files(artifact.native_bytes, artifact.session_id)
    events_path, manifest_path = target_import_paths(artifact, target_home)
    conversation_directory = events_path.parent
    ensure_target_paths_available(conversation_directory, manifest_path)
    if dry_run:
        return events_path, manifest_path

    manifest_bytes = (
        json.dumps(artifact.manifest(output_path=events_path), indent=2, sort_keys=True) + "\n"
    ).encode()
    created_conversation = False
    identities: list[tuple[Path, tuple[int, int]]] = []
    guards: list[int] = []
    try:
        _mkdir_private_tree(conversation_directory.parent)
        try:
            conversation_directory.mkdir(mode=0o700)
            created_conversation = True
        except FileExistsError as exc:
            raise JsonlError(
                f"refusing to overwrite existing OpenHands session: {conversation_directory}"
            ) from exc
        events_path.mkdir(mode=0o700)
        for name, data in event_files:
            path = events_path / name
            identity = write_private_atomic(path, data)
            identities.append((path, identity))
            guards.append(_open_identity_guard(path, identity))
        manifest_identity = write_private_atomic(manifest_path, manifest_bytes)
        identities.append((manifest_path, manifest_identity))
        guards.append(_open_identity_guard(manifest_path, manifest_identity))
        if not all(_path_matches_identity(path, identity) for path, identity in identities):
            raise JsonlError("OpenHands artifact changed during installation")
    except BaseException:
        for path, identity in reversed(identities):
            _unlink_if_identity_matches(path, identity)
        if created_conversation:
            with suppress(OSError):
                events_path.rmdir()
            with suppress(OSError):
                conversation_directory.rmdir()
        raise
    finally:
        for descriptor in guards:
            os.close(descriptor)
    return events_path, manifest_path


def install_opencode_artifact(
    artifact: ConversionArtifact,
    *,
    manifest_path: Path,
    target_cli: Path | None = None,
    dry_run: bool = False,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Preflight and import through OpenCode's public CLI without touching SQLite."""

    if artifact.target_format != TargetFormat.OPENCODE:
        raise SessionMigrateError("official OpenCode import requires an OpenCode artifact")
    opencode.validate_native_bytes(artifact.native_bytes, artifact.session_id)
    values = dict(os.environ if environ is None else environ)
    values.setdefault("OPENCODE_DISABLE_AUTOUPDATE", "true")
    values.setdefault("OPENCODE_DISABLE_PRUNE", "true")
    if artifact.target_cli_version != opencode.PINNED_OPENCODE_VERSION:
        raise SessionMigrateError(
            "automatic OpenCode import requires target metadata version "
            f"{opencode.PINNED_OPENCODE_VERSION}; convert-only artifacts may opt into "
            "unvalidated metadata versions"
        )
    cli = _resolve_opencode_cli(target_cli, values)
    observed_version = _opencode_version(cli, values)
    if observed_version != opencode.PINNED_OPENCODE_VERSION:
        raise SessionMigrateError(
            "OpenCode CLI version mismatch: expected "
            f"{opencode.PINNED_OPENCODE_VERSION}, observed {observed_version}"
        )
    if artifact.session_id in _opencode_session_ids(cli, values):
        raise SessionMigrateError(
            "OpenCode session ID already exists; refusing to overwrite native session: "
            f"{artifact.session_id}"
        )

    manifest_path = _absolute_no_follow(manifest_path)
    ensure_target_paths_available(manifest_path)
    if dry_run:
        return cli

    target_location = f"opencode:{artifact.session_id}"
    manifest_bytes = (
        json.dumps(artifact.manifest(output_path=target_location), indent=2, sort_keys=True) + "\n"
    ).encode()
    reservation_identity: tuple[int, int] | None = None
    reservation_guard: int | None = None
    import_succeeded = False
    try:
        # Reserve the final manifest name before invoking an external importer.
        # A crash may leave a zero-byte reservation, which intentionally blocks
        # a blind retry instead of risking a second native import.
        reservation_identity = write_private_atomic(manifest_path, b"")
        reservation_guard = _open_identity_guard(manifest_path, reservation_identity, writable=True)
        if artifact.session_id in _opencode_session_ids(cli, values):
            raise SessionMigrateError(
                "OpenCode session ID appeared during import preflight; refusing to continue: "
                f"{artifact.session_id}"
            )

        temporary_root = values.get("TMPDIR")
        with tempfile.TemporaryDirectory(
            prefix="session-migrate-opencode-", dir=temporary_root
        ) as directory_name:
            directory = Path(directory_name)
            os.chmod(directory, 0o700)
            bundle_path = directory / "import.json"
            write_private_atomic(bundle_path, artifact.native_bytes)
            _invoke_opencode_import(cli, bundle_path, values)
            # From this point onward, any local cleanup/finalization failure
            # must warn that the native session may already exist.
            import_succeeded = True

        if artifact.session_id not in _opencode_session_ids(cli, values):
            raise SessionMigrateError(
                "OpenCode import returned success but the session was not discoverable afterward"
            )
        _write_reserved_file(
            reservation_guard,
            manifest_path,
            reservation_identity,
            manifest_bytes,
        )
    except BaseException as exc:
        if reservation_identity is not None:
            _unlink_if_identity_matches(manifest_path, reservation_identity)
        if import_succeeded:
            raise SessionMigrateError(
                "OpenCode import succeeded but migrator manifest finalization failed; "
                f"the native session may already exist as {artifact.session_id}"
            ) from exc
        raise
    finally:
        if reservation_guard is not None:
            os.close(reservation_guard)
    return cli


def install_kilo_artifact(
    artifact: ConversionArtifact,
    *,
    manifest_path: Path,
    target_cli: Path | None = None,
    dry_run: bool = False,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Preflight and import through Kilo's public CLI without touching SQLite."""

    if artifact.target_format != TargetFormat.KILO:
        raise SessionMigrateError("official Kilo import requires a Kilo artifact")
    kilo.validate_native_bytes(artifact.native_bytes, artifact.session_id)
    values = dict(os.environ if environ is None else environ)
    values.setdefault("OPENCODE_DISABLE_AUTOUPDATE", "true")
    values.setdefault("OPENCODE_DISABLE_PRUNE", "true")
    if artifact.target_cli_version != kilo.PINNED_KILO_VERSION:
        raise SessionMigrateError(
            "automatic Kilo import requires target metadata version "
            f"{kilo.PINNED_KILO_VERSION}; convert-only artifacts may opt into "
            "unvalidated metadata versions"
        )
    cli = _resolve_kilo_cli(target_cli, values)
    observed_version = _kilo_version(cli, values)
    if observed_version != kilo.PINNED_KILO_VERSION:
        raise SessionMigrateError(
            "Kilo CLI version mismatch: expected "
            f"{kilo.PINNED_KILO_VERSION}, observed {observed_version}"
        )
    if _kilo_session_exists(cli, artifact.session_id, values):
        raise SessionMigrateError(
            "Kilo session ID already exists; refusing to overwrite native session: "
            f"{artifact.session_id}"
        )

    manifest_path = _absolute_no_follow(manifest_path)
    ensure_target_paths_available(manifest_path)
    if dry_run:
        return cli

    target_location = f"kilo:{artifact.session_id}"
    manifest_bytes = (
        json.dumps(artifact.manifest(output_path=target_location), indent=2, sort_keys=True) + "\n"
    ).encode()
    reservation_identity: tuple[int, int] | None = None
    reservation_guard: int | None = None
    import_succeeded = False
    try:
        reservation_identity = write_private_atomic(manifest_path, b"")
        reservation_guard = _open_identity_guard(manifest_path, reservation_identity, writable=True)
        if _kilo_session_exists(cli, artifact.session_id, values):
            raise SessionMigrateError(
                "Kilo session ID appeared during import preflight; refusing to continue: "
                f"{artifact.session_id}"
            )

        temporary_root = values.get("TMPDIR")
        with tempfile.TemporaryDirectory(
            prefix="session-migrate-kilo-", dir=temporary_root
        ) as directory_name:
            directory = Path(directory_name)
            os.chmod(directory, 0o700)
            bundle_path = directory / "import.json"
            write_private_atomic(bundle_path, artifact.native_bytes)
            _invoke_kilo_import(cli, bundle_path, artifact.cwd, values)
            import_succeeded = True

        if not _kilo_session_exists(cli, artifact.session_id, values):
            raise SessionMigrateError(
                "Kilo import returned success but the session was not discoverable afterward"
            )
        _write_reserved_file(
            reservation_guard,
            manifest_path,
            reservation_identity,
            manifest_bytes,
        )
    except BaseException as exc:
        if reservation_identity is not None:
            _unlink_if_identity_matches(manifest_path, reservation_identity)
        if import_succeeded:
            raise SessionMigrateError(
                "Kilo import succeeded but migrator manifest finalization failed; "
                f"the native session may already exist as {artifact.session_id}"
            ) from exc
        raise
    finally:
        if reservation_guard is not None:
            os.close(reservation_guard)
    return cli


def ensure_target_paths_available(*paths: Path) -> None:
    """Fail if a planned conversion would collide, including during dry-run."""

    collisions = [
        _absolute_no_follow(path) for path in paths if os.path.lexists(_absolute_no_follow(path))
    ]
    if collisions:
        joined = ", ".join(str(path) for path in collisions)
        raise JsonlError(f"refusing to overwrite existing target(s): {joined}")


def content_free_result(
    artifact: ConversionArtifact,
    *,
    output_path: Path | str,
    manifest_path: Path,
    dry_run: bool,
) -> dict[str, Any]:
    return {
        "dry_run": dry_run,
        "source_format": artifact.source.source_format.value,
        "target_format": artifact.target_format.value,
        "session_id": artifact.session_id,
        "cwd": str(artifact.cwd),
        "output": (str(output_path.resolve()) if isinstance(output_path, Path) else output_path),
        "manifest": str(manifest_path.resolve()),
        "records": artifact.native_record_count,
        "sha256": artifact.target_sha256,
        "dropped_events": artifact.dropped,
        "warnings": list(artifact.warnings),
    }


def _validated_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise SessionMigrateError(f"session ID is not a valid UUID: {value}") from exc


def _validate_native_bytes(data: bytes, target_format: TargetFormat, session_id: str) -> None:
    if target_format == TargetFormat.GROK:
        grok.validate_native_bytes(data, session_id)
        return
    if target_format == TargetFormat.KILO:
        kilo.validate_native_bytes(data, session_id)
        return
    if target_format == TargetFormat.OPENHANDS:
        openhands.validate_native_bytes(data, session_id)
        return
    if target_format == TargetFormat.MUSE:
        muse.validate_native_bytes(data, session_id)
        return
    if target_format == TargetFormat.QWEN:
        qwen.validate_native_bytes(data, session_id)
        return
    if target_format == TargetFormat.KIMI:
        kimi.validate_native_bytes(data, session_id)
        return
    if target_format == TargetFormat.ANTIGRAVITY:
        antigravity.validate_native_bytes(data, session_id)
        return
    if target_format == TargetFormat.CURSOR:
        cursor.validate_native_bytes(data, session_id)
        return
    if target_format == TargetFormat.VIBE:
        vibe.validate_native_bytes(data, session_id)
        return
    if target_format == TargetFormat.PI:
        pi.validate_native_bytes(data, session_id)
        return
    if target_format == TargetFormat.OMP:
        omp.validate_native_bytes(data, session_id)
        return
    if target_format == TargetFormat.OPENCODE:
        opencode.validate_native_bytes(data, session_id)
        return
    if target_format == TargetFormat.COPILOT:
        copilot.validate_native_bytes(data, session_id)
        return
    try:
        records = [json.loads(line) for line in data.splitlines() if line.strip()]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SessionMigrateError("generated target is not valid JSONL") from exc
    if not records or not all(isinstance(record, dict) for record in records):
        raise SessionMigrateError("generated target has no valid JSON object records")
    first = records[0]
    if target_format == TargetFormat.CODEX:
        payload = first.get("payload")
        if (
            first.get("type") != "session_meta"
            or not isinstance(payload, dict)
            or payload.get("id") != session_id
        ):
            raise SessionMigrateError("generated Codex rollout has invalid canonical metadata")
        if not any(record.get("type") in {"response_item", "compacted"} for record in records[1:]):
            raise SessionMigrateError(
                "generated Codex rollout has no resumable conversation history"
            )
    else:
        conversation = [record for record in records if record.get("type") in {"user", "assistant"}]
        if not conversation or any(
            record.get("sessionId") != session_id for record in conversation
        ):
            raise SessionMigrateError("generated Claude transcript has invalid session linkage")


def _pinned_target_version(target_format: TargetFormat) -> str:
    return {
        TargetFormat.CLAUDE: claude.PINNED_CLAUDE_VERSION,
        TargetFormat.CODEX: codex.PINNED_CODEX_VERSION,
        TargetFormat.PI: pi.PINNED_PI_VERSION,
        TargetFormat.OMP: omp.PINNED_OMP_VERSION,
        TargetFormat.OPENCODE: opencode.PINNED_OPENCODE_VERSION,
        TargetFormat.COPILOT: copilot.PINNED_COPILOT_VERSION,
        TargetFormat.ANTIGRAVITY: antigravity.PINNED_ANTIGRAVITY_VERSION,
        TargetFormat.CURSOR: cursor.PINNED_CURSOR_VERSION,
        TargetFormat.VIBE: vibe.PINNED_VIBE_VERSION,
        TargetFormat.MUSE: muse.PINNED_MUSE_VERSION,
        TargetFormat.QWEN: qwen.PINNED_QWEN_VERSION,
        TargetFormat.KIMI: kimi.PINNED_KIMI_VERSION,
        TargetFormat.GROK: grok.PINNED_GROK_VERSION,
        TargetFormat.KILO: kilo.PINNED_KILO_VERSION,
        TargetFormat.OPENHANDS: openhands.PINNED_OPENHANDS_VERSION,
    }[target_format]


def _native_record_count(data: bytes, target_format: TargetFormat) -> int:
    if target_format == TargetFormat.GROK:
        return grok.native_record_count(data)
    if target_format == TargetFormat.KILO:
        return kilo.native_record_count(data)
    if target_format == TargetFormat.OPENHANDS:
        return openhands.native_record_count(data)
    if target_format == TargetFormat.OMP:
        return omp.native_record_count(data)
    if target_format == TargetFormat.KIMI:
        return kimi.native_record_count(data)
    if target_format == TargetFormat.ANTIGRAVITY:
        return antigravity.native_record_count(data)
    if target_format == TargetFormat.CURSOR:
        return cursor.native_record_count(data)
    if target_format == TargetFormat.VIBE:
        return vibe.native_record_count(data)
    if target_format != TargetFormat.OPENCODE:
        return data.count(b"\n")
    value = json.loads(data)
    messages = value.get("messages", []) if isinstance(value, dict) else []
    return (
        1
        + len(messages)
        + sum(
            len(message.get("parts", []))
            for message in messages
            if isinstance(message, dict) and isinstance(message.get("parts"), list)
        )
    )


def _mkdir_private_tree(path: Path) -> None:
    """Create only missing parent directories with private permissions."""

    missing: list[Path] = []
    cursor = path
    while not os.path.lexists(cursor):
        missing.append(cursor)
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            if not directory.is_dir():
                raise


def _resolve_opencode_cli(target_cli: Path | None, environ: Mapping[str, str]) -> Path:
    candidates: list[Path] = []
    if target_cli is not None:
        candidates.append(_absolute_no_follow(target_cli))
    elif environ.get("OPENCODE_BIN"):
        candidates.append(_absolute_no_follow(Path(environ["OPENCODE_BIN"])))
    else:
        discovered = shutil.which("opencode", path=environ.get("PATH"))
        if discovered:
            candidates.append(_absolute_no_follow(Path(discovered)))
        home = Path(environ.get("HOME", str(Path.home()))).expanduser()
        candidates.append(_absolute_no_follow(home / ".opencode/bin/opencode"))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise SessionMigrateError(
        "OpenCode CLI was not found; pass --target-cli, set OPENCODE_BIN, add opencode "
        "to PATH, or install it at ~/.opencode/bin/opencode"
    )


def _opencode_version(cli: Path, environ: Mapping[str, str]) -> str:
    completed = _run_opencode([str(cli), "--version"], environ)
    version = completed.stdout.strip()
    if not version or "\n" in version:
        raise SessionMigrateError("OpenCode CLI returned an invalid version string")
    return version


def _opencode_session_ids(cli: Path, environ: Mapping[str, str]) -> set[str]:
    completed = _run_opencode([str(cli), "session", "list", "--format", "json", "--pure"], environ)
    if len(completed.stdout.encode()) > 64 * 1024 * 1024:
        raise SessionMigrateError("OpenCode session list exceeded the safety limit")
    # Pinned OpenCode 1.17.20 emits an empty stream, rather than ``[]``, when
    # its freshly initialized store has no sessions.
    if not completed.stdout.strip():
        return set()
    try:
        value = json.loads(completed.stdout, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise SessionMigrateError("OpenCode session list did not return valid JSON") from exc
    if not isinstance(value, list):
        raise SessionMigrateError("OpenCode session list returned an unexpected JSON shape")
    result: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise SessionMigrateError("OpenCode session list contains invalid metadata")
        result.add(item["id"])
    return result


def _invoke_opencode_import(cli: Path, bundle_path: Path, environ: Mapping[str, str]) -> None:
    _run_opencode([str(cli), "import", str(bundle_path), "--pure"], environ)


def _invoke_opencode_export(
    cli: Path,
    session_id: str,
    bundle_path: Path,
    environ: Mapping[str, str],
) -> None:
    """Export to a regular file because the pinned CLI truncates large stdout pipes."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(bundle_path, flags, 0o600)
        completed = subprocess.run(
            [str(cli), "export", session_id, "--pure"],
            env=dict(environ),
            check=False,
            stdout=descriptor,
            stderr=subprocess.PIPE,
            timeout=OPENCODE_EXPORT_TIMEOUT_SECONDS,
        )
        os.fsync(descriptor)
    except (OSError, subprocess.TimeoutExpired) as exc:
        with suppress(OSError):
            bundle_path.unlink()
        raise SessionMigrateError("OpenCode CLI export failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if completed.returncode != 0:
        with suppress(OSError):
            bundle_path.unlink()
        raise SessionMigrateError(
            f"OpenCode CLI export failed with exit status {completed.returncode}"
        )
    try:
        exported_size = bundle_path.stat().st_size
    except OSError as exc:
        raise SessionMigrateError("OpenCode export artifact is unavailable") from exc
    if exported_size == 0 or exported_size > opencode.MAX_NATIVE_BYTES:
        with suppress(OSError):
            bundle_path.unlink()
        raise SessionMigrateError("OpenCode export artifact is empty or exceeds the safety limit")


def _run_opencode(
    command: list[str], environ: Mapping[str, str]
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            env=dict(environ),
            check=False,
            capture_output=True,
            text=True,
            timeout=OPENCODE_COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SessionMigrateError("OpenCode CLI invocation failed") from exc
    if completed.returncode != 0:
        raise SessionMigrateError(
            f"OpenCode CLI command failed with exit status {completed.returncode}"
        )
    return completed


def _resolve_kilo_cli(target_cli: Path | None, environ: Mapping[str, str]) -> Path:
    candidates: list[Path] = []
    if target_cli is not None:
        candidates.append(_absolute_no_follow(target_cli))
    elif environ.get("KILO_BIN"):
        candidates.append(_absolute_no_follow(Path(environ["KILO_BIN"])))
    else:
        discovered = shutil.which("kilo", path=environ.get("PATH"))
        if discovered:
            candidates.append(_absolute_no_follow(Path(discovered)))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise SessionMigrateError(
        "Kilo CLI was not found; pass --target-cli, set KILO_BIN, or add kilo to PATH"
    )


def _kilo_version(cli: Path, environ: Mapping[str, str]) -> str:
    completed = _run_kilo([str(cli), "--version"], environ)
    version = completed.stdout.strip()
    if not version or "\n" in version:
        raise SessionMigrateError("Kilo CLI returned an invalid version string")
    return version


def _kilo_session_exists(cli: Path, session_id: str, environ: Mapping[str, str]) -> bool:
    """Probe one Kilo session without trusting its broken 7.5.0 list command.

    The pinned CLI can import and export sessions correctly, but its JSON list
    command raises while formatting imported rows.  Export is the supported
    per-session API and gives an unambiguous not-found diagnostic.  Discarding
    stdout also avoids materializing transcript bodies during collision checks.
    """

    try:
        completed = subprocess.run(
            [str(cli), "export", session_id, "--pure"],
            env=dict(environ),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=OPENCODE_COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SessionMigrateError("Kilo CLI session probe failed") from exc
    if completed.returncode == 0:
        return True
    if completed.returncode == 1 and f"Session not found: {session_id}" in completed.stderr:
        return False
    raise SessionMigrateError(
        f"Kilo CLI session probe failed with exit status {completed.returncode}"
    )


def _invoke_kilo_import(
    cli: Path,
    bundle_path: Path,
    cwd: Path,
    environ: Mapping[str, str],
) -> None:
    # Kilo 7.5.0 intentionally replaces the bundle's directory with the
    # importer's current instance directory.  Run the official importer from
    # the requested target cwd so the resumed session is attached to the right
    # workspace rather than session-migrate's own process directory.
    _run_kilo([str(cli), "import", str(bundle_path), "--pure"], environ, cwd=cwd)


def _invoke_kilo_export(
    cli: Path,
    session_id: str,
    bundle_path: Path,
    environ: Mapping[str, str],
) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(bundle_path, flags, 0o600)
        completed = subprocess.run(
            [str(cli), "export", session_id, "--pure"],
            env=dict(environ),
            check=False,
            stdout=descriptor,
            stderr=subprocess.PIPE,
            timeout=OPENCODE_EXPORT_TIMEOUT_SECONDS,
        )
        os.fsync(descriptor)
    except (OSError, subprocess.TimeoutExpired) as exc:
        with suppress(OSError):
            bundle_path.unlink()
        raise SessionMigrateError("Kilo CLI export failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if completed.returncode != 0:
        with suppress(OSError):
            bundle_path.unlink()
        raise SessionMigrateError(f"Kilo CLI export failed with exit status {completed.returncode}")
    try:
        exported_size = bundle_path.stat().st_size
    except OSError as exc:
        raise SessionMigrateError("Kilo export artifact is unavailable") from exc
    if exported_size == 0 or exported_size > kilo.MAX_NATIVE_BYTES:
        with suppress(OSError):
            bundle_path.unlink()
        raise SessionMigrateError("Kilo export artifact is empty or exceeds the safety limit")


def _run_kilo(
    command: list[str],
    environ: Mapping[str, str],
    *,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            env=dict(environ),
            check=False,
            capture_output=True,
            text=True,
            timeout=OPENCODE_COMMAND_TIMEOUT_SECONDS,
            cwd=cwd,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SessionMigrateError("Kilo CLI invocation failed") from exc
    if completed.returncode != 0:
        raise SessionMigrateError(
            f"Kilo CLI command failed with exit status {completed.returncode}"
        )
    return completed


def _write_reserved_file(
    descriptor: int,
    path: Path,
    identity: tuple[int, int],
    data: bytes,
) -> None:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.ftruncate(descriptor, 0)
        view = memoryview(data)
        written = 0
        while written < len(view):
            chunk_size = os.write(descriptor, view[written:])
            if chunk_size == 0:
                raise OSError("zero-byte write while finalizing OpenCode manifest")
            written += chunk_size
        os.fsync(descriptor)
    except OSError as exc:
        raise JsonlError(f"cannot finalize OpenCode manifest: {exc.strerror or exc}") from exc
    if not _path_matches_identity(path, identity):
        raise JsonlError("OpenCode manifest changed during import")
    _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _unlink_if_identity_matches(path: Path, identity: tuple[int, int]) -> None:
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == identity:
        path.unlink()


def _open_identity_guard(path: Path, identity: tuple[int, int], *, writable: bool = False) -> int:
    try:
        descriptor = os.open(
            path,
            (os.O_RDWR if writable else os.O_RDONLY)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise JsonlError(
            f"cannot guard target session during install: {exc.strerror or exc}"
        ) from exc
    guarded = os.fstat(descriptor)
    if not stat.S_ISREG(guarded.st_mode) or (guarded.st_dev, guarded.st_ino) != identity:
        os.close(descriptor)
        raise JsonlError("target session changed during installation")
    return descriptor


def _path_matches_identity(path: Path, identity: tuple[int, int]) -> bool:
    try:
        current = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == identity


def _absolute_no_follow(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))
