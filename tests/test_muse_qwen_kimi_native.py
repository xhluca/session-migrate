"""Credential-free exact-client trajectories for Muse, Qwen Code, and Kimi Code."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from pathlib import Path

import pytest
from offline_provider import offline_provider, request_text

from session_migrate.conversion import (
    ConversionOptions,
    convert_session,
    install_kimi_artifact,
    target_import_paths,
    write_artifact,
)
from session_migrate.formats import claude, kimi, muse, qwen
from session_migrate.model import EventKind, Role, TargetFormat

FIXTURE = Path(__file__).parent / "fixtures/claude-2.1.209/basic.jsonl"
PROMPT = (
    "Continue from the imported history. In one short sentence, identify the "
    "file inspected earlier. Do not use tools."
)
EXPECTED_RECALL = "README"
OFFLINE_REPLY = "README.md was the file inspected earlier."


def _binary(variable: str, expected_version: str, *, version_flag: str = "--version") -> Path:
    value = os.environ.get(variable)
    if not value:
        pytest.skip(f"set {variable} to the exact pinned native binary")
    binary = Path(value)
    completed = subprocess.run(
        [str(binary), version_flag],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if completed.returncode != 0 or expected_version not in completed.stdout.strip():
        pytest.fail(f"{variable} is not the pinned {expected_version} binary")
    return binary


def _private_directories(tmp_path: Path, *names: str) -> tuple[Path, ...]:
    paths = tuple(tmp_path / name for name in names)
    for path in paths:
        path.mkdir(mode=0o700, parents=True)
    return paths


def _assistant_recalled(path: Path, parser: object) -> bool:
    session = parser(path)  # type: ignore[operator]
    return any(
        event.kind == EventKind.MESSAGE
        and event.role == Role.ASSISTANT
        and EXPECTED_RECALL in (event.text or "")
        for event in session.events
    )


def _replay(provider: object) -> str:
    requests = provider.requests  # type: ignore[attr-defined]
    return "\n".join(request_text(request) for _path, request in requests)


def _assert_replayed_import(replay: str) -> None:
    assert PROMPT in replay
    assert "ALPHA-1042" in replay
    assert "Synthetic fixture output" in replay


def test_qwen_0221_offline_resume_replays_imported_history(tmp_path: Path) -> None:
    binary = _binary("SESSION_MIGRATE_QWEN_BIN", qwen.PINNED_QWEN_VERSION)
    home, work, system_home = _private_directories(tmp_path, "qwen", "work", "home")
    artifact = convert_session(
        claude.parse(FIXTURE),
        ConversionOptions(
            target_format=TargetFormat.QWEN,
            session_id="51515151-5151-4515-8515-515151515151",
            cwd=work,
            model="session-migrate/offline-echo",
        ),
    )
    native_path, manifest_path = target_import_paths(artifact, home)
    write_artifact(artifact, output_path=native_path, manifest_path=manifest_path)
    before = native_path.read_bytes()

    with offline_provider(OFFLINE_REPLY) as provider:
        settings = {
            "$version": 4,
            "modelProviders": {
                "openai": [
                    {
                        "id": "session-migrate/offline-echo",
                        "name": "Session Migrate Offline Echo",
                        "envKey": "OFFLINE_API_KEY",
                        "baseUrl": f"http://127.0.0.1:{provider.server_address[1]}/v1",
                    }
                ]
            },
            "security": {"auth": {"selectedType": "openai"}},
            "model": {"name": "session-migrate/offline-echo"},
        }
        settings_path = home / "settings.json"
        settings_path.write_text(json.dumps(settings), encoding="utf-8")
        settings_path.chmod(0o600)
        environment = {
            "HOME": str(system_home),
            "QWEN_HOME": str(home),
            "OFFLINE_API_KEY": "credential-free-loopback",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "TERM": "dumb",
            "NO_COLOR": "1",
        }
        completed = subprocess.run(
            [
                str(binary),
                "--safe-mode",
                "--resume",
                artifact.session_id,
                "--model",
                "session-migrate/offline-echo",
                "--prompt",
                PROMPT,
                "--output-format",
                "json",
            ],
            cwd=work,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
        replay = _replay(provider)

    assert completed.returncode == 0, completed.stderr
    _assert_replayed_import(replay)
    after = native_path.read_bytes()
    assert len(after) > len(before) and after.startswith(before)
    assert _assistant_recalled(native_path, qwen.parse_session)


def test_kimi_0380_offline_resume_replays_imported_history(tmp_path: Path) -> None:
    binary = _binary("SESSION_MIGRATE_KIMI_BIN", kimi.PINNED_KIMI_VERSION)
    home, work, system_home = _private_directories(tmp_path, "kimi", "work", "home")
    artifact = convert_session(
        claude.parse(FIXTURE),
        ConversionOptions(
            target_format=TargetFormat.KIMI,
            session_id="62626262-6262-4626-8626-626262626262",
            cwd=work,
            model="session-migrate/offline-echo",
        ),
    )
    wire_path, _manifest_path = install_kimi_artifact(artifact, target_home=home)
    before = wire_path.read_bytes()

    with offline_provider(OFFLINE_REPLY) as provider:
        environment = {
            "HOME": str(system_home),
            "KIMI_CODE_HOME": str(home),
            "KIMI_MODEL_NAME": "session-migrate/offline-echo",
            "KIMI_MODEL_PROVIDER_TYPE": "openai",
            "KIMI_MODEL_BASE_URL": f"http://127.0.0.1:{provider.server_address[1]}/v1",
            "KIMI_MODEL_API_KEY": "credential-free-loopback",
            "CHOKIDAR_USEPOLLING": "1",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "TERM": "dumb",
            "NO_COLOR": "1",
        }
        completed = subprocess.run(
            [
                str(binary),
                "--session",
                artifact.session_id,
                "--model",
                "__kimi_env_model__",
                "--prompt",
                PROMPT,
                "--output-format",
                "stream-json",
            ],
            cwd=work,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
        replay = _replay(provider)

    assert completed.returncode == 0, completed.stderr
    _assert_replayed_import(replay)
    after = wire_path.read_bytes()
    assert len(after) > len(before) and after.startswith(before)
    assert _assistant_recalled(wire_path, kimi.parse_session)


def _unused_port() -> int:
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        return int(handle.getsockname()[1])


def _wait_for_port(process: subprocess.Popen[str], port: int) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail("muse-openrouter exited before accepting connections")
        with socket.socket() as handle:
            handle.settimeout(0.2)
            if handle.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    pytest.fail("muse-openrouter did not start in time")


def test_muse_021_offline_resume_replays_imported_history(tmp_path: Path) -> None:
    binary = _binary("SESSION_MIGRATE_MUSE_BIN", muse.PINNED_MUSE_VERSION)
    adapter = _binary("SESSION_MIGRATE_MUSE_OPENROUTER_BIN", "0.3.2")
    data_home, config_home, work, system_home = _private_directories(
        tmp_path, "data", "config", "work", "home"
    )
    target_home = data_home / "muse"
    artifact = convert_session(
        claude.parse(FIXTURE),
        ConversionOptions(
            target_format=TargetFormat.MUSE,
            session_id="73737373-7373-4737-8737-737373737373",
            cwd=work,
            model_provider="meta",
            model="meta/muse-glimmer-30b",
        ),
    )
    native_path, manifest_path = target_import_paths(artifact, target_home)
    write_artifact(artifact, output_path=native_path, manifest_path=manifest_path)
    before = native_path.read_bytes()
    port = _unused_port()
    credential = tmp_path / "offline-api-key"
    credential.write_text(f"sk-or-v1-{'0' * 64}\n")
    credential.chmod(0o600)
    adapter_environment = {
        "HOME": str(system_home),
        "XDG_CONFIG_HOME": str(config_home),
        "MUSE_CODE_OPENROUTER_CREDENTIAL_FILE": str(credential),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "NO_COLOR": "1",
    }

    with offline_provider(OFFLINE_REPLY) as provider:
        server = subprocess.Popen(
            [
                str(adapter),
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--model",
                "meta/muse-glimmer-30b",
                "--upstream",
                f"http://127.0.0.1:{provider.server_address[1]}/v1",
                "--log-level",
                "warning",
            ],
            env=adapter_environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            _wait_for_port(server, port)
            environment = {
                **adapter_environment,
                "XDG_DATA_HOME": str(data_home),
                "MUSE_NO_AUTO_UPDATE": "1",
                "META_API_KEY": "local-adapter-placeholder",
                "TERM": "dumb",
            }
            completed = subprocess.run(
                [
                    str(binary),
                    "exec",
                    "--session-id",
                    artifact.session_id,
                    "--provider",
                    "meta",
                    "--base-url",
                    f"http://127.0.0.1:{port}",
                    "--model",
                    "meta/muse-glimmer-30b",
                    "--reasoning-effort",
                    "minimal",
                    "--workspace",
                    str(work),
                    "--disable-shell",
                    "--disable-write",
                    "--disable-web-tools",
                    "--json",
                    PROMPT,
                ],
                cwd=work,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=180,
            )
            replay = _replay(provider)
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)

    assert completed.returncode == 0, completed.stderr
    _assert_replayed_import(replay)
    after = native_path.read_bytes()
    assert len(after) > len(before) and after.startswith(before)
    assert _assistant_recalled(native_path, muse.parse_session)
