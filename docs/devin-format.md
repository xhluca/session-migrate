# Devin CLI session format

`session-migrate` reads and writes native local Devin CLI sessions. This
adapter is pinned to Devin CLI `3000.6.7` (`260a97c8`) and its SQLite schema
version 16. The observations below were repeated on 2026-08-30.

Public references:

- [Devin CLI commands](https://docs.devin.ai/cli/reference/commands)
- [Current official release manifest](https://static.devin.ai/cli/current/manifest.json)
- [Agent Sessions' independent Devin reader](https://github.com/jazzyalex/agent-sessions)

## Pinned binary

The official Linux x86-64 archive used for the native gate was:

```text
URL     https://static.devin.ai/cli/3000.6.7/devin-3000.6.7-x86_64-unknown-linux.tar.gz
bytes   57,776,222
sha256  f88edacea692553910d72f275515bd0b52b5d271d55250981b0c41011142d27b
```

The extracted executable reported `devin 3000.6.7 (260a97c8)`:

```text
bytes   174,094,920
sha256  862623068229249a5ac5a560d876532a40bb53fe16049ab7e415ac5d6b8ae36d
```

`verify_pinned_binary()` checks both the executable size and digest. A new
Devin release is not assumed compatible until its database and native gate
have been checked and these pins have deliberately changed.

## Native storage and identity

Devin uses one shared `sessions.db`, rather than one transcript file per
conversation:

| Platform | Default database |
| --- | --- |
| Linux | `$XDG_DATA_HOME/devin/cli/sessions.db`, otherwise `~/.local/share/devin/cli/sessions.db` |
| macOS | `~/Library/Application Support/devin/cli/sessions.db` |
| Windows | `%APPDATA%/devin/cli/sessions.db` |

A conversation's identity is therefore the pair `(database path,
sessions.id)`. Native IDs are case-sensitive human-readable ASCII slugs such
as `fix-timeline-merging`; they are not necessarily UUIDs. The accepted ID
grammar is deliberately bounded to 1–128 letters, digits, underscores, or
hyphens, beginning with a letter or digit.

The official `devin list --format json` only shows sessions whose
`working_directory` matches the current directory. Catalog discovery reads
the shared database itself so that it can find every visible session across
projects. Rows with `hidden = 1` are excluded. Native resume is:

```bash
devin --resume fix-timeline-merging
```

## Schema 16

The exact table/column order initialized by CLI `3000.6.7` is validated before
any read or write:

| Table | Columns |
| --- | --- |
| `sessions` | `id`, `working_directory`, `backend_type`, `model`, `agent_mode`, `created_at`, `last_activity_at`, `title`, `main_chain_id`, `shell_last_seen_index`, `cogs_json`, `workspace_dirs`, `hidden`, `metadata` |
| `message_nodes` | `row_id`, `session_id`, `node_id`, `parent_node_id`, `chat_message`, `created_at`, `metadata` |
| `prompt_history` | `id`, `content`, `timestamp`, `session_id`, `is_shell` |
| `tool_call_state` | `session_id`, `tool_call_id`, `tool_call_json`, `tool_call_update_json` |
| `rendered_commits` | `id`, `session_id`, `sequence_number`, `rendered_html`, `created_at` |
| `app_state` | `key`, `value` |
| `refinery_schema_history` | `version`, `name`, `applied_on`, `checksum` |

The adapter also validates every migration name and checksum from version 1
through 16, not only `MAX(version)`. Native timestamps are integer epoch
seconds. `workspace_dirs`, session `metadata`, and each `chat_message` are JSON
stored in text columns.

`message_nodes` is a forest. A session's `main_chain_id` identifies the tip of
the active branch, so parsing walks parent links from that tip back to the
root. Retried or edited-away nodes can remain in the database and must not be
silently spliced into the active conversation. Missing parents, cycles,
duplicate nodes, malformed JSON, invalid roles, and orphan tool results fail
closed.

Observed native `chat_message` roles and payloads are:

- `user`: string content and optional inline images containing dimensions and
  base64 bytes;
- `assistant`: string content, optional signed `thinking`, and function calls
  containing `id`, `index`, `name`, and object `arguments`;
- `tool`: string content linked by `tool_call_id`; and
- `system`: runtime context that is retained as an opaque portable event.

The adapter hashes only the selected session metadata and active chain. Adding
another conversation to the shared database therefore does not change a
session's source fingerprint.

## Portable mapping

| Native Devin history | Portable projection | Devin target behavior |
| --- | --- | --- |
| user/assistant string content | ordered message | written as native user/assistant nodes |
| user inline image | user image context | embedded as bounded native base64 image data |
| assistant function call | linked tool call | written into the assistant's `tool_calls` |
| tool message | linked tool result | written as a native tool node |
| signed assistant thinking | private thinking event | omitted on import because another provider's signature cannot be fabricated |
| system message | opaque runtime event | not promoted to target runtime instructions |
| compaction summary from another harness | compaction event | flattened to a clearly labeled non-runtime user history message |
| inactive forest branch | not projected | not written |

The flattened compaction node carries a private `session-migrate` metadata
marker. Reading that generated Devin session projects the node back to a
portable compaction event and removes the display prefix, so repeated portable
rewrites do not alternate between a compaction and an ordinary user message.
An unmarked native user message with the same visible text remains an ordinary
user message.

Every lossy transformation is counted in the conversion manifest. The writer
adds a small system marker, retains the title, cwd, source model selector,
ordered message history, prompt history, and active-chain links. It does not
invent cogs, pending tool UI state, signed thinking, authentication, permission
policy, MCP configuration, or workspace files. Completed calls stay visible to
the model through `message_nodes`; `tool_call_state` remains empty.

## Installation

Serialization produces one bounded `session-migrate.devin.v1` JSON bundle,
not a copy of the user's shared database. Installation validates the entire
bundle before opening a target and then inserts its session, nodes, prompts,
and tool state in one SQLite transaction.

- Existing session IDs are never replaced.
- Existing databases must match the pinned schema and migration checksums.
- A missing store is staged as an exact schema-16 WAL database, checkpointed,
  and atomically published with mode `0600` inside mode-`0700` directories.
- Symlinked database files or unsafe path prefixes are rejected.
- `dry_run=True` performs bundle, schema, path, and collision validation
  without creating or mutating a file or directory.

The JSON, database, active-chain, message, and image limits are enforced before
materializing portable events.

## Native validation

The opt-in native test uses the exact pinned executable with fresh mode-`0700`
home, data, config, cache, runtime, temporary, and project directories. It
constructs a small environment rather than inheriting API keys or Devin login
state and disables automatic updates. The test:

1. lets the real `devin list --format json` initialize its empty database;
2. installs three distinct sessions with titles, cwd values, messages, and
   linked successful tool calls/results;
3. proves the real CLI lists each imported identity from its matching cwd;
4. enumerates all three through the adapter and reparses every active chain;
5. invokes the real `--resume` path with stdin closed; and
6. verifies that the imported chain remains byte-semantically unchanged when
   the credential-free binary stops at login.

Run it with:

```bash
SESSION_MIGRATE_DEVIN_BIN=/path/to/devin \
  uv run pytest tests/test_devin_native.py
```

The normal unit suite mechanically covers bundle round trips, native schema
creation, multiple logical identities, active-versus-abandoned branches,
images, tools, omission counters, deterministic fingerprints, dry-run
behavior, collisions, permissions, malformed graphs and JSON, schema drift,
and size limits.

## Verified boundaries

The repeatable CI gate is deliberately credential-free. On `3000.6.7` it
initializes and reads the local store, selects an imported resume ID, and then
stops at Devin's real authentication boundary without inheriting a user's
credential file or spending model credits.

A separate manual gate on 2026-08-31 authenticated the exact pinned binary
with a disposable Devin Free account through its browser PKCE flow. A native
non-interactive request completed with Devin's default Free-plan model
(`swe-1-6-slow`) and persisted a 28-node session in the normal schema-v16
database. `smigrate inspect` followed that session's active ancestry and a
Devin→Claude conversion preserved the unique user/assistant marker. This
establishes authenticated native session production and subsequent migration,
in addition to the deterministic imported-session checks above.

The credential remains only in Devin's normal local credential store. It is
not copied into fixtures, documentation, tests, or CI. Re-running the live
gate is intentionally manual because it consumes account quota.
