"""Can the order book predict the next 10 or 60 seconds of mid-price moves?

Steps (see ``research/features.py`` for how each feature is built):

1. Build one row per snapshot from every recording given.
2. Split by time (earlier days train, later days test, with an embargo).
3. Baselines: A = predict zero, B = OLS on top-level imbalance alone.
4. All features, standardised inside a pipeline (so the scaler only ever sees
   training folds), then Ridge, Lasso and Elastic Net with the penalty chosen
   by TimeSeriesSplit cross-validation on the training set only.
5. Robustness: the same metrics per market, per day and per horizon, plus how
   often the predicted move is big enough to pay for half the spread and fees.

Usage (from the project root)::

    python scripts/signal_research.py                          # every recording in data/raw/
    python scripts/signal_research.py --data data/raw/cont_sb data/raw/cont_fed

Outputs in ``results/signal/``: ``report.md``, ``metrics.csv``,
``breakdown.csv``, ``coefficients.csv`` and ``models.json`` (the fitted
scaler and coefficients, for plugging into the market maker later).
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from sklearn.exceptions import ConvergenceWarning  # noqa: E402
from sklearn.linear_model import ElasticNet, Lasso, LinearRegression, Ridge  # noqa: E402
from sklearn.metrics import r2_score  # noqa: E402
from sklearn.model_selection import GridSearchCV, TimeSeriesSplit  # noqa: E402
from sklearn.pipeline import Pipeline  # noqa: E402
from sklearn.preprocessing import StandardScaler  # noqa: E402

from data.storage import load_market_data_dir  # noqa: E402
from research.features import FEATURES, HORIZONS_S, build_features, time_split  # noqa: E402

OUT = ROOT / "results" / "signal"
SCHEDULE_CACHE = ROOT / "data" / "raw" / "schedule_cache.json"
MAKER_FEE_RATE = 0.0175  # Kalshi maker fee: 0.0175 x P x (1 - P) per contract, in dollars


# --------------------------------------------------------------------------- data
def scheduled_expiry(directory: Path, markets: list[str], fetch: bool) -> dict[str, float]:
    """Scheduled expiry per market: from meta.json, else a local cache, else (optionally) Kalshi.

    Only ``expected_expiration_time`` is used: it is set when the market is
    created. ``close_time`` is not, because Kalshi rewrites it to the actual
    close when a market ends early (that would leak the future).
    """
    meta = json.loads((directory / "meta.json").read_text())
    info = dict(meta.get("market_info") or {})
    cache = json.loads(SCHEDULE_CACHE.read_text()) if SCHEDULE_CACHE.exists() else {}
    missing = [m for m in markets if m not in info and m not in cache]
    if missing and fetch:
        from data.kalshi import KalshiClient
        client = KalshiClient()
        for market in missing:
            try:
                details = client.get_market(market)
                cache[market] = {"expected_expiration_time": details.get("expected_expiration_time")}
            except Exception as error:
                print(f"  no schedule for {market} ({error})")
        SCHEDULE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        SCHEDULE_CACHE.write_text(json.dumps(cache, indent=2))
    expiry = {}
    for market in markets:
        when = (info.get(market) or cache.get(market) or {}).get("expected_expiration_time")
        if when:
            expiry[market] = pd.Timestamp(when).timestamp()
    return expiry


def load_dataset(directories: list[Path], fetch_schedule: bool) -> pd.DataFrame:
    frames = []
    for directory in directories:
        data = load_market_data_dir(directory)
        expiry = scheduled_expiry(directory, data.markets, fetch_schedule)
        frame = build_features(data, expiry, source=directory.name)
        print(f"  {directory.name}: {len(frame)} snapshots, {data.trades.shape[0]} trades, "
              f"{len(data.markets)} markets")
        frames.append(frame)
    dataset = pd.concat(frames, ignore_index=True).sort_values("timestamp", kind="mergesort").reset_index(drop=True)
    dataset["day"] = pd.to_datetime(dataset["timestamp"], unit="s", utc=True).dt.strftime("%Y-%m-%d")
    return dataset


# --------------------------------------------------------------------------- models
def direction_accuracy(y: np.ndarray, prediction: np.ndarray) -> float:
    """Share of non-zero actual moves whose sign the model got right (zero predictions count as wrong)."""
    moved = y != 0
    if not moved.any():
        return np.nan
    return float((np.sign(prediction[moved]) == np.sign(y[moved])).mean())


def tradable_stats(test: pd.DataFrame, prediction: np.ndarray, y: np.ndarray) -> dict[str, float]:
    """How often |predicted move| beats half the spread plus the maker fee, and what happened then."""
    p = test["mid"].to_numpy() / 100.0
    hurdle = test["spread"].to_numpy() / 2.0 + 100.0 * MAKER_FEE_RATE * p * (1 - p)  # cents
    chosen = np.abs(prediction) > hurdle
    if not chosen.any():
        return {"share_beating_hurdle": 0.0, "realised_move_when_beating": np.nan, "mean_hurdle": float(hurdle.mean())}
    realised = np.sign(prediction[chosen]) * y[chosen]  # move in the predicted direction, cents
    return {"share_beating_hurdle": float(chosen.mean()), "realised_move_when_beating": float(realised.mean()),
            "mean_hurdle": float(hurdle.mean())}


def penalised_models(n_train: int, gap: int) -> dict[str, GridSearchCV]:
    """Ridge, Lasso and Elastic Net, each with its penalty picked by time-ordered CV."""
    cv = TimeSeriesSplit(n_splits=5, gap=gap)
    def search(model, grid):
        pipe = Pipeline([("scale", StandardScaler()), ("model", model)])
        return GridSearchCV(pipe, {f"model__{k}": v for k, v in grid.items()}, cv=cv,
                            scoring="neg_mean_squared_error", n_jobs=-1)
    return {
        "Ridge": search(Ridge(), {"alpha": np.logspace(-2, 6, 17)}),
        "Lasso": search(Lasso(max_iter=5000), {"alpha": np.logspace(-5, 0, 16)}),
        "Elastic Net": search(ElasticNet(max_iter=5000),
                              {"alpha": np.logspace(-5, 0, 11), "l1_ratio": [0.2, 0.5, 0.8]}),
    }


def evaluate(name: str, prediction: np.ndarray, test: pd.DataFrame, y: np.ndarray, horizon: float) -> dict:
    return {"horizon_s": horizon, "model": name, "r2": r2_score(y, prediction),
            "direction_accuracy": direction_accuracy(y, prediction), **tradable_stats(test, prediction, y)}


def run_horizon(dataset: pd.DataFrame, horizon: float, test_share: float) -> tuple[list, list, list, dict]:
    target = f"target_{horizon:g}s"
    rows = dataset.dropna(subset=[target, *[f for f in FEATURES if f != "log_hours_to_expiry"]])
    features = [f for f in FEATURES if rows[f].notna().all()]  # drop expiry if any market lacks it
    train, test, how = time_split(rows, test_share, embargo_s=horizon)
    X_train, y_train = train[features].to_numpy(), train[target].to_numpy()
    X_test, y_test = test[features].to_numpy(), test[target].to_numpy()
    rows_per_second = len(train) / max(1.0, train["timestamp"].max() - train["timestamp"].min())
    gap = int(np.ceil(horizon * rows_per_second))  # rows whose targets overlap the next fold

    print(f"\n[{horizon:g} s] {how}; train {len(train)} rows, test {len(test)} rows; "
          f"{(y_test == 0).mean():.0%} of test targets are exactly zero")
    metrics, predictions = [], {}
    predictions["A: zero"] = np.zeros(len(y_test))
    ols_imb = LinearRegression().fit(train[["imb_1"]], y_train)
    predictions["B: OLS on imbalance"] = ols_imb.predict(test[["imb_1"]])
    ols_all = Pipeline([("scale", StandardScaler()), ("model", LinearRegression())]).fit(X_train, y_train)
    predictions["OLS, all features"] = ols_all.predict(X_test)
    coefficients = [{"horizon_s": horizon, "model": "B: OLS on imbalance", "feature": "imb_1",
                     "coefficient": float(ols_imb.coef_[0])}]
    fitted = {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ConvergenceWarning)
        for name, search in penalised_models(len(train), gap).items():
            search.fit(X_train, y_train)
            predictions[name] = search.predict(X_test)
            model = search.best_estimator_.named_steps["model"]
            scaler = search.best_estimator_.named_steps["scale"]
            fitted[name] = {"features": features, "mean": scaler.mean_.tolist(), "scale": scaler.scale_.tolist(),
                            "coef": model.coef_.tolist(), "intercept": float(model.intercept_),
                            "params": {k.replace("model__", ""): float(v) for k, v in search.best_params_.items()}}
            for feature, coefficient in zip(features, model.coef_):
                coefficients.append({"horizon_s": horizon, "model": name, "feature": feature,
                                     "coefficient": float(coefficient)})
    for name, prediction in predictions.items():
        row = evaluate(name, prediction, test, y_test, horizon)
        row["split"] = how
        metrics.append(row)

    # Robustness: per market and per day of the test set.
    breakdown = []
    for group_by in ("market_id", "day", "source"):
        for key, index in test.groupby(group_by).indices.items():
            if len(index) < 200:
                continue
            for name, prediction in predictions.items():
                breakdown.append({"horizon_s": horizon, "group_by": group_by, "group": key, "model": name,
                                  "rows": len(index), "r2": r2_score(y_test[index], prediction[index]),
                                  "direction_accuracy": direction_accuracy(y_test[index], prediction[index]),
                                  "share_zero_moves": float((y_test[index] == 0).mean())})
    return metrics, breakdown, coefficients, {f"{horizon:g}s": fitted}


# --------------------------------------------------------------------------- report
def report(dataset: pd.DataFrame, metrics: pd.DataFrame, breakdown: pd.DataFrame,
           coefficients: pd.DataFrame) -> str:
    lines = ["# Signal research", "",
             f"{len(dataset)} snapshots from {dataset['source'].nunique()} recordings, "
             f"{dataset['market_id'].nunique()} markets, days {', '.join(sorted(dataset['day'].unique()))}.", ""]
    for horizon, group in metrics.groupby("horizon_s"):
        lines += [f"## {horizon:g}-second horizon", "", f"Split: {group['split'].iloc[0]}.", "",
                  "| Model | Out-of-sample R² | Direction right (non-zero moves) | "
                  "Predictions beating half-spread + fee | Avg move when they do (c) |",
                  "|---|---|---|---|---|"]
        for _, row in group.iterrows():
            accuracy = "n/a" if np.isnan(row["direction_accuracy"]) else f"{row['direction_accuracy']:.1%}"
            realised = "n/a" if np.isnan(row["realised_move_when_beating"]) else f"{row['realised_move_when_beating']:.2f}"
            lines.append(f"| {row['model']} | {row['r2']:.4f} | {accuracy} | "
                         f"{row['share_beating_hurdle']:.2%} | {realised} |")
        lasso = coefficients[(coefficients["horizon_s"] == horizon) & (coefficients["model"] == "Lasso")]
        dropped = lasso.loc[lasso["coefficient"].abs() < 1e-12, "feature"].tolist()
        kept = lasso.loc[lasso["coefficient"].abs() >= 1e-12].sort_values("coefficient", key=np.abs, ascending=False)
        lines += ["", f"Lasso set to zero: {', '.join(dropped) if dropped else 'nothing'}.",
                  "Lasso kept (standardised coefficient, cents per standard deviation): "
                  + (", ".join(f"{r.feature} {r.coefficient:+.3f}" for r in kept.itertuples()) or "nothing") + ".", ""]
        for group_by, title in (("market_id", "market"), ("day", "day"), ("source", "recording")):
            part = breakdown[(breakdown["horizon_s"] == horizon) & (breakdown["group_by"] == group_by)
                             & breakdown["model"].isin(["B: OLS on imbalance", "Ridge", "Lasso"])]
            if part.empty:
                continue
            table = part.pivot_table(index=["group", "rows", "share_zero_moves"], columns="model", values="r2").reset_index()
            lines += [f"Out-of-sample R² by {title}:", "",
                      "| " + " | ".join([title.capitalize(), "Rows", "Zero moves"] + list(table.columns[3:])) + " |",
                      "|" + "---|" * len(table.columns)]
            for _, row in table.iterrows():
                lines.append("| " + " | ".join([str(row["group"]), f"{row['rows']:.0f}", f"{row['share_zero_moves']:.0%}"]
                                              + [f"{row[c]:.4f}" for c in table.columns[3:]]) + " |")
            lines.append("")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", nargs="+", default=None, help="Recording directories (default: all in data/raw)")
    parser.add_argument("--test-share", type=float, default=0.3, help="Share of days (or of time) held out for testing")
    parser.add_argument("--no-fetch-schedule", action="store_true",
                        help="Do not ask Kalshi for scheduled expiry times missing from meta.json")
    args = parser.parse_args()

    directories = [Path(d) for d in args.data] if args.data else sorted(
        p for p in (ROOT / "data" / "raw").iterdir() if (p / "books.csv").exists() and (p / "books.csv").stat().st_size)
    print("Building features:")
    dataset = load_dataset(directories, fetch_schedule=not args.no_fetch_schedule)

    all_metrics, all_breakdown, all_coefficients, models = [], [], [], {}
    for horizon in HORIZONS_S:
        metrics, breakdown, coefficients, fitted = run_horizon(dataset, horizon, args.test_share)
        all_metrics += metrics
        all_breakdown += breakdown
        all_coefficients += coefficients
        models.update(fitted)

    OUT.mkdir(parents=True, exist_ok=True)
    metrics = pd.DataFrame(all_metrics)
    breakdown = pd.DataFrame(all_breakdown)
    coefficients = pd.DataFrame(all_coefficients)
    metrics.to_csv(OUT / "metrics.csv", index=False)
    breakdown.to_csv(OUT / "breakdown.csv", index=False)
    coefficients.to_csv(OUT / "coefficients.csv", index=False)
    (OUT / "models.json").write_text(json.dumps(models, indent=2))
    text = report(dataset, metrics, breakdown, coefficients)
    (OUT / "report.md").write_text(text)
    print("\n" + text)


if __name__ == "__main__":
    main()
