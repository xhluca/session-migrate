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
scan and repaired. This is why the migrator writes only the rollout and never
mutates `state_5.sqlite`.

The current official Claude importer lives in
`codex-rs/external-agent-migration`. It is interactive and one-way, drops
thinking, and flattens tool activity into tagged text. The migrator instead keeps
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

## 2026-08-18: exhaustive multi-root session catalog

Filesystem inventory proved that neither native picker index is an exhaustive
session source. The default Claude home contained 102 main transcripts and 140
nested subagent JSONLs. The Codex filesystem contained 56,765 active/archive
rollouts at the final catalog snapshot, while its SQLite thread table contained
only 48,484 rows. The catalog therefore treats native JSONL enumeration as
authoritative and uses vendor indexes only to enrich native name/title and
spawn-lineage metadata.

A content-free initial refresh streamed 57,007 JSONLs totaling
65,647,590,591 bytes in 182.96 seconds. It classified 56,861 candidates and the
expected 146 unsupported inputs (140 Claude sidechains and six Codex
non-legacy histories), with zero corrupt files and zero root errors. Peak RSS
was 269,932 KiB. The final private SQLite database was 133,406,720 bytes after
its schema-v2 migration. A steady incremental refresh took 7.04 seconds while
reusing 57,001 unchanged files and rescanning six actively changing rollouts;
peak RSS was 251,900 KiB.

The first catalog prototype duplicated long native Codex title fields into a
normalized substring-search column and index, producing an unacceptable
996 MiB database. The final schema bounds every native label at 512 Unicode
code points and applies Unicode case-folding at query time, reducing the
database to about 127 MiB. The v1-to-v2 transaction preserves registered roots,
session rows, and labels while adding normalized timestamp filtering and
removing duplicated search values; both synthetic and full temporary catalogs
exercised the migration.

Aggregate integrity checks found zero labels above the bound, zero Claude
sidechains without a searchable native filename key, zero unexpected label
kinds, and zero candidate sessions without a native UUID. SQLite
`quick_check` passed. Two small real candidates, one per source format, were
copied to private temporary roots: 2/2 reached `validated`, and 2/2 exact
catalog-ID transfers completed authoritative dry runs without installing a
target.

The catalog scan also found that the current real Codex
`thread_name_updated` envelope stores the value under `thread_name`; the older
fixture/parser assumption used `name`. Catalog lookup now accepts both shapes
and has a current-field regression. All content-bearing temporary catalogs and
copied transcripts were removed after aggregate evidence was recorded.

## 2026-08-18: Pi, OpenCode, and Cursor target research

Pi 0.80.6 exposes a documented append-only v3 JSONL format and accepts an
explicit session file in offline RPC mode. A synthetic compaction/image/tool
chain loaded with its effective context intact; Pi appended ordinary native
state while preserving the converted file as an exact prefix. This provided a
real import/resume target without reverse-engineering a database.

OpenCode 1.17.20 exposes public JSON export/import commands. Its official
importer, list, export, and controlled loopback resume proved that a public
bundle can become native replay context. Direct SQLite writes were therefore
unnecessary and are prohibited. An empty-store list prints an empty stream,
and even a read-only-looking list initializes normal XDG cache, database,
gitignore, log, and lock files; the dry-run contract now states that boundary.

Two native ordering assumptions failed on real data before release. More than
4,095 same-millisecond generated IDs overflowed the counter, leading to a
strict logical-millisecond carry design within OpenCode's official 48-bit time
field. Separately, OpenCode's database/runtime pages messages by creation time
and ID, so decreasing source timestamps changed official export/replay order.
The writer now emits nondecreasing native times and counts each adjustment.
Large export verification also revealed exact 65,536-byte truncation when the
pinned CLI wrote to a captured pipe; the oracle uses a private regular file.

Cursor Agent CLI could list/resume local conversations, but official docs and
the installed 2026.03.20-44cb435 binary exposed no import API, export bundle, or
versioned transcript schema. Its workspace database held opaque proprietary
blobs. The migrator therefore recognizes `--to cursor` only to return a precise
unsupported-contract error.

