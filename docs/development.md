# Development and release guide

This project treats native session formats as untrusted, versioned
implementation details. Changes to an adapter require semantic tests and a
native acceptance check, not only a successful JSON parse.

## Set up

```console
uv sync --dev
uv run session-migrate --version
```

Python 3.11 or newer is required. Runtime dependencies are intentionally empty;
pytest and Ruff are development dependencies locked by `uv.lock`.

## Repository map

| Path | Responsibility |
| --- | --- |
| `src/session_migrate/cli.py` | Arguments, JSON results, and content-safe errors |
| `src/session_migrate/jsonl.py` | Bounded JSONL reads and atomic private writes |
| `src/session_migrate/inspection.py` | Format detection and content-free inventory |
| `src/session_migrate/discovery.py` | Native UUID lookup |
| `src/session_migrate/catalog.py` | Private multi-root metadata index/search |
| `src/session_migrate/model.py` | Portable session/event model |
| `src/session_migrate/formats/claude.py` | Claude graph reader and linear writer |
| `src/session_migrate/formats/codex.py` | Codex rollout reader and legacy writer |
| `src/session_migrate/formats/pi.py` | Pi 0.80.6 v3 writer/parser/validator |
| `src/session_migrate/formats/omp.py` | Oh My Pi 18.0.5 title-slot v3 adapter |
| `src/session_migrate/formats/opencode.py` | OpenCode 1.17.20 public-bundle writer/parser/validator |
| `src/session_migrate/formats/copilot.py` | Copilot CLI 1.0.70 event writer/parser/validator |
| `src/session_migrate/formats/antigravity.py` | Antigravity 1.1.16 clean-room DB adapter |
| `src/session_migrate/formats/cursor.py` | Experimental pinned Cursor text DB adapter |
| `src/session_migrate/formats/vibe.py` | Mistral Vibe 2.24.3 two-file adapter |
| `src/session_migrate/formats/muse.py` | Muse Code 0.2.1 durable event adapter |
| `src/session_migrate/formats/qwen.py` | Qwen Code 0.22.1 chat-graph adapter |
| `src/session_migrate/formats/kimi.py` | Kimi Code 0.38.0 state/wire adapter |
| `src/session_migrate/formats/grok.py` | Grok 1.0.5 ACP-update adapter |
| `src/session_migrate/formats/kilo.py` | Kilo Code 7.5.0 official-bundle adapter |
| `src/session_migrate/formats/openhands.py` | OpenHands 1.16.0 SDK-event adapter |
| `src/session_migrate/formats/hermes.py` | Hermes Agent 0.20.6 shared-state adapter |
| `src/session_migrate/formats/mastracode.py` | MastraCode 0.37.1 LibSQL adapter |
| `src/session_migrate/formats/devin.py` | Devin CLI 3000.6.7 active-forest adapter |
| `src/session_migrate/formats/common.py` | Shared timestamps, text, and image validation |
| `src/session_migrate/conversion.py` | Mapping orchestration, manifests, and installation |
| `tests/fixtures/` | Synthetic, credential-free pinned-version transcripts |
| `scripts/verify-native-resume.sh` | Pinned Docker native-resume oracle |
| `scripts/validate-additional-target-corpus.py` | Content-safe seven-format corpus validator |
| `scripts/validate-copilot-native.py` | Exact Copilot cold-resume/provider-replay oracle |
| `scripts/validate-core-target-native.py` | Real-source Claude/Codex cold-resume oracle in the pinned image |
| `scripts/validate-antigravity-native.py` | Exact Antigravity native load/append oracle |
| `scripts/validate-opencode-source-corpus.py` | OpenCode export/source matrix and catalog oracle |
| `scripts/validate-authenticated-pi-tui.py` | Disposable two-step Pi TUI/Codex-OAuth trajectory |

## Fast and full gates

Run the normal local gate:

```console
uv run ruff check .
uv run pytest
git diff --check
```

GitHub Actions runs the locked Python suite on Python 3.11–3.13, builds the
package, and separately lints/builds/tests the landing page. Exact native gates
remain opt-in and use no CI provider tokens.

Changes to adapters, native paths, serialization, discovery, or installation
also require:

```console
bash -n scripts/verify-native-resume.sh
scripts/verify-native-resume.sh
uv build
uv tool run --isolated \
  --from dist/session_migrate-<version>-py3-none-any.whl \
  session-migrate --version
uv tool run --isolated \
  --from dist/session_migrate-<version>-py3-none-any.whl \
  smigrate --version
```

The Docker check is credential-free and network-disabled. It must prove the
target selected the imported UUID, preserved the imported prefix, and appended
to the same file. A provider response is not required.

Pi/OMP/OpenCode/Copilot/Antigravity/Cursor/Vibe/Muse/Qwen/Kimi/Grok/Kilo/OpenHands/Hermes/MastraCode/Devin adapter changes
additionally require the exact pinned binaries when available:

