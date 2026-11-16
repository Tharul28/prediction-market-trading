"""Derived quantities computed from order book snapshots.

The functions here turn raw snapshots into the numbers a trader looks at:
spread, mid, microprice, implied probability, depth and imbalance, plus
helpers for multi-outcome markets (overround and basket prices).
"""

from __future__ import annotations

from typing import Iterable, Mapping, Sequence

import pandas as pd

from orderbook.book import PAYOUT_CENTS, OrderBookSnapshot


def snapshot_metrics(book: OrderBookSnapshot, depth_levels: int = 5) -> dict[str, float | None]:
    """Compute the standard metrics for one snapshot.

    Args:
        book: The snapshot.
        depth_levels: Number of levels used for the deeper depth and
            imbalance measures.

    Returns:
        Dictionary of metric name to value (None where undefined, e.g. the
        spread of a one-sided book).
    """
    return {
        "timestamp": book.timestamp,
        "market_id": book.market_id,
        "best_bid": book.best_bid,
        "best_ask": book.best_ask,
        "best_bid_size": book.best_bid_size,
        "best_ask_size": book.best_ask_size,
        "spread": book.spread,
        "mid": book.mid,
        "microprice": book.microprice,
        "implied_prob": book.implied_probability,
        "bid_depth": book.depth("bid", depth_levels),
        "ask_depth": book.depth("ask", depth_levels),
        "imbalance_top": book.imbalance(1),
        f"imbalance_{depth_levels}": book.imbalance(depth_levels),
    }


def metrics_frame(books: Iterable[OrderBookSnapshot], depth_levels: int = 5) -> pd.DataFrame:
    """Compute metrics for many snapshots and return them as a DataFrame.

    Args:
        books: Snapshots, in any order.
        depth_levels: Passed to :func:`snapshot_metrics`.

    Returns:
        One row per snapshot, sorted by market and timestamp.
    """
    rows = [snapshot_metrics(book, depth_levels) for book in books]
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    return frame.sort_values(["market_id", "timestamp"]).reset_index(drop=True)


def decimal_odds_to_probability(decimal_odds: float) -> float:
    """Convert decimal (European) odds, as used on Betfair, to a probability.

    Decimal odds of 4.0 return 4 units for a 1 unit stake (including the
    stake), so the break-even probability is 1 / 4.0 = 0.25.
    """
    if decimal_odds <= 1.0:
        raise ValueError("Decimal odds must be greater than 1.0")
    return 1.0 / decimal_odds


def probability_to_decimal_odds(probability: float) -> float:
    """Convert a probability in (0, 1) to decimal odds."""
    if not 0.0 < probability < 1.0:
        raise ValueError("Probability must be strictly between 0 and 1")
    return 1.0 / probability


def overround(probabilities: Sequence[float]) -> float:
    """Sum of implied probabilities minus 1 for a set of exhaustive outcomes.

    A positive overround means the prices collectively imply more than 100%
    (normal for prices you can buy at); a negative one means less than 100%.
    """
    return float(sum(probabilities)) - 1.0


def basket_prices(books: Mapping[str, OrderBookSnapshot]) -> dict[str, float | None]:
    """Cost of buying one YES of every outcome, and proceeds of selling one of each.

    For mutually exclusive and exhaustive outcomes, exactly one YES pays
    100 cents. So if ``sum_best_asks < 100`` you could buy every outcome and
    lock in a profit before fees, and if ``sum_best_bids > 100`` you could sell
    every outcome and lock in a profit before fees.

    Args:
        books: Latest snapshot of each outcome's market.

    Returns:
        Dict with ``sum_best_asks``, ``sum_best_bids`` (cents) and the two
        edges ``buy_edge = 100 - sum_asks`` and ``sell_edge = sum_bids - 100``.
        Values are None if any book is one-sided.
    """
    asks = [book.best_ask for book in books.values()]
    bids = [book.best_bid for book in books.values()]
    sum_asks = None if any(a is None for a in asks) else float(sum(asks))  # type: ignore[arg-type]
    sum_bids = None if any(b is None for b in bids) else float(sum(bids))  # type: ignore[arg-type]
    return {
        "sum_best_asks": sum_asks,
        "sum_best_bids": sum_bids,
        "buy_edge": None if sum_asks is None else PAYOUT_CENTS - sum_asks,
        "sell_edge": None if sum_bids is None else sum_bids - PAYOUT_CENTS,
    }

