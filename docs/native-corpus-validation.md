# Native corpus and route validation

This document defines the stronger evidence required to say that all 324
ordered routes have been tested from native-produced source sessions. It also
records the limits of the current route oracle. It is an implementation and
release contract, not a claim that the native corpus described below already
exists.

## Current status

The current parametrized test covers every ordered pair among the eighteen
formats. It runs 324 conversions, materializes each target artifact, and either
reparses it with the corresponding `session-migrate` reader or validates the
Hermes import bundle. This is valuable deterministic coverage of routing,
serialization, native layout, and the portable event order.

It is not yet a native-produced 18-source corpus:

| Matrix source | How the current source `Session` is constructed |
| --- | --- |
| Claude Code | Parse a checked-in synthetic native-shaped JSONL fixture |
| Codex | Parse a checked-in synthetic native-shaped rollout fixture |
| Pi | Parse a checked-in synthetic native-shaped v3 fixture |
| OMP | Parse a checked-in synthetic native-shaped v3 fixture |
| OpenCode | Parse a checked-in synthetic official-export-shaped bundle |
| Copilot | Parse a checked-in synthetic native-event-shaped JSONL fixture |
| Antigravity | Serialize the Claude fixture with this project's writer, then parse the generated database |
| Cursor | Serialize the Claude fixture with this project's writer, then parse and project the generated database |
| Vibe | Serialize the Claude fixture, materialize its two native files, then parse them |
| Muse | Serialize the Claude fixture, then parse the generated event stream |
| Qwen | Serialize the Claude fixture, then parse the generated chat graph |
| Kimi | Serialize the Claude fixture, materialize its state and wire files, then parse them |
| Grok | Serialize the Claude fixture, materialize its summary and update files, then parse them |
| Kilo | Serialize the Claude fixture, then parse the generated official-import-shaped bundle |
| OpenHands | Serialize the Claude fixture, materialize its event files, then parse them |
| Hermes | Relabel the parsed Claude `Session`; no Hermes source parser is involved in this matrix input |
| MastraCode | Serialize the Claude fixture to a database, then parse it |
| Devin | Serialize the Claude fixture, install it into a database, then parse it |

All checked-in fixtures contain deliberately synthetic text, IDs, paths, media,
and tool activity. Seventeen matrix inputs pass through a project reader, but
that does not make them native-produced: eleven of those are correlated
writer-to-reader round trips, and Hermes is only a relabel. Separate exact-client
oracles provide important native loading and continuation evidence, but they do
not replace a native-produced source fixture for every row of the route matrix.

### What the current route comparison asserts

For the portable classes enabled for a target, the current signature compares
ordered values for:

- user and assistant message text;
- user image URLs;
- tool call ID, name, and canonical JSON input;
- tool result call ID, text, error flag, and supported content blocks; and
- compaction summary text.

The comparison intentionally reflects known target limits:

- Claude, Antigravity, Cursor, Muse, and Qwen exclude compaction from the
  route signature;
- Antigravity, Cursor, Muse, and Grok exclude images;
- Cursor excludes tool calls and results;
- Hermes, MastraCode, and Devin do not compare the portable tool-result block
  envelope; and
- Copilot and Vibe compare adjacent same-role messages after their native
  grouping transformation.

Thinking, opaque events, system messages, timestamps, provenance, title, CWD,
model/provider metadata, graph topology, and most native-only state are not in
the route signature. Exact route-wise loss counters are not currently compared;
the matrix only adds a same-format rewrite warning assertion. The target output
is not launched through its real CLI in each of the 324 cases. Because most
outputs are written and read by the same adapter, a mutually compatible reader
and writer defect can evade this oracle.

## Stronger acceptance criteria

The stronger route claim is accepted only when all of the following are true:

1. Each pinned harness has at least one source session created by that exact
   native CLI through a real conversation flow, not by a `session-migrate`
   writer.
2. The source session includes every safely triggerable portable capability the
   pinned harness exposes. A capability that cannot be exercised has an explicit
   evidence-backed status instead of being silently absent.
3. Each native capture is parsed independently and its expected portable events,
   source-only events, ordering, linkage, and metadata are asserted before it is
   used as a matrix source.
4. Every one of the eighteen parsed native captures is converted to all eighteen
   targets, including a fresh same-format portable rewrite: 324 output cases.
5. Every output is strictly validated, materialized in its native layout,
   reparsed, and compared against a route-specific semantic and loss oracle.
6. Each target writer has an independent exact-version native load/resume gate.
   A project writer-to-reader round trip alone is insufficient evidence that the
   vendor CLI accepts the output.
7. Unmapped data produces exact documented loss counters or a fail-closed error.
   A test must not obtain equality merely by excluding the same class from both
   sides.
