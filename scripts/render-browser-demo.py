#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright==1.55.0"]
# ///
"""Render the website's real-cast handoff story into MP4 and GIF assets.

This renderer does not replay or reconstruct a conversation. It captures the
same local asciinema casts and six-stage DOM animation used by the landing page.
Run the native capture harness first, then copy its cast files into the static
site's ``assets`` directory before invoking this script.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import http.server
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

FPS = 10
DURATION = 43


def browser_executable() -> Path:
    cache = Path.home() / ".cache" / "ms-playwright"
    candidates = sorted(
        [
            *cache.glob("chromium-*/chrome-linux64/chrome"),
            *cache.glob("chromium-*/chrome-linux/chrome"),
        ],
        reverse=True,
    )
    if not candidates:
        raise RuntimeError("no Playwright Chromium executable found")
    return candidates[0]


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@contextlib.contextmanager
def static_server(root: Path):
    port = free_port()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def encode(frames: Path, mp4: Path, gif: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required")
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-framerate",
            str(FPS),
            "-i",
            str(frames / "frame-%04d.jpg"),
            "-vf",
            "pad=ceil(iw/2)*2:ceil(ih/2)*2",
            "-c:v",
            "libx264",
            "-preset",
            "slow",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(mp4),
        ],
        check=True,
    )
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(mp4),
            "-vf",
            "fps=10,scale=1440:-2:flags=lanczos,split[s0][s1];"
            "[s0]palettegen=max_colors=112[p];[s1][p]paletteuse=dither=bayer",
            "-loop",
            "0",
            str(gif),
        ],
        check=True,
    )


def render(site_root: Path, output: Path, targets: list[str]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="session-migrate-story-") as scratch_value:
        scratch = Path(scratch_value)
        with static_server(site_root) as url, sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=str(browser_executable()),
                headless=True,
                args=["--disable-dev-shm-usage", "--no-sandbox"],
            )
            page = browser.new_page(viewport={"width": 1920, "height": 1200}, device_scale_factor=1)
            page.goto(url, wait_until="networkidle")
            page.add_style_tag(
                content=".shell{width:min(1720px,calc(100% - 48px))!important}"
                ".trajectory-frame{width:100%!important}"
            )
            page.locator("#demo").scroll_into_view_if_needed()
            page.wait_for_function(
                "window.__sessionMigrateDemo && "
                "document.querySelectorAll('.ap-player').length === 2"
            )
            frame = page.locator(".trajectory-frame")

            for target in targets:
                label = "Pi" if target == "pi" else "Codex"
                page.get_by_role("button", name=f"Claude → {label}").click()
                page.wait_for_function("document.querySelectorAll('.ap-player').length === 2")
                page.evaluate("window.__sessionMigrateDemo.pause()")
                page.evaluate("window.__sessionMigrateDemo.setTime(0)")
                time.sleep(0.3)
                frames = scratch / target
                frames.mkdir()
                for index in range(DURATION * FPS):
                    started = time.monotonic()
                    page.evaluate("time => window.__sessionMigrateDemo.setTime(time)", index / FPS)
                    frame.screenshot(
                        path=str(frames / f"frame-{index:04d}.jpg"),
                        type="jpeg",
                        quality=92,
                    )
                    remaining = 1 / FPS - (time.monotonic() - started)
                    if remaining > 0:
                        time.sleep(remaining)
                encode(frames, output / f"demo-{target}.mp4", output / f"demo-{target}.gif")
            browser.close()

    shutil.copyfile(output / "demo-pi.mp4", output / "demo.mp4")
    shutil.copyfile(output / "demo-pi.gif", output / "demo.gif")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("site_root", type=Path, help="static website root containing index.html")
    parser.add_argument("output", type=Path, help="directory for demo-pi/codex MP4 and GIF files")
    parser.add_argument("--target", choices=("pi", "codex"), action="append")
    args = parser.parse_args()
    render(args.site_root.resolve(), args.output.resolve(), args.target or ["pi", "codex"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
