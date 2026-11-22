"""Kalshi client retries and the collector's automatic stop, without network access."""

import json

import pytest
import requests

from data.kalshi import CollectorConfig, KalshiClient, KalshiCollector, parse_utc, retry_delay_s
from data.simulator import SimulationConfig, simulate_market
from tests.replay_client import FakeClock, ReplayClient


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, headers: dict | None = None) -> None:
        self.status_code = status_code
        self.payload = payload or {}
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")

    def json(self) -> dict:
        return self.payload


def scripted_client(responses: list, max_retries: int = 5) -> tuple[KalshiClient, list[float]]:
    """A client whose HTTP calls return (or raise) the given items in order, recording sleeps."""
    queue = list(responses)
    sleeps: list[float] = []

    def http_get(url, params=None, timeout=None):
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    client = KalshiClient(http_get=http_get, sleep=sleeps.append, pause_s=0.0, max_retries=max_retries)
    return client, sleeps


def test_rate_limit_is_retried_with_backoff_and_retry_after():
    client, sleeps = scripted_client([
        FakeResponse(429),
        FakeResponse(429, headers={"Retry-After": "3"}),
        requests.ConnectionError("dropped"),
        FakeResponse(200, {"market": {"ticker": "A", "status": "active"}}),
    ])
    assert client.get_market("A")["ticker"] == "A"
    # backoff 1 s, then the server's 3 s, then backoff 4 s; then the 0 s pause after success
    assert sleeps == [1.0, 3.0, 4.0, 0.0]
    assert client.retries_used == 3


def test_gives_up_after_max_retries_and_does_not_retry_client_errors():
    client, _ = scripted_client([FakeResponse(429)] * 3, max_retries=2)
    with pytest.raises(requests.HTTPError):
        client.get_market("A")
    client, sleeps = scripted_client([FakeResponse(404)])
    with pytest.raises(requests.HTTPError):
        client.get_market("A")
    assert sleeps == []


def test_retry_delay_caps_and_ignores_unparseable_header():
    assert retry_delay_s(10, None) == 30.0
    assert retry_delay_s(1, "Wed, 21 Oct 2026 07:28:00 GMT") == 2.0
    assert retry_delay_s(0, "120") == 30.0


def test_collector_stops_when_markets_close_and_saves_settlement(tmp_path):
    sim = simulate_market(SimulationConfig(duration_s=600, seed=2))
    clock = FakeClock(0.0)
    client = ReplayClient(sim, clock)
    client.close_time = 0.0  # already closed: the first status check stops the run
    client.results = {sim.markets[0]: "yes", **{m: "no" for m in sim.markets[1:]}}
    config = CollectorConfig(out_dir=tmp_path / "rec", tickers=sim.markets, interval_s=0.0,
                             status_check_s=0.0, settle_wait_s=0.0, max_polls=50)
    assert KalshiCollector(client, config, clock=clock).run() == "closed"
    meta = json.loads((tmp_path / "rec" / "meta.json").read_text())
    assert meta["settlement"] == {sim.markets[0]: 100, **{m: 0 for m in sim.markets[1:]}}


def test_collector_keeps_going_while_markets_are_open(tmp_path):
    sim = simulate_market(SimulationConfig(duration_s=600, seed=2))
    clock = FakeClock(0.0)
    config = CollectorConfig(out_dir=tmp_path / "rec", tickers=sim.markets, interval_s=0.0,
                             status_check_s=0.0, max_polls=5)
    assert KalshiCollector(ReplayClient(sim, clock), config, clock=clock).run() == "max_polls"


def test_parse_utc_defaults_to_utc():
    assert parse_utc("2026-11-17T19:15:00") == parse_utc("2026-11-17T19:15:00Z") == 1794942900.0


def test_until_online_waits_out_network_errors_but_not_http_errors():
    import requests
    from data.kalshi import until_online
    calls, naps = [], []
    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise requests.ConnectionError("offline")
        return "ok"
    assert until_online(flaky, wait_s=60, sleep=naps.append) == "ok"
    assert naps == [60, 60]
    def not_found():
        raise requests.HTTPError("404")
    with pytest.raises(requests.HTTPError):
        until_online(not_found, sleep=naps.append)
