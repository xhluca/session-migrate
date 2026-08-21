# Additional native formats

This document summarizes the six adapters beyond the original Claude/Codex
pair. All are readable sources, writable targets, searchable catalog formats,
and same-format portable-rewrite targets in `session-migrate` 0.7.0.

| Format | Pinned build | Native import strategy | Support level |
| --- | --- | --- | --- |
| Pi | `0.80.6` | Write native v3 JSONL | Stable pinned adapter |
| OpenCode | `1.17.20` | Official `export`/`import` CLI | Stable pinned adapter |
| GitHub Copilot CLI | `1.0.70` | Write public session-event schema | Stable pinned adapter |
| Antigravity CLI | `1.1.16` | Clean-room SQLite/protobuf DB | Stable version-pinned adapter |
| Cursor Agent | `2026.03.20-44cb435` | Clean-room content-addressed SQLite/protobuf graph | Experimental, text only |
| Mistral Vibe | `2.24.3` | Write official two-file local session shape | Stable pinned adapter |

“Stable” here means the exact pinned version passed the documented native
oracle. It does not mean a vendor promises its private local format as an
interchange API. The Antigravity and Cursor adapters are explicitly unofficial.
Vibe is public Apache-2.0 software; its adapter is still version-pinned because
the local format is an implementation contract rather than a promised standard.

## Mistral Vibe 2.24.3

Vibe stores `meta.json` and `messages.jsonl` inside one session directory below
`$VIBE_HOME/logs/session`. It provides native text, readable reasoning, linked
tools/results, images, and compaction boundaries. The writer matches Vibe's
exact last-message fingerprint so the native CLI appends without rewriting the
generated prefix. See [Mistral Vibe session format](vibe-format.md) for the
field mapping, loss keys, install contract, and credential-free native oracle.

## Shared contract

Every adapter:

- parses bounded native state into the ordered neutral event model;
- rejects malformed identity, linkage, unsafe paths, unsupported versions, and
  empty resumable histories;
- emits a new target identity and native discovery metadata;
- counts every known omission, transformation, or retained inconsistency in the
  migration manifest; and
- leaves source sessions and credential stores untouched.

Same-format migration runs through this portable model. It creates a new
independent native session, not a byte-for-byte clone and not a synchronized
fork.

## Pi 0.80.6

Pi v3 is append-only JSONL. A `session` header is followed by entries linked by
`id`/`parentId`, including user/assistant/tool-result messages, compaction,
branch summaries, configuration changes, and `session_info` names.

The writer preserves portable text, user images, linked tool calls/results, and
compaction summaries. The parser follows the selected active ancestry and
counts abandoned/runtime-only entries. Native import writes below:

```text
$PI_CODING_AGENT_DIR/sessions/<workspace-bucket>/<timestamp>_<uuid>.jsonl
```

Pi's visible thinking and provider-bound replay signatures are deliberately not
migrated. The exact behavior is documented in [Pi thinking traces](pi-thinking-traces.md).

The pinned native oracle loads generated sessions through offline RPC, checks
the exact generated prefix, and allows Pi's normal startup suffix. An isolated
actual TUI trajectory also completed two live turns using a schema-translated
copy of existing Codex OAuth; credential translation is test-only and is not a
migration feature.

## OpenCode 1.17.20

OpenCode exposes the strongest public contract in this group:

```bash
opencode export SESSION_ID
opencode import BUNDLE.json
opencode session list --format json
```

The migrator reads official export bundles, writes official import bundles, and
never writes `opencode.db`. Catalog refresh reads only bounded metadata columns
from the read-only `session` table; transfer invokes the pinned official
exporter for the selected ID.

Portable parts include text, images/files supported by the source block,
readable reasoning as a counted private event, tools and terminal states,
compaction, model/provider metadata, and parent relationships. OpenCode-only
step/snapshot/patch/runtime fields are explicit opaque losses. Native message
IDs and `time.created` values are made monotonic because the official runtime
orders replay by `(time_created, id)`.

