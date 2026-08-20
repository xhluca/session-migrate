#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "$script_dir/.." && pwd)"
asset_dir="$repo_dir/docs/assets"
cast_path="$asset_dir/demo.cast"
render_dir="$(mktemp -d)"

cleanup() {
  find "$render_dir" -depth -delete
}
trap cleanup EXIT

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
grep -q 'BEFORE' "$cast_path"
grep -q 'CONVERT' "$cast_path"
grep -q 'AFTER' "$cast_path"

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

for phase in before after; do
  asciinema rec \
    --quiet \
    --overwrite \
    --cols 100 \
    --rows 28 \
    --idle-time-limit 3 \
    --command "env DEMO_PHASE=$phase SESSION_MIGRATE_BIN=$repo_dir/.venv/bin/smigrate $script_dir/demo-trajectory.sh" \
    "$render_dir/$phase.cast"

  agg \
    --quiet \
    --theme github-dark \
    --font-size 14 \
    --fps-cap 10 \
    --last-frame-duration 1 \
    "$render_dir/$phase.cast" \
    "$render_dir/$phase.gif"

  frame_count="$(ffprobe -v error -count_frames -select_streams v:0 \
    -show_entries stream=nb_read_frames -of csv=p=0 "$render_dir/$phase.gif")"
  last_frame=$((frame_count - 1))

  ffmpeg -hide_banner -loglevel error -y \
    -i "$render_dir/$phase.gif" \
    -vf "select=eq(n\,$last_frame)" \
    -frames:v 1 \
    "$asset_dir/demo-$phase.png"
done

python3 - "$cast_path" "$render_dir/before.cast" "$render_dir/after.cast" <<'PY'
import json
import re
import sys
from pathlib import Path

ansi = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

def output(path):
    events = [json.loads(line) for line in Path(path).read_text().splitlines()[1:]]
    return events, ansi.sub("", "".join(event[2] for event in events if event[1] == "o")).replace("\r", "")

main_events, main_output = output(sys.argv[1])
phase_times = {}
for event in main_events:
    if event[1] != "o":
        continue
    for phase in ("BEFORE", "CONVERT", "AFTER"):
        if phase not in phase_times and phase in event[2]:
            phase_times[phase] = event[0]
assert list(phase_times) == ["BEFORE", "CONVERT", "AFTER"], phase_times
assert phase_times["BEFORE"] < phase_times["CONVERT"] < phase_times["AFTER"]
assert "session playback 2.5×" in main_output
assert "real terminal usage · 1×" in main_output

semantic = re.compile(r"^(you|image|assistant|tool|result|summary)\s+(.+)$")
screens = []
for path in sys.argv[2:]:
    _, text = output(path)
    screens.append([match.groups() for line in text.splitlines() if (match := semantic.match(line))])
assert screens[0] == screens[1], (screens[0], screens[1])
assert len(screens[0]) == 10
print(
    "demo verified:",
    "before/conversion/after at",
    ", ".join(f"{phase_times[name]:.2f}s" for name in ("BEFORE", "CONVERT", "AFTER")),
    "with 10 exact semantic rows",
)
PY

install -m 0644 "$asset_dir/demo-before.png" "$asset_dir/demo.png"

if [ -d "$repo_dir/website/public" ]; then
  install -m 0644 \
    "$asset_dir/demo.gif" \
    "$asset_dir/demo.mp4" \
    "$asset_dir/demo.png" \
    "$asset_dir/demo-before.png" \
    "$asset_dir/demo-after.png" \
    "$repo_dir/website/public/"
fi
