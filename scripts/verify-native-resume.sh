#!/usr/bin/env bash
set -euo pipefail

# This test never mounts credentials and disables container networking. A resume
# is successful when the target CLI selects the imported UUID and appends local
# turn/error records before authentication necessarily fails.

image_name=${BRIDGE_TEST_IMAGE:-basic-claude-uv:latest}
expected_image_id=sha256:8f170f660813ac358f347fa8a3580139972f3ea7a9fb087834f1da44669d9392
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "$script_dir/.." && pwd)
state_dir=$(mktemp -d /tmp/session-bridge-native.XXXXXX)
host_user=$(id -u):$(id -g)
trap 'rm -rf -- "$state_dir"' EXIT

actual_image_id=$(docker image inspect "$image_name" --format '{{.Id}}')
if [[ $actual_image_id != "$expected_image_id" && ${BRIDGE_ALLOW_IMAGE_DRIFT:-0} != 1 ]]; then
  echo "unexpected image ID: $actual_image_id" >&2
  echo "expected pinned image: $expected_image_id" >&2
  echo "set BRIDGE_ALLOW_IMAGE_DRIFT=1 to test another build explicitly" >&2
  exit 1
fi

docker run --rm --network none \
  --user "$host_user" \
  -v "$repo_root:/bridge:ro" \
  -v "$state_dir:/state" \
  -w /work \
  "$image_name" bash -lc '
set -eu
mkdir -p /state/codex /state/claude /state/codex-home /state/claude-home
PYTHONPATH=/bridge/src python3 -m session_bridge import \
  /bridge/tests/fixtures/claude-2.1.209/basic.jsonl \
  --to codex --home /state/codex \
  --session-id 30000000-0000-4000-8000-000000000000 --cwd /work \
  > /state/codex-import.json
PYTHONPATH=/bridge/src python3 -m session_bridge import \
  /bridge/tests/fixtures/codex-0.144.4/basic.jsonl \
  --to claude --home /state/claude \
  --session-id 40000000-0000-4000-8000-000000000000 --cwd /work \
  > /state/claude-import.json
'

docker run --rm --network none \
  --user "$host_user" \
  -v "$state_dir:/state" \
  -w /work \
  "$image_name" bash -lc '
set -u
session_id=30000000-0000-4000-8000-000000000000
rollout=/state/codex/sessions/2026/08/17/rollout-2026-08-17T12-00-00-${session_id}.jsonl
before=$(stat -c %s "$rollout")
HOME=/state/codex-home CODEX_HOME=/state/codex timeout 20s \
  codex exec resume --skip-git-repo-check "$session_id" \
  "Synthetic offline native-resume validation probe." \
  > /state/codex-resume.log 2>&1 || true
after=$(stat -c %s "$rollout")
grep -q "session id: $session_id" /state/codex-resume.log
test "$after" -gt "$before"
printf "Codex native resume: PASS (%s -> %s bytes)\n" "$before" "$after"
'

docker run --rm --network none \
  --user "$host_user" \
  -v "$state_dir:/state" \
  -w /work \
  "$image_name" bash -lc '
set -u
session_id=40000000-0000-4000-8000-000000000000
transcript=/state/claude/projects/-work/${session_id}.jsonl
before=$(stat -c %s "$transcript")
HOME=/state/claude-home CLAUDE_CONFIG_DIR=/state/claude timeout 20s \
  claude -p --resume "$session_id" \
  "Synthetic offline native-resume validation probe." \
  > /state/claude-resume.log 2>&1 || true
after=$(stat -c %s "$transcript")
grep -q "Not logged in" /state/claude-resume.log
test "$after" -gt "$before"
printf "Claude native resume: PASS (%s -> %s bytes)\n" "$before" "$after"
'
