# Antigravity CLI and GitHub Copilot CLI targets

## Native-contract research

- [ ] Pin exact official Antigravity CLI and GitHub Copilot CLI builds, hashes,
  schemas, stores, discovery rules, and resume behavior.
- [ ] Prove a minimum cold-resumable native session using isolated homes and no
  pre-existing indexes, databases, or credentials.
- [ ] Record the supported authentication matrix. Never reinterpret or copy a
  Codex OAuth token into a Google/Gemini or GitHub credential store.
- [ ] Fail closed if an official, stable native import/resume contract cannot be
  proved for a target version.

## Implementation

- [ ] Add pure parsers, serializers, byte validators, collision checks, and
  target installation/import paths for each proven target.
- [ ] Preserve portable message order and roles, linked tool calls/results,
  supported images, compaction semantics, timestamps, and explicit loss counts.
- [ ] Integrate target choices, generated identifiers, CLI options, private
  manifests, dry-run behavior, and exact-version gates without regressing the
  Claude, Codex, Pi, OpenCode, or Cursor contracts.

## Native and trajectory verification

- [ ] Exercise deterministic local-provider multi-turn trajectories and cold
  resume through the exact target runtime.
- [ ] Launch each actual TUI in a PTY, write several synthetic steps in an
  isolated workspace, resume the imported session, and verify append/order.
- [ ] When a target-specific supported credential is already available, run a
  real authenticated trajectory without printing, persisting, or committing
  credential material. Otherwise document the exact authentication blocker.
- [ ] Convert all accessible top-level Claude sessions to every enabled target,
  validate and reparse them, compare portable semantics and loss counters, and
  manually inspect a private stratified sample including images and tools.

## Documentation and release

- [ ] Document native formats, credential boundaries, privacy, compatibility,
  failures, CLI examples, validation revisions, and reproducible test commands.
- [ ] Run Ruff, the full test suite, native tests, corpus validator, link checks,
  build and isolated-wheel smoke; commit and push bounded milestones.

Started on 2026-08-18 after the Pi/OpenCode v0.2.0 release. The implementation
must use official target authentication and replay/import surfaces; deterministic
local providers are validation oracles, not substitutes for authenticated tests.
