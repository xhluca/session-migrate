# Session format compatibility

This document records the on-disk behavior observed by this project and the
exact subset implemented by `session-bridge`. Neither Claude Code nor Codex CLI
publishes its local transcript format as a stable interchange standard. Treat
these findings as versioned integration evidence, not as a promise from either
vendor.

All examples below are schematic and contain no real transcript content.

## Validation scope

The Claude/Codex native write paths are pinned to the two source CLIs in the
local `basic-claude-uv:latest` integration image. Additional targets are pinned
to separately installed host binaries:

| Component | Validated version |
| --- | --- |
| Image | `sha256:8f170f660813ac358f347fa8a3580139972f3ea7a9fb087834f1da44669d9392` |
| Claude Code | `2.1.209` |
| Codex CLI | `0.144.4` |
| Pi target | `0.80.6` |
| OpenCode target | `1.17.20` |
| GitHub Copilot CLI target | `1.0.70` |
| Antigravity CLI inspected, unsupported | `1.1.14` |

Claude Code `2.1.234` and Codex CLI `0.147.0` were also inspected on the host.
The Codex `rust-v0.147.0` source was used to understand rollout discovery and
index repair, but output from those newer versions has not received the same
native-resume test. The converter warns when a source declares a CLI version
other than its pinned version.

The native test runs without credentials and with container networking
disabled. For each direction it imports a synthetic fixture, invokes the
target CLI by the imported UUID, verifies that the CLI selected that UUID, and
verifies that the target appended native records before authentication or
network access failed. This proves discovery, parsing, selection, and append
compatibility. It does not claim that an unauthenticated model turn completed.

Pi, OpenCode, and Copilot are additional write targets, not source formats.
Their exact schema mappings, native probes, loss keys, and the
Antigravity/Cursor unsupported decisions are specified in
[Additional native targets](additional-target-formats.md) and the
[Copilot/Antigravity research](copilot-antigravity-targets.md). All three
preserve a portable ordered text/image/tool/compaction projection; OpenCode may
adjust native timestamps, while Copilot warns when tool-result image replay is
provider-dependent despite exact native asset retention.

## Discovery and indexes

### Claude Code

The default store is `~/.claude`; `CLAUDE_CONFIG_DIR` replaces that root. A
project transcript is discovered at:

```text
$CLAUDE_CONFIG_DIR/projects/<encoded-cwd>/<session-uuid>.jsonl
```

If `CLAUDE_CONFIG_DIR` is unset, `$CLAUDE_CONFIG_DIR` above means `~/.claude`.
The project directory name is the absolute working directory with every
non-ASCII-alphanumeric character replaced by `-`. For example, `/work/a.b`
becomes `-work-a-b`. This encoding is not injective, so distinct paths can
collide.

In the inspected corpus, the filename UUID equaled every conversation record's
`sessionId`. `sessions-index.json` was not required: placing a valid transcript
at the native path was sufficient for explicit `claude --resume <uuid>`, and
Claude appended to that file. Global configuration, authentication, and index
files are therefore neither read nor written by the bridge.

`session-bridge transfer UUID --from claude` searches these project
directories directly. Supplying `--source-cwd` selects the exact encoded
project directory; without it, more than one filename match is treated as
ambiguous because the CWD encoding can collide.

### Codex CLI

The default store is `~/.codex`; `CODEX_HOME` replaces that root and must
already exist for the pinned CLI. Rollouts are stored at:

```text
$CODEX_HOME/sessions/YYYY/MM/DD/rollout-YYYY-MM-DDTHH-MM-SS-<session-uuid>.jsonl
```

Codex also maintains `state_5.sqlite` and related logs, goals, memories, and
shell snapshots. The SQLite row is a derived discovery cache, not the
authoritative transcript. Explicit UUID resume fell back to scanning rollout
files and repaired its state without a pre-created database row. The bridge
therefore writes only the rollout and its own manifest; it never edits Codex's
database.

