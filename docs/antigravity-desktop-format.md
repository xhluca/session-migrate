# Antigravity Desktop session format

`session-migrate` supports Antigravity Desktop (macOS, Linux, and Windows)
through a clean-room adapter pinned to the 2.18.1 release:

| Property | Pinned value |
| --- | --- |
| Application | Antigravity Desktop `2.18.1` (Electron `44.3.0`) |
| macOS arm64 binary | `148,990,608` bytes, SHA-256 `300ee20f3108a511be1149602b91ba84cbbdcc42213067151e255584129be30f` |
| Linux x64 binary | `181,432,528` bytes, SHA-256 `ab4937445fa3817bc374a1db71062de7daecdb093cbfb6bd36b80f443198670e` |
| Windows x64 binary | `163,640,320` bytes, SHA-256 `1ed84e6a1d1e51064d80c9f382ab3a519eb2775cb91d552c63064a18cfdf3cf2` |
| Conversation store | `<home>/.gemini/antigravity/conversations/<uuid>.db` |
| Picker index | `<home>/.gemini/antigravity/conversation_summaries.db` (`PRAGMA user_version = 3`) |
| Hub summary cache | `<home>/.gemini/antigravity/agyhub_summaries_proto.pb` |
| UI project catalog | `<home>/.gemini/config/projects/<id>.json` + `app_storage.json` |

This is deliberately not a claim of compatibility with unreleased or arbitrary
Antigravity versions. Automatic installation checks executable digests and
schema invariants when native verification is requested.

No vendor code, binaries, descriptors, credentials, or user transcripts are
included in this repository. All schemas, wire mappings, and RPC endpoints were
established through clean-room differential probing and synthetic test fixtures.

## How the format was established

The adapter was derived from independent observations of synthetic sessions and
package analysis across macOS, Linux, and Windows distributions:

1. generate synthetic conversations with unique marker text in isolated environments;
2. inspect SQLite schema, columns, and Protobuf wire field numbers and types;
3. analyze Electron application resources (`app.asar`), verifying identical JavaScript
   (`3c03ce352dc3c1b43f357c27ead1f73c74d198a8ded89b1b3d6a2539e7a6ac5c`) across all three OSes;
4. inspect native Go language server (`language_server`) symbols and Connect-RPC endpoints;
5. verify round-trip serialization and cold-reload step retrieval via Connect-RPC
   (`GetCascadeTrajectorySteps`); and
6. test real-world conversation migration end-to-end with active multi-turn sessions.

## SQLite layout

Each conversation is stored as an independent SQLite 3 database with write-ahead
logging (`WAL`) containing these tables:

```text
trajectory_meta
steps
gen_metadata
executor_metadata
parent_references
trajectory_metadata_blob
battle_mode_infos
```

While the table names match Antigravity CLI, Desktop enforces distinct wire and
metadata invariants:

| Property | Antigravity CLI (`antigravity`) | Antigravity Desktop (`antigravity-desktop`) |
| --- | --- | --- |
| App data subdirectory | `~/.gemini/antigravity-cli` | `~/.gemini/antigravity` |
| `trajectory_meta.source` | `17` (`CLI`) | `1` (`IDE` / `Hub`) |
| `trajectory_metadata_blob` project ID | `"default-cli-project"` | `"outside-of-project"` (or workspace UUID) |
| `conversation_summaries.source` | `"antigravity-cli"` | `""` (empty string) |
| `conversation_summaries.app_data_dir` | `"antigravity-cli"` | `"antigravity"` |
| `conversation_summaries.status` | `""` (empty string) | `"CASCADE_RUN_STATUS_IDLE"` |
| `conversation_summaries.project_id` | `"default-cli-project"` | `"outside-of-project"` |
| Summary columns | 19 or 21 columns | 21 columns (includes `raw_summary`, `group_id`) |
| Subagent trajectory roots | root ID == conversation ID | root ID points to root cascade ID |

The main `trajectory_meta` row stores a fresh trajectory UUID, conversation UUID,
trajectory type `4` (cascade), and source `1` (`IDE`/`Hub`). `steps.idx` is a
contiguous zero-based sequence. Every step duplicates its type and status in
both the SQLite row columns and the serialized `Step` blob.

### Sidebar discovery and project grouping

Antigravity Desktop filters sidebar conversations strictly by active projects:

1. **Active Projects List**: Tracked in `app_storage.json` under `projectsOrder`.
   - macOS: `~/Library/Application Support/Antigravity/app_storage.json`
   - Linux: `$XDG_CONFIG_HOME/Antigravity/app_storage.json` (or `~/.config/Antigravity`)
   - Windows: `%APPDATA%\Antigravity\app_storage.json`
