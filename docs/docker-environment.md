# Pinned Docker integration environment

This document records the Docker environment used to validate native Claude
Code and Codex session imports. It is an integration-test reference, not a
claim that either vendor guarantees its private on-disk format.

The observations below were reproduced on 2026-08-17. All resume probes used
synthetic transcripts, fixed synthetic UUIDs, no credentials, and
`--network none`. No message text or credential value from a real session was
captured in this repository.

## Image identity

The tested image is the local `basic-claude-uv:latest` build from
`../agent-talk-extras/docker/basic-claude-uv/Dockerfile`.

That sibling directory is not part of this repository and was not itself a Git
repository at inspection time. A fresh clone of `session-migrate`
therefore cannot reproduce the environment unless the local pinned image or
equivalent private sibling files are supplied separately. The inspected helper
inputs had these SHA-256 digests:

| Sibling file | SHA-256 |
| --- | --- |
| `Dockerfile` | `82c8d70c477739334e9e6dd02d976c00e8cb4b4572641742120aa28b3a859e5e` |
| `run.sh` | `3e25180329a39db48462c226de48df6d044d4f532709e9cfe1eb16a53859fb42` |
| `compose.yaml` | `b6712784f7808e0e06d4cf08fe6eb3a3ac818645e8ea1d4e5bc0a82ca639ef32` |

Those hashes identify the inspected files, but rebuilding still cannot recreate
the image deterministically because its base/uv images, npm packages, and
installer downloads are mutable. The image ID remains the integration pin.

| Property | Observed value |
| --- | --- |
| Local image ID | `sha256:8f170f660813ac358f347fa8a3580139972f3ea7a9fb087834f1da44669d9392` |
| Created | `2026-07-14T15:29:50.880294884-04:00` |
| Platform | `linux/amd64` |
| Size | 2,155,735,311 bytes |
| Container user | root (Docker config leaves `User` empty) |
| Working directory | `/work` |
| Default command | `bash` |
| Repository digests | none (`RepoDigests` was `[]`) |

The full image ID, rather than `basic-claude-uv:latest`, is the reproducibility
pin. The tag is mutable and the image has no pullable registry digest. In
particular, `basic-claude-uv@sha256:...` cannot be pulled from a registry; use
the local image ID directly after confirming it is present.

Reproduce the metadata check without starting a container:

```bash
docker image inspect basic-claude-uv:latest \
  --format '{{.Id}} {{json .RepoDigests}} {{json .Created}} {{.Os}}/{{.Architecture}} {{.Size}} {{json .Config.User}} {{json .Config.WorkingDir}} {{json .Config.Cmd}}'
```

The source Dockerfile starts from `node:22-bookworm-slim`, copies `uv` and
`uvx` from the mutable `ghcr.io/astral-sh/uv:latest` image, and installs the
Claude Code and Codex npm packages without version pins. The local image is
therefore stable only while addressed by its image ID; rebuilding the same
Dockerfile can produce a materially different test environment.

## Tool versions and locations

The pinned image reported:

| Tool | Location/version |
| --- | --- |
| Claude Code | `/usr/local/bin/claude`, `2.1.209 (Claude Code)` |
| Codex CLI | `/usr/local/bin/codex`, `codex-cli 0.144.4` |
| Node.js | `v22.23.1` |
| npm | `10.9.8` |
| Python | `3.11.2` |
| uv | `0.11.28 (x86_64-unknown-linux-musl)` |

The version probe used the local image ID directly:

```bash
docker run --rm --entrypoint sh \
  sha256:8f170f660813ac358f347fa8a3580139972f3ea7a9fb087834f1da44669d9392 \
  -c 'id; command -v claude; claude --version; command -v codex; codex --version; node --version; npm --version; python3 --version; uv --version'
```

## Session stores in the image

With the default root user and `/work` current directory, the relevant native
paths are:

| Agent | Default location | Override |
| --- | --- | --- |
| Claude Code | `/root/.claude/projects/-work/<session-uuid>.jsonl` | `CLAUDE_CONFIG_DIR` replaces `/root/.claude` |
| Codex | `/root/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<session-uuid>.jsonl` | `CODEX_HOME` replaces `/root/.codex` |

Claude derives the `-work` directory from the session working directory. Its
observed encoding replaces every non-ASCII-alphanumeric character with `-`;
different paths can therefore collide. The transcript filename and every
ordinary record's `sessionId` must agree for a conventional imported session.
The optional `sessions-index.json` is not required for explicit UUID resume.

Codex stores additional state under `CODEX_HOME`, notably `state_5.sqlite`,
logs, goals, memories, and shell snapshots. The SQLite database is an index and
derived state, not part of the portable transcript: an explicit UUID resume
can find a valid rollout by scanning the sessions tree and then repair/update
the index. The tested Codex version requires a custom `CODEX_HOME` directory to
exist before startup.

The migrator writes only the target JSONL and its content-free sidecar manifest.
It does not copy either agent's external credentials, configuration, database,
logs, memories, plugins, or shell snapshots. It does not redact portable
conversation content, so a credential embedded in a message or tool result is
copied and the target transcript must remain private.

## Authentication behavior

The Dockerfile itself installs the CLIs but does not configure authentication.
The sibling helper scripts configure Claude only:

