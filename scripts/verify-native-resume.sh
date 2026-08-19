#!/usr/bin/env bash
set -euo pipefail

# This test never mounts credentials and disables container networking. A resume
# is successful when the target CLI selects the imported UUID and appends local
# turn/error records before authentication necessarily fails.

image_name=${MIGRATE_TEST_IMAGE:-basic-claude-uv:latest}
expected_image_id=sha256:8f170f660813ac358f347fa8a3580139972f3ea7a9fb087834f1da44669d9392
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "$script_dir/.." && pwd)
state_dir=$(mktemp -d /tmp/session-migrate-native.XXXXXX)
host_user=$(id -u):$(id -g)
trap 'rm -rf -- "$state_dir"' EXIT

actual_image_id=$(docker image inspect "$image_name" --format '{{.Id}}')
if [[ $actual_image_id != "$expected_image_id" && ${MIGRATE_ALLOW_IMAGE_DRIFT:-0} != 1 ]]; then
  echo "unexpected image ID: $actual_image_id" >&2
  echo "expected pinned image: $expected_image_id" >&2
  echo "set MIGRATE_ALLOW_IMAGE_DRIFT=1 to test another build explicitly" >&2
  exit 1
fi

docker run --rm --network none \
  --user "$host_user" \
  -v "$repo_root:/project:ro" \
  -v "$state_dir:/state" \
  -w /work \
  "$actual_image_id" bash -lc '
set -eu
mkdir -p \
  /state/codex /state/claude /state/codex-home /state/claude-home \
  /state/source-claude/projects/-work \
  /state/source-codex/sessions/2026/08/17
cp /project/tests/fixtures/claude-2.1.209/basic.jsonl \
  /state/source-claude/projects/-work/10000000-0000-4000-8000-000000000000.jsonl
cp /project/tests/fixtures/codex-0.144.4/basic.jsonl \
  /state/source-codex/sessions/2026/08/17/rollout-fixture-20000000-0000-4000-8000-000000000000.jsonl
PYTHONPATH=/project/src python3 -m session_migrate transfer \
  10000000-0000-4000-8000-000000000000 \
  --from claude --source-home /state/source-claude --source-cwd /work \
  --home /state/codex \
  --session-id 30000000-0000-4000-8000-000000000000 --cwd /work \
  > /state/codex-import.json
PYTHONPATH=/project/src python3 -m session_migrate transfer \
  20000000-0000-4000-8000-000000000000 \
  --from codex --source-home /state/source-codex \
  --home /state/claude \
  --session-id 40000000-0000-4000-8000-000000000000 --cwd /work \
  > /state/claude-import.json
'

docker run --rm --network none \
  --user "$host_user" \
  -v "$state_dir:/state" \
  -w /work \
  "$actual_image_id" bash -lc '
set -eu
session_id=30000000-0000-4000-8000-000000000000
rollout=/state/codex/sessions/2026/08/17/rollout-2026-08-17T12-00-00-${session_id}.jsonl
before=$(stat -c %s "$rollout")
before_hash=$(head -c "$before" "$rollout" | sha256sum | cut -d " " -f 1)
HOME=/state/codex-home CODEX_HOME=/state/codex timeout 20s \
  codex exec resume --skip-git-repo-check "$session_id" \
  "Synthetic offline native-resume validation probe." \
  > /state/codex-resume.log 2>&1 || true
after=$(stat -c %s "$rollout")
after_prefix_hash=$(head -c "$before" "$rollout" | sha256sum | cut -d " " -f 1)
grep -q "session id: $session_id" /state/codex-resume.log
test "$after" -gt "$before"
test "$after_prefix_hash" = "$before_hash"
test -s /state/codex/state_5.sqlite
printf "Codex native resume: PASS (%s -> %s bytes)\n" "$before" "$after"
'

docker run --rm --network none \
  --user "$host_user" \
  -v "$state_dir:/state" \
  -w /work \
  "$actual_image_id" bash -lc '
set -eu
session_id=40000000-0000-4000-8000-000000000000
transcript=/state/claude/projects/-work/${session_id}.jsonl
before=$(stat -c %s "$transcript")
before_hash=$(head -c "$before" "$transcript" | sha256sum | cut -d " " -f 1)
HOME=/state/claude-home CLAUDE_CONFIG_DIR=/state/claude timeout 20s \
  claude -p --resume "$session_id" \
  "Synthetic offline native-resume validation probe." \
  > /state/claude-resume.log 2>&1 || true
after=$(stat -c %s "$transcript")
after_prefix_hash=$(head -c "$before" "$transcript" | sha256sum | cut -d " " -f 1)
grep -q "Not logged in" /state/claude-resume.log
test "$after" -gt "$before"
test "$after_prefix_hash" = "$before_hash"
python3 -c "import json, sys; data = open(sys.argv[1], \"rb\").read(); before = int(sys.argv[2]); head = [json.loads(line) for line in data[:before].splitlines()]; tail = [json.loads(line) for line in data[before:].splitlines()]; leaf = next(record[\"uuid\"] for record in reversed(head) if record.get(\"type\") in {\"user\", \"assistant\"}); appended = next(record for record in tail if record.get(\"type\") == \"user\"); nodes = {record[\"uuid\"]: record for record in tail if record.get(\"uuid\")}; walk = lambda cursor: cursor if cursor not in nodes else walk(nodes[cursor].get(\"parentUuid\")); assert walk(appended.get(\"parentUuid\")) == leaf" "$transcript" "$before"
printf "Claude native resume: PASS (%s -> %s bytes)\n" "$before" "$after"
'