2. **Project Configurations**: Stored in `~/.gemini/config/projects/<id>.json`,
   defining `project_id`, workspace paths, and display labels.
3. Unassociated conversations (`project_id = "outside-of-project"`) are not
   grouped in the project tree; assigning a registered project ID ensures instant
   sidebar visibility.

## Protobuf subset and Hub cache

The adapter contains a bounded wire codec without generated vendor classes. It
accepts canonical varints, length-delimited bytes, and fixed 32/64-bit wire types.

The `Step` envelope uses:

| Field | Meaning |
| --- | --- |
| `1` | step type enum |
| `4` | step status enum |
| `19` | user-input payload when type is `14` (`CORTEX_STEP_TYPE_USER_INPUT`) |
| `20` | planner-response payload when type is `15` (`CORTEX_STEP_TYPE_PLANNER_RESPONSE`) |
| `140` | generic-tool payload when type is `132` (`CORTEX_STEP_TYPE_GENERIC_TOOL`) |

Status `3` is done, status `5` is cleared, and status `7` is error. User-input
field `2` stores visible user text. Planner-response field `1` stores visible
assistant text, field `6` stores a fresh message ID, and repeated field `7`
stores tool calls.

### Hub summary cache (`agyhub_summaries_proto.pb`)

Antigravity Desktop caches summaries in `agyhub_summaries_proto.pb`. On startup,
the native Go process reconciles SQLite records with this protobuf cache. If a
conversation exists in SQLite but has no entry in the cache or `raw_summary`
column, metadata fields reset to zero defaults (`0001-01-01`). The adapter
synchronizes both stores during installation.

## Conversion policy

| Portable event | Antigravity Desktop representation |
| --- | --- |
| User text | done user-input step |
| Assistant text | done planner-response step |
| Tool call | planner-response tool call |
| Tool result | following generic-tool step |
| Thinking/reasoning | omitted and counted as `thinking:private` |
| Compaction | omitted and counted |
| System/developer message | omitted as privileged context |
| Images and other context | omitted by type and counted |
| Unknown/opaque source event | omitted by reason and counted |

Private chain-of-thought is not portable. Thinking bytes and model signatures are
omitted from generated outputs, and source reading emits only a content-free
`thinking` marker.

A tool result without a preceding call receives a fresh synthetic call and an
explicit orphan counter. Duplicate or out-of-order IDs are rewritten or counted
to prevent corrupted native trajectories.

## Reading and installation

Source databases may have active write-ahead logs (`.db-wal`). The reader uses
SQLite's backup API to obtain a transactionally consistent snapshot, then hashes
and parses that snapshot. It validates:

- exact table, column, and schema invariants;
- SQLite page bounds and `integrity_check`;
- valid canonical UUIDv4 IDs and source `1` / `17` discrimination;
- contiguous step indices and supported step types; and
- structurally valid, bounded protobuf values.

Native installation creates target directories privately, reserves the summary
ID with `BEGIN IMMEDIATE`, writes the database atomically, and commits the
summary transaction. If a transaction fails, only newly created files are
unlinked. Existing sessions are never overwritten.

## Native validation evidence

1. **Synthetic Round-Trip Invariants**: Synthetic multi-turn sessions with tool
   invocations, results, and thinking events serialize and re-parse with exact
   fidelity, asserting that thinking text is removed.
2. **Native Language Server Verification**: The official Go binary
   (`language_server`) was executed in an isolated sandbox, queried over Connect-RPC
   (`GetCascadeTrajectorySteps`), and asserted to return all imported steps with
   identical structure.
3. **Live User Session Migration**: A live ESPHome voice satellite conversation
   with 256 steps was migrated from Antigravity CLI to Antigravity Desktop,
   confirming complete rendering and sidebar searchability.

Run the test suite with:

```console
uv run pytest -q tests/test_antigravity_desktop_format.py
```

Run the native language server oracle (macOS with Antigravity Desktop installed):

```console
uv run pytest -q tests/test_antigravity_desktop_native.py
```

## Known boundaries

- Native oracle hash validation is pinned to official release builds. Unvalidated
  binaries can be tested with `SESSION_MIGRATE_UNVALIDATED_DESKTOP_BIN=1`.
- Generic tool transport is visually render-proven; internal vendor-privileged tools
  are preserved as generic tool steps to maintain auditability.
- Private reasoning tokens, system instructions, and ephemeral UI selections are
  not transferred.
- Subagent conversations are mapped to independent portable sessions with their
  parent reference preserved in metadata.
