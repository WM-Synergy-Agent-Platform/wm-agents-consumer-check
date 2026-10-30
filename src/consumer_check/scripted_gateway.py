"""The scripted model gateway: stands in for a tenant's gateway, so no model is called.

It answers ``POST /v1/chat/completions`` (the ``openai-chat`` wire format) with one scripted
completion: a call of the agent's ``final_result`` output tool carrying :data:`ANSWER`. It
accepts a call only with ``Authorization: Bearer <token>`` for one of the tokens it was
started with (one per tenant process, each from a file), refuses any other with ``401``,
and counts the calls it answered per token. ``GET /`` answers ``200`` (the agents' health
probe); ``GET /_scripted/calls`` returns the counts by the token files' labels, never a
token. It logs no header and no body.

Standard library only: it is the consumer repo's own code, never ``agent_core``'s test
helpers.

    python -m consumer_check.scripted_gateway --port 8080 \
        --token tenant-a=/run/secrets/tenant-a-token --token tenant-b=/run/secrets/tenant-b-token
"""

from __future__ import annotations

import argparse
import hmac
import json
import sys
import threading
from collections.abc import Mapping, Sequence
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final

CHAT_PATH: Final = "/v1/chat/completions"
CALLS_PATH: Final = "/_scripted/calls"
#: The scripted model output: what a 200 from ``POST /invoke`` must carry.
ANSWER: Final = "synthetic answer from the scripted gateway"
#: The largest request body read; a chat request from the agent is far smaller.
MAX_BODY_BYTES: Final = 1024 * 1024


def completion(model: str) -> dict[str, Any]:
    """The scripted chat completion: the agent's ``final_result`` tool with :data:`ANSWER`."""
    call = {
        "id": "call-scripted-final",
        "type": "function",
        "function": {"name": "final_result", "arguments": json.dumps({"answer": ANSWER})},
    }
    return {
        "id": "chatcmpl-scripted",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": None, "tool_calls": [call]},
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


class Ledger:
    """The accepted tokens by label, and how many chat calls each one made."""

    def __init__(self, tokens: Mapping[str, str]) -> None:
        if not tokens:
            raise ValueError("the scripted gateway needs at least one token")
        self._tokens = dict(tokens)
        self._calls = dict.fromkeys(self._tokens, 0)
        self._lock = threading.Lock()

    def label_for(self, authorization: str | None) -> str | None:
        """The label of the token in an ``Authorization: Bearer`` header, if it is one."""
        if authorization is None or not authorization.startswith("Bearer "):
            return None
        presented = authorization.removeprefix("Bearer ").encode()
        for label, token in self._tokens.items():
            if hmac.compare_digest(presented, token.encode()):
                return label
        return None

    def record(self, label: str) -> None:
        with self._lock:
            self._calls[label] += 1

    def calls(self) -> dict[str, int]:
        with self._lock:
            return dict(self._calls)


def handler_for(ledger: Ledger) -> type[BaseHTTPRequestHandler]:
    """A request handler class answering from ``ledger``."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "scripted-gateway"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:
            """No access log: it would carry paths only, but nothing is needed from it."""

        def _send(self, status: HTTPStatus, body: Mapping[str, Any]) -> None:
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            if self.path == "/":
                self._send(HTTPStatus.OK, {"status": "scripted"})
            elif self.path == CALLS_PATH:
                self._send(HTTPStatus.OK, {"calls": ledger.calls()})
            else:
                self._send(HTTPStatus.NOT_FOUND, {"error": "no such route"})

        def do_POST(self) -> None:
            label = ledger.label_for(self.headers.get("Authorization"))
            if label is None:
                self._send(HTTPStatus.UNAUTHORIZED, {"error": "unknown gateway token"})
                return
            if self.path != CHAT_PATH:
                self._send(HTTPStatus.NOT_FOUND, {"error": "no such route"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_BODY_BYTES:
                self._send(HTTPStatus.BAD_REQUEST, {"error": "bad body length"})
                return
            try:
                request = json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                request = None
            if not isinstance(request, dict) or request.get("stream"):
                self._send(HTTPStatus.BAD_REQUEST, {"error": "not a non-streaming chat call"})
                return
            ledger.record(label)
            self._send(HTTPStatus.OK, completion(str(request.get("model", "scripted"))))

    return Handler


def read_tokens(specs: Sequence[str]) -> dict[str, str]:
    """``label=path`` pairs to ``{label: token}``, each token read from its file."""
    tokens: dict[str, str] = {}
    for spec in specs:
        label, separator, path = spec.partition("=")
        if not separator or not label or not path:
            raise ValueError("each --token is label=path")
        token = Path(path).read_text(encoding="ascii").strip()
        if not token:
            raise ValueError(f"the token file for {label} is empty")
        tokens[label] = token
    return tokens


def serve(ledger: Ledger, port: int, host: str = "0.0.0.0") -> ThreadingHTTPServer:  # noqa: S104
    """The server, bound to ``host`` and ``port``; call ``serve_forever`` on it."""
    return ThreadingHTTPServer((host, port), handler_for(ledger))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The scripted model gateway.")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--token", action="append", default=[], metavar="LABEL=PATH")
    args = parser.parse_args(argv)
    try:
        ledger = Ledger(read_tokens(args.token))
    except (OSError, ValueError) as error:
        print(f"scripted gateway: {error}", file=sys.stderr)
        return 1
    server = serve(ledger, args.port)
    print(f"scripted gateway: listening on {args.port}, {len(ledger.calls())} tokens", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
