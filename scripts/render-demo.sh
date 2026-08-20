#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "$script_dir/.." && pwd)"
asset_dir="$repo_dir/docs/assets"

if [[ "${MIGRATE_NATIVE_CAPTURE_AUTH:-}" != "1" ]]; then
  echo "Set MIGRATE_NATIVE_CAPTURE_AUTH=1 to authorize disposable local OAuth copies." >&2
  exit 2
fi

mkdir -p "$asset_dir"
uv run python \
  "$script_dir/capture-native-tui-demo.py" \
  "$asset_dir"

if [[ -d "$repo_dir/website/public" ]]; then
  for asset in \
    demo.gif demo.mp4 demo.png demo-before.png demo-after.png \
    demo-pi.gif demo-pi.mp4 demo-after-pi.png \
    demo-codex.gif demo-codex.mp4 demo-after-codex.png
  do
    install -m 0644 "$asset_dir/$asset" "$repo_dir/website/public/$asset"
  done
fi

echo "Rendered 1x tmux/asciinema Claude -> Pi and Claude -> Codex native demos."
