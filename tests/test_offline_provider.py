from __future__ import annotations

import json
import urllib.request

import pytest
from offline_provider import offline_provider


def _post(url: str, payload: dict[str, object]) -> tuple[str, str]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.headers["Content-Type"], response.read().decode()


@pytest.mark.parametrize(
    ("path", "payload", "expected"),
    [
        (
            "/v1/chat/completions",
            {"model": "fixture", "messages": [{"role": "user", "content": "hello"}]},
            '"content": "OFFLINE_ECHO"',
        ),
        (
            "/v1/responses",
            {"model": "fixture", "input": "hello", "stream": False},
            '"text": "OFFLINE_ECHO"',
        ),
        (
            "/v1/messages",
            {"model": "fixture", "messages": [{"role": "user", "content": "hello"}]},
            '"text": "OFFLINE_ECHO"',
        ),
    ],
)
def test_offline_provider_non_streaming_protocols(
    path: str, payload: dict[str, object], expected: str
) -> None:
    with offline_provider("OFFLINE_ECHO") as provider:
        content_type, body = _post(f"http://127.0.0.1:{provider.server_address[1]}{path}", payload)

    assert content_type == "application/json"
    assert expected in body
    assert provider.requests == [(path, payload)]


@pytest.mark.parametrize(
    ("path", "terminal_event"),
    [
        ("/v1/chat/completions", "data: [DONE]"),
        ("/v1/responses", "response.completed"),
        ("/v1/messages", "event: message_stop"),
    ],
)
def test_offline_provider_streaming_protocols(path: str, terminal_event: str) -> None:
    with offline_provider("OFFLINE_STREAM_ECHO") as provider:
        content_type, body = _post(
            f"http://127.0.0.1:{provider.server_address[1]}{path}",
            {"model": "fixture", "stream": True, "messages": []},
        )

    assert content_type == "text/event-stream"
    assert "OFFLINE_STREAM_ECHO" in body
    assert terminal_event in body
