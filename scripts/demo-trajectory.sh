#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "$script_dir/.." && pwd)"
smigrate_bin="${SESSION_MIGRATE_BIN:-$repo_dir/.venv/bin/smigrate}"
demo_dir="$(mktemp -d)"

cleanup() {
  find "$demo_dir" -depth -delete
}
trap cleanup EXIT

cp "$repo_dir/tests/fixtures/claude-2.1.209/basic.jsonl" "$demo_dir/claude-session.jsonl"
mkdir "$demo_dir/project"
cd "$demo_dir"

type_command() {
  local command_text="$1"
  printf '\033[38;5;112m❯\033[0m '
  while IFS= read -r -n1 character; do
    printf '%s' "$character"
    sleep 0.018
  done <<<"$command_text"
  printf '\n'
  sleep 0.35
}

pause() {
  sleep "${1:-0.65}"
}

printf '\033[2J\033[H'
printf '\033[1;37msession-migrate\033[0m  \033[2m— carry the conversation forward\033[0m\n\n'
pause 0.8

type_command "smigrate inspect claude-session.jsonl"
"$smigrate_bin" inspect claude-session.jsonl \
  | grep -E '^(format|records|session_id|tool_calls|tool_results|content_blocks):'
pause 1.1

printf '\n'
type_command "smigrate convert claude-session.jsonl --to codex --output codex-session.jsonl"
"$smigrate_bin" convert claude-session.jsonl \
  --to codex \
  --output codex-session.jsonl \
  --cwd "$demo_dir/project" \
  --session-id 12345678-1234-4234-8234-123456789abc \
  | python3 -c '
import json, sys
result = json.load(sys.stdin)
print("  {}  →  {}".format(result["source_format"], result["target_format"]))
print("  {} native records written".format(result["records"]))
print("  session {}".format(result["session_id"]))
print("  ✓ transcript + manifest ready")
'
pause 1.25

printf '\n'
type_command "smigrate inspect codex-session.jsonl"
"$smigrate_bin" inspect codex-session.jsonl \
  | grep -E '^(format|records|session_id|tool_calls|tool_results):'
pause 1.1

printf '\n\033[38;5;112m✓\033[0m Continue with \033[1mcodex resume 12345678…\033[0m\n'
pause 2.4
