#!/usr/bin/env python3
"""Record a real Claude -> Pi/Codex migration inside the native TUIs.

This is an opt-in release-media harness. It makes short-lived copies of the
local Claude and Codex OAuth records, submits synthetic prompts to the real
clients, records only the post-login session views, and removes the private
workspace after rendering the public assets.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pexpect

IMAGE_ID = "sha256:8f170f660813ac358f347fa8a3580139972f3ea7a9fb087834f1da44669d9392"
CLAUDE_SESSION_ID = "10000000-0000-4000-8000-000000000000"
PI_SESSION_ID = "20000000-0000-4000-8000-000000000000"
CODEX_SESSION_ID = "30000000-0000-4000-8000-000000000000"
WIDTH = 110
HEIGHT = 34
SOURCE_PROMPT = 'Reply with exactly "Migration begins in Claude." and do not use tools.'
SOURCE_REPLY = "Migration begins in Claude."
WARMUP_PROMPT = 'Reply with exactly "This native session is ready." and do not use tools.'
WARMUP_REPLY = "This native session is ready."
TARGET_PROMPTS = {
    "pi": 'Reply with exactly "Continued in Pi." and do not use tools.',
    "codex": 'Reply with exactly "Continued in Codex." and do not use tools.',
}
TARGET_REPLIES = {"pi": "Continued in Pi.", "codex": "Continued in Codex."}


@dataclass(frozen=True)
class Recording:
    title: str
    events: tuple[list[object], ...]

    @property
    def duration(self) -> float:
        return float(self.events[-1][0]) if self.events else 0.0


class NativeTui:
    def __init__(
        self,
        command: list[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        title: str,
    ) -> None:
        self.title = title
        self.started = time.monotonic()
        self.recording_started: float | None = None
        self.events: list[list[object]] = []
        self.recent = b""
        self.child = pexpect.spawn(
            command[0],
            command[1:],
            cwd=str(cwd),
            env=environment,
            dimensions=(HEIGHT, WIDTH),
            echo=False,
            encoding=None,
            timeout=0.1,
        )

    def pump(self, seconds: float = 0.1) -> bytes:
        deadline = time.monotonic() + seconds
        collected = b""
        while time.monotonic() < deadline:
            try:
                data = self.child.read_nonblocking(size=65536, timeout=0.05)
            except pexpect.TIMEOUT:
                continue
            except pexpect.EOF:
                break
            if not data:
                continue
            collected += data
            self.recent = (self.recent + data)[-524288:]
            answer_terminal_queries(self.child, data)
            if self.recording_started is not None:
                self.events.append(
                    [
                        round(time.monotonic() - self.recording_started, 6),
                        "o",
                        data.decode("utf-8", errors="replace"),
                    ]
                )
        return collected

    def wait_for(self, marker: bytes, timeout: float = 45) -> None:
        deadline = time.monotonic() + timeout
        trust_confirmed = False
        while time.monotonic() < deadline:
            self.pump(0.25)
            if not trust_confirmed and (
                b"Quick safety check:" in self.recent
                or b"Do you trust the contents of this directory?" in self.recent
                or b"Yes, continue" in self.recent
            ):
                self.child.send(b"\r")
                trust_confirmed = True
            if marker in self.recent:
                return
        diagnostics = [
            label
            for label, value in (
                ("trust_prompt", b"Quick safety check:"),
                ("login_expired", b"Login expired"),
                ("no_conversation", b"No conversation found"),
                ("api_error", b"API Error"),
                ("input_prompt", b"esc to interrupt"),
            )
            if value in self.recent
        ]
        excerpt = self.recent.decode("utf-8", errors="replace")[-4000:]
        excerpt = re.sub(r"[\w.+-]+@[\w.-]+", "[redacted-email]", excerpt)
        excerpt = excerpt.replace(str(Path.home()), "~")
        raise RuntimeError(
            f"{self.title} did not render {marker!r}; diagnostics={diagnostics}; "
            f"sanitized_excerpt={excerpt!r}"
        )

    def begin_recording(self) -> None:
        self.events.clear()
        self.recording_started = time.monotonic()
        # A resize makes the native client repaint the complete current viewport.
        self.child.setwinsize(HEIGHT, WIDTH - 1)
        self.pump(0.35)
        self.child.setwinsize(HEIGHT, WIDTH)
        self.pump(1.0)

    def type_at_human_speed(self, value: str) -> None:
        for character in value:
            self.child.send(character.encode())
            self.pump(0.045)
        self.pump(0.35)
        self.child.send(b"\r")
        self.pump(0.5)

    def send(self, value: bytes, pause: float = 0.5) -> None:
        self.child.send(value)
        self.pump(pause)

    def wait_for_native_reply(
        self,
        path: Path,
        expected: str,
        native_format: str,
        timeout: float = 150,
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pump(0.3)
            if assistant_text_exists(path, expected, native_format):
                self.pump(2.0)
                return
        raise RuntimeError(f"{self.title} did not persist its synthetic reply")

    def finish(self) -> Recording:
        # The public recording ends while the conversation is still visible.
        # Shutdown bytes belong to the private harness, not the demo.
        self.recording_started = None
        self.send(b"\x03", 0.25)
        self.send(b"\x03", 0.75)
        if self.child.isalive():
            self.child.kill(signal.SIGTERM)
            self.child.close(force=True)
        return Recording(self.title, tuple(self.events))


def require_program(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(f"required program is not installed: {name}")
    return path


def private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(stat.S_IRWXU)


def private_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    target.chmod(stat.S_IRUSR | stat.S_IWUSR)


def write_private_json(path: Path, value: dict[str, Any]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.write("\n")


def answer_terminal_queries(child: pexpect.spawn, data: bytes) -> None:
    for query, reply in (
        (b"\x1b[6n", b"\x1b[1;1R"),
        (b"\x1b]10;?\x1b\\", b"\x1b]10;rgb:ffff/ffff/ffff\x1b\\"),
        (b"\x1b]11;?\x1b\\", b"\x1b]11;rgb:1111/1111/1111\x1b\\"),
        (b"\x1b]11;?\x07", b"\x1b]11;rgb:1111/1111/1111\x07"),
        (b"\x1b[?u", b"\x1b[?0u"),
        (b"\x1b[c", b"\x1b[?1;2c"),
    ):
        if query in data:
            child.send(reply)


def assistant_text_exists(path: Path, expected: str, native_format: str) -> bool:
    if not path.is_file():
        return False
    try:
        records = [json.loads(line) for line in path.read_text().splitlines() if line]
    except (OSError, json.JSONDecodeError):
        return False
    if native_format == "claude":
        blocks = [
            record.get("message", {}).get("content")
            for record in records
            if record.get("type") == "assistant"
        ]
    elif native_format == "pi":
        blocks = [
            record.get("message", {}).get("content")
            for record in records
            if record.get("type") == "message"
            and record.get("message", {}).get("role") == "assistant"
        ]
    elif native_format == "codex":
        blocks = [
            record.get("payload", {}).get("content")
            for record in records
            if record.get("type") == "response_item"
            and record.get("payload", {}).get("type") == "message"
            and record.get("payload", {}).get("role") == "assistant"
        ]
    else:
        raise AssertionError(native_format)
    for content in blocks:
        if content == expected:
            return True
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("text") == expected:
                    return True
    return False


def isolated_environment(home: Path, temporary: Path) -> dict[str, str]:
    values = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "TERM": "xterm-256color",
        "NO_COLOR": "0",
        "TMPDIR": str(temporary / "tmp"),
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_CACHE_HOME": str(home / "cache"),
        "XDG_STATE_HOME": str(home / "state"),
    }
    for key in (
        "HOME",
        "TMPDIR",
        "XDG_DATA_HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
    ):
        private_directory(Path(values[key]))
    for key in ("LANG", "LC_ALL", "LC_CTYPE", "SSL_CERT_FILE", "SSL_CERT_DIR"):
        if value := os.environ.get(key):
            values[key] = value
    return values


def translate_codex_oauth(source: Path) -> dict[str, Any]:
    value = json.loads(source.read_text(encoding="utf-8"))
    tokens = value.get("tokens") if isinstance(value, dict) else None
    if not isinstance(tokens, dict):
        raise RuntimeError("Codex auth file has no token object")
    access = tokens.get("access_token")
    refresh = tokens.get("refresh_token")
    account = tokens.get("account_id")
    if not all(isinstance(item, str) and item for item in (access, refresh, account)):
        raise RuntimeError("Codex OAuth record is incomplete")
    payload = access.split(".")[1]
    expiry = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["exp"]
    if not isinstance(expiry, int) or expiry <= int(time.time()):
        raise RuntimeError("Codex OAuth access token is expired")
    return {
        "type": "oauth",
        "access": access,
        "refresh": refresh,
        "expires": expiry * 1000,
        "accountId": account,
    }


def prepare_source(repo: Path, root: Path, work: Path) -> tuple[Path, dict[str, str]]:
    claude_config = root / "claude"
    claude_home = root / "claude-home"
    private_directory(claude_config)
    private_directory(claude_home)
    private_copy(Path.home() / ".claude" / ".credentials.json", claude_config / ".credentials.json")
    private_copy(Path.home() / ".claude.json", claude_config / ".claude.json")
    encoded = re.sub(r"[^A-Za-z0-9]", "-", str(work.resolve())) or "-"
    project = claude_config / "projects" / encoded
    private_directory(project)
    session = project / f"{CLAUDE_SESSION_ID}.jsonl"
    private_copy(repo / "tests/fixtures/claude-2.1.209/basic.jsonl", session)
    environment = isolated_environment(claude_home, root)
    environment.update(
        {
            "CLAUDE_CONFIG_DIR": str(claude_config),
            "DISABLE_AUTOUPDATER": "1",
        }
    )
    return session, environment


def record_claude(repo: Path, root: Path, work: Path) -> tuple[Recording, Path]:
    source, environment = prepare_source(repo, root, work)
    tui = NativeTui(
        [
            require_program("claude"),
            "--safe-mode",
            "--permission-mode",
            "manual",
            "--resume",
            CLAUDE_SESSION_ID,
        ],
        cwd=work,
        environment=environment,
        title="Claude Code · native source",
    )
    try:
        tui.wait_for(b"fixture", 60)
        # Add one real off-camera turn so the account welcome panel scrolls out
        # before the public recording begins.
        tui.type_at_human_speed(WARMUP_PROMPT)
        tui.wait_for_native_reply(source, WARMUP_REPLY, "claude")
        # Dismiss transient account-usage notices before the public capture.
        tui.send(b" ", 0.2)
        tui.send(b"\x7f", 0.8)
        tui.begin_recording()
        tui.type_at_human_speed(SOURCE_PROMPT)
        tui.wait_for_native_reply(source, SOURCE_REPLY, "claude")
        tui.send(b"\x7f\x7f\x7f", 0.3)
        tui.send(b"\x15", 0.4)
    finally:
        recording = tui.finish()
    if not assistant_text_exists(source, SOURCE_REPLY, "claude"):
        raise RuntimeError("Claude source trajectory was not persisted")
    return recording, source


def install_pi_cli(root: Path) -> Path:
    prefix = root / "pi-cli"
    completed = subprocess.run(
        [
            require_program("npm"),
            "install",
            "--silent",
            "--prefix",
            str(prefix),
            "@earendil-works/pi-coding-agent@0.80.6",
        ],
        check=False,
        timeout=120,
    )
    if completed.returncode != 0:
        raise RuntimeError("could not install the pinned Pi 0.80.6 capture client")
    binary = prefix / "node_modules" / ".bin" / "pi"
    version = subprocess.run(
        [str(binary), "--version"], check=True, capture_output=True, text=True
    ).stdout.strip()
    if version != "0.80.6":
        raise RuntimeError(f"unexpected Pi capture version: {version}")
    return binary


def run_import(
    repo: Path,
    source: Path,
    target: str,
    target_home: Path,
    session_id: str,
    cwd: Path,
) -> Path:
    command = [
        str(repo / ".venv" / "bin" / "smigrate"),
        "import",
        str(source),
        "--to",
        target,
        "--home",
        str(target_home),
        "--session-id",
        session_id,
        "--cwd",
        str(cwd),
    ]
    if target == "pi":
        command.extend(["--model-provider", "openai-codex", "--model", "gpt-5.6-luna"])
    else:
        command.extend(["--model-provider", "openai"])
    completed = subprocess.run(command, check=True, capture_output=True, text=True)
    result = json.loads(completed.stdout)
    if result.get("target_format") != target:
        raise RuntimeError(f"unexpected {target} import result")
    return Path(result["output"])


def record_pi(
    repo: Path,
    root: Path,
    work: Path,
    source: Path,
    pi_binary: Path,
) -> tuple[Recording, Path]:
    target_home = root / "pi"
    private_directory(target_home)
    target = run_import(repo, source, "pi", target_home, PI_SESSION_ID, work)
    write_private_json(
        target_home / "auth.json",
        {"openai-codex": translate_codex_oauth(Path.home() / ".codex" / "auth.json")},
    )
    environment = isolated_environment(root / "pi-home", root)
    environment.update(
        {
            "PI_CODING_AGENT_DIR": str(target_home),
            "PI_TELEMETRY": "0",
            "PI_SKIP_VERSION_CHECK": "1",
        }
    )
    tui = NativeTui(
        [
            str(pi_binary),
            "--provider",
            "openai-codex",
            "--model",
            "gpt-5.6-luna",
            "--thinking",
            "low",
            "--session",
            str(target),
            "--no-tools",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-context-files",
            "--approve",
        ],
        cwd=work,
        environment=environment,
        title="Pi 0.80.6 · migrated native session",
    )
    try:
        tui.wait_for(b"Migration", 60)
        tui.begin_recording()
        # Expand the imported compacted history, then review it at a readable pace.
        tui.send(b"\x0f", 1.2)
        for _ in range(3):
            tui.send(b"\x1b[5~", 0.8)
        for _ in range(2):
            tui.send(b"\x1b[6~", 0.8)
        tui.type_at_human_speed(TARGET_PROMPTS["pi"])
        tui.wait_for_native_reply(target, TARGET_REPLIES["pi"], "pi")
        tui.send(b"\x15", 0.4)
    finally:
        recording = tui.finish()
    return recording, target


def record_codex(
    repo: Path,
    root: Path,
    work: Path,
    source: Path,
) -> tuple[Recording, Path]:
    target_home = root / "codex"
    private_directory(target_home)
    target = run_import(repo, source, "codex", target_home, CODEX_SESSION_ID, Path("/work"))
    private_copy(Path.home() / ".codex" / "auth.json", target_home / "auth.json")
    state = root.resolve()
    uid_gid = f"{os.getuid()}:{os.getgid()}"
    command = [
        require_program("docker"),
        "run",
        "--rm",
        "-it",
        "--user",
        uid_gid,
        "-e",
        "TERM=xterm-256color",
        "-e",
        "HOME=/state/codex-os-home",
        "-e",
        "CODEX_HOME=/state/codex",
        "-v",
        f"{state}:/state",
        "-w",
        "/work",
        IMAGE_ID,
        "codex",
        "resume",
        "--no-alt-screen",
        "--disable",
        "apps",
        "--disable",
        "plugins",
        "--disable",
        "browser_use",
        CODEX_SESSION_ID,
    ]
    tui = NativeTui(
        command,
        cwd=work,
        environment={**os.environ, "TERM": "xterm-256color"},
        title="Codex 0.144.4 · migrated native session",
    )
    try:
        tui.wait_for(b"Migration", 60)
        tui.begin_recording()
        tui.type_at_human_speed(TARGET_PROMPTS["codex"])
        tui.wait_for_native_reply(target, TARGET_REPLIES["codex"], "codex")
        tui.send(b"\x15", 0.4)
    finally:
        recording = tui.finish()
    return recording, target


def title_card(target: str) -> list[list[object]]:
    target_label = "Pi 0.80.6" if target == "pi" else "Codex 0.144.4"
    return [
        [0.0, "o", "\x1b[2J\x1b[H\x1b[1;38;5;154msession-migrate\x1b[0m\n\n"],
        [0.6, "o", f"  Claude Code  \x1b[38;5;154m→\x1b[0m  {target_label}\n\n"],
        [1.2, "o", f"$ smigrate import claude-session.jsonl --to {target}\n"],
        [2.0, "o", f"\x1b[38;5;154m✓\x1b[0m native {target_label} session ready\n"],
        [3.0, "o", "\nOpening the migrated conversation…\n"],
    ]


def merge_recordings(source: Recording, target: Recording, target_name: str) -> Recording:
    events = [list(event) for event in source.events]
    offset = source.duration + 1.8
    for event in title_card(target_name):
        events.append([round(offset + float(event[0]), 6), event[1], event[2]])
    offset += 4.4
    events.append([round(offset, 6), "o", "\x1b[2J\x1b[H"])
    for event in target.events:
        events.append([round(offset + float(event[0]), 6), event[1], event[2]])
    return Recording(f"Claude Code to {target.title}", tuple(events))


def write_cast(path: Path, recording: Recording) -> None:
    header = {
        "version": 2,
        "width": WIDTH,
        "height": HEIGHT,
        "timestamp": int(time.time()),
        "env": {"SHELL": "/bin/bash", "TERM": "xterm-256color"},
        "title": recording.title,
    }
    public_events = [
        event
        for event in recording.events
        if not any(
            private_marker in str(event[2])
            for private_marker in ("weekly", "Organization", "Welcome back")
        )
    ]
    path.write_text(
        "\n".join(json.dumps(value, ensure_ascii=False) for value in (header, *public_events))
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def render_cast(cast: Path, gif: Path, mp4: Path) -> None:
    subprocess.run(
        [
            require_program("agg"),
            "--quiet",
            "--theme",
            "github-dark",
            "--font-size",
            "14",
            "--fps-cap",
            "15",
            "--speed",
            "1",
            "--idle-time-limit",
            "60",
            "--last-frame-duration",
            "3",
            str(cast),
            str(gif),
        ],
        check=True,
    )
    subprocess.run(
        [
            require_program("ffmpeg"),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(gif),
            "-movflags",
            "faststart",
            "-pix_fmt",
            "yuv420p",
            "-vf",
            "fps=24,scale=trunc(iw/2)*2:trunc(ih/2)*2",
            str(mp4),
        ],
        check=True,
    )


def render_frame(cast: Path, output: Path, scratch: Path, *, at: str = "100%") -> None:
    animation = scratch / f"{output.stem}.gif"
    subprocess.run(
        [
            require_program("agg"),
            "--quiet",
            "--theme",
            "github-dark",
            "--font-size",
            "14",
            "--speed",
            "1",
            "--idle-time-limit",
            "60",
            "--select",
            at,
            "--last-frame-duration",
            "1",
            str(cast),
            str(animation),
        ],
        check=True,
    )
    subprocess.run(
        [
            require_program("ffmpeg"),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(animation),
            "-frames:v",
            "1",
            str(output),
        ],
        check=True,
    )


def assert_public_capture(path: Path) -> None:
    forbidden = (
        str(Path.home()),
        "@anthropic",
        "access_token",
        "refresh_token",
        "account_id",
        "sk-ant-",
        "Bearer ",
    )
    data = path.read_bytes()
    for value in forbidden:
        if value.encode() in data:
            raise RuntimeError(f"private marker leaked into {path.name}")


def main() -> int:
    if os.environ.get("MIGRATE_NATIVE_CAPTURE_AUTH") != "1":
        raise RuntimeError("set MIGRATE_NATIVE_CAPTURE_AUTH=1 to authorize disposable OAuth copies")
    for name in ("agg", "claude", "docker", "ffmpeg", "npm"):
        require_program(name)
    repo = Path(__file__).resolve().parent.parent
    output = Path(sys.argv[1]).resolve() if len(sys.argv) == 2 else repo / "docs" / "assets"
    output.mkdir(parents=True, exist_ok=True)
    image = subprocess.run(
        ["docker", "image", "inspect", IMAGE_ID, "--format", "{{.Id}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if image != IMAGE_ID:
        raise RuntimeError(f"unexpected Docker image identity: {image}")

    scratch = Path(tempfile.mkdtemp(prefix="session-migrate-native-demo."))
    scratch.chmod(0o700)
    try:
        work = scratch / "work"
        private_directory(work)
        pi_binary = install_pi_cli(scratch)
        source_recording, source = record_claude(repo, scratch, work)
        pi_recording, pi_path = record_pi(repo, scratch, work, source, pi_binary)
        codex_recording, codex_path = record_codex(repo, scratch, work, source)
        if not assistant_text_exists(pi_path, TARGET_REPLIES["pi"], "pi"):
            raise RuntimeError("Pi continuation is missing")
        if not assistant_text_exists(codex_path, TARGET_REPLIES["codex"], "codex"):
            raise RuntimeError("Codex continuation is missing")

        source_cast = scratch / "claude.cast"
        pi_cast = scratch / "demo-pi.cast"
        codex_cast = scratch / "demo-codex.cast"
        write_cast(source_cast, source_recording)
        write_cast(pi_cast, merge_recordings(source_recording, pi_recording, "pi"))
        write_cast(codex_cast, merge_recordings(source_recording, codex_recording, "codex"))
        render_cast(pi_cast, output / "demo-pi.gif", output / "demo-pi.mp4")
        render_cast(codex_cast, output / "demo-codex.gif", output / "demo-codex.mp4")
        render_frame(source_cast, output / "demo-before.png", scratch)
        pi_only = scratch / "pi.cast"
        codex_only = scratch / "codex.cast"
        write_cast(pi_only, pi_recording)
        write_cast(codex_only, codex_recording)
        render_frame(pi_only, output / "demo-after-pi.png", scratch)
        render_frame(codex_only, output / "demo-after-codex.png", scratch)
        shutil.copyfile(output / "demo-pi.gif", output / "demo.gif")
        shutil.copyfile(output / "demo-pi.mp4", output / "demo.mp4")
        shutil.copyfile(output / "demo-before.png", output / "demo.png")
        for path in output.glob("demo*.*"):
            path.chmod(0o644)
            if path.suffix in {".gif", ".mp4", ".png"}:
                assert_public_capture(path)
        print(
            json.dumps(
                {
                    "claude_native_turns": 1,
                    "pi_native_continuations": 1,
                    "codex_native_continuations": 1,
                    "playback_speed": "1x",
                    "pi_version": "0.80.6",
                    "codex_version": "0.144.4",
                    "private_workspace_removed": True,
                },
                indent=2,
                sort_keys=True,
            )
        )
    finally:
        shutil.rmtree(scratch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