The final aggregate run converted and reparsed all 102 top-level Claude main
sessions into both new targets with exact unified portable timelines and loss
counters. A private actual-content review covered 20 sessions/40 target cases
and 24,324 exact rows per target; real images were byte- and pixel-identical.
Ten real conversions per target passed isolated native structural smoke tests.
All content-bearing audit files and isolated native homes were removed. Full
methods and limitations are in the [validation report](validation-report.md).

## 2026-08-18: Copilot and Antigravity target research

GitHub Copilot CLI 1.0.70 publishes the session event schema used by its local
`session-state/<uuid>/events.jsonl`; its SQLite databases are derived subsets.
A generated event log and workspace sidecar cold-resumed by explicit UUID with
no pre-existing database, kept the complete generated prefix, rebuilt SQLite,
and replayed the portable history to a controlled provider. This supported a
conservative writer for messages, linked tool activity, compaction summaries,
and content-addressed images.

Media replay exposed an important distinction. Copilot forwarded an imported
user image through the pinned OpenAI-compatible path, but did not forward a
tool-result image even though the native event log retained the exact
content-addressed asset. The adapter preserves that asset and reports
`tool_result:image_provider_dependent` rather than presenting native retention
as guaranteed model context.

One existing OpenAI API key was passed only through Copilot's documented BYOK
environment to an isolated subprocess. The actual full-screen TUI resumed an
imported session and completed two authenticated turns. A separate loopback TUI
run completed two turns without credentials. No credential value was printed,
persisted, or copied; Codex desktop OAuth was explicitly not treated as a
general-purpose credential.

Antigravity CLI 1.1.14 was pinned from its official release and exercised in
its actual full-screen TUI for two deterministic turns through the supported
Gemini API-key/base-URL configuration. Its native per-conversation SQLite held
the expected step sequence. However, the conversation body is stored as
version-private protobuf trajectory blobs. Neither the CLI nor the public SDK
exposes an arbitrary-history seed/import. The target therefore fails closed
instead of synthesizing private state.

After final Copilot media hardening, all 102 top-level Claude sessions passed
conversion, byte validation, migrator reparse, exact unified semantic comparison,
and independently computed loss counters. A stratified 10-session native run
passed exact cold resume, prefix preservation, derived-index rebuild, and
provider replay. Detailed sanitized evidence and limitations are recorded in
[the target research](copilot-antigravity-targets.md) and
[validation report](validation-report.md).

## 2026-08-18: Codex and Pi full source matrix

Pi v3 was promoted from a write-only verifier to a first-class source. Its
append-only entries still form a tree, so the reader follows the final active
leaf through `parentId`, emits that ancestry in forward semantic order, and
accounts for abandoned branches and runtime entries. Detection, content-free
inspection, UUID/CWD discovery, direct transfer, default/environment/project
root registration, catalog search, and schema-v3 catalog migration were added
with same-format conversion still rejected.

The real-session validator was generalized to exactly one Claude, Codex, or Pi
root and every different safe target. It serializes, target-byte-validates,
reparses, independently projects the portable timeline, independently computes
all loss counters, and deletes each target before moving on. Defects were found
by clean corpus restarts rather than weakened assertions:

- Python `splitlines()` treated valid JSON string U+2028/U+2029 characters as
  record boundaries in Pi and Copilot validation; LF-only splitting fixed it.
- Retaining full projections for every Codex file made the audit itself grow to
  about 10 GiB; the checked-in harness now retains only bounded aggregate and
selected-report state.
- A Codex source record containing text/image/text exposed Copilot's native
  coalescing of text blocks around an attachment. The writer now reports
  `message:native_text_blocks_grouped`, and the independent oracle models that
  exact native grouping.
