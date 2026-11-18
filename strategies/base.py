"""The interface between strategies and the backtest engine.

A strategy never touches the engine's internals. On every book update in a
market it subscribes to, the engine calls :meth:`Strategy.on_book_update`
with a read-only :class:`StrategyContext` describing what is known *now*,
and the strategy answers with a list of actions. The engine then applies
latency, position limits, fees and the fill model.

To add a strategy: subclass :class:`Strategy`, set ``name`` and
``markets``, implement ``on_book_update``, and pass an instance to
``BacktestEngine``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping, Union

from backtest.orders import Fill, Side
from orderbook.book import OrderBookSnapshot

if TYPE_CHECKING:
    from backtest.events import TradeEvent


@dataclass(frozen=True)
class PlaceLimit:
    """Rest a passive (post-only) limit order on the book.

    If the order would cross the book when it becomes active, the engine
    rejects it rather than letting it trade as a taker.
    """

    market_id: str
    side: Side
    price: int
    size: float
    tag: str = ""


@dataclass(frozen=True)
class PlaceTaker:
    """Cross the spread immediately (immediate-or-cancel).

    Fills against the first snapshot after latency, at prices no worse than
    ``limit_price``. Whatever cannot be filled at once is cancelled.
    """

    market_id: str
    side: Side
    limit_price: int
    size: float
    tag: str = ""


@dataclass(frozen=True)
class Cancel:
    """Cancel an order. Takes effect after latency, so it can still fill meanwhile."""

    order_id: int


Action = Union[PlaceLimit, PlaceTaker, Cancel]


@dataclass(frozen=True)
class OpenOrderView:
    """What a strategy is allowed to see about one of its own live orders."""

    order_id: int
    market_id: str
    side: Side
    price: int
    remaining: float
    is_taker: bool
    is_active: bool
    cancel_requested: bool
    tag: str


@dataclass(frozen=True)
class StrategyContext:
    """Read-only snapshot of everything the strategy may know at ``timestamp``.

    Attributes:
        timestamp: Current time.
        books: Latest snapshot of each market at or before ``timestamp``.
            Markets with no snapshot yet are missing.
        positions: Current signed position per market.
        open_orders: This strategy's live orders.
        max_position: Engine-enforced absolute position limit per market.
    """

    timestamp: float
    books: Mapping[str, OrderBookSnapshot]
    positions: Mapping[str, float]
    open_orders: tuple[OpenOrderView, ...]
    max_position: float

    def orders_for(self, market_id: str, side: Side | None = None) -> list[OpenOrderView]:
        """Live orders in one market, optionally filtered by side."""
        return [o for o in self.open_orders
                if o.market_id == market_id and (side is None or o.side is side)]


class Strategy(ABC):
    """Base class for all strategies."""

    name: str = "strategy"
    markets: list[str] = []

    @abstractmethod
    def on_book_update(self, market_id: str, context: StrategyContext) -> list[Action]:
        """React to a new snapshot in ``market_id``.

        Args:
            market_id: The market whose book just changed.
            context: What is known right now.

        Returns:
            Actions to send to the engine (possibly empty).
        """

    def on_trade(self, trade: "TradeEvent", context: StrategyContext) -> list[Action]:
        """React to a public trade in one of ``markets``. Optional; default does nothing.

        Called after the engine has matched the trade against our resting
        orders, so any fill it caused is already in ``context.positions``.
        """
        return []

    def on_fill(self, fill: Fill, context: StrategyContext) -> None:
        """Called after each of this strategy's fills. Optional."""
