#!/usr/bin/env python3
"""Run a disposable Pi TUI trajectory with copied Codex OAuth credentials.

The harness translates the current Codex OAuth record into Pi's documented
auth shape, launches the actual Pi TUI in a pseudoterminal, submits two
synthetic prompts, verifies two complete turns and append-only persistence, and
removes all copied credentials and transcripts. It never prints token or model
response values.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import pty
import select
import signal
import struct
import subprocess
import tempfile
import termios
import time
from pathlib import Path
from typing import Any

from session_migrate.conversion import ConversionOptions, convert_session
from session_migrate.errors import SessionMigrateError
from session_migrate.formats import codex, pi
from session_migrate.jsonl import write_private_atomic
from session_migrate.model import EventKind, Role, TargetFormat

FIRST_PROMPT = "Reply with exactly one short lowercase word and do not use tools."
SECOND_PROMPT = "Reply with exactly one different short lowercase word and do not use tools."


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-auth", type=Path, required=True)
    parser.add_argument("--pi-bin", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()

    require_version(args.pi_bin, pi.PINNED_PI_VERSION)
    credential = translated_credential(args.codex_auth)
    workspace: Path | None = None
    with tempfile.TemporaryDirectory(prefix="session-migrate-live-pi-tui-") as directory:
        workspace = Path(directory)
        os.chmod(workspace, 0o700)
        work = workspace / "work"
        work.mkdir(mode=0o700)
        pi_agent = workspace / "pi-agent"
        pi_agent.mkdir(mode=0o700)
        pi_auth = pi_agent / "auth.json"
        write_private_json(pi_auth, {"openai-codex": credential})
        pi_home = workspace / "pi-home"
        environment = isolated_environment(pi_home, workspace)
        environment["PI_CODING_AGENT_DIR"] = str(pi_agent)
        environment["PI_TELEMETRY"] = "0"

        session_path = prepare_pi_session(workspace, work)
        before = session_path.read_bytes()
        log_path = workspace / "pi-tui.log"
        run_tui(
            [
                str(args.pi_bin),
                "--provider",
                "openai-codex",
                "--model",
                "gpt-5.6-luna",
                "--thinking",
                "low",
                "--session",
                str(session_path),
                "--no-tools",
                "--no-extensions",
                "--no-skills",
                "--no-prompt-templates",
                "--no-context-files",
                "--approve",
            ],
            cwd=work,
            environment=environment,
            log_path=log_path,
            session_path=session_path,
            timeout=args.timeout,
        )
        after = session_path.read_bytes()
        if len(after) <= len(before) or not after.startswith(before):
            raise RuntimeError("Pi TUI changed the imported session prefix")
        assert_trajectory(session_path)
        for path in (pi_auth, log_path, session_path):
            if not path.is_file() or path.stat().st_mode & 0o077:
                raise RuntimeError("private Pi TUI artifact permissions changed")

    if workspace is None or workspace.exists():
        raise RuntimeError("private authenticated Pi TUI workspace was not removed")
    print(
        json.dumps(
            {
                "actual_tuis_launched": 1,
                "live_codex_oauth_steps": 2,
                "pi_version": pi.PINNED_PI_VERSION,
                "source": "sanitized_codex_fixture",
                "exact_imported_prefixes": 1,
                "credentials_copied_from_codex": True,
                "credential_values_reported": False,
                "private_workspace_removed": True,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def translated_credential(path: Path) -> dict[str, Any]:
    if path.stat().st_mode & 0o077:
        raise RuntimeError("Codex auth file must not be group/world accessible")
    value = json.loads(path.read_text(encoding="utf-8"))
    tokens = value.get("tokens") if isinstance(value, dict) else None
    if not isinstance(tokens, dict):
        raise RuntimeError("Codex auth file has no token object")
    access = tokens.get("access_token")
    refresh = tokens.get("refresh_token")
    account = tokens.get("account_id")
    if not all(isinstance(item, str) and item for item in (access, refresh, account)):
        raise RuntimeError("Codex OAuth record is incomplete")
    return {
        "type": "oauth",
        "access": access,
        "refresh": refresh,
        "expires": jwt_expiry_ms(access),
        "accountId": account,
    }


def jwt_expiry_ms(token: str) -> int:
    try:
        payload = token.split(".")[1]
        padding = "=" * (-len(payload) % 4)
        value = json.loads(base64.urlsafe_b64decode(payload + padding))
        expiry = value["exp"]
    except (IndexError, KeyError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Codex access token has no usable expiry metadata") from exc
    if not isinstance(expiry, int) or expiry <= int(time.time()):
        raise RuntimeError("Codex access token is expired")
    return expiry * 1000


def write_private_json(path: Path, value: dict[str, Any]) -> None:
    write_private_atomic(
        path,
        (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode(),
    )


def isolated_environment(home: Path, temporary: Path) -> dict[str, str]:
    values = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "TERM": "xterm-256color",
        "NO_COLOR": "1",
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
        Path(values[key]).mkdir(parents=True, mode=0o700, exist_ok=True)
    for key in ("LANG", "LC_ALL", "LC_CTYPE", "SSL_CERT_FILE", "SSL_CERT_DIR"):
        if value := os.environ.get(key):
            values[key] = value
    return values


def prepare_pi_session(workspace: Path, work: Path) -> Path:
    source = codex.parse(Path("tests/fixtures/codex-0.144.4/basic.jsonl"))
    artifact = convert_session(
        source,
        ConversionOptions(
            target_format=TargetFormat.PI,
            session_id="73000000-0000-4000-8000-000000000001",
            cwd=work,
            model_provider="openai-codex",
            model="gpt-5.6-luna",
        ),
    )
    path = workspace / "codex-to-pi.jsonl"
    write_private_atomic(path, artifact.native_bytes)
    return path


def run_tui(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    log_path: Path,
    session_path: Path,
    timeout: int,
) -> None:
    descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    pid, master = pty.fork()
    if pid == 0:
        os.chdir(cwd)
        os.execve(command[0], command, environment)
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    deadline = time.monotonic() + timeout
    base_users, base_assistants = trajectory_counts(session_path)
    try:
        time.sleep(3)
        os.write(master, FIRST_PROMPT.encode() + b"\r")
        read_until_native_turn(
            master,
            descriptor,
            session_path,
            base_users + 1,
            base_assistants + 1,
            deadline,
        )
        os.write(master, SECOND_PROMPT.encode() + b"\r")
        read_until_native_turn(
            master,
            descriptor,
            session_path,
            base_users + 2,
            base_assistants + 2,
            deadline,
        )
        os.write(master, b"\x03")
        time.sleep(1)
        os.write(master, b"\x03")
        wait_for_exit(pid, 10)
    finally:
        os.close(descriptor)
        os.close(master)
        try:
            finished, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            finished = pid
        if not finished:
            os.kill(pid, signal.SIGTERM)
            if not wait_for_exit(pid, 5):
                os.kill(pid, signal.SIGKILL)
                wait_for_exit(pid, 5)


def read_until_native_turn(
    master: int,
    log_descriptor: int,
    session_path: Path,
    expected_users: int,
    expected_assistants: int,
    deadline: float,
) -> None:
    while time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], 0.5)
        if not ready:
            continue
        try:
            chunk = os.read(master, 65536)
        except OSError:
            break
        if not chunk:
            break
        os.write(log_descriptor, chunk)
        users, assistants = trajectory_counts(session_path)
        if users >= expected_users and assistants >= expected_assistants:
            return
    raise RuntimeError("Pi TUI did not persist the expected native turn")


def wait_for_exit(pid: int, seconds: int) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            finished, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return True
        if finished:
            return True
        time.sleep(0.1)
    return False


def assert_trajectory(path: Path) -> None:
    session = pi.parse_session(path)
    messages = [
        event.text
        for event in session.events
        if event.kind == EventKind.MESSAGE
        and event.role in {Role.USER, Role.ASSISTANT}
        and event.text
    ]
    for prompt in (FIRST_PROMPT, SECOND_PROMPT):
        try:
            index = messages.index(prompt)
        except ValueError as exc:
            raise RuntimeError("Pi TUI prompt was not persisted") from exc
        if not any(
            value for value in messages[index + 1 :] if value not in {FIRST_PROMPT, SECOND_PROMPT}
        ):
            raise RuntimeError("Pi TUI reply was not persisted")


def trajectory_counts(path: Path) -> tuple[int, int]:
    try:
        session = pi.parse_session(path)
    except (OSError, SessionMigrateError):
        return 0, 0
    users = sum(
        event.kind == EventKind.MESSAGE and event.role == Role.USER and bool(event.text)
        for event in session.events
    )
    assistants = sum(
        event.kind == EventKind.MESSAGE and event.role == Role.ASSISTANT and bool(event.text)
        for event in session.events
    )
    return users, assistants


def require_version(binary: Path, expected: str) -> None:
    completed = subprocess.run(
        [str(binary), "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if completed.returncode != 0 or completed.stdout.strip() != expected:
        raise RuntimeError(f"authenticated Pi TUI harness requires version {expected}")


if __name__ == "__main__":
    raise SystemExit(main())
