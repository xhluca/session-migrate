#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""Render native terminal casts into a direct MP4/GIF migration story.

The renderer follows Agent Talk's media pipeline: render each asciinema cast
independently, then composite those terminal streams on one shared video
timeline. It does not load or screenshot the project website.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

WIDTH = 1440
HEIGHT = 560
DURATION = 53
VIDEO_FPS = 30
GIF_FPS = 20
SOURCE_SPEED = 4.6
SOURCE_LIVE_END = 8.1
PULLBACK_START = 11.7
PULLBACK_END = 15.0
MIGRATION_START = 15.0
TARGET_START = 28.0
HIGHLIGHT_START = 30.5
OVERLAP_END = 36.0
TARGET_ZOOM_END = 38.5
FULL_WIDTH = 910
SPLIT_WIDTH = 690
FULL_X = 265
SPLIT_SOURCE_X = 20
SPLIT_TARGET_X = 730
FULL_Y = 6
SPLIT_Y = 72
SESSION_TITLE = "fix-timeline-merging"
FONT_SIZE = 22
LINE_HEIGHT = 1.14


def require_program(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"{name} is required")
    return path


def run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def render_cast(
    agg: str,
    source: Path,
    output: Path,
    *,
    select: str,
    speed: float = 1,
    idle_limit: int = 2,
) -> None:
    run(
        [
            agg,
            "--quiet",
            "--font-size",
            str(FONT_SIZE),
            "--line-height",
            str(LINE_HEIGHT),
            "--theme",
            "asciinema",
            "--fps-cap",
            str(VIDEO_FPS),
            "--speed",
            str(speed),
            "--idle-time-limit",
            str(idle_limit),
            "--last-frame-duration",
            "0",
            "--select",
            select,
            str(source),
            str(output),
        ]
    )


def write_migration_cast(path: Path, target: str) -> None:
    label = "Pi" if target == "pi" else "Codex"
    command = f"smigrate transfer --title {SESSION_TITLE} --from claude --to {target}"
    header = {
        "version": 2,
        "width": 76,
        "height": 22,
        "timestamp": 0,
        "env": {"TERM": "xterm-256color", "SHELL": "/bin/sh"},
    }
    events: list[list[object]] = [
        [0.0, "o", "\u001b[2J\u001b[H\u001b[92m$\u001b[0m "],
    ]
    for index, character in enumerate(command, start=1):
        events.append([index * 6 / len(command), "o", character])
    events.extend(
        [
            [6.45, "o", "\r\n\r\n\u001b[90msource\u001b[0m   Claude Code · fix-timeline-merging"],
            [7.1, "o", "\r\n\u001b[90mscan\u001b[0m     linked messages and tool history"],
            [8.0, "o", "\r\n\u001b[90mmapping\u001b[0m  portable timeline validated"],
            [9.0, "o", f"\r\n\u001b[90mtarget\u001b[0m   {label} · native session ready"],
            [9.7, "o", "\r\n\r\n\u001b[92m✓ migration complete\u001b[0m"],
            [10.25, "o", f"\r\nContinue “{SESSION_TITLE}” in {label}"],
            [12.95, "o", "\u001b[0m"],
        ]
    )
    path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in [header, *events]) + "\n",
        encoding="utf-8",
    )


