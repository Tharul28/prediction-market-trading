"""Order book representation for a single binary contract.

Conventions used everywhere in this project:

* A contract pays $1 if the outcome happens and $0 otherwise.
* Prices are integer **cents** between 1 and 99. A price of 37 means $0.37,
  which is also read as an implied probability of 0.37.
* Sizes are numbers of contracts (floats, because Kalshi now reports
  fractional counts as fixed-point strings).
* ``bids`` are sorted best (highest price) first and ``asks`` are sorted best
  (lowest price) first.

Integer cents are used instead of floats so that price comparisons such as
"is my bid at the same level as the best bid?" are exact.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

PriceLevel = tuple[int, float]  # (price in cents, size in contracts)

MIN_PRICE = 1
MAX_PRICE = 99
PAYOUT_CENTS = 100


@dataclass(frozen=True)
class OrderBookSnapshot:
    """The visible order book of one market at one moment in time.

    Attributes:
        timestamp: Time of the snapshot in seconds (Unix time for real data,
            seconds since the start of the session for simulated data).
        market_id: Identifier of the market, for example a Kalshi ticker.
        bids: Buy orders as (price, size) pairs, best (highest) first.
        asks: Sell orders as (price, size) pairs, best (lowest) first.
    """

    timestamp: float
    market_id: str
    bids: tuple[PriceLevel, ...]
    asks: tuple[PriceLevel, ...]

    @classmethod
    def from_levels(
        cls,
        timestamp: float,
        market_id: str,
        bids: Iterable[PriceLevel],
        asks: Iterable[PriceLevel],
    ) -> "OrderBookSnapshot":
        """Build a snapshot from unsorted levels, merging duplicate prices and
        dropping empty levels.

        Args:
            timestamp: Snapshot time in seconds.
            market_id: Market identifier.
            bids: Iterable of (price, size) buy levels in any order.
            asks: Iterable of (price, size) sell levels in any order.

        Returns:
            A snapshot with correctly sorted levels.
        """
        bid_sizes: dict[int, float] = {}
        for price, size in bids:
            if size > 0:
                bid_sizes[int(price)] = bid_sizes.get(int(price), 0.0) + float(size)
        ask_sizes: dict[int, float] = {}
        for price, size in asks:
            if size > 0:
                ask_sizes[int(price)] = ask_sizes.get(int(price), 0.0) + float(size)
        sorted_bids = tuple(sorted(bid_sizes.items(), key=lambda level: -level[0]))
        sorted_asks = tuple(sorted(ask_sizes.items(), key=lambda level: level[0]))
        return cls(float(timestamp), market_id, sorted_bids, sorted_asks)

    # ----------------------------------------------------------------- top of book
    @property
    def best_bid(self) -> int | None:
        """Highest bid price in cents, or None if there are no bids."""
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> int | None:
        """Lowest ask price in cents, or None if there are no asks."""
        return self.asks[0][0] if self.asks else None

    @property
    def best_bid_size(self) -> float:
        """Size resting at the best bid (0 if there are no bids)."""
        return self.bids[0][1] if self.bids else 0.0

    @property
    def best_ask_size(self) -> float:
        """Size resting at the best ask (0 if there are no asks)."""
        return self.asks[0][1] if self.asks else 0.0

    @property
    def is_two_sided(self) -> bool:
        """True if the book has at least one bid and one ask."""
        return bool(self.bids) and bool(self.asks)

    @property
    def is_crossed(self) -> bool:
        """True if best bid >= best ask, which should not happen in clean data."""
        return self.is_two_sided and self.best_bid >= self.best_ask  # type: ignore[operator]

    @property
    def spread(self) -> int | None:
        """Best ask minus best bid in cents, or None for a one-sided book."""
        if not self.is_two_sided:
            return None
        return self.best_ask - self.best_bid  # type: ignore[operator]

    @property
    def mid(self) -> float | None:
        """Simple average of best bid and best ask in cents."""
        if not self.is_two_sided:
            return None
        return (self.best_bid + self.best_ask) / 2.0  # type: ignore[operator]

    @property
    def microprice(self) -> float | None:
        """Size-weighted mid price in cents.

        Weights each side's price by the size on the *opposite* side:
        ``(bid * ask_size + ask * bid_size) / (bid_size + ask_size)``.
        If much more size is bidding than offering, the microprice moves
        towards the ask, reflecting that the next trade is more likely to be
        a buy.
        """
        if not self.is_two_sided:
            return None
        bid_size, ask_size = self.best_bid_size, self.best_ask_size
        total = bid_size + ask_size
        if total <= 0:
            return self.mid
        return (self.best_bid * ask_size + self.best_ask * bid_size) / total  # type: ignore[operator]

    @property
    def implied_probability(self) -> float | None:
        """Mid price converted to a probability between 0 and 1.

        This ignores fees, the time value of money and any risk premium, so it
        is the market's rough probability estimate, not a true probability.
        """
        mid = self.mid
        return None if mid is None else mid / PAYOUT_CENTS

    # ------------------------------------------------------------------- depth
    def size_at(self, side: str, price: int) -> float:
        """Visible size at an exact price.

        Args:
            side: "bid" or "ask".
            price: Price in cents.

        Returns:
            Size resting at that price on that side (0 if none).
        """
        levels = self.bids if side == "bid" else self.asks
        for level_price, size in levels:
            if level_price == price:
                return size
        return 0.0

    def depth(self, side: str, n_levels: int = 5) -> float:
        """Total size in the best ``n_levels`` price levels of one side.

        Args:
            side: "bid" or "ask".
            n_levels: Number of levels to include.

        Returns:
            Sum of sizes in those levels.
        """
        levels = self.bids if side == "bid" else self.asks
        return float(sum(size for _, size in levels[:n_levels]))

    def imbalance(self, n_levels: int = 1) -> float | None:
        """Order book imbalance in [-1, 1] over the best ``n_levels`` levels.

        ``(bid_depth - ask_depth) / (bid_depth + ask_depth)``. Positive values
        mean more size wants to buy than sell.
        """
        bid_depth = self.depth("bid", n_levels)
        ask_depth = self.depth("ask", n_levels)
        total = bid_depth + ask_depth
        if total <= 0:
            return None
        return (bid_depth - ask_depth) / total
