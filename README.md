<p align="center">
  <a href="https://session-migrate.github.io/"><img src="https://raw.githubusercontent.com/xhluca/session-migrate/main/docs/assets/logo-lockup.svg" alt="session-migrate" width="430"></a>
</p>

<p align="center"><strong>Migrate your sessions to any harness.</strong></p>

<p align="center">
  <a href="https://pypi.org/project/session-migrate/"><img src="https://img.shields.io/pypi/v/session-migrate?style=flat-square&color=b8f94a&label=PyPI" alt="PyPI version"></a>
  <a href="https://pepy.tech/project/session-migrate"><img src="https://img.shields.io/pepy/dt/session-migrate?style=flat-square&label=downloads" alt="Total PyPI downloads"></a>
  <a href="https://pypi.org/project/session-migrate/"><img src="https://img.shields.io/pypi/pyversions/session-migrate?style=flat-square" alt="Python versions"></a>
  <a href="https://github.com/xhluca/session-migrate/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-8dbdff?style=flat-square" alt="MIT license"></a>
  <a href="https://session-migrate.github.io/"><img src="https://img.shields.io/badge/website-live-b8f94a?style=flat-square" alt="Project website"></a>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/xhluca/session-migrate/main/docs/assets/demo.gif" alt="A Claude Code session migrated and continued inside the native Pi TUI" width="860">
</p>

<p align="center">
  Move coding agent sessions among <strong>Claude Code</strong>,
  <strong>Codex</strong>, <strong>Pi</strong>, <strong>Oh My Pi</strong>,
  <strong>OpenCode</strong>,
  <strong>GitHub Copilot CLI</strong>, <strong>Antigravity CLI</strong>,
  <strong>Cursor Agent</strong>, <strong>Mistral Vibe</strong>,
  <strong>Muse Code</strong>, <strong>Qwen Code</strong>, and
  <strong>Kimi Code</strong>, <strong>Grok</strong>,
  <strong>Kilo Code</strong>, <strong>OpenHands</strong>,
  <strong>Hermes Agent</strong>, <strong>MastraCode</strong>, and
  <strong>Devin</strong>.
</p>

## Install

```bash
curl -LsSf https://session-migrate.github.io/install.sh | sh
```

Or install with `uv`:

```bash
uv tool install session-migrate
```

`pipx install session-migrate` works too. Python 3.11+ and Linux are currently
supported. The full command is `session-migrate`; `smigrate` is the shorthand.
Already installed? Run `uv tool upgrade session-migrate`.

## Quick start

Inspect any native transcript without printing its conversation:

```bash
smigrate inspect ~/.claude/projects/-work/SESSION.jsonl
```

Move a Claude session into Codex and resume it from the same project directory:

```bash
smigrate transfer SESSION_UUID --from claude --to codex --cwd "$PWD"
codex resume NEW_SESSION_UUID
```

Or find an older session by its native title/name first:

```bash
smigrate catalog refresh
smigrate catalog search "oauth refresh" --format claude
smigrate transfer --title "oauth refresh" --from claude --to pi
```

Search is case-insensitive and every word must match, in any order. It searches
native titles, names, and IDs—not conversation bodies. A few useful patterns:

```bash
# “Fix flaky PostgreSQL timeout” also matches this reversed keyword order.
smigrate catalog search "timeout postgres"

# Find a release conversation among archived Codex sessions from this month.
smigrate catalog search "release notes" --format codex \
  --lifecycle archived --since 2026-08-01T00:00:00Z

# Opt in to matching a project directory when the title is vague.
smigrate catalog search "checkout api" --include-paths
```

`catalog refresh` is exhaustive inside the default, environment-selected,
registered, and explicitly discovered roots. It does not crawl your whole disk.

## Give it to your coding agent

