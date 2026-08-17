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

## Remaining compatibility work

- Validate authenticated semantic recall with a disposable transfer nonce.
- Add native fixtures for remote-URL images, interrupted turns, branching, and
  schema drift when sanitized examples can be generated safely.
- Re-run the pinned integration suite for every supported Claude/Codex version
  pair.
