"""Tests for Kalshi parsing, the simulator, storage and the loader."""

import numpy as np
import pytest

from data.kalshi import dollars_to_cents, parse_orderbook, parse_settlement, parse_trades
from data.loader import load_market_data
from data.schema import snapshots_from_books, validate_market_data
from data.simulator import SimulationConfig, simulate_market
from data.storage import load_market_data_dir, save_market_data


def test_parse_kalshi_fixed_point_orderbook():
    # Documented format: bids only, ascending price, best bid last.
    payload = {"orderbook_fp": {
        "yes_dollars": [["0.4000", "100.00"], ["0.4200", "13.00"]],
        "no_dollars": [["0.5000", "20.00"], ["0.5600", "7.00"]],
    }}
    bids, asks = parse_orderbook(payload)
    assert bids == [(42, 13.0), (40, 100.0)]
    # A NO bid at 56 is a YES ask at 44.
    assert asks == [(44, 7.0), (50, 20.0)]


def test_parse_legacy_orderbook_and_empty_sides():
    bids, asks = parse_orderbook({"orderbook": {"yes": [[30, 5]], "no": None}})
    assert bids == [(30, 5.0)] and asks == []
    with pytest.raises(ValueError):
        parse_orderbook({"something_else": {}})


def test_parse_kalshi_trades_maps_taker_side_to_aggressor():
    payload = {"trades": [
        {"ticker": "X", "count_fp": "3.00", "yes_price_dollars": "0.6100",
         "taker_outcome_side": "yes", "created_time": "2026-11-01T12:00:00Z"},
        {"ticker": "X", "count_fp": "2.00", "yes_price_dollars": "0.6000",
         "taker_side": "no", "created_time": "2026-11-01T12:00:01Z"},
    ]}
    rows = parse_trades(payload)
    assert [r["aggressor"] for r in rows] == ["buy", "sell"]
    assert [r["price"] for r in rows] == [61, 60]
    assert rows[1]["timestamp"] - rows[0]["timestamp"] == pytest.approx(1.0)


def test_parse_settlement():
    assert parse_settlement([{"ticker": "A", "result": "yes"}, {"ticker": "B", "result": "no"}]) == {"A": 100, "B": 0}
    assert parse_settlement([{"ticker": "A", "result": ""}]) is None
    assert dollars_to_cents("0.0700") == 7


def test_simulator_is_valid_and_deterministic():
    config = SimulationConfig(duration_s=300, seed=3)
    first = simulate_market(config)
    second = simulate_market(config)
    validate_market_data(first)
    assert first.books.equals(second.books) and first.trades.equals(second.trades)
    totals = first.truth.groupby("timestamp")["true_prob"].sum()
    assert np.allclose(totals, 1.0)
    assert sum(first.settlement.values()) == 100
    for snap in snapshots_from_books(first.books):
        assert not snap.is_crossed


def test_storage_round_trip(tmp_path):
    data = simulate_market(SimulationConfig(duration_s=60))
    save_market_data(data, tmp_path / "sim")
    loaded = load_market_data_dir(tmp_path / "sim")
    assert loaded.markets == data.markets
    assert len(loaded.books) == len(data.books) and len(loaded.trades) == len(data.trades)
    assert loaded.settlement == data.settlement
    # The one-line swap: a directory path works exactly like "simulated".
    assert len(load_market_data(str(tmp_path / "sim")).books) == len(data.books)
