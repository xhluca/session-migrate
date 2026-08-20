# CLI reference

This page documents `session-migrate` 0.5.2. `smigrate` is an exact executable
alias. `inspect`, `convert`, `import`, and
`transfer` read one local JSONL transcript; catalog commands inventory bounded
native roots. No command prints message text or tool payloads. Paths, session IDs,
working directories, and SHA-256 hashes are operational metadata and can still
be sensitive; keep command output and manifests private.

The generated target transcript is not content-free. The migrator copies the
supported conversation, does no secret scanning or redaction, and provides no
encryption. Embedded credentials in messages or tool data therefore transfer
even though external CLI credential stores do not.

## Command summary

| Command | Purpose | Writes files |
| --- | --- | --- |
| `inspect PATH` | Detect and inventory a transcript without showing conversation content | No |
| `convert PATH --to AGENT --output PATH` | Convert to an explicit target file | Yes |
| `import PATH --to TARGET` | Convert and install through the target's supported native path/API | Yes, unless `--dry-run`; OpenCode's list probe may initialize XDG state |
| `transfer UUID --from AGENT [--to TARGET]` | Discover a native Claude/Codex/Pi source and import it | Yes, unless `--dry-run`; same OpenCode qualification |
| `catalog ...` | Index, list, and search every session in configured native roots | Catalog only |

Source `AGENT` is `claude`, `codex`, or `pi`. `TARGET` is
`claude|codex|pi|opencode|copilot|antigravity|cursor`; Antigravity and Cursor
are accepted as requests but fail closed because no supported native import
contract exists. A source cannot be converted to the same format. Successful
commands exit `0`. Validation, discovery, collision, and
conversion failures print `session-migrate: error: ...` to standard error and
exit `2`; command-line usage errors also exit `2`.

Global discovery options are:

```console
session-migrate --help
session-migrate --version
session-migrate --catalog /private/path/catalog.sqlite3 catalog list
```

## `inspect`

```console
session-migrate inspect PATH [--format claude|codex|pi] [--json]
```

The default human-readable output and `--json` contain the same fields:

| Field | Meaning |
| --- | --- |
| `format` | Detected source format |
| `path`, `bytes`, `sha256` | Resolved source identity and digest |
| `records` | Non-empty JSON object records |
| `session_id`, `cwd`, `cli_version`, `started_at` | First usable native metadata |
| `record_types`, `roles`, `content_blocks`, `event_types` | Content-free structural counts |
| `tool_calls`, `tool_results` | Tool record counts |

Missing scalar values render as `-` in the default text view and `null` in
JSON.

`--format` bypasses automatic detection; it does not relax JSONL or size
validation. `inspect` never emits message text, tool names, arguments, results,
image data, or unknown raw records.

Inspection is a raw structural inventory, not a full semantic adapter parse.
Successful inspection does not guarantee conversion; branch ancestry,
history-mode, linkage, or resumable-history validation can still fail later.

## `convert`

```console
session-migrate convert PATH \
  --to claude|codex|pi|opencode|copilot|antigravity|cursor \
  --output OUTPUT [OPTIONS]
```

The command creates two files and refuses to overwrite either:

```text
OUTPUT
OUTPUT.session-migrate.json
```

The first is the native target transcript. The second is the conversion
manifest. Pi output is native v3 JSONL. OpenCode output is the public JSON
import bundle; use a `.json` suffix for clarity. Copilot output is only
`events.jsonl`; it does not include `workspace.yaml`, so use `import` or
`transfer` for a directly resumable Copilot installation. Antigravity and
Cursor fail before writing. `convert` does not install into an agent home and
has no dry-run mode. Use `inspect` for a read-only source inventory or
`import --dry-run` to preview a native installation.

## `import`

```console
session-migrate import PATH \
  --to claude|codex|pi|opencode|copilot|antigravity|cursor \
  [--home HOME] [--dry-run] [OPTIONS]
```

The native output path is derived from the target session UUID, timestamp, and
working directory:

```text
Claude: HOME/projects/<encoded-cwd>/<uuid>.jsonl
Codex:  HOME/sessions/YYYY/MM/DD/rollout-<timestamp>-<uuid>.jsonl
Pi:     HOME/sessions/--<encoded-cwd>--/<timestamp>_<uuid>.jsonl
Copilot: HOME/session-state/<uuid>/events.jsonl
Manifest: HOME/session-migrate/manifests/<uuid>.json
```

