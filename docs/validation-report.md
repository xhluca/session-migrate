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

## Acceptance definitions

- **Native acceptance** means the pinned target CLI selected the requested
  imported UUID, parsed the generated transcript, appended to that exact file,
  and preserved its imported byte prefix.
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
| Claude nested subagent sessions | 140 | Inventoried; excluded because v0.1 intentionally does not import sidechains |
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

## Known boundaries

- Codex paginated history and `history_base` lineage remain fail-closed until
  their effective-history and fork semantics can be reproduced and native
  tested without relying on derived SQLite state.
- Provider-encrypted Codex replacement-history state cannot be translated to
  Claude. The bridge retains visible expanded history and reports the semantic
  difference.
- Audio, Claude subagents/sidechains, system/developer instructions, private
  thinking/reasoning, runtime policy, credentials, and live tool state are not
  replayed.
- Authenticated semantic recall with a live model is outside this
  credential-free campaign. The portable history itself is compared exactly,
  and native CLIs are proven to load and append it offline.
