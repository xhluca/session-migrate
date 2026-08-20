# Changelog

All notable changes to `session-migrate` are recorded
here. Native format compatibility is documented separately in
[`docs/format-compatibility.md`](docs/format-compatibility.md).

## Unreleased

- Rebuild the public demo around an explicit native Claude before-state,
  real-speed conversion/inspection commands, and an equivalent native Codex
  after-state; accelerate only conversation playback to 2.5×.
- Add collapsed, directly comparable before/after terminal screenshots beneath
  the landing-page video.

## 0.5.2 - 2026-08-20

- Move the canonical project website to the anonymous public GitHub Pages
  address at `https://session-migrate.github.io/`.
- Publish the landing page source in the public
  `session-migrate/session-migrate.github.io` repository and update package,
  repository, README, social-card, robots, and sitemap metadata.

## 0.5.1 - 2026-08-20

- Publish `session-migrate` to PyPI with MIT licensing, complete package
  metadata, and verified `uv tool install` and `pipx` entry points.
- Replace the research-heavy README with a concise open-source overview,
  animated real-CLI demo, practical examples, and a compact compatibility map.
- Add a standalone one-line shell installer with isolated-venv fallback and
  no-clobber handling for pre-existing command files.
- Add and deploy a responsive landing page with an animated migration pipeline,
  real trajectory video, social card, copyable installs, and native-target
  matrix.

## 0.5.0 - 2026-08-19

- Rename the project, Python distribution, import package, executable, state
  directory, environment variables, generated metadata, and documentation to
  `session-migrate`; add the collision-checked `smigrate` executable alias.
- Make the rename intentionally breaking: no legacy executable, import,
  manifest path, state lookup, or compatibility shim remains. Manifests now
  use schema version 2 and the `migration_version` key.
- Prepare the repository for public access with a complete current-tree and
  reachable-history credential/content audit.
- Document how Pi 0.80.6 separates thinking-level configuration, visible
  reasoning summaries, and opaque same-provider replay signatures, with
  synthetic transformation and real-store offline-RPC evidence. The migrator
  continues to count rather than transfer private reasoning.

## 0.4.0 - 2026-08-18

- Promote Pi 0.80.6 v3 to a first-class detectable source with active-tree
  parsing, UUID/CWD discovery, direct transfer, catalog indexing/search, and
  exact conversion into Claude, Codex, OpenCode, and Copilot.
- Generalize the bounded real-session matrix to Claude, Codex, and Pi sources
  and every different supported target; preserve same-format rejection and the
  Antigravity/Cursor fail-closed capability boundary.
- Add real-source pinned native Claude/Codex resume oracles, generalized Pi and
  OpenCode native checks, and Codex/Pi-to-Copilot cold-resume/provider replay.
- Add a disposable actual Pi TUI trajectory that translates existing Codex
  OAuth only inside a private temporary home, completes two live turns, checks
  context recall and append-only persistence, and removes copied credentials.
- Fix Unicode JSON line-separator handling, bounded exhaustive-audit memory,
  OpenCode tool-result association accounting, Copilot source-text grouping,
  Copilot excess-result native linkage, and silent Pi nested tool-result and
  parent-lineage omissions.

## 0.3.0 - 2026-08-18

- Add GitHub Copilot CLI 1.0.70 conversion/import using its public local event
  schema, content-addressed images, exact UUID resume, and derived-index rebuild.
- Keep Antigravity CLI 1.1.14 explicitly fail closed after proving that its
  public CLI/SDK has no arbitrary transcript import despite successful actual
  two-turn TUI/runtime checks.
- Add exhaustive 102-session Copilot conversion/reparse/loss validation,
  10-session native cold-resume/provider-replay validation, and actual
  two-turn loopback plus authenticated Copilot TUI checks.
- Document the credential boundary: use target-supported login or BYOK only;
  never copy or reinterpret Codex OAuth as GitHub/Google/OpenAI credentials.

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
