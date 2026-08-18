# CLI reference

This page documents `session-bridge` 0.1.1. All commands read one local JSONL
transcript and print no message text or tool payloads. Paths, session IDs,
working directories, and SHA-256 hashes are operational metadata and can still
be sensitive; keep command output and manifests private.

The generated target transcript is not content-free. The bridge copies the
supported conversation, does no secret scanning or redaction, and provides no
encryption. Embedded credentials in messages or tool data therefore transfer
even though external CLI credential stores do not.

## Command summary

| Command | Purpose | Writes files |
| --- | --- | --- |
| `inspect PATH` | Detect and inventory a transcript without showing conversation content | No |
| `convert PATH --to AGENT --output PATH` | Convert to an explicit target file | Yes |
| `import PATH --to AGENT` | Convert and install at the target CLI's native path | Yes, unless `--dry-run` |
| `transfer UUID --from AGENT` | Discover a native source by UUID and import it into the other CLI | Yes, unless `--dry-run` |

`AGENT` is `claude` or `codex`. A source cannot be converted to the same
format. Successful commands exit `0`. Validation, discovery, collision, and
conversion failures print `session-bridge: error: ...` to standard error and
exit `2`; command-line usage errors also exit `2`.

Global discovery options are:

```console
session-bridge --help
session-bridge --version
```

## `inspect`

```console
session-bridge inspect PATH [--format claude|codex] [--json]
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
session-bridge convert PATH --to claude|codex --output OUTPUT [OPTIONS]
```

The command creates two files and refuses to overwrite either:

```text
OUTPUT
OUTPUT.session-bridge.json
```

The first is the native target transcript. The second is the conversion
manifest. `convert` does not install into an agent home and has no dry-run
mode. Use `inspect` for a read-only source inventory or `import --dry-run` to
preview a native installation.

## `import`

```console
session-bridge import PATH --to claude|codex [--home HOME] [--dry-run] [OPTIONS]
```

The native output path is derived from the target session UUID, timestamp, and
working directory:

```text
Claude: HOME/projects/<encoded-cwd>/<uuid>.jsonl
Codex:  HOME/sessions/YYYY/MM/DD/rollout-<timestamp>-<uuid>.jsonl
Manifest: HOME/session-bridge/manifests/<uuid>.json
```

Without `--home`, Claude uses `CLAUDE_CONFIG_DIR` or `~/.claude`; Codex uses
`CODEX_HOME` or `~/.codex`. `--home` takes precedence over those defaults.
Missing destination directories are created with mode `0700`; existing
directory permissions are not changed. The native transcript and manifest are
created with mode `0600`.

`--dry-run` still reads, detects, parses, maps, validates, and collision-checks
the complete conversion. It prints the planned paths and warnings but creates
no directories or files. A dry run without an explicit `--session-id` uses a
new preview UUID; rerunning without that option intentionally generates a
different UUID. Even with a fixed UUID, conversion is regenerated: structural
record IDs and the target hash can differ, and a missing/invalid source
timestamp can move a Codex target across date partitions. Review the applied
JSON result rather than treating dry-run output as a byte-identical plan.

## `transfer`

```console
session-bridge transfer SOURCE_UUID --from claude|codex [OPTIONS]
```

`transfer` discovers the native source and infers the opposite target format.
It then uses the same conversion and installation path as `import`.
`SOURCE_UUID` always identifies the source; the separate `--session-id`, when
present, chooses the new target UUID.

Source lookup is filesystem-only:

- Claude searches `SOURCE_HOME/projects/*/<uuid>.jsonl`. `--source-cwd` narrows
  lookup to the encoded directory for one project and should be supplied when
  known because Claude's directory encoding can collide.
- Codex searches active `SOURCE_HOME/sessions/YYYY/MM/DD` rollouts and
  `SOURCE_HOME/archived_sessions`. `--source-cwd` is invalid for Codex.
- `--source-home` overrides `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, or the normal
  home for source lookup. `--home` independently selects the target home.
- The discovered transcript must contain a valid native session ID equal to
  `SOURCE_UUID`. Missing, mismatched, duplicate, and ambiguous matches fail
  closed.

The bridge does not consult Claude indexes, Codex SQLite, or interactive
pickers during discovery.

## Conversion options

These options are shared by `convert`, `import`, and `transfer` unless noted:

| Option | Default | Behavior |
| --- | --- | --- |
| `--format claude|codex` | Automatic detection | Overrides source detection for `convert` and `import`; `transfer` already knows the source from `--from` |
| `--session-id UUID` | Fresh UUID | Selects the target UUID after normalization; it never authorizes overwrite |
| `--cwd PATH` | Source CWD, then current process CWD | Stores an absolute resolved target working directory; a nonexistent directory is allowed with a warning |
| `--target-cli-version VERSION` | Claude `2.1.209` or Codex `0.144.4` | Changes only the version string written to metadata; the emitted schema remains pinned and a non-default value produces `unvalidated_target_version` |
| `--model-provider ID` | `openai` | Codex target metadata only |
| `--model LABEL` | Source model, then `unknown` | Claude target assistant-message metadata only |

Passing a target-only option while producing the other target has no effect on
that target's native records.

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
  "manifest": "/target/home/session-bridge/manifests/<uuid>.json",
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

`records` counts generated native JSONL records. `sha256` is the digest of the
generated target, not the source. During dry-run it is the preview target's
digest and need not equal a later regenerated apply. `dropped_events` includes
both omissions and
documented transformations, and can include retained-but-diagnosed details
such as UI-only projections or duplicate tool results. The name is historical;
a nonzero count does not always mean the associated record vanished. Consult
the warning message and the
[compatibility matrix](format-compatibility.md) before deciding whether a
conversion is acceptable.

## Manifest

The manifest is a private, content-free audit record with schema version `1`:

```json
{
  "schema_version": 1,
  "created_at": "<RFC-3339 timestamp>",
  "bridge_version": "0.1.1",
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

Use the exact target UUID printed by the bridge and run from the same target
working directory:

```console
cd /target/project
codex resume TARGET_UUID
```

```console
cd /target/project
claude --resume TARGET_UUID
```

Explicit UUID resume is authoritative. Picker visibility, ordering, and CWD
filtering vary by CLI version. Authentication remains the target CLI's
responsibility; the bridge never copies external credential stores.
