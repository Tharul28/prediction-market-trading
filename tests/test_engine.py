"""End-to-end tests of the backtest engine: latency, lookahead, limits, fees."""

import pytest

from backtest.engine import BacktestConfig, BacktestEngine
from backtest.fees import FeeModel
from backtest.fill_model import FillModel
from backtest.orders import Side
from strategies.base import Action, PlaceLimit, PlaceTaker, Strategy, StrategyContext
from strategies.cross_outcome_arb import ArbConfig, CrossOutcomeArbitrage, unhedged_contracts
from strategies.market_maker import InventorySkewMarketMaker, MarketMakerConfig
from tests.helpers import book, make_data

NO_FEES = FeeModel(enabled=False)


class OneShot(Strategy):
    """Sends a fixed list of actions on the first book update only."""

    def __init__(self, actions: list[Action], markets: list[str] | None = None) -> None:
        self.actions = actions
        self.markets = markets or ["M"]
        self.name = "one_shot"
        self.sent = False
        self.seen_timestamps: list[float] = []

    def on_book_update(self, market_id: str, context: StrategyContext) -> list[Action]:
        self.seen_timestamps.append(max(b.timestamp for b in context.books.values()))
        if self.sent:
            return []
        self.sent = True
        return self.actions


def test_order_cannot_fill_before_latency_has_passed():
    snaps = [book(0, [(50, 10)], [(52, 10)]), book(1, [(50, 10)], [(52, 10)])]
    trades = [
        {"timestamp": 0.2, "market_id": "M", "price": 49, "size": 5, "aggressor": "sell"},  # too early
        {"timestamp": 1.5, "market_id": "M", "price": 49, "size": 5, "aggressor": "sell"},
    ]
    strategy = OneShot([PlaceLimit("M", Side.BUY, 51, 10)])
    result = BacktestEngine(make_data(snaps, trades), strategy,
                            BacktestConfig(latency_s=0.5, fees=NO_FEES)).run()
    assert list(result.fills["timestamp"]) == [1.5]
    assert result.fills["size"].sum() == 5


def test_taker_order_executes_on_the_next_snapshot_not_the_one_it_saw():
    # The strategy sees an ask of 40 at t=0, but by t=1 the ask is 45.
    snaps = [book(0, [(38, 10)], [(40, 10)]), book(1, [(43, 10)], [(45, 10)])]
    strategy = OneShot([PlaceTaker("M", Side.BUY, 40, 5)])
    result = BacktestEngine(make_data(snaps), strategy, BacktestConfig(fees=NO_FEES)).run()
    assert result.fills.empty  # the 40 offer is gone, the IOC order is cancelled
    strategy = OneShot([PlaceTaker("M", Side.BUY, 45, 5)])
    result = BacktestEngine(make_data(snaps), strategy, BacktestConfig(fees=NO_FEES)).run()
    assert list(result.fills["price"]) == [45]


def test_strategy_never_sees_future_books():
    snaps = [book(t, [(50, 10)], [(52, 10)]) for t in range(5)]
    strategy = OneShot([])
    engine = BacktestEngine(make_data(snaps), strategy, BacktestConfig())
    engine.run()
    assert strategy.seen_timestamps == [0, 1, 2, 3, 4]


def test_position_limit_is_enforced_including_open_orders():
    snaps = [book(t, [(50, 100)], [(52, 100)]) for t in range(3)]
    trades = [{"timestamp": 1.5, "market_id": "M", "price": 40, "size": 500, "aggressor": "sell"}]
    strategy = OneShot([PlaceLimit("M", Side.BUY, 51, 60), PlaceLimit("M", Side.BUY, 51, 60)])
    result = BacktestEngine(make_data(snaps, trades), strategy,
                            BacktestConfig(max_position=100, fees=NO_FEES)).run()
    assert result.fills["size"].sum() == 100
    assert result.equity["position_M"].abs().max() <= 100


def test_post_only_order_that_would_cross_is_rejected():
    snaps = [book(0, [(50, 10)], [(52, 10)]), book(1, [(50, 10)], [(52, 10)])]
    strategy = OneShot([PlaceLimit("M", Side.BUY, 52, 10)])
    result = BacktestEngine(make_data(snaps), strategy, BacktestConfig()).run()
    assert result.orders["status"].tolist() == ["rejected"]
    assert result.fills.empty


def test_gross_equals_net_plus_fees():
    snaps = [book(0, [(48, 50)], [(50, 50)]), book(1, [(48, 50)], [(50, 50)])]
    strategy = OneShot([PlaceTaker("M", Side.BUY, 50, 100)])
    with_fees = BacktestEngine(make_data(snaps), strategy, BacktestConfig()).run()
    assert with_fees.fees_paid == pytest.approx(0.88)  # 0.07 * 50 * 0.25 = 0.875, rounded up
    assert with_fees.gross_pnl == pytest.approx(with_fees.net_pnl + with_fees.fees_paid)
    no_fees = BacktestEngine(make_data(snaps), OneShot([PlaceTaker("M", Side.BUY, 50, 100)]),
                             BacktestConfig(fees=NO_FEES)).run()
    assert no_fees.gross_pnl == pytest.approx(with_fees.gross_pnl)


