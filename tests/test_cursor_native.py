"""Opt-in native oracle for the exact pinned Cursor Agent build.

Run with ``SESSION_MIGRATE_RUN_CURSOR_NATIVE=1``.  The test uses an isolated
home, a loopback-only synthetic service, and synthetic transcript markers.  It
does not read, copy, or print a real Cursor credential or session.
"""

import fcntl
import json
import os
import pty
import select
import sqlite3
import struct
import subprocess
import termios
import threading
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from session_migrate.errors import SessionMigrateError
from session_migrate.formats import cursor
from session_migrate.model import AgentFormat, Event, EventKind, Provenance, Role, Session

TARGET_ID = "99999999-aaaa-4bbb-8ccc-dddddddddddd"
USER_MARKER = "CURSOR_NATIVE_USER_ALPHA"
ASSISTANT_MARKER = "CURSOR_NATIVE_ASSISTANT_OMEGA"
APPEND_MARKER = "CURSOR_NATIVE_APPEND_GAMMA"
SYNTHETIC_ACCESS_TOKEN = "eyJhbGciOiJub25lIn0.eyJleHAiOjQxNDI0NDQ4MDB9."


def _varint(value: int) -> bytes:
    encoded = bytearray()
    while value >= 128:
        encoded.append((value & 127) | 128)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _blob(number: int, value: bytes) -> bytes:
    return _varint(number * 8 + 2) + _varint(len(value)) + value


def _text(number: int, value: str) -> bytes:
    return _blob(number, value.encode())


def _scalar(number: int, value: int) -> bytes:
    return _varint(number * 8) + _varint(value)


def _get_blob_message(request_id: int, blob_id: bytes) -> bytes:
    get_args = _blob(1, blob_id)
    kv_server_message = _scalar(1, request_id) + _blob(2, get_args)
    return _blob(4, kv_server_message)


def _connect_frame(message: bytes) -> bytes:
    return b"\x00" + len(message).to_bytes(4, "big") + message


MODEL_DETAILS = (
    _text(1, "synthetic-local-model")
    + _text(3, "synthetic-local-model")
    + _text(4, "Synthetic Local Model")
    + _text(5, "Synthetic")
)
USABLE_MODELS_RESPONSE = _blob(1, MODEL_DETAILS)
DEFAULT_MODEL_RESPONSE = _blob(1, MODEL_DETAILS)