- A late, 119 MB Codex rollout contained two more results than matching tool
  calls. OpenCode correctly marked the exhausted native associations as both
  duplicate and orphaned, exposing a multiplicity bug in the independent
  oracle. After that oracle was fixed, Copilot's strict byte validator exposed
  a writer bug: the extra completions lacked synthetic preceding requests.
  Copilot now emits one auditable request/start with a fresh target-native ID
  for each excess completion and reports both conditions; OpenCode uses the
  same fresh-ID isolation policy for its synthetic tool part. Reusing the
  source call ID looked structurally valid but the Copilot runtime deduplicated
  it, so the exact 1.0.70 cold-resume oracle was essential. The exact failing
  rollout and the entire remaining 1,493-file tail then passed all four targets
  without weakening validation.

OpenCode required two other exact native policies. A result can correlate to an
earlier tool part even after an intervening assistant message, which is now
reported as `tool_result:native_order_associated`. Its IDs and message times
must both be monotonic under native paging; same-millisecond overflow and
decreasing source timestamps are carried forward without reordering history.
The Pi source audit also found that future/malformed nested result blocks needed
an opaque sentinel; they are now counted instead of disappearing.

The four accessible Pi sessions passed conversion, native-byte validation,
reparse, independent semantic comparison, and independent loss accounting to
Claude, Codex, OpenCode, and Copilot after the final parser change. Real native
checks then loaded Pi-derived Claude and Codex files in the pinned Docker image,
loaded Pi/OpenCode targets through their public runtimes, and cold-resumed
Copilot through a loopback provider. Every check preserved the exact generated
prefix; Codex rebuilt SQLite and Claude's appended user record linked to the
imported leaf.

A clean restart covered all 56,766 Codex files. All 56,760 supported legacy
rollouts converted to Claude, Pi, OpenCode, and Copilot with exact target-byte,
reparse, portable-semantic, and independent loss-counter checks; the six
paginated roots failed closed. The final fresh-ID change touched only the two
excess results in that corpus. The former 119 MB failure passed the current-code
four-target matrix and the exact Copilot CLI cold-resumed, appended, rebuilt
SQLite, and completed a provider turn. Copilot pruned that exceptionally large
history to runtime context, so it was not mislabeled as exact provider replay.
A compact one-call/three-result fixture then proved the changed path itself:
all three distinct call/result pairs reached the loopback provider exactly.

An explicitly invoked PTY harness translated the existing Codex OAuth record
into Pi's documented `openai-codex` shape only inside a private temporary home.
The actual Pi 0.80.6 TUI completed two live synthetic turns, the second recalled
the first, and the imported prefix remained intact. No token or response value
was printed, and the credentials/transcript/log workspace was removed. Normal
migrator commands never read credentials.

The corresponding actual OpenCode TUI loaded an imported sanitized Pi history
and submitted a follow-up with the translated disposable credential, but its
persisted assistant turn remained unfinished and text-free. With no verifiable
reply or recall, this was recorded as a failed live-service gate. Official
OpenCode import/list/export and loopback-provider resume remained exact, so the
supported import contract was not weakened or overstated.

A separate private browser-rendered review covered all four accessible Pi
sources plus three stratified Codex sources across every different safe target:
seven source cases, 28 target cases, and 592 sampled actual-value rows. The
rendered role/order/message/tool pairs matched side by side. One real inline
image source/target pair was value-identical and visually identical. The
mode-`0600` report/screenshots were deleted and both private browser tabs were
closed; no content or identifier is reproduced here.

## 2026-08-19: Pi thinking-trace mechanism

Pi 0.80.6 separates future-turn effort (`thinking_level_change`) from persisted
assistant reasoning state. A native assistant `thinking` block contains an
optional visible summary plus an opaque `thinkingSignature`. Session context
reconstruction keeps the entire block on the active branch. The provider layer
preserves it only when provider, API, and model match: OpenAI Responses stores
the complete reasoning output item as signature JSON, Anthropic retains native
thinking/redacted-thinking signatures, and Google retains thought signatures.

