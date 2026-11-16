"""Cash, positions and profit and loss.

Accounting rules:

* Buying ``q`` contracts at ``p`` cents costs ``q * p / 100`` dollars of cash.
  Selling receives the same amount. Fees are subtracted from cash separately
  and also tracked in ``fees_paid``.
* Positions are signed contract counts in each market (negative = short YES).
* **Mark-to-market equity** = cash + sum(position x mark price). The mark is
  the mid price, which is the market's best guess of value but is not a
  price you can actually trade at.
* **Liquidation equity** values longs at the best bid and shorts at the best
  ask, i.e. what you would get by crossing the spread right now (ignoring
  depth and the extra fee).
* **Settlement equity** uses the final payout (100 or 0 cents) of each market.

Everything starts at zero, so equity *is* profit and loss.
"""

from __future__ import annotations

from typing import Mapping

from backtest.orders import Fill
from orderbook.book import PAYOUT_CENTS, OrderBookSnapshot


class Portfolio:
    """Tracks cash, positions and fees for one backtest."""

    def __init__(self, markets: list[str]) -> None:
        """Start flat with zero cash in every market."""
        self.cash: float = 0.0
        self.fees_paid: float = 0.0
        self.positions: dict[str, float] = {market: 0.0 for market in markets}

    def apply_fill(self, fill: Fill) -> None:
        """Update cash, position and fees for one fill."""
        notional = fill.size * fill.price / PAYOUT_CENTS
        self.positions[fill.market_id] = self.positions.get(fill.market_id, 0.0) + fill.side.sign * fill.size
        self.cash -= fill.side.sign * notional
        self.cash -= fill.fee
        self.fees_paid += fill.fee

    def mark_to_market(self, marks_cents: Mapping[str, float]) -> float:
        """Net equity in dollars using the given mark price for each market."""
        value = self.cash
        for market, position in self.positions.items():
            if position != 0:
                value += position * marks_cents[market] / PAYOUT_CENTS
        return value

    def liquidation_value(self, books: Mapping[str, OrderBookSnapshot]) -> float:
        """Net equity in dollars if every position were closed at the touch."""
        value = self.cash
        for market, position in self.positions.items():
            if position == 0:
                continue
            book = books[market]
            exit_price = book.best_bid if position > 0 else book.best_ask
            if exit_price is None:  # one-sided book: fall back to the worst possible price
                exit_price = 0 if position > 0 else PAYOUT_CENTS
            value += position * exit_price / PAYOUT_CENTS
        return value

    def settlement_value(self, settlement_cents: Mapping[str, int]) -> float:
        """Net equity in dollars once every market has paid out."""
        value = self.cash
        for market, position in self.positions.items():
            value += position * settlement_cents[market] / PAYOUT_CENTS
        return value
