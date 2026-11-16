"""Turn market data into a single time-ordered stream of events.

The engine replays this stream one event at a time, which is what makes the
backtest event-driven: at any moment the strategy can only know what has
already been replayed.

Ordering rule for equal timestamps: trades come before book snapshots. A
snapshot taken at time ``t`` already reflects trades up to ``t``, so playing
the trades first is the consistent choice.
"""

from __future__ import annotations

from dataclasses import dataclass

from backtest.orders import Side
from data.schema import MarketData, snapshots_from_books
from orderbook.book import OrderBookSnapshot


@dataclass(frozen=True)
class TradeEvent:
    """A public trade printed on the exchange."""

    timestamp: float
    market_id: str
    price: int
    size: float
    aggressor: Side


@dataclass(frozen=True)
class BookEvent:
    """A new order book snapshot for one market."""

    timestamp: float
    market_id: str
    book: OrderBookSnapshot


Event = TradeEvent | BookEvent


def build_event_stream(data: MarketData) -> list[Event]:
    """Merge snapshots and trades into one list sorted by time.

    Args:
        data: Market data in the standard format.

    Returns:
        Events sorted by (timestamp, trades before books). The sort is
        stable, so events with equal keys keep their input order.
    """
    events: list[tuple[float, int, Event]] = []
    for row in data.trades.itertuples(index=False):
        event = TradeEvent(float(row.timestamp), str(row.market_id), int(row.price),
                           float(row.size), Side(row.aggressor))
        events.append((event.timestamp, 0, event))
    for book in snapshots_from_books(data.books):
        events.append((book.timestamp, 1, BookEvent(book.timestamp, book.market_id, book)))
    events.sort(key=lambda item: (item[0], item[1]))
    return [event for _, _, event in events]
