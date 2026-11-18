"""The market maker's jump guard and the engine's on_trade hook."""

from backtest.engine import BacktestConfig, BacktestEngine
from backtest.fees import FeeModel
from backtest.orders import Side
from strategies.market_maker import InventorySkewMarketMaker, MarketMakerConfig
from tests.helpers import book, make_data

NO_FEES = FeeModel(enabled=False)


def run(snaps, trades, guard: bool):
    strategy = InventorySkewMarketMaker(MarketMakerConfig(market_id="M", jump_guard=guard, guard_cooldown_s=30.0))
    result = BacktestEngine(make_data(snaps, trades), strategy, BacktestConfig(fees=NO_FEES)).run()
    return strategy, result


def test_ask_collapse_pulls_our_ask_before_the_sweep():
    # Asks at 51 shrink from 1000 to 50 at t=3 (makers pulling), then at t=8 buyers sweep to 60.
    snaps = [book(t, [(50, 1000)], [(51, 1000)]) for t in (0, 1, 2)]
    snaps += [book(3, [(50, 1000)], [(51, 50)]), book(4, [(50, 1000)], [(51, 50)]),
              book(9, [(59, 100)], [(60, 100)])]
    sweep = [{"timestamp": 8.0, "market_id": "M", "price": p, "size": 500, "aggressor": "buy"} for p in (51, 52, 55)]
    _, plain = run(snaps, sweep, guard=False)
    strategy, guarded = run(snaps, sweep, guard=True)
    sold = lambda r: r.fills.loc[r.fills["side"] == "sell", "size"].sum() if not r.fills.empty else 0.0
    assert sold(plain) == 10           # without the guard our ask at 52 is run over
    assert sold(guarded) == 0          # with it, the ask was cancelled at t=3.5
    assert strategy.guard_triggers[0] == (3, "ask collapse")  # only the ask side is pulled here...
    assert strategy.guard_triggers[1] == (8.0, "trade through touch")  # ...then the sweep pulls both
    assert strategy.paused_until[Side.SELL] == strategy.paused_until[Side.BUY] == 38.0


def test_small_levels_do_not_trigger_the_guard():
    snaps = [book(0, [(50, 20)], [(51, 20)]), book(1, [(50, 20)], [(51, 1)])]
    strategy, _ = run(snaps, [], guard=True)
    assert strategy.guard_triggers == []


def test_trade_through_the_touch_pulls_both_sides():
    snaps = [book(t, [(50, 1000)], [(51, 1000)]) for t in range(0, 40)]
    trades = [{"timestamp": 5.2, "market_id": "M", "price": 53, "size": 5, "aggressor": "buy"}]
    strategy, result = run(snaps, trades, guard=True)
    assert strategy.guard_triggers == [(5.2, "trade through touch")]
    orders = result.orders
    cancelled_at_trade = orders[(orders["submitted_at"] < 5.2) & (orders["status"] == "cancelled")]
    assert set(cancelled_at_trade["side"]) == {"buy", "sell"}
    # No new quotes during the 30 s cooldown; quoting resumes afterwards.
    placed = orders["submitted_at"]
    assert not ((placed > 5.2) & (placed < 35.2)).any()
    assert (placed >= 35.2).any()
