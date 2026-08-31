# Thorough validation report

Date: 2026-08-18; updated 2026-08-26 for Grok, Kilo Code, and OpenHands support

This report records the validation campaign requested after the v0.1 baseline.
It deliberately separates native acceptance, portable semantic equivalence,
expected loss, and manual review. A session is not described as lossless merely
because the target CLI accepts the generated file.

No real prompt, response, path, session ID, tool argument, tool output, or
credential is included here. Aggregate audits operated locally. Temporary
reports containing content were mode `0600`, were never committed or printed,
and were removed after review.

The migrator itself is not a secret scanner. This campaign checked mapping and
loss accounting, not whether real conversations contained sensitive strings;
portable embedded secrets would be copied to a target transcript.

## Evidence artifacts and reproducibility

The campaign used several deliberately distinct code and evidence points:

| Evidence | Migrator revision | Reproducibility |
| --- | --- | --- |
| Full 102-Claude and 56,750-Codex semantic conversion/reparse | `63a360e` | One-off bounded local harness over a private corpus; aggregate results only |
| Focused full-corpus duplicate/orphan counter audit | `3304abf` | One-off bounded local harness over the refreshed private corpus |
| Duplicate-call/result regressions | `e47a3ed` | Checked-in pytest |
| v0.1.1 release gates and official two-way native probe | tag `v0.1.1`, commit `4d8f3e5` | Checked-in tests and `scripts/verify-native-resume.sh` |

The post-fix linkage audit parsed and converted every supported file and
independently checked its expected warning counters. It did not repeat the
target reparse already completed by the main semantic pass because the fix
changed diagnostic accounting, not emitted portable history.

The full-corpus, 23-case adversarial, 200-case generated-property, six-case
expanded-native, and manual-review harnesses were private temporary programs,
not committed test utilities. The generated sweep was deterministic during the
run, but its seed/program and the real-session selection list were deleted with
the content-bearing audit workspace. Their aggregate evidence is documented
for auditability but cannot be replayed from a fresh clone. The reproducible
release gates are:

```console
uv run ruff check .
uv run pytest
bash -n scripts/verify-native-resume.sh
scripts/verify-native-resume.sh
uv build
uv tool run --isolated \
  --from dist/session_migrate-0.1.1-py3-none-any.whl \
  session-migrate --version
```

On v0.1.1 those gates produced 55 passing tests, a clean Ruff and shell-syntax
check, successful sdist/wheel builds, an isolated `session-migrate 0.1.1`
installation, and passing official native resumes in both directions. The
stratified manual selection method and coverage are recorded below; private
paths and the exact selection list were deleted.

### v0.1.2 documentation-contract follow-up

The v0.1.2 hardening is identified by CLI/path commit `b0691b3`, structured
tool-result accounting commit `f2b6ab3`, documentation commit `0efc568`, and
the final annotated tag `v0.1.2`. Paired synthetic regressions cover both
source directions.

A content-free follow-up scanned all 56,758 Codex rollouts visible in the later
snapshot, within the normal input bounds and with pre/post identity checks:

- 11,651 files contained 122,162 structured output arrays with 130,601
  elements in the primary pass.
- Zero arrays contained a non-object element.
- A second complete pass found zero malformed known image blocks without a URL
  and zero tool-reference blocks without a tool name.
- The live corpus added 44 arrays between passes; both independent complete
  passes had zero incidence and no read/stability errors.

The real-corpus differential was therefore vacuous: no existing supported file
used either newly accounted shape. Synthetic current-worktree probes produced
exactly two `tool_result:opaque` counters for two non-object/unknown values and
exactly two for a malformed image/reference pair.

The reproducible v0.1.2 release gate passed 58 pytest tests, Ruff,
`git diff --check`, shell syntax, the official credential-free native-resume
script in both directions, sdist and wheel builds, an isolated wheel install
reporting `session-migrate 0.1.2`, and internal-link checking. The official
native results remained:

```text
Codex native resume: PASS (3004 -> 9827 bytes)
Claude native resume: PASS (3689 -> 15712 bytes)
```

## Acceptance definitions

- **Native acceptance** is target-specific: a pinned filesystem target must
  load the requested imported ID and preserve the generated prefix; a target
  with a public importer must import the requested ID, export/reparse the
  portable history, and pass a controlled resume-replay oracle.
- **Portable semantic equivalence** means an independent projection of the
  source's supported messages, tools, media, and compaction events exactly
  equals a fresh parse of the generated target file. This comparison includes
  full values, order, roles, IDs, and call/result linkage; it is not a count-only
  comparison.
- **Expected loss** means a source-only feature was omitted or transformed in
  accordance with the compatibility matrix and was counted in the conversion
  manifest.
- **Unexplained discrepancy** means any difference not predicted by that
  mapping or not accounted for by the manifest. This is a release blocker.

## Corpus inventory

The campaign inventoried every accessible local top-level transcript at the
start of the run:

| Store | Files in scope | Treatment |
| --- | ---: | --- |
| Claude main sessions | 102 | Exhaustive conversion and semantic reparse |
| Claude nested subagent sessions | 140 | Inventoried; excluded because the v0.1.x line intentionally does not import sidechains |
| Codex active and archived rollouts | 56,750 | Exhaustive classification; every supported legacy rollout converted and semantically reparsed |

The corpus changed naturally as local CLIs ran, so these counts are a dated
snapshot rather than a permanent statement about either home directory.

## Exhaustive programmatic audit

### Claude to Codex

All 102 main Claude transcripts were detected, parsed, converted, reparsed as
Codex, and compared against an independently projected portable semantic
signature:

- 102/102 completed; zero parse, conversion, or target-reparse failures.
- 102/102 matched the full portable semantic signature.
- Zero unexplained differences and zero manifest-counter mismatches.
- 951,819,217 source bytes were processed.
- 83,899 raw records produced 80,434 source events: 9,124 messages,
  10,896 calls, 10,744 results, 8,877 thinking events, 23 compactions, and
  40,770 opaque/native-metadata events.
- The generated Codex files reparsed to 30,787 portable target events.
- Source and target contained 10,896 tool calls and 10,744 tool results.
- All 10,744 results linked to a preceding call; there were zero inversions and
  zero unmatched selected results.
- Six sessions exercised compaction structure, 98 used `last-prompt` leaf
  selection, and 14 contained selected `isMeta` ancestry.

Expected, counted source-only features included 8,877 thinking blocks, 422
tool-error flags, 97 tool-reference result blocks, 23 detailed compaction
boundaries, and additional inactive-branch, title, and native metadata records.
Those values were not treated as portable equivalence.

### Codex to Claude

All 56,750 Codex rollouts were detected. The 56,744 supported legacy rollouts
were parsed, converted, reparsed as Claude, and compared against an independent
portable semantic signature:

- 56,744/56,744 supported rollouts completed and matched exactly.
- Zero parse, conversion, or target-reparse failures in the supported set.
- Zero unexplained semantic differences and zero manifest-counter mismatches.
- 64,355,421,720 source bytes and 1,847,707 raw records were processed.
- The parser produced 1,624,189 source events: 360,106 messages, 289,343
  calls, 289,340 results, 177,375 reasoning events, 364 compactions, 69,634
  context events, and 438,027 opaque/native events.
- The generated Claude files reparsed to 835,586 portable target events:
  256,536 messages, 289,343 calls, 289,340 results, and 367 user images.
- Source and target both contained 289,343 tool calls and 289,340 tool results.
- 289,338 results linked to a preceding call; two additional results exceeded
  source call multiplicity. A focused rerun on the post-fix tree classified
  both as duplicate results in one rollout and emitted exactly
  `tool_result:duplicate_id: 2`.
- Exactly six rollouts failed closed at the unsupported paginated-history
  guard; none was silently treated as legacy.

Unsupported paginated/history-lineage rollouts are classified separately and
are not counted as successful legacy conversions.

One new active rollout appeared while the campaign was running. A focused
post-fix snapshot therefore contained 56,751 files: 56,745 supported and the
same six unsupported paginated roots. All 56,745 supported files parsed and
converted; expected per-file linkage counters exactly equaled artifact
counters, with zero orphan-result, duplicate-call, or counter mismatches. The
main full semantic reparse remains the fixed 56,750-file start snapshot above.

