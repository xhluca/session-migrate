# Exploration log

This is a chronological engineering notebook. Stable conclusions graduate to
`format-compatibility.md`; uncertain observations stay here with their evidence.

## 2026-08-17: initial environment inventory

- Candidate source image: `basic-claude-uv:latest` (`8f170f660813`, 2.16 GB at
  inspection time).
- Source Dockerfile:
  `agent-talk-extras/docker/basic-claude-uv/Dockerfile`.
- Base: `node:22-bookworm-slim`; work directory: `/work`; default command:
  `bash`.
- Installs `@anthropic-ai/claude-code` and `@openai/codex` globally with npm,
  plus Python 3, Git, curl, ripgrep, tmux, and `uv`/`uvx`.
- Compose support copies the host Claude credential file into the container at
  startup. Credentials are deliberately not part of this investigation.

The image is therefore the expected integration target: both CLIs share one
filesystem and one `/root` home while retaining separate session stores.

## 2026-08-17: official Codex behavior

The official Codex command reference documents stable `codex resume` and
`codex fork` commands. Resume selects sessions by UUID/name and uses saved
working-directory metadata. The public reference describes behavior but does
not define the on-disk rollout schema.

The current Codex documentation also provides an interactive `/import` flow for
Claude Code and Cursor. It imports up to 50 chats from the last 30 days and is
not available during an active task, remote session, or local app-server daemon
connection. This project remains useful for non-interactive, inspectable,
bidirectional conversion—especially Codex to Claude—which the documented flow
does not provide.

Sources:

- <https://learn.chatgpt.com/docs/developer-commands?surface=cli>
- <https://learn.chatgpt.com/docs/import>

## 2026-08-17: pinned runtime inventory

The cached image was pinned by its full local image ID rather than the mutable
`latest` tag:

```text
sha256:8f170f660813ac358f347fa8a3580139972f3ea7a9fb087834f1da44669d9392
linux/amd64, 2,155,735,311 bytes, created 2026-07-14
Claude Code 2.1.209
Codex CLI 0.144.4
Node 22.23.1
Python 3.11.2
uv 0.11.28
```

The image runs as root, has `/work` as its working directory, and contains no
baked credential environment variables. Its Dockerfile uses mutable base and
package references, so rebuilding the same source does not reproduce this
compatibility target. Full commands and hashes are in `docker-environment.md`.

## 2026-08-17: Claude Code transcript probes

Controlled, isolated configuration homes established that a project transcript
lives at:

```text
$CLAUDE_CONFIG_DIR/projects/<encoded-cwd>/<session-uuid>.jsonl
```

Every non-ASCII-alphanumeric CWD character becomes `-`, so encodings can
collide. Conversation records form a UUID/`parentUuid` graph. The most recent
valid `last-prompt.leafUuid` selects the active leaf; metadata records can sit in
the graph between visible messages. A minimal transcript containing timestamped
user/assistant records with `uuid`, `parentUuid`, and `sessionId` resumed by
explicit UUID and was appended without a sessions index.

Assistant content is block-based (`text`, `thinking`, `tool_use`, and others).
Tool results appear as user content blocks linked by `tool_use_id`. Images use a
base64 or URL source wrapper. Compaction uses a visible-in-transcript summary
record plus a `system/compact_boundary`; sidechains use nested subagent files and
are not part of the top-level linear continuation.

## 2026-08-17: Codex rollout and source inspection

The native rollout path is:

```text
$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<timestamp>-<uuid>.jsonl
```

Legacy rollouts use envelopes with `timestamp`, `type`, and `payload`.
`session_meta` is the canonical head. Model-visible history is stored in
`response_item`; `event_msg` is a durable UI projection. Current source also
supports paginated history with ordinals and lineage, but the v0.1 writer uses
the smaller, backwards-compatible legacy mode.

Inspection of OpenAI's current source at commit
`fe5889928c24f98b32ca1fd8c7a2ebe275da60bf` confirmed that the SQLite thread
table is a derived index. A JSONL missing from the database can be found by UUID
scan and repaired. This is why the bridge writes only the rollout and never
mutates `state_5.sqlite`.

The current official Claude importer lives in
`codex-rs/external-agent-migration`. It is interactive and one-way, drops
thinking, and flattens tool activity into tagged text. The bridge instead keeps
native tool call/result records and adds the reverse Codex-to-Claude path.

## 2026-08-17: credential-free native resume validation

Synthetic fixtures contain only generic prompts, deterministic IDs, `/work`
paths, and fake tool activity. No real session data or credentials were used.
The automated test imported both directions into fresh homes, disabled
networking, resumed by explicit UUID, and checked that the exact imported file
grew:

```text
Codex native resume: PASS (3004 -> 9827 bytes)
Claude native resume: PASS (3689 -> 15712 bytes)
```

The advanced fixtures exercise text, a portable image, native tool linkage,
structured image tool output, and compaction. The test also proves append-only
prefix preservation, verifies that Codex rebuilt its SQLite state, and checks
that Claude's appended graph reaches the imported leaf. Authentication
necessarily failed offline; the evidence under test is native discovery,
parsing, session-ID selection, and append behavior—not a model response.

## 2026-08-18: content-free corpus hardening

All corpus work reported only aggregate structure, sizes, and linkage counts;
no paths, IDs, prompts, responses, tool arguments, or tool output were emitted.

