"""The run's gateway-refusal step passes on the real gateway and fails on a lenient one.

``assert_gateway_refuses`` is the end-to-end step of ``consumer_check.check`` (review P2-1):
the run calls it against the scripted gateway container. Here it runs against the same
gateway code in a thread, and against a mutated gateway that accepts any token, which the
step must catch.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest

from consumer_check.check import Results, assert_gateway_refuses
from consumer_check.scripted_gateway import Ledger, serve

#: Synthetic tokens, never real.
TOKENS = {"tenant-a": "synthetic-token-a-0123456789abcdef", "tenant-b": "synthetic-token-b"}


class AnyTokenLedger(Ledger):
    """The mutation: an unknown or missing token is taken for the first tenant's."""

    def label_for(self, authorization: str | None) -> str | None:
        return super().label_for(authorization) or next(iter(TOKENS))


def _serving(ledger: Ledger) -> Iterator[str]:
    server = serve(ledger, 0, host="127.0.0.1")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def honest_gateway() -> Iterator[str]:
    yield from _serving(Ledger(TOKENS))


@pytest.fixture
def lenient_gateway() -> Iterator[str]:
    yield from _serving(AnyTokenLedger(TOKENS))


def test_the_step_passes_on_the_scripted_gateway(
    honest_gateway: str, capsys: pytest.CaptureFixture[str]
) -> None:
    results = Results()
    assert_gateway_refuses(results, honest_gateway)
    assert results.failures == 0, capsys.readouterr().out
    assert capsys.readouterr().out.count("PASS:") == 3


def test_the_step_fails_on_a_gateway_that_accepts_any_token(
    lenient_gateway: str, capsys: pytest.CaptureFixture[str]
) -> None:
    results = Results()
    assert_gateway_refuses(results, lenient_gateway)
    # Both refusals fail, and so does the unchanged-counts check.
    assert results.failures == 3, capsys.readouterr().out
