# Codex and Pi full conversion matrix

## Source support

- [ ] Promote Pi v3 from a target verifier to a first-class source adapter.
- [ ] Detect and inspect Pi without confusing Claude/Codex markers.
- [ ] Preserve Pi active-tree order, messages, tools, images, compaction, model,
  provider, title, and explicit loss accounting for branches/runtime metadata.
- [ ] Add Pi UUID/CWD discovery, `transfer --from pi`, and multi-root catalog
  indexing/search without trusting a derived picker index.

## Matrix validation

- [ ] Validate every supported Codex rollout to Claude, Pi, OpenCode, and
  Copilot with exact portable semantics and independently computed loss counts.
- [ ] Validate every accessible Pi v3 session to Claude, Codex, OpenCode, and
  Copilot, including native cold resume and appended follow-up trajectories.
- [ ] Keep same-format conversion rejected and Antigravity/Cursor fail closed.
- [ ] Add synthetic branch, compaction, custom-message, media, tool-linkage,
  interrupted, malformed, mixed-format, discovery, and catalog regressions.

## Release

- [ ] Document the source matrix, native semantics, privacy, corpus methods,
  limitations, and exact commands.
- [ ] Run formatting/lint, full tests, native oracles, full corpus validation,
  link checks, builds, isolated-wheel smoke, Docker regression, and secret scan.
- [ ] Commit/push bounded milestones and tag the release.

Started on 2026-08-18 after v0.3.0. “All harnesses” means every target with a
proven import contract: Claude, Codex, Pi, OpenCode, and Copilot. Antigravity
and Cursor remain recognized capability errors, not fabricated imports.
