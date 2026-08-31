# MastraCode native session format

This note records the reverse-engineering and native validation behind
session-migrate's MastraCode adapter. It describes the local LibSQL backend used
by the official CLI; remote LibSQL and PostgreSQL stores are outside the current
adapter.

## Pinned implementation

Research and tests were completed on 2026-08-30/31 against the official
[`mastra-ai/mastra`](https://github.com/mastra-ai/mastra) repository and npm
package:

| Item | Pin |
|---|---|
| npm package | `mastracode@0.37.1` |
| Git tag | `mastracode@0.37.1` |
| source commit | `003e75745c5fd6a7af8464ece1d2930f81dd15af` |
| npm tarball | 1,557,550 bytes; SHA-256 `1ebdf39c630469d5a7c635c60cff339679d8a1a1c3a5647375133cfb5da0c0e9` |
| installed `dist/cli.js` | 10,526 bytes; SHA-256 `9921609cd35cb9dc91c8a2ae5d606d937d904404f084b89d7b9739cca260f35b` |
| Node requirement | `>=22.19.0` |

The source paths used to corroborate behavior were
`mastracode/sdk/src/utils/project.ts`, `mastracode/sdk/src/headless/cli.ts`,
`mastracode/sdk/src/headless/run-mc.ts`, the session/controller implementation,
and the storage implementation pinned by the package dependencies
(`@mastra/core@1.63.2`, `@mastra/libsql@1.22.2`, and
`@mastra/memory@1.28.1`).

## Native research procedure

The package, exact source tag, test homes, app-data roots, projects, and npm
prefix were created under one fresh mode-`0700` temporary directory. Package
installation used the published tarball and did not reuse credentials. An
OpenAI-compatible loopback server derived from MastraCode's own render-smoke
fixture supplied deterministic responses.

Two independent native sessions were created with the official CLI before the
adapter was written:

1. A titled, tool-free session containing a unique user marker and assistant
   response.
2. An untitled session containing a unique user marker, repeated
   `execute_command` tool calls, large arguments, successful results, internal
   observational-memory parts, and a max-turn termination.

Both were inspected through SQL, the pinned TypeScript source, the official
headless resume path, and session-migrate's parser. A third trajectory, now the
opt-in native test, starts from a session-migrate artifact and proves that the
exact 0.37.1 CLI:

- selects the imported UUID with `--thread`;
- replays imported user, assistant, tool call/result, and compaction markers to
  a fresh loopback provider;
- appends the follow-up and provider response to the same thread; and
- initializes its remaining runtime tables around the imported minimal store.

No API tokens are consumed by this trajectory.

## Discovery and paths

MastraCode stores all local conversations in one database rather than one file
per session. The active local database has this precedence:

1. `MASTRA_DB_PATH`
2. `${MASTRA_APP_DATA_DIR}/mastra.db`
3. the platform app-data path:
   - Linux: `${XDG_DATA_HOME:-~/.local/share}/mastracode/mastra.db`
   - macOS: `~/Library/Application Support/mastracode/mastra.db`
   - Windows: `%APPDATA%/mastracode/mastra.db`

`list_sessions(path)` expands the central database into one content-free
inventory item per `mastra_threads` row. Each item contains the thread UUID,
title, project path, creation time, pinned/native CLI version, update time in
nanoseconds, and message count. Catalog integrations should therefore expose
virtual MastraCode entries keyed by thread UUID instead of presenting the
entire database as one conversation.

Local inventory and parsing use SQLite's backup API so committed WAL content is
included in one consistent snapshot. The concurrency fingerprint covers the
database and WAL. It intentionally excludes `-shm`, whose mtime changes when a
reader merely acquires a WAL lock.

Remote LibSQL (`MASTRA_DB_URL`) and PostgreSQL (`MASTRA_STORAGE_BACKEND=pg`) are
supported by MastraCode itself but are not silently accessed by
session-migrate.

## Native resume

The supported exact resume form is:

```bash
mastracode --thread <UUID> --resource-id <RESOURCE> --prompt "Continue..."
```

MastraCode scopes thread lookup by resource ID. session-migrate preserves the
native resource ID when reading and derives the default target resource from
the target working directory using the official project identity algorithm.
That algorithm prefers the normalized Git remote, handles worktrees, and falls
back to the resolved path; the visible prefix is a slug plus the first twelve
hex digits of SHA-256.

Native title search is not a resume primitive. Title matching belongs in
session-migrate's catalog; after selection it must pass the exact thread UUID.

MastraCode 0.37.1 also has an ordering detail relevant to deterministic
headless tests: `--model` is applied before `--thread`, and switching threads
then restores `currentModelId` from thread metadata. Consequently,
`serialize(..., model=<target-model>)` seeds both `currentModelId` and
`modeModelId_build`. Normal user migrations can omit `model` and use the
target's configured/default model.

## Database schema

The 0.37.1 local store uses WAL journal mode. Conversation identity and history
are in these two tables:

```text
mastra_threads(
  id TEXT PRIMARY KEY,
  resourceId TEXT NOT NULL,
  title TEXT NOT NULL,
  metadata TEXT,
  createdAt TEXT NOT NULL,
  updatedAt TEXT NOT NULL
)

mastra_messages(
  id TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL,
  content TEXT NOT NULL,
  role TEXT NOT NULL,
  type TEXT NOT NULL,
  createdAt TEXT NOT NULL,
  resourceId TEXT
)
```

Despite the declared `TEXT` affinity, `mastra_threads.metadata` is normally a
SQLite JSONB BLOB. The adapter has a bounded standard-library JSONB codec so it
does not require SQLite 3.45 or a nonstandard Python extension. It reads
`projectPath`, native model fields, and session-migrate metadata while ignoring
unknown keys.

`content` is UTF-8 JSON with format version 2:

```json
{
  "format": 2,
  "parts": [
    {"type": "text", "text": "..."}
  ]
}
```

Observed native roles were `signal`, `user`, and `assistant`; normal imported
history uses `user`, `assistant`, or `system`. Native user input created by the
headless CLI is commonly a `signal` role with `type: "user"` and signal
metadata. Both shapes project to a portable user message.

Observed part mappings are:

| MastraCode part | Portable event |
|---|---|
| `text` | message, or compaction when tagged |
| `reasoning` | readable thinking |
| `tool-invocation` with `state: call` | tool call |
| `tool-invocation` with `state: result` | tool call followed by tool result |
| `file` with an image data URL | image context |
| `data-user-message` | user message |
| runtime/UI/OM parts | opaque evidence |

MastraCode folds a completed tool call and its result into the same assistant
part. session-migrate unfolds it when reading and folds a matching portable
call/result pair when writing. Orphan tool results are omitted because there is
no valid native invocation to attach them to.

## Observational memory and compaction

Native observational memory lives in `mastra_observational_memory`; its
`activeObservations` field is the currently usable compacted context. A
non-empty value is projected as a portable compaction event. Runtime buffering,
reflection bookkeeping, token counters, and internal `om-continuation`
messages remain opaque.

The target artifact does not synthesize MastraCode's observational-memory state
machine. Portable compaction is instead stored as a tagged user history row.
This matters because 0.37.1 excludes historical system-role rows when building
the next provider request. The tag restores the row to `COMPACTION` when parsed,
while the user role ensures the summary is actually replayed during native
resume. MastraCode initializes and evolves its own OM tables after launch.

## Artifact and installation

The serialized artifact is a small SQLite database with exactly one thread and
its messages. It is not a byte-copy of the user's shared store. Validation
requires:

- the pinned table/column layout and exactly one requested UUID;
- SQLite integrity, valid RFC 3339 timestamps, and consistent thread/resource
  links;
- bounded file, metadata, message, JSON depth, and record counts;
- format-2 content with supported generated parts;
- unique message and tool-call IDs; and
- no symbolic-link database target.

Installation never replaces an existing central database. It validates the
artifact first, then inserts its thread and messages inside `BEGIN IMMEDIATE`.
An existing UUID is rejected unless overwrite was explicitly requested. A new
database is created mode `0600` below mode-`0700` directories.

## Preservation

| Data | Result |
|---|---|
| user/assistant text | preserved |
| session title and working directory | preserved |
| timestamps | preserved and monotonically normalized when needed |
| tool names, IDs, object arguments, results, error flag | preserved/folded into native invocation parts |
| image data URLs | preserved |
| portable compaction summary | preserved as replayable tagged history |
| native active observations | read as compaction |
| native readable reasoning | readable on source; only rewritten for MastraCode-origin history |
| private/encrypted thinking | omitted |
| orphan tool results | omitted and counted |
| internal OM/runtime/UI parts | retained only as opaque evidence while reading |
| OM buffering/reflection state and token accounting | rebuilt by MastraCode |
| live harness-session ownership/locks | not migrated |
| remote LibSQL/PostgreSQL stores | not accessed |

## Tests

Credential-free format tests run in the default suite:

```bash
uv run pytest -q tests/test_mastracode_format.py tests/test_mastracode_native.py
```

The native test is opt-in because it requires the pinned npm installation, but
still uses only its local loopback provider:

```bash
SESSION_MIGRATE_MASTRACODE_BIN=/path/to/mastracode/dist/cli.js \
  uv run pytest -q tests/test_mastracode_native.py -vv
```

The test checks the CLI file size/hash and package version before executing, so
an unreviewed upstream version cannot accidentally satisfy the trajectory.
