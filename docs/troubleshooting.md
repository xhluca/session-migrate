# Troubleshooting and recovery

The bridge is intentionally fail-closed. Most failures mean no target file was
created and the source was untouched. Commands return exit status `2` and
print a content-safe explanation to standard error.

## Start with a dry run

For native installation, first pin a fresh target UUID so the same command can
be reviewed and repeated:

```console
session-bridge import SOURCE.jsonl --to codex \
  --cwd /target/project \
  --session-id 11111111-1111-4111-8111-111111111111 \
  --dry-run
```

Review `warnings`, `dropped_events`, `cwd`, `output`, and `manifest`, then run
the same command without `--dry-run`. `transfer` supports the same pattern.
The real conversion is regenerated, so target structural IDs and SHA-256 can
differ. A missing/invalid source timestamp can also change a Codex date path;
review the applied JSON rather than assuming byte- or path-identical output.

## Source changed while being read

The source CLI appended to or replaced the transcript during detection,
parsing, or hashing. Wait for that turn to finish and retry. Do not copy a
partially written file. The bridge compares device, inode, size, and
nanosecond modification time across the read to avoid mixing snapshots.

## Cannot detect the format

Use a content-free inspection with an explicit source format:

```console
session-bridge inspect SOURCE.jsonl --format claude --json
session-bridge inspect SOURCE.jsonl --format codex --json
```

An explicit format resolves ambiguous markers but does not make malformed or
unsupported history valid. A file containing decisive markers for both formats
is rejected by automatic detection; do not use `--format` to force a genuinely
mixed transcript through one adapter.

## No resumable conversation history

The source parsed, but its portable subset contained no user/assistant
conversation, tool activity, or supported compaction that the target can
resume. Metadata, titles, private reasoning, or unsupported attachments alone
are insufficient. The bridge refuses to create a metadata-only native session.

## Source is already the requested target format

The bridge only converts between agents; it is not a normalizer for a
same-format transcript. Choose a different supported `--to` value. To duplicate or move
a native session within one agent, use that agent's supported workflow rather
than rewriting it through the bridge.

## Standalone Claude sidechain or subagent

Nested subagent histories are intentionally outside the conversion
scope. Transfer the top-level parent session instead. Do not flatten a
sidechain by removing `isSidechain`; its context and ancestry differ from a
normal main conversation.

The catalog still inventories nested sidechain JSONL files as `unsupported` and
indexes their non-content `agentId`/filename key. This makes the complete native
store auditable without implying that a standalone sidechain can be resumed by
another target.

## Catalog does not show a custom or project-local home

“All sessions” means every session under every cataloged root, not every JSONL
on the machine. Add an arbitrary home directly:

```console
session-bridge catalog refresh --claude-root /path/to/claude-home
session-bridge catalog refresh --codex-root /path/to/codex-home
```

Or search for conventional project-local hidden homes inside a bounded tree:

```console
session-bridge catalog refresh --discover-under /path/to/workspaces
```

Run `session-bridge catalog roots list` to audit the exact search boundary.
Discovery never follows directory symlinks or widens beyond the supplied tree.

## Catalog entry is stale, busy, missing, or corrupt

Run `catalog refresh` after the native CLI finishes writing. An unavailable
root keeps its previous rows instead of marking everything missing. A
successfully scanned root marks disappeared files `missing`; add
`--include-missing` to see them. `busy` means the source changed during the
scan. `corrupt` or `unsupported` entries remain searchable but cannot be passed
through `transfer --catalog-id`.

The catalog is derived state. If its SQLite file is damaged, move it aside and
refresh; do not delete or edit native JSONL or Codex's `state_*.sqlite`.

## Unsupported Codex history mode

Codex paginated history and `history_base` lineage are rejected. Do not remove
the guard or rewrite the metadata to `legacy`: real paginated roots contain
contextual user-role records that require different visibility semantics, and
blind conversion can inject stale environment state as a user prompt.

Keep the original Codex session and use a supported legacy rollout, or wait for
scoped paginated support. The exact research and prerequisites are documented
in the [validation report](validation-report.md).

## Source UUID was not found or was ambiguous

For Claude, pass both the source home and original project CWD:

```console
session-bridge transfer SOURCE_UUID --from claude \
  --source-home /state/claude \
  --source-cwd /original/project \
  --cwd /target/project \
  --dry-run
```

Claude's encoded project-directory names can collide, so more than one match
is never guessed. For Codex, check both active and archived stores; a duplicate
UUID in both locations must be resolved outside the bridge before retrying.
The filename is not sufficient: the transcript's native session metadata must
also be present and match.

## Refusing to overwrite an existing target

Either the planned transcript or manifest path already exists, including a
broken symbolic link. The bridge never overwrites it, even during dry-run.

