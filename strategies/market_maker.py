"""A simple inventory-aware market maker for one binary contract.

Idea: quote a bid below and an ask above a fair value, earn the spread when
both sides trade, and avoid building a large position.

Each time the market's book updates:

1. **Fair value** = the mid price (or the microprice, if configured). This is
   deliberately simple: it uses only the current visible book, so it has no
   view of its own about where the price should be.
2. **Inventory skew.** Shift the centre of our quotes against our position:
   ``reservation = fair - skew_cents_at_max * position / max_position``.
   If we are long, both quotes move down, so we are less likely to buy more
   and more likely to sell. This is the core idea of the Avellaneda-Stoikov
   model, in its simplest linear form.
3. **Quotes.** ``bid = floor(reservation - half_spread)`` and
   ``ask = ceil(reservation + half_spread)``, then pushed back if needed so
   they never cross the visible book (we only want to be a maker).
4. **Position limits.** Bid size shrinks to zero as the position approaches
   ``+max_position`` and ask size shrinks to zero as it approaches
   ``-max_position``. The engine also enforces a hard limit.
5. **Keep queue priority.** If the existing order on a side is already at the
   desired price, leave it alone instead of cancelling and re-sending, which
   would put us at the back of the queue again.
6. **Jump guard (optional).** Pull quotes when the book says news is
   arriving, and stay out for ``guard_cooldown_s``:

   * *Touch collapse*: the best price on one side is unchanged but most of
     its size vanished since the previous snapshot. Usually faster makers
     cancelling ahead of a move, so our quote on that side is pulled.
   * *Trade through the touch*: a public trade prints beyond the best bid or
     ask we last saw, so our view of the book is stale. Both sides are pulled.

   Cancels still take the engine's latency to arrive, so the guard cannot
   avoid fills that happen within that time.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from backtest.orders import Side
from orderbook.book import MAX_PRICE, MIN_PRICE
from strategies.base import Action, Cancel, PlaceLimit, Strategy, StrategyContext

if TYPE_CHECKING:
    from backtest.events import TradeEvent


@dataclass(frozen=True)
class MarketMakerConfig:
    """Parameters of :class:`InventorySkewMarketMaker`.

    Attributes:
        market_id: Market to quote.
        half_spread_cents: Distance from the reservation price to each quote
            before rounding outwards to whole cents.
        quote_size: Contracts per quote.
        max_position: Strategy-level position limit (should be no larger than
            the engine's limit).
        skew_cents_at_max: How far the reservation price moves when the
            position reaches ``max_position``. Zero disables skewing.
        fair_value: ``"mid"`` or ``"microprice"``.
        jump_guard: Turn on the jump guard (point 6 in the module docstring).
        guard_cooldown_s: How long a side stays pulled after a trigger.
        guard_depletion: Share of the best level's size that must vanish
            between two snapshots (same price) to count as a touch collapse.
        guard_min_touch: Ignore collapses of levels smaller than this many
            contracts; small levels come and go all the time.
        guard_through_ticks: How many cents beyond the last seen best bid/ask
            a trade must print to count as "through the touch". With
            snapshots a few seconds apart, 1 cent happens on every ordinary
            tick, so the default is 2.
        guard_on_collapse / guard_on_trade_through: Switch each trigger.
    """

    market_id: str
    half_spread_cents: float = 1.0
    quote_size: float = 10.0
    max_position: float = 100.0
    skew_cents_at_max: float = 3.0
    fair_value: str = "mid"
    jump_guard: bool = False
    guard_cooldown_s: float = 30.0
    guard_depletion: float = 0.8
    guard_min_touch: float = 50.0
    guard_through_ticks: int = 2
    guard_on_collapse: bool = True
    guard_on_trade_through: bool = True


class InventorySkewMarketMaker(Strategy):
    """Quotes both sides around fair value, skewed by inventory."""

    def __init__(self, config: MarketMakerConfig) -> None:
        self.config = config
        self.name = "market_maker"
        self.markets = [config.market_id]
        self.paused_until = {Side.BUY: float("-inf"), Side.SELL: float("-inf")}
        self.guard_triggers: list[tuple[float, str]] = []  # (time, reason), for analysis
        self._last_touch: dict[Side, tuple[int | None, float]] = {Side.BUY: (None, 0.0), Side.SELL: (None, 0.0)}

    # ------------------------------------------------------------------ jump guard
    def _pause(self, sides: tuple[Side, ...], now: float, reason: str) -> None:
        for side in sides:
            self.paused_until[side] = max(self.paused_until[side], now + self.config.guard_cooldown_s)
        self.guard_triggers.append((now, reason))

    def _check_touch_collapse(self, context: StrategyContext) -> None:
        """Pause a side whose best level kept its price but lost most of its size."""
        cfg = self.config
        book = context.books[cfg.market_id]
        current = {Side.BUY: (book.best_bid, book.best_bid_size or 0.0),
                   Side.SELL: (book.best_ask, book.best_ask_size or 0.0)}
        for side, (price, size) in current.items():
            last_price, last_size = self._last_touch[side]
            if (price is not None and price == last_price and last_size >= cfg.guard_min_touch
                    and size <= (1.0 - cfg.guard_depletion) * last_size):
                # Bids vanishing warn of a fall (pull our bid); asks vanishing warn of a rise.
                self._pause((side,), context.timestamp, f"{'bid' if side is Side.BUY else 'ask'} collapse")
        self._last_touch = current

    def on_trade(self, trade: "TradeEvent", context: StrategyContext) -> list[Action]:
        """With the guard on, pull everything when a trade prints through the touch we last saw."""
        cfg = self.config
        if (not cfg.jump_guard or not cfg.guard_on_trade_through or trade.market_id != cfg.market_id
                or cfg.market_id not in context.books):
            return []
        book = context.books[cfg.market_id]
        ticks = cfg.guard_through_ticks
        through = ((book.best_ask is not None and trade.price >= book.best_ask + ticks)
                   or (book.best_bid is not None and trade.price <= book.best_bid - ticks))
        if not through:
            return []
        self._pause((Side.BUY, Side.SELL), context.timestamp, "trade through touch")
        return [Cancel(o.order_id) for o in context.orders_for(cfg.market_id) if not o.cancel_requested]

    def fair_value(self, context: StrategyContext) -> float | None:
        """Current fair value estimate in cents, or None if the book is one-sided."""
        book = context.books[self.config.market_id]
        if self.config.fair_value == "microprice":
            return book.microprice
        return book.mid

    def desired_quotes(self, context: StrategyContext) -> dict[Side, tuple[int, float]]:
        """Return {side: (price, size)} for the quotes we want right now.

        A size of 0 means "no quote on this side".
        """
        cfg = self.config
        book = context.books[cfg.market_id]
        fair = self.fair_value(context)
        if fair is None or book.best_bid is None or book.best_ask is None:
            return {Side.BUY: (0, 0.0), Side.SELL: (0, 0.0)}

        position = context.positions.get(cfg.market_id, 0.0)
        reservation = fair - cfg.skew_cents_at_max * position / cfg.max_position

        bid_price = math.floor(reservation - cfg.half_spread_cents)
        ask_price = math.ceil(reservation + cfg.half_spread_cents)
        # Never cross the visible book: we want to be a maker, not a taker.
        bid_price = min(bid_price, book.best_ask - 1)
        ask_price = max(ask_price, book.best_bid + 1)
        bid_price = max(MIN_PRICE, min(MAX_PRICE, bid_price))
        ask_price = max(MIN_PRICE, min(MAX_PRICE, ask_price))

        bid_size = max(0.0, min(cfg.quote_size, cfg.max_position - position))
        ask_size = max(0.0, min(cfg.quote_size, cfg.max_position + position))
        if bid_price >= ask_price:  # can only happen at the price limits
            bid_size = ask_size = 0.0
        return {Side.BUY: (bid_price, bid_size), Side.SELL: (ask_price, ask_size)}

    def on_book_update(self, market_id: str, context: StrategyContext) -> list[Action]:
        """Cancel quotes that are at the wrong price and place the ones we want."""
        if market_id != self.config.market_id:
            return []
        if self.config.jump_guard and self.config.guard_on_collapse:
            self._check_touch_collapse(context)
        actions: list[Action] = []
        for side, (price, size) in self.desired_quotes(context).items():
            if context.timestamp < self.paused_until[side]:
                size = 0.0  # side pulled by the jump guard: cancel and do not re-quote
            existing = [o for o in context.orders_for(market_id, side) if not o.cancel_requested]
            keep_existing = False
            for order in existing:
                if size > 0 and order.price == price and not keep_existing:
                    keep_existing = True  # already where we want it: keep queue position
                else:
                    actions.append(Cancel(order.order_id))
            if size > 0 and not keep_existing:
                actions.append(PlaceLimit(market_id, side, price, size))
        return actions
