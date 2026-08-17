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

## Open investigations

- Exact versions and package layout in the cached image.
- Claude JSONL record graph and minimum resumable transcript.
- Codex rollout JSONL and SQLite/state-index interaction.
- Treatment of tool calls, tool results, compaction, images, sidechains, and
  reasoning records.
- Native discovery/listing behavior after a synthetic import.