Direct transfer lookup searches both the date-partitioned active store and
`$CODEX_HOME/archived_sessions`. Duplicate active/archive matches fail closed.
For either source format, transcript metadata must contain the requested UUID;
a filename alone is not trusted.

## Claude transcript model

A Claude transcript is JSON Lines whose conversation records form a UUID
graph. A compact observed minimum for a resumable message record is:

```json
{
  "type": "user",
  "message": {"role": "user", "content": "..."},
  "uuid": "<record-uuid>",
  "parentUuid": null,
  "sessionId": "<session-uuid>",
  "timestamp": "<RFC-3339 timestamp>"
}
```

Controlled probes found the timestamp to be required. The bridge emits a more
conservative native shape: `isSidechain`, `userType`, `cwd`, `version`,
`gitBranch`, and, for assistants, the Anthropic message wrapper, generated
message/request IDs, stop fields, and zeroed usage fields. Every emitted
conversation record gets a new record UUID and is linked into one linear
`parentUuid` chain.

Important graph and content behavior:

- A trailing `last-prompt.leafUuid`, when valid, identifies the active leaf.
  The bridge walks `parentUuid` through all UUID-bearing records, including
  metadata nodes, and follows a compaction boundary's `logicalParentUuid` back
  into pre-compaction history. It reverses the ancestry walk and emits graph
  order; physical line order is not semantic order during streamed writes.
- Without a valid recorded leaf, the last eligible conversation record is used.
  If it lacks a usable UUID, all eligible conversation records are retained in
  file order.
- Inactive branch messages become opaque events and are reported as dropped on
  write. `isSidechain: true` records are never selected as the main branch.
- Selected `isMeta: true` nodes remain in ancestry traversal but are emitted
  only as content-free opaque omissions. Their message/tool blocks are never
  replayed as target conversation history.
- Assistant output can be split across several records or several content
  blocks. Text, `tool_use`, `tool_result`, image, thinking, and document blocks
  were observed.
- Tool calls use `id`, `name`, and structured `input`; tool results refer back
  with `tool_use_id` and may contain text or structured blocks.
- Compaction is represented by a `system`/`compact_boundary` record plus a user
  record marked `isCompactSummary: true`. Other system subtypes and top-level
  attachment/queue/title records exist. A preserved-segment compaction can
  contain a metadata-declared back-edge; the reader accepts only the validated
  anchor/head/tail shape observed in native files and rejects other cycles.
- Subagent transcripts may live below
  `<session-uuid>/subagents/agent-<id>.jsonl`; the bridge does not recursively
  import those files. A standalone sidechain file is rejected with a specific
  instruction to transfer the parent session.

## Codex rollout model

A Codex rollout is also JSON Lines, but it is an ordered envelope stream rather
than a UUID graph:

```json
{"timestamp":"<RFC-3339>","type":"<record-type>","payload":{}}
```

Newer files can add `ordinal`; the pinned writer deliberately emits the legacy
mode without ordinals. The first envelope is `session_meta`. The conservative
resumable shape written by the bridge is:

```json
{
  "timestamp": "<RFC-3339 timestamp>",
  "type": "session_meta",
  "payload": {
    "session_id": "<session-uuid>",
    "id": "<session-uuid>",
    "timestamp": "<RFC-3339 timestamp>",
    "cwd": "/absolute/project/path",
    "originator": "agent-session-bridge",
    "cli_version": "0.144.4",
    "source": "cli",
    "model_provider": "openai",
    "history_mode": "legacy"
  }
}
```

`id`, `session_id`, and the filename UUID agree. A useful conversation then has
ordered `response_item` envelopes. Text messages use `type: "message"` with
`input_text` or `output_text` content. Tool activity uses `function_call` and
`function_call_output`; call arguments are a JSON-encoded string, and both
records share `call_id`.

