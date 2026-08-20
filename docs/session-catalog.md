# Native session catalog

The catalog finds and searches native Claude Code, Codex CLI, Pi, OpenCode, and
GitHub Copilot CLI sessions across more than one agent home. Native JSONL files
remain authoritative for file-based formats. OpenCode's read-only `session`
table is its authoritative inventory. The catalog is a private, disposable
SQLite index; it never changes any agent's session store.

## What “all sessions” means

An exhaustive refresh means **every recognized native session below every
enabled catalog root**: every expected JSONL, including missing Copilot event
logs, plus every OpenCode `session` row. It does not mean an implicit whole-disk
crawl. Agent homes can have arbitrary names and locations, so discovering all
of them still requires either a known root or an explicit search boundary.

The catalog adds these roots automatically when they exist:

- `~/.claude`, `~/.codex`, `~/.pi/agent`, and `~/.copilot`, even if an
  environment override selects another home;
- `$XDG_DATA_HOME/opencode`, or `~/.local/share/opencode` when `XDG_DATA_HOME`
  is unset;
- `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `PI_CODING_AGENT_DIR`, and `COPILOT_HOME`;
  and
- `.claude`, `.codex`, `.pi/agent`, or `.copilot` native homes in the current
  directory or one of its ancestors.

Use the repeatable `--claude-root`, `--codex-root`, `--pi-root`,
`--opencode-root`, or `--copilot-root` option for arbitrary custom homes. These
roots persist for later refreshes. Use repeatable `--discover-under DIRECTORY`
to find project-local file-based homes below a specific workspace. Discovery
does not follow directory symlinks, stops descending once it finds a native
home, and never searches outside the supplied directory.

Claude enumeration includes both:

```text
HOME/projects/<encoded-cwd>/<session-uuid>.jsonl
HOME/projects/<encoded-cwd>/<parent-uuid>/subagents/agent-<id>.jsonl
```

Codex enumeration includes every JSONL below both:

```text
HOME/sessions/
HOME/archived_sessions/
```

Pi enumeration includes every v3 JSONL below:

```text
HOME/sessions/
```

Copilot enumeration includes every immediate native session directory, even
when its event log is corrupt, missing, or a refused symlink:

```text
HOME/session-state/<session-uuid>/events.jsonl
```

OpenCode enumeration opens `HOME/opencode.db` with SQLite `mode=ro` and
`query_only`, then projects only these `session` columns:

```text
id, title, directory, version, time_created, time_updated,
parent_id, time_archived
```

It does not run `opencode export` per row or inspect `message`/`part` tables.
This keeps a 70,000-session refresh proportional to the small inventory table,
not the total transcript corpus.

Consequently, archived sessions, duplicate UUIDs, nested sidechains/subagents,
malformed files, and absent Copilot event logs remain discoverable. Claude
sidechains and Codex paginated/history-base sessions are listed as
`unsupported`; listing them does not make them convertible.

## Quick start

The default database is
`$XDG_STATE_HOME/session-migrate/catalog.sqlite3`, or
`~/.local/state/session-migrate/catalog.sqlite3` when `XDG_STATE_HOME` is unset.
`SESSION_MIGRATE_CATALOG` or the global `--catalog PATH` option overrides it.
The current schema is version 4. Opening a v1, v2, or v3 catalog migrates it
transactionally and preserves configured roots, indexed sessions, labels, and
opaque catalog IDs. Schema v4 expands roots to OpenCode and Copilot and adds a
metadata fingerprint for virtual inventory rows without rewriting any native
store.

```console
# Index all existing automatic and previously registered roots.
session-migrate catalog refresh

# Add custom homes immediately and persist them.
session-migrate catalog refresh \
  --claude-root /agent-homes/claude-one \
  --claude-root /agent-homes/claude-two \
  --codex-root /agent-homes/codex \
  --pi-root /agent-homes/pi \
  --opencode-root /agent-homes/opencode \
  --copilot-root /agent-homes/copilot

# Find project-local homes within an explicit workspace boundary.
session-migrate catalog refresh --discover-under /workspaces

# Search native names/titles and UUIDs. Paths are hidden by default.
session-migrate catalog search "release investigation"
session-migrate catalog search 12345678 --format codex --json

# Inspect one exact result, then transfer it.
session-migrate catalog show CATALOG_ID --include-paths
session-migrate transfer --catalog-id CATALOG_ID --to TARGET --dry-run
```

`catalog list`, `catalog search`, and `catalog show` expose an opaque
`catalog_id`. It selects one physical JSONL even when several roots contain the
same native UUID. For OpenCode it selects a virtual `(root, native session ID)`
reference instead of pretending `opencode.db` is an export bundle. Transfer
then invokes the official OpenCode exporter for that one ID. File-based sources
are reopened and authoritatively parsed before conversion; an index status
never bypasses normal conversion validation.

## Commands and filters

```text
session-migrate catalog refresh
    [--claude-root HOME]... [--codex-root HOME]... [--pi-root HOME]...
    [--opencode-root HOME]... [--copilot-root HOME]...
    [--discover-under DIRECTORY]... [--no-auto-roots] [--validate] [--json]

