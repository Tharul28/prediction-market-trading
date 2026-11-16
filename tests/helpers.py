"""Small builders for hand-made test data."""

from __future__ import annotations

import pandas as pd

from data.schema import BOOK_COLUMNS, TRADE_COLUMNS, MarketData, books_to_rows
from orderbook.book import OrderBookSnapshot


def make_data(snapshots: list[OrderBookSnapshot], trades: list[dict] | None = None,
              markets: list[str] | None = None, settlement: dict[str, int] | None = None) -> MarketData:
    """Wrap hand-built snapshots and trades in a MarketData object."""
    rows = [row for snap in snapshots for row in books_to_rows(snap)]
    books = pd.DataFrame(rows, columns=BOOK_COLUMNS)
    trade_frame = pd.DataFrame(trades or [], columns=TRADE_COLUMNS)
    markets = markets or sorted({snap.market_id for snap in snapshots})
    return MarketData(books=books, trades=trade_frame, markets=markets, settlement=settlement)


def book(t: float, bids: list[tuple[int, float]], asks: list[tuple[int, float]],
         market: str = "M") -> OrderBookSnapshot:
    """Shorthand for OrderBookSnapshot.from_levels."""
    return OrderBookSnapshot.from_levels(t, market, bids, asks)