Without `--home`, Claude uses `CLAUDE_CONFIG_DIR` or `~/.claude`; Codex uses
`CODEX_HOME` or `~/.codex`; Pi uses `PI_CODING_AGENT_DIR` or `~/.pi/agent`;
Copilot uses `COPILOT_HOME` or `~/.copilot`. `--home` takes precedence over
those defaults for these four filesystem targets. Copilot also creates
`workspace.yaml` inside the target session directory and refuses a collision
with any pre-existing directory at that exact UUID.
Missing destination directories are created with mode `0700`; existing
directory permissions are not changed. The native transcript and manifest are
created with mode `0600`.

OpenCode is different by contract. The migrator invokes the exact pinned OpenCode
`1.17.20` binary's public importer, never its SQLite database. `--home` is
rejected; use the normal HOME/XDG environment. `--target-cli` chooses the
binary, followed by `OPENCODE_BIN`, `PATH`, and `~/.opencode/bin/opencode`.
Its content-free manifest is stored below
`$XDG_STATE_HOME/session-migrate/manifests/opencode` (or `~/.local/state`) and
records `opencode:<ses_id>` as the target location.

`--dry-run` still reads, detects, parses, maps, validates, and collision-checks
the complete conversion. It prints the planned paths and warnings but creates
no migrator-owned native artifact. A dry run without an explicit `--session-id` uses a
new preview UUID; rerunning without that option intentionally generates a
different UUID. Even with a fixed UUID, conversion is regenerated: structural
record IDs and the target hash can differ, and a missing/invalid source
timestamp can move a Codex target across date partitions. Review the applied
JSON result rather than treating dry-run output as a byte-identical plan.

For Claude, Codex, Pi, and Copilot this creates no target directories or files. For
OpenCode it creates no session, temporary import bundle, or migrator manifest,
but its required official `session list` collision probe may initialize normal
OpenCode cache, database, log, and lock files under XDG.

## `transfer`

```console
session-migrate transfer SOURCE_UUID --from claude|codex|pi [--to TARGET] [OPTIONS]
```

`transfer` discovers the native source. Without `--to`, it infers the opposite
Claude/Codex target for backward compatibility. A Pi source requires an
explicit different `--to`. It then uses the same conversion and
installation path as `import`.
`SOURCE_UUID` always identifies the source; the separate `--session-id`, when
present, chooses the new target UUID.

Source lookup is filesystem-only:

- Claude searches `SOURCE_HOME/projects/*/<uuid>.jsonl`. `--source-cwd` narrows
  lookup to the encoded directory for one project and should be supplied when
  known because Claude's directory encoding can collide.
- Codex searches active `SOURCE_HOME/sessions/YYYY/MM/DD` rollouts and
  `SOURCE_HOME/archived_sessions`. `--source-cwd` is invalid for Codex.
- Pi searches every v3 session below `SOURCE_HOME/sessions`; `--source-cwd`
  disambiguates duplicate UUIDs by the header CWD.
- `--source-home` overrides `CLAUDE_CONFIG_DIR`, `CODEX_HOME`,
  `PI_CODING_AGENT_DIR`, or the normal
  home for source lookup. `--home` independently selects the target home.
- The discovered transcript must contain a valid native session ID equal to
  `SOURCE_UUID`. Missing, mismatched, duplicate, and ambiguous matches fail
  closed.

The migrator does not consult Claude indexes, Codex SQLite, or interactive
pickers during discovery.

Alternatively, `transfer --catalog-id CATALOG_ID` selects one exact physical
source returned by `catalog list` or `catalog search`. In this form, do not pass
the positional UUID, `--source-home`, or `--source-cwd`; `--from` is optional
and, when present, must match. The source is still fully reopened, parsed, and
validated before conversion. A stale catalog row cannot bypass conversion
safety checks.

## `catalog`

The catalog provides multi-root discovery and metadata-only title/UUID search:

```console
session-migrate catalog refresh
session-migrate catalog search "session title"
session-migrate catalog list --status unsupported --json
session-migrate catalog show CATALOG_ID --include-paths
```

It includes Claude top-level and nested sidechain files, Codex active and
archived rollouts, Pi v3 workspace sessions, duplicates, malformed files, and known unsupported history
modes under every configured root. It never promises an unbounded whole-disk
crawl. Explicit/custom/project-local root behavior, status meanings,
incremental refresh, exact commands, initial-scan cost, and the metadata privacy
boundary are specified in the [catalog guide](session-catalog.md).

## Conversion options

These options are shared by `convert`, `import`, and `transfer` unless noted:

