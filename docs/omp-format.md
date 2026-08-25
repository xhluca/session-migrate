# Oh My Pi session format

`session-migrate` reads and writes Oh My Pi (OMP) sessions as a first-class
format. The adapter is pinned to OMP `18.0.5`, upstream tag
[`v18.0.5`](https://github.com/can1357/oh-my-pi/tree/v18.0.5) at commit
`eab72e88e447a4be45bea2bc302995844c0c51a2`.

This document describes observed, versioned behavior. It is not a promise that
future OMP releases will retain the same local format.

## Native store and discovery

The default-profile agent directory is:

```text
~/.omp/agent
```

`PI_CODING_AGENT_DIR` replaces it. Named OMP profiles normally use
`~/.omp/profiles/<name>/agent`; register those explicitly with `--omp-root`.
Current session paths are:

```text
<agent-dir>/sessions/<cwd-bucket>/<timestamp>_<session-uuid>.jsonl
```

The migrator implements OMP 18.0.5's separate home-relative, temporary-root,
and absolute CWD bucket rules. Direct lookup accepts `--source-cwd`; the
catalog scans every workspace bucket and indexes the bounded native title and
UUID without indexing message bodies.

OMP and Pi deliberately share the `PI_CODING_AGENT_DIR` variable. Catalog root
selection therefore inspects a recognized journal head and registers that
custom root exactly once. A current OMP file starts with an exactly 256-byte
title slot. A current Pi file starts directly with its session header. The
override is expected to name one active agent family, not a manually mixed
store.

## Current journal

The current native sequence is:

1. one fixed-width JSON `title` record (`v: 1`);
2. one v3 `session` header containing ID, timestamp, and CWD; and
3. append-only entries linked by `id` and `parentId`.

The title slot is mutable, space padded, UTF-8 safe, and authoritative for
picker/search display. The reader also understands header titles and trailing
`title_change` entries. Generated targets always use the fixed-slot form.

Legacy slotless OMP v3 journals remain readable when the caller explicitly
selects `--format omp` or `--from omp`. Their head is indistinguishable from a
Pi v3 journal, so automatic file detection conservatively classifies that old
shape as Pi.

## Portable mapping

The active `id`/`parentId` ancestry is the model-visible history. The adapter
projects ordered user/assistant text, linked tool calls/results, supported
images, and compaction summaries. Content-addressed image references under
`<agent-dir>/blobs/<sha256>` are opened without following symlinks, bounded,
hash checked, and converted to self-contained portable image data.

OMP-specific runtime state is not silently treated as conversation:

- inactive branch entries are counted;
- credential pins, mode/service-tier changes, session initialization, and
  runtime injections become reason-specific opaque losses;
- private/provider-bound thinking is never replayed as assistant text; and
- a native `reset_boundary` hides all earlier active entries. Those entries
  are reported as `omp_pre_reset_entry` rather than resurrected in a target.

Every omission or transformation appears in the migration manifest. Auth,
configuration, extensions, profiles, and runtime processes are never copied.

## Commands

Find a named OMP session and migrate it:

```bash
smigrate catalog refresh
smigrate catalog search "fix timeline merging" --format omp
smigrate transfer --title "fix timeline merging" --from omp --to codex
```

Use a native ID and explicit project:

```bash
smigrate transfer SOURCE_UUID --from omp --source-cwd "$PWD" --to pi
```

Create OMP as the target and resume it:

```bash
smigrate transfer SOURCE_UUID --from claude --to omp --cwd "$PWD"
omp --resume NEW_SESSION_UUID
```

Use `--source-home` or `--home` for an isolated/custom active agent directory.
Use `--omp-root` to persist a custom source root in the catalog.

## Native acceptance evidence

The exact tested Linux x64 binary reports `omp/18.0.5` and has:

```text
bytes   183420104
sha256  d5a322af241cebe2662b3b792ff29d3ea6e61364328e916c9429065f346391ed
```

The opt-in test `tests/test_omp_native.py` uses an isolated home, agent store,
temporary directory, and loopback OpenAI-compatible provider. It does not read
or copy credentials. The test:

1. converts a sanitized Claude history containing tools and compaction to OMP;
2. opens that generated file with the exact OMP binary in RPC mode;
3. verifies the imported active prefix is returned by OMP;
4. sends a new prompt and verifies the OMP model request includes the imported
   user, tool-result, assistant, compaction, and follow-up history;
5. receives a synthetic provider reply and verifies OMP appended the turn
   without changing the imported body prefix; and
6. renames the session through OMP and reparses the updated native title slot.

Run it only with the exact pinned binary:

```bash
SESSION_MIGRATE_OMP_BIN=/path/to/omp-18.0.5 \
  uv run pytest -q tests/test_omp_native.py
```

The normal unit suite remains credential-free and network-free. Sanitized
fixtures cover title bounds, current/legacy detection, reset behavior,
branches, tools, compaction, blob hashes/symlinks, corrupt trees, discovery,
catalog search, and every ordered source/target route.

## Boundaries

- The target is a portable rewrite with a fresh identity, not a byte clone.
- Only the default profile is auto-detected. Register named profile agent
  directories explicitly.
- The binary hash is recorded as native evidence; ordinary JSONL installation
  does not execute or copy the OMP binary.
- Later OMP schemas must receive new sanitized fixtures and an exact native
  replay/append oracle before the pin changes.
