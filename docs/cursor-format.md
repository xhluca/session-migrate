# Cursor Agent session format

`session-migrate` has an experimental clean-room adapter for the private Cursor
Agent resume store. Compatibility is deliberately limited to one Linux x86_64
release:

| Shipped artifact | Size | SHA-256 |
| --- | ---: | --- |
| `cursor-agent` launcher | 800 | `8756ac4a808cc90b220416ac8743560aa473a94d6fe5911bb602c250c046c4a3` |
| `index.js` | 7,361,289 | `a7961f327172fa9eecdf69d3941c86a5c2785103bebaf63183ad8e9522f3f620` |
| `891.index.js` | 11,839,834 | `7226059f6a648d5a25a4e0ef1f2bee363879baecc2468aa3ade4c6e481b15423` |
| bundled `node` | 129,074,464 | `e0e46d3a1c0667117303412647cafcbcefb1be7612493015ec8fd6b7440162a4` |

The CLI must also report version `2026.03.20-44cb435`. Installation fails
closed if the version, size, or digest of any of these four artifacts differs.
The pin is a compatibility boundary, not an assertion that later Cursor builds
use the same private format.

The independently observed graph description and synthetic standard-library
generator are published in the unofficial
[Cursor session interoperability repository](https://github.com/xhluca/cursor-session-interoperability).
No vendor code, binaries, descriptors, credentials, or unsanitized private
transcripts are included there or here. This repository's native-corpus fixture
was created by the exact public client from a synthetic media-boundary scenario,
then structurally sanitized and content-address rehashed by a replayable script.

## Scope and status

The adapter can read native resume databases and create a new, resumable native
database containing user and assistant text. The shipped runtime, actual TUI,
and backend-facing native blob protocol have all loaded a database produced by
the Python encoder.

The target is intentionally text-only. Tools, thinking, images, compaction,
system context, and runtime metadata are not injected into Cursor. Every
omission is counted. A native source record that cannot be represented becomes
an opaque accounting event with a reason such as
`cursor:tool_call:unsupported`; downstream writers retain that reason in their
own conversion-loss manifests.

Cursor also maintains readable JSONL under `~/.cursor/projects`, but that is not
the native Agent resume store. This adapter reads the content-addressed SQLite
store used by `cursor-agent --resume`.

## Clean-room method

The field map was established with synthetic content in isolated homes:

1. create a native conversation with unique non-secret markers;
2. inspect database schema and protobuf wire shapes without copying vendor code;
3. vary one value at a time and compare content-addressed blobs;
4. encode a new store from scratch with an independent bounded wire codec;
5. load that store through the shipped runtime and real TUI; and
6. ask the unmodified CLI for the imported blobs through a loopback-only fake
   Agent service.

The repository contains no vendor source, protobuf descriptor, generated
binding, executable, credential, account identifier, or private session. The
fixture contains only synthetic values and independently encoded wire bytes.

## Storage location

The tested config-root precedence is:

1. non-empty `CURSOR_CONFIG_DIR`;
2. `$XDG_CONFIG_HOME/cursor`; or
3. `~/.cursor`.

One conversation lives at:

```text
<config-root>/chats/<workspace-key>/<conversation-uuid>/store.db
```

`workspace-key` is lowercase MD5 of the normalized absolute workspace path.
The directory name and metadata `agentId` are canonical UUIDv4 values and must
match.

## SQLite and metadata

The exact accepted schema is:

```sql
PRAGMA user_version = 1;
CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
```

Every `blobs.id` is lowercase SHA-256 hex of its exact protobuf bytes. The
singleton `meta` row has key `0`; its value is hex-encoded UTF-8 JSON with these
fields:

| Field | Meaning |
| --- | --- |
| `agentId` | conversation UUIDv4 |
| `latestRootBlobId` | root blob SHA-256 |
| `name` | display title |
| `createdAt` | Unix time in milliseconds |
| `mode` | `default`, `auto-run`, `plan`, `background`, `search`, or `debug` |
| `lastUsedModel` | optional model identifier |
| `resumeBcId` | optional backend resume identifier |
| `currentPlanUri` | optional plan reference |

Unknown metadata fields fail closed. Optional runtime fields are readable but
are accounted for as omissions where they have no portable equivalent.

## Content-addressed protobuf graph

The encoder writes this minimal graph:

```text
metadata.latestRootBlobId
  -> conversation root
       repeated field 8: turn blob ID
          -> turn
               field 1: agent-turn value
                    field 1: user-message blob ID
                    repeated field 2: step blob ID
```

Each reference is exactly 32 bytes and resolves through the `blobs` table.

| Value | Wire mapping used by the adapter |
| --- | --- |
| Conversation root | repeated bytes field `8` = turn IDs |
| Turn | bytes field `1` = agent-turn; native bytes field `2` is a shell-turn variant and is not projected |
| Agent turn | bytes field `1` = one user ID; repeated bytes field `2` = step IDs; optional bytes field `3` = request ID |
| User message | string field `1` = text; string field `2` = message ID |
| Conversation step | oneof-like bytes field `1` = assistant; field `2` = tool call; field `3` = thinking |
| Assistant message | string field `1` = visible text |

The implementation is a small independent protobuf-wire codec. It accepts
canonical bounded varints and wire types 0, 1, 2, and 5; groups are rejected.
Every generated database is parsed again before installation.

## Projection and omission policy

| Semantic content | Native source | Native target |
| --- | --- | --- |
| User text | projected | written |
| Assistant text | projected | written |
| Tool call/result | counted, content omitted | counted, not written |
| Thinking/reasoning | counted, content omitted | counted, not written |
| Image/rich context | counted, content omitted | counted, not written |
| Compaction/summary state | counted, content omitted | counted, not written |
| System/subagent context | counted, content omitted | counted, not written |
| Model, timestamps, request IDs, mode, plan/backend state | selectively read; counted when dropped | counted, not written |

The reader recognizes observed semantic locations rather than treating arbitrary
bytes as text. For example, root field `1` is system content, root fields `6`,
`9`, and `11` are compaction-related state, user field `3` is selected context,
and user fields `11` and `12` are privileged/context resources. Unknown fields
remain reason-specific runtime-metadata losses.

Private thinking text and tool payload bytes are never copied into the portable
session. Their presence is represented only by content-free omission counts.

## Reader and installer behavior

`parse(path, cwd=...)` makes a bounded SQLite backup so committed WAL state is
included, validates it, then returns a `ParsedCursorSession`. Call
`project_session(parsed, source_format=...)` to create the portable `Session`;
this is the step that expands native loss counts into opaque accounting events.

`serialize(session, session_id=..., cwd=...)` returns `(database_bytes, losses)`.
The caller must surface `losses`. `install_database(...)` validates the bytes,
checks the exact pinned CLI, computes the safe relative path, and publishes with
private permissions and atomic create-if-absent semantics. It never overwrites
or merges an existing conversation. `dry_run=True` performs validation and
collision checks without publication.

Before accepting a native database the adapter checks:

- exact SQLite objects, columns, primary keys, and `user_version`;
- SQLite integrity plus bounded database, blob, metadata, and field counts;
- canonical UUIDv4 IDs and matching directory identity;
- SHA-256 identity for every blob, including unreachable blobs;
- one valid turn variant, one user reference, and valid 32-byte references;
- bounded, structurally valid protobuf and UTF-8 text; and
- no unsupported fields or unreachable blobs in adapter-generated output.

Credentials are out of scope. The adapter neither reads nor copies Cursor
tokens, account state, browser state, provider settings, or remote checkpoints.

## Native validation evidence

### Exact public-client source trajectory

`tests/native_corpus/v1/sources/cursor/2026.03.20-44cb435/portable-rich`
contains a vendor-backed source trajectory made by the exact pinned client. The
capture began with an empty isolated chat store and used the public client for
two turns under one native session ID. The first turn read text and Python,
visually decoded a PNG, extracted a PDF marker, and exercised Cursor's native
tool calls and results. Native `Read` rejected WAV and MP4 input; shell tools
then inspected those files, so the fixture records the rejected attachment
boundary without claiming native audio or video support. The store also contains
native thinking and compaction state, both exposed only as content-free losses.

The replayable `scripts/native-corpus/sanitize-cursor.py` makes a WAL-aware
SQLite backup, replaces path and account-shaped text, recursively rehashes every
changed content-addressed blob, and verifies schema, blob identity, artifact
digests, and the final portable projection. `provenance.json` states the result
for all ten corpus modalities, including rejected and unattempted cases; there
are no implicit support cells.

The deterministic gate reparses the sanitized database and migrates it to every
registered target. A separate opt-in gate copies the fixture into a fresh,
otherwise empty Cursor store, launches the same exact public client, resumes the
same native ID, checks semantic recall without reading the fixture files, and
persists a follow-up. This proves a real vendor-backed checkpoint survives a
cold sanitized-store reload and continuation. It does not expand target writing
beyond the documented user/assistant-text subset.

Run the deterministic source gate anywhere:

```console
uv run pytest -q tests/test_cursor_corpus_source.py
```

Run the live cold-reload gate only with the exact binary and disposable copies
of local Cursor authentication/configuration files:

```console
SESSION_MIGRATE_RUN_CURSOR_CORPUS=1 \
SESSION_MIGRATE_CURSOR_BIN=/path/to/2026.03.20-44cb435/cursor-agent \
SESSION_MIGRATE_CURSOR_AUTH_JSON=/path/to/auth.json \
SESSION_MIGRATE_CURSOR_CONFIG_JSON=/path/to/cli-config.json \
  uv run pytest -q -s \
  tests/test_cursor_corpus_source.py::test_exact_cursor_cold_reloads_sanitized_native_fixture
```

The live gate skips by default and never commits credentials.

### Synthetic target-store oracle

The opt-in oracle in `tests/test_cursor_native.py` uses only synthetic content,
a fake unsigned token, an isolated config/home, and a loopback-only service. It
performs three independent checks against the pinned installed runtime:

1. It evaluates the shipped loader with a test-only in-memory entrypoint hook,
   opens the Python-built store through Cursor's own `AgentKv`, and resolves one
   turn containing the exact synthetic user and assistant markers.
2. It launches `cursor-agent --resume=<id> --print --trust <new-user>` against a
   local fake Agent service. The CLI emits a run request containing the new user
   once and the imported turn pointer. When the service sends native `GetBlob`
   requests, the CLI returns the imported turn, user, and assistant blobs; the
   exact markers each resolve once. The command exits zero.
3. It launches the full-screen TUI in a 120x40 PTY and verifies that both
   imported messages are visibly rendered by the actual CLI.

The oracle intentionally forces HTTP/1.1 only inside its disposable config so
Python's local HTTP server can stand in for the Agent endpoint. It never contacts
Cursor or a model provider.

Run the structural suite anywhere:

```console
uv run pytest -q tests/test_cursor_format.py
```

Run the native oracle only when the exact pinned Cursor installation is present:

```console
SESSION_MIGRATE_RUN_CURSOR_NATIVE=1 \
  uv run pytest -q -s tests/test_cursor_native.py
```

Without the opt-in environment variable, the native test skips.

## Known boundaries

- This is a private, version-specific format. Any shipped-runtime drift is a
  hard error until a new clean-room audit and native oracle pass.
- Target writing is user/assistant text only. Tool execution, thinking,
  compaction, attachments, rich text, memories, plans, and runtime state are not
  synthesized.
- Source reading is conservative. Known unsupported native fields are counted;
  unknown future schema and malformed graphs fail closed.
- The native backend proof validates local resume and history resolution. It
  does not claim that Cursor supports an arbitrary transcript-import API; the
  adapter writes a pinned private store.
- The official ACP load surface and SDK bridge can resume native sessions but do
  not currently expose arbitrary historical assistant/tool injection. They are
  not used by this writer.
