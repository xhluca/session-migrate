# Architecture

`session-migrate` converts native coding-agent histories without treating a
terminal transcript as an interchange format.

```text
native source
    │ bounded parse + identity checks
    ▼
ordered portable event timeline
    │ target-specific projection + loss accounting
    ▼
validated native target + content-free manifest
```

The source is always authoritative and never modified. A target is a new,
independent conversation.

## Layers

| Layer | Responsibility |
| --- | --- |
| `model.py` | Agent/target enums and neutral `Session`/`Event` types |
| `formats/*.py` | One bounded native reader/writer per agent |
| `inspection.py` | Content-free format detection and structural inventory |
| `discovery.py` | Exact native-ID lookup without trusting mutable indexes |
| `catalog.py` | Private multi-root metadata index and title/ID search |
| `conversion.py` | Dispatch, portable rewrite, validation, manifests, installation |
| `cli.py` | User-facing commands and JSON/error contracts |

The neutral event kinds are message, tool call, tool result, thinking,
compaction, context, and opaque. Roles are user, assistant, system, and tool.
Each event retains source record ordinal/type and, where available, source IDs
and block ordinals. The model is intentionally small: it represents portable
conversation history, not an agent's entire runtime.

## Read paths

### JSON and JSONL sources

Claude, Codex, Pi, OMP, Copilot, Vibe, Muse, Qwen, and Kimi messages are bounded
JSON/line streams. Readers cap total
bytes, record bytes, record count, JSON nesting/nodes, and media payloads. They
validate source identity before and after reading so an actively appending,
replaced, or truncated file fails with a retryable error.

- Claude reconstructs the active UUID ancestry selected by `last-prompt`,
  validates compaction back-edges, and excludes inactive branches/meta prompts.
- Codex replays canonical legacy `response_item` history, deduplicates UI
  projections, and rejects paginated/history-base lineage.
- Pi follows the v3 `id`/`parentId` active tree and rejects unsupported schema
  versions.
- OMP follows its v3 active tree after validating the fixed 256-byte title
  slot, honors native reset boundaries, and safely resolves hashed image blobs.
- Copilot validates its schema-v1 event envelope, root agent, assets, and tool
  linkage.
- Vibe snapshots `meta.json` and `messages.jsonl` together, validates the
  documented `LLMMessage` shape, and projects readable reasoning, tools,
  images, compaction, and injected-runtime omissions.
- Muse validates its durable metadata, retained markers, and linked
  intent→run→materialization lifecycle before projecting committed events.
- Qwen follows the active UUID/parent chat graph and counts inactive branches.
- Kimi snapshots `state.json` and the main-agent protocol-`1.5` wire journal
  together before projecting context events.

### OpenCode virtual sources

The catalog reads only native session metadata from `opencode.db`. A selected
source is exported through exact pinned `opencode export`, parsed as an official
bundle, and represented by the virtual path `opencode:<id>`. The migrator never
queries message/part tables or writes OpenCode SQLite.

### SQLite/protobuf sources

Antigravity and Cursor readers make a consistent SQLite backup that includes
committed WAL state. They then validate exact table/index/user-version
contracts and decode protobuf wire structures through independently written,
bounded codecs.

- Antigravity projects user/planner messages and observed generic tool
  steps/results.
- Cursor walks SHA-256-addressed root→turn→user/assistant blobs and projects
  text only. Every unsupported native occurrence becomes a reason-specific
  opaque event so a later target manifest cannot silently claim losslessness.

## Write paths

Writers consume the same ordered event timeline and return `(native_bytes,
loss_counters)`. No writer reads another source format directly.

| Target | Native artifact |
| --- | --- |
| Claude | UUID-linked project JSONL |
| Codex | Legacy rollout JSONL with canonical response items and UI projection |
| Pi | v3 session JSONL tree |
| Oh My Pi | v3 title-slot session JSONL tree |
| OpenCode | Official JSON import bundle |
| Copilot | Schema-v1 event JSONL plus workspace sidecar |
| Antigravity | Complete trajectory SQLite/protobuf DB plus picker summary on install |
| Cursor | Complete content-addressed SQLite/protobuf DB, text only |
| Vibe | Native `meta.json` plus `messages.jsonl` session directory |
| Muse | Date-partitioned durable session event JSONL |
| Qwen | Project-scoped append-only chat graph JSONL |
| Kimi | Native `state.json` plus main-agent `wire.jsonl` session directory |

Every generated artifact is reparsed/validated before publication. Target
required IDs, timestamps, and metadata may be synthesized. Source tool output,
reasoning, or system instructions are never invented. Known transformations
and omissions are counted.

OpenCode IDs and timestamps are made monotonically sortable because its runtime
pages history by `(time_created, id)`. Claude emits a fresh linear parent chain.
Codex emits legacy history rather than synthesizing the more coupled paginated
projection. Cursor groups assistant steps beneath the most recent user turn and
rejects histories with no portable user message.