8. Credentialed model calls remain explicit release gates. Default CI is
   deterministic, credential-free, and tests frozen sanitized captures.

The 324 frozen conversions do not require 324 paid model calls. A real model
conversation is required once per native source capture where an account and
model are available. Independent target gates then prove that the exact target
CLI can discover, load, and continue a representative imported history.

## Native fixture provenance

A checked-in fixture may be derived from a native session only when its
provenance sidecar records enough information to reproduce and audit it. The
sidecar must contain:

| Field | Required evidence |
| --- | --- |
| Harness | Canonical format name and exact CLI/package version |
| Build identity | Package digest, binary digest, source revision, or all available equivalents |
| Platform | OS, architecture, and relevant runtime versions |
| Capture time | UTC timestamp and repository revision of the capture procedure |
| Invocation | Content-safe command shape and environment-variable names, never values of secrets |
| Provider | Provider/model identifier or `loopback`; no token, account ID, or credential path |
| Interaction plan | Ordered safe prompts, tool requests, modality attempts, and expected markers |
| Native artifacts | Relative artifact roles, public-fixture hashes, and required multi-file snapshot boundaries |
| Sanitization | Script/version, fields changed, fields removed, and why each change preserves the schema under test |
| Verification | Exact native command that reloaded the sanitized capture and the structural assertions it passed |
| Capability result | Observed, unsupported, rejected, unavailable, or not attempted, with a reason |

The capture conversation must use deterministic, non-sensitive content. It may
ask the model to identify a generated test image, read a disposable fixture
file, run a harmless command, and recall a unique nonce. It must not include a
real repository, private conversation, user home path, credential, or personal
identifier.

Credentials and account state never enter a fixture, sidecar, log, or test
snapshot. Raw local artifacts are treated as private until a schema-aware
sanitizer produces the public fixture. Sanitization must be mechanical and
reviewable; manually reconstructing an artifact from the documented schema does
not qualify as a native capture. The sanitized artifact must then load in the
same exact native CLI. Any modified checksum, graph ID, timestamp, database
index, content-addressed blob, or cross-file reference must be regenerated by
the sanitizer and revalidated.

For a shared database, the public fixture should contain only the captured test
identity and the minimum native schema needed to load it. For a multi-file
session, the capture must snapshot all authoritative files coherently. The
sidecar records the hashes of the sanitized public artifacts; a private raw hash
may be retained outside the repository but must not become a prerequisite for
CI.

## Capability and modality matrix

Harness capability and portable mapping are different questions. The corpus
must maintain a versioned matrix with one row per capability and one column per
harness. Each cell uses one of these states:

| State | Meaning |
| --- | --- |
| `native-verified` | The exact native CLI created and reloaded the event |
| `mapped` | The reader and at least one target writer preserve the portable form |
| `loss-tested` | Native data exists but migration intentionally omits it with an exact counter |
| `native-only` | The CLI supports it, but no portable representation is defined yet |
| `unsupported` | The pinned CLI rejects or does not expose the capability |
| `unavailable` | A required account, platform, or provider prevented a conclusive test |
| `unknown` | No reliable observation has been made; this blocks a complete claim |

At minimum, the capability rows should cover:

| Capability family | Cases to distinguish |
| --- | --- |
| Conversation | Multiple user/assistant turns, adjacent same-role blocks, Unicode, and empty/whitespace boundaries |
| User images | PNG and JPEG, inline and file/URL forms where supported, multiple images, and surrounding text |
| Tool-result images | Image with text, image-only result, multiple images, and invalid/oversized media loss |
| Files/documents | Text file, source file, PDF/document attachment, and provider-side file reference where supported |
| Audio and video | Native input, rejection, or explicit loss; never assume image semantics apply |
| Tools | Successful and failed calls, structured arguments, multiline output, parallel/repeated calls, and stable call/result linkage |
| Thinking/reasoning | Readable trace, opaque/provider-bound trace, effort-level metadata, and exact privacy-preserving omission |
| Compaction | Native summary/checkpoint, post-compaction continuation, and source-only pre-compaction state |
| Graph behavior | Retry, branch, reset, abandoned leaf, and active-tip selection where the harness supports them |
| Session metadata | Native title/name, CWD, model/provider, timestamps, and same-format new identity |

The current portable event model has no dedicated audio, video, or document
event kind. Before claiming those modalities, the project must either define a
bounded portable representation and verified native mappings or assert an exact
unsupported/loss outcome. A harness accepting a file at runtime does not by
itself prove that the file is durably represented in its session history.

## Fixture interaction contract

Each harness-specific capture plan should use the smallest interaction that
exercises its verified capabilities. A typical capable harness would perform:

