# Session catalog and additional targets

## Working contract

- [ ] Index every session under each configured Claude and Codex root.
- [ ] Include active, archived, project-scoped, nested sidechain/subagent,
  malformed, duplicate, and explicitly unsupported sessions with a status.
- [ ] Search metadata only by default: agent, native/session name or title,
  UUID, working directory, source root/path, lifecycle, version, and time.
- [ ] Support default/configured roots plus repeatable explicit roots; do not
  crawl an entire disk or index conversation bodies implicitly.
- [ ] Keep the catalog private, incremental, resilient to stale/deleted files,
  and usable without mutating native Claude or Codex state.
- [ ] Add name/title, UUID, cwd/path, agent/type, status, and time filters.

## Additional target research and implementation

- [ ] Pin the installed Pi, OpenCode, and Cursor CLI versions and native stores.
- [ ] Prove each native import/resume path with isolated, credential-free data.
- [ ] Implement Claude export only for targets whose replay/import semantics are
  understood and validated; reject incomplete/proprietary paths explicitly.
- [ ] Preserve portable messages, tool linkage, supported media, order, and
  loss accounting through target reparse and native resume.

## Verification and documentation

- [ ] Add sanitized unit, adversarial, incremental-index, and CLI tests.
- [ ] Exercise the catalog against the complete accessible native corpus.
- [ ] Run native resume/import matrices for every enabled new target.
- [ ] Document schema research, privacy, exact limitations, recovery, and CLI.
- [ ] Commit and push bounded milestones to the private repository.

Started on 2026-08-18. The safe defaults are metadata-only indexing, explicit
roots rather than disk crawling, and fail-closed conversion for any target that
has not passed native import/resume verification.