Synthetic transformation probes confirmed same-model signature replay,
cross-model conversion of visible thinking to ordinary text, cross-model
removal of redacted thinking, and exact rehydration of a Responses `reasoning`
item. The four-file real Pi store contained two signed reasoning blocks; one
had no visible text. Relocated private copies loaded 4/4 through the actual
offline RPC runtime, which retained both signatures and all normalized input
prefixes. This evidence explains Pi's continuity without exposing any trace
content.

Pi's cross-model text fallback and its inclusion of visible thinking in the
compaction summarizer also confirm the migrator policy: private reasoning remains
counted but untransferred. The detailed source hashes, provider behaviors,
probe results, and requirements for any future exact same-provider opt-in are
in [Pi thinking-trace handling](pi-thinking-traces.md).

## 2026-08-20: seven readable/writable formats

The earlier OpenCode/Copilot target-only and Antigravity/Cursor fail-closed
decisions were revisited with exact installed builds, clean-room storage
analysis, and native runtime oracles.

OpenCode 1.17.20 official exports became a bounded source adapter. Copilot
1.0.70's shipped schema-v1 event declaration and native event trajectories
became a source adapter. Both now support same-format portable rewrites. The
catalog inventories OpenCode's read-only metadata table and Copilot session
directories without indexing conversation bodies.

