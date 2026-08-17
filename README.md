# Agent Session Bridge

`session-bridge` converts local conversations between Claude Code and Codex CLI
session formats so a conversation can continue in the other agent.

The project is intentionally research-first. Both tools treat their persisted
session schema as an implementation detail, so adapters are version-aware,
conversion is non-destructive, and unsupported data is reported rather than
silently discarded.

## Intended interface

```console
# Identify a session and print a content-free structural summary.
session-bridge inspect PATH

# Write a converted native session plus a conversion manifest.
session-bridge convert PATH --to codex --output OUTPUT

# Discover and safely install a converted session into the target home.
session-bridge import PATH --to claude --dry-run
session-bridge import PATH --to claude
```

The command interface is provisional while the native formats are being
validated. See [the specification](docs/specification.md) and
[exploration log](docs/exploration-log.md).

## Safety contract

- Source sessions are never modified.
- Existing target sessions are never overwritten implicitly.
- Import defaults to a newly generated session ID.
- A dry run reports every planned path and compatibility warning.
- Installed files are written atomically and restrictive permissions are used.
- Raw conversation content is never printed by `inspect`.
- Unrepresentable source data is inventoried in a sidecar conversion manifest.

## Development

```console
uv sync --dev
uv run pytest
uv run ruff check .
```

This repository does not commit real session files. Test fixtures are synthetic
and stripped of credentials, personal paths, and private conversation content.

