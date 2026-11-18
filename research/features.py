"""Turn recorded order books and trades into a feature table for predicting short-term mid moves.

One row per order book snapshot (per market). Every feature uses only
information available at the snapshot's timestamp ``t``:

* the snapshot itself (taken at ``t``),
* trades with timestamp ``<= t``,
* earlier snapshots of the same market,
* the market's *scheduled* expiry, as published before the event
  (``expected_expiration_time``). Kalshi moves ``close_time`` to the actual
  close once a market ends early, so using it would leak when the event ended.

The targets are the only columns that look ahead: ``target_<h>s`` is the mid
at ``t + h`` (last snapshot at or before that time) minus the mid at ``t``.

Columns (prices in cents):

==================  ========================================================
mid, spread         best bid/ask midpoint and width
imb_1, imb_3        (bid size - ask size) / (bid size + ask size), over the
                    best 1 and best 3 levels; in [-1, 1]
micro_dev           microprice minus mid
flow_<N>s           buy minus sell aggressor volume over the last N seconds
                    (contracts)
flow_rel_<N>s       flow_<N>s divided by the size in the best 3 levels of
                    both sides, so it is comparable across markets
ret_<N>s            mid now minus mid N seconds ago (recent momentum)
log_hours_to_expiry log(1 + hours until scheduled expiry); NaN if unknown
target_<h>s         mid(t + h) - mid(t)  -- the label, not a feature
==================  ========================================================
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from data.schema import MarketData

FLOW_WINDOWS_S = (10.0, 60.0)
RETURN_WINDOWS_S = (10.0, 60.0)
HORIZONS_S = (10.0, 60.0)


def snapshot_table(books: pd.DataFrame) -> pd.DataFrame:
    """Best prices, sizes and 3-level depth for every (timestamp, market) snapshot, vectorised.

    Snapshots missing either side of the book are dropped (no mid).
    """
    top = books[books["level"] == 0].pivot_table(index=["timestamp", "market_id"], columns="side",
                                                  values=["price", "size"], aggfunc="first")
    top.columns = [f"{side}_{field}" for field, side in top.columns]
    depth = (books[books["level"] < 3].groupby(["timestamp", "market_id", "side"])["size"].sum()
             .unstack("side").rename(columns={"bid": "bid_depth_3", "ask": "ask_depth_3"}))
    table = top.join(depth).reset_index().dropna(subset=["bid_price", "ask_price"])
    table = table[table["ask_price"] > table["bid_price"]]
    return table.sort_values(["market_id", "timestamp"]).reset_index(drop=True)


def _value_at_or_before(times: np.ndarray, values: np.ndarray, when: np.ndarray) -> np.ndarray:
    """values[i] for the last times[i] <= when (NaN if there is none). ``times`` must be sorted."""
    index = np.searchsorted(times, when, side="right") - 1
    out = np.full(len(when), np.nan)
    ok = index >= 0
    out[ok] = values[index[ok]]
    return out


def build_features(data: MarketData, expiry: dict[str, float] | None = None,
                   source: str = "") -> pd.DataFrame:
    """Feature table for every snapshot of every market in ``data``.

    Args:
        data: A recording in the standard format.
        expiry: {market_id: scheduled expiry as a Unix time}, for the time to
            resolution. Markets missing from it get NaN.
        source: Label for the recording (kept in a ``source`` column).
    """
    snaps = snapshot_table(data.books)
    trades = data.trades.sort_values("timestamp")
    expiry = expiry or {}
    frames = []
    for market, frame in snaps.groupby("market_id", sort=False):
        frame = frame.sort_values("timestamp").reset_index(drop=True)
        t = frame["timestamp"].to_numpy(dtype=float)
        bid, ask = frame["bid_price"].to_numpy(dtype=float), frame["ask_price"].to_numpy(dtype=float)
        bid_size, ask_size = frame["bid_size"].to_numpy(dtype=float), frame["ask_size"].to_numpy(dtype=float)
        bid3 = frame["bid_depth_3"].fillna(0).to_numpy(dtype=float)
        ask3 = frame["ask_depth_3"].fillna(0).to_numpy(dtype=float)
        mid = (bid + ask) / 2.0

        out = pd.DataFrame({"timestamp": t, "market_id": market, "source": source, "mid": mid,
                            "spread": ask - bid})
        out["imb_1"] = (bid_size - ask_size) / (bid_size + ask_size)
        out["imb_3"] = (bid3 - ask3) / np.where(bid3 + ask3 > 0, bid3 + ask3, np.nan)
        micro = (bid * ask_size + ask * bid_size) / (bid_size + ask_size)
        out["micro_dev"] = micro - mid

        # Trade flow: cumulative signed volume, read at t and at t - N (trades at exactly t count as known).
        market_trades = trades[trades["market_id"] == market]
        trade_t = market_trades["timestamp"].to_numpy(dtype=float)
        signed = np.where(market_trades["aggressor"].to_numpy() == "buy", 1.0, -1.0) * \
            market_trades["size"].to_numpy(dtype=float)
        cumulative = np.concatenate([[0.0], np.cumsum(signed)])
        def flow_until(when: np.ndarray) -> np.ndarray:
            return cumulative[np.searchsorted(trade_t, when, side="right")]
        depth = bid3 + ask3
        for window in FLOW_WINDOWS_S:
            flow = flow_until(t) - flow_until(t - window)
            out[f"flow_{window:g}s"] = flow
            out[f"flow_rel_{window:g}s"] = flow / np.where(depth > 0, depth, np.nan)

        for window in RETURN_WINDOWS_S:
            out[f"ret_{window:g}s"] = mid - _value_at_or_before(t, mid, t - window)

        if market in expiry:
            hours = np.maximum((expiry[market] - t) / 3600.0, 0.0)
            out["log_hours_to_expiry"] = np.log1p(hours)
        else:
            out["log_hours_to_expiry"] = np.nan

        # Labels: mid at t + h, only where the recording continues past t + h.
        for horizon in HORIZONS_S:
            future = _value_at_or_before(t, mid, t + horizon)
            out[f"target_{horizon:g}s"] = np.where(t + horizon <= t[-1], future - mid, np.nan)
        frames.append(out)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).sort_values(["timestamp", "market_id"]).reset_index(drop=True)


FEATURES = ["mid", "spread", "imb_1", "imb_3", "micro_dev",
            *[f"flow_{w:g}s" for w in FLOW_WINDOWS_S], *[f"flow_rel_{w:g}s" for w in FLOW_WINDOWS_S],
            *[f"ret_{w:g}s" for w in RETURN_WINDOWS_S], "log_hours_to_expiry"]


def time_split(frame: pd.DataFrame, test_share: float = 0.3, embargo_s: float = 60.0
               ) -> tuple[pd.DataFrame, pd.DataFrame, str]:
    """Split by time: earlier rows train, later rows test, never at random.

    With data on two or more UTC days, the last ``test_share`` of the days
    (at least one) is the test set. With a single day, the cut is at the
    time below which ``1 - test_share`` of the rows fall. Training rows within
    ``embargo_s`` of the first test row are dropped, because their targets
    look ahead into the test period.

    Returns:
        (train, test, description of the cut).
    """
    days = pd.to_datetime(frame["timestamp"], unit="s", utc=True).dt.date
    unique_days = sorted(days.unique())
    if len(unique_days) >= 2:
        n_test = max(1, int(round(test_share * len(unique_days))))
        first_test_day = unique_days[-n_test]
        cut = pd.Timestamp(first_test_day, tz="UTC").timestamp()
        how = f"by day: train {unique_days[0]}..{unique_days[-n_test - 1]}, test {first_test_day}..{unique_days[-1]}"
    else:
        cut = float(frame["timestamp"].quantile(1.0 - test_share))
        how = f"single day: cut at {pd.Timestamp(cut, unit='s', tz='UTC'):%Y-%m-%d %H:%M} UTC"
    train = frame[frame["timestamp"] < cut - embargo_s]
    test = frame[frame["timestamp"] >= cut]
    return train, test, how + f", {embargo_s:g} s embargo"