`response_item` is the canonical, model-visible history. `event_msg` records
drive list preview and UI display. The writer emits both for text messages so
the text is visible to the resumed model and to the interface. The reader
deduplicates UI messages against response-item messages. It uses UI events as a
legacy fallback when a rollout has no canonical messages. In a mixed partial
rollout, exact normalized duplicates are removed; unmatched UI projections are
retained as marked messages and reported in the manifest.

Current legacy rollouts can contain `compacted.replacement_history`. Codex
installs that array as the effective history at the checkpoint and replays only
later items. In a bounded structural audit, replacement arrays ended in a
provider-encrypted `compaction` item; Claude cannot interpret that state. The
bridge intentionally chooses an expanded-transcript mapping: it retains the
visible response items that Codex compacted away, omits the encrypted state,
and emits a dedicated manifest warning. It does not claim an exact
post-compaction context transfer. The paired `event_msg.context_compacted` UI
notification is deduplicated against the checkpoint.

Other observed envelopes include `compacted`, `turn_context`, `world_state`,
reasoning response items, inter-agent communication, and newer paginated or
fork-related state. Those records are not all portable conversation history.

## Mapping matrix

Legend:

- **Supported**: the conversation meaning and applicable linkage identifiers
  are retained; target-specific wrappers, block grouping, and record UUIDs may
  be regenerated.
- **Lossy**: a useful representation is emitted, but grouping, role, status, or
  source-only metadata is lost. Known omissions appear in the sidecar manifest
  unless the row explicitly says otherwise.
- **Unsupported**: no safe target-history representation is emitted.