| Option | Default | Behavior |
| --- | --- | --- |
| `--format claude|codex|pi` | Automatic detection | Overrides source detection for `convert` and `import`; `transfer` already knows the source from `--from` |
| `--session-id UUID` | Fresh UUID | Selects the target UUID after normalization; it never authorizes overwrite |
| `--cwd PATH` | Source CWD, then current process CWD | Stores an absolute resolved target working directory; a nonexistent directory is allowed with a warning |
| `--target-cli-version VERSION` | Target-specific pinned version | Changes only the version string written to metadata; the emitted schema remains pinned and a non-default value produces `unvalidated_target_version`. OpenCode automatic import rejects overrides. |
| `--target-cli PATH` | OpenCode lookup chain | OpenCode `import`/`transfer` only; selects the official importer binary |
| `--model-provider ID` | Codex: `openai`; Pi/OpenCode: inferred from source | Target model provider metadata; ignored by Copilot |
| `--model LABEL` | Source model, then `unknown` | Target model label where supported, including Copilot |

`--target-cli` is rejected outside OpenCode `import`/`transfer`. Model and
metadata-version options affect only target schemas that consume them;
OpenCode automatic import additionally rejects any schema/version override.
Copilot target metadata can be overridden with a warning, but the emitted event
schema remains the one validated against 1.0.70.

Every CLI path expands a leading `~`; relative paths remain relative to the
process working directory until the applicable source, home, CWD, or output
normalization makes them absolute. Collision checks cover the exact planned
native and manifest paths. They do not globally search other target project,
date, or archive directories for the same explicit target UUID, so a fresh
generated UUID remains safest.

## Successful conversion output

`convert`, `import`, and `transfer` print a content-free JSON object. A
schematic result is:

```json
{
  "cwd": "/target/project",
  "dropped_events": {
    "thinking": 2
  },
  "dry_run": false,
  "manifest": "/target/home/session-migrate/manifests/<uuid>.json",
  "output": "/target/native/path/<uuid>.jsonl",
  "records": 12,
  "session_id": "<target-uuid>",
  "sha256": "<target-jsonl-sha256>",
  "source_format": "codex",
  "target_format": "claude",
  "warnings": [
    {
      "code": "dropped_event_kind",
      "count": 2,
      "event_kind": "thinking",
      "message": "target conversion omitted or transformed this source detail"
    }
  ]
}
```

`records` counts generated native records (for OpenCode, bundle/session/message
parts). `sha256` is the digest of the generated target, not the source. During dry-run it is the preview target's
digest and need not equal a later regenerated apply. `dropped_events` includes
both omissions and
documented transformations, and can include retained-but-diagnosed details
such as UI-only projections, duplicate tool results, or Copilot tool-result
images whose native asset is retained while provider replay remains uncertain.
The name is historical;
a nonzero count does not always mean the associated record vanished. Consult
the warning message and the
[compatibility matrix](format-compatibility.md) before deciding whether a
conversion is acceptable.

## Manifest

The manifest is a private, content-free audit record with schema version `2`:

```json
{
  "schema_version": 2,
  "created_at": "<RFC-3339 timestamp>",
  "migration_version": "0.5.2",
  "source": {
    "format": "claude",
    "path": "/source/session.jsonl",
    "sha256": "<source-sha256>",
    "session_id": "<source-uuid>",
    "cli_version": "2.1.209",
    "records": 8,
    "events": {"message": 4, "tool_call": 1, "tool_result": 1}
  },
  "target": {
    "format": "codex",
    "path": "/target/rollout.jsonl",
    "sha256": "<target-sha256>",
    "session_id": "<target-uuid>",
    "cli_version": "0.144.4",
    "cwd": "/target/project",
    "timestamp": "<RFC-3339 timestamp>",
    "records": 12
  },
  "dropped_events": {},
  "warnings": []
}
```

It never contains message text, tool names, arguments, results, image data, or
raw unknown records. It does contain paths, UUIDs, versions, timestamps,
counts, and hashes. Do not publish it without reviewing that metadata.

Common warning codes include:

- `invalid_session_timestamp`, `synthesized_cwd`, and `cwd_not_directory`;
- `unvalidated_source_version` and `unvalidated_target_version`; and
- `dropped_event_kind`, accompanied by `event_kind` and `count`.

The compatibility matrix defines the event-specific meaning. A warning does
not necessarily mean the generated session is unusable, but it always deserves
review before deleting or archiving the source.

## Resume the imported session

Use the exact target UUID printed by the migrator and run from the same target
working directory:

```console
cd /target/project
codex resume TARGET_UUID
```

```console
cd /target/project
claude --resume TARGET_UUID
```

```console
pi --session /exact/path/from/the-success-json
```

```console
opencode run "follow-up" --session ses_TARGET_ID --pure
```

```console
cd /target/project
copilot --resume TARGET_UUID
```

Explicit UUID resume is authoritative. Picker visibility, ordering, and CWD
filtering vary by CLI version. Authentication remains the target CLI's
responsibility; the migrator never copies external credential stores. Copilot
can use GitHub login or its documented BYOK environment. A Codex OAuth session
is not a portable Copilot/GitHub/Google credential.