- `docker/basic-claude-uv/run.sh` prefers a dedicated token supplied as
  `CLAUDE_CODE_OAUTH_TOKEN`. Otherwise it mounts the host Claude credential
  file read-only at `/host-creds.json`, copies it to a writable
  `/root/.claude/.credentials.json`, and sets mode `0600`.
- `docker/basic-claude-uv/compose.yaml` uses the same read-only staging path
  and writable in-container copy.
- Neither helper injects Codex authentication.

Do not bind-mount a refreshable credential file read-only at its final native
location. The supplied helpers intentionally stage and copy it because a CLI
may need to rotate or refresh credentials. The migrator verification does not
need authentication at all: it proves native selection and append behavior
before the expected offline/authentication failure.

## Persistence model and operational hazards

The stock `run.sh` uses `docker run --rm` and mounts no workspace or session
home. All new files under `/work`, `/root/.claude`, and `/root/.codex` disappear
when the container exits. The Compose service retains them only for that
container's lifetime; `docker compose down` removes the container, and the
configuration declares no persistent volume for agent state.

For real use, bind-mount an explicit workspace and explicit agent homes. Keep
authentication material separate from transcripts. For example, a caller can
mount a host directory at `/state`, set `CLAUDE_CONFIG_DIR=/state/claude` or
`CODEX_HOME=/state/codex`, and leave the credential paths independently
managed.

A concrete credential-free Codex example imports on the host using the CWD as
seen inside the container, then mounts the resulting home persistently:

```bash
install -d -m 0700 ./workspace ./state/codex
TARGET_UUID=11111111-1111-4111-8111-111111111111

session-migrate import SOURCE.jsonl --to codex \
  --home ./state/codex \
  --cwd /work \
  --session-id "$TARGET_UUID" \
  --dry-run

# After reviewing paths and warnings, apply and review the new JSON result.
session-migrate import SOURCE.jsonl --to codex \
  --home ./state/codex \
  --cwd /work \
  --session-id "$TARGET_UUID"

docker run --rm -it \
  --network none \
  -v "$PWD/workspace:/work" \
  -v "$PWD/state/codex:/state/codex" \
  -e CODEX_HOME=/state/codex \
  sha256:8f170f660813ac358f347fa8a3580139972f3ea7a9fb087834f1da44669d9392
```

This starts as the image's root user, so bind-mounted outputs are normally
root-owned. Supplying `--user` changes ownership but also changes HOME and tool
configuration assumptions; test that variant explicitly rather than assuming
it is equivalent. Mount authentication separately only when a real provider
turn is intended.

Other integration hazards observed in this image are:

- The container runs as root. Files written to a bind mount will be root-owned
  unless `--user "$(id -u):$(id -g)"` is supplied.
- Working-directory metadata matters. Claude's project-directory key is
  derived from the target cwd, and both CLIs use saved cwd metadata when
  resuming. Import with the cwd that will exist inside the container.
- Interactive pickers can apply recency or cwd filtering. Explicit UUID resume
  is the authoritative native-acceptance test.
- A failed offline or unauthenticated resume can still append local turn and
  error records. Run probes only against disposable copies.
- `CODEX_HOME` must be created before invoking Codex 0.144.4 with a custom
  home.
- The image lacks a registry digest and its build inputs are unpinned. Verify
  the image ID before treating a result as evidence for this environment.

## Native-resume verification

The repository's integration probe installs both synthetic fixtures into
isolated native source homes, discovers them by UUID through the `transfer`
command, and imports them into isolated target homes. It then invokes each
target CLI by explicit UUID. The probe checks
that the CLI selects the UUID, preserves the imported byte prefix, and appends
records to the generated transcript. The fixtures include text, an image,
structured tool output, and compaction.
Network access and credentials are deliberately absent, so a completed model
response is not part of the pass condition.

Run the pinned probe from the repository root:

```bash
./scripts/verify-native-resume.sh
```

The script first verifies that `basic-claude-uv:latest` resolves to the pinned
image ID. Testing another image requires an explicit opt-in:

```bash
MIGRATE_TEST_IMAGE=another-local-tag \
MIGRATE_ALLOW_IMAGE_DRIFT=1 \
./scripts/verify-native-resume.sh
```

Internally, the native commands under test are equivalent to:

```bash
# The directories and JSONL files have already been created by session-migrate.
mkdir -p /state/codex-home /state/claude-home

HOME=/state/codex-home CODEX_HOME=/state/codex \
  codex exec resume --skip-git-repo-check \
  30000000-0000-4000-8000-000000000000 \
  'Synthetic offline native-resume validation probe.'

HOME=/state/claude-home CLAUDE_CONFIG_DIR=/state/claude \
  claude -p --resume \
  40000000-0000-4000-8000-000000000000 \
  'Synthetic offline native-resume validation probe.'
```

The 2026-08-18 run produced:

```text
Codex native resume: PASS (3004 -> 9827 bytes)
Claude native resume: PASS (3689 -> 15712 bytes)
```

Those byte counts are evidence for the pinned fixtures and image, not stable
API expectations. The durable assertions are that the requested UUID was
selected, the old byte prefix remained unchanged, the existing converted file
grew, Codex created its SQLite index, and Claude's appended graph links back to
the imported leaf. This demonstrates that copying
a structurally valid JSONL into a fresh target session home is sufficient for
explicit native resume in the two pinned CLI versions; it does not imply that
all private record variants are losslessly interchangeable.
