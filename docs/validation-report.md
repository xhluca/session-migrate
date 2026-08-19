# Thorough validation report

Date: 2026-08-18

This report records the validation campaign requested after the v0.1 baseline.
It deliberately separates native acceptance, portable semantic equivalence,
expected loss, and manual review. A session is not described as lossless merely
because the target CLI accepts the generated file.

No real prompt, response, path, session ID, tool argument, tool output, or
credential is included here. Aggregate audits operated locally. Temporary
reports containing content were mode `0600`, were never committed or printed,
and were removed after review.

The bridge itself is not a secret scanner. This campaign checked mapping and
loss accounting, not whether real conversations contained sensitive strings;
portable embedded secrets would be copied to a target transcript.

## Evidence artifacts and reproducibility

The campaign used several deliberately distinct code and evidence points:

| Evidence | Bridge revision | Reproducibility |
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
  --from dist/agent_session_bridge-0.1.1-py3-none-any.whl \
  session-bridge --version
```

On v0.1.1 those gates produced 55 passing tests, a clean Ruff and shell-syntax
check, successful sdist/wheel builds, an isolated `session-bridge 0.1.1`
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
reporting `session-bridge 0.1.2`, and internal-link checking. The official
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
visible history was retained for those checkpoints. The bridge also retained
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
`session-bridge 0.2.0`, and live help checks for the additional targets and
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
native event log and bridge reparse kept the exact bytes. That observed
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

## Known boundaries

- Codex paginated history and `history_base` lineage remain fail-closed until
  their effective-history and fork semantics can be reproduced and native
  tested without relying on derived SQLite state.
- Provider-encrypted Codex replacement-history state cannot be translated to
  Claude. The bridge retains visible expanded history and reports the semantic
  difference.
- Audio, Claude subagents/sidechains, system/developer instructions, private
  thinking/reasoning, runtime policy, external credential stores, and live tool
  state are not replayed.
- Real private-session content was never sent to a live model. The authenticated
  Copilot TUI check used synthetic nonce prompts; corpus replay remained local,
  content-safe, and exact. Antigravity had no real provider credential.
