# Development and release guide

This project treats native session formats as untrusted, versioned
implementation details. Changes to an adapter require semantic tests and a
native acceptance check, not only a successful JSON parse.

## Set up

```console
uv sync --dev
uv run session-bridge --version
```

Python 3.11 or newer is required. Runtime dependencies are intentionally empty;
pytest and Ruff are development dependencies locked by `uv.lock`.

## Repository map

| Path | Responsibility |
| --- | --- |
| `src/session_bridge/cli.py` | Arguments, JSON results, and content-safe errors |
| `src/session_bridge/jsonl.py` | Bounded JSONL reads and atomic private writes |
| `src/session_bridge/inspection.py` | Format detection and content-free inventory |
| `src/session_bridge/discovery.py` | Filesystem-only UUID lookup |
| `src/session_bridge/model.py` | Portable session/event model |
| `src/session_bridge/formats/claude.py` | Claude graph reader and linear writer |
| `src/session_bridge/formats/codex.py` | Codex rollout reader and legacy writer |
| `src/session_bridge/formats/pi.py` | Pi 0.80.6 v3 writer/parser/validator |
| `src/session_bridge/formats/opencode.py` | OpenCode 1.17.20 public-bundle writer/parser/validator |
| `src/session_bridge/formats/copilot.py` | Copilot CLI 1.0.70 event writer/parser/validator |
| `src/session_bridge/formats/common.py` | Shared timestamps, text, and image validation |
| `src/session_bridge/conversion.py` | Mapping orchestration, manifests, and installation |
| `tests/fixtures/` | Synthetic, credential-free pinned-version transcripts |
| `scripts/verify-native-resume.sh` | Pinned Docker native-resume oracle |
| `scripts/validate-additional-target-corpus.py` | Content-safe aggregate Pi/OpenCode/Copilot corpus validator |
| `scripts/validate-copilot-native.py` | Exact Copilot cold-resume/provider-replay oracle |

## Fast and full gates

Run the normal local gate:

```console
uv run ruff check .
uv run pytest
git diff --check
```

Changes to adapters, native paths, serialization, discovery, or installation
also require:

```console
bash -n scripts/verify-native-resume.sh
scripts/verify-native-resume.sh
uv build
uv tool run --isolated \
  --from dist/agent_session_bridge-<version>-py3-none-any.whl \
  session-bridge --version
```

The Docker check is credential-free and network-disabled. It must prove the
target selected the imported UUID, preserved the imported prefix, and appended
to the same file. A provider response is not required.

Pi/OpenCode/Copilot adapter changes additionally require the exact pinned binaries when
available:

```console
uv run pytest -q tests/test_additional_formats.py
uv run pytest -q tests/test_additional_formats_native.py
uv run python scripts/validate-additional-target-corpus.py \
  --claude-root /private/claude-home --manual-count 0
uv run python scripts/validate-copilot-native.py \
  --claude-root /private/claude-home \
  --copilot-bin /path/to/copilot-1.0.70 --count 10
```

The corpus command prints aggregate counts only. `--native-count N` also needs
explicit pinned Pi and OpenCode binary paths. The Copilot oracle uses a local
OpenAI-compatible provider, an isolated home, and no inherited credential
variables. Never commit or print the private source root. Actual authenticated
TUI checks may pass a supported provider key only in a subprocess environment;
never log it or make credential transfer part of the bridge.

## Adapter change checklist

1. Record the exact source CLI version and native record shape without copying
   real content into the repository.
2. Parse into the portable event model with source record/block provenance.
3. Keep system/developer instructions and private reasoning out of target user
   history.
4. Preserve message order and call/result IDs; count every unsupported nested
   block or transformation.
5. Emit only the conservative pinned target schema. Changing the metadata
   version is not a schema migration.
6. Validate generated IDs, native head metadata, and non-empty resumable
   history.
7. Add synthetic parser, serializer, loss-accounting, malformed-input, and
   round-trip regressions.
8. Reparse the generated file with the target adapter and compare portable
   semantics independently.
9. Run explicit native resume in an isolated target home.
10. Update the compatibility matrix, exploration log, validation evidence, and
    CLI reference when behavior or warnings change.

Unknown or ambiguous source state should fail closed. Do not make a file
convert merely by weakening history-mode, ancestry, role, or linkage checks.

## Test-fixture and privacy rules

- Commit only synthetic transcripts with deterministic fake IDs, generic text,
  fake tool activity, and non-personal paths.
- Never commit credentials, tokens, real prompts/responses, real tool
  arguments/results, extracted images, or CLI databases.
- Real-session audits may run locally when necessary, but reports must contain
  only aggregate structure and counters. Content-bearing temporary reports
  must be private and deleted after review.
- Tests must assert manifest loss counters, not only target record counts.
- Malformed, oversized, mixed-format, changing-source, collision, and
  interrupted histories deserve explicit regressions.

The [validation report](validation-report.md) defines native acceptance,
portable semantic equivalence, expected loss, and unexplained discrepancies.
An unexplained semantic or manifest difference blocks release.

## Documentation responsibilities

- `README.md`: supported use cases, quick start, safety summary, and navigation.
- `cli-reference.md`: exact commands, options, defaults, outputs, and manifest.
- `troubleshooting.md`: operational failure recovery.
- `format-compatibility.md`: supported/lossy/unsupported mapping contract.
- `architecture.md`: trust boundaries and implementation invariants.
- `exploration-log.md`: dated research evidence and design decisions.
- `docker-environment.md`: pinned integration environment and reproduction.
- `validation-report.md`: sanitized release evidence and known coverage gaps.

Keep claims scoped to an exact version or dated observation. Avoid calling a
conversion lossless when source-only state was omitted with a warning.

## Patch release checklist

1. Complete the applicable task file under `.claude/todos/` and move it to
   `.claude/todos/completed/`.
2. Update the version together in `pyproject.toml`,
   `src/session_bridge/__init__.py`, the CLI version test, and `uv.lock`.
3. Run the local, Docker, build, and isolated-wheel gates above.
4. Confirm the worktree is clean and the branch is synchronized with the
   private remote.
5. Create and push one annotated version tag that resolves to the release
   commit.
