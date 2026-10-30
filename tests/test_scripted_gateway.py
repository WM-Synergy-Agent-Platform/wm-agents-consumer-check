"""The scripted gateway answers the model call for a known token and refuses any other."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest

from consumer_check.scripted_gateway import ANSWER, CALLS_PATH, CHAT_PATH, Ledger, serve

#: Synthetic tokens, never real.
TOKENS = {"tenant-a": "synthetic-token-a-0123456789abcdef", "tenant-b": "synthetic-token-b"}


@pytest.fixture
def base_url() -> Iterator[str]:
    server = serve(Ledger(TOKENS), 0, host="127.0.0.1")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _call(url: str, token: str | None, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    data = None if body is None else json.dumps(body).encode()
    method = "GET" if body is None else "POST"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


CHAT = {"model": "scripted-chat-model", "messages": [{"role": "user", "content": "hi"}]}


def test_a_known_token_gets_the_scripted_final_result(base_url: str) -> None:
    status, body = _call(base_url + CHAT_PATH, TOKENS["tenant-a"], CHAT)
    assert status == 200
    (choice,) = body["choices"]
    (call,) = choice["message"]["tool_calls"]
    assert call["function"]["name"] == "final_result"
    assert json.loads(call["function"]["arguments"]) == {"answer": ANSWER}
    assert body["model"] == "scripted-chat-model"


@pytest.mark.parametrize("token", [None, "synthetic-unknown-token", TOKENS["tenant-a"] + "x"])
def test_any_other_token_is_refused_and_not_counted(base_url: str, token: str | None) -> None:
    status, _ = _call(base_url + CHAT_PATH, token, CHAT)
    assert status == 401
    assert _call(base_url + CALLS_PATH, None) == (200, {"calls": {"tenant-a": 0, "tenant-b": 0}})


def test_calls_are_counted_per_token_label(base_url: str) -> None:
    _call(base_url + CHAT_PATH, TOKENS["tenant-b"], CHAT)
    _call(base_url + CHAT_PATH, TOKENS["tenant-b"], CHAT)
    assert _call(base_url + CALLS_PATH, None) == (200, {"calls": {"tenant-a": 0, "tenant-b": 2}})


def test_a_streaming_call_is_refused(base_url: str) -> None:
    status, _ = _call(base_url + CHAT_PATH, TOKENS["tenant-a"], {**CHAT, "stream": True})
    assert status == 400


def test_the_health_probe_gets_an_answer_without_a_token(base_url: str) -> None:
    assert _call(base_url + "/", None)[0] == 200
