# Coding-agent instruction

You can delegate a migration to a coding agent instead of translating the CLI
steps yourself. Replace the three bracketed values, then paste this instruction
into Claude Code, Codex, or another coding agent with shell access:

> Use session-migrate from https://github.com/xhluca/session-migrate to migrate
> my local coding-agent conversation. Read the current README.md and
> docs/cli-reference.md in that repository, install the released tool in an
> isolated way, refresh its catalog across the available default roots, and
> locate session `[SESSION UUID OR DISTINCTIVE TITLE]`. Show only content-free
> metadata; if the search is ambiguous, stop and ask me which result to use.
> Migrate it from `[SOURCE AGENT]` to `[TARGET AGENT]` now. Before the dry-run,
> generate one fresh target UUID yourself and pass it with `--session-id` to
> both the dry-run and the apply command; do not let either command generate a
> different UUID. Stop if their session IDs or resolved target paths differ.
> Review and summarize every warning or counted transformation before applying,
> then give me the exact native resume command and required working directory.
> Never print transcript bodies or credentials, never overwrite an existing
> target, and do not modify the source session.

For example, replace the placeholders with `authentication refactor`, `Claude`,
and `Codex`. A UUID is safer than a title when you already know it.

## What the agent should do

The instruction deliberately describes the outcome rather than hard-coding a
command sequence. The agent should use the current released CLI and its current
documentation, but the observable workflow is:

1. Install `session-migrate` in an isolated tool environment.
2. Refresh the catalog and select exactly one source session.
3. Show structural metadata only, without message, tool, image, or credential
   bodies.
4. Generate a fresh target UUID and pass the same `--session-id` to both
   `transfer --dry-run` and the eventual apply command.
5. Explain every nonzero `dropped_events` entry and every warning.
6. Apply with the same target UUID, preserving the source.
7. Return the target CLI's native resume command and its required CWD.

The target is a new independent session. The instruction does not authorize the
agent to overwrite an artifact, edit the source, copy authentication material,
or suppress a warning.

## Sandbox verification

The exact instruction above is exercised against synthetic native sessions in
isolated Docker homes before release. The release gate runs it through both
Claude Code and Codex, in opposite migration directions, and independently
checks that:

- catalog discovery selects the requested native UUID;
- a fixed target UUID is shared by dry-run and apply;
- warnings and counted transformations match the written manifest;
- the generated target reparses as the advertised native format;
- tool linkage and supported media survive the fixture conversion;
- the source SHA-256 is unchanged; and
- the reported native resume command selects the generated target.

The sandbox uses synthetic transcripts. Authentication is available only to the
agent process in its private temporary home, is never printed, and is removed
with the sandbox after the aggregate checks.
