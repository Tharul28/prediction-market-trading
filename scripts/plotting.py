"""Matplotlib charts used by ``scripts/run_all.py``.

Every function takes plain DataFrames and a file path, draws one figure and
saves it. Colours follow a fixed order (blue, orange, aqua, ...) so the same
series keeps the same colour across charts. Charts never use two y-axes:
quantities with different units get separate panels.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # draw to files, no window needed
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

BLUE, ORANGE, AQUA, YELLOW, MAGENTA, GREEN, VIOLET, RED = (
    "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948")
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
GRID = "#e4e3df"
SURFACE = "#fcfcfb"

plt.rcParams.update({
    "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE,
    "axes.edgecolor": INK_SECONDARY,
    "axes.labelcolor": INK,
    "axes.titlesize": 11,
    "axes.titleweight": "bold",
    "axes.labelsize": 9,
    "axes.grid": True,
    "grid.color": GRID,
    "grid.linewidth": 0.8,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "xtick.color": INK_SECONDARY,
    "ytick.color": INK_SECONDARY,
    "xtick.labelsize": 8,
    "ytick.labelsize": 8,
    "legend.fontsize": 8,
    "legend.frameon": True,
    "legend.facecolor": SURFACE,
    "legend.edgecolor": GRID,
    "legend.framealpha": 0.92,
    "lines.linewidth": 1.5,
    "font.size": 9,
})


def _minutes(timestamps: pd.Series | np.ndarray, start: float) -> np.ndarray:
    return (np.asarray(timestamps, dtype=float) - start) / 60.0


def _save(fig: plt.Figure, path: Path) -> None:
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_market_and_fills(book_metrics: pd.DataFrame, truth: pd.DataFrame | None, fills: pd.DataFrame,
                          market: str, path: Path, zoom_minutes: tuple[float, float] = (30.0, 40.0)) -> None:
    """Best bid/ask band, mid and (if simulated) true probability, plus a zoom with our fills."""
    frame = book_metrics[book_metrics["market_id"] == market]
    start = float(book_metrics["timestamp"].min())
    minutes = _minutes(frame["timestamp"], start)
    fig, axes = plt.subplots(2, 1, figsize=(10, 6.5))

    for ax, window in ((axes[0], None), (axes[1], zoom_minutes)):
        mask = np.ones(len(frame), dtype=bool) if window is None else (minutes >= window[0]) & (minutes <= window[1])
        ax.fill_between(minutes[mask], frame["best_bid"].to_numpy()[mask], frame["best_ask"].to_numpy()[mask],
                        step="post", color=BLUE, alpha=0.18, linewidth=0, label="Best bid to best ask")
        ax.step(minutes[mask], frame["mid"].to_numpy()[mask], where="post", color=BLUE, linewidth=1.2, label="Mid")
        if truth is not None:
            true_frame = truth[truth["market_id"] == market]
            true_minutes = _minutes(true_frame["timestamp"], start)
            true_mask = np.ones(len(true_frame), dtype=bool) if window is None else (
                (true_minutes >= window[0]) & (true_minutes <= window[1]))
            ax.plot(true_minutes[true_mask], 100 * true_frame["true_prob"].to_numpy()[true_mask],
                    color=ORANGE, linewidth=1.5, label="True probability (simulator only)")
        ax.set_ylabel("Price (cents)")

    if not fills.empty and zoom_minutes is not None:
        fill_minutes = _minutes(fills["timestamp"], start)
        in_window = (fill_minutes >= zoom_minutes[0]) & (fill_minutes <= zoom_minutes[1]) & (fills["market_id"] == market)
        for side, marker, colour, label in (("buy", "^", AQUA, "Our buy fills"), ("sell", "v", RED, "Our sell fills")):
            chosen = in_window & (fills["side"] == side)
            axes[1].scatter(fill_minutes[chosen], fills.loc[chosen, "price"], marker=marker, s=46,
                            color=colour, edgecolor=SURFACE, linewidth=1.5, zorder=5, label=label)

    axes[0].set_title(f"{market}: visible market and true probability, whole session")
    axes[1].set_title(f"Zoom: minutes {zoom_minutes[0]:g} to {zoom_minutes[1]:g}, with the market maker's fills")
    axes[1].set_xlabel("Minutes since start")
    axes[0].legend(loc="best", ncols=3)
    axes[1].legend(loc="best", ncols=3)
    _save(fig, path)


def plot_orderbook_metrics(book_metrics: pd.DataFrame, market: str, path: Path) -> None:
    """Spread, top-of-book imbalance and depth over time for one market."""
    frame = book_metrics[book_metrics["market_id"] == market].sort_values("timestamp")
    start = float(book_metrics["timestamp"].min())
    minutes = _minutes(frame["timestamp"], start)
    fig, axes = plt.subplots(3, 1, figsize=(10, 7), sharex=True)

    axes[0].step(minutes, frame["spread"], where="post", color=BLUE, linewidth=1.0)
    axes[0].set_ylabel("Spread (cents)")
    axes[0].set_title(f"{market}: order book metrics")

    imbalance = frame["imbalance_top"].rolling(60, min_periods=1).mean()
    axes[1].plot(minutes, imbalance, color=BLUE)
    axes[1].axhline(0, color=INK_SECONDARY, linewidth=0.8)
    axes[1].set_ylabel("Top-of-book imbalance\n(60-snapshot average)")

    axes[2].plot(minutes, frame["bid_depth"].rolling(60, min_periods=1).mean(), color=BLUE, label="Bid depth")
    axes[2].plot(minutes, frame["ask_depth"].rolling(60, min_periods=1).mean(), color=ORANGE, label="Ask depth")
    axes[2].set_ylabel("Contracts in best 5 levels\n(60-snapshot average)")
    axes[2].set_xlabel("Minutes since start")
    axes[2].legend(loc="upper left", ncols=2)
    _save(fig, path)


def plot_equity_and_inventory(equity: pd.DataFrame, market: str, path: Path, title: str) -> None:
    """Gross and net equity on one panel, inventory on another."""
    start = float(equity["timestamp"].min())
    minutes = _minutes(equity["timestamp"], start)
    fig, axes = plt.subplots(2, 1, figsize=(10, 6), sharex=True, gridspec_kw={"height_ratios": [3, 2]})
    axes[0].plot(minutes, equity["gross_equity"], color=BLUE, label="Before fees")
    axes[0].plot(minutes, equity["net_equity"], color=ORANGE, label="After fees")
    axes[0].axhline(0, color=INK_SECONDARY, linewidth=0.8)
    axes[0].set_ylabel("P&L, marked at mid ($)")
    axes[0].set_title(title)
    axes[0].legend(loc="upper left", ncols=2)
    axes[1].step(minutes, equity[f"position_{market}"], where="post", color=BLUE)
    axes[1].axhline(0, color=INK_SECONDARY, linewidth=0.8)
    axes[1].set_ylabel("Position (contracts)")
    axes[1].set_xlabel("Minutes since start")
    _save(fig, path)


def plot_pnl_by_scenario(runs: pd.DataFrame, scenario_labels: dict[str, str], families: dict[str, str],
                         path: Path) -> None:
    """One dot per simulated session, per scenario; market making and arbitrage in separate panels."""
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.8), gridspec_kw={"width_ratios": [3, 2]})
    rng = np.random.default_rng(0)
    for ax, family, title in ((axes[0], "mm", "Market making"), (axes[1], "arb", "Cross-outcome arbitrage")):
        names = [n for n in scenario_labels if families[n] == family and n in set(runs["scenario"])]
        for row, name in enumerate(names):
            values = runs.loc[runs["scenario"] == name, "net_pnl"].to_numpy()
            jitter = rng.uniform(-0.15, 0.15, len(values))
            ax.scatter(values, row + jitter, s=20, color=BLUE, alpha=0.75, edgecolor=SURFACE, linewidth=0.8)
            ax.plot([values.mean()] * 2, [row - 0.3, row + 0.3], color=ORANGE, linewidth=2.5)
        ax.axvline(0, color=INK_SECONDARY, linewidth=0.8)
        ax.set_yticks(range(len(names)), [scenario_labels[n] for n in names])
        ax.invert_yaxis()
        ax.set_xlabel("Net P&L per session, marked at mid ($)")
        ax.set_title(title)
    axes[0].text(0.99, 0.01, "dots: sessions   orange bar: mean", transform=axes[0].transAxes,
                 ha="right", va="bottom", fontsize=8, color=INK_SECONDARY)
    _save(fig, path)


def plot_markouts(markout_table: pd.DataFrame, path: Path) -> None:
    """Average markout against horizon, one line per scenario."""
    fig, ax = plt.subplots(figsize=(8, 4.2))
    colours = [BLUE, ORANGE, AQUA, YELLOW]
    for colour, (label, row) in zip(colours, markout_table.iterrows()):
        horizons = [float(c.replace("markout_", "").replace("s", "")) for c in markout_table.columns]
        ax.plot(horizons, row.to_numpy(), marker="o", markersize=5, color=colour, label=label)
    ax.axhline(0, color=INK_SECONDARY, linewidth=0.8)
    ax.set_xscale("symlog", linthresh=5)
    ax.set_xticks([0, 5, 15, 30, 60, 120, 300], ["0", "5", "15", "30", "60", "120", "300"])
    ax.set_xlabel("Seconds after the fill")
    ax.set_ylabel("Mid move in our favour\n(cents per contract)")
    ax.set_title("Markouts: is the market maker adversely selected?")
    ax.legend(loc="lower left")
    _save(fig, path)


def plot_basket_prices(book_metrics: pd.DataFrame, markets: list[str], fee_threshold_cents: float,
                       acted_times: list[float], path: Path) -> None:
    """Sum of best asks and sum of best bids across outcomes, with the break-even lines."""
    start = float(book_metrics["timestamp"].min())
    asks = book_metrics.pivot_table(index="timestamp", columns="market_id", values="best_ask").ffill()[markets]
    bids = book_metrics.pivot_table(index="timestamp", columns="market_id", values="best_bid").ffill()[markets]
    minutes = _minutes(asks.index.to_numpy(), start)
    fig, ax = plt.subplots(figsize=(10, 4.5))
    ax.step(minutes, asks.sum(axis=1), where="post", color=BLUE, linewidth=1.0, label="Sum of best asks (cost to buy all)")
    ax.step(minutes, bids.sum(axis=1), where="post", color=ORANGE, linewidth=1.0, label="Sum of best bids (proceeds from selling all)")
    ax.axhline(100, color=INK, linewidth=1.0)
    ax.axhline(100 - fee_threshold_cents, color=INK_SECONDARY, linewidth=1.0, linestyle="--")
    ax.axhline(100 + fee_threshold_cents, color=INK_SECONDARY, linewidth=1.0, linestyle="--")
    label_box = {"facecolor": SURFACE, "edgecolor": "none", "alpha": 0.9, "pad": 1.5}
    ax.text(minutes[-1], 100.3, "payout = 100", ha="right", va="bottom", fontsize=8, color=INK, bbox=label_box)
    ax.text(minutes[-1], 100 + fee_threshold_cents + 0.3, "break-even after typical taker fees", ha="right",
            va="bottom", fontsize=8, color=INK_SECONDARY, bbox=label_box)
    if acted_times:
        acted_minutes = _minutes(np.asarray(acted_times), start)
        ax.scatter(acted_minutes, np.full(len(acted_minutes), ax.get_ylim()[0] + 0.5), marker="|", s=80,
                   color=AQUA, label="Basket sent (fees on)")
    ax.set_xlabel("Minutes since start")
    ax.set_ylabel("Cents")
    ax.set_title("Do the outcome prices add up to 100?")
    ax.legend(loc="upper left", ncols=3)
    _save(fig, path)


def plot_parameter_sweep(sweep: pd.DataFrame, path: Path) -> None:
    """Mean net and gross P&L (with one standard deviation) against half-spread."""
    fig, ax = plt.subplots(figsize=(8, 4.2))
    offsets = {"gross_pnl": -0.04, "net_pnl": 0.04}
    for column, colour, label in (("gross_pnl", BLUE, "Before fees"), ("net_pnl", ORANGE, "After fees")):
        ax.errorbar(sweep["half_spread"] + offsets[column], sweep[f"{column}_mean"], yerr=sweep[f"{column}_std"],
                    color=colour, marker="o", markersize=6, capsize=3, linewidth=1.5, label=label)
    ax.axhline(0, color=INK_SECONDARY, linewidth=0.8)
    ax.set_xticks(sweep["half_spread"])
    ax.set_xlabel("Quote half-spread around fair value (cents)")
    ax.set_ylabel("P&L per session ($)\nmean, bars = 1 std")
    ax.set_title("Sensitivity to the quoting width (in-sample, illustration only)")
    ax.legend(loc="lower left")
    _save(fig, path)


def plot_sim_vs_real(sim_runs: pd.DataFrame, real_runs: pd.DataFrame, scenario_labels: dict[str, str],
                     path: Path) -> None:
    """Net P&L per session for each scenario: simulated sessions beside real sessions."""
    names = [n for n in scenario_labels
             if n in set(sim_runs["scenario"]) and n in set(real_runs["scenario"]) and not n.startswith("mm_half")]
    fig, ax = plt.subplots(figsize=(10, 0.55 * len(names) + 1.6))
    rng = np.random.default_rng(0)
    for row, name in enumerate(names):
        for offset, runs, colour, label in ((-0.17, sim_runs, BLUE, "Simulated sessions"),
                                            (0.17, real_runs, ORANGE, "Real sessions")):
            values = runs.loc[runs["scenario"] == name, "net_pnl"].to_numpy()
            ax.scatter(values, row + offset + rng.uniform(-0.06, 0.06, len(values)), s=18, color=colour,
                       alpha=0.8, edgecolor=SURFACE, linewidth=0.8, label=label if row == 0 else None)
            ax.plot([values.mean()] * 2, [row + offset - 0.12, row + offset + 0.12], color=INK, linewidth=2)
    ax.axvline(0, color=INK_SECONDARY, linewidth=0.8)
    ax.set_yticks(range(len(names)), [scenario_labels[n] for n in names])
    ax.invert_yaxis()
    ax.set_xlabel("Net P&L per session, marked at mid ($); black bar = mean")
    ax.set_title("Simulated versus real data, same strategies and metrics")
    ax.legend(loc="lower right")
    _save(fig, path)


def plot_paper_vs_backtest(paper_equity: pd.DataFrame, backtest_equity: pd.DataFrame, path: Path) -> None:
    """Cumulative net P&L of the paper run and of the backtest over the same period."""
    start = float(min(paper_equity["timestamp"].min(), backtest_equity["timestamp"].min()))
    fig, ax = plt.subplots(figsize=(10, 4.2))
    ax.plot((backtest_equity["timestamp"] - start) / 3600.0, backtest_equity["net_equity"], color=BLUE,
            label="Backtest on the recorded data")
    ax.plot((paper_equity["timestamp"] - start) / 3600.0, paper_equity["net_pnl"], color=ORANGE,
            label="Paper trading (live)")
    ax.axhline(0, color=INK_SECONDARY, linewidth=0.8)
    ax.set_xlabel("Hours since start")
    ax.set_ylabel("Net P&L, marked at mid ($)")
    ax.set_title("Paper trading versus backtest, same period and settings")
    ax.legend(loc="upper left")
    _save(fig, path)
