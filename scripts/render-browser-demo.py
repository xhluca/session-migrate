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

CAPTURE_FPS = 5
OUTPUT_FPS = 10
DURATION = 53


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
            str(CAPTURE_FPS),
            "-i",
            str(frames / "frame-%04d.jpg"),
            "-vf",
            f"fps={OUTPUT_FPS},pad=ceil(iw/2)*2:ceil(ih/2)*2",
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
            f"fps={OUTPUT_FPS},scale=1440:-2:flags=lanczos,split[s0][s1];"
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
                content="""
                .shell { width: min(1872px, calc(100% - 24px)) !important; }
                .trajectory-frame { width: 100% !important; padding: 0 8px 8px !important; }
                .trajectory-topbar { display: none !important; }
                .native-window,
                .migration-window {
                  top: 3.5% !important;
                  left: 2% !important;
                  width: 96% !important;
                  height: 93% !important;
                }
                .native-window-bar {
                  grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr) !important;
                  column-gap: 12px !important;
                  height: 52px !important;
                  padding-inline: 16px !important;
                  font-size: 21px !important;
                }
                .native-window-bar b {
                  font-size: 23px !important;
                  white-space: nowrap !important;
                }
                .native-window-bar em {
                  min-width: 0 !important;
                  overflow: hidden !important;
                  font-size: 19px !important;
                  letter-spacing: .04em !important;
                  text-overflow: ellipsis !important;
                  white-space: nowrap !important;
                }
                .native-window-dots { gap: 7px !important; }
                .native-window-dots i { width: 10px !important; height: 10px !important; }
                .cast-mount { height: calc(100% - 52px) !important; }
                .context-limit {
                  gap: 7px !important;
                  padding: 12px 19px !important;
                  font-size: 20px !important;
                  line-height: 1.35 !important;
                }
                .context-limit b { font-size: 22px !important; }
                .context-limit span { font-size: 19px !important; }
                .migration-body {
                  height: calc(100% - 52px) !important;
                  padding: clamp(19px, 2.4vw, 31px) !important;
                  font-size: clamp(16px, 1.55vw, 22px) !important;
                }
                .migration-command {
                  min-width: 0 !important;
                  min-height: 5em !important;
                  overflow-wrap: anywhere !important;
                }
                .migration-output p {
                  grid-template-columns: 92px minmax(0, 1fr) !important;
                  gap: 14px !important;
                }
                .migration-output span,
                .migration-launch {
                  min-width: 0 !important;
                  overflow-wrap: anywhere !important;
                }
                .migration-launch { font-size: 22px !important; }
                .handoff-grid[data-phase="pullback"] .native-window-source,
                .handoff-grid[data-phase="convert"] .native-window-source,
                .handoff-grid[data-phase="launch"] .native-window-source,
                .handoff-grid[data-phase="overlap"] .native-window-source {
                  top: 9% !important;
                  left: .75% !important;
                  width: 48% !important;
                  height: 82% !important;
                }
                .handoff-grid[data-phase="convert"] .migration-window {
                  top: 9% !important;
                  left: 51.25% !important;
                  width: 48% !important;
                  height: 82% !important;
                }
                .handoff-grid[data-phase="launch"] .native-window-target,
                .handoff-grid[data-phase="overlap"] .native-window-target {
                  top: 9% !important;
                  left: 51.25% !important;
                  width: 48% !important;
                  height: 82% !important;
                }
                .handoff-grid[data-phase="target"] .native-window-target {
                  top: 3.5% !important;
                  left: 2% !important;
                  width: 96% !important;
                  height: 93% !important;
                }
                .history-bridge { left: 48.65% !important; width: 2.7% !important; }
                .history-marker span { font-size: 12px !important; }
                .history-bridge span { font-size: 9px !important; }
                .history-marker,
                .history-bridge { transition: none !important; }
                .trajectory-frame figcaption {
                  align-items: center !important;
                  color: #8f929b !important;
                  font-size: 17px !important;
                  font-weight: 600 !important;
                  font-variant-ligatures: none !important;
                  letter-spacing: .06em !important;
                  line-height: 1.2 !important;
                  white-space: nowrap !important;
                }
                .trajectory-frame figcaption span,
                .trajectory-frame figcaption em { flex: 0 0 auto !important; }
                """
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
                for index in range(DURATION * CAPTURE_FPS):
                    page.evaluate(
                        "time => window.__sessionMigrateDemo.setTime(time)",
                        index / CAPTURE_FPS,
                    )
                    page.evaluate(
                        """() => new Promise(resolve => requestAnimationFrame(
                          () => requestAnimationFrame(resolve)
                        ))"""
                    )
                    if index == 31 * CAPTURE_FPS:
                        highlight_state = page.evaluate(
                            """() => {
                              const grid = document.querySelector('.handoff-grid');
                              const mounts = document.querySelectorAll(
                                '[data-source-cast],[data-target-cast]'
                              );
                              return {
                                phase: grid.dataset.phase,
                                aligned: grid.dataset.historyAligned,
                                markers: [...document.querySelectorAll('.history-marker')].map(
                                  marker => ({
                                    anchored: marker.dataset.anchored,
                                    opacity: getComputedStyle(marker).opacity,
                                    visibility: getComputedStyle(marker).visibility,
                                  })
                                ),
                                castText: [...mounts].map(mount => ({
                                  hasStart: mount.textContent.includes(
                                    'So I read "backward compatible"'
                                  ),
                                  hasEnd: mount.textContent.includes(
                                    'two distinguishable cases.'
                                  ),
                                  length: mount.textContent.length,
                                })),
                              };
                            }"""
                        )
                        markers = highlight_state["markers"]
                        if (
                            highlight_state["phase"] != "overlap"
                            or highlight_state["aligned"] != "true"
                            or len(markers) != 2
                            or any(
                                marker["anchored"] != "true"
                                or marker["opacity"] != "1"
                                or marker["visibility"] != "visible"
                                for marker in markers
                            )
                        ):
                            raise RuntimeError(
                                f"shared-history highlight did not settle: {highlight_state}"
                            )
                    frame.screenshot(
                        path=str(frames / f"frame-{index:04d}.jpg"),
                        type="jpeg",
                        quality=92,
                    )
                page.evaluate("window.__sessionMigrateDemo.pause()")
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