Expected, counted source-only features included 103,570 privileged messages,
177,375 reasoning events, 69,267 turn-context records, 438,027 opaque/UI/runtime
records, and 364 encrypted replacement-history checkpoints. The expanded
visible history was retained for those checkpoints. The migrator also retained
and marked 5,037 unmatched UI projections, wrapped 8,218 non-object tool
inputs, omitted 2,026 namespaces, and synthesized four missing tool names.

Schema coverage included 1,499 explicit legacy rollouts, 55,245 rollouts whose
missing history marker defaults to legacy, 67 files with replacement history,
and 88 files with custom tools. Supported Codex producers spanned 17 observed
version strings from 0.38.0 through 0.147.0; the six additional 0.147.0 roots
were the unsupported paginated set. Claude producers spanned 14 observed
versions from 2.1.119 through 2.1.233.

A separate content-free feasibility audit examined all six paginated roots
encountered in the 1,674-rollout stratification frame. All six were archived
Codex 0.147.0 files with no `history_base`, contiguous ordinals, and projection
state consistent with the JSONL byte end. They contained no compaction,
rollback, inter-agent, or subagent-history records. Even in this restricted
shape, simply bypassing the current guard would be wrong: every file contained
one contextual environment-state `response_item` that Codex does not classify
as a completed user message, but the legacy adapter would replay it to Claude
as an ordinary user prompt. The release therefore keeps the explicit rejection.

## Content-level manual review

A stratified sample of 60 real sessions was reviewed side by side using actual
local content: 30 Claude-to-Codex conversions and 30 Codex-to-Claude
conversions. Selection covered small, medium, and large files; multiple CLI
versions; active and archived rollouts; tool-heavy, compacted, branched,
metadata-bearing, interrupted, and simple conversations.

The reviewer inspected beginnings, middles, ends, and every represented event
category rather than only spot-checking the first prompt:

- 81,216 native source records and 77,731 parsed source events were compared
  with 29,136 reparsed target events.
- 968 individual source/target event rows were visually reviewed.
- 60/60 portable histories were coherent in role, order, message boundaries,
  and tool linkage.
- 60/60 matched an independent full-value mapping exactly.
- 60/60 had exact manifest accounting for every omission or transformation.
- 10,132 linked tool call/result pairs were preserved with zero inversions.
- Supported media values were exact across 193 Claude image-result blocks, 61
  Codex image-result blocks, and two Codex user-image blocks.
- One real image pair in each direction was extracted and rendered. Both pairs
  were visually identical and byte-identical after wrapper normalization.
- Twenty-three Claude summary events were preserved exactly. Nine Codex
  replacement-history markers were omitted with the dedicated warning while
  visible expanded history remained ordered.
- Zero unexplained discrepancies and zero likely defects were found.

All 60 real sessions contained at least one source-only feature, such as native
metadata or private reasoning. They therefore classify as expected-loss with
an exact portable subset, not as fully lossless sessions.

The sample spanned Claude Code versions 2.1.119 through 2.1.233 and Codex legacy
rollouts from 0.38.0 through 0.147.0. It did not contain audio or a pure
UI-only rollout. Mixed UI/canonical histories and unmatched UI-only projections
were covered. Claude user-image coverage used real tool-result images because
no active-branch standalone user image occurred in the full 102-session Claude
main corpus.

## Adversarial and property testing

Synthetic, content-free testing complemented the real corpus with structures
that were absent or rare locally:

- 23/23 composite adversarial cases passed.
- 6/6 two-hop semantic round trips passed.
- 200/200 deterministic generated sessions passed: 100 shuffled Claude
  graph/branch/tool cases and 100 Codex canonical/UI/tool/compaction cases.
- Malformed input, oversized input, changing-source detection, collision
  handling, branch cycles, privileged roles, unsupported media, compaction,
  duplicate IDs, and incomplete tool linkage were exercised.

This track exposed one reporting defect: a retained orphan tool result was not
listed in the manifest even though Codex diagnosed it on resume. The writers
now report orphan and duplicate tool IDs while retaining the source record, and
regressions cover both conversion directions. Metadata-only conversion also
now returns the precise `conversion produced no resumable conversation
history` error.

## Pinned native-resume matrix

Six explicit-UUID imports were resumed inside the inspected Docker image by its
immutable image ID, with networking disabled and isolated homes containing no
credentials. Three cases targeted each CLI:

| Target | Case | Imported bytes | Bytes after resume |
| --- | --- | ---: | ---: |
| Codex | Interrupted conversation | 629 | 7,516 |
| Codex | Tool linkage | 1,602 | 8,489 |
| Codex | Image content | 1,145 | 8,032 |
| Claude | Interrupted conversation | 314 | 12,866 |
| Claude | Tool linkage | 1,966 | 15,530 |
| Claude | Compaction | 1,319 | 14,400 |

All 6/6 selected the requested UUID, appended to the correct file, and retained
the exact imported prefix. Claude's new prompt linked to the imported graph
leaf. Codex created or repaired its SQLite index. Native logs and generated
JSONL tails were manually inspected. A focused post-fix orphan-result resume
also passed; Codex retained the history and emitted its expected orphan-output
diagnostic.

These offline checks prove local discovery, native parsing, selection, and
append behavior. They do not claim that an unauthenticated model produced a
network response.

## Exhaustive catalog validation

The multi-root catalog received a separate content-free full-store validation:

- 57,007/57,007 native JSONLs were inventoried across the two automatic roots,
  totaling 65,647,590,591 bytes at the initial snapshot.
- All 102 Claude main transcripts, 140 nested Claude sidechains, 56,741 active
  Codex rollouts, and 24 archived Codex rollouts were present.
- Statuses were 56,861 structural candidates and 146 expected unsupported
  files: the 140 sidechains plus six non-legacy Codex histories. There were zero
  corrupt files and zero root errors.
- The initial scan took 182.96 seconds with 269,932 KiB peak RSS. The private
  schema-v2 database was 133,406,720 bytes after migration.
- A steady refresh reused 57,001 unchanged files, rescanned six live rollouts,
  and finished in 7.04 seconds with 251,900 KiB peak RSS.
- SQLite `quick_check` passed. Zero labels exceeded the 512-code-point bound,
  zero nested sidechains lacked a searchable native key, zero unexpected label
  kinds were stored, and zero candidate sessions lacked a native UUID.
- Two private real-session copies, one per format, passed explicit deep catalog
  validation and exact `transfer --catalog-id` dry runs (2/2 each).
- Synthetic regressions cover persistent/automatic/explicit/bounded-discovered
  roots; duplicates and missing rows; busy, corrupt, oversized, unsupported,
  and unavailable inputs; archived and nested sessions; Unicode title search;
  path privacy; incremental refresh; schema migration; native SQLite
  enrichment; and the prohibition on indexing prompt/preview/message bodies.

The native Codex SQLite index was intentionally not counted as inventory: it
contained fewer thread rows than filesystem rollouts. Only its `name`, `title`,
and spawn-edge fields were read, through a read-only connection. No real title,
path, UUID, prompt, response, or tool value was emitted in the report. Private
temporary catalogs and copied transcripts were removed after the aggregate
checks.

## v0.2.0 additional-target gate

The Pi/OpenCode work is separated from the historical v0.1.1/v0.1.2 evidence
above. The relevant checkpoints are leaf adapters `3878c11`, native synthetic
oracles `61b8285`, CLI/install integration `b578c61`, monotonic OpenCode IDs
`0b2663f`, strict portable-image validation `fac0a7e`, native replay ordering
`16dfbe1`, and the reusable content-safe corpus validator `046b547`.

### Exhaustive real-session conversion

The validator read all 102 accessible top-level Claude main sessions and, for
each of Pi and OpenCode, converted, byte-validated, reparsed, and compared one
unified portable timeline. The timeline interleaves messages, compaction,
images, tool calls, and tool results, with symbolic call-ID binding; it cannot
hide a cross-category reorder by comparing category totals independently.

