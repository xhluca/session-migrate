# Session format compatibility

This document records the on-disk behavior observed by this project and the
exact subset implemented by `session-bridge`. Neither Claude Code nor Codex CLI
publishes its local transcript format as a stable interchange standard. Treat
these findings as versioned integration evidence, not as a promise from either
vendor.

All examples below are schematic and contain no real transcript content.

## Validation scope

The native write paths are pinned to the two CLIs in the local
`basic-claude-uv:latest` integration image:

| Component | Validated version |
| --- | --- |
| Image | `sha256:8f170f660813ac358f347fa8a3580139972f3ea7a9fb087834f1da44669d9392` |
| Claude Code | `2.1.209` |
| Codex CLI | `0.144.4` |

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
  metadata nodes, and converts conversation records on that path.
- Without a valid recorded leaf, the last eligible conversation record is used.
  If it lacks a usable UUID, all eligible conversation records are retained in
  file order.
- Inactive branch messages become opaque events and are reported as dropped on
  write. `isSidechain: true` records are never selected as the main branch.
- Assistant output can be split across several records or several content
  blocks. Text, `tool_use`, `tool_result`, image, thinking, and document blocks
  were observed.
- Tool calls use `id`, `name`, and structured `input`; tool results refer back
  with `tool_use_id` and may contain text or structured blocks.
- Compaction is represented by a `system`/`compact_boundary` record plus a user
  record marked `isCompactSummary: true`. Other system subtypes and top-level
  attachment/queue/title records exist.
- Subagent transcripts may live below
  `<session-uuid>/subagents/agent-<id>.jsonl`; the bridge does not recursively
  import those files.

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
prefers `response_item` messages and uses `event_msg.user_message` and
`event_msg.agent_message` only when no response-item messages exist, avoiding
duplicate turns.

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
| User and assistant text | **Supported** | **Supported** | Text and ordering are retained. Empty text blocks are ignored. Codex system/developer message roles are currently coerced to user rather than preserved. |
| Message timestamps | **Supported** | **Lossy** | Per-event timestamps are copied. Claude output coalesces adjacent same-role blocks into a record carrying the first timestamp. Missing values fall back to the session timestamp. |
| Session ID | **Lossy by default** | **Lossy by default** | A fresh target UUID avoids collision. `--session-id` can explicitly choose the target UUID; source record UUIDs are not reused. |
| Working directory | **Supported** | **Supported** | Source `cwd` is retained or can be overridden. A missing/nonexistent directory produces a warning. Claude's encoded directory name can collide. |
| Model/provider metadata | **Lossy** | **Lossy** | Codex provider and Claude target model use target defaults/options. Turn-specific model, policy, sandbox, and token metadata are not reconstructed. |
| Conversation title | **Unsupported** | **Supported** | A Codex `thread_name_updated` title becomes Claude `custom-title`. Claude titles are not emitted into Codex and are not yet itemized in the manifest. |
| Tool call name, input, and ID | **Supported** | **Supported** | Claude `tool_use` maps to Codex `function_call`; JSON argument strings are parsed when valid. Missing IDs/names receive explicit synthetic fallbacks. |
| Text tool result | **Supported** | **Supported** | Call linkage and text output are retained. |
| Image tool result | **Supported** | **Supported** | Claude base64 sources normalize to self-contained data URLs; remote URLs remain URLs. |
| Tool error status | **Lossy** | **Lossy** | Codex has no emitted equivalent for Claude `is_error`; the output remains and `tool_result:is_error` is reported. Codex input does not restore an error flag in Claude. |
| Tool reference result block | **Unsupported** | **Supported when present** | Claude tool references have no emitted Codex equivalent and are counted. A Codex structured output block explicitly typed `tool_reference` can be emitted as Claude `tool_reference`. |
| Other structured result blocks | **Lossy/unsupported** | **Lossy/unsupported** | Target-equivalent text/images are retained. Audio, encrypted content, and unknown nested blocks are not portable; inventory is incomplete for unrecognized nested blocks. |
| Standalone message image | **Lossy** | **Lossy** | Image bytes/URL are retained as `input_image` or Claude image source, but original block grouping is not guaranteed and images are emitted with user role. Invalid data URLs are dropped and counted. |
| Document block or top-level attachment | **Unsupported** | **Unsupported** | Claude document events are dropped and counted as context. Standalone attachment graph records are opaque. Local files are never copied. |
| Claude compaction summary | **Lossy** | N/A | Summary text becomes a Codex `compacted` message; compact-boundary bookkeeping is dropped and counted. Full selected text/tool history remains present. |
| Codex compaction | N/A | **Unsupported** | `compacted.message` is recognized but omitted from Claude output and counted. Replacement-history/window metadata is not reconstructed. |
| Thinking/reasoning | **Unsupported by design** | **Unsupported by design** | Private thinking or reasoning content is never transferred. A content-free event is counted as dropped. |
| Active Claude branch | **Supported** | N/A | `last-prompt` ancestry selects one coherent branch. Target output is linear. |
| Inactive Claude branches | **Unsupported** | N/A | They become opaque events and are counted as dropped; no forks are created. |
| Claude sidechains/subagents | **Unsupported** | N/A | Top-level sidechain records are excluded/opaque; nested subagent files are not discovered recursively. |
| Codex legacy linear history | N/A | **Supported** | Ordered response items become one linear Claude UUID graph. |
| Codex paginated history/forks | N/A | **Unsupported** | `ordinal`, `history_base`, fork coupling, and archive state are not reproduced. The reader processes visible records in file order only. |
| Codex UI-only messages | N/A | **Lossy fallback** | Used only if the rollout has zero response-item messages. In a mixed partial file, UI-only messages can therefore be omitted without an itemized warning. |
| Turn context, policies, world state, snapshots | **Unsupported** | **Unsupported** | Codex `turn_context` is counted as dropped context. `world_state` and `security_risk_score` are ignored. Shell snapshots, approvals, credentials, MCP state, memories, goals, and configuration are outside transcript conversion. |
| Unknown source records/blocks | **Unsupported** | **Unsupported** | Most become opaque events and are counted at write time. A few explicitly ignored Codex state records and unknown nested tool-result blocks cannot yet be fully inventoried. |

## What “resumable” means

The bridge produces a new native conversation containing the portable visible
history, not a byte-for-byte clone of the original runtime. It does not transfer
authentication, pending permissions, live processes, shell state, task plans,
MCP connections, agent teams, or model caches. Tool calls and results provide
historical context; they are not re-executed.

Imports never mutate the source or overwrite an existing target. The native
file and a content-free provenance/omission manifest are each created through
an atomic rename with mode `0600`; if manifest creation fails after the new
native file is created, that new file is removed. Explicit UUID resume is the
authoritative integration check; picker ordering and previews can vary by CLI
version and current working directory.