def panel_filters(
    *,
    title: str,
    state: str,
    regular_font: Path,
    bold_font: Path,
    highlight_y: int | None = None,
    highlight_start: float = 0,
    highlight_end: float = 0,
) -> str:
    filters = [
        "pad=iw+5:ih+55:2:52:color=0x111318",
        "drawbox=x=0:y=0:w=iw:h=52:color=0x191c22:t=fill",
        "drawbox=x=0:y=0:w=iw:h=ih:color=0x3a3f49:t=2",
        "drawbox=x=16:y=21:w=9:h=9:color=0x555b66:t=fill",
        "drawbox=x=33:y=21:w=9:h=9:color=0x555b66:t=fill",
        "drawbox=x=50:y=21:w=9:h=9:color=0x555b66:t=fill",
        (
            f"drawtext=fontfile='{bold_font}':text='{title}':fontcolor=0xe9e9e5:"
            "fontsize=22:x=(w-text_w)/2:y=14"
        ),
        (
            f"drawtext=fontfile='{regular_font}':text='{state}':fontcolor=0x7f838c:"
            "fontsize=15:x=w-text_w-18:y=17"
        ),
    ]
    if highlight_y is not None:
        enabled = f"between(t,{highlight_start},{highlight_end})"
        filters.extend(
            [
                (
                    f"drawbox=x=10:y={highlight_y}:w=iw-20:h=132:"
                    f"color=0xf6d54f@0.17:t=fill:enable='{enabled}'"
                ),
                (
                    f"drawbox=x=10:y={highlight_y}:w=iw-20:h=132:"
                    f"color=0xf6d54f@0.92:t=3:enable='{enabled}'"
                ),
                (
                    f"drawtext=fontfile='{bold_font}':text='SHARED HISTORY':"
                    "fontcolor=0x111318:fontsize=13:box=1:boxcolor=0xf6d54f:boxborderw=5:"
                    f"x=w-text_w-15:y={highlight_y - 22}:enable='{enabled}'"
                ),
            ]
        )
    return ",".join(filters)


def make_source_panel(
    ffmpeg: str,
    live: Path,
    hold: Path,
    output: Path,
    scratch: Path,
    regular_font: Path,
    bold_font: Path,
) -> None:
    warning = scratch / "warning.txt"
    suggestion = scratch / "suggestion.txt"
    warning.write_text("You've hit your limit · resets 3pm (America/Montreal)", encoding="utf-8")
    suggestion.write_text("/upgrade to increase your usage limit.", encoding="utf-8")
    hold_duration = DURATION - SOURCE_LIVE_END
    panel = panel_filters(
        title="CLAUDE CODE",
        state="SOURCE SESSION",
        regular_font=regular_font,
        bold_font=bold_font,
        highlight_y=126,
        highlight_start=HIGHLIGHT_START,
        highlight_end=OVERLAP_END,
    )
    filter_graph = (
        f"[0:v]fps={VIDEO_FPS},trim=duration={SOURCE_LIVE_END},setpts=PTS-STARTPTS[live];"
        f"[1:v]fps={VIDEO_FPS},trim=duration={hold_duration},setpts=PTS-STARTPTS[hold];"
        "[live][hold]concat=n=2:v=1:a=0[body];"
        f"[body]{panel},"
        f"drawtext=fontfile='{bold_font}':textfile='{warning}':fontcolor=0xff6673:"
        "fontsize=20:x=42:y=482:enable='between(t,8.7,36)',"
        f"drawtext=fontfile='{regular_font}':textfile='{suggestion}':fontcolor=0x8a8d95:"
        "fontsize=19:x=42:y=510:enable='between(t,8.7,36)'[out]"
    )
    run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(live),
            "-stream_loop",
            "-1",
            "-i",
            str(hold),
            "-filter_complex",
            filter_graph,
            "-map",
            "[out]",
            "-t",
            str(DURATION),
            "-r",
            str(VIDEO_FPS),
            "-c:v",
            "libx264",
            "-crf",
            "15",
            "-pix_fmt",
            "yuv420p",
            str(output),
        ]
    )


def make_panel(
    ffmpeg: str,
    rendered_cast: Path,
    output: Path,
    *,
    duration: float,
    title: str,
    state: str,
    regular_font: Path,
    bold_font: Path,
    highlight_y: int | None = None,
    highlight_start: float = 0,
    highlight_end: float = 0,
) -> None:
    panel = panel_filters(
        title=title,
        state=state,
        regular_font=regular_font,
        bold_font=bold_font,
        highlight_y=highlight_y,
        highlight_start=highlight_start,
        highlight_end=highlight_end,
    )
    run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(rendered_cast),
            "-vf",
            f"fps={VIDEO_FPS},tpad=stop_mode=clone:stop_duration={duration},{panel},trim=duration={duration}",
            "-t",
            str(duration),
            "-r",
            str(VIDEO_FPS),
            "-c:v",
            "libx264",
            "-crf",
            "15",
            "-pix_fmt",
            "yuv420p",
            str(output),
        ]
    )


