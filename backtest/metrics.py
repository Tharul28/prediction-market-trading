"""Performance, risk and execution-quality metrics for a backtest result.

All money amounts are in dollars and all prices in cents.

Definitions:

* **Net P&L**: final mark-to-market equity after fees (marked at mid).
* **Gross P&L**: net P&L plus fees paid.
* **Liquidation P&L**: net P&L if the final position is closed by crossing
  the spread (more honest for a strategy that ends with inventory).
* **Interval P&L**: change in net equity over fixed intervals (default 60 s).
  Volatility and the Sharpe-style ratio are computed from these.
* **Sharpe-style ratio**: mean interval P&L divided by its standard
  deviation. It is *not* annualised: there is no natural capital base or
  trading calendar for a single prediction-market session, and
  annualising a two-hour simulation would suggest a precision it does not
  have. Across many simulated sessions, the run script also reports mean
  session P&L divided by the standard deviation of session P&L.
* **Max drawdown**: largest fall in net equity from a previous peak.
* **Fill rate**: share of passive orders that became active and received any
  fill, and filled contracts as a share of contracts quoted.
* **Markout**: for each fill, how far the mid price moved in our favour
  ``h`` seconds later, in cents per contract. A buy at 50 followed by a mid
  of 52 is a +2 markout. Negative markouts at longer horizons are the
  signature of adverse selection: you tend to trade just before the price
  moves against you. This is post-trade analysis, so using future mids
  here is fine; it never feeds back into the strategy.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from backtest.engine import BacktestResult
from strategies.cross_outcome_arb import unhedged_contracts

DEFAULT_MARKOUT_HORIZONS_S = (0.0, 5.0, 15.0, 30.0, 60.0, 120.0, 300.0)


def interval_pnl(equity: pd.DataFrame, interval_s: float = 60.0, column: str = "net_equity") -> pd.Series:
    """Change in equity over consecutive fixed-length intervals.

    Args:
        equity: The ``equity`` table from a :class:`BacktestResult`.
        interval_s: Interval length in seconds.
        column: Equity column to use.

    Returns:
        Series of P&L per interval (the first interval is measured from 0).
    """
    if equity.empty:
        return pd.Series(dtype=float)
    start = equity["timestamp"].iloc[0]
    bucket = np.floor((equity["timestamp"] - start) / interval_s).astype(int)
    end_of_interval = equity.groupby(bucket)[column].last()
    return end_of_interval.diff().fillna(end_of_interval.iloc[0])


def max_drawdown(equity_curve: pd.Series) -> float:
    """Largest peak-to-trough fall of an equity curve that starts at 0 (positive number)."""
    if equity_curve.empty:
        return 0.0
    values = np.concatenate([[0.0], equity_curve.to_numpy(dtype=float)])
    running_peak = np.maximum.accumulate(values)
    return float(np.max(running_peak - values))


def mid_lookup(book_metrics: pd.DataFrame) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """Per-market arrays of (timestamps, mids) for fast markout lookups.

    Args:
        book_metrics: Output of ``orderbook.metrics.metrics_frame``.
    """
    lookup = {}
    for market, frame in book_metrics.dropna(subset=["mid"]).groupby("market_id"):
        frame = frame.sort_values("timestamp")
        lookup[str(market)] = (frame["timestamp"].to_numpy(), frame["mid"].to_numpy())
    return lookup


def markouts(fills: pd.DataFrame, mids: dict[str, tuple[np.ndarray, np.ndarray]],
             horizons_s: tuple[float, ...] = DEFAULT_MARKOUT_HORIZONS_S) -> pd.DataFrame:
    """Markout of every fill at several horizons, in cents per contract.

    Horizon 0 uses the last mid at or before the fill (how much better than
    mid we traded). Positive horizons use the first mid at or after
    ``fill time + h``. Fills too close to the end of the data get NaN.

    Returns:
        DataFrame with one row per fill and a column ``markout_<h>s`` per horizon.
    """
    if fills.empty:
        return pd.DataFrame(columns=["size"] + [f"markout_{h:g}s" for h in horizons_s])
    result = fills[["timestamp", "market_id", "side", "price", "size", "liquidity"]].copy()
    sign = np.where(result["side"] == "buy", 1.0, -1.0)
    for horizon in horizons_s:
        values = np.full(len(result), np.nan)
        for market, (times, mid_values) in mids.items():
            mask = (result["market_id"] == market).to_numpy()
            if not mask.any():
                continue
            fill_times = result.loc[mask, "timestamp"].to_numpy()
            if horizon == 0:
                index = np.searchsorted(times, fill_times, side="right") - 1
                valid = index >= 0
            else:
                index = np.searchsorted(times, fill_times + horizon, side="left")
                valid = index < len(times)
            market_values = np.full(mask.sum(), np.nan)
            market_values[valid] = mid_values[index[valid]]
            values[mask] = market_values
        result[f"markout_{horizon:g}s"] = sign * (values - result["price"].to_numpy())
    return result


def average_markouts(markout_table: pd.DataFrame) -> pd.Series:
    """Size-weighted average markout per horizon (cents per contract)."""
    columns = [c for c in markout_table.columns if c.startswith("markout_")]
    averages = {}
    for column in columns:
        valid = markout_table[column].notna()
        weights = markout_table.loc[valid, "size"]
        averages[column] = (float(np.average(markout_table.loc[valid, column], weights=weights))
                            if weights.sum() > 0 else np.nan)
    return pd.Series(averages)


def basket_summary(result: BacktestResult) -> dict[str, float]:
    """For arbitrage runs: how many baskets filled completely, partly or not at all.

    A basket is the group of orders sharing a ``basket-N`` tag.
    """
    orders = result.orders[result.orders["tag"].str.startswith("basket-")]
    if orders.empty:
        return {"baskets_sent": 0, "baskets_complete": 0, "baskets_partial": 0, "baskets_missed": 0,
                "unhedged_contracts": 0.0}
    complete = partial = missed = 0
    for _, legs in orders.groupby("tag"):
        filled_fraction = legs["filled"] / legs["size"]
        if (filled_fraction >= 1.0 - 1e-9).all():
            complete += 1
        elif (legs["filled"] <= 0).all():
            missed += 1
        else:
            partial += 1
    positions = [float(result.equity[f"position_{m}"].iloc[-1]) for m in result.markets]
    unhedged = unhedged_contracts(positions)
    return {"baskets_sent": complete + partial + missed, "baskets_complete": complete,
            "baskets_partial": partial, "baskets_missed": missed, "unhedged_contracts": unhedged}


def performance_summary(result: BacktestResult, interval_s: float = 60.0) -> dict[str, float]:
    """Headline numbers for one backtest.

    Returns:
        Dictionary of metric name to value. See the module docstring for
        definitions.
    """
    equity = result.equity
    fills = result.fills
    orders = result.orders

    pnl_per_interval = interval_pnl(equity, interval_s)
    volatility = float(pnl_per_interval.std(ddof=1)) if len(pnl_per_interval) > 1 else np.nan
    sharpe = float(pnl_per_interval.mean() / volatility) if volatility and volatility > 0 else np.nan

    # .loc with an explicit boolean mask also works when there are no orders at all.
    is_taker = orders["is_taker"].astype(bool)
    passive = orders.loc[~is_taker]
    passive_active = passive.loc[passive["activated_at"].notna()]
    taker = orders.loc[is_taker]
    position_columns = [f"position_{m}" for m in result.markets]
    abs_positions = equity[position_columns].abs() if not equity.empty else pd.DataFrame()

    return {
        "net_pnl": result.net_pnl,
        "gross_pnl": result.gross_pnl,
        "fees_paid": result.fees_paid,
        "liquidation_pnl": result.liquidation_pnl,
        "settlement_pnl": np.nan if result.settlement_pnl is None else result.settlement_pnl,
        "n_fills": float(len(fills)),
        "contracts_traded": float(fills["size"].sum()) if not fills.empty else 0.0,
        "maker_fills": float((fills["liquidity"] == "maker").sum()) if not fills.empty else 0.0,
        "taker_fills": float((fills["liquidity"] == "taker").sum()) if not fills.empty else 0.0,
        "orders_submitted": float(len(orders)),
        "orders_rejected": float((orders["status"] == "rejected").sum()),
        "passive_fill_rate_orders": (float((passive_active["filled"] > 0).mean())
                                     if len(passive_active) else np.nan),
        "passive_fill_rate_contracts": (float(passive_active["filled"].sum() / passive_active["size"].sum())
                                        if len(passive_active) else np.nan),
        "taker_fill_rate_contracts": (float(taker["filled"].sum() / taker["size"].sum())
                                      if len(taker) else np.nan),
        "max_abs_inventory": float(abs_positions.to_numpy().max()) if not abs_positions.empty else 0.0,
        "mean_abs_inventory": float(abs_positions.sum(axis=1).mean()) if not abs_positions.empty else 0.0,
        "interval_pnl_vol": volatility,
        "sharpe_style": sharpe,
        "max_drawdown": max_drawdown(equity["net_equity"]) if not equity.empty else 0.0,
    }
