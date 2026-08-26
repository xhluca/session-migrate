# Troubleshooting

`session-migrate` fails closed. An error normally means no target was installed;
when an external/native installer may already have succeeded, the error says so
explicitly.

## Start with inspection and a dry run

```bash
smigrate inspect SOURCE --json
smigrate import SOURCE --to TARGET --cwd "$PWD" --dry-run
```

Review `warnings` and `dropped_events` before repeating the import without
`--dry-run`. Non-empty counters can mean omission, transformation, grouping, or
a retained inconsistency—not necessarily total record loss.

## Cannot detect the format

Automatic detection rejects weak or mixed decisive markers. If the file is
known and trusted, select its actual adapter:

```bash
smigrate inspect SOURCE --format claude
smigrate inspect SOURCE --format codex
smigrate inspect SOURCE --format omp
smigrate inspect SOURCE --format cursor
smigrate inspect SOURCE --format vibe
smigrate inspect SOURCE --format muse
smigrate inspect SOURCE --format qwen
smigrate inspect SOURCE --format kimi
smigrate inspect SOURCE --format grok
smigrate inspect SOURCE --format kilo
smigrate inspect SOURCE --format openhands
```

Forcing a format bypasses only detection. The adapter still rejects malformed,
wrong-version, or unsafe state.

## Source changed while being read

The native CLI appended/replaced/truncated the session during conversion. Stop
the active turn or copy a coherent snapshot, then retry. Do not repeatedly
convert a live transcript and assume the manifest hash matches the parsed
history. Antigravity/Cursor sources use WAL-aware SQLite snapshots but can still
fail if their file topology changes during snapshot creation.

## Collision / refusing to overwrite

There is no `--force`. Choose a fresh generated target ID or pass a different
`--session-id`; for `convert`, choose a new output path. Explicit UUID checks
cover the resolved native path and manifest. They are not a global scan of
every possible custom home.

After “native session may already exist,” inspect the target agent first. A
native install succeeded but manifest finalization/cleanup failed; blind retry
would risk duplication.

## No resumable conversation history

The source contains only metadata, reasoning, title/UI state, or unsupported
roles. A target requires at least one portable conversation turn. Cursor also
requires a user message because its native graph groups assistant steps beneath
a user turn.

## Same-format warning

Same-format migration is supported, but it is a portable rewrite, not a file
copy. The target gets a new ID and source-only runtime metadata may be omitted.
Review the manifest and do not expect an identical SHA-256.

## Claude session is ambiguous

Claude's encoded project directory can collide. Supply the exact project CWD:

```bash
smigrate transfer UUID --from claude --source-cwd /absolute/project --to codex
```

Pi, OMP, Cursor, Vibe, Qwen, Kimi, and Grok also accept `--source-cwd` to choose a
workspace-specific native store.

## Oh My Pi session is missing or detected as Pi

Current OMP 18.0.5 sessions begin with a fixed 256-byte native title record and
are detected automatically below `~/.omp/agent/sessions`. The default-profile
store honors the shared `PI_CODING_AGENT_DIR` override; the catalog inspects a
recognized journal head and registers that custom root once as Pi or OMP.

Older slotless OMP v3 journals are indistinguishable from Pi by their head.
Select the adapter explicitly and, when needed, the source home/CWD:

```bash
smigrate transfer UUID --from omp --source-home /path/to/omp/agent \
  --source-cwd "$PWD" --to codex
```

OMP reset boundaries intentionally hide earlier model context. The manifest
reports `omp_pre_reset_entry` instead of resurrecting that history. A generated
session resumes with `omp --resume NEW_UUID` from its recorded project.

## Codex active/archive duplicate

The same UUID exists in more than one native path. Use the catalog to select an
exact physical entry:

```bash
smigrate catalog refresh
smigrate catalog search UUID --include-paths
smigrate transfer --catalog-id RESULT_ID --to claude
```

Do not delete native sessions merely to satisfy lookup.

## OpenCode source or target errors

OpenCode source transfer and target import require exact pinned `1.17.20` and
its official `export`/`import`/`session list` commands.

- `--source-cli` selects its source exporter.
- `--target-cli` selects its target importer.
- `--home` is intentionally rejected; use an isolated normal `HOME`/XDG root.
- Auto-update and pruning are disabled for migrator subprocesses.

An OpenCode dry run can initialize ordinary XDG cache/database metadata because
the official list preflight is not side-effect-free. No imported session or
migrator artifact is written.

## Antigravity binary mismatch

Automatic Antigravity install supports exact `agy 1.1.16`. A metadata override
cannot bypass the binary version/digest gate. Install the pinned build or use
`convert` to inspect an artifact without native installation. Later builds need
a new observed-schema/native-oracle release.

Antigravity source files are:

```text
~/.gemini/antigravity-cli/conversations/<uuid>.db
```

If a live DB is locked, replaced, or has a different schema, retry after the CLI
stops or use the matching adapter version.

## Cursor binary mismatch or unsupported content

Cursor is experimental and supports exact Agent
`2026.03.20-44cb435`. Installation verifies launcher, main bundle,
protobuf-bearing chunk, bundled Node, sizes, hashes, and reported version.
Any drift fails before publication.

Cursor transfers ordered user/assistant text only. Tools, thinking, images,
attachments, compaction, system/runtime state, shell turns, and unknown graph
fields are intentionally counted and omitted. This is not a vendor-supported
import API. See [the exact boundary](cursor-format.md).

