# Changelog

All notable changes to the private `agent-session-bridge` project are recorded
here. Native format compatibility is documented separately in
[`docs/format-compatibility.md`](docs/format-compatibility.md).

## Unreleased

No entries yet.

## 0.2.0 - 2026-08-18

- Add a private, incremental, multi-root Claude/Codex session catalog with
  metadata-only title/name and UUID search.
- Inventory Claude nested sidechains, Codex active/archive rollouts,
  duplicates, missing/corrupt files, and known unsupported history modes
  without misrepresenting them as convertible.
- Add bounded project-local root discovery, persistent explicit roots, and
  exact `transfer --catalog-id` selection.
- Add native Pi 0.80.6 conversion/import and OpenCode 1.17.20 public-bundle
  conversion plus official-CLI import. Keep Cursor import explicitly
  unsupported until it exposes a supported import contract.
- Add a separate target-format model and explicit `transfer --to`, while
  preserving legacy Claude-to-Codex and Codex-to-Claude defaults.
- Harden portable image validation, OpenCode native ID/time ordering,
  empty-history rejection, target collision handling, private temporary files,
  and content-free post-import manifests.
- Pass exhaustive 102-session Claude-to-Pi and Claude-to-OpenCode semantic and
  loss-accounting validation, 20-session actual-content review per target, and
  isolated native smoke tests on 10 real conversions per target.

## 0.1.2 - 2026-08-18

- Added complete CLI, troubleshooting, and development/release documentation.
- Clarified sensitive-data handling, manifest semantics, evidence provenance,
  Docker reproducibility, and the v0.1.x support boundary.
- Count non-object and malformed known image/reference blocks in structured
  tool results instead of silently skipping them, in both conversion
  directions.
- Expand quoted `~` consistently in CLI paths and report the actual converted
  output/manifest locations.
- Passed 58 tests, the official two-way pinned native-resume probe, package
  builds, isolated-wheel installation, and a focused 56,758-rollout structured
  output audit.

## 0.1.1 - 2026-08-18

- Completed exhaustive supported-corpus semantic validation and a 60-session
  content-level manual audit.
- Added retained-record warnings for orphan and duplicate tool linkage.
- Added duplicate-call/result regression coverage in both directions.
- Published the sanitized thorough-validation report and pinned native-resume
  evidence.

## 0.1.0 - 2026-08-18

- Released the bidirectional Claude Code/Codex CLI conversion baseline.
- Added `inspect`, `convert`, `import`, and UUID-based `transfer` workflows.
- Added bounded parsing, content-free manifests, atomic no-clobber private
  writes, changing-source detection, and direct native discovery.
- Validated explicit native resume against Claude Code 2.1.209 and Codex CLI
  0.144.4 in the pinned Docker image.
