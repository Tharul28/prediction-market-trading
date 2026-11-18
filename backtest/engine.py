"""Event-driven backtest engine.

The engine replays market events in time order and, for each one:

1. applies any cancel requests whose latency has passed,
2. activates orders whose latency has passed (setting their queue position),
3. if the event is a public trade, lets the fill model decide whether any of
   our resting orders would have traded,
4. if the event is a book snapshot, updates the book, checks resting orders
   against it, executes due taker orders against it, and then asks the
   strategy what to do next,
5. records cash, positions and equity.

How lookahead is prevented
--------------------------
* The strategy only receives a :class:`StrategyContext` built from events
  already replayed. It never sees the event list, the truth path or the
  settlement.
* Every order and cancel takes effect only after ``latency_s`` and strictly
  after the event that triggered it. A taker order decided on the snapshot
  at time ``t`` executes against a *later* snapshot, not the one the
  strategy looked at.
* Equity is marked with the latest known mid price, never a future one.

Assumptions that are NOT realistic (also in the README):

* Our trades do not move the market: later snapshots and trades in the data
  are exactly as recorded, as if we were never there.
* Taker orders are filled from the displayed book with no hidden liquidity,
  and nobody else competes for the same liquidity at the same moment.
* Latency is a fixed number, not random.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType

import numpy as np
import pandas as pd

from backtest.events import BookEvent, Event, TradeEvent, build_event_stream
from backtest.fees import FeeModel
from backtest.fill_model import FillModel
from backtest.orders import Fill, Liquidity, Order, OrderStatus, Side
from backtest.portfolio import Portfolio
from data.schema import MarketData
from orderbook.book import MAX_PRICE, MIN_PRICE, OrderBookSnapshot
from strategies.base import Action, Cancel, OpenOrderView, PlaceTaker, Strategy, StrategyContext

SIZE_EPSILON = 1e-9


@dataclass(frozen=True)
class BacktestConfig:
    """Settings that belong to the simulated exchange, not the strategy.

    Attributes:
        latency_s: Delay between a strategy decision and it reaching the
            exchange (applies to new orders and cancels).
        max_position: Hard limit on absolute position per market. Orders that
            could take the position past it, counting all live orders on the
            same side, are reduced or rejected.
        fees: Fee model (switch off with ``FeeModel(enabled=False)``).
        fill_model: Passive fill rules.
        starting_balance: Optional cash balance in dollars. If set, orders
            are cut down or rejected when the account could not post the
            collateral (see :meth:`BacktestEngine.available_balance`). None
            means unlimited, which is the default for research backtests.
    """

    latency_s: float = 0.5
    max_position: float = 100.0
    fees: FeeModel = field(default_factory=FeeModel)
    fill_model: FillModel = field(default_factory=FillModel)
    starting_balance: float | None = None


@dataclass
class BacktestResult:
    """Everything produced by one backtest run.

    Attributes:
        strategy_name: Name of the strategy.
        config: The engine settings used.
        markets: Markets in the dataset.
        fills: One row per fill.
        orders: One row per order (including rejected ones).
        equity: Time series of cash, fees, mark-to-market equity and positions.
        net_pnl: Final mark-to-market profit after fees, in dollars.
        gross_pnl: ``net_pnl + fees_paid``.
        fees_paid: Total fees in dollars.
        liquidation_pnl: Net P&L if all positions were closed at the touch at the end.
        settlement_pnl: Net P&L after settlement, if the settlement is known.
    """

    strategy_name: str
    config: BacktestConfig
    markets: list[str]
    fills: pd.DataFrame
    orders: pd.DataFrame
    equity: pd.DataFrame
    net_pnl: float
    gross_pnl: float
    fees_paid: float
    liquidation_pnl: float
    settlement_pnl: float | None


class BacktestEngine:
    """Replays one dataset through one strategy."""

    def __init__(self, data: MarketData, strategy: Strategy,
                 config: BacktestConfig | None = None, events: list[Event] | None = None) -> None:
        """Set up the engine.

        Args:
            data: Market data in the standard format.
            strategy: The strategy to test.
            config: Exchange settings; defaults to :class:`BacktestConfig()`.
            events: Pre-built event stream (pass it to avoid rebuilding when
                running several backtests on the same data).
        """
        self.data = data
        self.strategy = strategy
        self.config = config or BacktestConfig()
        self.events = events if events is not None else build_event_stream(data)

    # ------------------------------------------------------------------ main loop
    def run(self) -> BacktestResult:
        """Run the backtest from the first event to the last."""
        self.reset()
        for event in self.events:
            self.process(event)
        return self.result()

    def reset(self) -> None:
        """Clear all state. ``run`` calls this; paper trading calls it once at start-up."""
        self.portfolio = Portfolio(self.data.markets)
        self.books: dict[str, OrderBookSnapshot] = {}
        self.last_mid: dict[str, float] = {}
        self.orders: dict[int, Order] = {}
        self.live_orders: list[Order] = []
        self.fills: list[Fill] = []
        self.equity_rows: list[tuple] = []
        self.next_order_id = 1

    def process(self, event: Event) -> None:
        """Advance the simulated exchange by one event.

        The backtest feeds events in perfect time order. Paper trading feeds
        them as they arrive from the live API, which is the same code path.
        """
        now = event.timestamp
        self._apply_due_cancels(now)
        self._activate_due_orders(now)
        if isinstance(event, TradeEvent):
            self._on_trade(event)
        else:
            self._on_book(event)

    def result(self) -> BacktestResult:
        """Summarise everything processed so far."""
        return self._build_result()

    # ------------------------------------------------------------- order lifecycle
    def _apply_due_cancels(self, now: float) -> None:
        """Cancel orders whose cancel request has reached the exchange."""
        for order in self.live_orders:
            if order.cancel_at is not None and order.cancel_at <= now:
                order.status = OrderStatus.CANCELLED
        self._prune_live_orders()

    def _activate_due_orders(self, now: float) -> None:
        """Put pending passive orders on the book once their latency has passed."""
        for order in self.live_orders:
            if order.status is not OrderStatus.PENDING or order.is_taker:
                continue
            if now < order.active_from or now <= order.submitted_at:
                continue
            book = self.books.get(order.market_id)
            if book is None:
                self._reject(order, "no book yet")
                continue
            # Post-only: a passive order that would cross the current book is rejected.
            if order.side is Side.BUY and book.best_ask is not None and order.price >= book.best_ask:
                self._reject(order, "post-only order would cross")
                continue
            if order.side is Side.SELL and book.best_bid is not None and order.price <= book.best_bid:
                self._reject(order, "post-only order would cross")
                continue
            order.status = OrderStatus.ACTIVE
            order.activated_at = now
            order.queue_ahead = self.config.fill_model.initial_queue_ahead(order, book)
        self._prune_live_orders()

    def _reject(self, order: Order, reason: str) -> None:
        order.status = OrderStatus.REJECTED
        order.reject_reason = reason

    def _prune_live_orders(self) -> None:
        self.live_orders = [order for order in self.live_orders if order.is_live]

    # --------------------------------------------------------------- event handlers
    def _on_trade(self, event: TradeEvent) -> None:
        """Check a public trade against our resting orders in that market."""
        # The activation-time check only matters in paper trading, where a trade can
        # arrive after we placed an order even though it happened before.
        resting = [o for o in self.live_orders
                   if o.market_id == event.market_id and o.status is OrderStatus.ACTIVE and not o.is_taker
                   and o.activated_at is not None and o.activated_at <= event.timestamp]
        if resting:
            allocations = self.config.fill_model.match_trade(resting, event.price, event.size, event.aggressor)
            for order, size in allocations:
                # A resting order always trades at its own limit price.
                reason = "queue" if order.price == event.price else "trade_through"
                self._fill(order, size, order.price, Liquidity.MAKER, event.timestamp, reason)
            self._prune_live_orders()
        if event.market_id in self.strategy.markets:
            actions = self.strategy.on_trade(event, self._context(event.timestamp))
            self._handle_actions(actions, event.timestamp)

    def _on_book(self, event: BookEvent) -> None:
        """Process a new snapshot: resting orders, taker orders, then the strategy."""
        book = event.book
        market = event.market_id
        now = event.timestamp
        self.books[market] = book
        if book.mid is not None:
            self.last_mid[market] = book.mid

        for order in [o for o in self.live_orders
                      if o.market_id == market and o.status is OrderStatus.ACTIVE and not o.is_taker]:
            size = self.config.fill_model.update_on_book(order, book)
            if size > SIZE_EPSILON:
                self._fill(order, size, order.price, Liquidity.MAKER, now, "book_cross")

        self._execute_due_takers(book, now)
        self._prune_live_orders()

        if market in self.strategy.markets:
            actions = self.strategy.on_book_update(market, self._context(now))
            self._handle_actions(actions, now)

        self._record_equity(now)

    def _execute_due_takers(self, book: OrderBookSnapshot, now: float) -> None:
        """Fill due immediate-or-cancel orders from the displayed book, then cancel the rest."""
        consumed: dict[tuple[str, int], float] = {}
        for order in self.live_orders:
            if not order.is_taker or order.market_id != book.market_id:
                continue
            if order.status is not OrderStatus.PENDING or now < order.active_from or now <= order.submitted_at:
                continue
            order.status = OrderStatus.ACTIVE
            order.activated_at = now
            levels = book.asks if order.side is Side.BUY else book.bids
            side_key = "ask" if order.side is Side.BUY else "bid"
            for price, size in levels:
                if order.side is Side.BUY and price > order.price:
                    break
                if order.side is Side.SELL and price < order.price:
                    break
                available = size - consumed.get((side_key, price), 0.0)
                take = min(order.remaining, available)
                if take > SIZE_EPSILON:
                    consumed[(side_key, price)] = consumed.get((side_key, price), 0.0) + take
                    self._fill(order, take, price, Liquidity.TAKER, now, "taker")
                if order.remaining <= SIZE_EPSILON:
                    break
            if order.remaining > SIZE_EPSILON:
                order.status = OrderStatus.CANCELLED  # immediate-or-cancel

    def _fill(self, order: Order, size: float, price: int, liquidity: Liquidity, now: float,
              reason: str) -> None:
        """Record a fill and update the order, portfolio and strategy."""
        fee = self.config.fees.fee(price, size, liquidity)
        fill = Fill(now, order.order_id, order.market_id, order.side, price, size, fee, liquidity,
                    order.tag, reason)
        self.portfolio.apply_fill(fill)
        order.remaining -= size
        order.filled += size
        if order.remaining <= SIZE_EPSILON:
            order.remaining = 0.0
            order.status = OrderStatus.FILLED
        self.fills.append(fill)
        self.strategy.on_fill(fill, self._context(now))

    # ------------------------------------------------------------- strategy actions
    def _context(self, now: float) -> StrategyContext:
        """Build the read-only view handed to the strategy."""
        views = tuple(
            OpenOrderView(o.order_id, o.market_id, o.side, o.price, o.remaining, o.is_taker,
                          o.status is OrderStatus.ACTIVE, o.cancel_at is not None, o.tag)
            for o in self.live_orders
        )
        return StrategyContext(
            timestamp=now,
            books=MappingProxyType(dict(self.books)),
            positions=MappingProxyType(dict(self.portfolio.positions)),
            open_orders=views,
            max_position=self.config.max_position,
        )

    def _room(self, market: str, side: Side) -> float:
        """Largest new order size that keeps the worst-case position within the limit."""
        position = self.portfolio.positions.get(market, 0.0)
        same_side = sum(o.remaining for o in self.live_orders if o.market_id == market and o.side is side)
        if side is Side.BUY:
            return self.config.max_position - position - same_side
        return self.config.max_position + position - same_side

    def available_balance(self) -> float:
        """Cash left for new orders, using Kalshi-style collateral, in dollars.

        On Kalshi a YES costs its price and a short YES (bought as a NO)
        costs ``100 - price``. In this project's accounting a short *receives*
        the price, so Kalshi's cash is our cash minus $1 per contract short.
        Live orders reserve their worst-case cost. Selling YES you already
        hold also reserves collateral here, which is conservative.
        """
        if self.config.starting_balance is None:
            return float("inf")
        short_contracts = sum(max(-p, 0.0) for p in self.portfolio.positions.values())
        cash = self.config.starting_balance + self.portfolio.cash - short_contracts
        reserved = sum(self._order_cost(o.side, o.price, o.remaining) for o in self.live_orders)
        return cash - reserved

    @staticmethod
    def _order_cost(side: Side, price: int, size: float) -> float:
        """Collateral in dollars for an order (YES at price, or NO at 100 - price)."""
        unit = price if side is Side.BUY else 100 - price
        return size * unit / 100.0

    def _handle_actions(self, actions: list[Action], now: float) -> None:
        """Validate strategy actions and turn them into pending orders or cancels."""
        for action in actions:
            if isinstance(action, Cancel):
                order = self.orders.get(action.order_id)
                if order is not None and order.is_live and order.cancel_at is None:
                    order.cancel_at = now + self.config.latency_s
                continue

            is_taker = isinstance(action, PlaceTaker)
            price = action.limit_price if isinstance(action, PlaceTaker) else action.price
            order = Order(
                order_id=self.next_order_id, market_id=action.market_id, side=action.side,
                price=int(price), size=float(action.size), is_taker=is_taker,
                submitted_at=now, active_from=now + self.config.latency_s, tag=action.tag,
            )
            self.next_order_id += 1
            self.orders[order.order_id] = order

            if action.market_id not in self.portfolio.positions:
                self._reject(order, "unknown market")
            elif not MIN_PRICE <= order.price <= MAX_PRICE:
                self._reject(order, "price out of range")
            elif order.size <= 0:
                self._reject(order, "non-positive size")
            else:
                room = self._room(order.market_id, order.side)
                unit_cost = self._order_cost(order.side, order.price, 1.0)
                affordable = float(np.floor(self.available_balance() / unit_cost + 1e-9)) if unit_cost > 0 else room
                if room <= SIZE_EPSILON:
                    self._reject(order, "position limit")
                elif affordable < 1:
                    self._reject(order, "insufficient balance")
                else:
                    size = min(order.size, room, affordable)
                    order.size = size
                    order.remaining = size
                    self.live_orders.append(order)

    # ----------------------------------------------------------------- recording
    def _record_equity(self, now: float) -> None:
        marks = {m: self.last_mid.get(m, 50.0) for m in self.data.markets}
        net = self.portfolio.mark_to_market(marks)
        positions = tuple(self.portfolio.positions[m] for m in self.data.markets)
        self.equity_rows.append((now, self.portfolio.cash, self.portfolio.fees_paid, net) + positions)

    def _build_result(self) -> BacktestResult:
        markets = self.data.markets
        equity = pd.DataFrame(self.equity_rows,
                              columns=["timestamp", "cash", "fees_paid", "net_equity"]
                              + [f"position_{m}" for m in markets])
        equity["gross_equity"] = equity["net_equity"] + equity["fees_paid"]

        fills = pd.DataFrame([{
            "timestamp": f.timestamp, "order_id": f.order_id, "market_id": f.market_id,
            "side": f.side.value, "price": f.price, "size": f.size, "fee": f.fee,
            "liquidity": f.liquidity.value, "tag": f.tag, "reason": f.reason,
        } for f in self.fills], columns=["timestamp", "order_id", "market_id", "side", "price",
                                          "size", "fee", "liquidity", "tag", "reason"])
        orders = pd.DataFrame([{
            "order_id": o.order_id, "market_id": o.market_id, "side": o.side.value, "price": o.price,
            "size": o.size, "filled": o.filled, "is_taker": o.is_taker, "status": o.status.value,
            "submitted_at": o.submitted_at, "activated_at": o.activated_at, "tag": o.tag,
            "reject_reason": o.reject_reason,
        } for o in self.orders.values()], columns=["order_id", "market_id", "side", "price", "size",
                                                    "filled", "is_taker", "status", "submitted_at",
                                                    "activated_at", "tag", "reject_reason"])

        net_pnl = float(equity["net_equity"].iloc[-1]) if not equity.empty else 0.0
        fees_paid = self.portfolio.fees_paid
        settlement = None
        if self.data.settlement is not None:
            settlement = self.portfolio.settlement_value(self.data.settlement)
        return BacktestResult(
            strategy_name=self.strategy.name,
            config=self.config,
            markets=markets,
            fills=fills,
            orders=orders,
            equity=equity,
            net_pnl=net_pnl,
            gross_pnl=net_pnl + fees_paid,
            fees_paid=fees_paid,
            liquidation_pnl=self.portfolio.liquidation_value(self.books),
            settlement_pnl=settlement,
        )