For direct lookup, the workspace affects the native path:

```bash
smigrate transfer UUID --from cursor --source-cwd "$PWD" --to claude
```

The catalog can locate it by ID/title without reconstructing the original CWD.

## Vibe session is missing, ambiguous, or rewritten

Vibe sources must contain both native files below one session directory:

```text
$VIBE_HOME/logs/session/session_*_<short-id>/meta.json
$VIBE_HOME/logs/session/session_*_<short-id>/messages.jsonl
```

Direct lookup uses the full UUID stored in `meta.json`; `--source-cwd` can
disambiguate custom roots. The installer rejects any existing directory that
shares the first eight UUID characters because Vibe's native resume lookup uses
that short suffix.

Generated metadata mirrors Vibe 2.24.3's exact last-message fingerprint so the
first native resume appends instead of rewriting the imported prefix. A later
Vibe version with different defaults is outside the current pin; upgrade
`session-migrate` rather than overriding only `--target-cli-version`.

## Muse, Qwen, or Kimi session is missing or rejected

These adapters are pinned to Muse `0.2.1`, Qwen `0.22.1`, and Kimi `0.38.0`.
Use their actual native roots:

```text
$XDG_DATA_HOME/muse/sessions/YYYY/MM/DD/<uuid>/session.jsonl
$QWEN_HOME/projects/<encoded-cwd>/chats/<uuid>.jsonl
$KIMI_CODE_HOME/sessions/<workdir-key>/session_<uuid>/
```

Qwen and Kimi direct lookup can use `--source-cwd`; Muse direct lookup is by
date-partitioned UUID. A Kimi source needs both `state.json` and
`agents/main/wire.jsonl`. Stop an active native turn and retry if a multi-file
snapshot changes while being read.

Migration never configures a model or moves authentication. For a Muse target,
`--model-provider meta --model MODEL` records appropriate native metadata, but
the target still needs its own provider configuration. The optional live
OpenRouter test procedure is documented in
[the format note](muse-qwen-kimi-formats.md); it is a release oracle, not a
credential-migration feature.

## Grok, Kilo Code, or OpenHands session is missing or rejected

These adapters are pinned to Grok `1.0.5`, Kilo Code `7.5.0`, and OpenHands
`1.16.0`. Grok and OpenHands use filesystem homes:

```text
$GROK_HOME/sessions/<encoded-cwd>/<uuid>/
$OPENHANDS_CONVERSATIONS_DIR/<uuid>/events/
```

Use `--source-cwd` when a Grok UUID is ambiguous. Kilo is a virtual source and
target backed by its normal XDG SQLite inventory; it deliberately rejects
`--home` and uses the exact official binary selected by `--source-cli` or
`--target-cli`. Kilo 7.5.0's JSON session-list command crashes on some valid
imports, so session-migrate uses an official per-ID export probe and discards
its body during collision checks. See
[the pinned format contracts](grok-kilo-openhands-formats.md).

## Codex paginated or history-base source

These lineage modes are recognized but unsupported. `--format codex` cannot
bypass the guard. The safe root-paginated subset still needs ordinal,
contextual-user, compaction/rollback/inter-agent, and lineage semantics before
it can be enabled.

## Claude sidechain/subagent

The catalog indexes nested sidechains and their agent IDs, but migration rejects
them. Transfer the parent main session or explicitly flatten the desired branch
outside this tool after reviewing privilege/context implications.

## Target CWD does not exist

The migrator can create an artifact but warns because native resume may filter
or contextualize by CWD. Pass the path the target CLI will actually use. For a
container, this is the inside-container path, not necessarily the host path.

## Catalog finds fewer sessions than expected

“All” means all recognized sessions under enabled roots. Check roots:

```bash
smigrate catalog roots list
smigrate catalog refresh --discover-under /bounded/workspace
smigrate catalog refresh --cursor-root /custom/cursor \
  --antigravity-root /custom/agy --vibe-root /custom/vibe \
  --muse-root /custom/muse --qwen-root /custom/qwen --kimi-root /custom/kimi \
  --grok-root /custom/grok --kilo-root /custom/kilo \
  --openhands-root /custom/openhands/conversations
```

Arbitrary custom directory names require explicit registration. Search defaults
to native title/name/ID metadata; it cannot find prose without a native title.
Use `--include-paths` only when path/CWD exposure is acceptable.

Catalog statuses are structural by default. `candidate` is not a conversion
guarantee; use `refresh --validate` or rely on authoritative validation during
transfer. OpenCode and Kilo candidates are validated only when the selected ID is
officially exported.

## Authentication or model failure after successful resume

Migration does not copy credentials or provider configuration. Authenticate the
target through its normal supported login. Historical validation used isolated,
test-only credential translations only where a target technically accepted the
same Codex OAuth provider schema; the tool does not perform this translation.

Native load can succeed even when the account has no available model. Inspect
the native history/picker first, then separately diagnose target authentication
or provider availability.

## Privacy

Inspect output and manifests omit message/tool/media bodies, but include paths,
CWDs, IDs, timestamps, hashes, and structural counts. The catalog adds native
titles/names. These are operationally sensitive.

The converter performs no redaction or secret scanning. Secrets embedded in a
supported message, tool argument/result, or image are copied. New files/dirs are
private, but existing parent directory permissions are preserved.
