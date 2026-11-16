"""Tests for order book representation and derived metrics."""

import math

import pytest

from orderbook.metrics import (basket_prices, decimal_odds_to_probability, overround,
                               probability_to_decimal_odds, snapshot_metrics)
from tests.helpers import book


def test_top_of_book_spread_and_mid():
    snap = book(0, bids=[(48, 10), (50, 20), (49, 5)], asks=[(53, 7), (52, 30)])
    assert snap.best_bid == 50 and snap.best_ask == 52
    assert snap.best_bid_size == 20 and snap.best_ask_size == 30
    assert snap.spread == 2
    assert snap.mid == 51.0
    assert snap.implied_probability == pytest.approx(0.51)


def test_from_levels_sorts_merges_and_drops_empty_levels():
    snap = book(0, bids=[(40, 5), (41, 0), (40, 3)], asks=[(45, 1)])
    assert snap.bids == ((40, 8.0),)


def test_microprice_leans_towards_the_thin_side():
    # Lots of bids, few asks: the next trade is more likely a buy, so the
    # microprice should sit above the mid, closer to the ask.
    snap = book(0, bids=[(50, 90)], asks=[(52, 10)])
    assert snap.microprice == pytest.approx((50 * 10 + 52 * 90) / 100)
    assert snap.mid < snap.microprice < snap.best_ask


def test_depth_and_imbalance():
    snap = book(0, bids=[(50, 30), (49, 10), (48, 10)], asks=[(51, 10), (52, 10)])
    assert snap.depth("bid", 2) == 40
    assert snap.depth("ask", 5) == 20
    assert snap.imbalance(1) == pytest.approx((30 - 10) / 40)
    assert snap.imbalance(5) == pytest.approx((50 - 20) / 70)


def test_one_sided_book_has_no_spread_or_mid():
    snap = book(0, bids=[(50, 10)], asks=[])
    assert snap.spread is None and snap.mid is None and snap.microprice is None
    metrics = snapshot_metrics(snap)
    assert metrics["best_ask"] is None and metrics["bid_depth"] == 10


def test_size_at_exact_price():
    snap = book(0, bids=[(50, 30), (49, 10)], asks=[(51, 5)])
    assert snap.size_at("bid", 49) == 10
    assert snap.size_at("bid", 47) == 0
    assert snap.size_at("ask", 51) == 5


def test_odds_conversions_round_trip():
    assert decimal_odds_to_probability(4.0) == pytest.approx(0.25)
    assert probability_to_decimal_odds(0.25) == pytest.approx(4.0)
    with pytest.raises(ValueError):
        decimal_odds_to_probability(1.0)


def test_overround_and_basket_prices():
    assert overround([0.5, 0.3, 0.25]) == pytest.approx(0.05)
    books = {
        "A": book(0, [(48, 5)], [(50, 5)], "A"),
        "B": book(0, [(28, 5)], [(30, 5)], "B"),
        "C": book(0, [(17, 5)], [(19, 5)], "C"),
    }
    basket = basket_prices(books)
    assert basket["sum_best_asks"] == 99 and basket["buy_edge"] == 1
    assert basket["sum_best_bids"] == 93 and basket["sell_edge"] == -7
    assert not math.isnan(basket["buy_edge"])