- Pi: 102/102 conversions, byte validations, reparses, exact semantic
  projections, and independent exact loss-counter checks.
- OpenCode: the same 102/102 in all five stages.
- Zero final failures or unexplained differences.
- Source feature incidence was 95 sessions with tools, six with compaction, 81
  with supported images, 12 interrupted, all 102 with branch/native metadata,
  32 at least 10 MiB, and another 55 between 1 and 10 MiB.

Both targets independently counted 24 compact-boundary metadata omissions,
five generic opaque events, 5,942 active graph-metadata records, 2,414 inactive
or metadata conversation records, 21,851 non-conversation records, 10,834
top-level tool-result metadata fields, 8,941 thinking events, and 97 tool
references. OpenCode additionally counted 18 native-order timestamp
adjustments. Generated and independently predicted counters agreed per session.

Two mismatches were found during this campaign and reported before repair. One
real OpenCode conversion exceeded 4,095 same-millisecond generated IDs and
wrapped out of ascending order; the logical-millisecond carry fix and a
greater-than-4,096-ID regression followed. Another real imported session had
decreasing source timestamps; OpenCode's official message page reordered it by
creation time during export/replay. The writer now emits nondecreasing native
times with explicit loss accounting, and a decreasing-time import/resume
regression proves replay order. The complete 102-by-two corpus was restarted
from the beginning after each final hardening change.

### Actual-content side-by-side review

A private, mode-`0600` report displayed actual source values beside each
reparsed target value for a stratified 20-session sample, yielding 40 target
cases. The sample covered tools in 17 sessions, compaction in five, supported
images in 12, branch/metadata in all 20, interrupted histories in nine, six
files at least 10 MiB, eight from 1 to 10 MiB, and six below 1 MiB.

For each target, 24,324 actual rows were inspected: 7,738 conversation rows
(7,716 messages plus 22 compactions), 8,350 tool calls, 8,236 tool results, and
171 supported tool-result images. All 24,324/24,324 rows and all 20/20 loss
checks were exact for both Pi and OpenCode. There were zero mismatches.

One supported real image was extracted for each target. Source and reparsed
payloads were byte-identical and independently decodable, with identical
geometry and color space; rendered pixel-difference count was zero for both.
The private workspace was mode `0700` and all eight files were mode `0600`.
After review, every file was overwritten, zeroed, and removed; the directory
was absent, with zero open handles and zero residual audit processes.
A separate content-safe manual report and the isolated OpenCode dry-run probe
tree were likewise securely removed after their aggregate observations were
recorded.

### Native target checks

Synthetic authentication-free oracles cover Pi compaction, user/result images,
tools, RPC context, and exact prefix preservation, plus OpenCode official
import/export and an HTTP-loopback resume that inspects the actual history sent
to the model endpoint. These prove semantic replay without provider
credentials.

A stratified real-session subset added 10 Pi 0.80.6 offline RPC loads with 10
exact on-disk prefixes and 10 OpenCode 1.17.20 official imports/exports with 10
exact semantic projections. It covered all 10 with branch/native metadata, one
compaction case, one supported-image case, five interrupted cases, four tool
cases, one file at least 10 MiB, and two additional files at least 1 MiB. Both
process environments used isolated HOME/XDG/temporary/target roots and
inherited only PATH, terminal/color, and locale values; provider/API credential
variables were not passed. The private workspace was removed.

OpenCode exports in this oracle use private regular-file stdout redirection.
The pinned CLI truncated a captured pipe at exactly 65,536 bytes for a large
session even though a regular-file export was complete; this was treated as an
oracle transport limitation, not as a session mismatch.

The real-session subset proves offline/native structural load or official
import/export, not authenticated live-model recall. Only the controlled
synthetic OpenCode loopback inspects a resumed request, and no check claims a
provider generated a response for real private content.

The final v0.2.0 release tree passed Ruff and all 127 pytest tests (including
the available exact-version native tests), `git diff --check`, sdist/wheel
builds, isolated installation of the built wheel reporting
`session-migrate 0.2.0`, and live help checks for the additional targets and
Cursor's unsupported label. The pinned, credential-free, network-disabled
Docker regression also passed both original directions on the final tree:
Codex native resume appended `3004 -> 9827` bytes and Claude native resume
appended `3689 -> 15712` bytes while preserving each imported prefix.

## Copilot/Antigravity target gate

This later campaign started from the queued-target checkpoint `017bfdf`, the
Copilot implementation `21e39b8`, and native replay hardening `5fb7f7b`. It
keeps the historical v0.2.0 evidence above unchanged.

### Copilot exhaustive conversion

After the final content-addressed media/provider-warning change, the reusable
validator restarted from the first source and processed all 102 accessible
top-level Claude main sessions for Pi, OpenCode, and Copilot.

Copilot passed 102/102 serialization, native-byte validation, adapter reparse,
unified semantic projection, and independent loss-counter comparison. There
were zero failures or unexplained differences. Source coverage included 95
tool sessions, six compaction sessions, 81 image sessions, 12 interrupted
sessions, 32 files of at least 10 MiB, 55 more of at least 1 MiB, and
branch/native metadata in every session.

The final aggregate Copilot warnings were:

- 24 compact-boundary metadata transformations;
- five generic opaque events;
- 5,942 active graph-metadata, 2,414 inactive/metadata conversation, 21,851
  non-conversation, and 10,834 top-level tool-result metadata records;
- 8,941 thinking events;
- 21 native timestamp-order adjustments;
- 97 tool-reference blocks; and
- 1,061 tool-result images retained as exact native assets but marked
  provider-dependent for model replay.

Every per-session counter matched the independently predicted value. A
content-safe 20-session structural report compared 70,911 rows across the
three targets (60 target cases) with zero semantic differences. No body values,
paths, IDs, or media bytes were retained in that report.

### Copilot native replay and actual TUI

`scripts/validate-copilot-native.py` selected 10 feature-stratified real
sessions from all 102 and imported each into an isolated `COPILOT_HOME`. Exact
Copilot CLI 1.0.70 resumed the requested UUID against a deterministic loopback
OpenAI-compatible provider. Results were 10/10 native cold resumes, 10/10 exact
generated-prefix checks, 10/10 derived-SQLite rebuilds, and 10/10 portable
provider-request value checks. The set contained four tool cases, one
compaction, one image case, five interrupted cases, and three large files.
Only PATH, terminal/color, and present locale variables entered the subprocess;
no provider/API credential variable was inherited.

A focused synthetic media request proved that Copilot replayed one imported
user image as an `image_url` block. The same runtime did not place an imported
tool-result image into OpenAI Chat Completions model context, although the
native event log and migrator reparse kept the exact bytes. That observed
boundary is why the retained image receives an explicit warning.

Two actual full-screen TUI campaigns were also performed in isolated homes.
The deterministic loopback TUI completed two turns. A second TUI used an
already available OpenAI API key exclusively through Copilot's documented BYOK
subprocess environment and completed two real model turns with exact synthetic
nonce responses. The secret was never printed, saved in the fixture/home, or
committed. Codex desktop OAuth was not copied or reinterpreted.

### Antigravity runtime and fail-closed decision

Official Antigravity CLI 1.1.14 was pinned and its actual full-screen TUI
completed two deterministic turns in an isolated workspace using the supported
Gemini base-URL/API-key configuration against a loopback provider. The native
conversation exited with a native resume ID, and its private per-conversation
database held the expected user, agent, and checkpoint steps.

The runtime proof did not establish an import contract. Official CLI/SDK
inspection showed that arbitrary prior turns cannot be seeded through a public
operation; the native body is version-private protobuf data inside SQLite.
`--to antigravity` therefore fails before serialization and never writes that
store. No real Google/Gemini credential was available or duplicated.

The reusable scripts, exact schemas, credential boundary, and cleanup policy
are documented in [the Copilot/Antigravity research report](copilot-antigravity-targets.md).

### v0.3.0 release gates

