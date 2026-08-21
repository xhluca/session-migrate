#!/usr/bin/env python3
"""Record Claude -> Pi/Codex migrations through real native terminal panes.

This release-media harness follows the agent-talk recording method: each CLI
runs inside tmux and asciinema records the terminal that the CLI actually
sees. It publishes the sanitized native casts and base release assets; the
browser renderer then turns the same casts into the staged website/README
story.

The run is opt-in because it makes short-lived, mode-0600 copies of local
Claude and Codex OAuth records. Published conversation text comes from an
isolated boundary-bug fixture; the private recording root is removed on exit.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

DEMO_ROOT = Path("/tmp/session-migrate-demo")
CLAUDE_SESSION_ID = "10000000-0000-4000-8000-000000000000"
PI_SESSION_ID = "20000000-0000-4000-8000-000000000000"
CODEX_SESSION_ID = "30000000-0000-4000-8000-000000000000"
CODEX_VERSION = "0.144.4"
IMAGE_ID = "sha256:8f170f660813ac358f347fa8a3580139972f3ea7a9fb087834f1da44669d9392"
COLS = 76
ROWS = 22
SOURCE_PLAYBACK_SPEED = 2.0
MASTER_WIDTH = 2560
MASTER_HEIGHT = 1440
TERMINAL_FONT_SIZE = 24
TERMINAL_LINE_HEIGHT = 1.65
GIF_WIDTH = 1440
SEED_PROMPT = (
    "The session replay timeline sometimes splits two events that should be one. "
    "Review src/timeline.py and its tests, find the boundary bug, and explain it "
    "without editing anything."
)
SOURCE_PROMPT = (
    "Keep gap_ms=0 backward compatible. Propose the smallest patch and one "
    "regression test that separates touching events from a real 1 ms gap."
)
TARGET_PROMPTS = {
    "pi": (
        "Continue in Pi: implement the patch you proposed, add the regression test, "
        "and run the focused test suite now. Use the available tools; don't just "
        "describe the change."
    ),
    "codex": (
        "Continue in Codex: implement the patch you proposed, add the regression "
        "test, and run the focused test suite now. Use the available tools; don't "
        "just describe the change."
    ),
}


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    capture: bool = False,
    timeout: float = 120,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        check=True,
        capture_output=capture,
        text=True,
        timeout=timeout,
    )


def require_program(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(f"required program is not installed: {name}")
    return path


def private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(stat.S_IRWXU)


def remove_private_tree(path: Path) -> None:
    """Remove our fixed private scratch tree, tolerating late CLI shutdown writes."""

    for attempt in range(5):
        shutil.rmtree(path, ignore_errors=attempt < 4)
        if not path.exists():
            return
        time.sleep(0.2)
    raise RuntimeError(f"could not remove private recording root: {path}")


def terminate_recording_processes() -> None:
    """Stop only subprocesses whose command line references our fixed scratch root."""

    owned: list[int] = []
    marker = str(DEMO_ROOT).encode()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            command = (entry / "cmdline").read_bytes()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if marker in command:
            owned.append(int(entry.name))
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in owned:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, sig)
        time.sleep(0.25)


def private_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    target.chmod(stat.S_IRUSR | stat.S_IWUSR)


def write_private_json(path: Path, value: dict[str, Any]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.write("\n")


def write_launcher(path: Path, environment: dict[str, str], command: list[str]) -> None:
    exports = " ".join(f"{key}={shlex.quote(value)}" for key, value in sorted(environment.items()))
    path.write_text(
        f"#!/usr/bin/env bash\nset -euo pipefail\nexec env {exports} {shlex.join(command)}\n",
        encoding="utf-8",
    )
    path.chmod(0o700)


def isolated_environment(home: Path) -> dict[str, str]:
    values = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "TERM": "xterm-256color",
        "NO_COLOR": "0",
        "TMPDIR": str(home / "tmp"),
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


def write_demo_project(work: Path) -> None:
    """Create the tiny real project used by every native demo trajectory."""

    source = work / "src"
    tests = work / "tests"
    private_directory(source)
    private_directory(tests)
    (source / "__init__.py").write_text("", encoding="utf-8")
    (source / "timeline.py").write_text(
        """\
from dataclasses import dataclass


@dataclass(frozen=True)
class Event:
    state: str
    start_ms: int
    end_ms: int