def test_market_maker_skews_quotes_against_inventory():
    mm = InventorySkewMarketMaker(MarketMakerConfig("M", half_spread_cents=1.0, max_position=100,
                                                    skew_cents_at_max=4.0))
    snap = book(0, [(45, 10)], [(55, 10)])
    flat = StrategyContext(0, {"M": snap}, {"M": 0.0}, (), 100)
    long = StrategyContext(0, {"M": snap}, {"M": 50.0}, (), 100)
    flat_quotes = mm.desired_quotes(flat)
    long_quotes = mm.desired_quotes(long)
    assert flat_quotes[Side.BUY][0] == 49 and flat_quotes[Side.SELL][0] == 51
    assert long_quotes[Side.BUY][0] < flat_quotes[Side.BUY][0]
    assert long_quotes[Side.SELL][0] < flat_quotes[Side.SELL][0]


def test_market_maker_stops_bidding_at_the_position_limit():
    mm = InventorySkewMarketMaker(MarketMakerConfig("M", max_position=100))
    context = StrategyContext(0, {"M": book(0, [(45, 10)], [(55, 10)])}, {"M": 100.0}, (), 100)
    quotes = mm.desired_quotes(context)
    assert quotes[Side.BUY][1] == 0 and quotes[Side.SELL][1] > 0


def _three_books(t: float, asks: list[int]) -> list:
    return [book(t, [(a - 2, 50)], [(a, 50)], m) for m, a in zip("ABC", asks)]


def test_arbitrage_fires_only_when_edge_survives_fees():
    markets = ["A", "B", "C"]
    cheap = _three_books(0, [45, 30, 20]) + _three_books(1, [45, 30, 20])  # asks sum to 95
    strategy = CrossOutcomeArbitrage(ArbConfig(tuple(markets)), FeeModel())
    result = BacktestEngine(make_data(cheap, markets=markets, settlement={"A": 100, "B": 0, "C": 0}),
                            strategy, BacktestConfig()).run()
    assert strategy.baskets_sent >= 1
    assert sorted(result.fills["market_id"].unique()) == markets
    assert result.settlement_pnl > 0

    tight = _three_books(0, [50, 30, 19]) + _three_books(1, [50, 30, 19])  # asks sum to 99
    strategy = CrossOutcomeArbitrage(ArbConfig(tuple(markets)), FeeModel())
    BacktestEngine(make_data(tight, markets=markets), strategy, BacktestConfig()).run()
    assert strategy.baskets_sent == 0  # 1 cent of edge does not cover three taker fees
    strategy = CrossOutcomeArbitrage(ArbConfig(tuple(markets)), NO_FEES)
    BacktestEngine(make_data(tight, markets=markets), strategy, BacktestConfig(fees=NO_FEES)).run()
    assert strategy.baskets_sent >= 1


def test_unhedged_contracts():
    assert unhedged_contracts([10, 10, 10]) == 0
    assert unhedged_contracts([10, 10, 0]) == 20
    assert unhedged_contracts([20, 10, 10]) == 10
    assert unhedged_contracts([-10, -10, -10]) == 0


def test_optimistic_fill_model_fills_at_least_as_much():
    snaps = [book(t, [(50, 40)], [(53, 40)]) for t in range(4)]
    trades = [{"timestamp": 2.5, "market_id": "M", "price": 50, "size": 20, "aggressor": "sell"}]
    data = make_data(snaps, trades)
    back = BacktestEngine(data, OneShot([PlaceLimit("M", Side.BUY, 50, 10)]),
                          BacktestConfig(fees=NO_FEES)).run()
    front = BacktestEngine(data, OneShot([PlaceLimit("M", Side.BUY, 50, 10)]),
                           BacktestConfig(fees=NO_FEES, fill_model=FillModel(queue_position="front"))).run()
    assert back.fills.empty  # 40 contracts were ahead of us and only 20 traded
    assert front.fills["size"].sum() == 10


def test_metrics_work_when_a_strategy_never_trades():
    from backtest.metrics import basket_summary, performance_summary

    snaps = [book(t, [(50, 10)], [(52, 10)]) for t in range(3)]
    result = BacktestEngine(make_data(snaps), OneShot([]), BacktestConfig()).run()
    summary = performance_summary(result)
    assert summary["n_fills"] == 0 and summary["net_pnl"] == 0
    assert basket_summary(result)["baskets_sent"] == 0


def test_starting_balance_limits_order_size():
    snaps = [book(t, [(40, 100)], [(50, 100)]) for t in range(3)]
    trades = [{"timestamp": 1.5, "market_id": "M", "price": 30, "size": 100, "aggressor": "sell"}]
    strategy = OneShot([PlaceLimit("M", Side.BUY, 45, 100)])
    result = BacktestEngine(make_data(snaps, trades), strategy,
                            BacktestConfig(fees=NO_FEES, starting_balance=9.0)).run()
    assert result.orders["size"].tolist() == [20]  # $9 buys 20 contracts at 45c
    assert result.fills["size"].sum() == 20


def test_late_trade_cannot_fill_an_order_placed_after_it():
    # Paper-trading situation: the engine receives a trade stamped before our
    # order was active. It must not fill us.
    from backtest.events import BookEvent, TradeEvent

    snaps = [book(0, [(50, 10)], [(52, 10)]), book(1, [(50, 10)], [(52, 10)])]
    engine = BacktestEngine(make_data(snaps), OneShot([PlaceLimit("M", Side.BUY, 51, 10)]),
                            BacktestConfig(fees=NO_FEES), events=[])
    engine.reset()
    for snap in snaps:
        engine.process(BookEvent(snap.timestamp, "M", snap))
    engine.process(TradeEvent(0.8, "M", 49, 5, Side.SELL))  # happened before activation at t=1
    assert engine.result().fills.empty
    engine.process(TradeEvent(1.2, "M", 49, 5, Side.SELL))
    assert engine.result().fills["size"].sum() == 5