session-migrate catalog roots list [--json]
session-migrate catalog roots add PATH --format claude|codex|pi|opencode|copilot [--json]
session-migrate catalog roots remove ROOT_ID

session-migrate catalog list [FILTERS] [--json]
session-migrate catalog search QUERY [FILTERS] [--json]
session-migrate catalog show CATALOG_ID [--include-paths] [--json]
```

List/search filters are repeatable `--status STATUS` and `--kind KIND`, plus
repeatable `--lifecycle project|active|archived`, a source `--format`,
timezone-aware RFC-3339 `--since`/`--until`, `--include-missing`, `--limit`, and
`--offset`.
Search is a case-insensitive substring match over:

- native metadata UUID and a structurally valid filename UUID (the latter keeps
  a malformed or partially written candidate findable);
- Claude `custom-title` and `ai-title` values;
- Codex `thread_name_updated` names and the native SQLite thread `name` and
  `title` fields; and
- Claude sidechain `agentId` and `agent-<id>` filename keys;
- Pi `session_info.name` values and native session IDs;
- OpenCode native session IDs and bounded `session.title` values; and
- Copilot session IDs, `session.title_changed` values, and bounded picker names
  from `workspace.yaml`.

Each stored native label is bounded to 512 Unicode code points. This prevents a
vendor field containing an unexpectedly long prompt-like title from making the
derived database unbounded; search beyond that prefix is intentionally not
supported.

Search does **not** inspect prompts, responses, first-user-message fields,
previews, tool names, arguments, results, images, or arbitrary message content.
Use `--include-paths` to additionally search and print source paths and working
directories. Paths and CWDs are intentionally omitted without that flag.

`catalog roots remove` removes only the root and its catalog rows. It never
deletes a native agent file. Automatic roots are added again by a future
automatic refresh; use `--no-auto-roots` when refreshing only explicitly
registered roots.

## Status model

| Status | Meaning |
| --- | --- |
| `candidate` | Fast structural metadata scan passed; full conversion has not been requested. OpenCode rows remain candidates until their one-session official export is parsed. |
| `validated` | The exact stat identity was fully parsed, dry-converted, and target-validated during `refresh --validate`. |
| `unsupported` | The file is a recognized session type intentionally rejected by conversion, such as a Claude sidechain or Codex paginated/history-base rollout. |
| `corrupt` | JSONL, native structure, or explicit conversion validation failed. |
| `oversized` | The source exceeds the migrator's bounded input limits. |
| `busy` | The source changed while it was being scanned; retry after the native CLI finishes appending. |
| `unreadable` | The candidate could not be read with current permissions. |
| `missing` | It disappeared after a successful scan, or a Copilot session directory has no `events.jsonl`. |

Malformed or partially written files retain any safe metadata extracted before
the failure, but are never advertised as validated. A root that is unavailable
does not cause all its entries to become `missing`: the previous rows remain and
the root records a failed scan. Missing rows are hidden unless
`--include-missing` is supplied.

Duplicate UUIDs are preserved as separate physical entries and carry
`duplicate: true`. Missing rows do not make a live row a duplicate. Use the
opaque catalog ID to select deliberately instead of asking UUID discovery to
guess.

## Incremental refresh and validation

File refresh compares device, inode, byte size, and nanosecond modification
time. OpenCode refresh fingerprints every indexed metadata field per session,
so a title, parent, archive state, version, CWD, or timestamp change is detected
even if a third-party writer fails to advance `time_updated`. An unchanged
source reuses its structural result. The JSONL scanner checks the source
snapshot again after reading it so an actively changing transcript becomes
`busy`. A successful root scan marks disappeared sources missing. Failed root
scans retain prior state.

The fast default streams every changed JSONL because title records can occur
late in a transcript. It does not materialize message bodies, but an initial
refresh still performs I/O proportional to the total bytes in all configured
session stores. Native Codex SQLite is used only to add `name`, `title`, and
spawn-lineage metadata; it is not trusted as inventory because it can omit
rollout files. OpenCode SQLite is authoritative for OpenCode because the
official CLI itself lists and exports sessions from that store. If a native
database is temporarily absent, locked, has an unsupported schema, or is
replaced by a symlink, the root scan fails closed and its previous rows remain
intact.

`--validate` is deliberately explicit. For every changed file-based
`candidate`, it runs the same bounded source adapter and target conversion
validation used by the normal migrator. OpenCode inventory refresh never
exports tens of thousands of bundles merely to validate them; transfer exports
and validates the selected native ID. A later file change clears a validation
guarantee. Transfer always performs an authoritative load regardless of
catalog status.

## Privacy and recovery

Names, AI-generated titles, UUIDs, timestamps, CWDs, paths, and version fields
can reveal sensitive project information. In particular, a native picker title
can itself be derived from conversation text even though the migrator does not
read a message body to create one. The database is created with mode
`0600` in a mode-`0700` application directory. It is not encrypted or
secret-scanned. Protect backups and terminal/JSON output accordingly.

The catalog stores no first prompt, preview, `first_user_message`, message/tool
body, or credential material by design. Codex and OpenCode databases are opened
read-only; no native index is mutated. Removing the catalog file is safe: it
deletes only the derived search index, and the next refresh rebuilds it from
native stores. Do not remove native sessions to repair the catalog.
