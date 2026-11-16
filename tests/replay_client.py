"""A fake Kalshi client that replays simulated data, so the collector and the
paper trader can be tested without network access."""

from __future__ import annotations

from typing import Any

import numpy as np

from data.schema import MarketData, snapshots_from_books


class FakeClock:
    """A clock the test moves forward by hand."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class ReplayClient:
    """Answers get_orderbook / get_trades from a MarketData object at the clock's time."""

    def __init__(self, data: MarketData, clock: FakeClock) -> None:
        self.clock = clock
        self.snapshots: dict[str, list] = {}
        for snap in snapshots_from_books(data.books):
            self.snapshots.setdefault(snap.market_id, []).append(snap)
        self.times = {m: np.array([s.timestamp for s in snaps]) for m, snaps in self.snapshots.items()}
        self.trades = data.trades.reset_index(drop=True)
        self.trades["trade_id"] = [f"t{i}" for i in range(len(self.trades))]

    def get_orderbook(self, ticker: str, depth: int = 10):
        index = int(np.searchsorted(self.times[ticker], self.clock.now, side="right")) - 1
        snap = self.snapshots[ticker][max(index, 0)]
        return list(snap.bids[:depth]), list(snap.asks[:depth])

    def get_trades(self, ticker: str, min_ts: int, max_ts: int) -> list[dict[str, Any]]:
        t = self.trades
        chosen = t[(t["market_id"] == ticker) & (t["timestamp"] >= min_ts) & (t["timestamp"] <= max_ts)
                   & (t["timestamp"] <= self.clock.now)]
        return chosen.to_dict("records")