def compose(
    ffmpeg: str,
    source: Path,
    migration: Path,
    target: Path,
    output: Path,
    *,
    route: str,
    bold_font: Path,
) -> None:
    pull = f"clip((t-{PULLBACK_START})/({PULLBACK_END - PULLBACK_START}),0,1)"
    pull_ease = f"({pull})*({pull})*(3-2*({pull}))"
    zoom = f"clip((t-{OVERLAP_END})/({TARGET_ZOOM_END - OVERLAP_END}),0,1)"
    zoom_ease = f"({zoom})*({zoom})*(3-2*({zoom}))"
    source_width = f"trunc(({FULL_WIDTH}-{FULL_WIDTH - SPLIT_WIDTH}*({pull_ease}))/2)*2"
    target_width = f"trunc(({SPLIT_WIDTH}+{FULL_WIDTH - SPLIT_WIDTH}*({zoom_ease}))/2)*2"
    source_x = f"{FULL_X}+({SPLIT_SOURCE_X - FULL_X})*({pull_ease})"
    source_y = f"{FULL_Y}+({SPLIT_Y - FULL_Y})*({pull_ease})"
    target_x = f"{SPLIT_TARGET_X}+({FULL_X - SPLIT_TARGET_X})*({zoom_ease})"
    target_y = f"{SPLIT_Y}+({FULL_Y - SPLIT_Y})*({zoom_ease})"
    connector_y = 206 if route == "pi" else 242
    filter_graph = (
        f"[0:v]drawgrid=w=40:h=40:t=1:c=white@0.025,format=yuv420p[bg];"
        f"[1:v]scale=w='{source_width}':h=-2:eval=frame[src];"
        f"[2:v]format=rgba,fade=t=in:st={MIGRATION_START}:d=0.35:alpha=1,"
        f"scale=w={SPLIT_WIDTH}:h=-2[mig];"
        f"[3:v]format=rgba,fade=t=in:st={TARGET_START}:d=0.35:alpha=1,"
        f"scale=w='{target_width}':h=-2:eval=frame[target];"
        f"[bg][src]overlay=x='{source_x}':y='{source_y}':eval=frame:"
        f"enable='lte(t,{OVERLAP_END})':eof_action=pass[v1];"
        f"[v1][mig]overlay=x={SPLIT_TARGET_X}:y={SPLIT_Y}:eval=frame:"
        f"enable='between(t,{MIGRATION_START},{TARGET_START})':eof_action=pass[v2];"
        f"[v2][target]overlay=x='{target_x}':y='{target_y}':eval=frame:"
        f"enable='gte(t,{TARGET_START})':eof_action=pass[v3];"
        f"[v3]drawbox=x=704:y={connector_y}:w=32:h=2:color=0xf6d54f@0.9:t=fill:"
        f"enable='between(t,{HIGHLIGHT_START},{OVERLAP_END})',"
        f"drawtext=fontfile='{bold_font}':text='SAME HISTORY':fontcolor=0xf6d54f:"
        f"fontsize=11:x=(w-text_w)/2:y={connector_y - 20}:"
        f"enable='between(t,{HIGHLIGHT_START},{OVERLAP_END})'[out]"
    )
    run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x080b10:s={WIDTH}x{HEIGHT}:r={VIDEO_FPS}:d={DURATION}",
            "-i",
            str(source),
            "-itsoffset",
            str(MIGRATION_START),
            "-i",
            str(migration),
            "-itsoffset",
            str(TARGET_START),
            "-i",
            str(target),
            "-filter_complex",
            filter_graph,
            "-map",
            "[out]",
            "-t",
            str(DURATION),
            "-r",
            str(VIDEO_FPS),
            "-c:v",
            "libx264",
            "-preset",
            "slow",
            "-crf",
            "17",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )


