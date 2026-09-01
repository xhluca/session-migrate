# Session migration specification

This is the user-facing contract for `session-migrate` 0.10.0.

## Scope

The migrator reads and writes native sessions for:

- Claude Code
- Codex CLI legacy rollouts
- Pi v3
- Oh My Pi 18.0.5 v3
- OpenCode
- GitHub Copilot CLI
- Antigravity CLI
- Cursor Agent (experimental, pinned, text only)
- Mistral Vibe 2.24.3
- Muse Code 0.2.1
- Qwen Code 0.22.1
- Kimi Code 0.38.0
- Grok 1.0.5
- Kilo Code 7.5.0
- OpenHands 1.16.0
- Hermes Agent 0.20.6
- MastraCode 0.37.1
- Devin CLI 3000.6.7

Every source can target every destination, including itself. A migration
creates a new independent target session; it does not move/delete the source,
clone runtime state byte-for-byte, synchronize future turns, or re-execute
historical tools.

Codex paginated/history-base lineage and Claude sidechain import remain
non-transferable. They are discoverable in the catalog and fail closed.

## Functional requirements

### Inspect

`inspect` identifies one native artifact and prints only structural inventory:
format, path, bytes/hash, record counts, native identity/CWD/version/timestamp,
roles/block/event counts, and tool counts. It never prints message/tool/media
bodies or titles.

### Convert

`convert` accepts one file-based source and produces:

1. one complete target artifact or official OpenCode/Kilo import bundle; and
2. one adjacent schema-v2 content-free manifest.

It never installs or invokes a target CLI.

### Import

`import` converts and installs into the target's native store. It validates the
artifact before publication, refuses every collision, and writes a private
manifest. OpenCode and Kilo use only their official pinned importers.
Antigravity and Cursor use clean-room, exact-version database installers. Vibe,
Kimi, Grok, and OpenHands publish their native multi-file session directories;
Muse and Qwen publish one native JSONL plus a manifest. Hermes uses its pinned
official importer; MastraCode and Devin transactionally merge one validated
identity into their shared databases.

### Transfer

`transfer` resolves a native ID within one selected home or consumes an exact
catalog ID, authoritatively loads the source, converts it, and installs the
target. A requested source ID must be present and match native metadata.

### Catalog

The catalog exhaustively enumerates all recognized sessions within configured,
auto-detected, or explicitly bounded-discovered roots. It indexes native
names/titles and IDs, including archives, parents/subagents, duplicates,
unsupported/corrupt entries, missing Copilot/Cursor stores, and
  OMP/Vibe/Muse/Qwen/Kimi/Grok/Kilo/OpenHands/Hermes/MastraCode/Devin sessions.
It does not
promise whole-disk discovery or content search.

## Portable event model

A `Session` contains source identity/metadata plus an ordered tuple of `Event`:

| Event | Portable intent |
| --- | --- |
| `message` | User/assistant/system text and supported blocks |
| `tool_call` | Name, call ID, and structured/free-form input |
| `tool_result` | Call ID, text/structured result, media, error marker |
| `thinking` | Private/readable reasoning marker; never blindly transferred |
| `compaction` | Portable summary/checkpoint semantics |
| `context` | Supported standalone media/runtime context |
| `opaque` | Counted source-only state or unsupported structure |

Provenance retains record ordinal/type and, when available, source ID/block
ordinal. Ordering and linkage are validated independently of physical file
order where the native format is a graph.

## Mapping policy

1. Preserve ordered user/assistant text on every route.
2. Preserve linked tool calls/results, supported user images, and portable
   compaction when both source and target have a verified mapping.
3. Never downgrade privileged system/developer/meta input into a user prompt.
4. Never transfer private/model-bound thinking as ordinary assistant output.
5. Never invent tool output, reasoning, system instructions, credentials, or
   workspace data.
6. Synthesize only target-required structural IDs, timestamps, and default
   provider/version/model metadata. Source approval/sandbox policies are
   omitted rather than reconstructed.
7. Report every known omission, transformation, grouping, synthesized fallback,
   and retained linkage inconsistency through exact counters/warnings.
8. Reject an artifact with no resumable portable conversation.

Cursor intentionally implements only item 1. Every other portable class is
counted as unsupported for that experimental target.

## Version policy

Each writer has a pinned schema/build. `--target-cli-version` changes only a
metadata label and emits a warning; it never selects a different architecture.
Automatic OpenCode, Kilo, Antigravity, and Cursor install requires the exact
pinned runtime. Cursor additionally requires exact digests/sizes for four
shipped artifacts.

OMP's current fixed-title-slot v3 writer is pinned to 18.0.5. Vibe's writer
and append-boundary fingerprint are pinned to 2.24.3. Muse,
Qwen, and Kimi writers are pinned to 0.2.1, 0.22.1, and 0.38.0. Grok, Kilo,
and OpenHands are pinned to 1.0.5, 7.5.0, and 1.16.0. Hermes, MastraCode, and
Devin are pinned to 0.20.6, 0.37.1, and 3000.6.7. Installation
does not invoke Vibe; the exact CLI is exercised by the credential-free native
resume gate.

Unknown future schemas, mixed decisive markers, malformed JSON/protobuf/SQLite,
missing/gapped native linkage, duplicate unsafe identity, and source mutation
fail closed.

## Storage and publication

- No existing target is overwritten; there is no force flag.
- New transcript/manifest/database files are private (`0600`).
- New application/session directories are private (`0700`).
- Existing directory permissions are preserved.
- Publication is no-clobber and rollback-aware.
- OpenCode and Kilo SQLite are never directly written.
- Hermes, MastraCode, and Devin shared stores are changed only by validated,
  collision-safe native transactions for the selected new identity.
- Antigravity's summary database is updated transactionally only as part of its
  verified native install.

The source remains untouched on success and failure.

## Manifest contract

The schema-v2 manifest contains:

- migration version and creation time;
- source format/path/hash/native ID/version/structural counts;
- target format/path/hash/native ID/version/CWD/timestamp/counts;
- `dropped_events` counters; and
- warning objects.

`dropped_events` is a historical field name and includes transformed or
retained-with-warning details, not only erased records. The manifest omits
conversation bodies but its paths, IDs, timestamps, CWDs, and hashes may be
sensitive.

## Input bounds

Default limits are 256 MiB per source artifact, 64 MiB per JSON record/native
blob, and 100,000 JSON records, plus format-specific node/depth/blob limits.
SQLite sources must be regular non-symlink files and pass schema/integrity
checks. Readers verify stable source identity or take a consistent WAL-aware
snapshot.

## Privacy boundary

The migrator never copies external authentication/config stores. It performs no
redaction, encryption, or secret scan: credentials embedded inside supported
messages, tool arguments/results, or images are copied. Treat the target as
sensitively as the source.

Catalogs/manifests/inspect output omit bodies but retain operational metadata.
The catalog database is private-permission but unencrypted.

## Compatibility definition

“Resumable” means the exact pinned target CLI discovered/loaded the generated
native session and the portable prefix survived its native parser. It does not
promise identical model behavior, provider availability, live process state,
or future compatibility with an untested CLI release.

Antigravity and Cursor support is clean-room, unofficial, and version-pinned.
Cursor remains experimental because the private format is pinned to one exact
Linux build and target writing is text-only. Exact public-client source capture
now proves a real vendor-backed checkpoint, sanitized cold reload, and same-ID
continuation; the existing native TUI/loader/backend-blob evidence separately
proves generated text history is native-resolvable.