Choose the route on the [project website](https://session-migrate.github.io/),
or replace the three bracketed values yourself:

> Follow https://session-migrate.github.io/llms.txt to migrate a session from
> `[SOURCE]` to `[TARGET]`. Session: `[UUID OR TITLE]`

The linked procedure is sandbox-tested with both Claude Code and Codex. See the
[agent workflow and verification](https://github.com/xhluca/session-migrate/blob/main/docs/coding-agent-instruction.md).

## Compatibility

<table>
  <tr>
    <td align="center" width="16.6%"><a href="https://github.com/anthropics/claude-code"><img src="https://session-migrate.github.io/agents/claude-code.svg" width="64" height="64" alt="Claude Code logo"><br><strong>Claude Code</strong></a></td>
    <td align="center" width="16.6%"><a href="https://github.com/openai/codex"><img src="https://session-migrate.github.io/agents/codex.svg" width="64" height="64" alt="Codex logo"><br><strong>Codex</strong></a></td>
    <td align="center" width="16.6%"><a href="https://pi.dev"><img src="https://session-migrate.github.io/agents/pi.svg" width="64" height="64" alt="Pi logo"><br><strong>Pi</strong></a></td>
    <td align="center" width="16.6%"><a href="https://github.com/can1357/oh-my-pi"><img src="https://session-migrate.github.io/agents/oh-my-pi.svg" width="64" height="64" alt="Oh My Pi logo"><br><strong>Oh My Pi</strong></a></td>
    <td align="center" width="16.6%"><a href="https://github.com/anomalyco/opencode"><img src="https://session-migrate.github.io/agents/opencode.svg" width="64" height="64" alt="OpenCode logo"><br><strong>OpenCode</strong></a></td>
    <td align="center" width="16.6%"><a href="https://github.com/github/copilot-cli"><img src="https://session-migrate.github.io/agents/copilot.svg" width="64" height="64" alt="Copilot logo"><br><strong>Copilot</strong></a></td>
  </tr>
  <tr>
    <td align="center"><a href="https://developers.google.com/antigravity"><img src="https://session-migrate.github.io/agents/antigravity.svg" width="64" height="64" alt="Antigravity logo"><br><strong>Antigravity</strong></a></td>
    <td align="center"><a href="https://github.com/mistralai/mistral-vibe"><img src="https://session-migrate.github.io/agents/mistral-vibe.svg" width="64" height="64" alt="Mistral Vibe logo"><br><strong>Mistral Vibe</strong></a></td>
    <td align="center"><a href="https://dev.meta.ai/"><img src="https://session-migrate.github.io/agents/muse.svg" width="64" height="64" alt="Muse Code logo"><br><strong>Muse Code</strong></a></td>
    <td align="center"><a href="https://github.com/QwenLM/qwen-code"><img src="https://session-migrate.github.io/agents/qwen-code.svg" width="64" height="64" alt="Qwen Code logo"><br><strong>Qwen Code</strong></a></td>
    <td align="center"><a href="https://github.com/MoonshotAI/kimi-cli"><img src="https://session-migrate.github.io/agents/kimi-code.svg" width="64" height="64" alt="Kimi Code logo"><br><strong>Kimi Code</strong></a></td>
    <td align="center"><a href="https://github.com/xai-org/grok-build"><img src="https://session-migrate.github.io/agents/grok.svg" width="64" height="64" alt="Grok logo"><br><strong>Grok</strong></a></td>
  </tr>
  <tr>
    <td align="center"><a href="https://github.com/Kilo-Org/kilocode"><img src="https://session-migrate.github.io/agents/kilo-code.svg" width="64" height="64" alt="Kilo Code logo"><br><strong>Kilo Code</strong></a></td>
    <td align="center"><a href="https://github.com/All-Hands-AI/OpenHands"><img src="https://session-migrate.github.io/agents/openhands.svg" width="64" height="64" alt="OpenHands logo"><br><strong>OpenHands</strong></a></td>
    <td align="center"><a href="https://github.com/NousResearch/hermes-agent"><img src="https://session-migrate.github.io/agents/hermes-agent.png" width="64" height="64" alt="Hermes Agent logo"><br><strong>Hermes Agent</strong></a></td>
    <td align="center"><a href="https://github.com/mastra-ai/mastra/tree/main/mastracode"><img src="https://session-migrate.github.io/agents/mastracode.svg" width="64" height="64" alt="MastraCode logo"><br><strong>MastraCode</strong></a></td>
    <td align="center"><a href="https://github.com/CognitionAI/devin-cli"><img src="https://session-migrate.github.io/agents/devin.png" width="64" height="64" alt="Devin logo"><br><strong>Devin</strong></a></td>
    <td align="center"><a href="https://cursor.com/cli"><img src="https://session-migrate.github.io/agents/cursor.svg" width="64" height="64" alt="Cursor logo"><br><strong>Cursor*</strong></a></td>
  </tr>
</table>

Every listed format can be a source or target: 324 ordered routes, including
same-format portable rewrites. Cursor deliberately transfers only ordered
user/assistant text and is pinned to one exact Linux build; it is not a
vendor-supported import API. Same-format migration creates a new independent
session—it is not a byte-for-byte clone or a live sync. The matrix is exercised
from [one exact native-produced source per harness](https://github.com/xhluca/session-migrate/blob/main/docs/native-corpus-validation.md).

## What survives

| Session data | Result | Notes |
| --- | :---: | --- |
| User and assistant messages | ✓ | Preserved in order on every route |
| Tool calls and results | ✓ / partial | Preserved when both adapters support the native shape |
| Images | ✓ / partial | Supported image blocks move; other media is format-dependent |
| Compaction summaries | ✓ / partial | Recreated where the target has a portable equivalent |
| Readable reasoning | Vibe-only portable rewrite | Vibe keeps its explicit readable field when rewritten to Vibe; other/private/signed traces never move |
| Session name, ID, and picker entry | ✓ / partial | The target gets a new native identity; OpenHands derives its picker title from the first user turn |
| Branches, forks, and subagents | Not flattened | Cataloged separately where detectable; migrate the parent session |
| Private or signed thinking | No | Model/provider-bound traces are deliberately omitted |
| Auth, hooks, policies, MCP, and runtime config | No | These remain with the source client |

Every omission or transformation is counted in a content-free migration
manifest. The source session is never modified. Cursor intentionally accepts
text only. See
[Pi thinking traces](https://github.com/xhluca/session-migrate/blob/main/docs/pi-thinking-traces.md).

## How it works

```text
native session → validated event timeline → native target → resume
```

Each reader projects a versioned native transcript into a small ordered model.
Each writer then emits only structures verified against the target CLI. This is
session migration, not text export: the target receives a discoverable,
resumable native session.

## More

- [CLI reference](https://github.com/xhluca/session-migrate/blob/main/docs/cli-reference.md)
- [Coding-agent instruction](https://github.com/xhluca/session-migrate/blob/main/docs/coding-agent-instruction.md)
- [Session catalog](https://github.com/xhluca/session-migrate/blob/main/docs/session-catalog.md)
- [Compatibility details](https://github.com/xhluca/session-migrate/blob/main/docs/format-compatibility.md)
- [Troubleshooting](https://github.com/xhluca/session-migrate/blob/main/docs/troubleshooting.md)
- [Format research and validation](https://github.com/xhluca/session-migrate/blob/main/docs/validation-report.md)
- [Native-source 18×18 test evidence](https://github.com/xhluca/session-migrate/blob/main/docs/native-corpus-validation.md)
- [Credential-free exact-client CI](https://github.com/xhluca/session-migrate/blob/main/docs/credential-free-native-testing.md)
- [Data handling and architecture](https://github.com/xhluca/session-migrate/blob/main/docs/architecture.md)
- [Antigravity format](https://github.com/xhluca/session-migrate/blob/main/docs/antigravity-format.md)
- [Oh My Pi format](https://github.com/xhluca/session-migrate/blob/main/docs/omp-format.md)
- [Experimental Cursor format](https://github.com/xhluca/session-migrate/blob/main/docs/cursor-format.md)
- [Mistral Vibe format](https://github.com/xhluca/session-migrate/blob/main/docs/vibe-format.md)
- [Muse, Qwen Code, and Kimi Code formats](https://github.com/xhluca/session-migrate/blob/main/docs/muse-qwen-kimi-formats.md)
- [Grok, Kilo Code, and OpenHands formats](https://github.com/xhluca/session-migrate/blob/main/docs/grok-kilo-openhands-formats.md)
- [Hermes Agent format](https://github.com/xhluca/session-migrate/blob/main/docs/hermes-format.md)
- [MastraCode format](https://github.com/xhluca/session-migrate/blob/main/docs/mastracode-format.md)
- [Devin CLI format](https://github.com/xhluca/session-migrate/blob/main/docs/devin-format.md)

The Antigravity and Cursor adapters are clean-room, unofficial, and
version-pinned. Their independently observed formats are published separately:
[Antigravity research](https://github.com/xhluca/antigravity-session-interoperability)
and [Cursor research](https://github.com/xhluca/cursor-session-interoperability).

The demo above uses real native casts recorded with the same tmux + asciinema
approach as agent-talk. Claude diagnoses a boundary bug in a small project; the
migrated session is reopened in Pi, which applies the proposed patch and runs
the regression test. It shows the source TUI, the migration command, the shared
history, and the continued target session. The website plays those casts
directly in JavaScript; the README animation is rendered from that same scene.
The same source session is also continued in
[Claude → Codex](https://raw.githubusercontent.com/xhluca/session-migrate/main/docs/assets/demo-codex.gif).
[Watch the larger-text Pi video](https://github.com/xhluca/session-migrate/raw/main/docs/assets/demo-pi.mp4),
[watch the larger-text Codex video](https://github.com/xhluca/session-migrate/raw/main/docs/assets/demo-codex.mp4),
or [reproduce both](https://github.com/xhluca/session-migrate/blob/main/scripts/render-demo.sh).
The recorder uses disposable credential copies only to drive the native clients;
the published assets contain only the controlled demo project and omit account
status.

<details>
<summary>Compare Claude Code → Pi inside the native clients</summary>

| Before · Claude Code TUI | After · Pi TUI |
| --- | --- |
| ![Claude Code native session before migration](https://raw.githubusercontent.com/xhluca/session-migrate/main/docs/assets/demo-before.png) | ![Migrated session continued inside the native Pi TUI](https://raw.githubusercontent.com/xhluca/session-migrate/main/docs/assets/demo-after-pi.png) |

</details>

<details>
<summary>Compare Claude Code → Codex inside the native clients</summary>

| Before · Claude Code TUI | After · Codex TUI |
| --- | --- |
| ![Claude Code native session before migration](https://raw.githubusercontent.com/xhluca/session-migrate/main/docs/assets/demo-before.png) | ![Migrated session continued inside the native Codex TUI](https://raw.githubusercontent.com/xhluca/session-migrate/main/docs/assets/demo-after-codex.png) |

</details>

<a href="https://session-migrate.github.io/">
  <img src="https://raw.githubusercontent.com/xhluca/session-migrate/main/docs/assets/landing.png" alt="session-migrate project website" width="860">
</a>

## Contributing

```bash
git clone https://github.com/xhluca/session-migrate.git
cd session-migrate
uv sync --dev
uv run pytest
```

See [the development guide](https://github.com/xhluca/session-migrate/blob/main/docs/development.md)
before changing a native
adapter. New formats need sanitized fixtures and a real native-resume oracle.

## License

[MIT](https://github.com/xhluca/session-migrate/blob/main/LICENSE)
