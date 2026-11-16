"""Tests for the passive fill rules in backtest/fill_model.py."""

from backtest.fill_model import FillModel
from backtest.orders import Order, Side
from tests.helpers import book


def _order(side: Side, price: int, size: float, order_id: int = 1) -> Order:
    return Order(order_id, "M", side, price, size, is_taker=False, submitted_at=0.0, active_from=0.0)


def test_new_order_joins_back_of_queue():
    model = FillModel()
    order = _order(Side.BUY, 50, 10)
    assert model.initial_queue_ahead(order, book(0, [(50, 30)], [(52, 5)])) == 30
    assert FillModel(queue_position="front").initial_queue_ahead(order, book(0, [(50, 30)], [(52, 5)])) == 0


def test_trade_at_our_price_must_clear_queue_first():
    model = FillModel()
    order = _order(Side.BUY, 50, 10)
    order.queue_ahead = 30
    assert model.match_trade([order], 50, 20, Side.SELL) == []
    assert order.queue_ahead == 10
    allocations = model.match_trade([order], 50, 15, Side.SELL)
    assert [(o.order_id, size) for o, size in allocations] == [(1, 5)]
    assert order.queue_ahead == 0


def test_trade_through_our_price_fills_us():
    model = FillModel()
    order = _order(Side.BUY, 50, 10)
    order.queue_ahead = 100  # irrelevant: a seller at 49 would have hit our 50 bid first
    allocations = model.match_trade([order], 49, 4, Side.SELL)
    assert [(o.order_id, size) for o, size in allocations] == [(1, 4)]


def test_trades_on_the_wrong_side_or_price_do_not_fill():
    model = FillModel(queue_position="front")
    bid = _order(Side.BUY, 50, 10)
    assert model.match_trade([bid], 51, 10, Side.SELL) == []  # sold above our bid
    assert model.match_trade([bid], 50, 10, Side.BUY) == []  # a buyer cannot fill our bid
    ask = _order(Side.SELL, 55, 10, order_id=2)
    assert model.match_trade([ask], 54, 10, Side.BUY) == []  # bought below our ask


def test_one_trade_is_shared_not_duplicated_across_our_orders():
    model = FillModel(queue_position="front")
    better = _order(Side.BUY, 51, 10, order_id=1)
    worse = _order(Side.BUY, 50, 10, order_id=2)
    allocations = model.match_trade([worse, better], 50, 12, Side.SELL)
    assert [(o.order_id, size) for o, size in allocations] == [(1, 10), (2, 2)]


def test_queue_is_capped_at_displayed_size():
    model = FillModel()
    order = _order(Side.SELL, 55, 10)
    order.queue_ahead = 40
    model.update_on_book(order, book(1, [(50, 5)], [(55, 12)]))
    assert order.queue_ahead == 12
    model.update_on_book(order, book(2, [(50, 5)], [(55, 30)]))
    assert order.queue_ahead == 12  # new size joined behind us, so it does not count


def test_market_moving_through_our_price_fills_us():
    model = FillModel()
    order = _order(Side.BUY, 50, 10)
    order.queue_ahead = 20
    # Someone now offers at 49 and 50, at or below our bid: we would have traded.
    filled = model.update_on_book(order, book(1, [(48, 10)], [(49, 3), (50, 4), (51, 50)]))
    assert filled == 7
    assert FillModel(fill_on_cross=False).update_on_book(order, book(1, [(48, 10)], [(49, 3)])) == 0