The native oracle imports, lists, exports, and resumes against a loopback
provider. An actual pinned TUI trajectory completed two typed user turns and two
assistant turns in one isolated session.

See [OpenCode source exploration](opencode-source-exploration.md).

## GitHub Copilot CLI 1.0.70

Copilot uses a public local session-event stream:

```text
$COPILOT_HOME/session-state/<uuid>/events.jsonl
$COPILOT_HOME/session-state/<uuid>/workspace.yaml
```

The adapter preserves root-agent text, tools/results, supported images through
content-addressed binary assets, readable reasoning as a private counted event,
compaction, title/workspace metadata, and the final model label. Privileged
prompts, subagents, session-bound encrypted reasoning, lifecycle/UI/permission
state, and provider-specific tool presentation are explicit losses.

The pinned binary cold-resume oracle rewrites a real built-in tool trajectory,
resumes it by the new UUID, confirms the provider receives the imported prefix,
and verifies append-only persistence.

See [Copilot source format](copilot-source-format.md).

## Antigravity CLI 1.1.16

Antigravity stores one trajectory per SQLite database:

```text
~/.gemini/antigravity-cli/conversations/<uuid>.db
```

The clean-room adapter implements the exact observed schema and a bounded
protobuf wire codec. It maps user/planner messages plus generic linked tool
steps/results, takes WAL-consistent source snapshots, installs without
overwriting, and transactionally adds the native picker summary. Private
thinking, permissions, executor state, subtrajectories, task rendering, and
unknown step types are omitted and counted.

The exact `agy` binary is verified by version, size, and SHA-256. The real
1.1.16 CLI loaded generated histories and appended native rows; the actual TUI
rendered imported messages/tools and accepted a typed follow-up. The isolated
account did not expose a working model in the earliest probe, so historical
evidence distinguishes load/append from provider generation.

See [Antigravity format](antigravity-format.md) and the public
[clean-room research repository](https://github.com/xhluca/antigravity-session-interoperability).

## Cursor Agent 2026.03.20-44cb435

Cursor stores resumable CLI state at:

```text
$CURSOR_CONFIG_DIR/chats/<md5-absolute-workspace>/<uuid>/store.db
```

with XDG and `~/.cursor` fallbacks. The database contains a hex-JSON metadata
singleton and SHA-256-addressed protobuf blobs forming conversation-root,
turn, user-message, and assistant-step structures.

The experimental clean-room adapter transfers **only ordered user and assistant
text**. Tools, results, thinking, images, attachments, compaction, system
context, request IDs, shell turns, and unknown fields are never guessed: they
become exact loss counters. Cursor sources likewise expose only text plus one
reason-specific opaque accounting event per unsupported native occurrence.

Automatic install requires exact hashes and sizes for the launcher, `index.js`,
`891.index.js`, bundled Node runtime, and reported version. A Python-built store
was opened through shipped `AgentKv`, rendered by the actual TUI, selected by
the actual CLI, and resolved by the native backend `GetBlob` protocol. A real
authenticated assistant checkpoint followed by a second resume remains
unproven, so the support label stays experimental.

See [Cursor format](cursor-format.md) and the public
[clean-room research repository](https://github.com/xhluca/cursor-session-interoperability).

## Credentials and external state

Authentication is never part of a migration artifact. Native trajectory tests
may use isolated copies or schema translations of credentials only when the
target technically accepts that account/provider shape. Those copies live in
mode-`0700` temporary homes, are never logged, and are deleted after the test.
Users must authenticate each target through its normal supported mechanism.

The converter does not copy shell processes, pending approvals, MCP servers,
workspace files, caches, memories, agent teams, or provider encryption state.
Supported message/tool/image bodies can themselves contain secrets; no redaction
or secret scanning is performed.