## Same-format migration

Same-format is not a fast copy. It deliberately runs reader→portable
model→writer, allocates a new target identity, and emits
`same_format_portable_rewrite`. This removes source-only runtime state just as a
cross-format route would. It is useful for moving between homes or normalizing
an older supported transcript, but it is not byte-identical and does not keep
the two sessions synchronized.

## Native installation

Filesystem targets use no-clobber publication. New files are mode `0600`; new
state directories are mode `0700`; existing directory permissions are not
silently changed. The source is never overwritten.

- Claude/Codex/Pi/OMP write one native transcript and one manifest atomically.
- Muse/Qwen write one native transcript and one manifest atomically.
- Kimi reserves a native session directory and publishes its state, wire
  journal, and manifest with rollback guards.
- Vibe reserves a short-ID-safe native directory and atomically publishes its
  metadata, message stream, and manifest.
- Copilot reserves the complete session directory and writes events, workspace
  sidecar, and manifest.
- OpenCode reserves a private external manifest, invokes only the official
  pinned importer, confirms the ID through official listing, then finalizes the
  manifest.
- Antigravity and Cursor reserve the manifest, verify the exact pinned binary,
  invoke their clean-room atomic database installers, validate the installed
  session, then finalize the manifest.

If failure occurs after an external/native installer succeeds, the error says
that the session may already exist. Blind retry is intentionally avoided.

## Version boundaries

Claude/Codex writers are pinned to the local integration image; Pi, OMP, OpenCode,
Copilot, Antigravity, Cursor, Vibe, Muse, Qwen, and Kimi to exact host
builds/releases. A source declaring a
different version produces `unvalidated_source_version`. A
`--target-cli-version` override changes metadata only and produces
`unvalidated_target_version`; it never changes writer architecture.

Automatic OpenCode, Antigravity, and Cursor installation is stricter: metadata
overrides cannot bypass exact runtime version checks. Antigravity verifies its
binary digest. Cursor verifies launcher, main bundle, protobuf-bearing chunk,
bundled Node, sizes, SHA-256 values, and reported version.

Cursor remains experimental because only text history has passed the native
loader/TUI/backend-blob gates and a real authenticated assistant checkpoint
followed by a second resume has not been proven.

## Catalog architecture

The catalog is a derived, private SQLite database. It stores roots, scan runs,
session metadata, and bounded labels—never message/tool/media bodies. Native
files/rows remain authoritative.

Enumeration covers:

- Claude main sessions and nested sidechains;
- Codex active and archived rollouts;
- Pi and OMP workspace buckets, classified by their native heads;
- every OpenCode `session` row, including parents/archives;
- Copilot session directories, including missing event logs;
- Antigravity conversation DBs; and
- Cursor workspace/chat DBs, including missing stores;
- Vibe and Kimi multi-file session directories;
- Muse date-partitioned event streams; and
- Qwen project chat graphs.

JSONL rows use stat identity. Vibe and Kimi fingerprint both native files.
Antigravity/Cursor include DB/WAL/SHM fingerprints.
OpenCode rows use a fingerprint of every indexed metadata field. Unavailable
roots retain prior rows instead of falsely marking everything missing.

Search covers native names/titles and IDs. Paths/CWDs are opt-in. “All sessions”
means all recognized sessions in auto-selected, registered, or explicitly
bounded-discovered roots; arbitrary whole-disk discovery is neither safe nor
honest.

## Manifest semantics

Schema-v2 manifests contain migration/source/target identities, paths, hashes,
versions, structural counts, `dropped_events`, and warning objects. They contain
no message/tool/media bodies. The historical `dropped_events` name includes:

- data that was omitted;
- data that was transformed or grouped; and
- inconsistent records retained with a warning.

Non-empty counters do not necessarily mean a whole event vanished. Operators
should inspect warning keys before resume.

## Data and credential boundary

The project does not read or copy login stores, cookies, API keys, shell state,
pending approvals, processes, MCP connections, memories, or workspace files.
Native validation may use isolated test-only credential copies when a target
technically accepts the same account/provider schema; that is not a migration
feature.

There is no redaction, encryption, or secret scanning. A secret embedded in a
supported message, tool argument/result, or image is copied into the target.
Treat sources, targets, manifests, catalog metadata, and CLI JSON as sensitive
according to their contents.

## Adding a format

A new adapter needs:

1. a sanitized native-shaped fixture;
2. strict bounded parsing and generated-byte validation;
3. explicit loss counters for every unsupported semantic class;
4. conversion, detection, discovery, catalog, and CLI integration;
5. all-routes semantic and loss-accounting tests; and
6. an actual native load/resume oracle at an exact pinned version.

Private-format work must be clean-room and publish only independently observed
descriptions and synthetic generators—not vendor code, binaries, descriptors,
credentials, or real transcripts.
