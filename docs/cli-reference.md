# CLI reference

This page documents `session-migrate` 0.9.0. `smigrate` is an exact shorthand
for the same executable.

## Commands

```text
session-migrate inspect PATH [--format FORMAT] [--json]
session-migrate convert PATH --to TARGET --output PATH [OPTIONS]
session-migrate import PATH --to TARGET [--home PATH] [--dry-run] [OPTIONS]
session-migrate transfer SOURCE_ID --from FORMAT --to TARGET [OPTIONS]
session-migrate transfer --catalog-id ID --to TARGET [OPTIONS]
session-migrate transfer --title TITLE [--from FORMAT] --to TARGET [OPTIONS]
session-migrate catalog refresh [ROOT OPTIONS] [--validate] [--json]
session-migrate catalog roots list|add|remove ...
session-migrate catalog list [FILTERS]
session-migrate catalog search QUERY [FILTERS]
session-migrate catalog show CATALOG_ID [--include-paths] [--json]
```

`FORMAT` and `TARGET` accept:

```text
claude  codex  pi  omp  opencode  copilot  antigravity  cursor
vibe  muse  qwen  kimi  grok  kilo  openhands
```

All fifteen formats are readable and writable. Cursor is an experimental,
text-only adapter pinned to one exact Cursor Agent build. Same-format migration
is supported as a portable rewrite into a new independent session.

## `inspect`

`inspect` reports structural metadata and counts without printing message text,
tool arguments/results, image bytes, or titles.

```bash
smigrate inspect ~/.codex/sessions/2026/08/20/rollout-...jsonl
smigrate inspect ./store.db --format cursor --json
```

It prints the source path, CWD, UUID, timestamps, SHA-256, and structural
counts. Those fields can still be sensitive. Successful inspection means the
container is structurally recognizable; conversion applies stricter semantic
validation.

`--format` bypasses automatic format selection. It does not make an unsupported
schema or version safe.

Kilo and OpenCode official bundles intentionally share one schema and retain
stored metadata across cross-imports. A standalone bundle cannot be attributed
reliably, so automatic inspection fails closed; pass `--format kilo` or
`--format opencode` for a trusted bundle.

## `convert`

`convert` writes a standalone target artifact plus a sidecar manifest:

```bash
smigrate convert SOURCE --to codex --output ./rollout.jsonl
```

The manifest is `OUTPUT.session-migrate.json`. `convert` never installs into a
native agent home and never invokes a target CLI. For OpenCode and Kilo Code it
writes an official import bundle; for Antigravity and Cursor it writes a
complete SQLite database. Multi-file targets such as Vibe, Kimi, Grok, and
OpenHands use a validation bundle that `import` publishes into their complete
native layout.

## `import`

`import` converts and installs into a target store:

```bash
smigrate import SOURCE --to pi --cwd "$PWD" --dry-run
smigrate import SOURCE --to pi --cwd "$PWD"
```

The target paths are collision checked and never overwritten. `--dry-run`
performs conversion, native validation, and collision checks but does not
install a session or manifest. OpenCode's official read/list preflight can
initialize its ordinary XDG cache/database metadata during a dry run.

OpenCode and Kilo import always use their official pinned CLIs and do not accept
`--home`; isolate or select them with normal `HOME`/XDG variables. Antigravity
and Cursor installs verify the exact pinned executable and its published
hashes. Muse and Qwen install one native JSONL. Kimi, Grok, and OpenHands
publish their validated multi-file native sessions together.

## `transfer`

Direct lookup uses a native source ID:

```bash
smigrate transfer SOURCE_UUID --from claude --to codex --cwd "$PWD"
smigrate transfer SOURCE_UUID --from omp --source-cwd "$PWD" --to codex
smigrate transfer SOURCE_UUID --from cursor --source-cwd "$PWD" --to claude
smigrate transfer SOURCE_UUID --from vibe --source-cwd "$PWD" --to codex
smigrate transfer SOURCE_UUID --from qwen --source-cwd "$PWD" --to kimi
smigrate transfer session_SOURCE_UUID --from kimi --source-cwd "$PWD" --to muse
smigrate transfer ses_... --from opencode --to pi --source-cli ~/.opencode/bin/opencode
smigrate transfer SESSION_UUID --from grok --source-cwd "$PWD" --to openhands
smigrate transfer ses_... --from kilo --to claude --source-cli /path/to/kilo
smigrate transfer SESSION_UUID --from openhands --to qwen
```

Claude, Pi, OMP, Cursor, Vibe, Qwen, Kimi, and Grok can use `--source-cwd` to
select a workspace-specific store. OpenCode and Kilo are virtual: the pinned
official CLI exports the requested ID. All other sources are read from their
native files.

Catalog transfer avoids ambiguous paths and duplicate UUIDs:

```bash
smigrate catalog search "parser refactor"
smigrate transfer --catalog-id CATALOG_ID --to copilot
smigrate transfer --title "parser refactor" --from claude --to copilot
```

`SOURCE_ID` selects the source. `--session-id` assigns the new target UUID;
they are deliberately different concepts.

`--title` searches the existing catalog and proceeds only when one session
matches. An exact case-insensitive title wins over partial keyword matches.
Refresh the catalog first; if the title is ambiguous, use `catalog search` and
pass the selected opaque ID with `--catalog-id`.

Without `--to`, only Claude→Codex and Codex→Claude retain their historical
default. Every other source requires an explicit target.

## Conversion options

