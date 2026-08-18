# Native session catalog

The catalog finds and searches native Claude Code and Codex CLI sessions across
more than one agent home. Native JSONL files remain authoritative. The catalog
is a private, disposable SQLite index; it never changes either agent's session
store.

## What “all sessions” means

An exhaustive refresh means **every recognized native session JSONL below every
enabled catalog root**. It does not mean an implicit whole-disk crawl. Agent
homes can have arbitrary names and locations, so no program can safely discover
all of them without a search boundary.

The catalog adds these roots automatically when they exist:

- `~/.claude` and `~/.codex`, even if an environment override selects another
  home;
- `CLAUDE_CONFIG_DIR` and `CODEX_HOME`; and
- `.claude` or `.codex` native homes in the current directory or one of its
  ancestors.

Use `catalog refresh --claude-root HOME` or `--codex-root HOME` for arbitrary
custom homes. These repeatable options also persist the roots for later
refreshes. Use repeatable `--discover-under DIRECTORY` to find project-local
`.claude` and `.codex` homes anywhere below a specific workspace. Discovery
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

Consequently, archived rollouts, duplicate UUIDs, nested sidechains/subagents,
malformed files, and formats that conversion deliberately rejects remain
discoverable. Claude sidechains and Codex paginated/history-base sessions are
listed as `unsupported`; listing them does not make them convertible.

## Quick start

The default database is
`$XDG_STATE_HOME/session-bridge/catalog.sqlite3`, or
`~/.local/state/session-bridge/catalog.sqlite3` when `XDG_STATE_HOME` is unset.
`SESSION_BRIDGE_CATALOG` or the global `--catalog PATH` option overrides it.

```console
# Index all existing automatic and previously registered roots.
session-bridge catalog refresh

# Add custom homes immediately and persist them.
session-bridge catalog refresh \
  --claude-root /agent-homes/claude-one \
  --claude-root /agent-homes/claude-two \
  --codex-root /agent-homes/codex

# Find project-local homes within an explicit workspace boundary.
session-bridge catalog refresh --discover-under /workspaces

# Search native names/titles and UUIDs. Paths are hidden by default.
session-bridge catalog search "release investigation"
session-bridge catalog search 12345678 --format codex --json

# Inspect one exact result, then transfer it.
session-bridge catalog show CATALOG_ID --include-paths
session-bridge transfer --catalog-id CATALOG_ID --dry-run
```

`catalog list`, `catalog search`, and `catalog show` expose an opaque
`catalog_id`. It selects one physical JSONL even when several roots contain the
same native UUID. `transfer --catalog-id` reopens and authoritatively parses the
current source file before conversion; an index status never bypasses normal
conversion validation.

## Commands and filters

```text
session-bridge catalog refresh
    [--claude-root HOME]... [--codex-root HOME]...
    [--discover-under DIRECTORY]... [--no-auto-roots] [--validate] [--json]

session-bridge catalog roots list [--json]
session-bridge catalog roots add PATH --format claude|codex [--json]
session-bridge catalog roots remove ROOT_ID

session-bridge catalog list [FILTERS] [--json]
session-bridge catalog search QUERY [FILTERS] [--json]
session-bridge catalog show CATALOG_ID [--include-paths] [--json]
```

List/search filters are repeatable `--status STATUS` and `--kind KIND`, plus
repeatable `--lifecycle project|active|archived`, `--format claude|codex`,
timezone-aware RFC-3339 `--since`/`--until`, `--include-missing`, `--limit`, and
`--offset`.
Search is a case-insensitive substring match over:

- native metadata UUID and a structurally valid filename UUID (the latter keeps
  a malformed or partially written candidate findable);
- Claude `custom-title` and `ai-title` values;
- Codex `thread_name_updated` names and the native SQLite thread `name` and
  `title` fields; and
- Claude sidechain `agentId` and `agent-<id>` filename keys.

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
| `candidate` | Fast structural metadata scan passed; full conversion has not been requested. |
| `validated` | The exact stat identity was fully parsed, dry-converted, and target-validated during `refresh --validate`. |
| `unsupported` | The file is a recognized session type intentionally rejected by conversion, such as a Claude sidechain or Codex paginated/history-base rollout. |
| `corrupt` | JSONL, native structure, or explicit conversion validation failed. |
| `oversized` | The source exceeds the bridge's bounded input limits. |
| `busy` | The source changed while it was being scanned; retry after the native CLI finishes appending. |
| `unreadable` | The candidate could not be read with current permissions. |
| `missing` | It existed during a successful earlier root scan but is no longer present. |

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

Refresh compares device, inode, byte size, and nanosecond modification time.
An unchanged file reuses its existing structural result; a changed or returned
file is rescanned. The scanner checks the source snapshot again after reading
it so an actively changing transcript becomes `busy`. A successful root scan
marks disappeared files missing. Failed root scans retain prior state.

The fast default streams every changed JSONL because title records can occur
late in a transcript. It does not materialize message bodies, but an initial
refresh still performs I/O proportional to the total bytes in all configured
session stores. Native Codex SQLite is used only to add `name`, `title`, and
spawn-lineage metadata; it is not trusted as inventory because it can omit
rollout files. If that optional database is temporarily absent or unreadable,
previously indexed native titles are retained rather than erased. The second
refresh normally stats and reuses unchanged entries.

`--validate` is deliberately explicit. For every changed `candidate`, it runs
the same bounded source adapter and target conversion validation used by the
normal bridge. A later change clears that guarantee and the replacement file
returns to `candidate` until validated again. Transfer always performs an
authoritative load regardless of catalog status.

## Privacy and recovery

Names, AI-generated titles, UUIDs, timestamps, CWDs, paths, and version fields
can reveal sensitive project information. In particular, a native picker title
can itself be derived from conversation text even though the bridge does not
read a message body to create one. The database is created with mode
`0600` in a mode-`0700` application directory. It is not encrypted or
secret-scanned. Protect backups and terminal/JSON output accordingly.

The catalog stores no first prompt, preview, `first_user_message`, message/tool
body, or credential material by design. Codex's native `state_*.sqlite` is
opened read-only and Claude indexes are not mutated. Removing the catalog file
is safe: it deletes only the derived search index, and the next refresh rebuilds
it from native stores. Do not remove native JSONL files to repair the catalog.
