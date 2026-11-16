"""Reports for a paper trading run: daily summary and paper-versus-backtest comparison.

``python -m paper.trader summary --out <dir>`` calls :func:`write_reports`,
which writes into ``<dir>/report/``:

* ``daily_summary.csv`` / ``.md``: per calendar day, P&L, fees, fills,
  contracts, maximum inventory and intraday drawdown.
* ``paper_vs_backtest.md``: the same strategy and engine settings re-run as a
  backtest on the market data the paper trader recorded, for exactly the
  same period, next to the paper results.
* ``paper_vs_backtest_daily.csv`` and ``paper_vs_backtest.png``.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from backtest.engine import BacktestEngine, BacktestResult
from data.storage import load_market_data_dir
from paper.trader import PaperConfig


def _local_dates(timestamps: pd.Series, timezone: str) -> pd.Series:
    return pd.to_datetime(timestamps, unit="s", utc=True).dt.tz_convert(timezone).dt.date


def daily_summary(equity: pd.DataFrame, fills: pd.DataFrame, markets: list[str],
                  timezone: str = "Europe/London") -> pd.DataFrame:
    """Summarise an equity log and a fill log by calendar day.

    Args:
        equity: Rows with ``timestamp``, ``net_pnl``, ``fees_paid`` and
            ``position_<market>`` columns (paper ``equity.csv`` or a backtest
            equity table).
        fills: Rows with ``timestamp`` and ``size``.
        markets: Market ids, for the position columns.
        timezone: Time zone that defines a "day".

    Returns:
        One row per day: start/end P&L, day P&L, fees, fills, contracts,
        max absolute inventory, intraday max drawdown.
    """
    if equity.empty:
        return pd.DataFrame()
    equity = equity.sort_values("timestamp").copy()
    equity["date"] = _local_dates(equity["timestamp"], timezone)
    fills = fills.copy()
    fills["date"] = _local_dates(fills["timestamp"], timezone) if not fills.empty else []
    position_columns = [f"position_{m}" for m in markets if f"position_{m}" in equity.columns]

    rows = []
    previous_pnl = 0.0
    previous_fees = 0.0
    for date, day in equity.groupby("date"):
        pnl = day["net_pnl"].to_numpy()
        path = np.concatenate([[previous_pnl], pnl])
        drawdown = float(np.max(np.maximum.accumulate(path) - path))
        day_fills = fills[fills["date"] == date] if not fills.empty else fills
        rows.append({
            "date": date,
            "start_pnl": previous_pnl,
            "end_pnl": float(pnl[-1]),
            "day_pnl": float(pnl[-1] - previous_pnl),
            "day_fees": float(day["fees_paid"].iloc[-1] - previous_fees),
            "fills": int(len(day_fills)),
            "contracts": float(day_fills["size"].sum()) if len(day_fills) else 0.0,
            "max_abs_inventory": float(day[position_columns].abs().to_numpy().max()) if position_columns else 0.0,
            "max_drawdown": drawdown,
            "hours_covered": float((day["timestamp"].max() - day["timestamp"].min()) / 3600.0),
        })
        previous_pnl = float(pnl[-1])
        previous_fees = float(day["fees_paid"].iloc[-1])
    return pd.DataFrame(rows)


def backtest_same_period(out_dir: Path, config: PaperConfig, start: float, end: float) -> BacktestResult:
    """Re-run the paper strategy as a backtest on the market data it recorded."""
    data = load_market_data_dir(out_dir / "market_data")
    data.books = data.books[(data.books["timestamp"] >= start) & (data.books["timestamp"] <= end)].reset_index(drop=True)
    data.trades = data.trades[(data.trades["timestamp"] >= start) & (data.trades["timestamp"] <= end)].reset_index(drop=True)
    data.settlement = None
    return BacktestEngine(data, config.build_strategy(), config.backtest_config()).run()


def _markdown_table(frame: pd.DataFrame, float_format: str = "{:.2f}") -> str:
    headers = list(frame.columns)
    lines = ["| " + " | ".join(str(h) for h in headers) + " |", "|" + "---|" * len(headers)]
    for _, row in frame.iterrows():
        cells = [float_format.format(v) if isinstance(v, (float, np.floating)) else str(v) for v in row]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def write_reports(out_dir: Path, timezone: str = "Europe/London") -> dict[str, pd.DataFrame]:
    """Write the daily summary and the paper-versus-backtest comparison.

    Returns:
        The tables written, keyed by name.
    """
    config = PaperConfig.load(out_dir / "paper_config.json")
    equity = pd.read_csv(out_dir / "equity.csv", on_bad_lines="skip")
    fills_path = out_dir / "fills.csv"
    fills = pd.read_csv(fills_path, on_bad_lines="skip") if fills_path.exists() else pd.DataFrame(
        columns=["timestamp", "size", "fee", "liquidity", "reason"])
    report_dir = out_dir / "report"
    report_dir.mkdir(exist_ok=True)

    paper_daily = daily_summary(equity, fills, config.markets, timezone)
    paper_daily.to_csv(report_dir / "daily_summary.csv", index=False)
    (report_dir / "daily_summary.md").write_text(
        f"Paper trading daily summary ({config.strategy} on {', '.join(config.markets)}, "
        f"starting balance ${config.starting_balance:,.0f}, days in {timezone})\n\n" + _markdown_table(paper_daily))

    start, end = float(equity["timestamp"].min()), float(equity["timestamp"].max())
    backtest = backtest_same_period(out_dir, config, start, end)
    backtest_equity = backtest.equity.rename(columns={"net_equity": "net_pnl"})
    backtest_daily = daily_summary(backtest_equity, backtest.fills, config.markets, timezone)

    def totals(net: float, fees: float, fill_table: pd.DataFrame, positions: pd.DataFrame) -> dict[str, float]:
        total = {"Net P&L $": net, "Gross P&L $": net + fees, "Fees $": fees, "Fills": float(len(fill_table)),
                 "Contracts": float(fill_table["size"].sum()) if len(fill_table) else 0.0,
                 "Max abs inventory": float(positions.abs().to_numpy().max()) if not positions.empty else 0.0}
        for reason in ("queue", "trade_through", "book_cross", "taker"):
            share = (fill_table.loc[fill_table["reason"] == reason, "size"].sum() / fill_table["size"].sum()
                     if len(fill_table) else np.nan)
            total[f"Fill share: {reason}"] = share
        return total

    paper_positions = equity[[f"position_{m}" for m in config.markets]]
    backtest_positions = backtest.equity[[f"position_{m}" for m in config.markets]]
    comparison = pd.DataFrame({
        "Paper": totals(float(equity["net_pnl"].iloc[-1]), float(equity["fees_paid"].iloc[-1]), fills, paper_positions),
        "Backtest, same period": totals(backtest.net_pnl, backtest.fees_paid, backtest.fills, backtest_positions),
    })
    comparison["Difference"] = comparison["Paper"] - comparison["Backtest, same period"]
    comparison.index.name = "Metric"

    daily = paper_daily[["date", "day_pnl", "fills"]].merge(
        backtest_daily[["date", "day_pnl", "fills"]], on="date", how="outer", suffixes=(" paper", " backtest"))
    daily.to_csv(report_dir / "paper_vs_backtest_daily.csv", index=False)

    notes = (
        "Same strategy, same engine settings, same recorded market data, same period.\n\n"
        "Why they can differ:\n\n"
        "* Live, trades are only seen at the next poll, after book snapshots that came later in time. "
        "The backtest replays everything in exact time order.\n"
        "* A trade that arrives after our order went live but happened before it cannot fill us "
        "(the engine checks the order's activation time), so paper trading can miss fills.\n"
        "* After a restart, paper trading starts with no open orders; the backtest runs straight through.\n"
        "* Polls that failed live are missing from both, but any data saved after a crash only feeds the backtest.\n"
    )
    (report_dir / "paper_vs_backtest.md").write_text(
        f"# Paper trading versus backtest\n\nPeriod: {pd.Timestamp(start, unit='s')} to "
        f"{pd.Timestamp(end, unit='s')} UTC\n\n" + _markdown_table(comparison.reset_index()) + "\n"
        + "Daily P&L\n\n" + _markdown_table(daily) + "\n" + notes)

    from scripts.plotting import plot_paper_vs_backtest
    plot_paper_vs_backtest(equity, backtest.equity, report_dir / "paper_vs_backtest.png")
    print((report_dir / "daily_summary.md").read_text())
    print((report_dir / "paper_vs_backtest.md").read_text())
    return {"daily": paper_daily, "comparison": comparison, "daily_comparison": daily}