Antigravity self-updated from the previously inspected 1.1.2/1.1.14 line to
1.1.16 during isolated research. Its per-conversation SQLite schema and bounded
protobuf fields were independently recovered. A from-scratch synthetic DB
loaded in the actual CLI and TUI, rendered imported user/planner/thinking/tool
state, accepted a typed follow-up, and appended native rows. The released
adapter omits private thinking and implements only the portable, runtime-proven
message/generic-tool subset. The synthetic schema notes and generator are
published in the
[Antigravity clean-room repository](https://github.com/xhluca/antigravity-session-interoperability).

Cursor Agent `2026.03.20-44cb435` stores CLI resume state in a workspace-keyed
SQLite content-addressed protobuf graph. A pure Python store was decoded through
the shipped loader, rendered by the actual TUI, selected by actual resume, and
served through Cursor's backend `GetBlob` exchange. That proves imported text is
native/model-resolvable rather than cosmetic UI state. Tools/thinking/media and
a real authenticated assistant checkpoint followed by a second resume remain
unproven, so the implementation is deliberately text-only and experimental.
The clean-room graph notes/generator are published in the
[Cursor research repository](https://github.com/xhluca/cursor-session-interoperability).

All seven formats now share the same source/target enums, 49-route unit matrix,
catalog selection, loss-counter contract, and same-format rewrite warning.
Actual credentials were never made a migration feature. Pi and OpenCode TUI
checks used isolated schema translations of existing Codex OAuth only after
proving those clients accepted the same provider shape; temporary homes and
credential copies were deleted.

## 2026-08-21: Mistral Vibe 2.24.3

Mistral Vibe was evaluated from its official Apache-2.0 repository at tag
`v2.24.3` (`a84be0391bf93e93a4025a5e08e8032ecb587123`) and an isolated exact PyPI
installation. Its native session is not one transcript file: each UUID has a
directory containing `meta.json` and `messages.jsonl` below
`$VIBE_HOME/logs/session`. The public `LLMMessage` model exposes ordered text,
readable reasoning, provider-bound reasoning payloads, linked function calls
and results, images, injected messages, and compaction boundaries.

The first native probe loaded the generated history and appended a turn but
rewrote the source prefix. Source inspection showed why: Vibe decides between
append and rewrite by hashing a Pydantic `model_dump` with materialized false
defaults and Python JSON's default escaping/separators. The writer was corrected
to mirror that boundary exactly. A fresh credential-free native probe then
preserved the generated JSONL byte prefix, sent the migrated post-compaction
history and follow-up to a loopback provider, persisted the synthetic reply,
and reparsed both appended messages.

Vibe is therefore the eighth readable/writable format. It participates in all
64 ordered source/target routes, native ID discovery, two-file inspection,
catalog title search, custom/default/project roots, same-format rewrite, and
content-free loss accounting. The exact mapping and native gate are documented
in [Mistral Vibe session format](vibe-format.md).

## Remaining compatibility work

- Repeat authenticated semantic recall when a supported target/provider version changes.
- Prove a real authenticated Cursor assistant checkpoint plus a second resume
  before considering removal of the experimental label.
- Add native fixtures for remote-URL images, branching, and schema drift when
  sanitized examples can be generated safely.
- Implement Codex paginated/history-base lineage only after ordinal, contextual
  user, compaction, rollback, and inter-agent semantics are independently gated.
- Re-run the pinned integration suite for every supported agent version/schema
  combination.

## 2026-08-25: Muse, Qwen Code, and Kimi Code

Three additional local harnesses were inspected at exact installed versions:
Muse Code `0.2.1 (0.2.1-R1215.1)`, Qwen Code `0.22.1`, and Kimi Code `0.38.0`.
Qwen was matched to official commit
`2755dbe1399f94e53e24377d2e21fa86ce923529`; Kimi was matched to official
commit `0999454`. Muse's installed metadata declares build SHA `b3170a534f`.

Qwen's store is a project-scoped append-only UUID graph. Kimi uses a native
state document plus a protocol-`1.5` main-agent wire journal. Muse's event
stream initially appeared loadable when only message/tool commits were written,
but a live continuation could not recall the imported tool history. Structural
comparison with a native run identified the missing contract: each historical
user turn also needs accepted-intent, run-started, and materialized-intent
records with exact envelope/run linkage and nonempty refill content. After the
writer reproduced that lifecycle, the same exact Muse CLI recalled the imported
file marker.

All three live gates used one explicitly supplied OpenRouter key only inside
disposable private homes. Qwen ran `qwen/qwen3-coder-next`; Kimi ran
`moonshotai/kimi-k2.7-code`; Muse used `meta/muse-glimmer-30b` through
`muse-code-openrouter 0.3.2`. Each target selected the imported ID, preserved
the complete generated byte prefix, appended native records, reparsed through
its source adapter, and named `README.md`, which existed only in the sanitized
imported tool history. The default test suite exposes none of those credentials
or network calls: the exact-binary provider tests skip unless a mode-`0600` key
file and explicit binary paths are supplied.

The portable route matrix now covers all 121 ordered pairs among eleven
formats. Muse, Qwen, and Kimi participate in native discovery, inspection,
catalog refresh/title search, direct/catalog transfer, private installation,
same-format rewrite, strict malformed-input rejection, and exact loss
accounting. Detailed field/path/test contracts are in
[Muse, Qwen Code, and Kimi Code formats](muse-qwen-kimi-formats.md).

## 2026-08-25: Oh My Pi 18.0.5

GitHub issue #1 requested Oh My Pi as a first-class CLI option. The official
`can1357/oh-my-pi` `v18.0.5` tag and exact Linux x64 release binary were
inspected before implementation. OMP still uses a v3 parent-linked journal,
but current files are not Pi aliases: they live below `~/.omp/agent`, use OMP
CWD buckets, and reserve an exactly 256-byte mutable title record before the
session header. OMP also adds reset boundaries, credential/mode/service-tier
state, profiles, and content-addressed image blobs.

The adapter therefore received its own source/target enum and module instead of
redirecting `omp` to `pi`. Current fixed-slot files auto-detect. Legacy
slotless v3 files require explicit OMP selection because their native head is
ambiguous with Pi. The catalog content-sniffs a shared `PI_CODING_AGENT_DIR`
and registers the custom root once under the detected active agent family.

The first exact-binary loopback trajectory passed without credentials: OMP
18.0.5 loaded a generated Claude-derived session, returned its imported active
messages through RPC, supplied the imported tool/compaction history plus a new
prompt to the model request, appended the provider reply to the same native
journal, and rewrote only its fixed title slot during a native rename. The
complete mapping and binary identity are recorded in
[Oh My Pi session format](omp-format.md).
