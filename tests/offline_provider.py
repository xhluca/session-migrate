"""Credential-free localhost model provider used by exact-CLI tests.

The server deliberately implements only the small wire subset exercised by the
native harness gates.  It accepts OpenAI Chat Completions, OpenAI Responses,
and Anthropic Messages requests, records the complete JSON request for replay
assertions, and returns deterministic text without contacting a model vendor.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class OfflineProvider(ThreadingHTTPServer):
    requests: list[tuple[str, dict[str, Any]]]
    reply: str


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") in {"", "/health"}:
            self._json({"status": "ok"})
            return
        if self.path.rstrip("/") in {"/models", "/v1/models"}:
            self._json(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": "session-migrate/offline-echo",
                            "object": "model",
                            "owned_by": "session-migrate",
                        }
                    ],
                }
            )
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("content-length", "0"))
            value = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self.send_error(400)
            return
        if not isinstance(value, dict):
            self.send_error(400)
            return
        provider = self.server
        assert isinstance(provider, OfflineProvider)
        provider.requests.append((self.path, value))
        if self.path.rstrip("/").endswith("/chat/completions"):
            self._chat(value, provider.reply)
            return
        if self.path.rstrip("/").endswith("/responses"):
            self._responses(value, provider.reply)
            return
        if self.path.rstrip("/").endswith("/messages"):
            self._messages(value, provider.reply)
            return
        self.send_error(404)

    def _chat(self, request: dict[str, Any], reply: str) -> None:
        model = str(request.get("model") or "session-migrate/offline-echo")
        if not request.get("stream"):
            self._json(
                {
                    "id": "chatcmpl_session_migrate_offline",
                    "object": "chat.completion",
                    "created": 1_788_220_000,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": reply},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 3,
                        "total_tokens": 13,
                    },
                }
            )
            return
        chunks = [
            {
                "id": "chatcmpl_session_migrate_offline",
                "object": "chat.completion.chunk",
                "created": 1_788_220_000,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": reply},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chatcmpl_session_migrate_offline",
                "object": "chat.completion.chunk",
                "created": 1_788_220_000,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
            },
        ]
        self._sse([json.dumps(chunk) for chunk in chunks] + ["[DONE]"])

    def _responses(self, request: dict[str, Any], reply: str) -> None:
        model = str(request.get("model") or "session-migrate/offline-echo")
        item = {
            "id": "msg_session_migrate_offline",
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": reply, "annotations": []}],
        }
        response = {
            "id": "resp_session_migrate_offline",
            "object": "response",
            "created_at": 1_788_220_000,
            "status": "completed",
            "completed_at": 1_788_220_001,
            "error": None,
            "incomplete_details": None,
            "instructions": None,
            "max_output_tokens": None,
            "model": model,
            "output": [item],
            "parallel_tool_calls": True,
            "previous_response_id": None,
            "reasoning": {"effort": "low", "summary": None},
            "store": False,
            "temperature": None,
            "text": {"format": {"type": "text"}},
            "tool_choice": "auto",
            "tools": [],
            "top_p": None,
            "truncation": "disabled",
            "usage": {
                "input_tokens": 10,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 3,
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": 13,
            },
            "user": None,
            "metadata": {},
        }
        if not request.get("stream"):
            self._json(response)
            return
        events = [
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
                "delta": reply,
            },
            {
                "type": "response.output_text.done",
                "item_id": item["id"],
                "output_index": 0,
                "content_index": 0,
                "text": reply,
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
        self._sse([json.dumps(event) for event in events] + ["[DONE]"])

    def _messages(self, request: dict[str, Any], reply: str) -> None:
        model = str(request.get("model") or "session-migrate/offline-echo")
        if not request.get("stream"):
            self._json(
                {
                    "id": "msg_session_migrate_offline",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [{"type": "text", "text": reply}],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 10, "output_tokens": 3},
                }
            )
            return
        events = [
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_session_migrate_offline",
                        "type": "message",
                        "role": "assistant",
                        "model": model,
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {
                            "input_tokens": 10,
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0,
                            "output_tokens": 1,
                        },
                    },
                },
            ),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": reply},
                },
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 3},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ]
        body = "".join(
            f"event: {name}\ndata: {json.dumps(event)}\n\n" for name, event in events
        ).encode()
        self._body(body, "text/event-stream")

    def _json(self, value: object) -> None:
        self._body(json.dumps(value).encode(), "application/json")

    def _sse(self, values: list[str]) -> None:
        self._body("".join(f"data: {value}\n\n" for value in values).encode(), "text/event-stream")

    def _body(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def offline_provider(reply: str) -> Iterator[OfflineProvider]:
    """Run a deterministic provider on an ephemeral loopback port."""

    provider = OfflineProvider(("127.0.0.1", 0), _Handler)
    provider.requests = []
    provider.reply = reply
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    try:
        yield provider
    finally:
        provider.shutdown()
        thread.join(timeout=5)
        provider.server_close()


def request_text(request: dict[str, Any]) -> str:
    """Return a stable JSON view used for imported-prefix marker assertions."""

    return json.dumps(request, ensure_ascii=False, sort_keys=True)
