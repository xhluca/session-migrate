#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "$script_dir/.." && pwd)"
asset_dir="$repo_dir/docs/assets"
cast_path="$asset_dir/demo.cast"

mkdir -p "$asset_dir"
asciinema rec \
  --quiet \
  --overwrite \
  --cols 100 \
  --rows 28 \
  --idle-time-limit 3 \
  --command "env SESSION_MIGRATE_BIN=$repo_dir/.venv/bin/smigrate $script_dir/demo-trajectory.sh" \
  "$cast_path"

grep -q 'Continue with' "$cast_path"

agg \
  --quiet \
  --theme github-dark \
  --font-size 14 \
  --fps-cap 20 \
  --last-frame-duration 3 \
  "$cast_path" \
  "$asset_dir/demo.gif"

ffmpeg -hide_banner -loglevel error -y \
  -i "$asset_dir/demo.gif" \
  -movflags faststart \
  -pix_fmt yuv420p \
  -vf "fps=30,scale=trunc(iw/2)*2:trunc(ih/2)*2" \
  "$asset_dir/demo.mp4"

ffmpeg -hide_banner -loglevel error -y \
  -ss 7 \
  -i "$asset_dir/demo.gif" \
  -frames:v 1 \
  "$asset_dir/demo.png"

if [ -d "$repo_dir/website/public" ]; then
  install -m 0644 \
    "$asset_dir/demo.gif" \
    "$asset_dir/demo.mp4" \
    "$asset_dir/demo.png" \
    "$repo_dir/website/public/"
fi
