"""Collector resume behaviour and paper trading, using a replay of simulated data."""

import pandas as pd

from data.kalshi import CollectorConfig, KalshiCollector, repair_partial_last_line
from data.schema import split_into_sessions
from data.simulator import SimulationConfig, simulate_market
from data.storage import load_market_data_dir
from paper.report import write_reports
from paper.trader import PaperConfig, PaperTrader
from tests.replay_client import FakeClock, ReplayClient

SIM = simulate_market(SimulationConfig(duration_s=1800, seed=1))


def _collect(out_dir, clock, client, polls, interval=5.0, tickers=None):
    collector = KalshiCollector(client, CollectorConfig(out_dir=out_dir, tickers=tickers or [], interval_s=interval,
                                                        mutually_exclusive=True, exhaustive=True), clock=clock)
    for _ in range(polls):
        clock.now += interval
        collector.poll_once()
    return collector


def test_collector_resumes_after_a_crash_without_duplicates(tmp_path):
    clock = FakeClock(0.0)
    client = ReplayClient(SIM, clock)
    out = tmp_path / "rec"
    _collect(out, clock, client, polls=40, tickers=SIM.markets)
    # Simulate a crash in the middle of writing a line.
    with open(out / "books.csv", "a") as handle:
        handle.write("12345.0,SIM-A,bi")
    assert repair_partial_last_line(out / "books.csv")
    with open(out / "trades.csv", "a") as handle:
        handle.write("999.0,SIM-A,5")
    # Restart without giving the market list again, and keep polling.
    _collect(out, clock, client, polls=40)

    data = load_market_data_dir(out)
    assert data.markets == SIM.markets and data.mutually_exclusive
    assert data.trades["trade_id"].is_unique
    expected = SIM.trades[SIM.trades["timestamp"] <= clock.now]
    assert len(data.trades) == len(expected)  # nothing missed, nothing duplicated
    assert data.books["timestamp"].nunique() == 80


def test_recorded_data_splits_into_sessions(tmp_path):
    clock = FakeClock(0.0)
    _collect(tmp_path / "rec", clock, ReplayClient(SIM, clock), polls=360, tickers=SIM.markets)
    data = load_market_data_dir(tmp_path / "rec")
    sessions = split_into_sessions(data, 600)
    assert len(sessions) == 3
    assert all(s.settlement is None for s in sessions)


def test_paper_trader_logs_resumes_and_compares_with_backtest(tmp_path):
    clock = FakeClock(0.0)
    client = ReplayClient(SIM, clock)
    out = tmp_path / "paper"
    config = PaperConfig(markets=list(SIM.markets), strategy="mm", interval_s=2.0, starting_balance=1000.0)
    trader = PaperTrader(client, config, out, clock=clock)
    for _ in range(300):
        clock.now += 2.0
        trader.step()
    equity = pd.read_csv(out / "equity.csv")
    assert len(equity) == 300
    assert (out / "orders.csv").exists()
    position_before = equity["position_SIM-A"].iloc[-1]

    # Restart: the portfolio is restored.
    trader = PaperTrader(client, PaperConfig.load(out / "paper_config.json"), out, clock=clock)
    assert trader.engine.portfolio.positions["SIM-A"] == position_before
    for _ in range(300):
        clock.now += 2.0
        trader.step()

    tables = write_reports(out, timezone="UTC")
    assert len(tables["daily"]) == 1
    assert {"Paper", "Backtest, same period"} <= set(tables["comparison"].columns)
    assert (out / "report" / "paper_vs_backtest.png").exists()


def test_paper_balance_is_enforced(tmp_path):
    clock = FakeClock(0.0)
    config = PaperConfig(markets=list(SIM.markets), strategy="mm", interval_s=2.0, starting_balance=2.0)
    trader = PaperTrader(ReplayClient(SIM, clock), config, tmp_path / "poor", clock=clock)
    for _ in range(200):
        clock.now += 2.0
        trader.step()
    orders = pd.read_csv(tmp_path / "poor" / "orders.csv")
    # $2 cannot pay for a 10-lot at these prices, so orders are cut down or rejected.
    assert (orders["size"] < 10).any() or (orders["reject_reason"] == "insufficient balance").any()
    assert pd.read_csv(tmp_path / "poor" / "equity.csv")["available_balance"].min() >= -1e-9
