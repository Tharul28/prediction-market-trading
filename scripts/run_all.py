"""Run the whole pipeline: data, order book metrics, backtests, metrics, charts.

Usage (from the project root)::

    # Simulated data only: 30 two-hour sessions per scenario.
    python scripts/run_all.py

    # Real data saved by the Kalshi collector, cut into two-hour sessions.
    python scripts/run_all.py --data data/raw/example

    # Real data AND simulated data, plus a side-by-side comparison.
    python scripts/run_all.py --data data/raw/example --compare

Both kinds of data go through exactly the same scenarios, strategies, engine
and metrics. Outputs:

* ``results/simulated/``: simulated runs,
* ``results/real_<name>/``: real-data runs,
* ``results/comparison_<name>/``: side-by-side table and chart.

Each results folder contains ``runs.csv`` (one row per scenario and session),
``scenario_summary.csv`` / ``.md``, ``markouts.csv``, ``fill_reasons.csv``,
``half_spread_sweep.csv`` and ``charts/``.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from backtest.engine import BacktestConfig, BacktestEngine, BacktestResult  # noqa: E402
from backtest.events import build_event_stream  # noqa: E402
from backtest.fees import FeeModel  # noqa: E402
from backtest.fill_model import FillModel  # noqa: E402
from backtest.metrics import (average_markouts, basket_summary, markouts, mid_lookup,  # noqa: E402
                              performance_summary)
from backtest.orders import Liquidity  # noqa: E402
from data.loader import SIMULATED, load_market_data  # noqa: E402
from data.schema import MarketData, snapshots_from_books, split_into_sessions  # noqa: E402
from data.simulator import SimulationConfig  # noqa: E402
from data.storage import save_market_data  # noqa: E402
from orderbook.metrics import metrics_frame  # noqa: E402
from scripts import plotting  # noqa: E402
from strategies.base import Strategy  # noqa: E402
from strategies.cross_outcome_arb import ArbConfig, CrossOutcomeArbitrage  # noqa: E402
from strategies.market_maker import InventorySkewMarketMaker, MarketMakerConfig  # noqa: E402

# ---------------------------------------------------------------------------
# Default data source when --data is not given. Change this one line to the
# directory written by the Kalshi collector to make real data the default.
DATA_SOURCE = SIMULATED  # e.g. "data/raw/example"
# ---------------------------------------------------------------------------

RESULTS = ROOT / "results"
FEES_ON = FeeModel()
FEES_OFF = FeeModel(enabled=False)
HALF_SPREAD_SWEEP = (0.5, 1.0, 2.0, 3.0)

Builder = Callable[[MarketData, str], tuple[Strategy, BacktestConfig]]


@dataclass(frozen=True)
class Scenario:
    """A named combination of strategy and exchange settings."""

    name: str
    label: str
    family: str  # "mm", "sweep" or "arb"
    build: Builder


def _mm(half_spread: float = 1.0, skew: float = 3.0, fees: FeeModel = FEES_ON, queue: str = "back") -> Builder:
    def build(data: MarketData, mm_market: str) -> tuple[Strategy, BacktestConfig]:
        strategy = InventorySkewMarketMaker(MarketMakerConfig(
            market_id=mm_market, half_spread_cents=half_spread, skew_cents_at_max=skew))
        return strategy, BacktestConfig(fees=fees, fill_model=FillModel(queue_position=queue))
    return build


def _arb(fees: FeeModel = FEES_ON, max_unhedged: float = 30.0) -> Builder:
    def build(data: MarketData, mm_market: str) -> tuple[Strategy, BacktestConfig]:
        strategy = CrossOutcomeArbitrage(ArbConfig(
            market_ids=tuple(data.markets), outcomes_mutually_exclusive=data.mutually_exclusive,
            outcomes_exhaustive=data.exhaustive, max_unhedged_contracts=max_unhedged), fees)
        return strategy, BacktestConfig(fees=fees)
    return build


SCENARIOS = [
    Scenario("mm_base", "MM: baseline (back of queue, fees on)", "mm", _mm()),
    Scenario("mm_no_fees", "MM: fees off", "mm", _mm(fees=FEES_OFF)),
    Scenario("mm_no_skew", "MM: no inventory skew", "mm", _mm(skew=0.0)),
    Scenario("mm_front_queue", "MM: front of queue (unrealistic)", "mm", _mm(queue="front")),
    Scenario("mm_front_queue_no_fees", "MM: front of queue, fees off", "mm", _mm(queue="front", fees=FEES_OFF)),
    *[Scenario(f"mm_half_spread_{h:g}", f"MM: half-spread {h:g}c", "sweep", _mm(half_spread=h))
      for h in HALF_SPREAD_SWEEP if h != 1.0],
    Scenario("arb_fees_on", "Arb: fees on", "arb", _arb()),
    Scenario("arb_fees_off", "Arb: fees off", "arb", _arb(fees=FEES_OFF)),
    Scenario("arb_fees_off_no_cap", "Arb: fees off, no exposure cap", "arb",
             _arb(fees=FEES_OFF, max_unhedged=float("inf"))),
]
LABELS = {s.name: s.label for s in SCENARIOS}
FAMILIES = {s.name: s.family for s in SCENARIOS}


def scenarios_for(data: MarketData) -> list[Scenario]:
    """Arbitrage only makes sense for two or more mutually exclusive outcomes."""
    can_arb = data.mutually_exclusive and len(data.markets) >= 2
    return [s for s in SCENARIOS if s.family != "arb" or can_arb]


# --------------------------------------------------------------------------- one scenario
def run_scenario(scenario: Scenario, data: MarketData, mm_market: str, events: list,
                 mids: dict) -> tuple[dict, BacktestResult, Strategy]:
    """Run one scenario on one session and collect its metrics."""
    strategy, config = scenario.build(data, mm_market)
    result = BacktestEngine(data, strategy, config, events=events).run()
    row = performance_summary(result)
    if scenario.family in ("mm", "sweep") and not result.fills.empty:
        for horizon, value in average_markouts(markouts(result.fills, mids)).items():
            row[horizon] = value
        reasons = result.fills.groupby("reason")["size"].sum() / result.fills["size"].sum()
        for reason, share in reasons.items():
            row[f"fill_share_{reason}"] = share
    if isinstance(strategy, CrossOutcomeArbitrage):
        row.update(basket_summary(result))
        row["opportunities_raw"] = float(len(strategy.opportunities))
        row["opportunities_after_fees"] = float(sum(o.net_edge_cents >= strategy.config.min_edge_cents
                                                    for o in strategy.opportunities))
        row["opportunities_acted"] = float(sum(o.acted for o in strategy.opportunities))
    return row, result, strategy


# --------------------------------------------------------------------------- summaries
def summarise(runs: pd.DataFrame) -> pd.DataFrame:
    """Average metrics across sessions for each scenario."""
    columns = ["net_pnl", "gross_pnl", "fees_paid", "liquidation_pnl", "settlement_pnl", "n_fills",
               "contracts_traded", "passive_fill_rate_contracts", "taker_fill_rate_contracts",
               "max_abs_inventory", "interval_pnl_vol", "sharpe_style", "max_drawdown",
               "markout_0s", "markout_30s", "markout_300s",
               "baskets_sent", "baskets_complete", "baskets_partial", "baskets_missed",
               "unhedged_contracts", "opportunities_raw", "opportunities_after_fees", "opportunities_acted"]
    columns = [c for c in columns if c in runs.columns]
    grouped = runs.groupby("scenario", sort=False)
    summary = grouped[columns].mean()
    summary["net_pnl_std"] = grouped["net_pnl"].std()
    summary["settlement_pnl_std"] = grouped["settlement_pnl"].std()
    summary["share_sessions_profitable"] = grouped["net_pnl"].apply(lambda s: float((s > 0).mean()))
    summary["sharpe_across_sessions"] = summary["net_pnl"] / summary["net_pnl_std"]
    summary["sessions"] = grouped.size()
    return summary


def to_markdown(summary: pd.DataFrame) -> str:
    """A compact markdown table of the headline numbers."""
    headers = ["Scenario", "Net P&L $ (mean)", "Net P&L $ (std)", "Gross P&L $", "Fees $",
               "Liquidation P&L $", "Profitable sessions", "Fills", "Max abs inventory", "Max drawdown $"]
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for name, row in summary.iterrows():
        std = "n/a" if pd.isna(row["net_pnl_std"]) else f"{row['net_pnl_std']:.2f}"
        lines.append("| " + " | ".join([
            LABELS.get(name, name), f"{row['net_pnl']:.2f}", std, f"{row['gross_pnl']:.2f}",
            f"{row['fees_paid']:.2f}", f"{row['liquidation_pnl']:.2f}", f"{row['share_sessions_profitable']:.0%}",
            f"{row['n_fills']:.0f}", f"{row['max_abs_inventory']:.0f}", f"{row['max_drawdown']:.2f}",
        ]) + " |")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- pipeline
@dataclass
class PipelineOutput:
    """What one pipeline run produced (also written to ``out_dir``)."""

    label: str
    out_dir: Path
    runs: pd.DataFrame
    summary: pd.DataFrame
    markouts: pd.DataFrame


def run_pipeline(sessions: Iterable[MarketData], label: str, out_dir: Path, mm_market: str | None = None,
                 save_first_session_to: Path | None = None) -> PipelineOutput:
    """Backtest every scenario on every session, then write tables and charts.

    Args:
        sessions: Market data sessions (simulated seeds or slices of a recording).
        label: Name used in printed output ("simulated" or "real").
        out_dir: Where to write results.
        mm_market: Market for the market maker (default: the first market).
        save_first_session_to: Optionally save the first session's data here.
    """
    charts = out_dir / "charts"
    charts.mkdir(parents=True, exist_ok=True)
    rows = []
    markout_tables: dict[str, list[pd.DataFrame]] = {"mm_base": [], "mm_front_queue": []}
    first: dict[str, object] = {}
    started = time.time()

    for index, data in enumerate(sessions):
        market = mm_market or data.markets[0]
        events = build_event_stream(data)
        book_metrics = metrics_frame(snapshots_from_books(data.books))
        mids = mid_lookup(book_metrics)
        for scenario in scenarios_for(data):
            row, result, strategy = run_scenario(scenario, data, market, events, mids)
            rows.append({"scenario": scenario.name, "session": index, **row})
            if scenario.name in markout_tables and not result.fills.empty:
                markout_tables[scenario.name].append(markouts(result.fills, mids))
            if index == 0:
                first[scenario.name] = (result, strategy)
        if index == 0:
            first.update(data=data, book_metrics=book_metrics, market=market)
            if save_first_session_to is not None:
                save_market_data(data, save_first_session_to)
        print(f"[{label}] session {index + 1} done ({time.time() - started:.0f}s)")

    if not rows:
        raise RuntimeError(f"No usable sessions for {label}: is the recording long enough?")
    runs = pd.DataFrame(rows)
    runs.to_csv(out_dir / "runs.csv", index=False)
    summary = summarise(runs)
    summary.to_csv(out_dir / "scenario_summary.csv")
    (out_dir / "scenario_summary.md").write_text(to_markdown(summary))

    pooled = {LABELS[name]: average_markouts(pd.concat(tables, ignore_index=True))
              for name, tables in markout_tables.items() if tables}
    markout_summary = pd.DataFrame(pooled).T
    markout_summary.to_csv(out_dir / "markouts.csv")

    reason_columns = [c for c in runs.columns if c.startswith("fill_share_")]
    fill_reasons = runs[runs["scenario"].isin(["mm_base", "mm_front_queue"])].groupby("scenario")[reason_columns].mean()
    fill_reasons.to_csv(out_dir / "fill_reasons.csv")

    sweep_names = {1.0: "mm_base", **{h: f"mm_half_spread_{h:g}" for h in HALF_SPREAD_SWEEP if h != 1.0}}
    sweep = pd.DataFrame([{
        "half_spread": h,
        "net_pnl_mean": runs.loc[runs["scenario"] == name, "net_pnl"].mean(),
        "net_pnl_std": runs.loc[runs["scenario"] == name, "net_pnl"].std(),
        "gross_pnl_mean": runs.loc[runs["scenario"] == name, "gross_pnl"].mean(),
        "gross_pnl_std": runs.loc[runs["scenario"] == name, "gross_pnl"].std(),
        "n_fills_mean": runs.loc[runs["scenario"] == name, "n_fills"].mean(),
    } for h, name in sorted(sweep_names.items())])
    sweep.to_csv(out_dir / "half_spread_sweep.csv", index=False)

    _draw_charts(first, runs, markout_summary, sweep, charts)
    print(f"\n[{label}] summary over {runs['session'].nunique()} session(s):\n" + to_markdown(summary))
    return PipelineOutput(label, out_dir, runs, summary, markout_summary)


def _draw_charts(first: dict, runs: pd.DataFrame, markout_summary: pd.DataFrame, sweep: pd.DataFrame,
                 charts: Path) -> None:
    """Charts for the first session plus cross-session charts."""
    data: MarketData = first["data"]  # type: ignore[assignment]
    book_metrics: pd.DataFrame = first["book_metrics"]  # type: ignore[assignment]
    market: str = first["market"]  # type: ignore[assignment]
    mm_result, _ = first["mm_base"]  # type: ignore[misc]
    length_minutes = (book_metrics["timestamp"].max() - book_metrics["timestamp"].min()) / 60.0
    zoom = (max(0.0, length_minutes / 2 - 5), length_minutes / 2 + 5)

    plotting.plot_market_and_fills(book_metrics, data.truth, mm_result.fills, market,
                                   charts / "01_market_and_fills.png", zoom)
    plotting.plot_orderbook_metrics(book_metrics, market, charts / "02_orderbook_metrics.png")
    plotting.plot_equity_and_inventory(mm_result.equity, market, charts / "03_mm_equity_inventory.png",
                                       f"Market maker on {market}, first session (baseline settings)")
    plotting.plot_pnl_by_scenario(runs, LABELS, FAMILIES, charts / "04_pnl_by_scenario.png")
    if not markout_summary.empty:
        plotting.plot_markouts(markout_summary, charts / "05_markouts.png")
    if "arb_fees_on" in first:
        _, arb_strategy = first["arb_fees_on"]  # type: ignore[misc]
        typical_mid = float(np.nanmedian(book_metrics["mid"]))
        typical_fee = len(data.markets) * FEES_ON.fee_per_contract_cents(int(round(typical_mid)), 10,
                                                                          Liquidity.TAKER)
        acted = [o.timestamp for o in arb_strategy.opportunities if o.acted]
        plotting.plot_basket_prices(book_metrics, data.markets, typical_fee, acted,
                                    charts / "06_arb_basket_prices.png")
    plotting.plot_parameter_sweep(sweep, charts / "07_half_spread_sweep.png")


# --------------------------------------------------------------------------- comparison
def compare(simulated: PipelineOutput, real: PipelineOutput, out_dir: Path) -> pd.DataFrame:
    """Side-by-side table and chart of simulated against real results, per scenario."""
    out_dir.mkdir(parents=True, exist_ok=True)
    metrics = [("net_pnl", "Net P&L $", "{:.2f}"), ("gross_pnl", "Gross P&L $", "{:.2f}"),
               ("liquidation_pnl", "Liquidation P&L $", "{:.2f}"), ("n_fills", "Fills", "{:.0f}"),
               ("passive_fill_rate_contracts", "Passive fill rate", "{:.1%}"),
               ("max_abs_inventory", "Max abs inventory", "{:.0f}"),
               ("markout_30s", "Markout 30s (c)", "{:.2f}"), ("max_drawdown", "Max drawdown $", "{:.2f}")]
    rows = []
    for name in [s.name for s in SCENARIOS if s.name in simulated.summary.index and s.name in real.summary.index]:
        row = {"scenario": LABELS[name]}
        for column, title, _ in metrics:
            for source, output in (("sim", simulated), ("real", real)):
                value = output.summary.loc[name, column] if column in output.summary.columns else np.nan
                row[f"{title} [{source}]"] = value
        row["Sessions [sim]"] = simulated.summary.loc[name, "sessions"]
        row["Sessions [real]"] = real.summary.loc[name, "sessions"]
        rows.append(row)
    table = pd.DataFrame(rows)
    table.to_csv(out_dir / "comparison.csv", index=False)

    lines = ["Means per session. Simulated and real sessions have the same length.", ""]
    headers = ["Scenario"] + [f"{title} sim / real" for _, title, _ in metrics] + ["Sessions sim / real"]
    lines += ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for _, row in table.iterrows():
        cells = [row["scenario"]]
        for _, title, fmt in metrics:
            pair = [row[f"{title} [{s}]"] for s in ("sim", "real")]
            cells.append(" / ".join("n/a" if pd.isna(v) else fmt.format(v) for v in pair))
        cells.append(f"{row['Sessions [sim]']:.0f} / {row['Sessions [real]']:.0f}")
        lines.append("| " + " | ".join(cells) + " |")
    (out_dir / "comparison.md").write_text("\n".join(lines) + "\n")

    plotting.plot_sim_vs_real(simulated.runs, real.runs, LABELS, out_dir / "08_sim_vs_real.png")
    print("\nSimulated vs real:\n" + "\n".join(lines))
    return table


# --------------------------------------------------------------------------- entry point
def simulated_sessions(n_sessions: int, session_hours: float) -> Iterable[MarketData]:
    """Generate simulated sessions lazily, one seed each."""
    config = SimulationConfig(duration_s=session_hours * 3600.0)
    for seed in range(n_sessions):
        yield load_market_data(SIMULATED, seed=seed, simulation_config=config)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", default=DATA_SOURCE,
                        help='"simulated" or a directory written by the Kalshi collector')
    parser.add_argument("--seeds", type=int, default=30, help="Number of simulated sessions")
    parser.add_argument("--session-hours", type=float, default=2.0,
                        help="Session length; real recordings are cut into sessions of this length")
    parser.add_argument("--mm-market", default=None, help="Market for the market maker (default: first)")
    parser.add_argument("--compare", action="store_true",
                        help="With --data DIR: also run simulated sessions and compare side by side")
    args = parser.parse_args()
    started = time.time()

    simulated_output = None
    if args.data == SIMULATED or args.compare:
        simulated_output = run_pipeline(
            simulated_sessions(args.seeds, args.session_hours), "simulated", RESULTS / "simulated",
            save_first_session_to=ROOT / "data" / "simulated" / "seed0")

    if args.data != SIMULATED:
        name = Path(args.data).name
        recording = load_market_data(args.data)
        sessions = split_into_sessions(recording, args.session_hours * 3600.0)
        print(f"Loaded {args.data}: {len(recording.markets)} markets, {len(recording.books)} book rows, "
              f"{len(recording.trades)} trades, {len(sessions)} session(s) of up to {args.session_hours:g} h")
        real_output = run_pipeline(sessions, "real", RESULTS / f"real_{name}", mm_market=args.mm_market)
        if simulated_output is not None:
            compare(simulated_output, real_output, RESULTS / f"comparison_{name}")

    print(f"\nDone in {time.time() - started:.0f}s. Results in {RESULTS}")


if __name__ == "__main__":
    main()