Release candidate `f8e700a` passed Ruff formatting and lint, all 138 pytest
tests, shell syntax, `git diff --check`, every internal Markdown link, sdist and
wheel builds, and isolated installation of the wheel reporting
`session-migrate 0.3.0`. Live help listed Copilot as supported and
Antigravity/Cursor as explicitly unsupported. A secret-pattern scan found no
credential value in the repository.

The final exact-version Copilot oracle produced the same 10/10 results reported
above. The original pinned Docker regression also passed on the v0.3.0 tree:
Codex resumed and appended `3004 -> 9827` bytes; Claude resumed and appended
`3689 -> 15712` bytes; both preserved their generated prefixes.

## v0.4.0 Codex/Pi source-matrix campaign

This campaign extends, rather than rewrites, the historical v0.1–v0.3 evidence
above. Pi source support began at `3cefa28`, catalog integration at `4e8a710`,
the generalized real-session matrix at `e7c22e5`, bounded-memory streaming at
`ccff738`, and real-source Claude/Codex oracles at `a3af046`. The clean Codex
matrix was started at `30ef82e`, after the Copilot native-grouping fix. Final Pi
source hardening is represented by `3d67c74` and `c6ee9b8`; same-format guards
by `418bf87`; the live Pi TUI harness by `a6ee83c`; and v0.4.0 release metadata
by `29a4fc2`. Copilot excess-result structural multiplicity was corrected by
`10ba28e`; fresh target-native IDs for exact runtime replay landed at `8a9a49d`.

### Late-corpus defect and independent tail gate

The first bounded Codex matrix run deliberately stopped at anonymous file
55,274 when a 119 MB rollout exposed two excess duplicate tool results. The
OpenCode writer had correctly classified those completions as unassociable
after its matching tool-part queue was exhausted, but the independent oracle
had modeled only set membership. Correcting the oracle then allowed Copilot's
own byte validator to expose a real writer defect: the retained extra
completions had no additional preceding native request.

The Copilot writer now emits a synthetic request/start with a fresh target ID
for every completion beyond source call multiplicity and reports both
`tool_result:duplicate_id` and `tool_result:orphan_id`. OpenCode likewise uses
a fresh synthetic tool-part ID. Focused regressions cover both targets.
The exact former failure then passed all four targets. An independent restart
from that file through the end validated all 1,493 late-store rollouts and all
5,972 generated targets: every target passed byte validation, migrator reparse,
portable-semantic comparison, and independently recomputed warning counts.
That tail contained 1,473 tool sessions, 1,429 image sessions, 53 compaction
sessions, 17 interrupted sessions, and exactly the two excess results. It ran
for 17 minutes 10.90 seconds and peaked at 2,010,612 KiB RSS; no safety limit
was bypassed. The full-store aggregate below is a separate clean restart from
file 1 after the structural correction. The final target-ID hardening affects
only the two corpus-wide excess results and received the focused current-code
and native replay checks below.

### Codex full-store matrix

The corrected, uninterrupted run inventoried 56,766 Codex rollout files. It
parsed and converted all 56,760 supported legacy rollouts to every different
safe target—Claude, Pi, OpenCode, and Copilot—and rejected exactly six
paginated/history-mode roots through the documented fail-closed guard. For
each target, all 56,760 artifacts passed target byte validation, migrator
reparse, independently projected portable semantics, and independently
recomputed loss counters. There were zero failures inside the supported set
and zero unexplained differences.

The source feature inventory was 17,271 tool sessions, 11,883 image sessions,
82 compaction sessions, 8,891 interrupted sessions, 56,000 sessions with
opaque/runtime metadata, 8,198 files from 1–10 MiB, and 1,366 files at least
10 MiB. The two excess duplicate results occurred in one supported rollout.
Claude and Pi retained them with duplicate warnings. OpenCode and Copilot also
reported them as orphaned native associations after call multiplicity was
exhausted. Every semantic and warning assertion matched; the final writers
give each excess result a distinct target-native call ID.

Important aggregate target-specific transformations were:

| Target | Selected exact aggregate counts |
| --- | --- |
| Claude | 103,717 privileged messages, 182,024 reasoning events, 69,440 turn-context records, 5,650 UI-only projections, 427 expanded replacement-history checkpoints, 2 duplicate results |
| Pi | 103,717 privileged messages, 182,024 reasoning events, 69,440 privileged context images, 5,650 UI-only projections, 460,417 opaque/runtime events, 2 duplicate results |
| OpenCode | the Pi categories plus 25,294 later results associated to earlier native parts, 5 native-time adjustments, and 2 duplicate/orphan results |
| Copilot | the Pi categories plus 14,369 source text-block groupings, 71 native-time adjustments, 114,720 provider-dependent tool-result images, and 2 duplicate/orphan results |

These are expected, manifest-accounted source-only omissions or target-native
transformations, not silent losses. In particular,
`tool_result:image_provider_dependent` means Copilot retained the exact native
asset while provider replay remained wire-protocol dependent.

The content-safe selected report covered 30 stratified source sessions and all
four targets: 120 side-by-side target cases and 115,391 structural/value-length
rows. Every row was `exact=True`; there were zero missing/false rows and zero
credential-pattern matches. The report contained no path, ID, title, message
or tool value, timestamp, CWD, or hash.

The post-corpus native subset selected ten feature-diverse sources. Pi 0.80.6
loaded 10/10 through offline RPC and preserved 10/10 exact generated prefixes.
OpenCode 1.17.20 officially imported/exported 10/10 and matched all ten
portable semantic projections. The subset included tools, images, compaction,
interruptions, opaque/runtime metadata, and a file above 1 MiB; its private
workspace was removed. The full command exited zero after 2 hours 18 minutes
5 seconds and peaked at 3,028,692 KiB RSS with no swap. All source limits
remained enabled.

The tagged tree also reran the independent core-target oracle on ten
feature-stratified real Codex sources. Claude Code 2.1.209 cold-resumed all
10/10 in the immutable Docker image, preserved every generated prefix, and
appended each new prompt with correct graph ancestry. The cases covered tools,
images, compaction, interrupted histories, and a large rollout. The image had
networking disabled, received no credential mount, and the private workspace
was removed. The same inventory pass parsed all 56,760 legacy rollouts and
classified the six paginated roots as the expected fail-closed rejection.

The exact 119 MB former failure was then regenerated on final code and again
passed all four target byte validators, reparses, semantic projections, and
loss counters. Copilot 1.0.70 cold-resumed it, preserved and appended to the
exact prefix, rebuilt SQLite, and completed a loopback-provider turn. Its
model request did not contain every historical value: the runtime pruned the
very large history to its usable context, so this probe is explicitly *not*
counted as exact provider replay. A compact synthetic one-call/three-result
case isolated the changed path: the exact 1.0.70 CLI cold-resumed it, preserved
the prefix, rebuilt SQLite, and replayed every message/call/result value to the
loopback provider with no inherited credentials. This proves the fresh-ID
linkage while preserving the honest large-context boundary.

### Pi real-store matrix

All four accessible Pi v3 sessions were parsed and converted to each different
safe target: Claude, Codex, OpenCode, and Copilot. All 16 artifacts passed
native-byte validation, target-adapter reparse, independently projected
portable timeline comparison, and independently recomputed loss counters. No
real Pi source failed or hit a safety bound. Source feature incidence was four
branch/runtime-metadata sessions and one interrupted session.

Every target reported the same expected source-only details: five
`model_change` records, four `thinking_level_change` records, one nonportable
assistant error stop reason, and two private-thinking events. No unexplained
difference remained. Synthetic regressions additionally cover active/inactive
branches, compaction, tools, images, malformed/cyclic ancestry, Unicode line
separators, unknown nested result blocks, and `parentSession` lineage. Official
Pi 0.80.6 source confirms that branch/fork files copy their selected/full entry
history and use `parentSession` as provenance rather than an external history
segment.

### Pi catalog and native targets