1. Start a new named session in a disposable workspace.
2. Send a user message containing a unique corpus nonce and a small generated
   image whose visible text differs from the filename.
3. Require the model to describe the image and invoke a harmless native tool to
   read a disposable file containing a second nonce.
4. Exercise one successful tool result and, when safe, one deterministic failed
   tool result.
5. Continue with a second user turn that asks the model to recall both nonces.
6. Trigger native compaction, branching, reset, or retry behavior only through a
   documented public/native control when the pinned harness exposes it.
7. Exit cleanly, snapshot the authoritative native files, sanitize them, reload
   the sanitized session by native ID, and append one verification turn.

If a native model cannot be used, a local loopback provider can prove request
replay and append behavior, but the sidecar must label that capture `loopback`.
It does not satisfy a real-model criterion. If the CLI has no supported way to
attach an image or other modality, the capture records `unsupported`; it must
not inject a fabricated native record and call it observed behavior.

## Test layers

### Stage 1: capability reconnaissance

- Pin the exact binary/package/source and record its digest.
- Determine authoritative session paths, multi-file snapshot rules, and native
  session selection.
- Exercise supported input controls through the actual CLI or TUI.
- Record inconclusive capabilities as `unknown` or `unavailable`.

No completeness claim is made while a required cell remains `unknown`.

### Stage 2: native capture and sanitization

- Produce one capability-rich native session per harness.
- Save terminal/cast evidence locally when useful, but commit only content-safe
  native artifacts and provenance.
- Run a deterministic sanitizer and inspect its diff at the structured-record
  level.
- Reload and continue the sanitized artifact in the exact native harness.

A fixture that only passes the project parser is rejected at this stage.

### Stage 3: source parser oracles

For each of the eighteen native captures:

- assert native identity, active chain, title, CWD, version, and record count;
- assert every expected message, image/media event, linked tool pair,
  compaction, and readable reasoning marker in order;
- assert exact opaque/source-only counts and reasons;
- assert malformed variants fail closed; and
- prove stable parsing from an immutable coherent snapshot.

Expected signatures must be stored independently of the parser implementation.

### Stage 4: 18 by 18 conversion oracle

For every native source and target pair:

1. Parse the frozen source capture.
2. Convert it using the normal public conversion path.
3. Assert target native validation, artifact count/layout, and manifest identity.
4. Materialize and reparse the complete target artifact.
5. Compare a route-specific expected signature, not a shared intersection that
   can hide unsupported data.
6. Assert the exact loss counters, transformation counters, and warning codes.
7. Assert tool linkage, event ordering, target identity, and same-format rewrite
   semantics.

The expected route contract should be generated from a reviewed capability map
and checked in, so changing an adapter cannot silently weaken both sides of the
comparison.

### Stage 5: independent target-native gates

For each target, use the exact pinned native client to:

- discover the installed target by its new native ID or title;
- render or export the imported prefix through a vendor-owned path;
- resume the selected session;
- show that the model/provider request receives the expected portable history;
- append a new assistant response to the same native identity; and
- reparse the continued native artifact without changing the imported prefix.

An official import/export command is preferred. Clean-room targets require an
independent loader/TUI/backend observation. These gates may use a representative
capability-rich input per target instead of repeating all 324 model calls, but
every target writer must have at least one such independent gate.

### Stage 6: CI and release evidence

Default CI runs all frozen source-parser and 324 conversion cases without
network access or credentials. Exact binaries that can be redistributed or
installed deterministically run in a separate native job. Credentialed or
platform-limited continuations remain explicit release gates and report only
content-safe pass/fail evidence.

A release report must list:

- the exact 324 conversion result count;
- the eighteen native source-capture statuses;
- the eighteen independent target-native gate statuses;
- every `unknown`, `unavailable`, experimental, and loss-tested capability;
- skipped gates and their reasons; and
- any route whose expected loss changed since the previous release.

## Completion checklist

The stronger claim is complete only when:

- [ ] eighteen native-CLI-produced, provenance-recorded source captures exist;
- [ ] each capture has passed exact-client reload and continuation;
- [ ] the modality matrix has no unexplained `unknown` cells in claimed scope;
- [ ] all source parser expectations and exact loss classifications pass;
- [ ] all 324 route-specific output cases pass;
- [ ] all eighteen independent target-native gates pass or are explicitly
      excluded from the public support claim;
- [ ] default CI remains credential-free and deterministic; and
- [ ] documentation describes unsupported and lossy behavior without implying
      native evidence that has not been collected.

Until those items are complete, project documentation should describe the
existing suite as a 324-route portable serialization/reparse oracle supplemented
by format-specific native gates, not as 324 conversions from eighteen
native-produced model sessions.
