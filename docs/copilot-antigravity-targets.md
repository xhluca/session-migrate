# GitHub Copilot CLI and Antigravity CLI target research

This document records the native contracts, security boundary, implementation
decision, and validation evidence for GitHub Copilot CLI and Antigravity CLI.
The investigation used synthetic markers and content-free aggregate reports.
No credential value, real prompt, response, tool payload, source path, or
session identifier is committed.

## Decision summary

| Target | Exact version | Import surface | Migrator decision |
| --- | --- | --- | --- |
| GitHub Copilot CLI | 1.0.70 | Public local session-event JSONL and explicit UUID resume | Supported |
| Antigravity CLI | 1.1.14 | Resume existing proprietary protobuf/SQLite trajectories only | Recognized, fail closed |

Copilot is supported because the generated public event log can be installed
without derived SQLite, resumed by the exact CLI, replayed to a provider, and
appended in place. Antigravity is not supported because neither its CLI nor its
SDK exposes an official operation for seeding an arbitrary prior transcript.
Knowing the private database tables is not a safe import contract.

## GitHub Copilot CLI 1.0.70

### Native store and discovery

The inspected package is `@github/copilot@1.0.70`. Copilot documents that each
CLI session is persisted under `~/.copilot/session-state/`; a local SQLite
session store is a structured subset used for history features. The pinned
runtime's canonical directory is:

```text
$COPILOT_HOME/session-state/<uuid>/
  events.jsonl
  workspace.yaml
```

`events.jsonl` is the append-only source of conversation truth. The global and
per-session SQLite databases are derived/runtime state and are never copied or
synthesized by the migrator. A cold resume from only the generated session
directory rebuilt SQLite and preserved the entire imported JSONL prefix.

Copilot supports exact explicit resume:

```console
copilot --resume <uuid>
```

The migrator imports into `COPILOT_HOME`, falling back to `~/.copilot`. It creates
the session directory with mode `0700`; `events.jsonl`, `workspace.yaml`, and
the content-free migrator manifest use mode `0600`. The entire session directory
is a collision boundary: if it already exists, dry-run and real import both
fail instead of merging or overwriting it.

### Event representation

The writer emits only event types proven against the package's public schema:

- `session.start` with target UUID, version, model, and absolute CWD;
- `user.message` and `assistant.message` for portable conversation text;
- `tool.execution_start` and `tool.execution_complete`, linked by the original
  tool-call ID;
- `session.compaction_complete` for a portable compaction summary;
- `session.binary_asset` for validated inline image bytes.

Every event has a unique UUIDv4, an RFC 3339 timestamp, and one linear
`parentId` chain. Source times that would regress are advanced by one
microsecond and counted as `timestamp:native_order_adjusted`. The byte validator
checks required metadata, UUID shape and uniqueness, the complete parent chain,
nondecreasing timestamps, call/result multiplicity, and binary-asset integrity.
It rejects a metadata-only artifact and requires at least one user turn, which
also permits a legitimate interrupted user-only session.

If repeated source results exceed the number of matching tool calls, each
excess result is counted as both a duplicate reference and an orphan for native
association. The writer emits a synthetic request/start immediately before that
completion with a fresh target-native call ID, so the result remains auditable
without violating Copilot's required call/result multiplicity or triggering
runtime ID deduplication. It never writes a completion that the native validator
cannot link to a distinct preceding request.

Images use content-addressed native records. The asset ID is the SHA-256 of the
decoded bytes, and each reference is checked against MIME type and byte length.
User images are attached to their message by asset ID. Tool-result images are
also retained exactly as native assets and referenced from the completed tool
result.

The exact image bytes survive native storage and migrator reparse. Model replay is
more limited: Copilot 1.0.70 forwarded a user image through the tested
OpenAI-compatible provider, while an image inside a tool result was not
forwarded under the OpenAI Chat Completions wire protocol. The migrator therefore
keeps the tool image but emits
`tool_result:image_provider_dependent`; this is a retained-with-warning detail,
not a claim that the native bytes were deleted.

### CLI behavior

```console
# Write only events.jsonl plus a sidecar migrator manifest.
session-migrate convert SOURCE --to copilot --output events.jsonl

# Install the complete resumable session directory.
session-migrate import SOURCE --to copilot --cwd /target/project --dry-run
session-migrate import SOURCE --to copilot --cwd /target/project

# Discover a Claude, Codex, or Pi source and import it.
session-migrate transfer SOURCE_UUID --from claude --to copilot \
  --source-cwd /source/project --cwd /target/project
```

`convert` is deliberately file-oriented and cannot install `workspace.yaml`;
use `import` or `transfer` for a directly resumable Copilot session. `--home`
overrides `COPILOT_HOME`. `--model` controls the target model metadata.
`--target-cli` remains OpenCode-only because Copilot import does not invoke a
subprocess.

### Credential boundary and actual TUI checks

The migrator never reads, copies, or rewrites Copilot, GitHub, Codex, browser, or
OS credential stores. A Codex desktop OAuth session is service-specific and was
not reinterpreted as a GitHub or Google credential.

GitHub officially supports Copilot CLI BYOK through
`COPILOT_PROVIDER_BASE_URL`, `COPILOT_PROVIDER_API_KEY`, and `COPILOT_MODEL`.
One existing OpenAI API key was passed only in the environment of an isolated
Copilot subprocess using that documented mechanism; the value was never
printed, written to a fixture, copied into a target home, or committed.

Three distinct runtime checks were completed:

