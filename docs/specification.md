# CLI specification

Status: implemented v0.1.x compatibility line; current release v0.1.2; native
resume validated on 2026-08-18.

## Goal

Transfer a local coding-agent conversation between Claude Code and Codex CLI so
the target can resume it as a normal local session. Conversion must be safe,
auditable, useful on real transcripts, and explicit about semantic loss.

## Scope

The first stable release targets local Claude Code and Codex CLI sessions on
Linux, including the `basic-claude-uv` image. It supports file-to-file
conversion, UUID-based native discovery, and optional installation into a
target home. External credential stores, global configuration, memories,
plugins, skills, and MCP settings are not copied. The bridge does not redact or
secret-scan the portable conversation itself: embedded credentials in
messages, tool data, or images are copied into the target JSONL.

## Commands

### `inspect`

Detects the source format and reports IDs, timestamps, record counts, content
kinds, tool activity, and schema/version hints. It does
not print message text or tool payloads unless a future explicit unsafe flag is
added.

### `convert`

Parses a source transcript into a neutral ordered event model, maps supported
events into the target's conservative native subset, validates the generated
transcript, and writes:

1. the native target session; and
2. `<output>.session-bridge.json`, containing provenance, hashes, warnings,
   omissions/transformations, a raw record count, and portable event-type
   counts.

### `import`

Performs `convert`, resolves the target session path from the selected home,
checks for collisions, and installs atomically. `--dry-run` performs discovery,
parse, mapping, target validation, and collision checks without writing.

### `transfer`

Locates a native transcript by source UUID, infers the opposite target format,
and performs `import`. Lookup is filesystem-only: Claude searches its encoded
project directories (or an exact `--source-cwd`), while Codex searches active
and archived rollout filenames. The discovered transcript must declare the
requested native session ID. Missing, mismatched, and ambiguous matches fail
closed. No picker, SQLite database, or global session index is consulted.

## Neutral event model

The model retains the source session metadata plus a totally ordered stream of:

- user and assistant text;
- tool invocations and results;
- content-free reasoning or thinking markers;
- portable images and attachment/context markers;
- compaction summaries/boundaries;
- system/context changes; and
- opaque source records for accounting and possible future adapters.

Every event retains source provenance: record ordinal and, where available,
source identifier and block ordinal.
Adapters do not manufacture tool output or reasoning. Target writers do create
fresh structural UUIDs and may synthesize fallback timestamps and target
metadata required by the native schema; applicable fallbacks are warned or
counted.

## Mapping policy

1. Preserve semantic conversation order over source implementation order.
2. Emit only record shapes accepted by the target CLI version family.
3. Never expose private chain-of-thought. Claude `thinking` and Codex reasoning
   content are not transferred; a content-free omission is recorded instead.
4. Tool names, argument JSON, call IDs, and results are preserved where target
   schemas have an equivalent. Missing IDs receive fresh, linked synthetic
   fallbacks and a manifest warning.
5. Unknown records are not injected into a native transcript. They are counted
   in the manifest; the source file itself is identified by SHA-256.
6. Required target metadata such as provider, version, model label, CWD, and
   structural IDs uses explicit options or safe local defaults and is recorded
   in the target/manifest where applicable. Source approval and sandbox policy
   are omitted rather than reconstructed.
7. Source `system` and `developer` messages are never downgraded to target user
   messages. They are omitted and counted.

## Safety and privacy

- No source mutation.
- Reject a source whose identity, size, or modification time changes during
  detection, parsing, and hashing.
- No external credential/config-store copying.
- No redaction, secret scanning, or encryption of portable conversation
  content; generated transcripts require the same protection as sources.
- No implicit overwrite or delete.
- Atomic no-clobber install via a sibling temporary file and hard-link publish.
- File mode `0600` for conversation artifacts.
- No metadata/index mutation. Any future in-place update must add backups first.
- SHA-256 provenance hashes.
- Content-free logs and inspection output by default.
- Clear warnings for schema drift and lossy conversion.

## Compatibility promise

Native session formats are not public interchange standards. Each adapter
records the observed producer version. The tested baseline is Claude Code
`2.1.209` and Codex CLI `0.144.4`; a different source version is parsed
best-effort and produces a warning. The v0.1.x line has no strict-version mode.

## v0.1.x exclusions

- discovering a session from a picker or database (direct UUID filesystem
  discovery is supported);
- importing external credential stores, global configuration, memories,
  plugins, skills, MCP state, sandbox policy, or shell snapshots;
- translating subagent sidechains or a full branch tree;
- replaying private thinking/reasoning content;
- fetching or copying local attachment paths;
- synthesizing Codex paginated history or directly mutating SQLite indexes; and
- byte-identical round trips.

Codex paginated/history-base lineage is rejected rather than converted
incompletely. Codex replacement-history checkpoints contain provider-encrypted
compaction state that Claude cannot decode. The cross-provider policy retains
the visible, expanded pre-compaction transcript and records
`compaction:replacement_history_expanded` in the manifest. This favors useful
conversation continuity while explicitly differing from Codex's compacted
effective context. Other exclusions become manifest counters when the reader
creates a corresponding portable event.
