"""Where does the market maker make and lose money in a real recording?

Finds sudden price jumps (in football: goals, red cards, VAR decisions), runs
the market-making scenarios on every market of the recording, and splits each
run's P&L into the minutes around the jumps and everything else.

Usage (from the project root)::

    python scripts/analyse_recording.py --data data/raw/ger_ned
    python scripts/analyse_recording.py --data data/raw/ger_ned --kickoff 2026-11-16T19:45:00Z

Outputs in ``results/analysis_<name>/``:

* ``jumps.csv``: each detected jump, with prices before and after.
* ``pnl_split.csv``: per scenario, market and session, net P&L inside jump
  windows and outside them (and before/after kick-off, if given).
* ``pnl_split.md``: the same, summed over sessions, as a table.
* ``charts/prices_and_jumps.png`` and ``charts/equity_<market>.png``.

A jump is a move of at least ``--jump-cents`` in some market's mid price
within 60 seconds. Its window runs from ``--before`` seconds before the move
starts to ``--after`` seconds after it ends. P&L inside a window is the
change in marked-to-mid equity across it, so it includes the loss on
inventory that the jump re-prices.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from backtest.events import build_event_stream  # noqa: E402
from backtest.metrics import mid_lookup  # noqa: E402
from data.kalshi import parse_utc  # noqa: E402
from data.loader import load_market_data  # noqa: E402
from data.schema import MarketData, snapshots_from_books, split_into_sessions  # noqa: E402
from orderbook.metrics import metrics_frame  # noqa: E402
from scripts import plotting  # noqa: E402
from scripts.run_all import LABELS, SCENARIOS, run_scenario  # noqa: E402

ANALYSED = ["mm_base", "mm_jump_guard", "mm_no_skew", "mm_front_queue_no_fees", "mm_half_spread_2",
            "mm_half_spread_3"]
COLOURS = [plotting.BLUE, plotting.GREEN, plotting.ORANGE, plotting.AQUA, plotting.MAGENTA, plotting.VIOLET]


def short(market: str) -> str:
    """Last part of a Kalshi ticker, e.g. 'GER' for KXUEFANLGAME-26NOV16GERNED-GER."""
    return market.rsplit("-", 1)[-1]


# --------------------------------------------------------------------------- jumps
def mid_grid(book_metrics: pd.DataFrame, step_s: float = 10.0) -> pd.DataFrame:
    """Mid price of every market on a regular time grid (last value carried forward)."""
    mids = book_metrics.pivot_table(index="timestamp", columns="market_id", values="mid").sort_index().ffill()
    grid = np.arange(mids.index.min(), mids.index.max() + step_s, step_s)
    return mids.reindex(mids.index.union(grid)).ffill().reindex(grid)


def find_jumps(mids: pd.DataFrame, jump_cents: float, lookback_s: float = 60.0,
               merge_gap_s: float = 180.0) -> pd.DataFrame:
    """Episodes where some market's mid moved at least ``jump_cents`` within ``lookback_s``.

    Returns:
        One row per episode: start (when the move began), end, the largest
        60 s move, and each market's mid before and after.
    """
    step = float(mids.index[1] - mids.index[0]) if len(mids) > 1 else lookback_s
    lag = max(1, int(round(lookback_s / step)))
    move = (mids - mids.shift(lag)).abs().max(axis=1)
    flagged = move[move >= jump_cents]
    if flagged.empty:
        return pd.DataFrame(columns=["start", "end", "max_move_cents"])
    episode = np.cumsum(np.diff(flagged.index, prepend=flagged.index[0]) > merge_gap_s)
    rows = []
    for _, group in flagged.groupby(episode):
        start, end = float(group.index.min()) - lookback_s, float(group.index.max())
        before = mids.loc[:start].iloc[-1]
        after = mids.loc[end:].iloc[min(lag, len(mids.loc[end:]) - 1)]
        row = {"start": start, "end": end, "max_move_cents": float(group.max())}
        for market in mids.columns:
            row[f"before_{short(market)}"] = float(before[market])
            row[f"after_{short(market)}"] = float(after[market])
        rows.append(row)
    return pd.DataFrame(rows)


def windows_from_jumps(jumps: pd.DataFrame, before_s: float, after_s: float) -> list[tuple[float, float]]:
    """Merge overlapping [start - before, end + after] windows."""
    windows: list[tuple[float, float]] = []
    for start, end in sorted(zip(jumps["start"] - before_s, jumps["end"] + after_s)):
        if windows and start <= windows[-1][1]:
            windows[-1] = (windows[-1][0], max(windows[-1][1], end))
        else:
            windows.append((start, end))
    return windows


# --------------------------------------------------------------------------- P&L split
def equity_at(equity: pd.DataFrame, when: float) -> float:
    """Net equity at a time (last value at or before it; 0 before the first row)."""
    times = equity["timestamp"].to_numpy()
    index = int(np.searchsorted(times, when, side="right")) - 1
    return 0.0 if index < 0 else float(equity["net_equity"].iloc[index])


def split_pnl(equity: pd.DataFrame, fills: pd.DataFrame, windows: list[tuple[float, float]],
              kickoff: float | None) -> dict[str, float]:
    """Net P&L and fills inside and outside the jump windows (and before/after kick-off)."""
    total = float(equity["net_equity"].iloc[-1]) if not equity.empty else 0.0
    start, end = (float(equity["timestamp"].iloc[0]), float(equity["timestamp"].iloc[-1])) if not equity.empty else (0, 0)
    inside = 0.0
    fills_inside = 0.0
    seconds_inside = 0.0
    for low, high in windows:
        low, high = max(low, start), min(high, end)
        if high <= low:
            continue
        inside += equity_at(equity, high) - equity_at(equity, low)
        seconds_inside += high - low
        if not fills.empty:
            fills_inside += float(fills.loc[(fills["timestamp"] >= low) & (fills["timestamp"] < high), "size"].sum())
    contracts = float(fills["size"].sum()) if not fills.empty else 0.0
    row = {
        "net_pnl": total, "pnl_in_jump_windows": inside, "pnl_outside": total - inside,
        "contracts": contracts, "contracts_in_jump_windows": fills_inside,
        "share_of_time_in_windows": seconds_inside / (end - start) if end > start else np.nan,
    }
    if kickoff is not None:
        row["pnl_before_kickoff"] = equity_at(equity, kickoff) if kickoff > start else 0.0
        row["pnl_after_kickoff"] = total - row["pnl_before_kickoff"]
    return row


# --------------------------------------------------------------------------- charts
def plot_prices_and_jumps(mids: pd.DataFrame, jumps: pd.DataFrame, windows: list[tuple[float, float]],
                          sessions: list[MarketData], kickoff: float | None, path: Path) -> None:
    start = float(mids.index.min())
    fig, ax = plt.subplots(figsize=(11, 4.5))
    for colour, market in zip(COLOURS, mids.columns):
        ax.plot(plotting._minutes(mids.index, start), mids[market], color=colour, label=short(market))
    for low, high in windows:
        ax.axvspan((low - start) / 60, (high - start) / 60, color=plotting.RED, alpha=0.12, linewidth=0)
    for session in sessions[1:]:
        ax.axvline((float(session.books["timestamp"].min()) - start) / 60, color=plotting.INK_SECONDARY,
                   linestyle="--", linewidth=0.8)
    if kickoff is not None:
        ax.axvline((kickoff - start) / 60, color=plotting.INK, linewidth=1.0)
        ax.annotate("kick-off", ((kickoff - start) / 60, 98), fontsize=8, color=plotting.INK, ha="left")
    ax.set_ylim(0, 100)
    ax.set_ylabel("Mid price (cents)")
    ax.set_xlabel(f"Minutes since {pd.Timestamp(start, unit='s', tz='UTC'):%H:%M} UTC")
    ax.set_title(f"Mid prices; red bands = {len(jumps)} jump window(s); dashed = session boundaries")
    ax.legend(loc="center left", ncols=1)
    plotting._save(fig, path)


def plot_equity(runs: dict[str, pd.DataFrame], market: str, windows: list[tuple[float, float]],
                start: float, path: Path) -> None:
    fig, axes = plt.subplots(2, 1, figsize=(11, 6.5), sharex=True, gridspec_kw={"height_ratios": [3, 2]})
    for colour, (name, equity) in zip(COLOURS, runs.items()):
        minutes = plotting._minutes(equity["timestamp"], start)
        axes[0].plot(minutes, equity["net_equity"], color=colour, label=LABELS[name])
        axes[1].step(minutes, equity[f"position_{market}"], where="post", color=colour)
    for ax in axes:
        for low, high in windows:
            ax.axvspan((low - start) / 60, (high - start) / 60, color=plotting.RED, alpha=0.12, linewidth=0)
        ax.axhline(0, color=plotting.INK_SECONDARY, linewidth=0.8)
    axes[0].set_ylabel("Net P&L, marked at mid ($)")
    axes[0].set_title(f"Market maker on {short(market)}, all sessions joined (each session starts flat)")
    axes[0].legend(loc="lower left", ncols=2)
    axes[1].set_ylabel("Position (contracts)")
    axes[1].set_xlabel(f"Minutes since {pd.Timestamp(start, unit='s', tz='UTC'):%H:%M} UTC")
    plotting._save(fig, path)


# --------------------------------------------------------------------------- main
def to_markdown(table: pd.DataFrame, has_kickoff: bool) -> str:
    headers = ["Scenario", "Market", "Net P&L $", "In jump windows $", "Outside $", "Contracts",
               "Share of contracts in windows"]
    if has_kickoff:
        headers += ["Before kick-off $", "After kick-off $"]
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for _, row in table.iterrows():
        share = row["contracts_in_jump_windows"] / row["contracts"] if row["contracts"] else float("nan")
        cells = [LABELS[row["scenario"]], row["market"], f"{row['net_pnl']:.2f}", f"{row['pnl_in_jump_windows']:.2f}",
                 f"{row['pnl_outside']:.2f}", f"{row['contracts']:.0f}", "n/a" if np.isnan(share) else f"{share:.0%}"]
        if has_kickoff:
            cells += [f"{row['pnl_before_kickoff']:.2f}", f"{row['pnl_after_kickoff']:.2f}"]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", required=True, help="Directory written by the Kalshi collector")
    parser.add_argument("--session-hours", type=float, default=2.0)
    parser.add_argument("--jump-cents", type=float, default=8.0, help="Smallest 60-second mid move counted as a jump")
    parser.add_argument("--before", type=float, default=30.0, help="Seconds before a jump included in its window")
    parser.add_argument("--after", type=float, default=300.0, help="Seconds after a jump included in its window")
    parser.add_argument("--kickoff", default=None, help="Start of play, e.g. 2026-11-16T19:45:00Z (optional)")
    args = parser.parse_args()

    recording = load_market_data(args.data)
    name = Path(args.data).name
    out = ROOT / "results" / f"analysis_{name}"
    (out / "charts").mkdir(parents=True, exist_ok=True)
    kickoff = parse_utc(args.kickoff) if args.kickoff else None

    book_metrics = metrics_frame(snapshots_from_books(recording.books))
    mids = mid_grid(book_metrics)
    jumps = find_jumps(mids, args.jump_cents)
    windows = windows_from_jumps(jumps, args.before, args.after)
    jumps_out = jumps.copy()
    for column in ("start", "end"):
        jumps_out[column] = pd.to_datetime(jumps_out[column], unit="s", utc=True).dt.strftime("%Y-%m-%d %H:%M:%S")
    jumps_out.to_csv(out / "jumps.csv", index=False)
    print(f"{len(jumps)} jump(s) of {args.jump_cents:g}c or more in 60 s (UTC):\n"
          f"{jumps_out.to_string(index=False, float_format=lambda v: f'{v:.1f}')}\n")

    sessions = split_into_sessions(recording, args.session_hours * 3600.0)
    scenarios = [s for s in SCENARIOS if s.name in ANALYSED]
    rows = []
    joined: dict[str, dict[str, list[pd.DataFrame]]] = {m: {s.name: [] for s in scenarios} for m in recording.markets}
    for index, session in enumerate(sessions):
        events = build_event_stream(session)
        mids_lookup = mid_lookup(metrics_frame(snapshots_from_books(session.books)))
        for market in session.markets:
            for scenario in scenarios:
                _, result, _ = run_scenario(scenario, session, market, events, mids_lookup)
                fills = result.fills[result.fills["market_id"] == market] if not result.fills.empty else result.fills
                rows.append({"scenario": scenario.name, "market": short(market), "session": index,
                             **split_pnl(result.equity, fills, windows, kickoff)})
                joined[market][scenario.name].append(result.equity)
        print(f"session {index + 1} of {len(sessions)} done")

    table = pd.DataFrame(rows)
    table.to_csv(out / "pnl_split.csv", index=False)
    totals = table.drop(columns=["session", "share_of_time_in_windows"]).groupby(
        ["scenario", "market"], sort=False).sum().reset_index()
    markdown = to_markdown(totals, kickoff is not None)
    (out / "pnl_split.md").write_text(markdown)
    print("\nSummed over sessions:\n" + markdown)

    start = float(mids.index.min())
    plot_prices_and_jumps(mids, jumps, windows, sessions, kickoff, out / "charts" / "prices_and_jumps.png")
    for market, by_scenario in joined.items():
        runs = {name: pd.concat(frames, ignore_index=True) for name, frames in by_scenario.items() if frames}
        if runs:
            plot_equity(runs, market, windows, start, out / "charts" / f"equity_{short(market)}.png")
    print(f"Results in {out}")


if __name__ == "__main__":
    main()