A fresh private Pi-only catalog deep-validated 4/4 files and a real opaque
catalog-ID transfer dry run authoritatively reopened one source and produced a
Codex plan without writing a target. The actual default catalog then migrated
to schema v3 and inventoried 57,012 files across three roots: 242 Claude, 56,766
Codex, and four Pi. Statuses were 56,866 candidates and 146 expected unsupported
files, with zero corrupt files or root errors. The full refresh took 209.81
seconds, peaked at 270,952 KiB RSS, and produced a mode-`0600` 133,419,008-byte
private database.
A following incremental refresh took 6.88 seconds: 57,011 files were reused
unchanged and one live Codex rollout was rescanned, with zero missing files or
root errors and a 257,504 KiB peak RSS.

On final-code native reruns:

- all four Pi sources resumed as both Claude and Codex in the pinned Docker
  image: 8/8 resumes and exact prefixes, four Claude append-ancestry checks,
  four Codex SQLite rebuilds, no network, and no mounted credentials;
- all four imported through OpenCode 1.17.20's official importer and exported
  with exact semantic projections; and
- an exact disposable Copilot CLI 1.0.70 installation cold-resumed 4/4,
  preserved all four prefixes, rebuilt all four derived SQLite indexes, and
  replayed exact portable values to a loopback provider with no inherited
  credentials.

### Actual-value review and live Pi TUI

A private browser-rendered review covered all four Pi sources plus three
stratified Codex sources across every different safe target: seven source
cases, 28 target cases, and 592 sampled actual-value rows. Roles, order,
message/tool boundaries, values, and loss summaries matched side by side. One
real inline-image pair was value-identical and visually identical. The
mode-`0600` report and screenshots were deleted and both private browser tabs
were closed.

The checked-in PTY harness separately translated the existing Codex OAuth
record into Pi's documented `openai-codex` shape only inside a private
temporary home. The actual Pi 0.80.6 TUI completed two synthetic live turns;
the second response recalled the first, the imported prefix remained exact,
and the trajectory was persisted. No token or response value was reported, and
the credentials/transcript/log workspace was removed. This explicit test is
not part of `inspect`, `convert`, `import`, `transfer`, or catalog behavior.

An actual OpenCode 1.17.20 TUI also loaded an imported sanitized Pi history and
submitted a follow-up with a disposable translated credential, but persisted
only an unfinished assistant turn with no text. Because neither reply nor
recall was verifiable, this is recorded as a failed live-service gate. It does
not replace or weaken the passing official import/export and loopback-provider
resume contract.

### Pi thinking-trace audit

A later content-free inspection pinned the relevant installed Pi 0.80.6 source
files and tested its reasoning transformation directly. Same-model history kept
the native thinking block and opaque signature; changing models removed the
signature and converted nonempty non-redacted thinking into ordinary text;
cross-model redacted thinking was removed. The OpenAI Responses projection
rehydrated the synthetic raw `reasoning` item in its original position.

The four-file Pi store contained two signed reasoning blocks, including one
whose visible text was empty. After relocating only stale CWD metadata in
private copies, the actual offline RPC runtime loaded 4/4, returned both blocks
and signatures, and preserved all four normalized prefixes. The originals were
untouched and no reasoning value, signature, path, model ID, or session ID was
reported. These results support the existing fail-safe policy: thinking is
counted but not transferred. Full mechanics and source fingerprints are in
[Pi thinking-trace handling](pi-thinking-traces.md).

### v0.4.0 release gates

The final candidate includes the fresh native-ID hardening at `8a9a49d` plus
documentation-only release closure. It passed Ruff, all 158 pytest tests,
Python and shell syntax checks, `git diff --check`, every internal Markdown
link, sdist and wheel builds, and an isolated installation of the built wheel
reporting `session-migrate 0.4.0`. Live help exposed Claude, Codex, and Pi as
sources; Claude, Codex, Pi, OpenCode, and Copilot as writable targets; and
Antigravity/Cursor as explicit unsupported capability choices.

The pinned credential-free Docker regression passed on the same final tree:
Codex appended `3004 -> 9827` bytes and Claude appended `3689 -> 15712` bytes,
with both original generated prefixes intact. A tracked-file secret-pattern
scan found no credential value. All private corpus reports, converted targets,
copied real-session input, disposable credentials, temporary CLI installation,
screenshots, and pointer files were removed; no validation process remained.

### v0.5.0 rename and publication gates

The breaking 0.5.0 release renames the distribution, import package, command,
state paths, environment variables, generated metadata, fixtures, scripts, and
documentation to `session-migrate`. The canonical command and the `smigrate`
alias were both installed from the built wheel in a fresh Python 3.11 virtual
environment; each reported 0.5.0, and `python -m session_migrate` reported the
same version. The fresh environment contained neither a compatibility command
nor a compatibility import package. Manifest validation uses schema version 2
and the `migration_version` key.

The renamed tree passed Ruff, all 158 pytest tests, Python and shell syntax
checks, `git diff --check`, sdist/wheel builds, and isolated-wheel installation.
The pinned credential-free Docker native-resume oracle also passed on the
renamed module/script paths: Codex appended `3003 -> 9826` bytes and Claude
appended `3697 -> 15720` bytes, with both generated prefixes unchanged.

Before public visibility was enabled, the current tree was checked for
developer-machine absolute paths and the removed identity. Every one of the
840 objects reachable from all local refs was then scanned, up to 64 MiB per
blob, for private-key markers and common OpenAI/Anthropic, GitHub, AWS, Google,
Slack, npm, PyPI, JSON-token, and authorization-header forms. No credential or
private session-store path was found. The only token-shaped match was the
literal `synthetic-not-a-secret` provider key in the checked-in isolated native
test. Historical commits and release tags were retained for provenance; the
0.5.0 tree provides no compatibility behavior for its former identity.

### v0.6.0 seven-format campaign

The 0.6.0 candidate combines the seven-format integration at `835f467`, the
public-facing documentation at `888d97e`, and the reusable validation harnesses
at `6b78331`. OpenCode, Copilot, and Antigravity are now readable as well as
writable; same-format portable rewrites are supported; Cursor is deliberately
presented as pinned experimental text-only support.

The generated route oracle passed all 49 source/target pairs plus one auxiliary
Codex tool-error case. A stratified set of 100 real OpenCode 1.17.20 exports was
also rewritten through the official 1.2.27 CLI and sent to every target: all
1,400 artifacts passed native-byte validation, target reparse, ordered text
projection, and independently computed loss counters. The selection covered
tools, errors, parent-linked sessions, all ten time buckets, and files from
below 10 KiB to above 1 MiB.

The experimental Cursor target campaign selected 416 sessions: Claude 104,
Codex 100, OpenCode 200, Pi 5, Antigravity 6, and one exact sanitized Copilot
fixture. Every one of the 414 supported inputs passed; the other two were Codex
paginated/history-lineage sessions rejected by the documented fail-closed
policy. Cursor native basic and repeated-loss fixtures migrated to all seven
targets with exact reason-specific omission accounting. Six real Antigravity
sources migrated to all seven targets, and the Copilot fixture did the same.

Manual content-safe review compared 20 OpenCode sources across six targets and
two source-release views: 240 cases and 12,996 anonymous text rows matched
exactly, with zero unexplained differences. No prompt, response, path, ID, tool
body, or image was retained in the report.

The metadata-only catalog indexed all 70,915 available OpenCode session rows.
An immediate incremental refresh reused all 70,915 with zero rescans, and ten
stratified catalog-ID selections completed official export followed by Codex
dry-run conversion. This proves exhaustive coverage of that configured root,
not whole-disk discovery and not full semantic conversion of every indexed row.

Pinned native/runtime gates included:

- Cursor Agent `2026.03.20-44cb435`: shipped AgentKv load, controlled backend
  blob resolution, headless load, and actual TUI rendering passed. This is an
  experimental clean-room text gate, not proof of a vendor import API or of a
  successful authenticated assistant checkpoint.
- Antigravity CLI 1.1.16: native load/append passed for six source types, and an
  actual full-screen TUI loaded the imported history and accepted a typed turn.
- GitHub Copilot CLI 1.0.70: the exact-binary source trajectory passed, as did
  five native target cold resumes.
- Pi 0.80.6 and OpenCode 1.17.20: representative native load/import/export
  subsets passed. Each actual TUI also persisted two user and two assistant
  turns in a disposable home.
