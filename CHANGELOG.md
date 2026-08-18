# Changelog

All notable changes to the private `agent-session-bridge` project are recorded
here. Native format compatibility is documented separately in
[`docs/format-compatibility.md`](docs/format-compatibility.md).

## Unreleased

- Added complete CLI, troubleshooting, and development/release documentation.
- Clarified sensitive-data handling, manifest semantics, evidence provenance,
  Docker reproducibility, and the v0.1.x support boundary.
- Count non-object Codex structured tool-result blocks instead of silently
  skipping them.
- Expand quoted `~` consistently in CLI paths and report the actual converted
  output/manifest locations.

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

