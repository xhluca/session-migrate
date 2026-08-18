"""Conversion orchestration, manifests, target paths, and atomic installation."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from session_bridge import __version__
from session_bridge.errors import FormatDetectionError, JsonlError, SessionBridgeError
from session_bridge.formats import claude, codex
from session_bridge.formats.common import valid_rfc3339
from session_bridge.inspection import detect_format
from session_bridge.jsonl import iter_jsonl, write_private_atomic
from session_bridge.model import AgentFormat, Session


@dataclass(frozen=True, slots=True)
class ConversionOptions:
    target_format: AgentFormat
    session_id: str | None = None
    cwd: Path | None = None
    target_cli_version: str | None = None
    model_provider: str = "openai"
    model: str | None = None


@dataclass(frozen=True, slots=True)
class ConversionArtifact:
    source: Session
    target_format: AgentFormat
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

    def manifest(self, *, output_path: Path) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "created_at": _utc_now(),
            "bridge_version": __version__,
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
                "path": str(output_path.resolve()),
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
    source_format = source_format or detect_format([record.value for record in iter_jsonl(path)])
    if source_format == AgentFormat.CLAUDE:
        return claude.parse(path)
    if source_format == AgentFormat.CODEX:
        return codex.parse(path)
    raise FormatDetectionError(f"unsupported source format: {source_format}")


def convert_session(session: Session, options: ConversionOptions) -> ConversionArtifact:
    if session.source_format == options.target_format:
        raise SessionBridgeError(
            f"source is already {options.target_format.value}; choose the opposite target format"
        )
    target_id = _validated_uuid(options.session_id) if options.session_id else str(uuid.uuid4())
    target_cwd = (options.cwd or session.cwd or Path.cwd()).resolve()
    timestamp = valid_rfc3339(session.started_at) or _utc_now()
    warnings: list[dict[str, Any]] = []
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

    if options.target_format == AgentFormat.CLAUDE:
        target_version = options.target_cli_version or claude.PINNED_CLAUDE_VERSION
        native_bytes, dropped = claude.serialize(
            session,
            session_id=target_id,
            cwd=target_cwd,
            cli_version=target_version,
            model=options.model,
            timestamp=timestamp,
        )
    else:
        target_version = options.target_cli_version or codex.PINNED_CODEX_VERSION
        native_bytes, dropped = codex.serialize(
            session,
            session_id=target_id,
            cwd=target_cwd,
            cli_version=target_version,
            model_provider=options.model_provider,
            timestamp=timestamp,
        )
    pinned_target = (
        claude.PINNED_CLAUDE_VERSION
        if options.target_format == AgentFormat.CLAUDE
        else codex.PINNED_CODEX_VERSION
    )
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
        raise SessionBridgeError("conversion produced no native session records")
    _validate_native_bytes(native_bytes, options.target_format, target_id)
    for kind, count in dropped.items():
        warnings.append(
            {
                "code": "dropped_event_kind",
                "event_kind": kind,
                "count": count,
                "message": "target conversion omitted or transformed this source detail",
            }
        )
    if session.cli_version:
        pinned_source = (
            claude.PINNED_CLAUDE_VERSION
            if session.source_format == AgentFormat.CLAUDE
            else codex.PINNED_CODEX_VERSION
        )
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
        target_format=options.target_format,
        session_id=target_id,
        cwd=target_cwd,
        target_cli_version=target_version,
        timestamp=timestamp,
        native_bytes=native_bytes,
        native_record_count=native_bytes.count(b"\n"),
        dropped=dropped,
        warnings=tuple(warnings),
    )


def target_import_paths(artifact: ConversionArtifact, target_home: Path) -> tuple[Path, Path]:
    target_home = target_home.expanduser().resolve()
    if artifact.target_format == AgentFormat.CLAUDE:
        native_path = (
            target_home
            / "projects"
            / claude.project_directory_name(artifact.cwd)
            / f"{artifact.session_id}.jsonl"
        )
    else:
        native_path = target_home / codex.rollout_relative_path(
            artifact.session_id, artifact.timestamp
        )
    manifest_path = target_home / "session-bridge" / "manifests" / (
        f"{artifact.session_id}.json"
    )
    return native_path, manifest_path


def default_target_home(target_format: AgentFormat) -> Path:
    if target_format == AgentFormat.CLAUDE:
        configured = os.environ.get("CLAUDE_CONFIG_DIR")
        return Path(configured).expanduser() if configured else Path.home() / ".claude"
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def write_artifact(
    artifact: ConversionArtifact, *, output_path: Path, manifest_path: Path
) -> None:
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


def ensure_target_paths_available(output_path: Path, manifest_path: Path) -> None:
    """Fail if a planned conversion would collide, including during dry-run."""

    collisions = [
        _absolute_no_follow(path)
        for path in (output_path, manifest_path)
        if os.path.lexists(_absolute_no_follow(path))
    ]
    if collisions:
        joined = ", ".join(str(path) for path in collisions)
        raise JsonlError(f"refusing to overwrite existing target(s): {joined}")


def content_free_result(
    artifact: ConversionArtifact,
    *,
    output_path: Path,
    manifest_path: Path,
    dry_run: bool,
) -> dict[str, Any]:
    return {
        "dry_run": dry_run,
        "source_format": artifact.source.source_format.value,
        "target_format": artifact.target_format.value,
        "session_id": artifact.session_id,
        "cwd": str(artifact.cwd),
        "output": str(output_path.resolve()),
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
        raise SessionBridgeError(f"session ID is not a valid UUID: {value}") from exc


def _validate_native_bytes(data: bytes, target_format: AgentFormat, session_id: str) -> None:
    try:
        records = [json.loads(line) for line in data.splitlines() if line.strip()]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SessionBridgeError("generated target is not valid JSONL") from exc
    if not records or not all(isinstance(record, dict) for record in records):
        raise SessionBridgeError("generated target has no valid JSON object records")
    first = records[0]
    if target_format == AgentFormat.CODEX:
        payload = first.get("payload")
        if (
            first.get("type") != "session_meta"
            or not isinstance(payload, dict)
            or payload.get("id") != session_id
        ):
            raise SessionBridgeError("generated Codex rollout has invalid canonical metadata")
        if not any(
            record.get("type") in {"response_item", "compacted"} for record in records[1:]
        ):
            raise SessionBridgeError(
                "generated Codex rollout has no resumable conversation history"
            )
    else:
        conversation = [
            record for record in records if record.get("type") in {"user", "assistant"}
        ]
        if not conversation or any(
            record.get("sessionId") != session_id for record in conversation
        ):
            raise SessionBridgeError("generated Claude transcript has invalid session linkage")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _unlink_if_identity_matches(path: Path, identity: tuple[int, int]) -> None:
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == identity:
        path.unlink()


def _open_identity_guard(path: Path, identity: tuple[int, int]) -> int:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
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