- Codex 0.144.4: the pinned Docker cold native resume/load passed. A separate
  scripted live two-turn TUI attempt was inconclusive because onboarding screens
  consumed its input, so it is not counted as a passing gate.

For the Pi and OpenCode TUI checks only, isolated mode-0700 homes received
mode-0600, schema-mapped copies of the existing Codex OAuth fields. Token values
were never printed or added to a manifest; expiry was derived from the JWT; the
copies and homes were deleted. Credential translation is validation scaffolding,
not a session-migrate feature or a supported login workflow.

The final default gate passed Ruff and 293 pytest tests with four expected
environment-gated skips. Python compilation, shell syntax, lock/diff checks,
sdist/wheel builds, isolated `session-migrate`, `smigrate`, and
`python -m session_migrate` entry points, Vinext build/render/ESLint tests, and
internal Markdown links also passed. All campaign-owned corpus, review,
authentication, and temporary native artifacts were removed, and no validation
process remained.

### v0.6.1 coding-agent instruction gate

The 0.6.1 documentation at `8df676d` adds one plain-language instruction that
delegates a migration to a coding agent. The exact published wording was run in
both directions inside separate homes in Docker image
`sha256:8f170f660813ac358f347fa8a3580139972f3ea7a9fb087834f1da44669d9392`.
The image contains Claude Code 2.1.209 and Codex 0.144.4. Each agent fetched the
public repository documentation and installed the released
`session-migrate==0.6.0` into its own isolated tool environment; neither agent
used the checkout under test.

The Claude agent located a single synthetic Claude UUID through the default
catalog roots, generated a target UUID before conversion, passed it explicitly
to both dry-run and apply, and verified identical session, output, manifest, and
CWD fields. It reported no warnings or counted losses. Independent inspection
reparsed the 15-record Codex target and found the expected ordered messages,
one image, one compaction, and one linked call/result pair. The source SHA-256
was unchanged.

The Codex agent repeated the exact instruction from a fresh source home. Its
dry-run and apply used one fixed UUID and one resolved Claude path. It correctly
reported the fixture's single counted `compaction` transformation and no other
warning. Independent inspection reparsed the six-record Claude target with the
expected ordered messages, image, and linked call/result pair; the source hash
was unchanged. A separate ambiguous catalog run found three
duplicate UUID candidates and stopped before UUID generation or target writes,
as the instruction requires.

Finally, both generated targets were loaded by their native CLIs with networking
disabled. Codex selected the exact generated session ID, appended from 3,003 to
9,415 bytes, rebuilt `state_5.sqlite`, and preserved the original prefix hash.
Claude appended from 3,697 to 7,743 bytes, chained the first new native graph
record to the imported leaf, and preserved the original prefix hash. Network
failure was expected and prevented a model response; these checks prove native
selection and append structure, not live-provider recall.

The disposable agent homes were mode 0700 and transcript/auth files mode 0600.
Only synthetic transcript values were used. Credential copies were confined to
the sandboxed agent and native-loader processes, never included in reports, and
removed with the complete sandbox after these aggregate checks.

The final 0.6.1 tree passed Ruff lint and format checks, 293 pytest tests with
four expected environment-gated skips, shell syntax and diff checks, the Vinext
build/render test, ESLint, and an interactive desktop/mobile browser check of
the copy control. The built wheel and sdist both contain the instruction, and
fresh isolated invocations of `session-migrate`, `smigrate`, and
`python -m session_migrate` each reported 0.6.1.

### v0.6.2 short coding-agent handoff

The long embedded instruction was replaced by one sentence:

> Follow https://session-migrate.github.io/llms.txt to migrate session
> `[UUID OR TITLE]` from `[SOURCE]` to `[TARGET]` now.

The root, landing-page, and GitHub Pages copies of `llms.txt` were byte-identical,
and the served file returned `text/plain`. The first isolated Codex run exposed
a prompt-derived catalog title in an intermediate tool log even though its final
answer was content-free. The procedure was therefore hardened to redirect raw
catalog JSON into a mode-0600 temporary file and print only an explicit
structural-field allowlist. A subsequent Codex 0.144.4 run contained no fixture
message/title markers, used public `session-migrate==0.6.1`, produced exactly one
six-record Claude target plus one manifest, preserved its image and linked tool
pair, reported the expected single compaction transformation, and left source
and authentication files unchanged.

The first allowed-tool Claude retry found another failure mode: after incorrectly
concluding that the package was unavailable, it hand-wrote a Codex rollout and
omitted the manifest. That artifact was rejected and removed. `llms.txt` was
hardened again with the exact PyPI virtual-environment command, a mandatory stop
on install/version failure, and an explicit prohibition on target-CLI templates
or manually constructed native data. The fresh Claude Code 2.1.209 run then used
the released CLI, produced exactly one 15-record Codex target and one manifest,
reported no losses, preserved its image and linked tool pair, emitted no fixture
body/title/tool-ID marker, and left source and authentication files unchanged.

Both exact prompts ran in the pinned Docker image
`sha256:8f170f660813ac358f347fa8a3580139972f3ea7a9fb087834f1da44669d9392`
with separate homes and synthetic native sessions. Browser QA loaded the public
landing page, clicked the instruction control, observed `Copied`, matched the
one-sentence clipboard source, and loaded the hardened public `llms.txt`.

The 0.6.2 release candidate passed Ruff lint and format checks, 293 pytest tests
with four expected opt-in skips, shell syntax and diff checks, internal Markdown
link verification, the Vinext build/render tests, and ESLint. Its wheel and
source distribution built successfully; fresh isolated invocations of
`session-migrate`, `smigrate`, and `python -m session_migrate` each reported
0.6.2. Both credential-bearing sandbox directories were then permanently
removed and no owned sandbox process remained.

### Native demo capture gate

The public before/after assets were regenerated from actual native clients,
not from a transcript renderer. The recorder uses the same tmux + asciinema +
`agg` approach as agent-talk. Claude Code 2.1.237 ran at low effort, inspected a controlled Python
timeline project, diagnosed its strict boundary comparison, accepted a second
prompt typed character by character at real-time speed, and proposed the
minimal patch plus regression test. The resulting JSONL was converted
independently to Pi and Codex. Pi 0.80.6 and Codex 0.144.4 in the pinned Docker
image each reopened the imported history, accepted a native typed continuation,
changed `<` to `<=`, added the touching-versus-1-ms-gap regression, and left all
three focused tests passing. The capture harness independently executes that
behavioral check and requires the new third test before rendering succeeds.

The published MP4s and GIFs render at 1440×560. Both show the source, migration,
resume, shared-history, and target-continuation stages. The three comparison
PNGs are full-resolution final frames from those native TUI recordings. The
landing page serves the sanitized native `.cast` files through a vendored
Asciinema Player build; it contains no video element. Its controller first
centers Claude, moves that cast left, types the migration and native resume
commands, reveals the target cast, marks shared history in both panes, and then
expands the target. The collapsed before/after comparison mounts paused,
seekable players over those same native casts instead of screenshot elements.
`scripts/render-video-demo.py` independently composites those cast streams into
the README MP4/GIF without capturing the landing page or its controls.
The capture ran inside a mode-0700 temporary workspace with mode-0600 copies of
Claude and Codex OAuth state. Pi received only the documented field mapping of
the copied Codex OAuth record. Account welcome/status events were excluded from
the public cast; all published conversation text belongs to the controlled
fixture. The complete
credential-bearing workspace, npm prefix, raw PTY recordings, and logs were
removed after rendering. Both native target files independently contained the
expected new assistant turn before cleanup.

### Catalog and presentation hardening

The unreleased title-search update was tested with reversed Unicode keywords,
missing-keyword rejection, UUID lookup, and the existing rule that conversation
bodies are not searchable. The complete default suite passed with 292 tests and
five expected environment-gated skips; Ruff, diff checks, the Vinext build,
ESLint, and all four rendered-page tests also passed.

Browser QA opened the static GitHub Pages candidate at 1440×1000 and 390×844.
The collapsed comparison mounted two Asciinema players only after opening,
rendered nonempty Claude/Pi terminal states, switched and remounted Codex, used
zero comparison images, hid both start overlays, and produced zero horizontal
overflow at either viewport. The new SVG thread-handoff mark was checked at
navigation and hero sizes in the same browser run.

