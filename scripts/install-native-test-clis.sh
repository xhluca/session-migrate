#!/usr/bin/env bash
# Install the exact Linux x86_64 clients used by the credential-free native CI gate.

set -euo pipefail

native_root="${1:?usage: install-native-test-clis.sh DIRECTORY [CLIENT ...]}"
shift
requested=" $* "
install_all=0
if [[ $# -eq 0 ]]; then
  install_all=1
fi
mkdir -p "$native_root"
native_root="$(cd "$native_root" && pwd)"
env_file="$native_root/session-migrate-native.env"
: >"$env_file"

record() {
  local name="$1" value="$2"
  printf '%s=%s\n' "$name" "$value" >>"$env_file"
  if [[ -n "${GITHUB_ENV:-}" ]]; then
    printf '%s=%s\n' "$name" "$value" >>"$GITHUB_ENV"
  fi
}

verify() {
  local expected="$1" path="$2"
  printf '%s  %s\n' "$expected" "$path" | sha256sum --check --status
}

download() {
  local url="$1" expected="$2" target="$3"
  mkdir -p "$(dirname "$target")"
  curl --fail --location --silent --show-error --retry 3 --output "$target" "$url"
  verify "$expected" "$target"
  chmod 0755 "$target"
}

npm_cli() {
  local directory="$1" package="$2" variable="$3" relative_binary="$4"
  local prefix="$native_root/npm/$directory"
  mkdir -p "$prefix"
  npm install --prefix "$prefix" --no-audit --no-fund --ignore-scripts=false "$package"
  record "$variable" "$prefix/$relative_binary"
}

want() {
  [[ "$install_all" == 1 || "$requested" == *" $1 "* ]]
}

if want claude; then
  npm_cli claude '@anthropic-ai/claude-code@2.1.209' \
    SESSION_MIGRATE_CLAUDE_BIN node_modules/.bin/claude
fi
if want codex; then
  npm_cli codex '@openai/codex@0.144.4' \
    SESSION_MIGRATE_CODEX_BIN \
    node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex
fi
if want copilot; then
  npm_cli copilot '@github/copilot@1.0.70' \
    SESSION_MIGRATE_COPILOT_BIN node_modules/@github/copilot-linux-x64/copilot
fi
if want kilo; then
  npm_cli kilo '@kilocode/cli@7.5.0' \
    SESSION_MIGRATE_KILO_BIN node_modules/@kilocode/cli-linux-x64/bin/kilo
fi
if want kimi; then
  npm_cli kimi '@moonshot-ai/kimi-code@0.38.0' \
    SESSION_MIGRATE_KIMI_BIN node_modules/.bin/kimi
fi
if want mastracode; then
  npm_cli mastracode 'mastracode@0.37.1' \
    SESSION_MIGRATE_MASTRACODE_BIN node_modules/.bin/mastracode
fi
if want opencode; then
  npm_cli opencode 'opencode-ai@1.17.20' \
    SESSION_MIGRATE_OPENCODE_BIN node_modules/.bin/opencode
fi
if want pi; then
  npm_cli pi '@earendil-works/pi-coding-agent@0.80.6' \
    SESSION_MIGRATE_PI_BIN node_modules/.bin/pi
fi
if want qwen; then
  npm_cli qwen '@qwen-code/qwen-code@0.22.1' \
    SESSION_MIGRATE_QWEN_BIN node_modules/.bin/qwen
fi

if want antigravity; then
  antigravity_archive="$native_root/antigravity/antigravity.tar.gz"
  mkdir -p "$(dirname "$antigravity_archive")"
  curl --fail --location --silent --show-error --retry 3 --output "$antigravity_archive" \
    'https://storage.googleapis.com/antigravity-public/antigravity-cli/1.1.16-6607970839166976/linux-x64/cli_linux_x64.tar.gz'
  tar -xzf "$antigravity_archive" -C "$native_root/antigravity"
  verify b233e6a4f38564a06a0d3220aa79f6a7c8f11da2b85fc8f0957f8a14d46e6cc9 \
    "$native_root/antigravity/antigravity"
  # Version 1.1.16 may replace its own executable even for ``--version``.
  # Keep the installation directory immutable; runtime state belongs in the
  # isolated HOME supplied by the tests.
  chmod 0555 "$native_root/antigravity"
  record SESSION_MIGRATE_ANTIGRAVITY_BIN "$native_root/antigravity/antigravity"
fi

if want cursor; then
  cursor_archive="$native_root/cursor/cursor.tar.gz"
  mkdir -p "$(dirname "$cursor_archive")"
  curl --fail --location --silent --show-error --retry 3 --output "$cursor_archive" \
    'https://downloads.cursor.com/lab/2026.03.20-44cb435/linux/x64/agent-cli-package.tar.gz'
  tar -xzf "$cursor_archive" -C "$native_root/cursor"
  cursor_dir="$native_root/cursor/dist-package"
  verify 8756ac4a808cc90b220416ac8743560aa473a94d6fe5911bb602c250c046c4a3 \
    "$cursor_dir/cursor-agent"
  verify a7961f327172fa9eecdf69d3941c86a5c2785103bebaf63183ad8e9522f3f620 \
    "$cursor_dir/index.js"
  verify 7226059f6a648d5a25a4e0ef1f2bee363879baecc2468aa3ade4c6e481b15423 \
    "$cursor_dir/891.index.js"
  verify e0e46d3a1c0667117303412647cafcbcefb1be7612493015ec8fd6b7440162a4 \
    "$cursor_dir/node"
  record SESSION_MIGRATE_CURSOR_BIN "$cursor_dir/cursor-agent"
  record SESSION_MIGRATE_RUN_CURSOR_NATIVE 1
fi

if want devin; then
  devin_archive="$native_root/devin/devin.tar.gz"
  mkdir -p "$(dirname "$devin_archive")"
  curl --fail --location --silent --show-error --retry 3 --output "$devin_archive" \
    'https://static.devin.ai/cli/3000.6.7/devin-3000.6.7-x86_64-unknown-linux.tar.gz'
  verify f88edacea692553910d72f275515bd0b52b5d271d55250981b0c41011142d27b \
    "$devin_archive"
  tar -xzf "$devin_archive" -C "$native_root/devin"
  verify 862623068229249a5ac5a560d876532a40bb53fe16049ab7e415ac5d6b8ae36d \
    "$native_root/devin/bin/devin"
  record SESSION_MIGRATE_DEVIN_BIN "$native_root/devin/bin/devin"
fi

if want grok; then
  download 'https://x.ai/cli/grok-1.0.5-linux-x86_64' \
    9ba87444e1819e8f6104adbbf4676a870c204380aa5c3e1c38a926c4ea677238 \
    "$native_root/grok/grok"
  record SESSION_MIGRATE_GROK_BIN "$native_root/grok/grok"
fi

if want omp; then
  download 'https://github.com/can1357/oh-my-pi/releases/download/v18.0.5/omp-linux-x64' \
    d5a322af241cebe2662b3b792ff29d3ea6e61364328e916c9429065f346391ed \
    "$native_root/omp/omp"
  record SESSION_MIGRATE_OMP_BIN "$native_root/omp/omp"
fi

if want openhands; then
  download 'https://github.com/OpenHands/OpenHands-CLI/releases/download/1.16.0/openhands-linux-x86_64' \
    cb04ee2da91c698733d5201c55cbc08d81dccc9d64b666275abf68a4e0c590e3 \
    "$native_root/openhands/openhands"
  record SESSION_MIGRATE_OPENHANDS_BIN "$native_root/openhands/openhands"
fi

if want muse; then
  download 'https://lookaside.facebook.com/lookaside/muse/download/?channel=muse&version=0.2.1-R1215.1&file=muse-x86-linux' \
    bfd8660b3a4fce67ab3287b0bd27ea64db1ee8472e8d7cb0f0f9aa8e083c9957 \
    "$native_root/muse/muse"
  record SESSION_MIGRATE_MUSE_BIN "$native_root/muse/muse"

  uv venv --python 3.13 "$native_root/muse-openrouter"
  uv pip install --python "$native_root/muse-openrouter/bin/python" \
    'muse-code-openrouter==0.3.2'
  record SESSION_MIGRATE_MUSE_OPENROUTER_BIN \
    "$native_root/muse-openrouter/bin/muse-openrouter"
fi

if want vibe; then
  uv venv --python 3.13 "$native_root/vibe"
  uv pip install --python "$native_root/vibe/bin/python" 'mistral-vibe==2.24.3'
  record SESSION_MIGRATE_VIBE_BIN "$native_root/vibe/bin/vibe"
fi

if want hermes; then
  hermes_archive="$native_root/hermes-agent-5fc308a7.tar.gz"
  if ! verify 8710e0017792e78369a7da3d96c16141f0787374285f1a6cf6f80d29b7b9ea2c \
    "$hermes_archive" 2>/dev/null; then
    curl --fail --location --silent --show-error --retry 3 --output "$hermes_archive" \
      'https://codeload.github.com/NousResearch/hermes-agent/tar.gz/5fc308a70719a83cccdbba4c0e39c23f5a8239d5'
  fi
  verify 8710e0017792e78369a7da3d96c16141f0787374285f1a6cf6f80d29b7b9ea2c \
    "$hermes_archive"
  mkdir -p "$native_root/hermes"
  tar -xzf "$hermes_archive" --strip-components=1 -C "$native_root/hermes"
  # Hermes' revision-3 lockfile uses options first supported by uv 0.10.
  uvx --from 'uv==0.10.9' uv sync \
    --directory "$native_root/hermes" --frozen --no-dev --python 3.13
  record SESSION_MIGRATE_HERMES_SOURCE "$native_root/hermes"
  record SESSION_MIGRATE_HERMES_BIN "$native_root/hermes/.venv/bin/hermes"
  record SESSION_MIGRATE_HERMES_REVISION 5fc308a70719a83cccdbba4c0e39c23f5a8239d5
fi

path_entries=()
if want pi; then
  path_entries+=("$native_root/npm/pi/node_modules/.bin")
fi
if want opencode; then
  path_entries+=("$native_root/npm/opencode/node_modules/.bin")
fi
if [[ ${#path_entries[@]} -gt 0 ]]; then
  native_path="$(IFS=:; printf '%s' "${path_entries[*]}")"
  record SESSION_MIGRATE_NATIVE_PATH "$native_path"
fi

printf 'Installed pinned native clients under %s\n' "$native_root"
printf 'Environment file: %s\n' "$env_file"
