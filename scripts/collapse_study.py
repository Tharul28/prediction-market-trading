"""Does a collapsing best bid or ask warn of a price jump?

Before Germany's first goal against the Netherlands, the 51c ask lost 95% of
its size, mostly through cancellations, five seconds before buyers swept the
book to 72c. This script checks whether that is a pattern across recordings.

A *collapse* at snapshot ``t``: the best ask (or bid) has the same price as in
the previous snapshot of that market, the previous size was at least
``--min-size`` contracts, and at most ``--keep`` of it is left. Snapshots more
than ``--max-gap`` seconds apart are not compared. An ask collapse predicts an
up move, a bid collapse a down move. Repeats within ``--dedupe`` seconds on the
same side are dropped. Everything here is visible to a bot that polls the book:
the signal uses only the two snapshots, plus trades stamped at or before ``t``
(to split the lost size into traded and cancelled).

Measured:

* **Precision.** After each collapse, the signed mid move at 10/30/60 s, and
  how often the price moves at least ``--big`` cents in the predicted direction
  within 60 s, compared with the same rate at every snapshot (the base rate).
* **Recall and warning time.** For each jump (mid moves at least ``--jump``
  cents within 60 s), whether a collapse in the jump's direction came in the
  60 s before the jump started, and how long before.

Usage (from the project root)::

    python scripts/collapse_study.py                    # every game recording in data/raw/
    python scripts/collapse_study.py --data data/raw/ger_ned data/raw/den_nor

Writes ``results/collapse_study/report.md``, ``collapses.csv`` and ``jumps.csv``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.storage import load_market_data_dir  # noqa: E402
from research.features import snapshot_table  # noqa: E402

HORIZONS_S = (10.0, 30.0, 60.0)
SKIP_PREFIXES = ("cont_",)  # long-dated markets: they barely move


def value_at_or_before(times: np.ndarray, values: np.ndarray, when: np.ndarray) -> np.ndarray:
    index = np.searchsorted(times, when, side="right") - 1
    out = np.full(len(when), np.nan)
    ok = index >= 0
    out[ok] = values[index[ok]]
    return out


def max_move_within(times: np.ndarray, mid: np.ndarray, window_s: float) -> tuple[np.ndarray, np.ndarray]:
    """Largest rise and largest fall of the mid from each snapshot over the next ``window_s`` seconds."""
    end = np.searchsorted(times, times + window_s, side="right")
    up, down = np.zeros(len(times)), np.zeros(len(times))
    for i in range(len(times)):
        path = mid[i:end[i]] - mid[i]
        up[i], down[i] = path.max(), -path.min()
    return up, down


def find_collapses(frame: pd.DataFrame, trades: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    """Collapse events in one market's snapshot table (sorted by time)."""
    t = frame["timestamp"].to_numpy(float)
    rows = []
    trade_t = trades["timestamp"].to_numpy(float)
    for side, direction, aggressor in (("ask", 1, "buy"), ("bid", -1, "sell")):
        price = frame[f"{side}_price"].to_numpy(float)
        size = frame[f"{side}_size"].to_numpy(float)
        hit = np.zeros(len(t), dtype=bool)
        hit[1:] = ((price[1:] == price[:-1]) & (size[:-1] >= args.min_size)
                   & (size[1:] <= args.keep * size[:-1]) & (np.diff(t) <= args.max_gap))
        last = -np.inf
        for i in np.flatnonzero(hit):
            if t[i] - last < args.dedupe:
                continue
            last = t[i]
            between = (trade_t > t[i - 1]) & (trade_t <= t[i])
            at_level = between & (trades["price"].to_numpy(float) == price[i]) & \
                (trades["aggressor"].to_numpy() == aggressor)
            lost = size[i - 1] - size[i]
            traded = float(trades["size"].to_numpy(float)[at_level].sum())
            rows.append({"index": i, "timestamp": t[i], "side": side, "direction": direction,
                         "price": price[i], "size_before": size[i - 1], "size_after": size[i],
                         "cancelled_share": max(0.0, 1.0 - traded / lost)})
    return pd.DataFrame(rows)


