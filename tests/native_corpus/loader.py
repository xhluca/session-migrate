"""Strict, dependency-free loader for the test-only native fixture corpus.

This module intentionally does not import :mod:`session_migrate`.  Corpus
validation must remain independent from the production adapters it tests.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA_VERSION = 1
EXPECTED_FORMATS = frozenset(
    {
        "antigravity",
        "claude",
        "codex",
        "copilot",
        "cursor",
        "devin",
        "grok",
        "hermes",
        "kilo",
        "kimi",
        "mastracode",
        "muse",
        "omp",
        "opencode",
        "openhands",
        "pi",
        "qwen",
        "vibe",
    }
)
ALLOWED_MODALITIES = frozenset(
    {
        "audio",
        "compaction",
        "document",
        "readable_reasoning",
        "text",
        "tool_call",
        "tool_result",
        "tool_result_image",
        "user_image",
        "video",
    }
)
PORTABILITY_POLICIES = frozenset({"drop", "lossy", "preserve", "same_format_only", "unsupported"})
ARTIFACT_ROLES = frozenset({"asset", "metadata", "transcript"})
PROVIDER_KINDS = frozenset({"loopback", "openrouter", "vendor"})
MUTATION_CATEGORIES = frozenset({"account", "path", "text", "time", "uuid"})
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_GIT_SHA = re.compile(r"[0-9a-f]{40}\Z")
_MAX_METADATA_BYTES = 4 * 1024 * 1024


class CorpusValidationError(ValueError):
    """Raised when corpus metadata or artifacts violate the fixture contract."""


@dataclass(frozen=True, slots=True)
class ArtifactSpec:
    path: PurePosixPath
    media_type: str
    size: int
    sha256: str
    mode: int
    role: str


@dataclass(frozen=True, slots=True)
class ModalitySpec:
    attempted: bool
    native_accepted: bool
    fixture_present: bool
    portable: str


@dataclass(frozen=True, slots=True)
class Producer:
    name: str
    version: str
    package_or_tag: str
    source_commit: str | None
    platform: str
    binary_sha256: str | None
    binary_size: int | None


@dataclass(frozen=True, slots=True)
class Capture:
    created_by_exact_cli: bool
    capture_kind: str
    scenario_version: int
    provider_kind: str
    model_alias: str
    captured_at: str
    raw_private_sha256: str | None


@dataclass(frozen=True, slots=True)
class Mutation:
    selector: str
    category: str
    count: int


@dataclass(frozen=True, slots=True)
class Sanitization:
    required: bool
    sanitizer: str
    sanitizer_git_sha: str
    mutations: tuple[Mutation, ...]
    secret_scan: str
    content_review: str
    sanitized_artifact_set_sha256: str
    reloaded_by_exact_cli: bool
    reloaded_version: str
    reloaded_binary_sha256: str | None


@dataclass(frozen=True, slots=True)
class Expectations:
    ir_path: PurePosixPath
    ir_sha256: str
    event_counts: Mapping[str, int]
    opaque_loss_reasons: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class CorpusProvenance:
    schema_version: int
    fixture_id: str
    format: str
    case: str
    native_session_id: str
    native_title: str
    native_cwd: str
    producer: Producer
    capture: Capture
    artifacts: tuple[ArtifactSpec, ...]
    modalities: Mapping[str, ModalitySpec]
    sanitization: Sanitization
    expectations: Expectations


@dataclass(frozen=True, slots=True)
class NativeMaterialization:
    root: Path
    artifact_paths: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class NativeFixture:
    format: str
    root: Path
    primary: bool
    provenance: CorpusProvenance
    expected_events: tuple[Mapping[str, Any], ...]

    @property
    def native_entrypoints(self) -> tuple[Path, ...]:
        return tuple(self.root / artifact.path for artifact in self.provenance.artifacts)

    def verify_artifacts(self) -> None:
        """Verify exact file inventory, metadata, hashes, and private modes."""

        declared = {artifact.path.as_posix() for artifact in self.provenance.artifacts}
        if len(declared) != len(self.provenance.artifacts):
            raise CorpusValidationError(f"{self.provenance.fixture_id}: duplicate artifact path")

        native_root = self.root / "native"
        if not native_root.is_dir() or native_root.is_symlink():
            raise CorpusValidationError(
                f"{self.provenance.fixture_id}: native directory is missing or is a symlink"
            )
        observed: set[str] = set()
        for path in native_root.rglob("*"):
            if path.is_symlink():
                raise CorpusValidationError(
                    f"{self.provenance.fixture_id}: native artifact may not be a symlink: {path}"
                )
            if path.is_file():
                observed.add(path.relative_to(self.root).as_posix())
        if observed != declared:
            unlisted = sorted(observed - declared)
            missing = sorted(declared - observed)
            raise CorpusValidationError(
                f"{self.provenance.fixture_id}: native artifact inventory mismatch; "
                f"unlisted={unlisted}, missing={missing}"
            )

        for artifact in self.provenance.artifacts:
            path = self.root / artifact.path
            if not path.is_file() or path.is_symlink():
                raise CorpusValidationError(
                    f"{self.provenance.fixture_id}: artifact is not a regular file: {artifact.path}"
                )
            size = path.stat().st_size
            if size != artifact.size:
                raise CorpusValidationError(
                    f"{self.provenance.fixture_id}: size mismatch for {artifact.path}: "
                    f"expected {artifact.size}, got {size}"
                )
            digest = _file_sha256(path)
            if digest != artifact.sha256:
                raise CorpusValidationError(
                    f"{self.provenance.fixture_id}: SHA-256 mismatch for {artifact.path}"
                )
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode != artifact.mode:
                raise CorpusValidationError(
                    f"{self.provenance.fixture_id}: mode mismatch for {artifact.path}: "
                    f"expected {artifact.mode:04o}, got {mode:04o}"
                )

        actual_set_digest = artifact_set_sha256(self.provenance.artifacts)
        expected_set_digest = self.provenance.sanitization.sanitized_artifact_set_sha256
        if actual_set_digest != expected_set_digest:
            raise CorpusValidationError(
                f"{self.provenance.fixture_id}: sanitized artifact-set SHA-256 mismatch"
            )

        expected_ir = self.root / self.provenance.expectations.ir_path
        if not expected_ir.is_file() or expected_ir.is_symlink():
            raise CorpusValidationError(
                f"{self.provenance.fixture_id}: expected IR is missing or is a symlink"
            )
        if _file_sha256(expected_ir) != self.provenance.expectations.ir_sha256:
            raise CorpusValidationError(
                f"{self.provenance.fixture_id}: expected IR SHA-256 mismatch"
            )

    def expected_signature(self) -> tuple[Mapping[str, Any], ...]:
        return self.expected_events

    def materialize(self, destination: Path) -> NativeMaterialization:
        """Copy the verified native artifact set to a new private directory."""

        self.verify_artifacts()
        if destination.exists():
            raise CorpusValidationError(
                f"materialization destination already exists: {destination}"
            )
        destination.mkdir(parents=True, mode=0o700)
        os.chmod(destination, 0o700)
        copied: list[Path] = []
        for artifact in self.provenance.artifacts:
            source = self.root / artifact.path
            target = destination / artifact.path
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(target.parent, 0o700)
            shutil.copyfile(source, target)
            os.chmod(target, artifact.mode)
            copied.append(target)
        return NativeMaterialization(destination, tuple(copied))


@dataclass(frozen=True, slots=True)
class NativeCorpus:
    root: Path
    scenario_path: Path
    fixtures: tuple[NativeFixture, ...]

    def formats(self) -> frozenset[str]:
        return frozenset(fixture.format for fixture in self.fixtures)

    def primary(self, format_name: str) -> NativeFixture:
        matches = [
            fixture
            for fixture in self.fixtures
            if fixture.format == format_name and fixture.primary
        ]
        if len(matches) != 1:
            raise CorpusValidationError(
                f"expected exactly one primary fixture for {format_name}, found {len(matches)}"
            )
        return matches[0]

    def verify_artifacts(self) -> None:
        for fixture in self.fixtures:
            fixture.verify_artifacts()


def artifact_set_sha256(artifacts: Iterable[ArtifactSpec]) -> str:
    """Hash an ordered, canonical description of a complete artifact set."""

    digest = hashlib.sha256()
    for artifact in sorted(artifacts, key=lambda item: item.path.as_posix()):
        line = (
            f"{artifact.path.as_posix()}\0{artifact.sha256}\0{artifact.size}\0"
            f"{artifact.mode:04o}\0{artifact.role}\0{artifact.media_type}\n"
        )
        digest.update(line.encode())
    return digest.hexdigest()


def load_corpus(root: Path) -> NativeCorpus:
    """Load and fully validate corpus metadata and all declared artifacts."""

    root = root.resolve()
    document = _as_object(_read_json(root / "corpus.json"), "corpus.json")
    _exact_keys(
        document,
        required={"schema_version", "scenario", "required_formats", "sources"},
        label="corpus.json",
    )
    _schema_version(document["schema_version"], "corpus.json.schema_version")
    scenario_relative = _safe_relative_path(document["scenario"], "corpus.json.scenario")
    scenario_path = root / scenario_relative
    _as_object(_read_json(scenario_path), scenario_relative.as_posix())

    required = _string_list(document["required_formats"], "corpus.json.required_formats")
    if len(required) != len(set(required)) or set(required) != EXPECTED_FORMATS:
        raise CorpusValidationError(
            "corpus.json.required_formats must contain each supported format exactly once"
        )

    sources = _as_list(document["sources"], "corpus.json.sources")
    fixtures: list[NativeFixture] = []
    fixture_dirs: set[PurePosixPath] = set()
    for index, value in enumerate(sources):
        label = f"corpus.json.sources[{index}]"
        source = _as_object(value, label)
        _exact_keys(source, required={"format", "fixture_dir", "primary"}, label=label)
        format_name = _nonempty_string(source["format"], f"{label}.format")
        if format_name not in EXPECTED_FORMATS:
            raise CorpusValidationError(f"{label}.format is unsupported: {format_name}")
        primary = _boolean(source["primary"], f"{label}.primary")
        fixture_dir = _safe_relative_path(source["fixture_dir"], f"{label}.fixture_dir")
        if fixture_dir in fixture_dirs:
            raise CorpusValidationError(f"duplicate fixture directory: {fixture_dir}")
        fixture_dirs.add(fixture_dir)
        fixture_root = root / fixture_dir
        if not fixture_root.is_dir() or fixture_root.is_symlink():
            raise CorpusValidationError(f"fixture directory is missing or a symlink: {fixture_dir}")
        provenance = _load_provenance(fixture_root / "provenance.json")
        if provenance.format != format_name:
            raise CorpusValidationError(
                f"{provenance.fixture_id}: source format {format_name} does not match provenance "
                f"format {provenance.format}"
            )
        expected_events = _read_jsonl(fixture_root / provenance.expectations.ir_path)
        fixtures.append(
            NativeFixture(
                format=format_name,
                root=fixture_root,
                primary=primary,
                provenance=provenance,
                expected_events=expected_events,
            )
        )

    fixture_ids = [fixture.provenance.fixture_id for fixture in fixtures]
    if len(fixture_ids) != len(set(fixture_ids)):
        raise CorpusValidationError("fixture IDs must be unique")
    native_ids = [fixture.provenance.native_session_id for fixture in fixtures]
    if len(native_ids) != len(set(native_ids)):
        raise CorpusValidationError("native session IDs must be unique")
    counts = Counter(fixture.format for fixture in fixtures if fixture.primary)
    if set(counts) != EXPECTED_FORMATS or any(count != 1 for count in counts.values()):
        raise CorpusValidationError("every supported format must have exactly one primary fixture")
    if frozenset(fixture.format for fixture in fixtures) != EXPECTED_FORMATS:
        raise CorpusValidationError("corpus source formats do not exactly cover supported formats")

    corpus = NativeCorpus(root=root, scenario_path=scenario_path, fixtures=tuple(fixtures))
    corpus.verify_artifacts()
    return corpus


def _load_provenance(path: Path) -> CorpusProvenance:
    value = _as_object(_read_json(path), str(path))
    _exact_keys(
        value,
        required={
            "artifacts",
            "capture",
            "case",
            "expectations",
            "fixture_id",
            "format",
            "modalities",
            "native_cwd",
            "native_session_id",
            "native_title",
            "producer",
            "sanitization",
            "schema_version",
        },
        label=str(path),
    )
    _schema_version(value["schema_version"], f"{path}.schema_version")
    fixture_id = _nonempty_string(value["fixture_id"], f"{path}.fixture_id")
    format_name = _nonempty_string(value["format"], f"{path}.format")
    if format_name not in EXPECTED_FORMATS:
        raise CorpusValidationError(f"{fixture_id}: unsupported provenance format {format_name}")
    artifacts = _load_artifacts(value["artifacts"], fixture_id)
    modalities = _load_modalities(value["modalities"], fixture_id)
    return CorpusProvenance(
        schema_version=SCHEMA_VERSION,
        fixture_id=fixture_id,
        format=format_name,
        case=_nonempty_string(value["case"], f"{fixture_id}.case"),
        native_session_id=_nonempty_string(
            value["native_session_id"], f"{fixture_id}.native_session_id"
        ),
        native_title=_nonempty_string(value["native_title"], f"{fixture_id}.native_title"),
        native_cwd=_nonempty_string(value["native_cwd"], f"{fixture_id}.native_cwd"),
        producer=_load_producer(value["producer"], fixture_id),
        capture=_load_capture(value["capture"], fixture_id),
        artifacts=artifacts,
        modalities=modalities,
        sanitization=_load_sanitization(value["sanitization"], fixture_id),
        expectations=_load_expectations(value["expectations"], fixture_id),
    )


def _load_artifacts(value: Any, fixture_id: str) -> tuple[ArtifactSpec, ...]:
    result: list[ArtifactSpec] = []
    for index, item in enumerate(_as_list(value, f"{fixture_id}.artifacts")):
        label = f"{fixture_id}.artifacts[{index}]"
        document = _as_object(item, label)
        _exact_keys(
            document,
            required={"media_type", "mode", "path", "role", "sha256", "size"},
            label=label,
        )
        path = _safe_relative_path(document["path"], f"{label}.path")
        if not path.parts or path.parts[0] != "native":
            raise CorpusValidationError(f"{label}.path must be below native/")
        mode_text = _nonempty_string(document["mode"], f"{label}.mode")
        if mode_text != "0600":
            raise CorpusValidationError(f"{label}.mode must be 0600")
        role = _nonempty_string(document["role"], f"{label}.role")
        if role not in ARTIFACT_ROLES:
            raise CorpusValidationError(f"{label}.role is unsupported: {role}")
        result.append(
            ArtifactSpec(
                path=path,
                media_type=_nonempty_string(document["media_type"], f"{label}.media_type"),
                size=_nonnegative_int(document["size"], f"{label}.size"),
                sha256=_hash(document["sha256"], f"{label}.sha256"),
                mode=0o600,
                role=role,
            )
        )
    if not result:
        raise CorpusValidationError(f"{fixture_id}.artifacts must not be empty")
    return tuple(result)


def _load_modalities(value: Any, fixture_id: str) -> Mapping[str, ModalitySpec]:
    document = _as_object(value, f"{fixture_id}.modalities")
    unknown = set(document) - ALLOWED_MODALITIES
    if unknown:
        raise CorpusValidationError(f"{fixture_id}: unsupported modalities: {sorted(unknown)}")
    if "text" not in document:
        raise CorpusValidationError(f"{fixture_id}: text modality is required")
    result: dict[str, ModalitySpec] = {}
    for name, item in document.items():
        label = f"{fixture_id}.modalities.{name}"
        modality = _as_object(item, label)
        _exact_keys(
            modality,
            required={"attempted", "fixture_present", "native_accepted", "portable"},
            label=label,
        )
        attempted = _boolean(modality["attempted"], f"{label}.attempted")
        accepted = _boolean(modality["native_accepted"], f"{label}.native_accepted")
        present = _boolean(modality["fixture_present"], f"{label}.fixture_present")
        portable = _nonempty_string(modality["portable"], f"{label}.portable")
        if portable not in PORTABILITY_POLICIES:
            raise CorpusValidationError(f"{label}.portable is unsupported: {portable}")
        if accepted and not attempted:
            raise CorpusValidationError(
                f"{label}: native acceptance requires an attempted modality"
            )
        if present and not accepted:
            raise CorpusValidationError(f"{label}: fixture presence requires native acceptance")
        result[name] = ModalitySpec(attempted, accepted, present, portable)
    return result


def _load_producer(value: Any, fixture_id: str) -> Producer:
    label = f"{fixture_id}.producer"
    document = _as_object(value, label)
    _exact_keys(
        document,
        required={
            "binary_sha256",
            "binary_size",
            "name",
            "package_or_tag",
            "platform",
            "source_commit",
            "version",
        },
        label=label,
    )
    source_commit = document["source_commit"]
    if source_commit is not None:
        source_commit = _hash40(source_commit, f"{label}.source_commit")
    binary_sha256 = document["binary_sha256"]
    if binary_sha256 is not None:
        binary_sha256 = _hash(binary_sha256, f"{label}.binary_sha256")
    binary_size = document["binary_size"]
    if binary_size is not None:
        binary_size = _nonnegative_int(binary_size, f"{label}.binary_size")
    return Producer(
        name=_nonempty_string(document["name"], f"{label}.name"),
        version=_nonempty_string(document["version"], f"{label}.version"),
        package_or_tag=_nonempty_string(document["package_or_tag"], f"{label}.package_or_tag"),
        source_commit=source_commit,
        platform=_nonempty_string(document["platform"], f"{label}.platform"),
        binary_sha256=binary_sha256,
        binary_size=binary_size,
    )


def _load_capture(value: Any, fixture_id: str) -> Capture:
    label = f"{fixture_id}.capture"
    document = _as_object(value, label)
    _exact_keys(
        document,
        required={
            "capture_kind",
            "captured_at",
            "created_by_exact_cli",
            "model_alias",
            "provider_kind",
            "scenario_version",
        },
        optional={"raw_private_sha256"},
        label=label,
    )
    if not _boolean(document["created_by_exact_cli"], f"{label}.created_by_exact_cli"):
        raise CorpusValidationError(f"{label}.created_by_exact_cli must be true")
    capture_kind = _nonempty_string(document["capture_kind"], f"{label}.capture_kind")
    if capture_kind != "actual_cli_trajectory":
        raise CorpusValidationError(f"{label}.capture_kind must be actual_cli_trajectory")
    provider = _nonempty_string(document["provider_kind"], f"{label}.provider_kind")
    if provider not in PROVIDER_KINDS:
        raise CorpusValidationError(f"{label}.provider_kind is unsupported: {provider}")
    raw_digest = document.get("raw_private_sha256")
    if raw_digest is not None:
        raw_digest = _hash(raw_digest, f"{label}.raw_private_sha256")
    return Capture(
        created_by_exact_cli=True,
        capture_kind=capture_kind,
        scenario_version=_positive_int(document["scenario_version"], f"{label}.scenario_version"),
        provider_kind=provider,
        model_alias=_nonempty_string(document["model_alias"], f"{label}.model_alias"),
        captured_at=_nonempty_string(document["captured_at"], f"{label}.captured_at"),
        raw_private_sha256=raw_digest,
    )


def _load_sanitization(value: Any, fixture_id: str) -> Sanitization:
    label = f"{fixture_id}.sanitization"
    document = _as_object(value, label)
    _exact_keys(
        document,
        required={
            "content_review",
            "mutations",
            "reloaded_binary_sha256",
            "reloaded_by_exact_cli",
            "reloaded_version",
            "required",
            "sanitized_artifact_set_sha256",
            "sanitizer",
            "sanitizer_git_sha",
            "secret_scan",
        },
        label=label,
    )
    mutations: list[Mutation] = []
    for index, item in enumerate(_as_list(document["mutations"], f"{label}.mutations")):
        mutation_label = f"{label}.mutations[{index}]"
        mutation = _as_object(item, mutation_label)
        _exact_keys(
            mutation,
            required={"category", "count", "selector"},
            label=mutation_label,
        )
        category = _nonempty_string(mutation["category"], f"{mutation_label}.category")
        if category not in MUTATION_CATEGORIES:
            raise CorpusValidationError(f"{mutation_label}.category is unsupported: {category}")
        mutations.append(
            Mutation(
                selector=_nonempty_string(mutation["selector"], f"{mutation_label}.selector"),
                category=category,
                count=_positive_int(mutation["count"], f"{mutation_label}.count"),
            )
        )
    for field in ("secret_scan", "content_review"):
        if document[field] != "pass":
            raise CorpusValidationError(f"{label}.{field} must be pass")
    if not _boolean(document["reloaded_by_exact_cli"], f"{label}.reloaded_by_exact_cli"):
        raise CorpusValidationError(f"{label}.reloaded_by_exact_cli must be true")
    reload_digest = document["reloaded_binary_sha256"]
    if reload_digest is not None:
        reload_digest = _hash(reload_digest, f"{label}.reloaded_binary_sha256")
    return Sanitization(
        required=_boolean(document["required"], f"{label}.required"),
        sanitizer=_nonempty_string(document["sanitizer"], f"{label}.sanitizer"),
        sanitizer_git_sha=_hash40(document["sanitizer_git_sha"], f"{label}.sanitizer_git_sha"),
        mutations=tuple(mutations),
        secret_scan="pass",
        content_review="pass",
        sanitized_artifact_set_sha256=_hash(
            document["sanitized_artifact_set_sha256"],
            f"{label}.sanitized_artifact_set_sha256",
        ),
        reloaded_by_exact_cli=True,
        reloaded_version=_nonempty_string(
            document["reloaded_version"], f"{label}.reloaded_version"
        ),
        reloaded_binary_sha256=reload_digest,
    )


def _load_expectations(value: Any, fixture_id: str) -> Expectations:
    label = f"{fixture_id}.expectations"
    document = _as_object(value, label)
    _exact_keys(
        document,
        required={"event_counts", "ir", "ir_sha256", "opaque_loss_reasons"},
        label=label,
    )
    return Expectations(
        ir_path=_safe_relative_path(document["ir"], f"{label}.ir"),
        ir_sha256=_hash(document["ir_sha256"], f"{label}.ir_sha256"),
        event_counts=_counter(document["event_counts"], f"{label}.event_counts"),
        opaque_loss_reasons=_counter(
            document["opaque_loss_reasons"], f"{label}.opaque_loss_reasons"
        ),
    )


def _counter(value: Any, label: str) -> Mapping[str, int]:
    document = _as_object(value, label)
    result: dict[str, int] = {}
    for key, count in document.items():
        if not isinstance(key, str) or not key:
            raise CorpusValidationError(f"{label} contains an empty/non-string key")
        result[key] = _nonnegative_int(count, f"{label}.{key}")
    return result


def _read_json(path: Path) -> Any:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise CorpusValidationError(f"cannot read required metadata file {path}: {exc}") from exc
    if size > _MAX_METADATA_BYTES:
        raise CorpusValidationError(f"metadata file exceeds {_MAX_METADATA_BYTES} bytes: {path}")
    try:
        return json.loads(path.read_text(), object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CorpusValidationError(f"invalid JSON metadata file {path}: {exc}") from exc


def _read_jsonl(path: Path) -> tuple[Mapping[str, Any], ...]:
    try:
        if path.stat().st_size > _MAX_METADATA_BYTES:
            raise CorpusValidationError(f"expected IR exceeds {_MAX_METADATA_BYTES} bytes: {path}")
        lines = path.read_text().splitlines()
    except OSError as exc:
        raise CorpusValidationError(f"cannot read expected IR {path}: {exc}") from exc
    result: list[Mapping[str, Any]] = []
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            raise CorpusValidationError(f"blank expected-IR line at {path}:{index}")
        try:
            value = json.loads(line, object_pairs_hook=_unique_object)
        except json.JSONDecodeError as exc:
            raise CorpusValidationError(f"invalid expected IR at {path}:{index}: {exc}") from exc
        result.append(_as_object(value, f"{path}:{index}"))
    if not result:
        raise CorpusValidationError(f"expected IR must not be empty: {path}")
    return tuple(result)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CorpusValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _exact_keys(
    value: Mapping[str, Any],
    *,
    required: set[str],
    label: str,
    optional: set[str] | None = None,
) -> None:
    optional = optional or set()
    missing = required - set(value)
    unknown = set(value) - required - optional
    if missing or unknown:
        raise CorpusValidationError(
            f"{label} fields mismatch; missing={sorted(missing)}, unknown={sorted(unknown)}"
        )


def _safe_relative_path(value: Any, label: str) -> PurePosixPath:
    text = _nonempty_string(value, label)
    path = PurePosixPath(text)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise CorpusValidationError(f"{label} must be a normalized relative POSIX path")
    if path.as_posix() != text:
        raise CorpusValidationError(f"{label} must be a normalized relative POSIX path")
    return path


def _schema_version(value: Any, label: str) -> None:
    if _positive_int(value, label) != SCHEMA_VERSION:
        raise CorpusValidationError(f"{label} must be {SCHEMA_VERSION}")


def _as_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CorpusValidationError(f"{label} must be an object")
    return value


def _as_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise CorpusValidationError(f"{label} must be an array")
    return value


def _string_list(value: Any, label: str) -> list[str]:
    return [
        _nonempty_string(item, f"{label}[{index}]")
        for index, item in enumerate(_as_list(value, label))
    ]


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise CorpusValidationError(f"{label} must be a non-empty string")
    return value


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise CorpusValidationError(f"{label} must be a boolean")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CorpusValidationError(f"{label} must be a non-negative integer")
    return value


def _positive_int(value: Any, label: str) -> int:
    result = _nonnegative_int(value, label)
    if result == 0:
        raise CorpusValidationError(f"{label} must be positive")
    return result


def _hash(value: Any, label: str) -> str:
    text = _nonempty_string(value, label)
    if not _SHA256.fullmatch(text):
        raise CorpusValidationError(f"{label} must be a lowercase SHA-256 digest")
    return text


def _hash40(value: Any, label: str) -> str:
    text = _nonempty_string(value, label)
    if not _GIT_SHA.fullmatch(text):
        raise CorpusValidationError(f"{label} must be a lowercase 40-character Git SHA")
    return text


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
