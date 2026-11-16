"""Orders and fills as the backtest engine tracks them."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Side(str, Enum):
    """Direction of an order, always in terms of the YES contract.

    Selling YES you do not own is treated as going short YES. On Kalshi you
    would do this by buying NO at ``100 - price``; the profit and loss is
    identical (see NOTES.md, "Short positions").
    """

    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        """+1 for buys, -1 for sells."""
        return 1 if self is Side.BUY else -1


class Liquidity(str, Enum):
    """Whether a fill added liquidity (maker) or removed it (taker)."""

    MAKER = "maker"
    TAKER = "taker"


class OrderStatus(str, Enum):
    """Lifecycle of an order inside the engine."""

    PENDING = "pending"  # submitted, waiting for latency to pass
    ACTIVE = "active"  # resting on the (simulated) book
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass
class Order:
    """An order submitted by a strategy.

    Attributes:
        order_id: Unique id assigned by the engine.
        market_id: Market the order is for.
        side: BUY or SELL (of YES).
        price: Limit price in cents.
        size: Original size in contracts.
        is_taker: True for immediate-or-cancel orders that cross the spread,
            False for passive resting orders.
        submitted_at: Time the strategy sent the order.
        active_from: Earliest time the order can interact with the market
            (``submitted_at + latency``).
        tag: Free text label set by the strategy, e.g. an arbitrage basket id.
        remaining: Size not yet filled.
        filled: Size filled so far.
        queue_ahead: Contracts estimated to be ahead of us at our price level.
        status: Current :class:`OrderStatus`.
        cancel_at: If set, the time a requested cancel takes effect.
        reject_reason: Why the engine rejected the order, if it did.
        activated_at: Time the order became active on the book, if it did.
    """

    order_id: int
    market_id: str
    side: Side
    price: int
    size: float
    is_taker: bool
    submitted_at: float
    active_from: float
    tag: str = ""
    remaining: float = 0.0
    filled: float = 0.0
    queue_ahead: float = 0.0
    status: OrderStatus = OrderStatus.PENDING
    cancel_at: float | None = None
    reject_reason: str = ""
    activated_at: float | None = None

    def __post_init__(self) -> None:
        if self.remaining == 0.0 and self.filled == 0.0:
            self.remaining = self.size

    @property
    def is_live(self) -> bool:
        """True while the order could still be filled."""
        return self.status in (OrderStatus.PENDING, OrderStatus.ACTIVE)


@dataclass(frozen=True)
class Fill:
    """One execution of (part of) an order.

    Attributes:
        timestamp: Time of the fill.
        order_id: The order that was filled.
        market_id: Market.
        side: Our side (BUY or SELL).
        price: Execution price in cents.
        size: Contracts filled.
        fee: Fee charged in dollars.
        liquidity: MAKER or TAKER.
        tag: Copied from the order.
        reason: Why the fill happened: "queue" (a trade at our price after
            the queue ahead was used up), "trade_through" (a trade at a
            worse price than ours), "book_cross" (a snapshot showed the market
            had moved through our price) or "taker" (we crossed the spread).
    """

    timestamp: float
    order_id: int
    market_id: str
    side: Side
    price: int
    size: float
    fee: float
    liquidity: Liquidity
    tag: str = ""
    reason: str = ""