```console
uv run pytest -q tests/test_additional_formats.py
uv run pytest -q tests/test_additional_formats_native.py
uv run pytest -q tests/test_cursor_native.py
SESSION_MIGRATE_OMP_BIN=/path/to/omp-18.0.5 \
  uv run pytest -q tests/test_omp_native.py
SESSION_MIGRATE_VIBE_BIN=/path/to/vibe-2.24.3 \
  uv run pytest -q tests/test_vibe_native.py
# Explicit live-provider release oracle; skipped by default and never run in CI.
SESSION_MIGRATE_OPENROUTER_KEY_FILE=/private/openrouter.key \
SESSION_MIGRATE_QWEN_BIN=/path/to/qwen-0.22.1 \
SESSION_MIGRATE_KIMI_BIN=/path/to/kimi-0.38.0 \
SESSION_MIGRATE_MUSE_BIN=/path/to/muse-0.2.1 \
SESSION_MIGRATE_MUSE_OPENROUTER_BIN=/path/to/muse-openrouter-0.3.2 \
  uv run pytest -q tests/test_muse_qwen_kimi_native.py
SESSION_MIGRATE_GROK_BIN=/path/to/grok-1.0.5 \
SESSION_MIGRATE_KILO_BIN=/path/to/kilo-7.5.0 \
SESSION_MIGRATE_OPENHANDS_BIN=/path/to/openhands-1.16.0 \
  uv run pytest -q tests/test_grok_kilo_openhands_native.py
SESSION_MIGRATE_HERMES_SOURCE=/path/to/hermes-agent-v2026.8.27 \
  uv run pytest -q tests/test_hermes_native.py
SESSION_MIGRATE_MASTRACODE_BIN=/path/to/mastracode-0.37.1 \
  uv run pytest -q tests/test_mastracode_native.py
SESSION_MIGRATE_DEVIN_BIN=/path/to/devin-3000.6.7 \
  uv run pytest -q tests/test_devin_native.py
uv run python scripts/validate-muse-qwen-kimi-corpus.py \
  --claude-root /private/claude-home
uv run python scripts/validate-muse-qwen-kimi-corpus.py \
  --codex-root /private/codex-home --sample 100
uv run python scripts/validate-additional-target-corpus.py \
  --claude-root /private/claude-home --manual-count 0
uv run python scripts/validate-additional-target-corpus.py \
  --codex-root /private/codex-home --manual-count 0
uv run python scripts/validate-additional-target-corpus.py \
  --pi-root /private/pi-home --manual-count 0
uv run python scripts/validate-copilot-native.py \
  --claude-root /private/claude-home \
  --copilot-bin /path/to/copilot-1.0.70 --count 10
uv run python scripts/validate-copilot-native.py \
  --codex-root /private/codex-home \
  --copilot-bin /path/to/copilot-1.0.70 --count 10
uv run python scripts/validate-copilot-native.py \
  --pi-root /private/pi-home \
  --copilot-bin /path/to/copilot-1.0.70 --count 10
uv run python scripts/validate-core-target-native.py \
  --codex-root /private/codex-home --count 10
uv run python scripts/validate-core-target-native.py \
  --pi-root /private/pi-home --count 10
uv run python scripts/validate-antigravity-native.py --help
uv run python scripts/validate-opencode-source-corpus.py --help
# Explicit live-provider gate; never run this in CI or print its private output.
uv run python scripts/validate-authenticated-pi-tui.py \
  --codex-auth /private/codex-home/auth.json \
  --pi-bin /path/to/pi-0.80.6
```

The corpus command prints aggregate counts only. `--native-count N` also needs
explicit pinned Pi and OpenCode binary paths. The Copilot oracle uses a local
OpenAI-compatible provider, an isolated home, and no inherited credential
variables. Never commit or print the private source root. Actual authenticated
TUI checks may pass a supported provider key only in a subprocess environment.
The Pi-specific harness may translate the current Codex OAuth record only into
a disposable, mode-`0600` isolated Pi auth file, never a normal Pi home. Never
log credentials or make credential transfer part of the migrator itself.

The source-matrix gate is symmetric: every readable source exercises all eighteen
targets, including same-format portable rewrites. Cursor comparisons project
only ordered text and independently verify every loss counter. Antigravity and
Cursor require their exact clean-room native oracles; Cursor remains labeled
experimental. Use catalog deep-validation separately when discovery/indexing
changes—a successful catalog scan never replaces the conversion matrix.

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

## Release checklist

1. Update the version together in `pyproject.toml`,
   `src/session_migrate/__init__.py`, the CLI version test, and `uv.lock`.
2. Update README, CLI/compatibility/catalog docs, changelog, landing page, and
   the dated validation report without rewriting historical evidence.
3. Run Ruff, the complete test suite, diff/link/site checks, applicable pinned
   native/TUI/corpus gates, `uv build`, and isolated wheel entry-point checks.
4. Inspect wheel/sdist contents and scan new tracked history for credentials,
   private paths, real transcript content, and vendor artifacts.
5. Confirm the worktree is clean and the branch is synchronized with the
   canonical remote.
6. Create/push one annotated tag, publish only its clean dist artifacts, verify
   PyPI JSON and a fresh public install, and create the GitHub release.
7. Deploy the matching canonical `session-migrate.github.io` page and verify
   served/local HTML plus assets, robots, sitemap, and canonical metadata.
