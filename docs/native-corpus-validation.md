# Native corpus and route validation

This document records the evidence behind the 324 ordered routes tested from
native-produced source sessions, the release contract that keeps that evidence
valid, and the limits of the route and exact-client oracles.

## Current status

The provenance-backed v1 corpus is active. It contains one session created from
an empty store by each exact pinned native client. Every public fixture was
mechanically sanitized, independently reviewed as normalized IR, and cold
reloaded or imported by the same client version. Missing capability cells are a
validation error: every source must explicitly record all ten modality classes.

`tests/test_native_corpus_route_matrix.py` runs the exact Cartesian product of
the eighteen sources and eighteen targets. All **324/324 ordered routes pass**.
For every route the test:

1. verifies artifact hashes and parses the native-produced source;
2. compares it with its reviewed expected IR and validates tool linkage;
3. converts through the public conversion path;
4. asserts the exact loss ledger and user-visible warnings;
5. validates and materializes the complete target-native artifact;
6. reparses it through the target reader; and
7. compares target identity, native record count, event order, and the
   route-specific semantic projection.

This is deliberately stronger than a writer-to-reader smoke test, but it is not
324 paid model calls. Real native clients created the eighteen sources once.
Separately, every target has a credential-free exact-client CI job. Sixteen can
replay the imported prefix to a deterministic local provider and persist its
reply. Cursor proves shipped-backend loading and real-TUI rendering; Devin
proves native discovery plus ACP history loading but stops at authentication.
The precise evidence is listed in
[Credential-free native client testing](credential-free-native-testing.md).

### Native source and modality evidence

`preserved` means the source reader exposes a portable representation;
`lossy`/`dropped` means the exact native session contains evidence that cannot
fully migrate; `rejected` is an observed native rejection; and `—` means the
bounded capture did not attempt that class. Full commands, hashes, observations,
and sanitization mutations live beside each fixture in `provenance.json`.

| Harness | Version | Provider | Tools | User image | Tool image | PDF/docs | Audio | Video | Reasoning | Compaction |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Antigravity | 1.1.16 | vendor | preserved | — | lossy | lossy | lossy | lossy | dropped | — |
| Claude Code | 2.1.209 | vendor | preserved | preserved | — | lossy | rejected | rejected | lossy | — |
| Codex | 0.144.4 | vendor | preserved | preserved | preserved | rejected | rejected | rejected | lossy | — |
| Copilot | 1.0.70 | loopback | preserved | preserved | — | lossy | rejected | rejected | — | — |
| Cursor | 2026.03.20-44cb435 | vendor | dropped | — | dropped | lossy | rejected | rejected | dropped | dropped |
| Devin | 3000.6.7 | vendor | preserved | preserved | — | lossy | lossy | lossy | lossy | — |
| Grok | 1.0.5 | loopback | preserved | — | preserved | lossy | rejected | rejected | — | — |
| Hermes | 0.20.6 | loopback | preserved | lossy | — | rejected | rejected | rejected | — | — |
| Kilo | 7.5.0 | loopback | preserved | preserved | — | lossy | rejected | rejected | — | — |
| Kimi | 0.38.0 | OpenRouter | preserved | unsupported | — | — | — | — | lossy | — |
| MastraCode | 0.37.1 | loopback | preserved | preserved | — | rejected | rejected | rejected | — | — |
| Muse | 0.2.1 | loopback | preserved | lossy | — | rejected | rejected | rejected | — | — |
| OMP | 18.0.5 | loopback | preserved | preserved | — | preserved | preserved | preserved | — | — |
| OpenCode | 1.17.20 | loopback | preserved | preserved | — | lossy | rejected | rejected | — | — |
| OpenHands | 1.16.0 | loopback | preserved | rejected | — | rejected | rejected | rejected | — | — |
| Pi | 0.80.6 | loopback | preserved | preserved | — | preserved | preserved | preserved | — | — |
| Qwen | 0.22.1 | OpenRouter | preserved | rejected | — | — | — | — | — | — |
| Vibe | 2.24.3 | loopback | preserved | preserved | — | lossy | lossy | lossy | — | — |

Loopback rows still use the exact public CLI and native session store; only the
model endpoint is deterministic. They prove native request replay and
persistence without spending tokens in CI. Credentialed source captures are
identified explicitly rather than being presented as equivalent evidence.

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

Default CI runs all frozen source-parser and 324 conversion cases without model
network access or credentials. A separate eighteen-client matrix downloads
checksum- or version-pinned clients and exercises them against localhost. Each
job fails when any assigned native test skips. Credentialed or platform-limited
continuations remain explicit release gates and report only content-safe
pass/fail evidence.

A release report must list:

- the exact 324 conversion result count;
- the eighteen native source-capture statuses;
- the eighteen independent target-native gate statuses;
- every `unknown`, `unavailable`, experimental, and loss-tested capability;
- skipped gates and their reasons; and
- any route whose expected loss changed since the previous release.

## Completion checklist

Status for the v1 corpus:

- [x] eighteen native-CLI-produced, provenance-recorded source captures exist;
- [x] each sanitized capture has passed its documented exact-client reload or
      official-import gate;
- [x] every fixture declares all ten modality cells explicitly;
- [x] all source parser expectations and exact loss classifications pass;
- [x] all 324 route-specific output cases pass;
- [x] one representative exact-client or official-import target gate passes for
      every writer;
- [x] default CI remains credential-free and deterministic; and
- [x] unsupported, rejected, lossy, and unattempted behavior is documented.

The representative target gates are not uniform end-to-end TUI tests. Pi and
Antigravity now complete deterministic offline provider turns and persist the
assistant append. Cursor proves its shipped loader, headless history replay,
and real TUI rendering but not a synthetic assistant continuation. Devin proves
native list/discovery, resume selection, and ACP history loading to its
authentication boundary without mutating the store. Some remaining headless
gates do not independently assert picker/TUI rendering. These limitations do
not weaken the 324 deterministic writer/materialization/reparse cases; they
bound what the separate vendor-client acceptance layer proves.