def coalesce(events: list[Event], gap_ms: int = 0) -> list[Event]:
    \"\"\"Merge equal neighboring states separated by at most ``gap_ms``.\"\"\"
    merged: list[Event] = []
    for event in events:
        if merged and merged[-1].state == event.state:
            gap = event.start_ms - merged[-1].end_ms
            if gap < gap_ms:
                previous = merged.pop()
                event = Event(event.state, previous.start_ms, event.end_ms)
        merged.append(event)
    return merged
""",
        encoding="utf-8",
    )
    (tests / "test_timeline.py").write_text(
        """\
import unittest

from src.timeline import Event, coalesce


class CoalesceTests(unittest.TestCase):
    def test_merges_events_inside_the_window(self) -> None:
        events = [Event("thinking", 0, 4), Event("thinking", 6, 9)]
        self.assertEqual(coalesce(events, gap_ms=3), [Event("thinking", 0, 9)])

    def test_keeps_events_outside_the_window(self) -> None:
        events = [Event("tool", 0, 4), Event("tool", 6, 9)]
        self.assertEqual(coalesce(events, gap_ms=1), events)


if __name__ == "__main__":
    unittest.main()
""",
        encoding="utf-8",
    )


def verify_demo_fix(work: Path) -> None:
    result = run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"],
        cwd=work,
        capture=True,
    )
    if "OK" not in result.stderr and "OK" not in result.stdout:
        raise RuntimeError("native target did not leave the focused test suite green")
    run(
        [
            sys.executable,
            "-c",
            "from src.timeline import Event, coalesce; "
            "assert coalesce([Event('x', 0, 4), Event('x', 4, 9)], 0) "
            "== [Event('x', 0, 9)]; "
            "assert len(coalesce([Event('x', 0, 4), Event('x', 5, 9)], 0)) == 2",
        ],
        cwd=work,
        capture=True,
    )
    tests = (work / "tests" / "test_timeline.py").read_text(encoding="utf-8")
    if tests.count("def test_") < 3:
        raise RuntimeError("native target did not add the requested regression test")


def wait_for_demo_fix(work: Path, timeout: float = 180) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            verify_demo_fix(work)
        except (OSError, RuntimeError, subprocess.CalledProcessError):
            time.sleep(0.5)
        else:
            return
    raise RuntimeError("native target never completed the requested fix")


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


def cast_records(path: Path) -> tuple[dict[str, Any], list[list[Any]]]:
    if not path.is_file():
        return {}, []
    lines = path.read_text(encoding="utf-8", errors="strict").splitlines()
    if not lines:
        return {}, []
    return json.loads(lines[0]), [json.loads(line) for line in lines[1:] if line]


def cast_last_timestamp(path: Path) -> float:
    _, events = cast_records(path)
    return float(events[-1][0]) if events else 0.0


class NativeCast:
    """A native CLI running in a real tmux terminal recorded by asciinema."""

    def __init__(
        self,
        name: str,
        launcher: Path,
        cwd: Path,
        raw_cast: Path,
    ) -> None:
        self.session = f"sm-{name}-{os.getpid()}"
        self.raw_cast = raw_cast
        subprocess.run(
            ["tmux", "kill-session", "-t", self.session],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        run(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                self.session,
                "-x",
                str(COLS),
                "-y",
                str(ROWS),
                "-c",
                str(cwd),
            ]
        )
        command = (
            f"{shlex.quote(require_program('asciinema'))} rec "
            f"{shlex.quote(str(raw_cast))} --overwrite -c "
            f"{shlex.quote(str(launcher))}"
        )
        run(["tmux", "send-keys", "-t", self.session, "-l", command])
        run(["tmux", "send-keys", "-t", self.session, "Enter"])

    def pane(self, history: int = 160) -> str:
        return run(
            [
                "tmux",
                "capture-pane",
                "-t",
                self.session,
                "-p",
                "-S",
                f"-{history}",
            ],
            capture=True,
        ).stdout

    def wait_for(self, pattern: str, timeout: float = 60) -> None:
        deadline = time.monotonic() + timeout
        trust_answered = False
        bypass_answered = False
        while time.monotonic() < deadline:
            pane = self.pane()
            if not trust_answered and re.search(
                r"trust the (?:contents|files)|Yes, continue", pane, re.I
            ):
                self.key("Enter")
                trust_answered = True
            if not bypass_answered and "Yes, I accept" in pane:
                self.key("Down")
                self.key("Enter")
                bypass_answered = True
            if re.search(pattern, pane, re.I | re.S):
                return
            time.sleep(0.4)
        sanitized = re.sub(r"[\w.+-]+@[\w.-]+", "[redacted-email]", self.pane())
        sanitized = sanitized.replace(str(Path.home()), "~")[-4000:]
        raise RuntimeError(f"{self.session} did not render /{pattern}/; pane={sanitized!r}")

    def key(self, value: str) -> None:
        run(["tmux", "send-keys", "-t", self.session, value])

    def type_line(self, value: str, delay: float = 0.035) -> None:
        for character in value:
            if character == ";":
                run(["tmux", "send-keys", "-t", self.session, "-H", "3b"])
            else:
                run(["tmux", "send-keys", "-t", self.session, "-l", character])
            time.sleep(delay)
        time.sleep(0.35)
        self.key("Enter")

    def repaint_start(self) -> float:
        before = cast_last_timestamp(self.raw_cast)
        run(
            [
                "tmux",
                "resize-window",
                "-t",
                self.session,
                "-x",
                str(COLS - 1),
                "-y",
                str(ROWS),
            ]
        )
        time.sleep(0.35)
        run(
            [
                "tmux",
                "resize-window",
                "-t",
                self.session,
                "-x",
                str(COLS),
                "-y",
                str(ROWS),
            ]
        )
        time.sleep(1.0)
        return before + 0.000001

    def wait_idle(self, timeout: float = 45) -> None:
        deadline = time.monotonic() + timeout
        stable = 0
        while time.monotonic() < deadline:
            pane = self.pane(40)
            busy = re.search(r"esc to interrupt", pane, re.I)
            stable = 0 if busy else stable + 1
            if stable >= 4:
                return
            time.sleep(0.5)
        raise RuntimeError(f"{self.session} never returned to an idle prompt")

    def finish(self, command: str) -> None:
        self.key("C-c")
        time.sleep(0.5)
        run(["tmux", "send-keys", "-t", self.session, "-l", command])
        self.key("Enter")
        time.sleep(1.2)
        subprocess.run(
            ["tmux", "kill-session", "-t", self.session],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def message_text(record: dict[str, Any], native_format: str) -> tuple[str, str] | None:
    if native_format == "claude" and record.get("type") in {"user", "assistant"}:
        role = record["type"]
        content = record.get("message", {}).get("content")
    elif native_format == "pi" and record.get("type") == "message":
        role = record.get("message", {}).get("role")
        content = record.get("message", {}).get("content")
    elif native_format == "codex" and record.get("type") == "response_item":
        payload = record.get("payload", {})
        if payload.get("type") != "message":
            return None
        role = payload.get("role")
        content = payload.get("content")
    else:
        return None
    texts: list[str] = []
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                texts.append(block["text"])
    if role not in {"user", "assistant"} or not texts:
        return None
    return role, "\n".join(texts)


def native_messages(path: Path, native_format: str) -> list[tuple[str, str]]:
    if not path.is_file():
        return []
    try:
        records = [json.loads(line) for line in path.read_text().splitlines() if line]
    except (OSError, json.JSONDecodeError):
        return []
    return [value for record in records if (value := message_text(record, native_format))]


def wait_for_turn(
    path: Path,
    prompt: str,
    native_format: str,
    timeout: float = 180,
) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        messages = native_messages(path, native_format)
        matching = [
            i for i, (role, text) in enumerate(messages) if role == "user" and prompt in text
        ]
        if matching:
            for role, text in messages[matching[-1] + 1 :]:
                if role == "assistant" and text.strip():
                    return text.strip()
        time.sleep(0.4)
    raise RuntimeError(f"{native_format} did not persist a reply after the demo prompt")


def trim_cast(source: Path, target: Path, start: float, end: float) -> None:
    header, events = cast_records(source)
    kept = [event for event in events if start <= float(event[0]) <= end]
    if not kept:
        raise RuntimeError(f"no terminal events survived trim for {source.name}")
    rebased = [
        [
            round(float(event[0]) - start, 6),
            event[1],
            sanitize_terminal_output(event[2]),
        ]
        for event in kept
    ]
    rebased.append([round(float(rebased[-1][0]) + 3.0, 6), "o", "\x1b[0m"])
    target.write_text(
        "\n".join(json.dumps(value, ensure_ascii=False) for value in (header, *rebased)) + "\n",
        encoding="utf-8",
    )
    target.chmod(0o600)


def sanitize_terminal_output(value: str) -> str:
    """Remove account-plan chrome while leaving the native terminal untouched."""

    value = re.sub(r"You've used[^\r\n]*", "", value, flags=re.I)
    return re.sub(r"\s*·\s*Claude Max", "", value, flags=re.I)


def prepare_claude(root: Path) -> tuple[dict[str, str], Path]:
    config = root / "claude"
    home = root / "claude-home"
    private_directory(config)
    private_copy(Path.home() / ".claude" / ".credentials.json", config / ".credentials.json")
    if (Path.home() / ".claude.json").is_file():
        private_copy(Path.home() / ".claude.json", config / ".claude.json")
    environment = isolated_environment(home)
    environment.update(
        {
            "CLAUDE_CONFIG_DIR": str(config),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
        }
    )
    return environment, config


def claude_session_path(config: Path, work: Path) -> Path:
    encoded = re.sub(r"[^A-Za-z0-9]", "-", str(work.resolve())) or "-"
    return config / "projects" / encoded / f"{CLAUDE_SESSION_ID}.jsonl"


def record_claude(repo: Path, root: Path, work: Path) -> tuple[Path, Path, str]:
    environment, config = prepare_claude(root)
    launcher = root / "launch-claude.sh"
    write_launcher(
        launcher,
        environment,
        [
            require_program("claude"),
            "--safe-mode",
            "--permission-mode",
            "bypassPermissions",
            "--effort",
            "low",
            "--session-id",
            CLAUDE_SESSION_ID,
            "--name",
            "Fix event coalescing",
        ],
    )
    raw = root / "claude-raw.cast"
    final = root / "claude.cast"
    session_path = claude_session_path(config, work)
    recorder = NativeCast("claude", launcher, work, raw)
    try:
        recorder.wait_for(
            r"esc to interrupt|for shortcuts|for agents|Try \"",
            80,
        )
        recorder.type_line(SEED_PROMPT)
        wait_for_turn(session_path, SEED_PROMPT, "claude")
        recorder.wait_idle(80)
        start = recorder.repaint_start()
        recorder.type_line(SOURCE_PROMPT)
        reply = wait_for_turn(session_path, SOURCE_PROMPT, "claude")
        recorder.wait_idle(80)
        time.sleep(3)
        end = cast_last_timestamp(raw)
    finally:
        recorder.finish("/exit")
    trim_cast(raw, final, start, end)
    capture_text = final.read_text(encoding="utf-8", errors="replace").lower()
    if "usage limit" in capture_text or "session limit" in capture_text:
        raise RuntimeError("Claude account-limit chrome appeared in the public capture")
    if "gap" not in reply.lower() or "<=" not in reply:
        raise RuntimeError("Claude did not produce the expected boundary-bug review")
    return final, session_path, reply


def install_pi(root: Path) -> Path:
    prefix = root / "pi-cli"
    run(
        [
            require_program("npm"),
            "install",
            "--silent",
            "--prefix",
            str(prefix),
            "@earendil-works/pi-coding-agent@0.80.6",
        ],
        timeout=180,
    )
    binary = prefix / "node_modules" / ".bin" / "pi"
    version = run([str(binary), "--version"], capture=True).stdout.strip()
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
    result = json.loads(run(command, capture=True).stdout)
    if result.get("target_format") != target:
        raise RuntimeError(f"unexpected {target} import result")
    return Path(result["output"])


def record_pi(
    repo: Path,
    root: Path,
    work: Path,
    source: Path,
    binary: Path,
) -> tuple[Path, str]:
    target_home = root / "pi"
    private_directory(target_home)
    target = run_import(repo, source, "pi", target_home, PI_SESSION_ID, work)
    write_private_json(
        target_home / "auth.json",
        {"openai-codex": translate_codex_oauth(Path.home() / ".codex" / "auth.json")},
    )
    environment = isolated_environment(root / "pi-home")
    environment.update(
        {
            "PI_CODING_AGENT_DIR": str(target_home),
            "PI_TELEMETRY": "0",
            "PI_SKIP_VERSION_CHECK": "1",
        }
    )
    launcher = root / "launch-pi.sh"
    write_launcher(
        launcher,
        environment,
        [
            str(binary),
            "--provider",
            "openai-codex",
            "--model",
            "gpt-5.6-luna",
            "--thinking",
            "low",
            "--session",
            str(target),
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-context-files",
            "--approve",
        ],
    )
    raw = root / "pi-raw.cast"
    final = root / "pi.cast"
    recorder = NativeCast("pi", launcher, work, raw)
    try:
        recorder.wait_for(r"gap_ms|boundary bug|touching events", 80)
        start = recorder.repaint_start()
        recorder.key("PageUp")
        time.sleep(1.1)
        recorder.key("PageDown")
        time.sleep(1.1)
        recorder.type_line(TARGET_PROMPTS["pi"])
        reply = wait_for_turn(target, TARGET_PROMPTS["pi"], "pi")
        wait_for_demo_fix(work)
        recorder.wait_idle(120)
        time.sleep(3)
        end = cast_last_timestamp(raw)
    finally:
        recorder.finish("/exit")
    trim_cast(raw, final, start, end)
    verify_demo_fix(work)
    return final, reply


def record_codex(
    repo: Path,
    root: Path,
    work: Path,
    source: Path,
) -> tuple[Path, str]:
    target_home = root / "codex"
    private_directory(target_home)
    target = run_import(repo, source, "codex", target_home, CODEX_SESSION_ID, Path("/work"))
    private_copy(Path.home() / ".codex" / "auth.json", target_home / "auth.json")
    private_directory(root / "codex-os-home")
    launcher = root / "launch-codex.sh"
    uid_gid = f"{os.getuid()}:{os.getgid()}"
    write_launcher(
        launcher,
        {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TERM": "xterm-256color"},
        [
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
            f"{root.resolve()}:/state",
            "-v",
            f"{work.resolve()}:/work",
            "-w",
            "/work",
            IMAGE_ID,
            "codex",
            "--dangerously-bypass-approvals-and-sandbox",
            "resume",
            "--no-alt-screen",
            "--disable",
            "apps",
            "--disable",
            "plugins",
            "--disable",
            "browser_use",
            CODEX_SESSION_ID,
        ],
    )
    raw = root / "codex-raw.cast"
    final = root / "codex.cast"
    recorder = NativeCast("codex", launcher, work, raw)
    try:
        recorder.wait_for(r"gap_ms|boundary bug|touching events", 80)
        start = recorder.repaint_start()
        recorder.type_line(TARGET_PROMPTS["codex"])
        reply = wait_for_turn(target, TARGET_PROMPTS["codex"], "codex")
        wait_for_demo_fix(work)
        recorder.wait_idle(120)
        time.sleep(3)
        end = cast_last_timestamp(raw)
    finally:
        recorder.finish("/quit")
    trim_cast(raw, final, start, end)
    verify_demo_fix(work)
    return final, reply


def write_timeline(
    source: Path,
    target: Path,
    *,
    offset: float,
    duration: float,
    speed: float = 1.0,
) -> None:
    header, events = cast_records(source)
    shifted = [[round(float(event[0]) / speed + offset, 6), event[1], event[2]] for event in events]
    shifted.append([round(duration, 6), "o", "\x1b[0m"])
    target.write_text(
        "\n".join(json.dumps(value, ensure_ascii=False) for value in (header, *shifted)) + "\n",
        encoding="utf-8",
    )
    target.chmod(0o600)


def ffprobe(path: Path, field: str) -> str:
    return run(
        [
            require_program("ffprobe"),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            f"stream={field}",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture=True,
    ).stdout.strip()


def render_terminal(cast: Path, gif: Path) -> None:
    run(
        [
            require_program("agg"),
            "--quiet",
            "--theme",
            "github-dark",
            "--font-size",
            str(TERMINAL_FONT_SIZE),
            "--line-height",
            str(TERMINAL_LINE_HEIGHT),
            "--fps-cap",
            "20",
            "--idle-time-limit",
            "3600",
            "--no-loop",
            str(cast),
            str(gif),
        ]
    )


def render_frame(cast: Path, output: Path, scratch: Path) -> None:
    animation = scratch / f"{output.stem}.gif"
    run(
        [
            require_program("agg"),
            "--quiet",
            "--theme",
            "github-dark",
            "--font-size",
            str(TERMINAL_FONT_SIZE),
            "--line-height",
            str(TERMINAL_LINE_HEIGHT),
            "--select",
            "100%",
            "--last-frame-duration",
            "1",
            str(cast),
            str(animation),
        ]
    )
    run(
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
        ]
    )


def compose(
    source_cast: Path,
    target_cast: Path,
    target_name: str,
    output_gif: Path,
    output_mp4: Path,
    scratch: Path,
) -> None:
    source_duration = cast_last_timestamp(source_cast) / SOURCE_PLAYBACK_SPEED
    target_duration = cast_last_timestamp(target_cast)
    offset = source_duration + 1.8
    duration = offset + target_duration
    left_cast = scratch / f"{target_name}-left.cast"
    right_cast = scratch / f"{target_name}-right.cast"
    write_timeline(
        source_cast,
        left_cast,
        offset=0,
        duration=duration,
        speed=SOURCE_PLAYBACK_SPEED,
    )
    write_timeline(target_cast, right_cast, offset=offset, duration=duration)
    left_gif = scratch / f"{target_name}-left.gif"
    right_gif = scratch / f"{target_name}-right.gif"
    render_terminal(left_cast, left_gif)
    render_terminal(right_cast, right_gif)
    width = int(ffprobe(left_gif, "width"))
    height = int(ffprobe(left_gif, "height"))
    if (width, height) != (
        int(ffprobe(right_gif, "width")),
        int(ffprobe(right_gif, "height")),
    ):
        raise RuntimeError("native demo panes rendered at different sizes")
    gap = 28
    left_x = (MASTER_WIDTH - 2 * width - gap) // 2
    right_x = left_x + width + gap
    pane_y = 330
    label_y = pane_y - 58
    label = "Pi 0.80.6" if target_name == "pi" else f"Codex {CODEX_VERSION}"
    font = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    bold = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    footer_y = pane_y + height + 34
    headline = f"Start in Claude Code. Continue in {label}."
    subline = "The same session, resumed in a different native terminal."
    filters = (
        f"color=c=0x0d1117:s={MASTER_WIDTH}x{MASTER_HEIGHT}:d={duration:.3f}[bg];"
        f"[0:v]fps=24,tpad=stop_mode=clone:stop_duration=90,"
        f"trim=duration={duration:.3f},setpts=PTS-STARTPTS[L];"
        f"[1:v]fps=24,tpad=stop_mode=clone:stop_duration=90,"
        f"trim=duration={duration:.3f},setpts=PTS-STARTPTS[R];"
        f"[bg][L]overlay=x={left_x}:y={pane_y}:shortest=1[a];"
        f"[a][R]overlay=x={right_x}:y={pane_y}[b];"
        f"[b]drawtext=fontfile={bold}:text='{headline}':"
        "fontsize=52:fontcolor=0xe6edf3:x=80:y=55,"
        f"drawtext=fontfile={font}:text='{subline}':"
        "fontsize=30:fontcolor=0x8b949e:x=80:y=128,"
        f"drawbox=x=80:y=205:w={MASTER_WIDTH - 160}:h=2:color=0x21262d:t=fill,"
        f"drawtext=fontfile={bold}:text='CLAUDE CODE · SOURCE':"
        f"fontsize=27:fontcolor=0xe6edf3:x={left_x}:y={label_y},"
        f"drawtext=fontfile={bold}:text='{label.upper()} · MIGRATED':"
        f"fontsize=27:fontcolor=0xb8f94a:x={right_x}:y={label_y},"
        f"drawtext=fontfile={font}:text='SOURCE SESSION · UNCHANGED':"
        f"fontsize=22:fontcolor=0x8b949e:x={left_x}:y={footer_y},"
        f"drawtext=fontfile={font}:text='NATIVE TARGET · CONTINUED':"
        f"fontsize=22:fontcolor=0x8b949e:x={right_x}:y={footer_y},"
        "format=yuv420p[v]"
    )
    run(
        [
            require_program("ffmpeg"),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ignore_loop",
            "1",
            "-i",
            str(left_gif),
            "-ignore_loop",
            "1",
            "-i",
            str(right_gif),
            "-filter_complex",
            filters,
            "-map",
            "[v]",
            "-t",
            f"{duration:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            "slow",
            "-crf",
            "18",
            "-movflags",
            "+faststart",
            str(output_mp4),
        ],
        timeout=300,
    )
    run(
        [
            require_program("ffmpeg"),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(output_mp4),
            "-vf",
            f"fps=10,scale={GIF_WIDTH}:-2:flags=lanczos,split[s0][s1];"
            "[s0]palettegen=max_colors=112[p];[s1][p]paletteuse=dither=bayer",
            "-loop",
            "0",
            str(output_gif),
        ],
        timeout=300,
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
        "Reply with exactly",
        "weekly limit",
        "Claude Max",
    )
    data = path.read_bytes()
    for value in forbidden:
        if value.encode() in data:
            raise RuntimeError(f"private or test-shaped marker leaked into {path.name}")


def main() -> int:
    if os.environ.get("MIGRATE_NATIVE_CAPTURE_AUTH") != "1":
        raise RuntimeError("set MIGRATE_NATIVE_CAPTURE_AUTH=1 to authorize disposable OAuth copies")
    for name in ("agg", "asciinema", "claude", "docker", "ffmpeg", "ffprobe", "npm", "tmux"):
        require_program(name)
    if DEMO_ROOT.exists() or DEMO_ROOT.is_symlink():
        raise RuntimeError(f"neutral demo root already exists: {DEMO_ROOT}")
    repo = Path(__file__).resolve().parent.parent
    output = Path(sys.argv[1]).resolve() if len(sys.argv) == 2 else repo / "docs" / "assets"
    output.mkdir(parents=True, exist_ok=True)
    image = run(
        ["docker", "image", "inspect", IMAGE_ID, "--format", "{{.Id}}"],
        capture=True,
    ).stdout.strip()
    if image != IMAGE_ID:
        raise RuntimeError(f"unexpected Docker image identity: {image}")
    private_directory(DEMO_ROOT)
    try:
        work = DEMO_ROOT / "project"
        private_directory(work)
        write_demo_project(work)
        pi_binary = install_pi(DEMO_ROOT)
        source_cast, source_session, source_reply = record_claude(repo, DEMO_ROOT, work)
        write_demo_project(work)
        pi_cast, pi_reply = record_pi(repo, DEMO_ROOT, work, source_session, pi_binary)
        write_demo_project(work)
        codex_cast, codex_reply = record_codex(repo, DEMO_ROOT, work, source_session)
        compose(
            source_cast,
            pi_cast,
            "pi",
            output / "demo-pi.gif",
            output / "demo-pi.mp4",
            DEMO_ROOT,
        )
        compose(
            source_cast,
            codex_cast,
            "codex",
            output / "demo-codex.gif",
            output / "demo-codex.mp4",
            DEMO_ROOT,
        )
        render_frame(source_cast, output / "demo-before.png", DEMO_ROOT)
        render_frame(pi_cast, output / "demo-after-pi.png", DEMO_ROOT)
        render_frame(codex_cast, output / "demo-after-codex.png", DEMO_ROOT)
        shutil.copyfile(source_cast, output / "demo-claude.cast")
        shutil.copyfile(pi_cast, output / "demo-pi.cast")
        shutil.copyfile(codex_cast, output / "demo-codex.cast")
        shutil.copyfile(output / "demo-pi.gif", output / "demo.gif")
        shutil.copyfile(output / "demo-pi.mp4", output / "demo.mp4")
        shutil.copyfile(output / "demo-before.png", output / "demo.png")
        shutil.copyfile(output / "demo-after-codex.png", output / "demo-after.png")
        for path in output.glob("demo*.*"):
            path.chmod(0o644)
            if path.suffix in {".cast", ".gif", ".mp4", ".png"}:
                assert_public_capture(path)
        print(
            json.dumps(
                {
                    "capture_method": "tmux+asciinema",
                    "claude_native_turns": 2,
                    "codex_native_continuations": 1,
                    "codex_reply_characters": len(codex_reply),
                    "codex_version": CODEX_VERSION,
                    "conversation": "real boundary-bug handoff",
                    "pi_native_continuations": 1,
                    "pi_reply_characters": len(pi_reply),
                    "pi_version": "0.80.6",
                    "playback": "coordinated native casts",
                    "resolution": f"{MASTER_WIDTH}x{MASTER_HEIGHT}",
                    "private_workspace_removed": True,
                    "source_reply_characters": len(source_reply),
                },
                indent=2,
                sort_keys=True,
            )
        )
    finally:
        for session in (
            f"sm-claude-{os.getpid()}",
            f"sm-pi-{os.getpid()}",
            f"sm-codex-{os.getpid()}",
        ):
            subprocess.run(
                ["tmux", "kill-session", "-t", session],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        terminate_recording_processes()
        remove_private_tree(DEMO_ROOT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
