# CLI specification

Status: implemented through the current development release; native validation
completed on 2026-08-18.

## Goal

Transfer a local Claude Code, Codex CLI, or Pi conversation into a supported native
target so the target can resume it as a normal local session. Claude, Codex,
Pi, OpenCode, and GitHub Copilot CLI are supported targets. Antigravity and
Cursor are recognized but fail closed because they have no public resumable
transcript import contracts. Conversion must be safe, auditable, useful on real
transcripts, and explicit about semantic loss.

## Scope

Sources are local Claude Code, Codex CLI, and Pi v3 sessions on Linux, including the
`basic-claude-uv` image. The migrator supports file-to-file conversion,
UUID/catalog-based source discovery, and native installation into Claude,
Codex, Pi 0.80.6, OpenCode 1.17.20, or Copilot CLI 1.0.70. A private multi-root catalog inventories
and searches Claude/Codex/Pi source metadata without treating vendor indexes as
authoritative. External
credential stores, global configuration, memories,
plugins, skills, and MCP settings are not copied. The migrator does not redact or
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
2. `<output>.session-migrate.json`, containing provenance, hashes, warnings,
   omissions/transformations, a raw record count, and portable event-type
   counts.

### `import`

Performs `convert`, checks for collisions, and installs through the target's
supported contract. Claude, Codex, Pi, and Copilot use private no-clobber files.
OpenCode invokes the exact pinned CLI's public `import` command and never writes
its SQLite database. Copilot installation writes its public event log plus a
workspace sidecar and never synthesizes its derived SQLite. `--dry-run`
performs discovery, parse, mapping, target
validation, and collision checks without installing a conversation. OpenCode's
required public `session list` probe may initialize normal XDG state.

### `transfer`

Locates a native Claude/Codex/Pi transcript by source UUID and performs `import`.
Without `--to`, it preserves the legacy opposite-Claude/Codex default;
`--to pi|opencode|copilot` selects an additional target. Pi sources require an
explicit different target. Lookup is filesystem-only:
Claude searches its encoded
project directories (or an exact `--source-cwd`), while Codex searches active
and archived rollout filenames, while Pi searches its workspace session
buckets. The discovered transcript must declare the
requested native session ID. Missing, mismatched, and ambiguous matches fail
closed. No picker, SQLite database, or global session index is consulted.

An opaque catalog ID can instead select one exact indexed physical file. This
resolves duplicate UUIDs across roots without guessing, but does not trust the
cached status: transfer reopens and authoritatively validates the current
source.

### `catalog`

Indexes every recognized native JSONL under all automatic and registered roots,
including nested, archived, duplicate, malformed, and explicitly unsupported
sessions. Automatic roots are bounded to the three normal homes, environment
overrides, and ancestor-local native homes. Recursive project discovery occurs
only below explicit `--discover-under` boundaries and never follows directory
symlinks.

Default search fields are native title/name metadata, UUIDs, and Claude
sidechain native keys. Paths/CWDs require `--include-paths`. Prompts, responses,
previews, first-user-message fields, tool content, and media are never indexed.
The fast status is structural; deep conversion validation is explicit and is
always repeated at transfer time.

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
- Atomic no-clobber install via a sibling temporary file and hard-link publish
  for filesystem targets.
- File mode `0600` for conversation artifacts.
- No direct native metadata/index mutation. OpenCode mutation is delegated only
  to its public pinned importer after an official list-based collision check.
  The migrator's private catalog is disposable derived state.
- SHA-256 provenance hashes.
- Content-free logs and inspection output by default.
- Clear warnings for schema drift and lossy conversion.

## Compatibility promise

Native session formats are not public interchange standards. Each adapter
records the observed producer version. The tested baseline is Claude Code
`2.1.209`, Codex CLI `0.144.4`, and Pi `0.80.6`. Different declared Claude or
Codex source versions are parsed best-effort with a warning. Pi accepts only a
v3 header, which does not declare the producing package version. Additional target schemas are pinned to Pi
`0.80.6`, OpenCode `1.17.20`, and Copilot CLI `1.0.70`; automatic OpenCode
import requires the observed binary to match exactly.

## Exclusions

- integrating with native interactive pickers or treating a vendor database as
  authoritative inventory (the private catalog uses JSONL discovery and only
  read-only metadata enrichment);
- importing external credential stores, global configuration, memories,
  plugins, skills, MCP state, sandbox policy, or shell snapshots;
- translating subagent sidechains or a full branch tree;
- replaying private thinking/reasoning content;
- fetching or copying local attachment paths;
- synthesizing Codex paginated history or directly mutating SQLite indexes; and
- byte-identical round trips.

OpenCode and Copilot are targets only. Pi v3 is a detectable source and target.
Antigravity and Cursor import remain unsupported until the vendors publish
versioned transcript import APIs or native schemas that can be independently
validated.

Codex paginated/history-base lineage is rejected rather than converted
incompletely. Codex replacement-history checkpoints contain provider-encrypted
compaction state that Claude cannot decode. The cross-provider policy retains
the visible, expanded pre-compaction transcript and records
`compaction:replacement_history_expanded` in the manifest. This favors useful
conversation continuity while explicitly differing from Codex's compacted
effective context. Other exclusions become manifest counters when the reader
creates a corresponding portable event.