The Claude main-session probe covered 102 top-level transcripts. The original
physical-line parser found 17 cases where a `tool_result` child had been written
before its `tool_use` parent and one valid compacted transcript whose preserved
segment forms a metadata-declared back-edge. After switching to UUID ancestry
order and validating that back-edge shape, 102/102 sessions parsed and
converted. Across 10,731 selected tool results, zero were emitted before their
matching call and zero selected links were missing.

The final safety audit also found selected `isMeta: true` Claude records inside
active ancestry. These are required as UUID links but can contain internal
caveats and reminders that must not become ordinary target user prompts. After
making them opaque-only, all 102 sessions still converted; 223 selected meta
records produced zero message, tool, or context events. Tool counts, IDs, and
call-before-result ordering remained exact, including 152 source calls that
were already incomplete and intentionally stayed without results.

A deterministic 600-rollout Codex sample initially converted 574 sessions; the
other 26 contained 217 `compacted.replacement_history` checkpoints in that
snapshot. A bounded audit of 64 replacement arrays from 15 sessions found
3–584 message items and
exactly one final provider-encrypted compaction item per array. Codex uses the
array as replacement model history, but Claude cannot decode the encrypted
state. The implemented cross-provider policy instead retains the visible
pre-compaction response transcript and records a dedicated lossy-expansion
warning. With that policy, all 600/600 sampled rollouts parsed and converted;
a final refreshed sample contained 218 checkpoints across the same 26-session
incidence.
The matching `event_msg.context_compacted` UI markers were exactly paired with
those checkpoints in the sample and are deduplicated to avoid two warnings for
one semantic event.

The initial sample contained 4,291 unmatched `event_msg.agent_message` records
in 32 sessions (4,299 in the final refreshed sample). Hash/length comparisons
found no exact match after safe newline
normalization, response-block concatenation, adjacent-item concatenation,
complete wrapper removal, or containment checks. They are therefore retained
as explicitly marked UI-only assistant messages; fuzzy matching is not used.

Input-limit calibration found these maxima:

| Corpus | Files | Size p95 | Size max | Records p95 | Records max |
| --- | ---: | ---: | ---: | ---: | ---: |
| Claude main sessions | 102 | 19.15 MiB | 70.09 MiB | 5,075 | 28,434 |
| Codex deterministic sample | 600 | 6.09 MiB | 23.76 MiB | 111 | 534 |

The default 256 MiB file and 100,000-record caps therefore retain more than
3.5× headroom over observed maxima while bounding eager materialization.

## 2026-08-18: direct UUID transfer

Controlled source homes confirmed that native transcript filenames are enough
for safe UUID discovery. The `transfer` command searches Claude project
directories (optionally disambiguated by `--source-cwd`) or Codex active and
archived rollout stores, verifies transcript metadata against the requested
UUID, and then uses the normal validated import pipeline. It never reads or
mutates CLI indexes.

The pinned, credential-free Docker probe was rerun through this direct UUID
workflow. Both native-resume checks retained the same passing byte-growth and
append-linkage assertions shown above.

## 2026-08-18: thorough validation campaign

The post-baseline campaign expanded validation from structural sampling to an
exhaustive supported-corpus conversion/reparse, a 60-session content-level
manual audit, generated property cases, adversarial fixtures, and six pinned
native resumes. The sanitized methodology and aggregate evidence are recorded
in [the thorough validation report](validation-report.md).

The campaign found one loss-accounting defect rather than a history-mapping
failure: an orphan tool result was retained in the target transcript but was
not mentioned in the manifest. Both writers now report orphan and duplicate
call/result IDs while preserving the source record. A deliberately orphaned
Codex result still resumes natively and produces Codex's own expected orphan
diagnostic, making the manifest warning useful without silently deleting
history.

A focused audit of six Codex 0.147.0 paginated roots confirmed that their JSONL
ordinals and SQLite projections were complete, but also proved that the legacy
reader cannot safely accept them unchanged. Each contained contextual
environment state in a user-role response item that was not a completed user
message; bypassing the guard would turn it into a spurious Claude prompt.
Paginated input therefore remains fail-closed pending ordinal validation,
effective-history reconstruction, contextual-fragment filtering, and a native
0.147.0 cold-resume oracle.

## 2026-08-18: documentation/contract audit

An independent end-to-end documentation audit compared the README and every
guide with CLI help, implementation, tests, Docker scripts, and v0.1.1 release
evidence. It resulted in dedicated CLI, troubleshooting, and development
references; explicit secret-handling and metadata-privacy warnings; validation
commit provenance; Docker source-file hashes; and a persistent-container
example.

Treating documentation claims as contracts also exposed two implementation
gaps. Non-object entries inside Codex structured tool output were skipped
instead of producing an opaque loss counter, and a quoted `~` in `--output`
could be expanded for the write but reported as a different path. Both cases
now have regression coverage: nested values produce `tool_result:opaque`, and
all CLI path arguments expand `~` consistently before use/reporting.

## Remaining compatibility work

- Validate authenticated semantic recall with a disposable transfer nonce.
- Add native fixtures for remote-URL images, branching, and schema drift when
  sanitized examples can be generated safely.
- Re-run the pinned integration suite for every supported Claude/Codex version
  pair.
