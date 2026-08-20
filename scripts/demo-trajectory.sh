#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "$script_dir/.." && pwd)"
smigrate_bin="${SESSION_MIGRATE_BIN:-$repo_dir/.venv/bin/smigrate}"
demo_phase="${DEMO_PHASE:-full}"
demo_dir="$(mktemp -d)"

cleanup() {
  find "$demo_dir" -depth -delete
}
trap cleanup EXIT

cp "$repo_dir/tests/fixtures/claude-2.1.209/basic.jsonl" "$demo_dir/claude-session.jsonl"
mkdir "$demo_dir/project"
cd "$demo_dir"

type_text() {
  local text="$1"
  local delay="${2:-0.018}"
  while IFS= read -r -n1 character; do
    printf '%s' "$character"
    if [ "$delay" != 0 ]; then sleep "$delay"; fi
  done <<<"$text"
}

type_command() {
  printf '\033[38;5;112m❯\033[0m '
  type_text "$1" 0.018
  printf '\n'
  sleep 0.35
}

pause() {
  sleep "${1:-0.65}"
}

ensure_target() {
  if [ ! -f codex-session.jsonl ]; then
    "$smigrate_bin" convert claude-session.jsonl \
      --to codex \
      --output codex-session.jsonl \
      --cwd "$demo_dir/project" \
      --session-id 12345678-1234-4234-8234-123456789abc \
      >/dev/null
  fi
}

native_lines() {
  python3 - "$1" "$2" <<'PY'
import json
import sys
from pathlib import Path

source_format, source_path = sys.argv[1:]
records = [json.loads(line) for line in Path(source_path).read_text().splitlines() if line.strip()]

def clipped(value, limit=72):
    value = " ".join(str(value).split())
    return value if len(value) <= limit else value[: limit - 1] + "…"

if source_format == "claude":
    for record in records:
        if record.get("isCompactSummary"):
            message = record.get("message", {})
            content = message.get("content", "") if isinstance(message, dict) else ""
            print(f"SUMMARY|{clipped(content)}")
            continue
        record_type = record.get("type")
        if record_type not in {"user", "assistant"} or record.get("isMeta"):
            continue
        message = record.get("message", {})
        content = message.get("content", []) if isinstance(message, dict) else []
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                label = "YOU" if record_type == "user" else "ASSISTANT"
                print(f"{label}|{clipped(block.get('text', ''))}")
            elif kind == "image":
                media_type = block.get("source", {}).get("media_type", "image")
                print(f"IMAGE|inline {media_type} preserved")
            elif kind == "tool_use":
                tool_input = block.get("input", {})
                argument = tool_input.get("file_path", "…") if isinstance(tool_input, dict) else "…"
                print(f"TOOL|{block.get('name', 'tool')}({clipped(argument, 48)})")
            elif kind == "tool_result":
                result = block.get("content", [])
                if isinstance(result, str):
                    result_text = result
                else:
                    result_text = next((part.get("text", "") for part in result if isinstance(part, dict) and part.get("type") == "text"), "")
                print(f"RESULT|{clipped(result_text)}")
                if isinstance(result, list) and any(isinstance(part, dict) and part.get("type") == "image" for part in result):
                    print("IMAGE|tool-result image/png preserved")
else:
    for record in records:
        record_type = record.get("type")
        payload = record.get("payload", {})
        if not isinstance(payload, dict):
            continue
        if record_type == "compacted":
            print(f"SUMMARY|{clipped(payload.get('message', ''))}")
            continue
        if record_type != "response_item":
            continue
        item_type = payload.get("type")
        if item_type == "message":
            label = "YOU" if payload.get("role") == "user" else "ASSISTANT"
            for block in payload.get("content", []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") in {"input_text", "output_text"}:
                    print(f"{label}|{clipped(block.get('text', ''))}")
                elif block.get("type") == "input_image":
                    print("IMAGE|inline image/png preserved")
        elif item_type == "function_call":
            arguments = payload.get("arguments", "{}")
            try:
                decoded = json.loads(arguments)
            except (TypeError, json.JSONDecodeError):
                decoded = {}
            argument = decoded.get("file_path", "…") if isinstance(decoded, dict) else "…"
            print(f"TOOL|{payload.get('name', 'tool')}({clipped(argument, 48)})")
        elif item_type == "function_call_output":
            output = payload.get("output", "")
            if isinstance(output, str):
                result_text = output
                has_image = False
            else:
                result_text = next((part.get("text", "") for part in output if isinstance(part, dict) and part.get("type") in {"input_text", "output_text"}), "")
                has_image = any(isinstance(part, dict) and part.get("type") == "input_image" for part in output)
            print(f"RESULT|{clipped(result_text)}")
            if has_image:
                print("IMAGE|tool-result image/png preserved")
PY
}

print_native_line() {
  local label="$1"
  local value="$2"
  local delay="$3"
  local color=250
  case "$label" in
    YOU) color=117 ;;
    ASSISTANT) color=112 ;;
    TOOL|RESULT) color=215 ;;
    IMAGE|SUMMARY) color=141 ;;
  esac
  printf '\033[38;5;%sm%-10s\033[0m ' "$color" "${label,,}"
  type_text "$value" "$delay"
  printf '\n'
  if [ "$delay" != 0 ]; then sleep 0.055; fi
}

show_native() {
  local source_format="$1"
  local source_path="$2"
  local heading="$3"
  local delay=0.0072
  if [ "$demo_phase" != full ]; then delay=0; fi

  printf '\033[2J\033[H'
  printf '\033[1;37m%s\033[0m\n' "$heading"
  printf '\033[2m%s native transcript · session playback \033[1m2.5×\033[0m\n\n' "$source_format"
  while IFS='|' read -r label value; do
    print_native_line "$label" "$value" "$delay"
  done < <(native_lines "$source_format" "$source_path")
}

show_conversion() {
  printf '\033[2J\033[H'
  printf '\033[1;37mCONVERT\033[0m  \033[2m— real terminal usage · \033[1m1×\033[0m\n\n'
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
}

case "$demo_phase" in
  before)
    show_native claude claude-session.jsonl "BEFORE  ·  Claude Code"
    pause 1.5
    ;;
  after)
    ensure_target
    show_native codex codex-session.jsonl "AFTER   ·  Codex"
    printf '\n\033[38;5;112m✓\033[0m Same messages · tool linkage · summary · images\n'
    pause 1.5
    ;;
  full)
    show_native claude claude-session.jsonl "BEFORE  ·  Claude Code"
    pause 1.4
    show_conversion
    show_native codex codex-session.jsonl "AFTER   ·  Codex"
    printf '\n\033[38;5;112m✓\033[0m Same messages · tool linkage · summary · images\n'
    printf '\033[38;5;112m✓\033[0m Continue with \033[1mcodex resume 12345678…\033[0m\n'
    pause 2.6
    ;;
  *)
    printf 'unknown DEMO_PHASE: %s\n' "$demo_phase" >&2
    exit 2
    ;;
esac
