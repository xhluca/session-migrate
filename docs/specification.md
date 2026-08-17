# CLI specification

Status: draft, pending native-format validation.

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
kinds, tool activity, schema/version hints, and compatibility warnings. It does
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
- reasoning or thinking summaries when available;
- images and local attachments;
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
   content are transferred only when the stored form is intended for replay;
   otherwise they become an omission entry in the manifest.
4. Tool names, argument JSON, call IDs, and results are preserved where target
   schemas have an equivalent. IDs are deterministically remapped if required.
5. Unknown records are not injected into a native transcript. They are counted
   and fingerprinted in the manifest.
6. Target-only metadata (approval policy, sandbox policy, model provider, CLI
   version) uses explicit CLI options or safe local defaults and is identified
   as synthesized.

## Safety and privacy

- No source mutation.
- No credential/config copying.
- No implicit overwrite or delete.
- Atomic install via a sibling temporary file and rename.
- File mode `0600` for conversation artifacts.
- Backups before any future in-place metadata/index update.
- SHA-256 provenance hashes.
- Content-free logs and inspection output by default.
- Clear warnings for schema drift and lossy conversion.

## Compatibility promise

Native session formats are not public interchange standards. Each adapter
records the observed producer version and recognizes tested schema families.
Unknown newer inputs fail safely in strict mode and may be parsed best-effort
only when the caller explicitly requests it.

