# Agent Session Bridge

`session-bridge` reads local Claude Code, Codex CLI, and Pi conversations. It
can hand any of those sources to every different supported target: Claude,
Codex, Pi, OpenCode, or GitHub Copilot CLI. Same-format conversion is rejected.
Antigravity and Cursor are explicit, fail-closed targets because their CLIs do
not expose documented transcript import contracts.

The project is intentionally research-first. Native agents treat their persisted
session schema as an implementation detail, so adapters are version-aware,
conversion is non-destructive, and unsupported data is reported rather than
silently discarded.

> **Sensitive data:** the bridge does not redact, secret-scan, or encrypt
> conversation content. Supported message text, tool arguments/results, and
> images are copied into the target transcript and can contain embedded tokens
> or other secrets. Treat the generated JSONL as sensitively as the source.
> External CLI credential/configuration stores are never copied.

## Install

The current release targets Linux. Python 3.11 or newer and
[uv](https://docs.astral.sh/uv/) are the only prerequisites. From an authorized
checkout of this private repository:

```console
uv tool install .
session-bridge --version
```

For development, use `uv run session-bridge` instead of installing the tool.

## Use

```console
# Identify a session and print a content-free structural summary.
session-bridge inspect PATH

# Write a converted native session plus a conversion manifest.
session-bridge convert PATH --to codex --output OUTPUT --cwd /target/project

# Safely install a converted session into the target home.
session-bridge import PATH --to codex --cwd /target/project --dry-run
session-bridge import PATH --to codex --cwd /target/project

# Install into Pi's native v3 JSONL store.
session-bridge import PATH --to pi --cwd /target/project

# Ask the pinned OpenCode CLI to import its public bundle format.
session-bridge import PATH --to opencode --cwd /target/project \
  --target-cli ~/.opencode/bin/opencode

# Install a GitHub Copilot CLI 1.0.70 event log and workspace sidecar.
session-bridge import PATH --to copilot --cwd /target/project

# Find a native source session by UUID and install it into a target agent.
session-bridge transfer SOURCE_UUID --from claude \
  --source-cwd /source/project --cwd /target/project --dry-run
session-bridge transfer SOURCE_UUID --from claude \
  --source-cwd /source/project --cwd /target/project
session-bridge transfer SOURCE_UUID --from codex --cwd /target/project
session-bridge transfer SOURCE_UUID --from claude --to opencode \
  --source-cwd /source/project --cwd /target/project
session-bridge transfer SOURCE_UUID --from codex --to copilot \
  --cwd /target/project
session-bridge transfer SOURCE_UUID --from pi --to claude \
  --source-cwd /source/project --cwd /target/project

# Index and search every session in all configured agent homes.
session-bridge catalog refresh
session-bridge catalog search "native session title"

# Select one exact result, including across duplicate UUIDs.
session-bridge transfer --catalog-id CATALOG_ID --dry-run
```

The recommended sequence is: inspect; dry-run with a fixed fresh target UUID;
review `warnings` and `dropped_events`; apply the identical command without
`--dry-run`; then resume explicitly from the recorded target CWD. An import
creates an independent target conversation—it does not move, synchronize, or
continuously mirror the source.

`PATH` is a Claude project JSONL, Codex rollout JSONL, or Pi v3 session JSONL. Format detection is
automatic; `--format` can override it. Claude, Codex, Pi, and Copilot imports
use `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `PI_CODING_AGENT_DIR`, `COPILOT_HOME`, or
their normal defaults unless `--home` is given. OpenCode import deliberately
rejects `--home`: it invokes the official pinned CLI and lets OpenCode use its
normal HOME/XDG configuration. The JSON result contains the new session ID and
exact installed location.

`transfer` is the shortest end-to-end workflow. It searches the selected
source home by UUID. Claude and Codex preserve their historical opposite-agent
default; a Pi source requires explicit `--to`. The command then performs the
same validated, no-clobber import. `--to pi|opencode|copilot`
selects an additional target explicitly. `--source-home` overrides the source CLI
home; `--home` overrides the target CLI home. Claude UUIDs can collide across
encoded project directories, so pass `--source-cwd` when the source project is
known. Ambiguous lookup fails instead of guessing. Codex lookup covers active
and archived rollouts. Pi lookup covers every v3 session below its workspace
buckets and uses `--source-cwd` to disambiguate duplicate UUIDs.

The [native session catalog](docs/session-catalog.md) covers more than this
single-home UUID lookup: it indexes all files in every automatic, registered,
or explicitly bounded project-local root, including archived, nested,
duplicate, malformed, and unsupported sessions. It searches native
names/titles and UUIDs without indexing conversation bodies. Use repeatable
`catalog refresh --claude-root`, `--codex-root`, `--pi-root`, or
`--discover-under` options
for non-global stores.

Run the target CLI from the same `--cwd` used during import:

```console
cd /target/project
codex resume NEW_UUID

cd /target/project
claude --resume NEW_UUID

pi --session /path/printed/by/session-bridge

opencode run "follow-up" --session ses_NEW_ID --pure

cd /target/project
copilot --resume NEW_UUID
```

The default is a fresh UUID. Supplying `--session-id UUID` is useful for
controlled automation but fails if the exact planned native or manifest path
already exists. It is not a global UUID scan across every target project/date
directory. Use an explicit `--cwd` when transferring between a host and
container, because both CLIs use the working directory for discovery or
filtering.

A dry run without `--session-id` generates a preview UUID; a later real run
intentionally generates another. Reusing an explicit fresh UUID usually pins
the planned native path when the source timestamp is valid, but each run
regenerates target structural IDs and hashes. A missing/invalid source timestamp
can also change a Codex date path. Always review the applied JSON result.

See the [specification](docs/specification.md),
[CLI reference](docs/cli-reference.md),
[native session catalog](docs/session-catalog.md),
[troubleshooting guide](docs/troubleshooting.md),
[format compatibility matrix](docs/format-compatibility.md),
[additional native target contracts](docs/additional-target-formats.md),
[Copilot and Antigravity research](docs/copilot-antigravity-targets.md),
[architecture](docs/architecture.md), [Docker environment](docs/docker-environment.md),
[exploration log](docs/exploration-log.md), and
[thorough validation report](docs/validation-report.md). Contributors should
also read the [development and release guide](docs/development.md). Release
changes are summarized in the [changelog](CHANGELOG.md).

## Safety contract

- Source sessions are never modified.
- A source that changes during detection, parsing, or hashing is rejected; retry
  after the active CLI finishes appending.
- Existing target sessions are never overwritten implicitly.
- Import defaults to a newly generated session ID.
- A dry run reports every planned path and compatibility warning. For OpenCode,
  it creates no imported session or bridge artifact, but the official collision
  probe may initialize normal OpenCode cache/database/lock files under XDG.
- Bridge-owned installed files are written atomically with restrictive
  permissions; OpenCode database mutation belongs only to its official importer.
- Raw conversation content is never printed by `inspect`.
- Unrepresentable source data is inventoried in a sidecar conversion manifest.

`inspect`, success JSON, and manifests omit conversation bodies but include
paths, CWDs, UUIDs, timestamps, counts, and hashes. They are content-free, not
metadata-free. Newly created files use mode `0600` and newly created
directories use `0700`; permissions of existing directories are not changed.

Codex paginated/fork lineage fails closed. For Codex replacement-history
compaction, the provider-encrypted state cannot be decoded by Claude; the
bridge retains the visible pre-compaction transcript and reports the expansion
as lossy. System/developer prompts, private reasoning, sidechains, standalone
attachments, audio, runtime policy, and external authentication/configuration
stores are not replayed. Embedded secrets in portable conversation content are
not detected or removed. Claude/Codex conversion can preserve remote HTTP(S)
image URLs, which the target may fetch later. Pi/OpenCode accept only validated
inline data images and count remote images as omitted; use self-contained base64
images for a portable offline transfer. Copilot stores supported inline images
as integrity-checked content-addressed assets. User-image provider replay is
proven, while tool-result image replay depends on provider/wire protocol and is
explicitly warned even though the exact native asset is retained.

The Claude/Codex compatibility baseline is the local `basic-claude-uv` image pinned
by image ID, with Claude Code `2.1.209` and Codex CLI `0.144.4`. Additional
Pi source and target support is pinned to `0.80.6`; other native targets are
pinned to OpenCode `1.17.20` and GitHub
Copilot CLI `1.0.70`. Newer
Claude/Codex source versions with legacy history and Pi v3 sources are accepted best-effort with
an explicit warning. Automatic OpenCode import requires the exact pinned
binary; file-only conversion can emit an explicitly warned metadata override.
Antigravity CLI `1.1.14` and Cursor Agent CLI are recognized but fail closed
until they publish supported import contracts.
Native session formats are implementation details, so rerun the integration
test after any CLI changes.

## Development

```console
uv sync --dev
uv run pytest
uv run ruff check .
scripts/verify-native-resume.sh
uv run python scripts/validate-core-target-native.py --help
uv run python scripts/validate-authenticated-pi-tui.py --help
```

Inputs are capped at 64 MiB per record, 256 MiB per file, and 100,000 records
by default. This repository does not commit real session files. Test fixtures
are synthetic and stripped of credentials, personal paths, and private
conversation content.

The Docker integration test mounts no credentials, disables networking, and
considers resume successful only when each CLI selects the imported UUID and
appends local records to that exact JSONL.
