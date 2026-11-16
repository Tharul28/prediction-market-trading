"""When does a resting (passive) order count as filled?

This is the most important modelling decision in any market-making backtest,
because the historical data does not contain our orders. We have to guess how
the market would have treated them. The rules here are deliberately
conservative.

Rules for a resting order at price ``p`` (described for a BUY; SELL is the
mirror image):

1. **Back of the queue.** When our order becomes active we assume every
   contract already displayed at ``p`` is ahead of us
   (``queue_ahead = displayed size at p``). We are never assumed to be first.
2. **Queue only shrinks for good reasons.** A public trade at ``p`` uses up
   queue ahead of us before it can reach us. If a later snapshot shows less
   size at ``p`` than our ``queue_ahead``, the queue ahead of us can be at
   most what is displayed, so we cap it there. Cancellations by others are
   otherwise assumed to come from *behind* us (they do not help us).
3. **Trade-through.** If someone sells at a price *below* our bid, they
   would have hit our better bid first, so we fill up to the trade's size.
4. **Market moves through us.** If a snapshot shows an ask at or below our
   bid, a seller was willing to trade at our price, so we fill at our
   price, up to the size offered at or below it. This is how adverse
   selection shows up: these fills tend to happen just before the price
   keeps moving against us. Ignoring them would flatter the results, so the
   conservative choice is to include them.
5. **Volume is shared, not duplicated.** One public trade can fill at most
   its own size across all our orders.

The ``"front"`` queue setting assumes we are always first in the queue. It is
unrealistic and exists only so the README can show how much the queue
assumption changes the results.
"""

from __future__ import annotations

from dataclasses import dataclass

from backtest.orders import Order, Side
from orderbook.book import OrderBookSnapshot

QUEUE_BACK = "back"
QUEUE_FRONT = "front"


@dataclass(frozen=True)
class FillModel:
    """Configuration of the passive fill rules.

    Attributes:
        queue_position: ``"back"`` (conservative, default) or ``"front"``
            (optimistic, for comparison only).
        fill_on_cross: Apply rule 4 above. Keep True unless you are
            deliberately testing the effect of ignoring adverse moves.
    """

    queue_position: str = QUEUE_BACK
    fill_on_cross: bool = True

    def __post_init__(self) -> None:
        if self.queue_position not in (QUEUE_BACK, QUEUE_FRONT):
            raise ValueError("queue_position must be 'back' or 'front'")

    def initial_queue_ahead(self, order: Order, book: OrderBookSnapshot | None) -> float:
        """Contracts ahead of a newly active order at its price (rule 1)."""
        if self.queue_position == QUEUE_FRONT or book is None:
            return 0.0
        side = "bid" if order.side is Side.BUY else "ask"
        return book.size_at(side, order.price)

    def update_on_book(self, order: Order, book: OrderBookSnapshot) -> float:
        """Update an active order's queue for a new snapshot and return any fill size.

        Applies rule 2 (cap queue at displayed size) and rule 4 (fill if the
        book has moved through our price). Mutates ``order.queue_ahead`` only;
        the engine applies the fill.

        Returns:
            Contracts filled (0 if none).
        """
        side = "bid" if order.side is Side.BUY else "ask"
        order.queue_ahead = min(order.queue_ahead, book.size_at(side, order.price))

        if not self.fill_on_cross:
            return 0.0
        if order.side is Side.BUY:
            crossing = sum(size for price, size in book.asks if price <= order.price)
        else:
            crossing = sum(size for price, size in book.bids if price >= order.price)
        return min(order.remaining, crossing)

    def match_trade(self, orders: list[Order], trade_price: int, trade_size: float,
                    aggressor: Side) -> list[tuple[Order, float]]:
        """Allocate one public trade to our resting orders (rules 2, 3 and 5).

        Args:
            orders: Our active orders in the trade's market.
            trade_price: Trade price in cents.
            trade_size: Trade size in contracts.
            aggressor: Side of the trader who crossed the spread. A "sell"
                aggressor hits bids, so it can only fill our BUY orders.

        Returns:
            List of (order, fill_size) pairs, best price first. Mutates
            ``queue_ahead`` on orders at the trade price.
        """
        passive_side = Side.BUY if aggressor is Side.SELL else Side.SELL
        if passive_side is Side.BUY:
            candidates = [o for o in orders if o.side is Side.BUY and o.price >= trade_price]
            candidates.sort(key=lambda o: (-o.price, o.order_id))
        else:
            candidates = [o for o in orders if o.side is Side.SELL and o.price <= trade_price]
            candidates.sort(key=lambda o: (o.price, o.order_id))

        volume_left = trade_size
        allocations: list[tuple[Order, float]] = []
        for order in candidates:
            if volume_left <= 0:
                break
            if order.price == trade_price and self.queue_position == QUEUE_BACK:
                used_by_queue = min(volume_left, order.queue_ahead)
                order.queue_ahead -= used_by_queue
                volume_left -= used_by_queue
            fill_size = min(volume_left, order.remaining)
            if fill_size > 0:
                allocations.append((order, fill_size))
                volume_left -= fill_size
        return allocations
