#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "$script_dir/.." && pwd)"
asset_dir="$repo_dir/docs/assets"
pages_dir="${SESSION_MIGRATE_PAGES_DIR:-$repo_dir/../session-migrate.github.io}"

if [[ "${MIGRATE_NATIVE_CAPTURE_AUTH:-}" != "1" ]]; then
  echo "Set MIGRATE_NATIVE_CAPTURE_AUTH=1 to authorize disposable local OAuth copies." >&2
  exit 2
fi

mkdir -p "$asset_dir"
uv run python \
  "$script_dir/capture-native-tui-demo.py" \
  "$asset_dir"

if [[ ! -f "$pages_dir/index.html" ]]; then
  echo "Static Pages checkout not found at $pages_dir." >&2
  echo "Set SESSION_MIGRATE_PAGES_DIR, then rerun to publish the direct video assets." >&2
  exit 2
fi

for asset in \
  demo-claude.cast demo-claude-hold.cast demo-pi.cast demo-codex.cast \
  demo-before.png demo-after-pi.png demo-after-codex.png
do
  install -m 0644 "$asset_dir/$asset" "$pages_dir/assets/$asset"
done

uv run --script \
  "$script_dir/render-video-demo.py" \
  "$asset_dir" \
  "$asset_dir"

if [[ -d "$repo_dir/website/public" ]]; then
  for asset in \
    demo.gif demo.mp4 demo.png demo-before.png demo-after.png \
    demo-pi.gif demo-pi.mp4 demo-after-pi.png \
    demo-codex.gif demo-codex.mp4 demo-after-codex.png \
    demo-claude.cast demo-claude-hold.cast demo-pi.cast demo-codex.cast
  do
    install -m 0644 "$asset_dir/$asset" "$repo_dir/website/public/$asset"
  done
fi

for asset in demo.gif demo.mp4 demo-pi.gif demo-pi.mp4 demo-codex.gif demo-codex.mp4; do
  install -m 0644 "$asset_dir/$asset" "$pages_dir/assets/$asset"
done

echo "Recorded native Claude, Pi, and Codex casts and rendered release assets."
