"""Kilo Code 7.5.0 session import/export adapter.

Kilo exposes a supported ``import``/``export`` JSON contract.  The contract is
compatible with the OpenCode bundle lineage, but Kilo remains a distinct
format: it has its own binary pin, source identity, installation command, and
native store.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from session_migrate.formats import opencode
from session_migrate.jsonl import file_sha256
from session_migrate.model import AgentFormat, Session

PINNED_KILO_VERSION = "7.5.0"
PINNED_KILO_LINUX_X64_BYTES = 145_118_408
PINNED_KILO_LINUX_X64_SHA256 = "ede061eb9178d0158ac66baa81619e2bf66859041d20d0a014798d38ddc7c1ce"
KILO_NATIVE_IMPORT_SUPPORTED = True
MAX_NATIVE_BYTES = opencode.MAX_NATIVE_BYTES

session_id_from_uuid = opencode.session_id_from_uuid


def serialize(
    session: Session,
    *,
    session_id: str,
    cwd: Path,
    cli_version: str = PINNED_KILO_VERSION,
    provider_id: str = "anthropic",
    model_id: str | None = None,
    agent: str = "build",
    timestamp: str | None = None,
    title: str | None = None,
) -> tuple[bytes, dict[str, int]]:
    """Serialize portable history as Kilo's supported import bundle."""

    return opencode.serialize(
        session,
        session_id=session_id,
        cwd=cwd,
        cli_version=cli_version,
        provider_id=provider_id,
        model_id=model_id,
        agent=agent,
        timestamp=timestamp,
        title=title,
    )


def parse(path: Path) -> opencode.ParsedOpenCodeSession:
    """Parse a bundle produced by ``kilo export``."""

    return opencode.parse_import(path)


def parse_session(path: Path) -> Session:
    """Project a Kilo export bundle into the portable event model."""

    parsed = parse(path)
    base = opencode.parse_session(path)
    return replace(
        base,
        source_format=AgentFormat.KILO,
        source_path=path.resolve(),
        source_sha256=file_sha256(path),
        cli_version=parsed.cli_version,
    )


def validate_native_bytes(data: bytes, session_id: str) -> None:
    """Validate Kilo's supported import document without invoking the CLI."""

    opencode.validate_native_bytes(data, session_id)


def native_record_count(data: bytes) -> int:
    """Count the session header, messages, and parts in an import bundle."""

    value = opencode._decode_import_bundle(data)
    messages = value.get("messages", [])
    return 1 + len(messages) + sum(len(item.get("parts", [])) for item in messages)
