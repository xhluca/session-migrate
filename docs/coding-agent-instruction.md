# Coding-agent instruction

You can delegate a migration to a coding agent instead of translating the CLI
steps yourself. Use the interactive route picker on the
[project website](https://session-migrate.github.io/), or replace the three
bracketed values before pasting this into a coding agent with shell access:

> Follow https://session-migrate.github.io/llms.txt to migrate a session from
> `[SOURCE]` to `[TARGET]`. Session: `[UUID OR TITLE]`

For example, replace the placeholders with `Claude`, `Codex`, and
`authentication refactor`. The session value stays last so it is easy to edit
after pasting. A UUID is safer than a title when you already know it.

The linked [`llms.txt`](../llms.txt) is the canonical agent procedure. It keeps
the user prompt short while retaining the discovery, privacy, dry-run,
fixed-UUID, no-overwrite, source-integrity, and native-resume requirements.

## What the agent should do

The instruction deliberately describes the outcome rather than hard-coding a
command sequence. The agent should use the current released CLI and its current
documentation, but the observable workflow is:

1. Install the released `session-migrate` package from PyPI in an isolated
   virtual environment; stop if it cannot be installed or verified.
2. Refresh the catalog and select exactly one source session.
3. Redirect raw discovery JSON into private temporary files and show only the
   structural allowlist—never titles, previews, source paths, message/tool/media
   bodies, or credentials.
4. Generate a fresh target UUID and pass the same `--session-id` to both
   `transfer --dry-run` and the eventual apply command.
5. Explain every nonzero `dropped_events` entry and every warning.
6. Apply with the same target UUID, preserving the source.
7. Return the target CLI's native resume command and its required CWD.

The target is a new independent session. The instruction does not authorize the
agent to overwrite an artifact, edit the source, copy authentication material,
suppress a warning, invoke the target CLI to manufacture a template, or
hand-write a native transcript/database.

## Sandbox verification

The exact instruction above is exercised against synthetic native sessions in
isolated Docker homes before release. The release gate runs it through both
Claude Code and Codex, in opposite migration directions, and independently
checks that:

- catalog discovery selects the requested native UUID;
- no prompt-derived title, preview, message, tool, or media body appears in the
  agent-visible log;
- a fixed target UUID is shared by dry-run and apply;
- warnings and counted transformations match the written manifest;
- the generated target reparses as the advertised native format;
- tool linkage and supported media survive the fixture conversion;
- the source SHA-256 is unchanged; and
- the reported native resume command selects the generated target.

The sandbox uses synthetic transcripts. Authentication is available only to the
agent process in its private temporary home, is never printed, and is removed
with the sandbox after the aggregate checks.