class CursorLoopbackHandler(BaseHTTPRequestHandler):
    blob_ids: tuple[bytes, bytes, bytes] = (b"", b"", b"")
    observations: Counter[str] = Counter()
    observation_lock = threading.Lock()

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _reply(
        self, status: int, body: bytes = b"", content_type: str = "application/json"
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        if "chunked" not in self.headers.get("Transfer-Encoding", "").lower():
            length = int(self.headers.get("Content-Length", "0"))
            return self.rfile.read(length) if length else b""
        chunks = bytearray()
        self.connection.settimeout(1.0)
        try:
            while True:
                size_line = self.rfile.readline()
                if not size_line:
                    break
                size = int(size_line.split(b";", 1)[0].strip(), 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    break
                chunks.extend(self.rfile.read(size))
                self.rfile.read(2)
        except TimeoutError:
            pass
        return bytes(chunks)

    def do_GET(self) -> None:  # noqa: N802
        self._reply(404, b"{}")

    def do_POST(self) -> None:  # noqa: N802
        body = self._read_body()
        with self.observation_lock:
            self.observations[f"path:{self.path}"] += 1
        if self.path.endswith("/BidiAppend"):
            probes = {
                "new_user": APPEND_MARKER.encode().hex().encode(),
                "imported_user": USER_MARKER.encode().hex().encode(),
                "imported_assistant": ASSISTANT_MARKER.encode().hex().encode(),
                "turn_pointer": self.blob_ids[0].hex().encode(),
            }
            with self.observation_lock:
                for name, marker in probes.items():
                    self.observations[name] += body.count(marker)

        if self.path == "/auth/exchange_user_api_key":
            response = json.dumps(
                {
                    "accessToken": SYNTHETIC_ACCESS_TOKEN,
                    "refreshToken": "synthetic-refresh-token",
                }
            ).encode()
            self._reply(200, response)
        elif self.path.endswith("/GetUsableModels"):
            self._reply(200, USABLE_MODELS_RESPONSE, "application/proto")
        elif self.path.endswith("/GetDefaultModelForCli"):
            self._reply(200, DEFAULT_MODEL_RESPONSE, "application/proto")
        elif self.path.endswith("/RunSSE"):
            messages = [
                _get_blob_message(index, blob_id)
                for index, blob_id in enumerate(self.blob_ids, start=1)
            ]
            stream = b"".join(_connect_frame(message) for message in messages)
            stream += b"\x02" + (2).to_bytes(4, "big") + b"{}"
            self.close_connection = True
            self._reply(200, stream, "application/connect+proto")
        elif self.path.startswith("/aiserver.v1."):
            content_type = self.headers.get("Content-Type", "")
            if "application/json" in content_type:
                self._reply(200, b"{}", "application/json")
            else:
                self._reply(200, b"", "application/proto")
        else:
            self._reply(404, b"{}")


@contextmanager
def loopback_server(blob_ids: tuple[bytes, bytes, bytes]) -> Iterator[str]:
    CursorLoopbackHandler.blob_ids = blob_ids
    CursorLoopbackHandler.observations = Counter()
    server = ThreadingHTTPServer(("127.0.0.1", 0), CursorLoopbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def exact_cursor() -> Path:
    if os.environ.get("SESSION_MIGRATE_RUN_CURSOR_NATIVE") != "1":
        pytest.skip("set SESSION_MIGRATE_RUN_CURSOR_NATIVE=1 to run the Cursor oracle")
    try:
        configured = os.environ.get("SESSION_MIGRATE_CURSOR_BIN")
        return cursor.verify_pinned_cli(Path(configured) if configured else None)
    except SessionMigrateError as exc:
        pytest.skip(str(exc))


def portable_session(workspace: Path) -> Session:
    events = (
        Event(
            kind=EventKind.MESSAGE,
            role=Role.USER,
            text=USER_MARKER,
            provenance=Provenance(0, "user"),
        ),
        Event(
            kind=EventKind.MESSAGE,
            role=Role.ASSISTANT,
            text=ASSISTANT_MARKER,
            provenance=Provenance(1, "assistant"),
        ),
    )
    return Session(
        source_format=AgentFormat.CLAUDE,
        source_path=workspace / "synthetic-source.jsonl",
        source_sha256="0" * 64,
        session_id=None,
        cwd=workspace,
        started_at="2026-08-20T12:00:00Z",
        cli_version=None,
        model=None,
        title="Synthetic Cursor native oracle",
        events=events,
        raw_record_count=len(events),
    )


def isolated_env(tmp_path: Path, config_root: Path) -> dict[str, str]:
    home = tmp_path / "home"
    for path in (home, config_root, tmp_path / "xdg-cache", tmp_path / "xdg-data"):
        path.mkdir(parents=True, mode=0o700)
    return {
        "HOME": str(home),
        "CURSOR_CONFIG_DIR": str(config_root),
        "XDG_CONFIG_HOME": str(config_root.parent),
        "XDG_CACHE_HOME": str(tmp_path / "xdg-cache"),
        "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TERM": "xterm-256color",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }


def graph_ids(path: Path) -> tuple[bytes, bytes, bytes]:
    with sqlite3.connect(path) as db:
        encoded = db.execute("SELECT value FROM meta WHERE key='0'").fetchone()[0]
        metadata = json.loads(bytes.fromhex(encoded))
        rows = dict(db.execute("SELECT id,data FROM blobs"))
    root = rows[metadata["latestRootBlobId"]]
    turn_id = cursor._bytes_values(cursor._decode_message(root), 8)[0]
    turn = rows[turn_id.hex()]
    agent = cursor._bytes_values(cursor._decode_message(turn), 1)[0]
    agent_fields = cursor._decode_message(agent)
    user_id = cursor._bytes_values(agent_fields, 1)[0]
    assistant_id = cursor._bytes_values(agent_fields, 2)[0]
    return turn_id, user_id, assistant_id


def run_shipped_loader(cli: Path, store: Path, tmp_path: Path) -> str:
    loader = tmp_path / "cursor-shipped-loader.js"
    loader.write_text(
        """
const fs = require("node:fs");
const path = require("node:path");
const base = process.argv[2];
const db = process.argv[3];
let bundle = fs.readFileSync(path.join(base, "index.js"), "utf8");
const entry = 'var __webpack_exports__=__webpack_require__("./src/index.tsx")})();';
if (!bundle.includes(entry)) throw new Error("pinned entrypoint signature changed");
bundle = bundle.replace(entry, "globalThis.__cursorRequire=__webpack_require__})();");
eval(bundle);
const req = globalThis.__cursorRequire;
Object.assign(req.m, require(path.join(base, "156.index.js")).modules);
const { M: SqliteBlobStore } = req("./src/state/sqlite-blob-store.ts");
const { pH: AgentKv } = req("../agent-kv/dist/index.js");
(async () => {
  const store = await SqliteBlobStore.initAndLoad(db);
  try {
    const kv = new AgentKv(store, store);
    await kv.resetFromDb(undefined);
    const full = await kv.getFullConversation(undefined);
    const turn = full.turns[0].turn.value;
    console.log(`turns=${full.turns.length}`);
    console.log(`user=${turn.userMessage.text}`);
    console.log(`assistant=${turn.steps[0].message.value.text}`);
  } finally { await store.dispose(); }
})().catch((error) => { console.error(String(error)); process.exitCode = 1; });
""".strip()
        + "\n"
    )
    runtime = cli.parent
    # The bundled sqlite loader resolves its native addon beside the outer
    # script. Point that lookup back to the user's pinned installation.
    (tmp_path / "node_sqlite3.node").symlink_to(runtime / "node_sqlite3.node")
    completed = subprocess.run(
        [str(runtime / "node"), str(loader), str(runtime), str(store)],
        cwd=runtime,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0
    return completed.stdout


def tui_output(command: list[str], *, cwd: Path, env: dict[str, str]) -> bytes:
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
    )
    os.close(slave)
    output = bytearray()
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.25)
            if ready:
                try:
                    output.extend(os.read(master, 64 * 1024))
                except OSError:
                    break
            if USER_MARKER.encode() in output and ASSISTANT_MARKER.encode() in output:
                break
            if process.poll() is not None:
                break
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        os.close(master)
    return bytes(output)


def test_pinned_cursor_loads_renders_and_serves_imported_history(tmp_path: Path) -> None:
    cli = exact_cursor()
    workspace = tmp_path / "work"
    workspace.mkdir()
    config_root = tmp_path / "xdg-config" / "cursor"
    env = isolated_env(tmp_path, config_root)
    # The standard-library loopback oracle is HTTP/1.1. Keep this synthetic
    # network preference inside the isolated config root so Agent transport
    # does not try HTTP/2 against it.
    (config_root / "cli-config.json").write_text(
        json.dumps({"version": 1, "network": {"useHttp1ForAgent": True}})
    )
    data, losses = cursor.serialize(
        portable_session(workspace),
        session_id=TARGET_ID,
        cwd=workspace,
        timestamp="2026-08-20T12:00:00Z",
    )
    assert losses == {"runtime_metadata:source_format": 1}
    installed = cursor.install_database(
        data,
        session_id=TARGET_ID,
        cwd=workspace,
        target_home=config_root,
        target_cli=cli,
        environ=env,
    )

    shipped_output = run_shipped_loader(cli, installed.conversation_path, tmp_path)
    assert "turns=1" in shipped_output
    assert f"user={USER_MARKER}" in shipped_output
    assert f"assistant={ASSISTANT_MARKER}" in shipped_output

    ids = graph_ids(installed.conversation_path)
    with loopback_server(ids) as endpoint:
        base_command = [
            str(cli),
            "--endpoint",
            endpoint,
            "--agent-endpoint",
            endpoint,
            "--api-key",
            "synthetic-api-key",
            f"--resume={TARGET_ID}",
        ]
        headless = subprocess.run(
            [*base_command, "--print", "--trust", APPEND_MARKER],
            cwd=workspace,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert headless.returncode == 0, json.dumps(
            {
                "stderr": headless.stderr.decode(errors="replace"),
                "observations": dict(CursorLoopbackHandler.observations),
            },
            sort_keys=True,
        )
        observed = CursorLoopbackHandler.observations
        assert observed["new_user"] == 1
        assert observed["turn_pointer"] >= 1
        assert observed["imported_user"] == 1
        assert observed["imported_assistant"] == 1

        rendered = tui_output(base_command, cwd=workspace, env=env)
        assert USER_MARKER.encode() in rendered
        assert ASSISTANT_MARKER.encode() in rendered
