"""The standard data format shared by the simulator, the Kalshi recorder and
the backtester.

Whatever the source, market data ends up as a :class:`MarketData` object with
two tables in "long" format:

``books``: one row per price level per snapshot.

    ========== ===============================================
    timestamp  float seconds
    market_id  str
    side       "bid" or "ask"
    level      int, 0 = best price on that side
    price      int cents (1 to 99)
    size       float contracts
    ========== ===============================================

``trades``: one row per public trade.

    ========== ===============================================
    timestamp  float seconds
    market_id  str
    price      int cents
    size       float contracts
    aggressor  "buy" (someone lifted the ask) or "sell" (someone hit the bid)
    ========== ===============================================

Because the backtester only ever sees this format, swapping simulated data
for recorded data is a one-line change in ``scripts/run_all.py``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd

from orderbook.book import MAX_PRICE, MIN_PRICE, OrderBookSnapshot

BOOK_COLUMNS = ["timestamp", "market_id", "side", "level", "price", "size"]
TRADE_COLUMNS = ["timestamp", "market_id", "price", "size", "aggressor"]


@dataclass
class MarketData:
    """Everything the backtester needs about one event (a group of markets).

    Attributes:
        books: Order book levels in the long format described above.
        trades: Public trades in the format described above.
        markets: Market ids that belong to the event, in a fixed order.
        mutually_exclusive: True if at most one market can resolve YES.
        exhaustive: True if exactly one market must resolve YES. Needed for
            the "buy every outcome" arbitrage to be risk free.
        settlement: Optional final payout per market in cents (100 or 0).
        truth: Simulator only. The true probability path, used for charts
            and analysis. **Never pass this to a strategy**: it is future
            information as far as the market is concerned.
        description: Free text describing where the data came from.
    """

    books: pd.DataFrame
    trades: pd.DataFrame
    markets: list[str]
    mutually_exclusive: bool = True
    exhaustive: bool = True
    settlement: dict[str, int] | None = None
    truth: pd.DataFrame | None = None
    description: str = ""
    extra: dict = field(default_factory=dict)


def validate_market_data(data: MarketData) -> None:
    """Raise ValueError if the tables do not match the standard schema.

    Checks column names, price range, positive sizes and valid sides. It does
    not fix anything: dirty data should be cleaned explicitly before this.
    """
    missing_books = set(BOOK_COLUMNS) - set(data.books.columns)
    if missing_books:
        raise ValueError(f"books table is missing columns: {sorted(missing_books)}")
    missing_trades = set(TRADE_COLUMNS) - set(data.trades.columns)
    if missing_trades:
        raise ValueError(f"trades table is missing columns: {sorted(missing_trades)}")

    for name, table in (("books", data.books), ("trades", data.trades)):
        if table.empty:
            continue
        prices = table["price"]
        if ((prices < MIN_PRICE) | (prices > MAX_PRICE)).any():
            raise ValueError(f"{name} has prices outside {MIN_PRICE}..{MAX_PRICE} cents")
        if (table["size"] <= 0).any():
            raise ValueError(f"{name} has non-positive sizes")
        unknown = set(table["market_id"].unique()) - set(data.markets)
        if unknown:
            raise ValueError(f"{name} has market ids not in data.markets: {sorted(unknown)}")

    if not data.books.empty and not set(data.books["side"].unique()) <= {"bid", "ask"}:
        raise ValueError("books.side must be 'bid' or 'ask'")
    if not data.trades.empty and not set(data.trades["aggressor"].unique()) <= {"buy", "sell"}:
        raise ValueError("trades.aggressor must be 'buy' or 'sell'")


def clean_books(books: pd.DataFrame) -> pd.DataFrame:
    """Basic cleaning for recorded book data.

    Drops rows with missing values, non-positive sizes or out-of-range
    prices, casts types and sorts. Returns a new DataFrame.
    """
    cleaned = books.dropna(subset=BOOK_COLUMNS).copy()
    cleaned["price"] = cleaned["price"].round().astype(int)
    cleaned["size"] = cleaned["size"].astype(float)
    cleaned["timestamp"] = cleaned["timestamp"].astype(float)
    cleaned = cleaned[(cleaned["size"] > 0)]
    cleaned = cleaned[(cleaned["price"] >= MIN_PRICE) & (cleaned["price"] <= MAX_PRICE)]
    return cleaned.sort_values(["timestamp", "market_id", "side", "level"]).reset_index(drop=True)


def clean_trades(trades: pd.DataFrame) -> pd.DataFrame:
    """Basic cleaning for recorded trades (same rules as :func:`clean_books`)."""
    cleaned = trades.dropna(subset=TRADE_COLUMNS).copy()
    cleaned["price"] = cleaned["price"].round().astype(int)
    cleaned["size"] = cleaned["size"].astype(float)
    cleaned["timestamp"] = cleaned["timestamp"].astype(float)
    cleaned = cleaned[(cleaned["size"] > 0)]
    cleaned = cleaned[(cleaned["price"] >= MIN_PRICE) & (cleaned["price"] <= MAX_PRICE)]
    return cleaned.sort_values(["timestamp", "market_id"]).reset_index(drop=True)


def snapshots_from_books(books: pd.DataFrame) -> list[OrderBookSnapshot]:
    """Convert the long ``books`` table into a time-sorted list of snapshots.

    A snapshot is identified by (timestamp, market_id). A market whose book
    is completely empty at some time will simply have no rows; recorders
    should write at least one row per snapshot if that matters.

    Returns:
        Snapshots sorted by timestamp, then market_id.
    """
    if books.empty:
        return []
    ordered = books.sort_values(["timestamp", "market_id", "side", "level"], kind="mergesort")
    timestamps = ordered["timestamp"].to_numpy()
    market_ids = ordered["market_id"].to_numpy()
    sides = ordered["side"].to_numpy()
    prices = ordered["price"].to_numpy()
    sizes = ordered["size"].to_numpy()

    # Positions where (timestamp, market_id) changes mark the start of a snapshot.
    change = np.ones(len(ordered), dtype=bool)
    change[1:] = (timestamps[1:] != timestamps[:-1]) | (market_ids[1:] != market_ids[:-1])
    starts = np.flatnonzero(change)
    ends = np.append(starts[1:], len(ordered))

    snapshots: list[OrderBookSnapshot] = []
    for start, end in zip(starts, ends):
        bids = []
        asks = []
        for i in range(start, end):
            level = (int(prices[i]), float(sizes[i]))
            if sides[i] == "bid":
                bids.append(level)
            else:
                asks.append(level)
        snapshots.append(
            OrderBookSnapshot.from_levels(float(timestamps[start]), str(market_ids[start]), bids, asks)
        )
    return snapshots


def books_to_rows(book: OrderBookSnapshot) -> list[dict]:
    """Convert one snapshot to rows of the long ``books`` table."""
    rows = []
    for level, (price, size) in enumerate(book.bids):
        rows.append({"timestamp": book.timestamp, "market_id": book.market_id,
                     "side": "bid", "level": level, "price": price, "size": size})
    for level, (price, size) in enumerate(book.asks):
        rows.append({"timestamp": book.timestamp, "market_id": book.market_id,
                     "side": "ask", "level": level, "price": price, "size": size})
    return rows


def split_into_sessions(data: MarketData, session_s: float, min_coverage: float = 0.5) -> list[MarketData]:
    """Cut a long recording into consecutive, non-overlapping windows ("sessions").

    Each session is backtested separately, starting flat, so a long real
    recording produces several samples that can be compared with simulated
    sessions of the same length.

    Args:
        data: The full recording.
        session_s: Session length in seconds.
        min_coverage: Drop windows whose book data spans less than this
            fraction of ``session_s`` (e.g. the collector was off).

    Returns:
        List of sessions. Settlement is kept only when the whole recording
        fits in one session, because a market only settles once, at the end.
        If the recording is shorter than ``session_s`` it is returned whole.
    """
    if data.books.empty:
        return []
    start = float(data.books["timestamp"].min())
    end = float(data.books["timestamp"].max())
    if end - start <= session_s:
        return [data]
    sessions = []
    for index in range(math.ceil((end - start) / session_s)):
        low, high = start + index * session_s, start + (index + 1) * session_s
        books = data.books[(data.books["timestamp"] >= low) & (data.books["timestamp"] < high)]
        if books.empty or (books["timestamp"].max() - books["timestamp"].min()) < min_coverage * session_s:
            continue
        trades = data.trades[(data.trades["timestamp"] >= low) & (data.trades["timestamp"] < high)]
        sessions.append(replace(
            data, books=books.reset_index(drop=True), trades=trades.reset_index(drop=True),
            settlement=None, truth=None,
            description=f"{data.description} (session {index + 1}, {session_s / 3600:g} h)"))
    return sessions
