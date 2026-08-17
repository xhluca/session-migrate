# CLI specification

Status: implemented v0.1 baseline; native resume validated on 2026-08-17.

## Goal

Transfer a local coding-agent conversation between Claude Code and Codex CLI so
the target can resume it as a normal local session. Conversion must be safe,
auditable, useful on real transcripts, and explicit about semantic loss.

## Scope

The first stable release targets local Claude Code and Codex CLI sessions on
Linux, including the `basic-claude-uv` image. It supports file-to-file
conversion and optional installation into a target home. Authentication,
credentials, global configuration, memories, plugins, skills, and MCP settings
are not session data and are not copied.

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
   omissions, and a content-free record inventory.

### `import`

Performs `convert`, resolves the target session path from the selected home,
checks for collisions, and installs atomically. `--dry-run` performs discovery,
parse, mapping, target validation, and collision checks without writing.

## Neutral event model

The model retains the source session metadata plus a totally ordered stream of:

- user and assistant text;
- tool invocations and results;
- content-free reasoning or thinking markers;
- portable images and attachment/context markers;
- compaction summaries/boundaries;
- system/context changes; and
- opaque source records for accounting and possible future adapters.

Every event retains source provenance (record ordinal and source identifier).
Adapters do not manufacture tool output, reasoning, or timestamps that were not
present.

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
6. Target-only metadata (approval policy, sandbox policy, model provider, CLI
   version) uses explicit CLI options or safe local defaults and is identified
   as synthesized.
7. Source `system` and `developer` messages are never downgraded to target user
   messages. They are omitted and counted.

## Safety and privacy

- No source mutation.
- No credential/config copying.
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
best-effort and produces a warning. v0.1 has no strict-version mode.

## v0.1 exclusions

- discovering a session from a picker or database rather than a supplied JSONL;
- importing credentials, global configuration, memories, plugins, skills, MCP
  state, sandbox policy, or shell snapshots;
- translating subagent sidechains or a full branch tree;
- replaying private thinking/reasoning content;
- fetching or copying local attachment paths;
- synthesizing Codex paginated history or directly mutating SQLite indexes; and
- byte-identical round trips.

Codex paginated/history-base lineage and replacement-history compaction are
rejected in v0.1 rather than converted incompletely. Other exclusions become
manifest counters when the reader creates a corresponding portable event.
