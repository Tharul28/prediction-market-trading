"""Paper trading: run a strategy live against Kalshi public data with a fake balance.

Nothing here sends orders to Kalshi. The paper trader:

1. polls the order books and recent trades of the chosen markets every N
   seconds, using the same collector as ``data/kalshi.py`` (so the market
   data it saw is saved too, in ``<out>/market_data/``),
2. feeds what it saw, as it arrives, into the **same** ``BacktestEngine``
   used for backtests: same strategy code, fill model, fees, latency,
   position limits, plus a cash balance,
3. appends every order event, fill and the running P&L to CSV files.

Files in ``<out>/``:

* ``paper_config.json``: strategy and engine settings (used on restart).
* ``orders.csv``: one row each time an order is created or changes status.
* ``fills.csv``: one row per simulated fill.
* ``equity.csv``: one row per poll: balance, P&L, fees, positions, marks.
* ``paper_state.json``: cash, positions and fees, saved every poll.
* ``market_data/``: the raw books and trades (collector format).

Restarting with the same ``--out`` resumes: the collector resumes, and the
portfolio (cash, positions, fees) is restored. Orders that were open when
the process stopped are treated as cancelled, and the strategy starts
quoting again from scratch.

Usage::

    python -m paper.trader run --strategy mm --markets TICKER --balance 1000 --interval 5 --out paper_runs/mm1
    python -m paper.trader run --strategy arb --event EVENT_TICKER --exhaustive --out paper_runs/arb1
    python -m paper.trader run --out paper_runs/mm1          # resume with the saved settings
    python -m paper.trader summary --out paper_runs/mm1      # daily summary + backtest comparison

Why paper results can differ from a backtest on the same period: trades are
only discovered at the next poll, so live, a trade can arrive after book
snapshots that came later in time. The backtest replays everything in exact
time order. ``summary`` measures this gap.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from backtest.engine import BacktestConfig, BacktestEngine
from backtest.events import BookEvent, Event, TradeEvent
from backtest.fees import FeeModel
from backtest.orders import Order, Side
from data.kalshi import DEFAULT_BASE_URL, CollectorConfig, KalshiClient, KalshiCollector
from data.schema import BOOK_COLUMNS, TRADE_COLUMNS, MarketData
from strategies.base import Strategy
from strategies.cross_outcome_arb import ArbConfig, CrossOutcomeArbitrage
from strategies.market_maker import InventorySkewMarketMaker, MarketMakerConfig

ORDER_LOG_COLUMNS = ["logged_at", "order_id", "market_id", "side", "price", "size", "filled", "remaining",
                     "is_taker", "status", "reject_reason", "tag", "submitted_at"]
FILL_LOG_COLUMNS = ["timestamp", "logged_at", "order_id", "market_id", "side", "price", "size", "fee",
                    "liquidity", "reason", "tag"]


@dataclass
class PaperConfig:
    """Everything needed to (re)start a paper trading run.

    Attributes:
        markets: Market tickers to trade and record.
        strategy: ``"mm"`` (market maker on ``markets[0]``) or ``"arb"``.
        strategy_params: Extra strategy settings, e.g. ``{"half_spread_cents": 1.0}``.
        interval_s: Seconds between polls.
        starting_balance: Fake cash in dollars.
        latency_s, max_position: Engine settings, as in backtests.
        fees_enabled, taker_rate, maker_rate: Fee model settings.
        mutually_exclusive, exhaustive: Event structure (needed for "arb").
        depth: Order book levels requested per side.
    """

    markets: list[str]
    strategy: str = "mm"
    strategy_params: dict[str, Any] = field(default_factory=dict)
    interval_s: float = 5.0
    starting_balance: float = 1000.0
    latency_s: float = 0.5
    max_position: float = 100.0
    fees_enabled: bool = True
    taker_rate: float = 0.07
    maker_rate: float = 0.0175
    mutually_exclusive: bool = False
    exhaustive: bool = False
    depth: int = 10

    def fee_model(self) -> FeeModel:
        return FeeModel(self.fees_enabled, self.taker_rate, self.maker_rate)

    def backtest_config(self) -> BacktestConfig:
        """The engine settings, shared by paper trading and the comparison backtest."""
        return BacktestConfig(latency_s=self.latency_s, max_position=self.max_position,
                              fees=self.fee_model(), starting_balance=self.starting_balance)

    def build_strategy(self) -> Strategy:
        """Create the strategy described by this config."""
        if self.strategy == "mm":
            params = {"max_position": self.max_position, **self.strategy_params}
            return InventorySkewMarketMaker(MarketMakerConfig(market_id=self.markets[0], **params))
        if self.strategy == "arb":
            params = {"max_position": self.max_position, **self.strategy_params}
            return CrossOutcomeArbitrage(ArbConfig(
                market_ids=tuple(self.markets), outcomes_mutually_exclusive=self.mutually_exclusive,
                outcomes_exhaustive=self.exhaustive, **params), self.fee_model())
        raise ValueError(f"Unknown strategy {self.strategy!r}: use 'mm' or 'arb'")

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def load(cls, path: Path) -> "PaperConfig":
        return cls(**json.loads(path.read_text()))


def _append_csv(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    """Append rows (header only for a new file) and flush them to disk."""
    if not rows:
        return
    is_new = not path.exists() or path.stat().st_size == 0
    with open(path, "a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        if is_new:
            writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())


class PaperTrader:
    """Runs one strategy live, through the backtest engine, with logging."""

    def __init__(self, client: Any, config: PaperConfig, out_dir: str | Path,
                 clock: Callable[[], float] = time.time) -> None:
        """Args:
            client: A ``KalshiClient`` (or a test replay with the same methods).
            config: Paper trading settings.
            out_dir: Output directory; reusing it resumes the run.
            clock: Returns the current Unix time. Only replaced in tests.
        """
        self.config = config
        self.clock = clock
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        config.save(self.out_dir / "paper_config.json")
        self.orders_path = self.out_dir / "orders.csv"
        self.fills_path = self.out_dir / "fills.csv"
        self.equity_path = self.out_dir / "equity.csv"
        self.state_path = self.out_dir / "paper_state.json"

        self.collector = KalshiCollector(client, CollectorConfig(
            out_dir=self.out_dir / "market_data", tickers=list(config.markets), interval_s=config.interval_s,
            depth=config.depth, mutually_exclusive=config.mutually_exclusive, exhaustive=config.exhaustive,
            description=f"Recorded by paper trader ({config.strategy})"), clock=clock)

        empty = MarketData(books=pd.DataFrame(columns=BOOK_COLUMNS), trades=pd.DataFrame(columns=TRADE_COLUMNS),
                           markets=list(config.markets), mutually_exclusive=config.mutually_exclusive,
                           exhaustive=config.exhaustive)
        self.strategy = config.build_strategy()
        self.engine = BacktestEngine(empty, self.strategy, config.backtest_config(), events=[])
        self.engine.reset()
        self.order_status_logged: dict[int, str] = {}
        self._restore_state()

    # ------------------------------------------------------------------ state
    def _restore_state(self) -> None:
        """Restore cash, positions and fees after a restart (open orders are not restored)."""
        if not self.state_path.exists():
            return
        state = json.loads(self.state_path.read_text())
        portfolio = self.engine.portfolio
        portfolio.cash = state["cash"]
        portfolio.fees_paid = state["fees_paid"]
        for market, position in state["positions"].items():
            portfolio.positions[market] = position
        self.engine.next_order_id = state["next_order_id"]
        self.engine.last_mid.update(state.get("last_mid", {}))
        print(f"Resumed: positions {state['positions']}, P&L so far ${state.get('net_pnl', 0.0):.2f}. "
              f"Orders open at shutdown are treated as cancelled.")

    def _save_state(self, net_pnl: float) -> None:
        portfolio = self.engine.portfolio
        state = {"cash": portfolio.cash, "fees_paid": portfolio.fees_paid, "positions": portfolio.positions,
                 "next_order_id": self.engine.next_order_id, "last_mid": self.engine.last_mid,
                 "net_pnl": net_pnl, "updated": self.clock()}
        temporary = self.state_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(state, indent=2))
        os.replace(temporary, self.state_path)

    # ------------------------------------------------------------------ one poll
    def step(self) -> dict[str, Any]:
        """Poll once, feed the new events to the engine, and log the results.

        Returns:
            The equity row written for this poll.
        """
        snapshots, trades = self.collector.poll_once()
        events: list[tuple[float, int, Event]] = []
        for trade in trades:
            events.append((trade["timestamp"], 0, TradeEvent(trade["timestamp"], trade["market_id"],
                                                             int(trade["price"]), float(trade["size"]),
                                                             Side(trade["aggressor"]))))
        for snapshot in snapshots:
            events.append((snapshot.timestamp, 1, BookEvent(snapshot.timestamp, snapshot.market_id, snapshot)))
        # Within one poll we can sort by time; across polls we cannot go back, which is
        # the main way paper trading differs from the backtest.
        for _, _, event in sorted(events, key=lambda item: (item[0], item[1])):
            self.engine.process(event)
        return self._log()

    def _log(self) -> dict[str, Any]:
        now = self.clock()
        new_fills = list(self.engine.fills)
        self.engine.fills.clear()  # logged to disk; no need to keep them in memory
        _append_csv(self.fills_path, FILL_LOG_COLUMNS, [{
            "timestamp": f.timestamp, "logged_at": now, "order_id": f.order_id, "market_id": f.market_id,
            "side": f.side.value, "price": f.price, "size": f.size, "fee": f.fee,
            "liquidity": f.liquidity.value, "reason": f.reason, "tag": f.tag} for f in new_fills])

        order_rows = []
        for order in self.engine.orders.values():
            if self.order_status_logged.get(order.order_id) != order.status.value:
                order_rows.append(self._order_row(order, now))
                self.order_status_logged[order.order_id] = order.status.value
        _append_csv(self.orders_path, ORDER_LOG_COLUMNS, order_rows)
        # Keep memory bounded: finished orders will not change again.
        for order_id in [i for i, o in self.engine.orders.items() if not o.is_live]:
            del self.engine.orders[order_id]
            self.order_status_logged.pop(order_id, None)

        portfolio = self.engine.portfolio
        marks = {m: self.engine.last_mid.get(m, 50.0) for m in self.config.markets}
        net_pnl = portfolio.mark_to_market(marks)
        row: dict[str, Any] = {
            "timestamp": now, "balance": self.config.starting_balance + net_pnl, "net_pnl": net_pnl,
            "gross_pnl": net_pnl + portfolio.fees_paid, "fees_paid": portfolio.fees_paid,
            "cash": portfolio.cash, "available_balance": self.engine.available_balance(),
            "open_orders": len(self.engine.live_orders), "new_fills": len(new_fills),
        }
        for market in self.config.markets:
            row[f"position_{market}"] = portfolio.positions[market]
            row[f"mid_{market}"] = marks[market]
        _append_csv(self.equity_path, list(row.keys()), [row])
        self._save_state(net_pnl)
        self.engine.equity_rows.clear()  # the engine's in-memory history is not needed live
        return row

    @staticmethod
    def _order_row(order: Order, now: float) -> dict[str, Any]:
        return {"logged_at": now, "order_id": order.order_id, "market_id": order.market_id,
                "side": order.side.value, "price": order.price, "size": order.size, "filled": order.filled,
                "remaining": order.remaining, "is_taker": order.is_taker, "status": order.status.value,
                "reject_reason": order.reject_reason, "tag": order.tag, "submitted_at": order.submitted_at}

    def run(self, max_polls: int | None = None) -> None:
        """Poll every ``interval_s`` seconds until Ctrl+C (or ``max_polls``)."""
        print(f"Paper trading {self.config.strategy} on {self.config.markets} with "
              f"${self.config.starting_balance:,.0f}, polling every {self.config.interval_s}s. Ctrl+C to stop.")
        polls = 0
        try:
            while max_polls is None or polls < max_polls:
                started = time.time()
                row = self.step()
                polls += 1
                if polls % 12 == 1 or row["new_fills"]:
                    positions = {m: row[f"position_{m}"] for m in self.config.markets}
                    print(f"  {pd.Timestamp(row['timestamp'], unit='s'):%Y-%m-%d %H:%M:%S}  "
                          f"P&L ${row['net_pnl']:+.2f}  fees ${row['fees_paid']:.2f}  "
                          f"positions {positions}  new fills {row['new_fills']}")
                time.sleep(max(0.0, self.config.interval_s - (time.time() - started)))
        except KeyboardInterrupt:
            print("Stopped. Run the same command again to resume.")


def main() -> None:
    """Command line entry point: ``python -m paper.trader --help``."""
    parser = argparse.ArgumentParser(description="Paper trade a strategy on live Kalshi data")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="Start or resume paper trading")
    run_parser.add_argument("--out", required=True, help="Output directory (reuse it to resume)")
    run_parser.add_argument("--strategy", choices=["mm", "arb"])
    group = run_parser.add_mutually_exclusive_group()
    group.add_argument("--markets", nargs="+", help="Tickers (the market maker quotes the first one)")
    group.add_argument("--event", help="Use every active market of this event")
    run_parser.add_argument("--balance", type=float, default=1000.0)
    run_parser.add_argument("--interval", type=float, default=5.0)
    run_parser.add_argument("--half-spread", type=float, default=None, help="Market maker half-spread (cents)")
    run_parser.add_argument("--skew", type=float, default=None, help="Market maker skew at max position (cents)")
    run_parser.add_argument("--quote-size", type=float, default=None)
    run_parser.add_argument("--max-position", type=float, default=100.0)
    run_parser.add_argument("--no-fees", action="store_true")
    run_parser.add_argument("--mutually-exclusive", action="store_true")
    run_parser.add_argument("--exhaustive", action="store_true")

    summary_parser = sub.add_parser("summary", help="Daily summary and comparison with a backtest")
    summary_parser.add_argument("--out", required=True)
    summary_parser.add_argument("--timezone", default="Europe/London")

    args = parser.parse_args()
    if args.command == "summary":
        from paper.report import write_reports
        write_reports(Path(args.out), args.timezone)
        return

    client = KalshiClient(base_url=args.base_url)
    config_path = Path(args.out) / "paper_config.json"
    if config_path.exists() and not (args.markets or args.event):
        config = PaperConfig.load(config_path)
        print(f"Resuming with saved settings from {config_path}")
    else:
        if not args.strategy or not (args.markets or args.event):
            parser.error("a new run needs --strategy and --markets or --event")
        markets = list(args.markets or [])
        mutually_exclusive = args.mutually_exclusive
        if args.event:
            event, event_markets = client.get_event(args.event)
            markets = [m["ticker"] for m in event_markets if m.get("status") in (None, "active", "open")]
            mutually_exclusive = mutually_exclusive or bool(event.get("mutually_exclusive", False))
        params: dict[str, Any] = {}
        if args.strategy == "mm":
            for key, value in (("half_spread_cents", args.half_spread), ("skew_cents_at_max", args.skew),
                               ("quote_size", args.quote_size)):
                if value is not None:
                    params[key] = value
        config = PaperConfig(markets=markets, strategy=args.strategy, strategy_params=params,
                             interval_s=args.interval, starting_balance=args.balance,
                             max_position=args.max_position, fees_enabled=not args.no_fees,
                             mutually_exclusive=mutually_exclusive, exhaustive=args.exhaustive)
    PaperTrader(client, config, args.out).run()


if __name__ == "__main__":
    main()
