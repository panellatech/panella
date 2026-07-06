"""Unit tests for `eval/longmemeval/ingest_retrieve.py`'s hard isolation guard
(`_assert_isolated_urls`) — the mandatory, no-escape-hatch check that both --store-url and
--facade-url point at the isolated eval box (loopback + eval ports 18000/18001) and NEVER a real
box (127.0.0.1:8000/8001). This script bulk-ingests and bulk-deletes; a guard bypass would let a
misconfigured run mutate production data."""
from __future__ import annotations

import pytest

from eval.longmemeval.ingest_retrieve import _assert_isolated_urls


def test_eval_ports_on_loopback_pass() -> None:
    """The exact URLs `make eval-up` publishes must be accepted — this is the happy path every
    real invocation relies on."""
    _assert_isolated_urls("http://127.0.0.1:18000", "http://127.0.0.1:18001")  # must not raise


def test_localhost_hostname_also_passes() -> None:
    """`localhost` resolves to loopback just as validly as the literal IP."""
    _assert_isolated_urls("http://localhost:18000", "http://localhost:18001")  # must not raise


@pytest.mark.parametrize(
    ("store_url", "facade_url"),
    [
        ("http://127.0.0.1:8000", "http://127.0.0.1:18001"),  # store = REAL box port
        ("http://127.0.0.1:18000", "http://127.0.0.1:8001"),  # facade = REAL box port
        ("http://127.0.0.1:8000", "http://127.0.0.1:8001"),  # BOTH = real box ports
    ],
)
def test_real_box_ports_refused(store_url: str, facade_url: str) -> None:
    """8000/8001 are the REAL box's ports (docker-compose.yml, not the eval overlay) — pointing
    either lane at them must hard-refuse before any network call, with no way to override."""
    with pytest.raises(SystemExit, match=r"REFUSING to run"):
        _assert_isolated_urls(store_url, facade_url)


@pytest.mark.parametrize(
    ("store_url", "facade_url"),
    [
        ("http://example.com:18000", "http://127.0.0.1:18001"),  # store = non-loopback host
        ("http://127.0.0.1:18000", "http://10.0.0.5:18001"),  # facade = non-loopback host
        ("http://prod.panella.internal:18000", "http://prod.panella.internal:18001"),  # both remote
    ],
)
def test_non_loopback_host_refused(store_url: str, facade_url: str) -> None:
    """A non-loopback hostname (a real remote/production box) must be refused even if it happens
    to use the eval ports — loopback-ness and eval-port-ness are BOTH required, not either/or."""
    with pytest.raises(SystemExit, match=r"REFUSING to run"):
        _assert_isolated_urls(store_url, facade_url)


def test_wrong_port_on_loopback_still_refused() -> None:
    """A loopback host on some arbitrary OTHER port (neither 8000 nor 18000) must also be refused —
    only the exact eval port is accepted, not \"any port that isn't the real one\"."""
    with pytest.raises(SystemExit, match=r"REFUSING to run"):
        _assert_isolated_urls("http://127.0.0.1:9999", "http://127.0.0.1:18001")


def test_missing_port_refused() -> None:
    """A URL with no explicit port (parsed.port is None) must be refused, not silently coerced to
    a scheme default that could accidentally match."""
    with pytest.raises(SystemExit, match=r"REFUSING to run"):
        _assert_isolated_urls("http://127.0.0.1", "http://127.0.0.1:18001")


def test_refusal_message_names_the_offending_lane_and_url() -> None:
    """The exit message must be a one-line explanation naming which lane/URL failed — an operator
    debugging a refused run needs to know WHICH flag to fix."""
    with pytest.raises(SystemExit) as exc_info:
        _assert_isolated_urls("http://127.0.0.1:8000", "http://127.0.0.1:18001")
    message = str(exc_info.value)
    assert "store" in message
    assert "127.0.0.1:8000" in message
