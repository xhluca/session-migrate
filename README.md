# Agent Session Bridge

`session-bridge` converts local conversations between Claude Code and Codex CLI
session formats so a conversation can continue in the other agent.

The project is intentionally research-first. Both tools treat their persisted
session schema as an implementation detail, so adapters are version-aware,
conversion is non-destructive, and unsupported data is reported rather than
silently discarded.

## Install

Python 3.11 or newer and
[uv](https://docs.astral.sh/uv/) are the only prerequisites:

```console
uv tool install .
session-bridge --version
```

For development, use `uv run session-bridge` instead of installing the tool.

## Use

```console
# Identify a session and print a content-free structural summary.
session-bridge inspect PATH

# Write a converted native session plus a conversion manifest.
session-bridge convert PATH --to codex --output OUTPUT --cwd /target/project

# Safely install a converted session into the target home.
session-bridge import PATH --to codex --cwd /target/project --dry-run
session-bridge import PATH --to codex --cwd /target/project
```

`PATH` is a Claude project JSONL or Codex rollout JSONL. Format detection is
automatic; `--format` can override it. Import uses `CLAUDE_CONFIG_DIR`,
`CODEX_HOME`, or the normal `~/.claude`/`~/.codex` default unless `--home` is
given. The JSON result contains the new session UUID and exact installed path.

Run the target CLI from the same `--cwd` used during import:

```console
cd /target/project
codex resume NEW_UUID

cd /target/project
claude --resume NEW_UUID
```

The default is a fresh UUID. Supplying `--session-id UUID` is useful for
controlled automation but fails if that native target already exists. Use an
explicit `--cwd` when transferring between a host and container, because both
CLIs use the working directory for discovery or filtering.

A dry run without `--session-id` generates a fresh preview UUID; a later real
run will intentionally generate another. Pass the preview UUID explicitly if
an automation needs the planned and applied paths to be identical.

See the [specification](docs/specification.md),
[format compatibility matrix](docs/format-compatibility.md),
[architecture](docs/architecture.md), [Docker environment](docs/docker-environment.md),
and [exploration log](docs/exploration-log.md).

## Safety contract

- Source sessions are never modified.
- Existing target sessions are never overwritten implicitly.
- Import defaults to a newly generated session ID.
- A dry run reports every planned path and compatibility warning.
- Installed files are written atomically and restrictive permissions are used.
- Raw conversation content is never printed by `inspect`.
- Unrepresentable source data is inventoried in a sidecar conversion manifest.

Codex paginated/fork lineage and replacement-history compaction fail closed in
v0.1. System/developer prompts, private reasoning, sidechains, standalone
attachments, audio, runtime policy, and credentials are not replayed. Remote
HTTP(S) image URLs are preserved but may be fetched by the target CLI when the
session resumes; use self-contained base64 images for an offline transfer.

The initial compatibility baseline is the local `basic-claude-uv` image pinned
by image ID, with Claude Code `2.1.209` and Codex CLI `0.144.4`. Newer source
versions with legacy history are accepted best-effort with an explicit warning.
Native session formats are implementation details, so rerun the integration
test after either CLI changes.

## Development

```console
uv sync --dev
uv run pytest
uv run ruff check .
scripts/verify-native-resume.sh
```

This repository does not commit real session files. Test fixtures are synthetic
and stripped of credentials, personal paths, and private conversation content.

The Docker integration test mounts no credentials, disables networking, and
considers resume successful only when each CLI selects the imported UUID and
appends local records to that exact JSONL.
