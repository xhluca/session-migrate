#!/usr/bin/env python3
"""Run a disposable two-turn OpenCode TUI trajectory with Codex OAuth.

The opt-in harness translates the current Codex OAuth record into OpenCode's
documented ``openai`` credential shape only inside a mode-0700 temporary home.
It drives the pinned mini TUI through a pseudoterminal, verifies two complete
native SQLite turns, then removes the copied credential, transcript, and
terminal log. Token and response values are never printed.
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
import sqlite3
import struct
import subprocess
import tempfile
import termios
import time
from pathlib import Path
from typing import Any

from session_migrate.jsonl import write_private_atomic

PINNED_OPENCODE_VERSION = "1.17.20"
FIRST_PROMPT = "Reply with exactly one short lowercase word and do not use tools."
SECOND_PROMPT = "Reply with exactly one different short lowercase word and do not use tools."


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-auth", type=Path, required=True)
    parser.add_argument("--opencode-bin", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()

    require_version(args.opencode_bin, PINNED_OPENCODE_VERSION)
    credential = translated_credential(args.codex_auth)
    workspace: Path | None = None
    with tempfile.TemporaryDirectory(prefix="session-migrate-live-opencode-tui-") as name:
        workspace = Path(name)
        os.chmod(workspace, 0o700)
        work = workspace / "work"
        work.mkdir(mode=0o700)
        environment, data_home = isolated_environment(workspace)
        auth_dir = data_home / "opencode"
        auth_dir.mkdir(mode=0o700)
        auth_path = auth_dir / "auth.json"
        write_private_json(auth_path, {"openai": credential})
        log_path = workspace / "opencode-tui.log"
        database = data_home / "opencode" / "opencode.db"

        run_tui(
            [
                str(args.opencode_bin),
                str(work),
                "--pure",
                "--mini",
                "--model",
                "openai/gpt-5.5",
            ],
            cwd=work,
            environment=environment,
            log_path=log_path,
            database=database,
            timeout=args.timeout,
        )
        assert_trajectory(database)
        for path in (auth_path, log_path, database):
            if not path.is_file() or path.stat().st_mode & 0o077:
                raise RuntimeError("private OpenCode TUI artifact permissions changed")

    if workspace is None or workspace.exists():
        raise RuntimeError("private authenticated OpenCode TUI workspace was not removed")
    print(
        json.dumps(
            {
                "actual_mini_tuis_launched": 1,
                "live_codex_oauth_steps": 2,
                "opencode_version": PINNED_OPENCODE_VERSION,
                "native_sqlite_trajectories": 1,
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
        value = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
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


def isolated_environment(root: Path) -> tuple[dict[str, str], Path]:
    home = root / "home"
    data = root / "data"
    config = root / "config"
    cache = root / "cache"
    state = root / "state"
    temporary = root / "tmp"
    for path in (home, data, config, cache, state, temporary):
        path.mkdir(mode=0o700)
    values = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "TERM": "xterm-256color",
        "TMPDIR": str(temporary),
        "XDG_DATA_HOME": str(data),
        "XDG_CONFIG_HOME": str(config),
        "XDG_CACHE_HOME": str(cache),
        "XDG_STATE_HOME": str(state),
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
    }
    for key in ("LANG", "LC_ALL", "LC_CTYPE", "SSL_CERT_FILE", "SSL_CERT_DIR"):
        if value := os.environ.get(key):
            values[key] = value
    return values, data


def run_tui(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    log_path: Path,
    database: Path,
    timeout: int,
) -> None:
    descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    pid, master = pty.fork()
    if pid == 0:
        os.chdir(cwd)
        os.execve(command[0], command, environment)
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    deadline = time.monotonic() + timeout
    try:
        time.sleep(8)
        os.write(master, FIRST_PROMPT.encode() + b"\r")
        read_until_native_turn(master, descriptor, database, 1, deadline)
        os.write(master, SECOND_PROMPT.encode() + b"\r")
        read_until_native_turn(master, descriptor, database, 2, deadline)
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
    database: Path,
    expected_turns: int,
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
        users, assistants = trajectory_counts(database)
        if users >= expected_turns and assistants >= expected_turns:
            return
    raise RuntimeError("OpenCode TUI did not persist the expected native turn")


def trajectory_counts(path: Path) -> tuple[int, int]:
    if not path.is_file():
        return 0, 0
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1) as database:
            messages = [
                (row[0], json.loads(row[1]).get("role"))
                for row in database.execute("SELECT id,data FROM message")
            ]
            assistant_ids = {message_id for message_id, role in messages if role == "assistant"}
            complete_assistants = {
                message_id
                for message_id, encoded in database.execute("SELECT message_id,data FROM part")
                if message_id in assistant_ids
                and isinstance((value := json.loads(encoded)), dict)
                and value.get("type") == "text"
                and isinstance(value.get("text"), str)
                and value["text"]
            }
    except (OSError, sqlite3.Error, json.JSONDecodeError):
        return 0, 0
    return sum(role == "user" for _message_id, role in messages), len(complete_assistants)


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
    if not path.is_file():
        raise RuntimeError("OpenCode TUI did not create its native database")
    users, complete_assistants = trajectory_counts(path)
    if users < 2 or complete_assistants < 2:
        raise RuntimeError("OpenCode TUI did not persist two complete message turns")


def require_version(binary: Path, expected: str) -> None:
    completed = subprocess.run(
        [str(binary), "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if completed.returncode != 0 or completed.stdout.strip() != expected:
        raise RuntimeError(f"authenticated OpenCode TUI harness requires version {expected}")


if __name__ == "__main__":
    raise SystemExit(main())
