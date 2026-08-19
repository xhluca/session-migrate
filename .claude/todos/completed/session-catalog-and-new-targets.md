# Session catalog and additional targets

## Working contract

- [x] Index every session under each configured Claude and Codex root.
- [x] Include active, archived, project-scoped, nested sidechain/subagent,
  malformed, duplicate, and explicitly unsupported sessions with a status.
- [x] Search metadata only by default: agent, native/session name or title,
  UUID, working directory, source root/path, lifecycle, version, and time.
- [x] Support default/configured roots plus repeatable explicit roots; do not
  crawl an entire disk or index conversation bodies implicitly.
- [x] Keep the catalog private, incremental, resilient to stale/deleted files,
  and usable without mutating native Claude or Codex state.
- [x] Add name/title, UUID, cwd/path, agent/type, status, and time filters.

## Additional target research and implementation

- [x] Pin the installed Pi, OpenCode, and Cursor CLI versions and native stores.
- [x] Prove each native import/resume path with isolated, credential-free data.
- [x] Implement Claude export only for targets whose replay/import semantics are
  understood and validated; reject incomplete/proprietary paths explicitly.
- [x] Preserve portable messages, tool linkage, supported media, order, and
  loss accounting through target reparse and native resume.

## Verification and documentation

- [x] Add sanitized unit, adversarial, incremental-index, and CLI tests.
- [x] Exercise the catalog against the complete accessible native corpus.
- [x] Run native resume/import matrices for every enabled new target.
- [x] Document schema research, privacy, exact limitations, recovery, and CLI.
- [x] Commit and push bounded milestones to the dedicated repository.

Started on 2026-08-18. The safe defaults are metadata-only indexing, explicit
roots rather than disk crawling, and fail-closed conversion for any target that
has not passed native import/resume verification.