### v0.7.0 Mistral Vibe gate

The Vibe adapter was implemented from the official Apache-2.0 source at tag
`v2.24.3` (`a84be0391bf93e93a4025a5e08e8032ecb587123`) and an isolated exact PyPI
installation. A sanitized two-file fixture covers native metadata, ordered
text, an inline image, linked tool call/result, and compaction. The adapter's
focused tests cover bundle validation, malformed fields, private publication,
short-ID collision, inspection, discovery, title indexing, catalog-ID transfer,
and every ordered source/target route.

The first actual CLI probe loaded the generated session but rewrote its JSONL
prefix. The discrepancy was traced to Vibe's append-boundary hash over a
Pydantic model dump with materialized defaults. After matching that exact
2.24.3 serialization/fingerprint, the credential-free native oracle passed:

- actual `vibe --resume <short-id> -p ...` selected the imported session;
- a loopback provider request contained the migrated post-compaction user and
  assistant history plus the new prompt;
- the generated `messages.jsonl` remained an exact byte prefix;
- Vibe appended a synthetic user and assistant turn; and
- the normal source adapter reparsed both appended messages.

The default candidate ran 324 collected Python tests: 318 passed and six
explicit environment-gated native/store tests skipped. The Vibe native test
then passed separately with `SESSION_MIGRATE_VIBE_BIN` pointing to exact
2.24.3. Ruff lint and format checks passed. The website lint/build suite and
all six rendered-page tests passed with eight visible agents, 64 route copy,
interactive Vibe source/target choices, canonical metadata, and the regenerated
eight-harness preview. Browser QA at 1440×1000 and 390×844 found all eight
capability entries, the exact Vibe→Vibe coding-agent prompt, and zero horizontal
overflow.

### v0.7.1 named-demo and title-transfer gate

The release demo uses the native catalog title `fix-timeline-merging` from
source selection through target resume. `transfer --title` was verified against
a private synthetic catalog for case-insensitive exact-title selection and a
credential-free Claude→Codex dry run; ambiguous and missing titles remain
fail-closed through the catalog API.

The browser renderer captures one continuous 43-second story for both
Claude→Pi and Claude→Codex. It refuses to publish unless both native terminal
panes contain the same bounded shared-history passage and both semantic
highlight anchors are visible during the overlap phase. The source cast's
split ANSI redraw was corrected at its original cursor boundary, eliminating
the one-character discrepancy visible only at the wide export geometry.
Representative frames were inspected at the completed-transfer hold, shared
history, and target-only stages for both routes. The hero terminal types once,
stays complete, and replays only through its explicit circular-arrow control.
The follow-up media export uses deterministic timeline seeking, expands focused
native panes from 80% to 96% of the frame, widens both handoff panes, and emits
1872×1112 MP4s without baking the website's story-control bar into the media.
Every authored overlay font was enlarged, the native red session-limit notice
and title bars received additional emphasis, and the migration pane uses bounded
wrapping so its right edge cannot clip. Frames downscaled to 640 pixels wide
were inspected to verify that `fix-timeline-merging`, the migration command and
output, and both matching shared-history passages remain legible, fitted, and
synchronized.

The session-limit beat is intentionally simulated rather than produced by
spending a real account's quota. Its wording and presentation were checked
against the installed Claude Code 2.1.241 Linux binary (SHA-256
`0771bd866cff82b76581fc0499f6529e1a36845078f144f8c81dccb3bc7037b8`): the
current renderer prints `You've hit your limit · resets …` with the `error`
color and renders the upgrade guidance as dim text. The demo injects those two
terminal rows only; it no longer labels a usage limit as a full context window.

The final 0.7.1 candidate passed 319 Python tests with six documented optional
native/store skips, Ruff lint and format checks, diff checks, the Vinext build,
ESLint, six rendered-page tests, and JavaScript syntax checks for the static
Pages build. Both MP4s are exactly 43 seconds, and every GIF/MP4 copy in the
README, application site, and canonical Pages tree has an identical route-wise
SHA-256 digest. The wheel and source distribution built successfully; isolated
invocations of `session-migrate`, `smigrate`, and `python -m session_migrate`
all reported 0.7.1.

### v0.8.0 Muse, Qwen Code, and Kimi Code gate

Three version-pinned, bidirectional native adapters were added for Muse Code
0.2.1, Qwen Code 0.22.1, and Kimi Code 0.38.0. The default mechanical suite
exercises malformed-input rejection, native serialization/reparse, discovery,
inspection, catalog title/ID search, collisions, exact loss counters, and the
complete 11-by-11 ordered route matrix. The matrix passed all 121 routes plus
one auxiliary tool-error case.

The reusable content-safe corpus gate
`scripts/validate-muse-qwen-kimi-corpus.py` then ran against every 86 top-level
Claude main transcript present in the release snapshot and an evenly selected
100-session Codex sample. For all three new targets, every supported source was
converted, byte-validated, reparsed, and matched an independent projection of
the target's documented portable timeline: 558 target artifacts and zero
mismatches. The selected histories covered 99 tool sessions, 7 compaction
sessions, 135 thinking-bearing sessions, and one Codex user-image session.
No session body, path, ID, title, tool value, media value, or hash was printed
or retained by the aggregate gate.

The three real native trajectories produced by the live gate were also parsed
as sources and migrated to all eleven targets. All 33 artifacts passed native
validation, reparse, and target-specific portable-history comparison. This
separately exercises the inverse direction instead of assuming that a writer
test proves the corresponding reader.

The opt-in provider oracle used one explicitly supplied mode-`0600` OpenRouter
key file and disposable mode-`0700` homes. It invoked the exact native clients
and validated models:

- Qwen Code 0.22.1 with `qwen/qwen3-coder-next`;
- Kimi Code 0.38.0 with `moonshotai/kimi-k2.7-code`; and
- Muse Code 0.2.1 through `muse-code-openrouter` 0.3.2 with
  `meta/muse-glimmer-30b`.

Each native client selected its imported session, appended a real provider
turn, kept the complete generated transcript as an unchanged byte prefix, and
was reparsed by the ordinary source adapter. Each model identified
`README.md`, a fact available only in the imported linked tool history. This is
stronger than checking that a process exits successfully: it proves that the
native runtime actually supplied migrated context to the provider.

The normal suite never reads a credential or uses the network. The live tests
skip unless all pinned binary and credential-file environment variables are
set explicitly. On the release candidate, the default gate produced 386
passes and 9 documented opt-in skips; the separately enabled OpenRouter gate
produced 3 passes. Ruff lint/format and the six-test landing-page build/render
suite also passed. The credential and every provider-created test home were
removed after the release gates; credentials and provider settings are never
migration inputs or outputs.

### Unreleased Oh My Pi 18.0.5 gate

The OMP implementation is split across adapter, discovery/catalog, and route
commits `ed2d438`, `fab6fed`, and `527d7b1` (with the independently discovered
OpenCode timestamp repair in `baf1caa`). The sanitized v3 fixture and generated
targets use OMP's current 256-byte title slot; no vendor binary, credential, or
real transcript content is tracked.

The complete default suite passed with **427 tests** and ten documented
environment-gated skips. The parametrized source/target oracle covered all
**144 ordered routes**, including OMP→OMP, and reparsed every target before
comparing its target-specific portable timeline. OMP-focused coverage includes
current and legacy heads, Unicode title bounds, reset boundaries, inactive
branches, private/runtime loss accounting, linked tools, compaction,
content-addressed image hashes and symlink refusal, malformed graphs, direct
discovery, catalog title search, incremental refresh, transfer, and packaged
CLI help.

The separately enabled exact-binary test passed against OMP `18.0.5` Linux x64
(`183420104` bytes, SHA-256
`d5a322af241cebe2662b3b792ff29d3ea6e61364328e916c9429065f346391ed`).
In an isolated credential-free home, actual OMP RPC loaded the generated
history, exposed it in a loopback model request, appended a new user/assistant
turn without changing the imported journal body prefix, and updated the native
title slot through `set_session_name`.

