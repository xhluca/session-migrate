#!/usr/bin/env python3
"""Capture a source-native Muse Code 0.2.1 trajectory without paid inference.

The exact official client is driven only through ``muse exec``.  A pinned
``muse-code-openrouter`` adapter connects it to a credential-free loopback
Responses server.  Raw output is private capture material and must be passed
through ``sanitize-muse.py`` before anything is committed.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

VERSION = "0.2.1"
RELEASE = "0.2.1-R1215.1"
BUILD_SHA = "b3170a534f"
BINARY_SHA256 = "bfd8660b3a4fce67ab3287b0bd27ea64db1ee8472e8d7cb0f0f9aa8e083c9957"
BINARY_BYTES = 191_895_736
ADAPTER_VERSION = "0.3.2"
MODEL = "meta/muse-glimmer-30b"
SESSION_ID = "74747474-7474-4747-8747-747474747474"

REPO = Path(__file__).resolve().parents[2]
ASSETS = REPO / "tests/native_corpus/v1/assets"
SCENARIO = json.loads((REPO / "tests/native_corpus/v1/scenario.json").read_text())
INITIAL_PROMPT = (
    f"{SCENARIO['markers']['conversation']} Inspect timeline.py and CORPUS_NOTE.txt "
    "with the native file tool. Then try missing-corpus-file.txt so the native log includes "
    "a failed tool result. Explain the interval boundary and quote the note marker."
)
IMAGE_PROMPT = "Inspect the attached image and identify its shape and visible nonce."
RECALL_PROMPT = (
    "Without tools, recall the task marker, note marker, image marker, and failed filename."
)


@dataclass(frozen=True, slots=True)
class RunEvidence:
    label: str
    command_surface: str
    returncode: int
    request_start: int
    request_end: int
    request_media_types: tuple[str, ...]
    request_markers: tuple[str, ...]
    stdout_tail: str
    stderr_tail: str


class Upstream(ThreadingHTTPServer):
    requests: list[dict[str, Any]]
    lock: threading.Lock


def _walk(value: Any) -> Iterator[Any]:
    yield value
    if isinstance(value, list):
        for item in value:
            yield from _walk(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)


def _request_text(request: dict[str, Any]) -> str:
    return json.dumps(request.get("input", []), ensure_ascii=False, separators=(",", ":"))


def _latest_user_text(request: dict[str, Any]) -> str:
    inputs = request.get("input")
    if not isinstance(inputs, list):
        return ""
    for value in reversed(inputs):
        if isinstance(value, dict) and value.get("role") == "user":
            return json.dumps(value.get("content", ""), ensure_ascii=False, separators=(",", ":"))
    return ""


def _request_media_types(request: dict[str, Any]) -> tuple[str, ...]:
    found: set[str] = set()
    for value in _walk(request.get("input")):
        if not isinstance(value, str) or not value.startswith("data:") or ";base64," not in value:
            continue
        found.add(value[5:].split(";", 1)[0])
    return tuple(sorted(found))


def _read_tool(request: dict[str, Any]) -> tuple[str, str] | None:
    for value in _walk(request.get("tools")):
        if not isinstance(value, dict) or value.get("name") != "read_file":
            continue
        parameters = value.get("parameters")
        properties = parameters.get("properties", {}) if isinstance(parameters, dict) else {}
        if isinstance(properties, dict):
            if "path" in properties:
                return "read_file", "path"
            if "file_path" in properties:
                return "read_file", "file_path"
    return None


def _response_base(response_id: str, output: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": response_id,
        "object": "response",
        "created_at": 1_788_221_000,
        "status": "completed",
        "completed_at": 1_788_221_001,
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "max_output_tokens": None,
        "model": MODEL,
        "output": output,
        "parallel_tool_calls": True,
        "previous_response_id": None,
        "reasoning": {"effort": "minimal", "summary": None},
        "store": False,
        "temperature": None,
        "text": {"format": {"type": "text"}},
        "tool_choice": "auto",
        "tools": [],
        "top_p": None,
        "truncation": "disabled",
        "usage": {
            "input_tokens": 32,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 8,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 40,
        },
        "user": None,
        "metadata": {},
    }


def _text_events(text: str, response_id: str) -> list[dict[str, Any]]:
    item = {
        "id": f"msg_{response_id}",
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }
    response = _response_base(response_id, [item])
    return [
        {
            "type": "response.created",
            "response": {**response, "status": "in_progress", "output": []},
        },
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {**item, "status": "in_progress", "content": []},
        },
        {
            "type": "response.content_part.added",
            "item_id": item["id"],
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        },
        {
            "type": "response.output_text.delta",
            "item_id": item["id"],
            "output_index": 0,
            "content_index": 0,
            "delta": text,
        },
        {
            "type": "response.output_text.done",
            "item_id": item["id"],
            "output_index": 0,
            "content_index": 0,
            "text": text,
        },
        {
            "type": "response.content_part.done",
            "item_id": item["id"],
            "output_index": 0,
            "content_index": 0,
            "part": item["content"][0],
        },
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": response},
    ]


def _tool_events(request: dict[str, Any], call_id: str, filename: str) -> list[dict[str, Any]]:
    tool = _read_tool(request)
    if tool is None:
        raise ValueError("exact Muse request did not declare the expected read_file tool")
    name, path_argument = tool
    arguments = json.dumps({path_argument: filename}, separators=(",", ":"))
    item = {
        "id": f"fc_{call_id}",
        "type": "function_call",
        "status": "completed",
        "name": name,
        "call_id": call_id,
        "arguments": arguments,
    }
    response = _response_base(f"resp_{call_id}", [item])
    return [
        {
            "type": "response.created",
            "response": {**response, "status": "in_progress", "output": []},
        },
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {**item, "status": "in_progress", "arguments": ""},
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": item["id"],
            "output_index": 0,
            "delta": arguments,
        },
        {
            "type": "response.function_call_arguments.done",
            "item_id": item["id"],
            "output_index": 0,
            "arguments": arguments,
        },
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": response},
    ]


def response_events(request: dict[str, Any]) -> list[dict[str, Any]]:
    text = _request_text(request)
    latest_user = _latest_user_text(request)
    has_read_tool = _read_tool(request) is not None
    if "SANITIZED_NATIVE_CORPUS_RELOAD_FOLLOWUP" in latest_user:
        return _text_events("SANITIZED_NATIVE_CORPUS_RELOAD_OK", "resp_muse_reload")
    if "Without tools, recall" in latest_user:
        return _text_events(
            "Recall: SM_CORPUS_7319, COPPER_4821, BLUE_TRIANGLE_7319, and missing-corpus-file.txt.",
            "resp_muse_recall",
        )
    if "attached image" in latest_user:
        return _text_events(
            "The image contains a blue triangle labeled BLUE_TRIANGLE_7319.",
            "resp_muse_image",
        )
    if has_read_tool and "call_muse_missing" in text:
        return _text_events(
            "The boundary comparison is strict, the note marker is COPPER_4821, and the "
            "missing-file failure was retained.",
            "resp_muse_summary",
        )
    if has_read_tool and "call_muse_note" in text:
        return _tool_events(request, "call_muse_missing", "missing-corpus-file.txt")
    if has_read_tool and "call_muse_timeline" in text:
        return _tool_events(request, "call_muse_note", "CORPUS_NOTE.txt")
    if (
        has_read_tool
        and SCENARIO["markers"]["conversation"] in latest_user
        and "call_muse_timeline" not in text
    ):
        return _tool_events(request, "call_muse_timeline", "timeline.py")
    return _text_events("Loopback auxiliary response.", "resp_muse_aux")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def do_GET(self) -> None:  # noqa: N802
        body = json.dumps(
            {
                "data": [
                    {
                        "id": MODEL,
                        "name": "Muse native corpus loopback",
                        "context_length": 131_072,
                        "top_provider": {"max_completion_tokens": 4096},
                    }
                ]
            },
            separators=(",", ":"),
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        request = json.loads(self.rfile.read(length))
        if not isinstance(request, dict):
            self.send_error(400)
            return
        with self.server.lock:  # type: ignore[attr-defined]
            self.server.requests.append(request)  # type: ignore[attr-defined]
        events = response_events(request)
        body = (
            "".join(f"data: {json.dumps(event, separators=(',', ':'))}\n\n" for event in events)
            + "data: [DONE]\n\n"
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def loopback() -> Iterator[tuple[Upstream, Any]]:
    try:
        from muse_code_openrouter.proxy import MuseOpenRouterServer
    except ImportError as exc:
        raise SystemExit(
            f"install muse-code-openrouter=={ADAPTER_VERSION} in the active environment"
        ) from exc
    try:
        adapter_version = importlib.metadata.version("muse-code-openrouter")
    except importlib.metadata.PackageNotFoundError as exc:
        raise SystemExit(
            f"install muse-code-openrouter=={ADAPTER_VERSION} in the active environment"
        ) from exc
    if adapter_version != ADAPTER_VERSION:
        raise SystemExit(
            f"muse-code-openrouter must be exactly {ADAPTER_VERSION}, got {adapter_version}"
        )
    upstream = Upstream(("127.0.0.1", 0), Handler)
    upstream.requests = []
    upstream.lock = threading.Lock()
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    adapter = MuseOpenRouterServer(
        ("127.0.0.1", 0),
        api_key="credential-free-loopback",
        model=MODEL,
        upstream=f"http://127.0.0.1:{upstream.server_port}/v1",
    )
    adapter_thread = threading.Thread(target=adapter.serve_forever, daemon=True)
    adapter_thread.start()
    try:
        yield upstream, adapter
    finally:
        adapter.shutdown()
        upstream.shutdown()
        adapter.server_close()
        upstream.server_close()
        adapter_thread.join(timeout=5)
        upstream_thread.join(timeout=5)


def verify_client(binary: Path) -> dict[str, Any]:
    binary = binary.resolve(strict=True)
    completed = subprocess.run(
        [str(binary), "--version"], capture_output=True, text=True, check=False, timeout=15
    )
    if completed.returncode != 0 or completed.stdout.strip() != f"Muse Code {VERSION} ({RELEASE})":
        raise SystemExit("Muse binary is not the exact pinned official 0.2.1 release")
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    size = binary.stat().st_size
    if digest != BINARY_SHA256 or size != BINARY_BYTES:
        raise SystemExit("Muse binary bytes do not match the pinned official x86_64 Linux client")
    return {
        "version": VERSION,
        "release": RELEASE,
        "build_sha": BUILD_SHA,
        "binary_sha256": digest,
        "binary_size": size,
    }


def _environment(root: Path) -> dict[str, str]:
    return {
        "HOME": str(root / "home"),
        "XDG_DATA_HOME": str(root / "data"),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "TMPDIR": str(root / "tmp"),
        "META_API_KEY": "credential-free-loopback",
        "MUSE_NO_AUTO_UPDATE": "1",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "TERM": "dumb",
        "NO_COLOR": "1",
    }


def _run(
    binary: Path,
    adapter_port: int,
    root: Path,
    upstream: Upstream,
    *,
    label: str,
    prompt: str,
    session_id: str,
    attachment: Path | None = None,
    expand_mentions: bool = False,
) -> RunEvidence:
    command = [
        str(binary),
        "exec",
        "--session-id",
        session_id,
        "--provider",
        "meta",
        "--base-url",
        f"http://127.0.0.1:{adapter_port}/v1",
        "--model",
        MODEL,
        "--reasoning-effort",
        "minimal",
        "--workspace",
        str(root / "work"),
        "--disable-approval",
        "--disable-sandbox",
        "--disable-web-tools",
        "--no-foreign-personal-context",
        "--json",
    ]
    surface = "prompt"
    if attachment is not None:
        command.extend(("--image", str(attachment)))
        surface = "--image"
    if expand_mentions:
        command.append("--expand-file-mentions")
        surface = "--expand-file-mentions"
    command.append(prompt)
    with upstream.lock:
        start = len(upstream.requests)
    completed = subprocess.run(
        command,
        cwd=root / "work",
        env=_environment(root),
        capture_output=True,
        text=True,
        check=False,
        timeout=90,
    )
    with upstream.lock:
        end = len(upstream.requests)
        selected_requests = upstream.requests[start:end]
        media = tuple(
            sorted(
                {
                    media_type
                    for request in selected_requests
                    for media_type in _request_media_types(request)
                }
            )
        )
        markers = tuple(
            marker
            for marker in SCENARIO["markers"].values()
            if any(marker in _request_text(request) for request in selected_requests)
        )
    return RunEvidence(
        label=label,
        command_surface=surface,
        returncode=completed.returncode,
        request_start=start,
        request_end=end,
        request_media_types=media,
        request_markers=markers,
        stdout_tail=completed.stdout[-1000:],
        stderr_tail=completed.stderr[-1000:],
    )


def capture(binary: Path, output: Path) -> None:
    identity = verify_client(binary)
    output = output.resolve()
    if output.exists():
        raise SystemExit("capture output already exists")
    output.mkdir(parents=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="session-migrate-muse-capture-") as temporary:
        root = Path(temporary)
        for name in ("home", "data", "config", "cache", "tmp", "work"):
            (root / name).mkdir(mode=0o700)
        for asset in ASSETS.iterdir():
            if asset.is_file():
                shutil.copy2(asset, root / "work" / asset.name)
        evidence: list[RunEvidence] = []
        with loopback() as (upstream, adapter):
            evidence.append(
                _run(
                    binary,
                    adapter.server_port,
                    root,
                    upstream,
                    label="text-tools",
                    prompt=INITIAL_PROMPT,
                    session_id=SESSION_ID,
                )
            )
            evidence.append(
                _run(
                    binary,
                    adapter.server_port,
                    root,
                    upstream,
                    label="image-main",
                    prompt=IMAGE_PROMPT,
                    session_id=SESSION_ID,
                    attachment=root / "work/corpus-card.png",
                )
            )
            evidence.append(
                _run(
                    binary,
                    adapter.server_port,
                    root,
                    upstream,
                    label="recall-main",
                    prompt=RECALL_PROMPT,
                    session_id=SESSION_ID,
                )
            )
            media = (
                ("image", "corpus-card.png"),
                ("document", "corpus-document.pdf"),
                ("audio", "corpus-tone.wav"),
                ("video", "corpus-transition.mp4"),
            )
            for index, (label, filename) in enumerate(media, start=1):
                evidence.append(
                    _run(
                        binary,
                        adapter.server_port,
                        root,
                        upstream,
                        label=f"{label}-image-flag",
                        prompt=f"Probe the {label} passed through the documented --image surface.",
                        session_id=f"75757575-7575-4757-8757-7575757575{index:02d}",
                        attachment=root / "work" / filename,
                    )
                )
            for index, (label, filename) in enumerate(media[1:], start=1):
                evidence.append(
                    _run(
                        binary,
                        adapter.server_port,
                        root,
                        upstream,
                        label=f"{label}-file-mention",
                        prompt=f"Inspect @{filename} through the public file-mention surface.",
                        session_id=f"76767676-7676-4767-8767-7676767676{index:02d}",
                        expand_mentions=True,
                    )
                )
        session_paths = list((root / "data/muse/sessions").glob(f"**/{SESSION_ID}/session.jsonl"))
        if len(session_paths) != 1:
            raise SystemExit("exact Muse client did not create one selected native session")
        native = output / "session.jsonl"
        shutil.copy2(session_paths[0], native)
        native.chmod(0o600)
        (output / "evidence.json").write_text(
            json.dumps(
                {
                    "client": identity,
                    "adapter_version": ADAPTER_VERSION,
                    "session_id": SESSION_ID,
                    "source_root": str(root),
                    "raw_session_sha256": hashlib.sha256(native.read_bytes()).hexdigest(),
                    "runs": [asdict(item) for item in evidence],
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n"
        )
        (output / "evidence.json").chmod(0o600)
        failures = [item.label for item in evidence[:3] if item.returncode != 0]
        if failures:
            raise SystemExit(f"main Muse capture turns failed: {', '.join(failures)}")
        print(output)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--muse", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    capture(arguments.muse, arguments.output)


if __name__ == "__main__":
    main()
