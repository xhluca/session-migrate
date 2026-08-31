# Hermes Agent format

This document pins the native Hermes Agent contract implemented by
`session-migrate`. Hermes's SQLite layout is an internal, versioned interface,
so the adapter fails closed on any other Hermes or schema version.

## Compatibility pin

| Item | Validated value |
| --- | --- |
| Project | [`NousResearch/hermes-agent`](https://github.com/NousResearch/hermes-agent) |
| Release tag | `v2026.8.27` |
| Python package | `hermes-agent==0.20.6` |
| Source commit | `5fc308a70719a83cccdbba4c0e39c23f5a8239d5` |
| Source-archive SHA-256 | `0ca18fcf97a69bad808ce4a0807694b25237afc61f7a13efab4f0aa26092a171` |
| Native SQLite schema | `26` |
| Observed CLI version | `Hermes Agent v0.20.6 (2026.8.27)` |

The source archive hash is the SHA-256 of `git archive` at the pinned commit.
The editable development launcher is deliberately not used as an artifact pin:
its shebang contains the temporary installation path.

## Native store and discovery

Hermes stores all CLI sessions in one SQLite database:

```text
$HERMES_HOME/state.db
```

`HERMES_HOME` defaults to `~/.hermes`. Unlike file-per-session harnesses, a
Hermes catalog entry is virtual: its identity is the pair `state.db` plus the
native session ID. Native IDs have the form:

```text
YYYYMMDD_HHMMSS_abcdef
```

The adapter validates the timestamp and the 6-to-8-character hexadecimal
suffix. `list_sessions()` inventories every row without loading message bodies.
It returns `session_id`, `title`, `cwd`, `started_at`, `updated_ns`, and the
stored message count. `cli_version` is `None` because Hermes does not persist a
per-session CLI version; database schema validation supplies the compatibility
gate instead.

The schema-26 tables used by the adapter are:

| Table | Fields used |
| --- | --- |
| `schema_version` | `version` |
| `sessions` | identity, title, CWD, model/provider, start/end/activity time, message count |
| `messages` | role/content, tool linkage, timestamp, private reasoning markers, active/compacted state |

Discovery and parsing first take a bounded SQLite backup. This includes
committed WAL frames and gives the catalog a stable SHA-256 fingerprint. A
symlink, non-regular file, oversized database, integrity failure, missing
columns, or schema other than 26 is rejected.

## Source projection

`parse_session(state_db, session_id=...)` requires an explicit native ID when
the database contains more than one session. It reads messages in database
order and maps only Hermes's active replay context:

| Hermes record | Portable event |
| --- | --- |
| `user` or `assistant` text | message |
| user `image_url` data block | image context |
| assistant `tool_calls` | linked tool call |
| `tool` row | linked tool result |
| `_compressed_summary=1` | compaction summary |
| inactive, `compacted=1` row | opaque `hermes_compacted_history` marker |
| other inactive row | opaque `hermes_rewound_history` marker |
| persisted reasoning fields | opaque `hermes_private_reasoning` marker |

Private reasoning text is never copied. Inactive rows are retained only as one
reason-specific accounting marker per row; replaying them would reintroduce
history that Hermes itself no longer sends to the model.

## Target bundle and installation

The serializer creates strict UTF-8 JSON with envelope schema
`session-migrate.hermes.v1`. Its `session` object is the exact shape returned by
Hermes's native session exporter. User and assistant text, inline data images,
linked function calls/results, title, CWD, model metadata, and compaction
summaries are represented natively. Timestamps are made monotonic without
changing semantic order.

Installation uses Hermes's own `SessionDB.import_sessions` transaction from the
pinned environment. It does not issue ad hoc SQL writes. The public adapter
entry point is:

```python
install_bundle(
    data: bytes,
    *,
    session_id: str,
    target_home: Path,
    target_cli: Path | None = None,
    dry_run: bool = False,
    environ: Mapping[str, str] | None = None,
) -> InstalledHermesSession
```

The installer verifies the exact CLI version, initializes a missing
`state.db` through the CLI, refuses an existing session ID, invokes the native
importer, and reparses the installed row before returning. Target directories
are private and symlink traversal is rejected.

Hermes's user-facing `hermes sessions import` command currently accepts Claude
and Codex sources, not Hermes's own export. `SessionDB.import_sessions` is the
native export/import transaction used internally and is therefore the pinned
installation boundary here.

Resume uses Hermes's native command:

```bash
hermes chat --resume YYYYMMDD_HHMMSS_abcdef
```

Hermes also exposes `--continue <title>`, but migration and verification use the
unambiguous session ID.

## What is omitted

| Source data | Behavior | Reason |
| --- | --- | --- |
| Private thinking text/signatures | counted, not serialized | private provider state is not portable |
| System prompt and provider credentials | not serialized | runtime ownership and secrets stay with the target |
| Tool namespaces | tool name/arguments kept; namespace counted | no equivalent portable Hermes field |
| Non-text tool-result blocks | text kept; other blocks counted | native tool row is text-oriented |
| Compaction boundary/replacement metadata | summary kept; metadata counted | Hermes stores the active summary, not portable boundary semantics |
| Rewound or already compacted bodies | opaque accounting only | not part of Hermes's active model replay |
| Token/cost/runtime counters | reset | derived native runtime state |

## Native research trajectories

Research and validation ran in a unique mode-`0700` temporary root. The pinned
source was installed into its own virtual environment; no user Hermes home or
credential store was read or copied.

Three native trajectories were inspected:

1. A real Hermes CLI session containing a user turn, assistant function call,
   terminal result, and final assistant response. Native ID
   `20260830_233734_3ff33e`; the tool linkage persisted as
   `call_hermes_1` and the result body as Hermes's terminal-result JSON.
2. A real `hermes sessions import` of a Claude fixture. Native ID
   `20260830_233912_1445cf`; Hermes recorded `source=claude-code` and CWD
   `/work`. This established what the public importer supports and what it does
   not.
3. A generated Hermes bundle imported through `SessionDB.import_sessions`,
   resumed by the real CLI against a loopback OpenAI-compatible provider, and
   compacted through the native `SessionDB.archive_and_compact` API. The same
   isolated store was also given a malformed session with an empty message
   role; the native importer returned `ok=false`, imported zero rows, and the
   adapter inventory proved that no partial session appeared.

The third trajectory mechanically proved that the actual model request saw the
imported user message, tool call ID, tool result, compaction summary, later user
message, and new follow-up. The loopback assistant response was appended to the
same native session and reparsed. After native compaction, the reader recovered
the new summary and accounted for archived rows.

One observed native nuance is documented rather than papered over: the first
CLI-created row stored a title but left `cwd` and `model` null despite launch
options, while imported sessions retained an explicit CWD. Both are valid
schema-26 states, so inventory and parsing treat native CWD/model as optional.

## Test contract

The default credential-free suite covers bundle round trips, images,
tool-call/result linkage, compaction, private-reasoning omission, malformed
JSON and orphan rejection, active versus inactive projection, multi-session
inventory, implicit single-session selection, WAL snapshots, symlink rejection,
and schema-version rejection:

```bash
uv run pytest -q tests/test_hermes_format.py tests/test_hermes_native.py
```

The native test is opt-in because it requires an exact pinned source checkout
and its installed CLI. It still uses no provider credential and binds its fake
provider only to loopback:

```bash
SESSION_MIGRATE_HERMES_SOURCE=/path/to/hermes-agent-v2026.8.27 \
SESSION_MIGRATE_HERMES_BIN=/path/to/hermes-agent-v2026.8.27/.venv/bin/hermes \
  uv run pytest -q tests/test_hermes_native.py -vv
```

The test verifies the source commit before launching Hermes, creates a fresh
mode-`0700` home and workspace, imports the portable history, resumes it through
the actual CLI, inspects the provider-visible request, confirms the appended
turn, invokes native compaction, and reparses the result.