Ruff lint and format checks, diff checks, sdist/wheel build, both isolated wheel
entry points, and a packaged Claude→OMP→inspect smoke test passed. The wheel
reported `omp` in source/target choices and exposed `--omp-root`.

### v0.9.0 Grok, Kilo Code, and OpenHands gate

Three first-class readable/writable adapters were added for exact Grok 1.0.5,
Kilo Code 7.5.0, and OpenHands CLI 1.16.0 / SDK 1.21.0 builds. Sanitized
fixtures cover ordered text, images, linked tools/results, compaction or
condensation, native titles, version metadata, and reason-specific loss
counters. Malformed-input tests cover identity/linkage mismatches, unsafe
paths, duplicate records, bounded JSON depth/counts, and live multi-file
replacement/append races.

The route oracle exercised all **225 ordered source/target pairs** and reparsed
every generated target before comparing the target-specific portable timeline.
Discovery, content-free inspection, catalog refresh/title search, direct and
catalog-ID transfer, collisions, same-format rewrites, and packaged CLI choices
are covered for all three new formats. OpenCode-lineage bundles fail closed
during autodetection because Kilo and OpenCode share a schema with no reliable
producer marker; explicit `--format` selection is tested for both.

Credential-free native gates used a loopback OpenAI-compatible server and the
exact pinned Linux x64 artifacts. Grok resumed and appended to its paired ACP
update stream; Kilo imported, continued, and re-exported through only its
official commands; OpenHands loaded SDK event files and appended native user
and assistant events without modifying the imported event prefix. Model
requests independently contained markers available only in imported history.
No provider key, account state, or network model was used.

The same exact-binary tests then opened each installed session through a real
interactive terminal path in a bounded PTY. Grok's fullscreen TUI and Kilo's
mini TUI rendered the imported compaction, final history, and appended reply.
OpenHands' TUI opened the imported conversation ID; its native `view` command
rendered the imported user/tool history and appended reply. This separates
visual/native presentation from the model-context assertion instead of treating
a successful headless exit as sufficient evidence.

The final local suite, in the fully provisioned validation environment, produced
**555 passes** and 13 explicit optional-native skips. Clean GitHub Actions
runners produced **551 passes** and 17 environment-gated skips on each of Python
3.11, 3.12, and 3.13. With the three exact binary variables enabled, all three
new native replay/TUI tests passed. Ruff lint and format checks passed, as did
the website build, ESLint, and all six rendered-page tests.

The release gate also checks coherent paired/directory reads while a native CLI
may be writing. Grok validates `summary.num_messages`, file identities, finite
JSON, depth, and total nodes. OpenHands snapshots its bounded event inventory
and derived metadata before and after reading and validates event/action
linkage. It deliberately does not fabricate a partial `base_state.json`: the
pinned SDK rebuilds the complete runtime snapshot on first resume, and the
native gate verifies the resulting CWD/model state. Kilo installation runs the
official importer from the requested workspace because 7.5.0 otherwise
rewrites the imported CWD.

### v0.10.0 Hermes Agent, MastraCode, and Devin CLI gate

The release adds strict adapters for Hermes Agent 0.20.6, MastraCode 0.37.1,
and Devin CLI 3000.6.7. Focused suites cover native ID/path resolution,
multiple logical sessions in one database, active-branch selection, linked
tools/results, images, compaction, collision refusal, non-mutating dry runs,
WAL-aware snapshots, malformed schemas/JSON/graphs, bounded input, and exact
loss counters. A two-generation Devin rewrite regression proves that an
imported compaction marker never alternates into ordinary user text.

The route oracle exercises all **324 ordered source/target pairs**. Existing
routes retain their strongest content-block comparisons; Hermes, MastraCode,
and Devin additionally verify the text, call ID, tool name/input, result/error
state, and supported message/image/compaction timeline that each native schema
can represent. Hermes bundles are validated against the official import shape;
MastraCode output is reparsed from a native one-thread LibSQL database; Devin
output is transactionally installed into a schema-v16 store and reparsed.

Catalog tests create two sessions in each shared store and prove exhaustive
six-session inventory, keyword/title lookup, content-free virtual entries,
authoritative per-ID loading, deep validation, stable incremental rescans, and
missing-ID detection. Separate CLI tests run direct dry transfers for
Hermes→MastraCode, MastraCode→Devin, and Devin→MastraCode. Automatic
environment roots and bounded project discovery are tested for all three.

The exact Hermes gate uses the pinned source tree and official importer. The
real `hermes chat --resume` loopback request received imported messages,
tool/result history, compaction, and a follow-up; its reply appended to the same
ID and native compaction reparsed. The exact MastraCode 0.37.1 binary likewise
resumed the imported UUID, exposed the full history to a loopback provider,
appended a reply, and initialized native observational-memory state. Neither
gate needed an external provider credential.

The exact Devin 3000.6.7 binary recognized three separately imported native
trajectories through `devin list --format json`. `devin --resume` selected the
requested session and reached its real login boundary without changing the
store. A separate authenticated manual gate then used a disposable Devin Free
account and the CLI's default model to complete a real `--print` turn. Devin
persisted 28 native message nodes; `smigrate inspect --format devin` selected
the active chain and projected the user/assistant history, and a Devin→Claude
conversion preserved the unique prompt/response marker. No login credential
or live-account state is present in the repository or inherited by CI.

The final local release suite produced **713 passes** and 16 explicit
optional-native skips. Enabling each new exact-client oracle separately added
one passing Hermes, MastraCode, and Devin native trajectory. Ruff lint and
format checks passed; the isolated wheel and source distribution passed Twine
metadata checks; and the website lint, build, and **seven rendered-page tests**
passed. GitHub Actions repeated the Python suite and package build on 3.11,
3.12, and 3.13 and repeated the complete website job successfully.

### Demo media synchronization gate

The README media is rendered directly from the corrected native casts. The
exporter does not load or screenshot the website: `agg` renders each source,
migration, and target terminal stream, then FFmpeg composites them on one
deterministic 53-second timeline. Website controls, the scrubber, captions, and
the fixed-aspect browser viewport therefore cannot leak into the release media.

Representative frames were visually inspected for the Claude limit warning,
the typed migration command, the Pi and Codex overlap states, and both expanded
target states. The red Claude warning remains inside the source terminal while
it moves left; both target highlights mark the exact same bounded passage as
the Claude highlight. Both MP4s are 1440×560 at 30 unique frames per second
(1,590 frames), and both GIFs are 1440×560 at 20 unique frames per second (1,060
frames). Frame-hash checks over the pullback and target-zoom intervals found no
duplicated motion frames. Route-wise copies in `docs/assets`, `website/public`,
and the GitHub Pages repository have identical SHA-256 digests.

## Known boundaries

- Codex paginated history and `history_base` lineage remain fail-closed until
  their effective-history and fork semantics can be reproduced and native
  tested without relying on derived SQLite state.
- Provider-encrypted Codex replacement-history state cannot be translated to
  Claude. The migrator retains visible expanded history and reports the semantic
  difference.
- Audio, Claude subagents/sidechains, system/developer instructions, private
  thinking/reasoning, runtime policy, external credential stores, and live tool
  state are not replayed.
- Cursor support remains tied to one exact Linux build and transfers ordered
  user/assistant text only. A successful authenticated Cursor assistant
  checkpoint followed by a second native resume has not been proven.
- Antigravity's imported history and typed append are native-tested, but the
  isolated account did not produce a successful new model response.
- Devin 3000.6.7 lists and selects imported trajectories through its real
  binary, and a separate disposable Free account has produced and reparsed an
  authenticated native model turn. CI remains credential-free and therefore
  covers the deterministic database boundary rather than spending live model
  credits.
- Vibe compatibility is pinned to 2.24.3. The native gate proves exact
  model-visible history and append behavior against a credential-free loopback
  provider, not compatibility with later local schemas.
- Real private-session content was never sent to a live model. Authenticated
  TUI checks used synthetic nonce prompts; corpus replay remained local,
  content-safe, and exact.
