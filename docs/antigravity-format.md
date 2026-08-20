# Antigravity CLI session format

`session-migrate` supports Antigravity CLI through a clean-room adapter pinned
to one exact release:

| Property | Pinned value |
| --- | --- |
| CLI | `agy 1.1.16` for Linux x86_64 |
| Executable size | `205,545,512` bytes |
| Executable SHA-256 | `b233e6a4f38564a06a0d3220aa79f6a7c8f11da2b85fc8f0957f8a14d46e6cc9` |
| ELF build ID | `95255e71338e3256f85c7f71f03ecaa0` |
| Conversation store | `~/.gemini/antigravity-cli/conversations/<uuid>.db` |
| Picker index | `~/.gemini/antigravity-cli/conversation_summaries.db` |

This is deliberately not a claim of compatibility with later Antigravity
versions. Automatic installation checks the executable version, byte size, and
digest before it touches the native store.

The independently observed schema notes and synthetic standard-library fixture
generator are published in the unofficial
[Antigravity session interoperability repository](https://github.com/xhluca/antigravity-session-interoperability).
No vendor code, binaries, descriptors, credentials, or real transcripts are
included there or here.

## How the format was established

The adapter was derived from independent observations of synthetic sessions:

1. create a conversation with unique marker text in an isolated `HOME`;
2. inspect SQLite schema and protobuf wire numbers, lengths, and enum values;
3. vary one field at a time and compare the resulting bytes;
4. build a new database without copying any native database or metadata blob;
5. load it in the actual 1.1.16 TUI, append a turn, and decode the appended row;
6. repeat with a synthetic generic tool call and result.

No vendor binary, descriptor set, generated binding, decompiled source, real
conversation content, credential, or account identifier is distributed in the
repository. The committed fixture contains only synthetic wire values produced
from the field map below.

## SQLite layout

Each conversation is an independent SQLite database with these tables:

```text
trajectory_meta
steps
gen_metadata
executor_metadata
parent_references
trajectory_metadata_blob
battle_mode_infos
```

The main `trajectory_meta` row contains a fresh trajectory UUID, the
conversation UUID, trajectory type `4` (cascade), and source `17` (CLI).
`steps.idx` is a contiguous zero-based sequence. Every step duplicates its type
and status in both the SQLite columns and the serialized `Step`; the validator
requires them to agree. The four auxiliary metadata tables are created empty.

The writer emits a minimal `trajectory_metadata_blob` named `main` containing:

| Field | Meaning |
| --- | --- |
| `2` | creation timestamp |
| `6` | root conversation UUID |
| `18` | `default-cli-project` |

The source reader accepts extra native trajectory/runtime metadata but projects
each retained-only row as an opaque loss event. It never silently treats those
values as conversation text.

The picker database contains one `conversation_summaries` row per conversation.
The installer writes the title, preview, exact step count, workspace file URI,
timestamps, last user-step index, project, source, and application-directory
fields observed in 1.1.16. It does not change `cache/last_conversations.json`;
that file is current UI selection state, not transcript truth.

## Protobuf subset

The implementation contains a small bounded wire codec, not generated protobuf
classes. It accepts canonical varints plus the ordinary protobuf wire types,
rejects groups, caps every length-delimited value, and caps the total field
count.

The `Step` envelope uses:

| Field | Meaning |
| --- | --- |
| `1` | step type enum |
| `4` | step status enum |
| `19` | user-input payload when type is `14` |
| `20` | planner-response payload when type is `15` |
| `140` | generic-tool payload when type is `132` |

Status `3` is done and status `7` is error. User-input field `2` stores visible
user text. Planner-response field `1` stores visible assistant text, field `6`
stores a fresh message ID, and repeated field `7` stores tool calls. A tool call
uses fields `1` ID, `2` name, and `3` arguments JSON.

The generic-tool payload has repeated field `1` argument-map entries and field
`2` result. Each map entry uses fields `1` key and `2` JSON-rendered value; the
result text is field `1` of the nested result. This fallback is intentional: it
retains a portable tool name, JSON arguments, call ID, result, ordering, and
error status without pretending an arbitrary source tool is one of
Antigravity's privileged built-ins.

Source parsing also has conservative projections for the observed `run_command`,
`view_file`, `shell_exec`, and MCP shapes. Other native step types become opaque
events with their numeric type. They are never guessed into text or tools.

## Conversion policy

| Portable event | Antigravity representation |
| --- | --- |
| User text | done user-input step |
| Assistant text | done planner-response step |
| Tool call | planner-response tool call |
| Tool result | following generic-tool step |
| Thinking/reasoning | omitted and counted as `thinking:private` |
| Compaction | omitted and counted; the stored schema has no proven compaction record |
| System/developer message | omitted as privileged context |
| Images and other context | omitted by type and counted |
| Unknown/opaque source event | omitted by reason and counted |

Antigravity planner payloads have fields for visible thinking, raw thinking, and
signatures. The source reader emits only a content-free `thinking` marker when
any of them is present. The writer never copies the text or signature. This
matches the project-wide rule that private chain-of-thought is not portable.

A tool result without a preceding call receives a fresh synthetic call and an
explicit orphan counter. Duplicate or out-of-order IDs are rewritten or counted
rather than producing an ambiguous native trajectory. Non-text tool-result
blocks are counted because the render-proven generic result is text-only.

## Reading and installation

Source databases may have live write-ahead logs. The reader uses SQLite's backup
API to obtain one transactionally consistent, bounded snapshot, then hashes and
parses that snapshot. It validates:

- the exact table, column, declaration, primary-key, and index layout;
- SQLite page bounds and `integrity_check`;
- a unique main CLI cascade and matching canonical UUIDv4 IDs;
- contiguous step indices and supported step encoding;
- matching row/payload step type and status; and
- bounded, structurally valid protobuf and JSON values.

Native installation creates the conversation directory privately, reserves the
summary ID with `BEGIN IMMEDIATE`, publishes the conversation DB using atomic
create-if-absent semantics, and commits the summary. Failure removes only the
newly published inode. Existing conversations are never merged or overwritten.
A dry run performs the version, schema, and collision checks without creating a
directory or database.

Credentials are outside this format. The migrator neither reads nor copies
Google OAuth tokens, Gemini API keys, browser state, or application settings.
The native test duplicates an already-present token byte-for-byte into a
temporary isolated `HOME` only when that test is explicitly run; it never
interprets or prints the value.

## Native validation evidence

Three independent 1.1.16 runtime checks were completed on 2026-08-20:

1. A new database built entirely from the clean-room encoder loaded in the
   full-screen TUI. The TUI rendered the imported user and assistant messages.
2. A planner tool call followed by a generic result rendered as an expandable
   `Tool(TOOL_RESULT_OMEGA)` row.
3. A database generated by the final adapter was installed into an isolated
   copied-auth home and resumed with the real CLI. `agy --conversation ...
   --print AGY_ADAPTER_APPEND_GAMMA` appended a native user step and error step
   because the isolated account exposed no usable model. The database grew from
   four to six rows; the original messages/tool trajectory reparsed exactly,
   the append marker decoded from the new user step, and private thinking bytes
   were absent before and after resume.

For the final adapter oracle, the consistent database snapshot changed from
SHA-256 `e87eb466d270493d2f8549e36d1479fecdf86135d58f41cb7d3169eb9cec45f5`
to `4d6496c52d9cd48f0ca3626e53f1ef8039a4de4966c5a8fa35cc3724bbd827c8`.
These are disposable synthetic artifacts, not published conversation files.

The native test is credential-optional:

```console
uv run pytest -q tests/test_antigravity_native.py
```

It skips unless the exact pinned binary and existing local OAuth state are
available. The credential-free structural suite is always runnable:

```console
uv run pytest -q tests/test_antigravity_format.py
```

## Known boundaries

- The format is version-private. A different executable hash fails closed.
- Generic tool transport is TUI-render proven. Built-in tool payloads are read
  conservatively, but only synthetic generic-tool execution was available for
  native visual validation.
- A failed resumed model turn still proves load and append, but not successful
  provider generation. Authentication and model availability remain the user's
  responsibility.
- Subtrajectories, battle mode, permissions, sandbox policy, model runtime
  metadata, memories, artifacts, media, and UI state are not migrated.
- The reader follows the main stored step sequence; auxiliary trajectories are
  accounted for as opaque metadata rather than flattened into that sequence.