| Feature | Claude to Codex | Codex to Claude | Exact behavior |
| --- | --- | --- | --- |
| User and assistant text | **Supported** | **Supported** | Text and ordering are retained. Empty text blocks are ignored. System/developer roles are explicitly omitted and counted; they are never downgraded into user prompts. |
| Message timestamps | **Supported** | **Lossy** | Valid timezone-aware timestamps are copied. Claude groups blocks only when they came from the same source record. Invalid/missing values fall back safely and invalid values are counted. |
| Session ID | **Lossy by default** | **Lossy by default** | A fresh target UUID avoids collision. `--session-id` can explicitly choose the target UUID; source record UUIDs are not reused. |
| Working directory | **Supported** | **Supported** | Source `cwd` is retained or can be overridden. A missing/nonexistent directory produces a warning. Claude's encoded directory name can collide. |
| Model/provider metadata | **Lossy** | **Lossy** | Codex provider and Claude target model use target defaults/options. Turn-specific model, policy, sandbox, and token metadata are not reconstructed. |
| Conversation title | **Unsupported** | **Supported** | A Codex `thread_name_updated` title becomes Claude `custom-title`. A Claude title is omitted from Codex and counted as `session:title`. |
| Tool call name, input, and ID | **Supported** | **Lossy** | Claude `tool_use` maps to Codex `function_call`; JSON argument strings are parsed when valid. Missing IDs/names receive linked synthetic fallbacks and warnings. Codex free-form input is wrapped in an object for Claude and counted. |
| Text tool result | **Supported** | **Supported** | Call linkage and text output are retained. Orphan and duplicate call/result IDs are preserved but explicitly counted because the target CLI may diagnose or normalize inconsistent linkage. |
| Image tool result | **Supported** | **Supported** | Claude base64 sources normalize to self-contained data URLs; remote URLs remain URLs. |
| Tool error status | **Lossy** | **Lossy** | Codex has no emitted equivalent for Claude `is_error`; the output remains and `tool_result:is_error` is reported. Codex input does not restore an error flag in Claude. |
| Tool reference result block | **Unsupported** | **Supported when present** | Claude tool references have no emitted Codex equivalent and are counted. A Codex structured output block explicitly typed `tool_reference` can be emitted as Claude `tool_reference`. |
| Other structured result blocks | **Lossy/unsupported** | **Lossy/unsupported** | Target-equivalent text/images are retained. Audio, encrypted content, and unknown nested blocks are not portable and are counted with an opaque sentinel. |
| Standalone message image | **Lossy** | **Lossy** | Supported PNG/JPEG/GIF/WebP base64 or HTTP(S) URLs are retained as `input_image` or Claude image source, but original grouping is not guaranteed. Only user-role images transfer; privileged/assistant images and malformed data are counted and omitted. A retained remote URL may be fetched later by the target CLI. |
| Document block or top-level attachment | **Unsupported** | **Unsupported** | Claude document events are dropped and counted as context. Standalone attachment graph records are opaque. Local files are never copied. |
| Claude compaction summary | **Lossy** | N/A | A boundary/summary pair becomes exactly one Codex `compacted` message while the selected pre/post history remains ordered. Detailed `compactMetadata`, when present, is omitted and counted. |
| Codex compaction | N/A | **Lossy** | A legacy `compacted.message` is recognized but omitted from Claude output and counted. For `replacement_history`, visible pre-compaction response items are retained while provider-encrypted compaction state is omitted; `compaction:replacement_history_expanded` explains the semantic difference. |
| Thinking/reasoning | **Unsupported by design** | **Unsupported by design** | Private thinking or reasoning content is never transferred. A content-free event is counted as dropped. |
| Active Claude branch | **Supported** | N/A | `last-prompt` ancestry selects one coherent branch. Target output is linear. |
| Inactive Claude branches | **Unsupported** | N/A | They become opaque events and are counted as dropped; no forks are created. |
| Claude sidechains/subagents | **Unsupported** | N/A | A standalone sidechain transcript is rejected with a precise error; nested subagent files are not discovered recursively. |
| Codex legacy linear history | N/A | **Supported** | Ordered response items become one linear Claude UUID graph. |
| Codex paginated history/forks | N/A | **Unsupported** | Non-legacy `history_mode` and `history_base` are rejected rather than risking an incomplete import. Replacement-history compaction uses the expanded-transcript policy above. |
| Codex UI-only messages | N/A | **Lossy fallback** | Used as the conversation when no response-item messages exist. In a mixed partial file, exact normalized duplicates are removed and unmatched projections are retained with `message:ui_only_projection`; fuzzy matching is never used. |
| Turn context, policies, world state, snapshots | **Unsupported** | **Unsupported** | Codex `turn_context` is counted as context; `world_state` and `security_risk_score` become counted opaque events. Shell snapshots, approvals, external credential stores, MCP state, memories, goals, and configuration are outside transcript conversion. |
| Unknown source records/blocks | **Unsupported** | **Unsupported** | They become content-free opaque/sentinel events where recognized and are counted at write time, including unknown nested tool-result blocks. |

## What “resumable” means

The bridge produces a new native conversation containing the portable visible
history, not a byte-for-byte clone of the original runtime. It does not transfer
authentication, pending permissions, live processes, shell state, task plans,
MCP connections, agent teams, or model caches. Tool calls and results provide
historical context; they are not re-executed.

External credential/configuration stores are excluded, but the bridge performs
no redaction or secret scanning. Tokens or other secrets embedded in supported
messages, tool arguments/results, or images are copied into the target
transcript. Treat source and target JSONL files as equally sensitive.

Imports never mutate the source or intentionally overwrite an existing target. Inputs are
bounded at 64 MiB per record, 256 MiB per file, and 100,000 records by default.
Device/inode/size/modification metadata is checked across the read so an
actively appending or replaced source fails for a clean retry.
Claude, Codex, Pi, and Copilot native files plus content-free manifests use atomic
no-clobber publication with mode `0600`; if manifest creation fails after a new
filesystem target is created, that target is removed. OpenCode instead uses
the exact pinned public importer and publishes only a private bridge manifest
after official list-based verification; the bridge never writes its SQLite.
Explicit UUID resume is the
authoritative integration check; picker ordering and previews can vary by CLI
version and current working directory.
