<h1 align="center"><a href="https://session-migrate.github.io/">↝ session-migrate</a></h1>

<p align="center"><strong>Carry the conversation forward.</strong></p>

<p align="center">
  <a href="https://pypi.org/project/session-migrate/"><img src="https://img.shields.io/pypi/v/session-migrate?style=flat-square&color=b8f94a&label=PyPI" alt="PyPI version"></a>
  <a href="https://pypi.org/project/session-migrate/"><img src="https://img.shields.io/pypi/pyversions/session-migrate?style=flat-square" alt="Python versions"></a>
  <a href="https://github.com/xhluca/session-migrate/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-8dbdff?style=flat-square" alt="MIT license"></a>
  <a href="https://session-migrate.github.io/"><img src="https://img.shields.io/badge/website-live-b8f94a?style=flat-square" alt="Project website"></a>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/xhluca/session-migrate/main/docs/assets/demo.gif" alt="session-migrate converting a Claude Code session into a native Codex session" width="860">
</p>

<p align="center">
  Move local coding-agent sessions between <strong>Claude Code</strong>,
  <strong>Codex</strong>, <strong>Pi</strong>, <strong>OpenCode</strong>,
  <strong>GitHub Copilot CLI</strong>, <strong>Antigravity CLI</strong>, and
  <strong>Cursor Agent</strong>.
</p>

## Install

```bash
uv tool install session-migrate
```

No `uv`? Use the standalone installer:

```bash
curl -LsSf https://raw.githubusercontent.com/xhluca/session-migrate/main/install.sh | sh
```

`pipx install session-migrate` works too. Python 3.11+ and Linux are currently
supported. The full command is `session-migrate`; `smigrate` is the shorthand.
Already on 0.5.x? Run `uv tool upgrade session-migrate`.

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

Or find an older session by title first:

```bash
smigrate catalog refresh
smigrate catalog search "authentication refactor"
smigrate transfer --catalog-id RESULT_ID --to pi
```

`catalog refresh` is exhaustive inside the default, environment-selected,
registered, and explicitly discovered roots. It does not crawl your whole disk.

## Compatibility

| Source ↓ / Target → | Claude | Codex | Pi | OpenCode | Copilot | Antigravity | Cursor* |
| --- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| Claude Code | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | T |
| Codex | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | T |
| Pi | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | T |
| OpenCode | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | T |
| Copilot CLI | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | T |
| Antigravity CLI | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | T |
| Cursor Agent* | T | T | T | T | T | T | T |

✓ means a native portable rewrite. `T` means ordered user/assistant text only.
Cursor support is experimental and pinned to one exact Linux build; it is not a
vendor-supported import API. Same-format migration creates a new independent
session—it is not a byte-for-byte clone or a live sync.

## What survives

- User and assistant messages, in order, on every route
- Tool calls/results, supported images, and portable summaries where the target supports them
- A new native identity, target discovery/picker metadata, and resume state

Anything target-specific is counted in a content-free migration manifest. The
source session is never modified. Cursor intentionally accepts text only;
private/model-bound thinking is not migrated. See
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
- [Session catalog](https://github.com/xhluca/session-migrate/blob/main/docs/session-catalog.md)
- [Compatibility details](https://github.com/xhluca/session-migrate/blob/main/docs/format-compatibility.md)
- [Troubleshooting](https://github.com/xhluca/session-migrate/blob/main/docs/troubleshooting.md)
- [Format research and validation](https://github.com/xhluca/session-migrate/blob/main/docs/validation-report.md)
- [Data handling and architecture](https://github.com/xhluca/session-migrate/blob/main/docs/architecture.md)
- [Antigravity format](https://github.com/xhluca/session-migrate/blob/main/docs/antigravity-format.md)
- [Experimental Cursor format](https://github.com/xhluca/session-migrate/blob/main/docs/cursor-format.md)

The Antigravity and Cursor adapters are clean-room, unofficial, and
version-pinned. Their independently observed formats are published separately:
[Antigravity research](https://github.com/xhluca/antigravity-session-interoperability)
and [Cursor research](https://github.com/xhluca/cursor-session-interoperability).

The demo above shows the native Claude conversation before migration, runs the
real conversion and inspection commands, then shows the equivalent native Codex
history. Conversation playback is accelerated to 2.5×; terminal usage remains
at 1×. It uses synthetic, credential-free fixtures. [Watch the MP4](https://github.com/xhluca/session-migrate/raw/main/docs/assets/demo.mp4)
or [reproduce it](https://github.com/xhluca/session-migrate/blob/main/scripts/render-demo.sh).

<details>
<summary>Compare the native session before and after</summary>

| Claude Code source | Codex target |
| --- | --- |
| ![Claude Code session before migration](https://raw.githubusercontent.com/xhluca/session-migrate/main/docs/assets/demo-before.png) | ![Codex session after migration](https://raw.githubusercontent.com/xhluca/session-migrate/main/docs/assets/demo-after.png) |

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