| Option | Meaning |
| --- | --- |
| `--format FORMAT` | Override source detection for file-based `inspect`, `convert`, or `import` |
| `--session-id UUID` | Assign a new target UUID; generated by default |
| `--cwd PATH` | Target working directory; precedence is option, source CWD, process CWD |
| `--target-cli-version VERSION` | Change emitted metadata only; the writer architecture remains pinned |
| `--target-cli PATH` | Pinned OpenCode, Kilo, Antigravity, or Cursor executable for native import |
| `--model-provider ID` | Codex, Pi, OMP, OpenCode, or Muse target provider |
| `--model ID` | Target model label for formats that persist one, including Grok, Kilo, and OpenHands |
| `--home PATH` | Target native home, except OpenCode and Kilo |
| `--dry-run` | Validate and collision-check without installing migrator artifacts |

An irrelevant target-specific option may be accepted but has no effect. The
manifest records the target metadata version and warns when it differs from the
validated writer pin. Automatic OpenCode, Kilo, Antigravity, and Cursor
installation still requires the exact pinned version.

## Home resolution

| Format | Default source/target root |
| --- | --- |
| Claude | `$CLAUDE_CONFIG_DIR`, otherwise `~/.claude` |
| Codex | `$CODEX_HOME`, otherwise `~/.codex` |
| Pi | `$PI_CODING_AGENT_DIR`, otherwise `~/.pi/agent` |
| Oh My Pi | `$PI_CODING_AGENT_DIR`, otherwise `~/.omp/agent` |
| OpenCode | official CLI under its normal XDG data root |
| Copilot | `$COPILOT_HOME`, otherwise `~/.copilot` |
| Antigravity | `~/.gemini/antigravity-cli` |
| Cursor | `$CURSOR_CONFIG_DIR`, `$XDG_CONFIG_HOME/cursor`, otherwise `~/.cursor` |
| Vibe | `$VIBE_HOME`, otherwise `~/.vibe` |
| Muse | `$XDG_DATA_HOME/muse`, otherwise `~/.local/share/muse` |
| Qwen | `$QWEN_HOME`, otherwise `~/.qwen` |
| Kimi | `$KIMI_CODE_HOME`, otherwise `~/.kimi-code` |
| Grok | `$GROK_HOME`, otherwise `~/.grok` |
| Kilo Code | official CLI under its normal XDG data root |
| OpenHands | `$OPENHANDS_CONVERSATIONS_DIR`, otherwise `~/.openhands/conversations` |

Explicit `--home` or `--source-home` wins where supported. All CLI path options
expand `~` consistently.

## Catalog

The private SQLite catalog is at
`$SESSION_MIGRATE_CATALOG`, otherwise
`$XDG_STATE_HOME/session-migrate/catalog.sqlite3`, otherwise
`~/.local/state/session-migrate/catalog.sqlite3`.

Refresh auto-registers existing default, environment-selected, and ancestor
project roots:

```bash
smigrate catalog refresh
smigrate catalog refresh --discover-under ~/dev --validate
```

Additional roots are repeatable:

```text
--claude-root PATH       --codex-root PATH
--pi-root PATH           --omp-root PATH
--opencode-root PATH
--copilot-root PATH      --antigravity-root PATH
--cursor-root PATH       --vibe-root PATH
--muse-root PATH         --qwen-root PATH
--kimi-root PATH
--grok-root PATH         --kilo-root PATH
--openhands-root PATH
```

`--discover-under` is bounded to the supplied directory, never follows
symlinked directories, and recognizes conventional hidden stores. Arbitrary
custom root names must be registered explicitly. “All sessions” means all
recognized entries in these configured/discovered roots—not a whole-disk scan.

Search is case-insensitive across native title/name metadata and IDs. Multiple
keywords are ANDed and may occur in any order:

```bash
smigrate catalog search "database migration" --format codex
smigrate catalog search "timeout postgres" --lifecycle archived
smigrate catalog list --status candidate --since 2026-08-01T00:00:00Z
```

Paths and CWDs are neither searched nor printed unless `--include-paths` is
passed. The catalog never stores conversation bodies or tool payloads. See the
[catalog guide](session-catalog.md) for statuses and schema behavior.

## Successful JSON output

`convert`, `import`, and `transfer` print one content-free JSON object:

```json
{
  "cwd": "/target/workspace",
  "dropped_events": {"thinking:unsupported": 2},
  "dry_run": true,
  "manifest": "/target/state/session-migrate/manifests/UUID.json",
  "output": "/target/native/session/path",
  "records": 12,
  "session_id": "TARGET_ID",
  "sha256": "TARGET_SHA256",
  "source_format": "claude",
  "target_format": "cursor",
  "warnings": []
}
```

The historical field name `dropped_events` includes omissions,
transformations, and retained inconsistencies that need operator attention.
Non-empty warnings do not mean the command failed. Review them before resume.

Manifests use schema version 2 and include migration version, source/target
identity and hashes, structural counts, warning objects, and the same loss
counters. They never include message/tool/media bodies, but their paths, IDs,
CWDs, timestamps, and hashes are operationally sensitive.

A dry-run conversion is regenerated on apply. A fixed `--session-id` usually
pins native paths, but generated structural IDs/timestamps can change, so do not
expect identical SHA-256 values.

## Exit behavior

- `0`: success, help, or version
- `2`: argument error or expected migration failure

Expected failures are printed to stderr as:

```text
session-migrate: error: MESSAGE
```

There is no overwrite/force mode. Source files are always left untouched. If
manifest finalization fails after an external/native install, the error states
that the target session may already exist so the operator can inspect it before
retrying.