def find_market_jumps(t: np.ndarray, mid: np.ndarray, jump: float, merge_gap_s: float = 180.0) -> list[dict]:
    """Jump episodes in one market: the mid moves ``jump`` cents within 60 s.

    The onset is the first snapshot where the mid is 2+ cents away from where it
    was 60 s before the episode was first flagged.
    """
    past = value_at_or_before(t, mid, t - 60.0)
    flagged = np.flatnonzero(np.abs(mid - past) >= jump)
    episodes, current = [], []
    for i in flagged:
        if current and t[i] - t[current[-1]] > merge_gap_s:
            episodes.append(current)
            current = []
        current.append(i)
    if current:
        episodes.append(current)
    out = []
    for episode in episodes:
        first = episode[0]
        anchor_time = t[first] - 60.0
        anchor = value_at_or_before(t, mid, np.array([anchor_time]))[0]
        if np.isnan(anchor):
            continue
        direction = 1 if mid[first] > anchor else -1
        window = np.flatnonzero((t >= anchor_time) & (t <= t[first]))
        moved = window[np.abs(mid[window] - anchor) >= 2.0]
        onset = t[moved[0]] if len(moved) else t[first]
        out.append({"onset": onset, "direction": direction, "from": anchor,
                    "to": mid[episode[-1]], "move": abs(mid[episode[-1]] - anchor)})
    return out


def study(directory: Path, args: argparse.Namespace) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    data = load_market_data_dir(directory)
    snaps = snapshot_table(data.books)
    collapses, jumps, base = [], [], []
    for market, frame in snaps.groupby("market_id"):
        frame = frame.sort_values("timestamp").reset_index(drop=True)
        t = frame["timestamp"].to_numpy(float)
        mid = ((frame["bid_price"] + frame["ask_price"]) / 2).to_numpy(float)
        up, down = max_move_within(t, mid, 60.0)
        known = t + 60.0 <= t[-1]
        base.append({"recording": directory.name, "market": market, "snapshots": int(known.sum()),
                     "p_big_up": float((up[known] >= args.big).mean()),
                     "p_big_down": float((down[known] >= args.big).mean())})

        events = find_collapses(frame, data.trades[data.trades["market_id"] == market], args)
        if not events.empty:
            i = events["index"].to_numpy()
            for h in HORIZONS_S:
                future = value_at_or_before(t, mid, t[i] + h)
                events[f"move_{h:g}s"] = np.where(t[i] + h <= t[-1], (future - mid[i]) * events["direction"], np.nan)
            favourable = np.where(events["direction"] > 0, up[i], down[i])
            events["big_move_60s"] = np.where(known[i], favourable >= args.big, np.nan)
            events["mid"] = mid[i]
            events.insert(0, "market", market)
            events.insert(0, "recording", directory.name)
            collapses.append(events.drop(columns="index"))

        times = events["timestamp"].to_numpy() if not events.empty else np.array([])
        dirs = events["direction"].to_numpy() if not events.empty else np.array([])
        for jump in find_market_jumps(t, mid, args.jump):
            before = (times <= jump["onset"]) & (times >= jump["onset"] - 60.0) & (dirs == jump["direction"])
            jumps.append({"recording": directory.name, "market": market, **jump,
                          "warned": bool(before.any()),
                          "warning_s": float(jump["onset"] - times[before].min()) if before.any() else np.nan})
    return (pd.concat(collapses, ignore_index=True) if collapses else pd.DataFrame(),
            pd.DataFrame(jumps), pd.DataFrame(base))