def encode_gif(ffmpeg: str, mp4: Path, gif: Path) -> None:
    run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(mp4),
            "-filter_complex",
            (
                f"fps={GIF_FPS},split[s0][s1];"
                "[s0]palettegen=max_colors=128:stats_mode=diff[p];"
                "[s1][p]paletteuse=dither=bayer:diff_mode=rectangle"
            ),
            "-loop",
            "0",
            str(gif),
        ]
    )


def render(cast_root: Path, output: Path, targets: list[str]) -> None:
    agg = require_program("agg")
    ffmpeg = require_program("ffmpeg")
    regular_font = Path("/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf")
    bold_font = Path("/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf")
    if not regular_font.is_file() or not bold_font.is_file():
        raise RuntimeError("Liberation Mono fonts are required")
    output.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="session-migrate-direct-video-") as value:
        scratch = Path(value)
        source_live = scratch / "source-live.gif"
        source_hold = scratch / "source-hold.gif"
        render_cast(
            agg,
            cast_root / "demo-claude.cast",
            source_live,
            select=f"..{SOURCE_LIVE_END}",
            speed=SOURCE_SPEED,
            idle_limit=999,
        )
        render_cast(
            agg,
            cast_root / "demo-claude-hold.cast",
            source_hold,
            select="0",
            idle_limit=999,
        )
        source_panel = scratch / "source-panel.mp4"
        make_source_panel(
            ffmpeg,
            source_live,
            source_hold,
            source_panel,
            scratch,
            regular_font,
            bold_font,
        )

        for target_name in targets:
            label = "Pi" if target_name == "pi" else "Codex"
            migration_cast = scratch / f"migration-{target_name}.cast"
            migration_gif = scratch / f"migration-{target_name}.gif"
            migration_panel = scratch / f"migration-{target_name}.mp4"
            target_gif = scratch / f"target-{target_name}.gif"
            target_panel = scratch / f"target-{target_name}.mp4"
            write_migration_cast(migration_cast, target_name)
            render_cast(
                agg,
                migration_cast,
                migration_gif,
                select="..100%",
                idle_limit=999,
            )
            render_cast(
                agg,
                cast_root / f"demo-{target_name}.cast",
                target_gif,
                select="..25",
                idle_limit=2,
            )
            make_panel(
                ffmpeg,
                migration_gif,
                migration_panel,
                duration=13,
                title="SESSION-MIGRATE",
                state="WORKING",
                regular_font=regular_font,
                bold_font=bold_font,
            )
            make_panel(
                ffmpeg,
                target_gif,
                target_panel,
                duration=25,
                title=label.upper(),
                state="RESUMED SESSION",
                regular_font=regular_font,
                bold_font=bold_font,
                highlight_y=148 if target_name == "pi" else 248,
                highlight_start=HIGHLIGHT_START - TARGET_START,
                highlight_end=OVERLAP_END - TARGET_START,
            )
            mp4 = output / f"demo-{target_name}.mp4"
            gif = output / f"demo-{target_name}.gif"
            compose(
                ffmpeg,
                source_panel,
                migration_panel,
                target_panel,
                mp4,
                route=target_name,
                bold_font=bold_font,
            )
            encode_gif(ffmpeg, mp4, gif)

    shutil.copyfile(output / "demo-pi.mp4", output / "demo.mp4")
    shutil.copyfile(output / "demo-pi.gif", output / "demo.gif")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "cast_root",
        type=Path,
        help="directory containing demo-claude/pi/codex casts",
    )
    parser.add_argument("output", type=Path)
    parser.add_argument("--target", choices=("pi", "codex"), action="append")
    args = parser.parse_args()
    render(args.cast_root.resolve(), args.output.resolve(), args.target or ["pi", "codex"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