1. A deterministic OpenAI-compatible loopback provider replayed an imported
   synthetic history and captured the exact provider request.
2. The actual full-screen Copilot TUI resumed an isolated imported session and
   completed two turns against the loopback provider.
3. The actual full-screen TUI completed two turns against the real OpenAI API
   through the supported BYOK environment. Both responses matched their
   synthetic nonce prompts, and the same native session remained resumable.

The authenticated test proves actual model/TUI operation, not credential
portability. Users must authenticate Copilot through GitHub or configure a
supported provider themselves.

### Validation evidence

The reusable content-safe corpus validator converted all 102 accessible
top-level Claude sessions to Copilot after the final media change. Results:

- 102/102 serialized, byte-validated, reparsed, and exact portable-semantic
  matched;
- 102/102 independently exact loss-counter matches;
- 95 tool sessions, six compaction sessions, 81 image sessions, 12 interrupted
  sessions, and 87 sessions at least 1 MiB;
- a 20-session structural report compared 23,637 rows for Copilot as part of
  70,911 rows across Pi/OpenCode/Copilot, with zero differences;
- 10/10 feature-stratified real conversions cold-resumed through exact Copilot
  1.0.70 using a no-credential loopback environment;
- all 10 preserved the generated prefix, rebuilt derived SQLite, and matched
  provider replay values.

The 10-case native subset included tools, compaction, images, interrupted and
large histories. Tool-result media was excluded only from provider-request
equality because of the documented provider boundary; it remained covered by
byte validation and semantic reparse.

## Antigravity CLI 1.1.14

### What is officially supported

The exact official release and source commit were pinned and inspected.
Antigravity documents workspace-scoped conversation picking, `--continue`,
`--conversation <id>`, headless JSON/streaming output, Google sign-in, ADC, and
Gemini API-key configuration. Its SDK can start and resume harness-managed
conversations.

The observed CLI store below `~/.gemini/antigravity-cli` contains one SQLite
database per conversation plus a summary/index database. The trajectory tables
store version-private protobuf blobs; the summary database is not the complete
model history. The installed `google-antigravity` 0.1.12 SDK exposes an internal
protobuf `initial_trajectory` field at the protocol layer, but its supported
Python connection API does not expose a way to populate it. Its public
`Conversation(history=...)` value is client-side history and does not seed the
native harness trajectory.

Directly constructing these SQLite/protobuf values, monkey-patching the hidden
field, or transplanting a database from another version would be unsupported
reverse engineering. It could create a picker-visible object that does not
replay correctly, or corrupt future versions. Accordingly:

```console
session-migrate import SOURCE --to antigravity
# session-migrate: error: Antigravity CLI 1.1.14 does not expose a
# documented resumable transcript import contract ...
```

No Antigravity writer exists, no private database is modified, and no output is
created. The target remains in the CLI choice list so automation receives an
intentional capability error instead of an unknown-option failure.

### Runtime verification despite the import blocker

The target runtime itself was still tested. In an isolated home and workspace,
the actual full-screen Antigravity 1.1.14 TUI was configured through its
supported Gemini API-key/base-URL path against a deterministic local Gemini
endpoint. The onboarding choices were completed, two synthetic turns finished,
the CLI exited with a resumable conversation ID, and its native database held
the expected user/agent/checkpoint step sequence. No real Google/Gemini
credential was available or copied.

This verifies that the exact runtime, TUI, trajectory creation, and native
resume mechanism function. It does **not** turn the proprietary trajectory into
a supported import contract. Support can be enabled later only if Google
publishes one of:

- a documented transcript import command;
- a stable, versioned trajectory serialization with a public validator; or
- an SDK operation that seeds and resumes arbitrary prior turns.

## Privacy and cleanup

Real source contents were processed only in private mode-`0700` temporary
directories. Reports and generated transcripts used mode `0600`; aggregate
counts were retained, not content. Credential values were restricted to
subprocess environments. Isolated Copilot/Antigravity homes, loopback request
captures, generated sessions, and validation reports were removed after the
release gates.

## Reproducible checks

```console
uv run pytest -q tests/test_copilot_format.py tests/test_target_integration.py

uv run python scripts/validate-additional-target-corpus.py \
  --claude-root /private/claude-home --manual-count 0

uv run python scripts/validate-copilot-native.py \
  --claude-root /private/claude-home \
  --copilot-bin /path/to/exact/copilot-1.0.70 --count 10
```

The corpus and native scripts emit aggregate structural results only. The
authenticated TUI and private actual-content visual checks are intentionally
not automated because doing so would require credential or transcript fixtures.

## Primary references

- GitHub Copilot session data:
  <https://docs.github.com/en/copilot/concepts/agents/copilot-cli/chronicle>
- GitHub Copilot resume behavior:
  <https://docs.github.com/en/copilot/how-tos/copilot-cli/use-copilot-cli/chronicle>
- GitHub Copilot CLI BYOK:
  <https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/use-byok-models>
- GitHub Copilot CLI command reference:
  <https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference>
- Antigravity CLI repository: <https://github.com/google-antigravity/antigravity-cli>
- Antigravity CLI 1.1.14 release:
  <https://github.com/google-antigravity/antigravity-cli/releases/tag/1.1.14>
- Antigravity conversation management:
  <https://antigravity.google/docs/cli/conversations/>
- Antigravity headless/resume behavior:
  <https://antigravity.google/docs/cli/headless/>
- Antigravity SDK: <https://antigravity.google/docs/sdk/overview/>