def report(collapses: pd.DataFrame, jumps: pd.DataFrame, base: pd.DataFrame, args: argparse.Namespace) -> str:
    base_rate = float(np.average((base["p_big_up"] + base["p_big_down"]) / 2, weights=base["snapshots"]))
    lines = [
        "# Does a collapsing touch warn of a jump?", "",
        f"{collapses['recording'].nunique()} recordings, {base['market'].nunique()} markets, "
        f"{int(base['snapshots'].sum()):,} snapshots. Collapse: same best price, previous size >= {args.min_size:g}, "
        f"at most {args.keep:.0%} left, snapshots <= {args.max_gap:g} s apart. Jump: mid moves >= {args.jump:g}c in 60 s.",
        "", "## After a collapse (precision)", "",
        f"Base rate: at a random snapshot, the price moves {args.big:g}c+ in a given direction within 60 s "
        f"{base_rate:.1%} of the time.", "",
        "| Collapses | Cancelled share > 50% | n | Mean signed move 10 s / 30 s / 60 s (c) | "
        f"Moved {args.big:g}c+ the predicted way in 60 s | vs base rate |",
        "|---|---|---|---|---|---|",
    ]
    groups = [("all", collapses), ("mostly cancelled", collapses[collapses["cancelled_share"] > 0.5]),
              ("mostly traded", collapses[collapses["cancelled_share"] <= 0.5])]
    for name, g in groups:
        if g.empty:
            continue
        moves = " / ".join(f"{g[f'move_{h:g}s'].mean():+.2f}" for h in HORIZONS_S)
        hit = g["big_move_60s"].mean()
        lines.append(f"| {name} | {(g['cancelled_share'] > 0.5).mean():.0%} | {len(g)} | {moves} | {hit:.1%} | "
                     f"{hit / base_rate:.1f}x |")
    lines += ["", "By recording:", "", "| Recording | Collapses | Mean signed 60 s move (c) | Big move rate |",
              "|---|---|---|---|"]
    for rec, g in collapses.groupby("recording"):
        lines.append(f"| {rec} | {len(g)} | {g['move_60s'].mean():+.2f} | {g['big_move_60s'].mean():.1%} |")
    warned = jumps["warned"]
    lines += ["", "## Before a jump (recall)", "",
              f"{len(jumps)} jumps of {args.jump:g}c+ across all markets. "
              f"{warned.mean():.0%} had a collapse in the jump's direction in the 60 s before it started"
              + (f"; median warning {jumps.loc[warned, 'warning_s'].median():.0f} s "
                 f"(range {jumps.loc[warned, 'warning_s'].min():.0f}-{jumps.loc[warned, 'warning_s'].max():.0f} s)."
                 if warned.any() else "."), "",
              "| Recording | Jumps | Warned | Median warning (s) |", "|---|---|---|---|"]
    for rec, g in jumps.groupby("recording"):
        w = g["warned"]
        lines.append(f"| {rec} | {len(g)} | {w.sum()} | "
                     f"{g.loc[w, 'warning_s'].median():.0f} |" if w.any() else f"| {rec} | {len(g)} | 0 | - |")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", nargs="+", default=None, help="Recordings (default: game recordings in data/raw)")
    parser.add_argument("--min-size", type=float, default=50.0)
    parser.add_argument("--keep", type=float, default=0.2, help="At most this share of the size left")
    parser.add_argument("--max-gap", type=float, default=15.0)
    parser.add_argument("--dedupe", type=float, default=30.0)
    parser.add_argument("--big", type=float, default=5.0, help="A 'big' move after a collapse, cents")
    parser.add_argument("--jump", type=float, default=8.0, help="A jump, cents within 60 s")
    parser.add_argument("--out", default="results/collapse_study")
    args = parser.parse_args()

    dirs = [Path(d) for d in args.data] if args.data else sorted(
        d for d in Path("data/raw").iterdir()
        if (d / "meta.json").exists() and not d.name.startswith(SKIP_PREFIXES) and (d / "books.csv").exists() and (d / "books.csv").stat().st_size > 0)
    results = [study(d, args) for d in dirs]
    collapses = pd.concat([r[0] for r in results], ignore_index=True)
    jumps = pd.concat([r[1] for r in results], ignore_index=True)
    base = pd.concat([r[2] for r in results], ignore_index=True)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    collapses.to_csv(out / "collapses.csv", index=False)
    jumps.to_csv(out / "jumps.csv", index=False)
    text = report(collapses, jumps, base, args)
    (out / "report.md").write_text(text)
    print(text)


if __name__ == "__main__":
    main()
