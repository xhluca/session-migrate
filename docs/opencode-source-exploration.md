# OpenCode source exploration (1.17.20)

This note records the evidence behind OpenCode-as-a-source support. All native
trajectories used synthetic markers in isolated XDG directories. No account
credentials or private session contents were read.

## Pinned artifacts

| Artifact | Observation |
| --- | --- |
| Installed CLI | `~/.opencode/bin/opencode --version` returned `1.17.20` |
| Installed binary SHA-256 | `373af49ceba30c1b64e964463a64f8065103f942f240933a955f6c461e1a67f6` |
| Official repository | `anomalyco/opencode` |
| Official tag | `v1.17.20` |
| Tag commit | `4473fc3c9055046183990a965d68df3db7ea6f62` |

The relevant pinned source files are:

- `packages/opencode/src/cli/cmd/export.ts`
- `packages/opencode/src/cli/cmd/import.ts`
- `packages/opencode/src/session/message-v2.ts`
- `packages/schema/src/v1/session.ts`
- `packages/core/src/session/sql.ts`

## Supported interface, not a database rewrite

OpenCode 1.17.20 exposes both halves of a stable-enough public interchange:

```text
opencode export [sessionID] --pure
opencode import <file> --pure
```

`export` asks `Session.Service` for the session and every paged message, then
writes one JSON object containing `info` and `messages`. Each message contains
its `info` and ordered `parts`. `import` decodes session, message, and part data
with the official Effect schemas. It overrides project/directory/path with the
current OpenCode instance, then writes the `session`, `message`, and `part`
tables itself.

That boundary matters. The migrator reads official export bundles and invokes
the official importer for writes. It does not couple normal operation to the
private SQLite layout. The SQLite source was inspected only to understand and
test the contract:

- `session` owns title, version, directory, model, lineage, summary, and time.
- `message` stores role-specific JSON plus a separately indexed creation time.
- `part` stores typed JSON and is ordered by `(message_id, id)`.
- `todo` is a separate table and is not included by the 1.17.20 exporter.
- importing an existing ID updates session location but uses conflict-ignore
  for messages and parts. Callers therefore must collision-check instead of
  treating import as an overwrite API.

The public `export` path pages messages by creation time and ID, then returns
them oldest-first. The adapter preserves that exported order. Assistant
`parentID` identifies the user turn that produced an assistant step; it is not
a generic single-parent transcript tree.

## Portable projection

| OpenCode native value | Portable event |
| --- | --- |
| user/assistant `text` part | message |
| user image `file` part with HTTP(S) or validated data URL | context image |
| assistant `reasoning` part | thinking |
| assistant `tool` part | tool call, plus result for completed/error state |
| image tool attachment | tool-result image block |
| compaction user part + summary assistant | one compaction summary |
| user `system` string | privileged system message |
| session title/version/model/provider/time/directory | `Session` metadata |

Model and provider use the latest exported message metadata, falling back to
session-level model metadata. The source SHA-256 covers the exact exported
bundle given to the adapter.

## Explicit loss accounting

OpenCode has native state with no neutral replay equivalent. The reader emits
reasoned opaque events rather than silently dropping it. Covered categories
include session lineage/summary/revert/share/permissions/metadata, message
summary/output-format/tool-policy metadata, ignored or empty text, non-image
files and file-source spans, incomplete tools, compacted tool output flags,
tool metadata, unpaired compactions, and structural `step-*`, snapshot, patch,
agent, subtask, and retry parts.

Reasoning is a first-class portable event even though some destinations elect
not to replay it. A paired compaction carries whether OpenCode supplied
`tail_start_id` or overflow boundary metadata, allowing target loss reports to
account for the nonportable boundary.

## Validation and hostile-input behavior

The source reader fails closed before producing a `Session` when it sees:

- non-UTF-8, non-JSON, duplicate JSON keys, NaN, or infinity;
- JSON nesting deeper than 128 levels;
- bundles over 256 MiB, one million messages, or four million top-level parts;
- missing session ID/directory/title/version/time metadata;
- duplicate/cross-session message or part IDs;
- an unknown pinned-version part type;
- malformed role, model, token, tool-state, attachment, or timestamp fields;
- a bundle with no resumable text/file/tool/compaction context.

The generous count ceilings are denial-of-service bounds, not expected normal
session sizes. The byte limit remains the primary memory bound.

## Native evidence

`tests/fixtures/opencode-source-1.17.20/comprehensive.json` is synthetic and
schema-valid. It covers text, system context, an image, reasoning, a completed
tool with an image attachment, compacted-result metadata, compaction, and
several intentionally nonportable native parts.

The native test performs this credential-free trajectory against the exact
installed 1.17.20 binary in a temporary HOME/XDG store:

1. official import of the comprehensive bundle;
2. read-only `opencode db` count check: 1 session, 4 messages, 13 top-level
   parts (the tool attachment is embedded in tool state);
3. official export and source parsing;
4. portable serialization under a new session ID;
5. official re-import and re-export;
6. source parsing again and marker checks for text, tool arguments/results,
   both images, and the compaction summary.

The ignored text and OpenCode-only structures are absent after the portable
loop while every omission is present in the serializer's loss report. Existing
native coverage separately resumes an imported session against a local
OpenAI-compatible loopback server, proving that OpenCode sends migrated history
back to a model without requiring real credentials.

## Integration points

The adapter entry point is `opencode.parse_session(path)`. Shared integration
must:

1. recognize official bundle JSON as `AgentFormat.OPENCODE`;
2. call `parse_session`, not the lower-level compatibility alias `parse`;
3. export an ID through the exact pinned CLI into a private regular file before
   conversion when the user selects a native OpenCode session;
4. index title, ID, directory, timestamps, version, and model from official
   `session list`/`export` output without storing transcript text in the catalog;
5. delete the private temporary export after parsing and preserve the existing
   collision checks on target import.

Direct discovery of `opencode.db` is not an interchange contract. The CLI is
the authority for selecting and exporting sessions from its active XDG store.