The safest recovery is to omit `--session-id` and generate a new UUID. If a
fixed UUID is required, inspect the reported existing paths and move or remove
them only after independently confirming they are disposable. The bridge has
no force flag.

If manifest publication fails after the bridge creates a new transcript, the
bridge removes only the transcript inode it created. It does not remove a file
that another process replaced during the failure. The source is never modified.

For OpenCode, collision detection uses its official `session list`. A
zero-length private manifest can be a reservation left by an interrupted
attempt; do not remove it until you have also listed native OpenCode sessions.
If the importer succeeded but later cleanup/finalization failed, the error says
the native session may already exist. Verify before retrying with a fresh ID.

## The target CWD does not exist

Conversion succeeds with `cwd_not_directory`, because the target path may
exist only inside a container or on another machine. Ensure that the recorded
absolute path is the directory from which the target CLI will resume. For a
host-to-container transfer, pass the path as seen inside the container, not the
host bind-mount source path.

## The imported session is absent from the picker

Resume explicitly by the UUID printed by the bridge and use the imported CWD:

```console
cd /target/project
codex resume TARGET_UUID
```

```console
cd /target/project
claude --resume TARGET_UUID
```

Codex may rebuild its SQLite index on first explicit resume. Claude does not
require `sessions-index.json` for explicit resume. Picker recency, indexing,
and CWD filters are not a reliable acceptance test.

Pi should be resumed from the exact path printed by the bridge:

```console
pi --session /exact/path/from-the-import-result.jsonl
```

OpenCode owns its native store and resumes by its `ses_` identifier:

```console
opencode run "follow-up" --session ses_TARGET_ID --pure
```

## OpenCode importer or version error

Automatic import requires OpenCode `1.17.20`; changing
`--target-cli-version` cannot bypass the observed-binary check. Select the
binary with `--target-cli`, `OPENCODE_BIN`, `PATH`, or the normal
`~/.opencode/bin/opencode` fallback. `--home` is deliberately rejected because
the official CLI chooses storage through its normal HOME/XDG environment.

OpenCode `--dry-run` does not import a conversation, create a temporary bundle,
or publish a bridge manifest. Its official `session list` collision probe can
still initialize normal XDG model-cache, SQLite/WAL/SHM, gitignore, log, and
lock files. Use an isolated HOME/XDG environment if the entire process must be
disposable.

## Resume appends an authentication or network error

Authentication is not transferred. A CLI can successfully discover and append
to the imported JSONL before a provider request fails. Configure the target CLI
normally, then resume a disposable copy if you are diagnosing credentials.
Never mount or copy external credential stores as part of transcript
conversion. Remember that embedded secrets in portable message/tool content are
not redacted.

## Warnings about source or target versions

`unvalidated_source_version` means a legacy-shaped source came from a version
other than the pinned integration version. `unvalidated_target_version` means
`--target-cli-version` changed the metadata label only; the writer still used
the pinned schema. Neither warning proves incompatibility, but both require a
fresh native-resume test before relying on a new CLI version.

## Tool-linkage warnings

Missing tool IDs/names receive linked synthetic fallbacks when possible.
Duplicate calls/results and orphan results are retained and explicitly
reported because the target CLI may diagnose or normalize them. Historical
tool records are never executed by the bridge.

## Input exceeds a safety limit

The defaults are 64 MiB per JSONL record, 256 MiB per file, and 100,000
non-empty records. They bound eager parsing and are not configurable through
the CLI. Do not split or truncate a native transcript arbitrarily; doing so can
break Claude ancestry or Codex compaction/linkage. Investigate the source shape
and add a tested streaming strategy before raising limits in code.

## Images are missing or may fetch remotely

Only PNG, JPEG, GIF, and WebP base64 data with valid syntax, or HTTP(S) image
URLs, are portable. Invalid data URLs, unsupported media types, audio, local
paths, and privileged/assistant-role standalone images are omitted and
counted. A preserved remote URL can cause the target CLI to fetch it later;
prefer base64 data for a self-contained offline transfer.

## Docker session disappeared or files are root-owned

The inspected image's stock `run.sh` uses `--rm` and does not persist the
workspace or session homes. Bind-mount explicit workspace and agent-home
directories. The image runs as root, so add an appropriate `--user` mapping if
host ownership matters. See the [Docker environment](docker-environment.md)
for exact paths and persistence hazards.

## Verify an installed artifact

The success JSON and manifest contain target and source SHA-256 digests. Verify
the installed target without printing it:

```console
sha256sum /path/to/imported-session.jsonl
```

The digest should equal `target.sha256` in the manifest. The source digest
records the exact input snapshot used for conversion. Hash equality verifies
bytes, not semantic completeness; review the manifest warnings and
[compatibility matrix](format-compatibility.md) as well.
