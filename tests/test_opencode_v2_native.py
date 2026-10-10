"""Pinned v2 native import/export, cold reopen and localhost-only continuation."""

import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from offline_provider import offline_provider

from session_migrate.conversion import ConversionOptions, convert_session, install_opencode_artifact
from session_migrate.formats import codex
from session_migrate.model import EventKind, TargetFormat


@pytest.mark.parametrize("compaction", [False, True])
def test_pinned_v2_native_reopens_and_continues_same_session(tmp_path, compaction):
    binary = os.environ.get("SESSION_MIGRATE_TEST_OPENCODE_V2")
    if not binary:
        pytest.skip("set SESSION_MIGRATE_TEST_OPENCODE_V2 to a stock 2.0.22 or 2.0.23 executable")
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "OPENCODE_DISABLE_AUTOUPDATE": "true"}
    for key in [
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "TMPDIR",
    ]:
        folder = tmp_path / key.lower()
        folder.mkdir()
        env[key] = str(folder)
    project = tmp_path / "project"
    project.mkdir()

    def run(arguments):
        result = subprocess.run(
            [binary, *arguments], cwd=project, env=env, capture_output=True, text=True, timeout=90
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    release = run(["--version"]).strip().removeprefix("opencode v")
    assert release in {"2.0.22", "2.0.23"}
    source = codex.parse(Path(__file__).parent / "fixtures/codex-0.144.4/basic.jsonl")
    if not compaction:
        source = replace(
            source, events=tuple(e for e in source.events if e.kind != EventKind.COMPACTION)
        )
    with offline_provider("PINNED-V2-CONTINUED-BETA-2048") as provider:
        config = project / "opencode.json"
        config.write_text(
            json.dumps(
                {
                    "warming": False,
                    "disabled_providers": ["opencode"],
                    "providers": {
                        "fixture": {
                            "package": "@opencode/ai/providers/openai-compatible",
                            "settings": {
                                "apiKey": "fake-only",
                                "baseURL": f"http://127.0.0.1:{provider.server_port}/v1",
                            },
                            "models": {"test": {"limit": {"context": 64000, "output": 4096}}},
                        }
                    },
                }
            )
        )
        env["OPENCODE_CONFIG"] = str(config)
        artifact = convert_session(
            source,
            ConversionOptions(
                target_format=TargetFormat.OPENCODE,
                target_cli_version=release,
                cwd=project,
                model_provider="fixture",
                model="test",
            ),
        )
        install_opencode_artifact(
            artifact, manifest_path=tmp_path / "manifest.json", target_cli=Path(binary), environ=env
        )
        before = json.loads(run(["session", "export", artifact.session_id, "--standalone"]))
        # Every invocation is a new process: continuation and final export cold-reopen storage.
        run(
            [
                "run",
                "--session",
                artifact.session_id,
                "--model",
                "fixture/test",
                "--standalone",
                "--format",
                "json",
                "pinned native continuation probe",
            ]
        )
        after = json.loads(run(["session", "export", artifact.session_id, "--standalone"]))
        assert after["info"]["id"] == before["info"]["id"] == artifact.session_id
        assert len(after["messages"]) > len(before["messages"])
        assert any(
            c.get("text") == provider.reply
            for m in after["messages"]
            if m["type"] == "assistant"
            for c in m["content"]
            if c["type"] == "text"
        )
        assert provider.requests
        for path, body in provider.requests:
            assert path.endswith("/chat/completions")
            assert "BETA-2048" in json.dumps(body)
            assert "pinned native continuation probe" in json.dumps(body)
            if not compaction:
                assert any(
                    m.get("role") == "tool" and "/work" in json.dumps(m) for m in body["messages"]
                )
                assert "call_fixture_1" in json.dumps(body)
