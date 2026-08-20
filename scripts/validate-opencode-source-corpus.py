#!/usr/bin/env python3
"""Content-safe validation over stratified official OpenCode exports.

The script reads only non-content store metadata while selecting cases. It
exports selected sessions through OpenCode's public CLI into a mode-0700
temporary workspace, validates every source/target route with the independent
corpus oracle, rewrites the same real histories through an older pinned
OpenCode release, and securely removes the temporary workspace on exit.

Only aggregate counts are printed. Session IDs, paths, titles, conversation
values, tool values, timestamps, hashes, and CWDs never appear in output.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from session_migrate.conversion import ConversionOptions, convert_session
from session_migrate.formats import opencode
from session_migrate.model import EventKind, TargetFormat


@dataclass(frozen=True, slots=True)
class Candidate:
    session_id: str
    time_stratum: int
    has_parent: bool
    message_count: int
    part_count: int
    has_tools: bool
    has_errors: bool


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--opencode-1-17-bin", type=Path, required=True)
    parser.add_argument("--opencode-1-2-bin", type=Path, required=True)
    parser.add_argument("--pi-bin", type=Path)
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--candidate-count", type=int, default=600)
    parser.add_argument("--manual-count", type=int, default=20)
    parser.add_argument("--native-count", type=int, default=10)
    parser.add_argument(
        "--target",
        action="append",
        choices=(
            "claude",
            "codex",
            "pi",
            "opencode",
            "copilot",
            "antigravity",
            "cursor",
        ),
        help="validate only this target; repeat for more than one",
    )
    parser.add_argument("--export-timeout", type=int, default=120)
    parser.add_argument("--progress-every", type=int, default=10)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.count < 1 or args.candidate_count < args.count:
        raise RuntimeError("candidate count must be at least the positive sample count")
    _require_version(args.opencode_1_17_bin, "1.17.20", pure=True)
    _require_version(args.opencode_1_2_bin, "1.2.27", pure=False)
    database = args.database.resolve()
    candidates, inventory_count = _profile_candidates(database, args.candidate_count)
    selected = _select(candidates, args.count)
    if len(selected) != args.count:
        raise RuntimeError("the store did not contain enough nonempty sessions")

    removed_root: Path | None = None
    with tempfile.TemporaryDirectory(prefix="session-migrate-opencode-source-corpus-") as name:
        root = Path(name)
        removed_root = root
        os.chmod(root, 0o700)
        exports_117 = root / "exports-1.17.20"
        exports_127 = root / "exports-1.2.27"
        work = root / "work"
        for directory in (exports_117, exports_127, work):
            directory.mkdir(mode=0o700)

        live_environment = dict(os.environ)
        live_environment["OPENCODE_DISABLE_AUTOUPDATE"] = "true"
        live_environment["OPENCODE_DISABLE_PRUNE"] = "true"
        _official_exports(
            args.opencode_1_17_bin,
            selected,
            exports_117,
            work,
            live_environment,
            timeout=args.export_timeout,
            pure=True,
            progress_every=args.progress_every,
        )
        selection_features = _verify_export_features(exports_117, selected)

        matrix_117 = _run_matrix(
            exports_117,
            root / "manual-1.17.20.txt",
            manual_count=args.manual_count,
            native_count=args.native_count,
            opencode_bin=args.opencode_1_17_bin,
            pi_bin=args.pi_bin,
            targets=tuple(args.target or ()),
        )
        manual_117 = _verify_manual_report(root / "manual-1.17.20.txt")

        old_environment = _isolated_opencode_environment(root / "old-home")
        _rewrite_through_old_release(
            selected,
            exports_117,
            exports_127,
            work,
            args.opencode_1_2_bin,
            old_environment,
            timeout=args.export_timeout,
            progress_every=args.progress_every,
        )
        matrix_127 = _run_matrix(
            exports_127,
            root / "manual-1.2.27.txt",
            manual_count=args.manual_count,
            native_count=0,
            opencode_bin=args.opencode_1_17_bin,
            pi_bin=None,
            targets=tuple(args.target or ()),
        )
        manual_127 = _verify_manual_report(root / "manual-1.2.27.txt")

    if removed_root is None or removed_root.exists():
        raise RuntimeError("private validation workspace was not removed")

    print(
        json.dumps(
            {
                "inventory_sessions": inventory_count,
                "candidate_sessions_profiled": len(candidates),
                "selected_sessions": len(selected),
                "selection_feature_counts": selection_features,
                "official_exports": {
                    "1.17.20": len(selected),
                    "1.2.27": len(selected),
                },
                "target_matrix": {
                    "1.17.20": _matrix_summary(matrix_117),
                    "1.2.27": _matrix_summary(matrix_127),
                },
                "manual_reports": {
                    "1.17.20": manual_117,
                    "1.2.27": manual_127,
                },
                "older_release_transformation": (
                    "portable_same_format_rewrite_before_official_import"
                ),
                "private_workspace_removed": True,
                "content_or_identifiers_printed": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _profile_candidates(database: Path, requested: int) -> tuple[list[Candidate], int]:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=1)
    try:
        rows = connection.execute(
            "SELECT id, parent_id, time_created FROM session ORDER BY time_created, id"
        ).fetchall()
        if not rows:
            return [], 0
        evenly_spaced = {
            round(index * (len(rows) - 1) / (requested - 1)) for index in range(requested)
        }
        parent_indexes = {
            index for index, row in enumerate(rows) if isinstance(row[1], str) and row[1]
        }
        indexes = sorted(evenly_spaced | parent_indexes)
        candidates: list[Candidate] = []
        for index in indexes:
            session_id, parent_id, _created = rows[index]
            message_count = connection.execute(
                "SELECT count(*) FROM message WHERE session_id = ?", (session_id,)
            ).fetchone()[0]
            part_count, tool_count, error_count = connection.execute(
                "SELECT count(*), "
                "sum(json_extract(data, '$.type') = 'tool'), "
                "sum(json_extract(data, '$.type') = 'tool' "
                "AND json_extract(data, '$.state.status') = 'error') "
                "FROM part WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            candidates.append(
                Candidate(
                    session_id=session_id,
                    time_stratum=min(9, index * 10 // len(rows)),
                    has_parent=bool(parent_id),
                    message_count=message_count,
                    part_count=part_count,
                    has_tools=bool(tool_count),
                    has_errors=bool(error_count),
                )
            )
        return candidates, len(rows)
    finally:
        connection.close()


def _select(candidates: list[Candidate], count: int) -> list[Candidate]:
    selected: list[Candidate] = []

    def add(values: list[Candidate], limit: int) -> None:
        for item in values:
            if item.message_count and item not in selected:
                selected.append(item)
                if len(selected) >= limit or len(selected) >= count:
                    return

    add([item for item in candidates if item.has_errors], min(count, 10))
    add([item for item in candidates if item.has_parent], min(count, len(selected) + 20))
    add(
        sorted(candidates, key=lambda item: (-item.part_count, item.time_stratum)),
        min(count, len(selected) + 10),
    )
    add([item for item in candidates if item.has_tools], min(count, len(selected) + 20))
    for stratum in range(10):
        add(
            [item for item in candidates if item.time_stratum == stratum],
            min(count, len(selected) + max(1, (count - len(selected)) // (10 - stratum))),
        )
    add(candidates, count)
    return selected[:count]


def _official_exports(
    binary: Path,
    selected: list[Candidate],
    output: Path,
    work: Path,
    environment: dict[str, str],
    *,
    timeout: int,
    pure: bool,
    progress_every: int,
) -> None:
    for index, item in enumerate(selected, start=1):
        path = output / f"session-{index:04d}.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        arguments = [str(binary), "export", item.session_id]
        if pure:
            arguments.append("--pure")
        with os.fdopen(descriptor, "wb") as stream:
            completed = subprocess.run(
                arguments,
                cwd=work,
                env=environment,
                check=False,
                stdout=stream,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
        if completed.returncode != 0:
            raise RuntimeError(
                f"official export failed at anonymous session {index}; "
                f"stderr_bytes={len(completed.stderr)}"
            )
        if progress_every > 0 and index % progress_every == 0:
            print(
                f"officially exported {index}/{len(selected)} anonymous sessions", file=sys.stderr
            )


def _verify_export_features(export_root: Path, selected: list[Candidate]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for index, candidate in enumerate(selected, start=1):
        path = export_root / f"session-{index:04d}.json"
        session = opencode.parse_session(path)
        counts[f"time_stratum_{candidate.time_stratum}"] += 1
        counts["parent"] += candidate.has_parent
        counts["tools"] += any(event.kind == EventKind.TOOL_CALL for event in session.events)
        counts["errors"] += candidate.has_errors
        size = path.stat().st_size
        counts["size_lt_10k"] += size < 10_000
        counts["size_10k_1m"] += 10_000 <= size < 1_000_000
        counts["size_ge_1m"] += size >= 1_000_000
    return dict(sorted(counts.items()))


def _rewrite_through_old_release(
    selected: list[Candidate],
    source_root: Path,
    output_root: Path,
    work: Path,
    binary: Path,
    environment: dict[str, str],
    *,
    timeout: int,
    progress_every: int,
) -> None:
    oracle = _load_oracle()
    for index, _candidate in enumerate(selected, start=1):
        source = opencode.parse_session(source_root / f"session-{index:04d}.json")
        artifact = convert_session(
            source,
            ConversionOptions(
                target_format=TargetFormat.OPENCODE,
                session_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"opencode-1.2.27-{index}")),
                cwd=work,
                target_cli_version="1.2.27",
            ),
        )
        bundle = output_root / f"bundle-{index:04d}.json"
        _write_private(bundle, artifact.native_bytes)
        imported = subprocess.run(
            [str(binary), "import", str(bundle)],
            cwd=work,
            env=environment,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        bundle.unlink()
        if imported.returncode != 0:
            raise RuntimeError(
                f"OpenCode 1.2.27 import failed at anonymous session {index}; "
                f"stderr_bytes={len(imported.stderr)}"
            )
        exported_path = output_root / f"session-{index:04d}.json"
        descriptor = os.open(exported_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            exported = subprocess.run(
                [str(binary), "export", artifact.session_id],
                cwd=work,
                env=environment,
                check=False,
                stdout=stream,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
        if exported.returncode != 0:
            raise RuntimeError(
                f"OpenCode 1.2.27 export failed at anonymous session {index}; "
                f"stderr_bytes={len(exported.stderr)}"
            )
        old_session = opencode.parse_session(exported_path)
        oracle.assert_projection_equal(
            index,
            "opencode-1.2.27-official-export",
            oracle.project(source.events, source=True, target=TargetFormat.OPENCODE),
            oracle.project(old_session.events, source=False),
        )
        if progress_every > 0 and index % progress_every == 0:
            print(
                f"rewrote {index}/{len(selected)} anonymous sessions through 1.2.27",
                file=sys.stderr,
            )


def _run_matrix(
    source_root: Path,
    manual_report: Path,
    *,
    manual_count: int,
    native_count: int,
    opencode_bin: Path,
    pi_bin: Path | None,
    targets: tuple[str, ...],
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(Path(__file__).with_name("validate-additional-target-corpus.py")),
        "--opencode-export-root",
        str(source_root),
        "--include-same-format",
        "--manual-report",
        str(manual_report),
        "--manual-count",
        str(manual_count),
        "--native-count",
        str(native_count),
        "--progress-every",
        "10",
    ]
    if native_count:
        command.extend(("--native-opencode-bin", str(opencode_bin)))
        if pi_bin is not None:
            command.extend(("--native-pi-bin", str(pi_bin)))
    for target in targets:
        command.extend(("--target", target))
    completed = subprocess.run(command, check=False, capture_output=True, timeout=3600)
    if completed.returncode != 0:
        raise RuntimeError(
            "five-target matrix failed; "
            f"stdout_bytes={len(completed.stdout)}, stderr_bytes={len(completed.stderr)}"
        )
    return json.loads(completed.stdout)


def _matrix_summary(result: dict[str, Any]) -> dict[str, Any]:
    targets = result["targets"]
    return {
        "parsed_sessions": result["parsed_sessions"],
        "targets": len(targets),
        "generated_artifacts": sum(value["converted"] for value in targets.values()),
        "byte_validated": sum(value["byte_validated"] for value in targets.values()),
        "reparsed": sum(value["reparsed"] for value in targets.values()),
        "semantic_projection_matches": sum(
            value["semantic_projection_matches"] for value in targets.values()
        ),
        "loss_counter_matches": sum(value["loss_counter_matches"] for value in targets.values()),
        "native": result["native"],
    }


def _verify_manual_report(path: Path) -> dict[str, int]:
    text = path.read_text()
    if "exact=False" in text:
        raise RuntimeError("content-safe manual report contains a mismatch")
    rows = text.count(" exact=")
    cases = sum(line.startswith("sample=") for line in text.splitlines())
    return {"side_by_side_cases": cases, "exact_rows": rows, "mismatches": 0}


def _isolated_opencode_environment(root: Path) -> dict[str, str]:
    root.mkdir(mode=0o700)
    names = ("home", "data", "config", "cache", "state", "tmp")
    for name in names:
        (root / name).mkdir(mode=0o700)
    return {
        "HOME": str(root / "home"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "TERM": "dumb",
        "NO_COLOR": "1",
        "XDG_DATA_HOME": str(root / "data"),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_STATE_HOME": str(root / "state"),
        "TMPDIR": str(root / "tmp"),
        "OPENCODE_CONFIG_DIR": str(root / "config/opencode"),
        "OPENCODE_DISABLE_AUTOUPDATE": "true",
        "OPENCODE_DISABLE_PRUNE": "true",
    }


def _require_version(binary: Path, expected: str, *, pure: bool) -> None:
    arguments = [str(binary), "--version"]
    if pure:
        arguments.append("--pure")
    completed = subprocess.run(arguments, check=False, capture_output=True, text=True, timeout=15)
    if completed.returncode != 0 or completed.stdout.strip() != expected:
        raise RuntimeError(f"native validation requires OpenCode {expected}")


def _load_oracle() -> ModuleType:
    path = Path(__file__).with_name("validate-additional-target-corpus.py")
    name = "session_migrate_additional_target_oracle"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load independent corpus oracle")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _write_private(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)


if __name__ == "__main__":
    raise SystemExit(main())
