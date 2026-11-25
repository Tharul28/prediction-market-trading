"""Market-maker variants on every game recording, chosen on some games and checked on others.

The default market maker quotes ``floor(mid - 1)`` / ``ceil(mid + 1)``. With
a 1-cent spread (50/51) that is 49/52: one tick *behind* the best bid and ask,
so it only fills when the price sweeps through it -- adverse selection by
construction. This script compares that with quoting at the touch
(half-spread 0.5 gives 50/51), with and without the jump guard, with
microprice as fair value, and with more or less inventory skew.

Fees are each series' real schedule: the taker fee everywhere, the maker fee
except in series that charge makers nothing (``MAKER_FREE_SERIES``, checked
against the Kalshi series endpoint in November 2026).

To avoid fitting the result to the data, the games are split by date: the
earlier games choose the variant (``--train``), and the later games, never
looked at while choosing, check it (``--test``). Sessions are 2.5 hours, all
markets of a game are traded separately, back-of-queue fills, 0.5 s latency.

Usage (from the project root)::

    python scripts/mm_lab.py                     # default train/test split below
    python scripts/mm_lab.py --workers 4

Writes ``results/mm_lab/runs.csv`` (one row per variant, game, market,
session) and ``results/mm_lab/report.md``.
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backtest.engine import BacktestConfig, BacktestEngine  # noqa: E402
from backtest.events import build_event_stream  # noqa: E402
from backtest.fees import FeeModel  # noqa: E402
from backtest.fill_model import FillModel  # noqa: E402
from backtest.metrics import performance_summary  # noqa: E402
from data.loader import load_market_data  # noqa: E402
from data.schema import split_into_sessions  # noqa: E402
from strategies.market_maker import InventorySkewMarketMaker, MarketMakerConfig  # noqa: E402

TRAIN = ["ger_ned", "nfl_lac_bal", "nba_gsw_nop", "nba_por_min", "nba_phx_den", "nhl_van_uta",
         "den_nor", "wal_por", "isr_aut", "pol_swe"]                       # Nov 16-17
TEST = ["bmu_koe", "nba_tor_det", "nba_phx_uta", "lfc_mun", "nfl_tb_det", "nfl_mia_buf",
        "nfl_lv_den", "nfl_min_sf"]                                         # Nov 20-23
MAKER_FREE_SERIES = ("KXUEFANLGAME",)

# name -> MarketMakerConfig overrides
VARIANTS = {
    "behind_touch":            dict(half_spread_cents=1.0),               # the current default
    "behind_touch_guard":      dict(half_spread_cents=1.0, jump_guard=True),
    "at_touch":                dict(half_spread_cents=0.5),
    "at_touch_guard":          dict(half_spread_cents=0.5, jump_guard=True),
    "at_touch_guard_micro":    dict(half_spread_cents=0.5, jump_guard=True, fair_value="microprice"),
    "at_touch_guard_skew1":    dict(half_spread_cents=0.5, jump_guard=True, skew_cents_at_max=1.0),
    "at_touch_guard_skew6":    dict(half_spread_cents=0.5, jump_guard=True, skew_cents_at_max=6.0),
    "at_touch_guard_60s":      dict(half_spread_cents=0.5, jump_guard=True, guard_cooldown_s=60.0),
    # round 2, chosen from round 1 on the training games only
    "behind_guard_60s":        dict(half_spread_cents=1.0, jump_guard=True, guard_cooldown_s=60.0),
    "behind_guard_120s":       dict(half_spread_cents=1.0, jump_guard=True, guard_cooldown_s=120.0),
    "at_touch_guard_120s":     dict(half_spread_cents=0.5, jump_guard=True, guard_cooldown_s=120.0),
    "at_touch_guard_micro_60s": dict(half_spread_cents=0.5, jump_guard=True, guard_cooldown_s=60.0,
                                     fair_value="microprice"),
    "at_touch_guard_micro_120s": dict(half_spread_cents=0.5, jump_guard=True, guard_cooldown_s=120.0,
                                      fair_value="microprice"),
    "behind_guard_micro_120s": dict(half_spread_cents=1.0, jump_guard=True, guard_cooldown_s=120.0,
                                    fair_value="microprice"),
}


def fee_model(markets: list[str]) -> FeeModel:
    maker_free = all(m.startswith(MAKER_FREE_SERIES) for m in markets)
    return FeeModel(maker_rate=0.0) if maker_free else FeeModel()


def run_game(game: str, session_hours: float) -> list[dict]:
    data = load_market_data(str(ROOT / "data" / "raw" / game))
    fees = fee_model(list(data.markets))
    rows = []
    for index, session in enumerate(split_into_sessions(data, session_hours * 3600.0)):
        events = build_event_stream(session)
        for market in session.markets:
            for name, overrides in VARIANTS.items():
                strategy = InventorySkewMarketMaker(MarketMakerConfig(market_id=market, **overrides))
                config = BacktestConfig(fees=fees, fill_model=FillModel(queue_position="back"))
                result = BacktestEngine(session, strategy, config, events=events).run()
                summary = performance_summary(result)
                rows.append({"variant": name, "game": game, "market": market, "session": index,
                             **{k: summary[k] for k in ("net_pnl", "gross_pnl", "fees_paid", "liquidation_pnl",
                                                        "n_fills", "contracts_traded")}})
    return rows


def table(runs: pd.DataFrame) -> pd.DataFrame:
    per_market = runs.groupby(["variant", "game", "market"])[["net_pnl", "fees_paid", "contracts_traded"]].sum()
    out = per_market.groupby("variant").agg(
        net_pnl=("net_pnl", "sum"), fees=("fees_paid", "sum"), contracts=("contracts_traded", "sum"),
        markets=("net_pnl", "size"), profitable=("net_pnl", lambda s: int((s > 0).sum())))
    per_game = per_market.groupby(["variant", "game"])["net_pnl"].sum()
    out["profitable_games"] = per_game.groupby("variant").apply(lambda s: f"{int((s > 0).sum())}/{len(s)}")
    out["cents_per_contract"] = 100 * out["net_pnl"] / out["contracts"].where(out["contracts"] > 0)
    out["profitable_markets"] = out["profitable"].astype(str) + "/" + out["markets"].astype(str)
    return out.drop(columns=["markets", "profitable"]).reindex(VARIANTS)


def to_markdown(frame: pd.DataFrame) -> str:
    lines = ["| Variant | Net P&L $ | Fees $ | Contracts | Cents/contract | Profitable games | Profitable markets |",
             "|---|---|---|---|---|---|---|"]
    for name, r in frame.iterrows():
        lines.append(f"| {name} | {r.net_pnl:+.2f} | {r.fees:.2f} | {r.contracts:,.0f} | "
                     f"{r.cents_per_contract:+.2f} | {r.profitable_games} | {r.profitable_markets} |")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--train", nargs="+", default=TRAIN)
    parser.add_argument("--test", nargs="+", default=TEST)
    parser.add_argument("--session-hours", type=float, default=2.5)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--only", choices=["train", "test", "both"], default="both")
    args = parser.parse_args()

    games = {"train": args.train, "test": args.test}
    wanted = ["train", "test"] if args.only == "both" else [args.only]
    out = ROOT / "results" / "mm_lab"
    out.mkdir(parents=True, exist_ok=True)
    report = ["# Market-maker lab", "",
              "Real fees per series, back-of-queue fills, 0.5 s latency, 2.5-hour sessions, every market traded.", ""]
    all_runs = []
    for part in wanted:
        with ProcessPoolExecutor(args.workers) as pool:
            results = pool.map(run_game, games[part], [args.session_hours] * len(games[part]))
            runs = pd.DataFrame([row for rows in results for row in rows]).assign(part=part)
        all_runs.append(runs)
        frame = table(runs)
        report += [f"## {part.title()} games ({len(games[part])}): {', '.join(games[part])}", "", to_markdown(frame), ""]
        print(f"\n{part.upper()}\n{frame.to_string(float_format=lambda v: f'{v:.2f}')}", flush=True)
    pd.concat(all_runs).to_csv(out / "runs.csv", index=False)
    (out / "report.md").write_text("\n".join(report))
    (out / "variants.json").write_text(json.dumps(VARIANTS, indent=2))


if __name__ == "__main__":
    main()
