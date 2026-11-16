"""Save and load :class:`MarketData` to and from disk.

A saved dataset is a directory containing:

* ``books.csv.gz`` (or ``books.parquet``)
* ``trades.csv.gz`` (or ``trades.parquet``)
* ``truth.csv.gz`` (simulated data only)
* ``meta.json``: market list, mutual exclusivity, exhaustiveness, settlement
  and a description.

The Kalshi collector writes plain ``books.csv`` / ``trades.csv`` (so it can
append to them), which this module also reads. Loading always cleans the
tables and removes duplicate trades by ``trade_id``.

Parquet is smaller and faster but needs the optional ``pyarrow`` package.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from data.schema import TRADE_COLUMNS, MarketData, clean_books, clean_trades, validate_market_data


def _write_table(frame: pd.DataFrame, path_without_suffix: Path, fmt: str) -> None:
    """Write one table as gzipped CSV or Parquet."""
    if fmt == "parquet":
        frame.to_parquet(path_without_suffix.with_suffix(".parquet"), index=False)
    elif fmt == "csv":
        frame.to_csv(path_without_suffix.with_suffix(".csv.gz"), index=False)
    else:
        raise ValueError("fmt must be 'csv' or 'parquet'")


def _read_table(directory: Path, name: str) -> pd.DataFrame | None:
    """Read ``name`` from a directory in whichever supported format exists."""
    parquet_path = directory / f"{name}.parquet"
    if parquet_path.exists():
        return pd.read_parquet(parquet_path)
    for candidate in (directory / f"{name}.csv.gz", directory / f"{name}.csv"):
        if candidate.exists():
            # on_bad_lines="skip" tolerates a line damaged by a crash mid-write.
            return pd.read_csv(candidate, on_bad_lines="skip", dtype={"trade_id": str})
    return None


def save_market_data(data: MarketData, directory: str | Path, fmt: str = "csv") -> Path:
    """Save a dataset to ``directory`` (created if needed) and return the path."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    _write_table(data.books, directory / "books", fmt)
    _write_table(data.trades, directory / "trades", fmt)
    if data.truth is not None:
        _write_table(data.truth, directory / "truth", fmt)
    meta = {
        "markets": data.markets,
        "mutually_exclusive": data.mutually_exclusive,
        "exhaustive": data.exhaustive,
        "settlement": data.settlement,
        "description": data.description,
    }
    (directory / "meta.json").write_text(json.dumps(meta, indent=2))
    return directory


def load_market_data_dir(directory: str | Path) -> MarketData:
    """Load a dataset previously written by :func:`save_market_data`.

    Raises:
        FileNotFoundError: If the directory or its required files are missing.
        ValueError: If the tables do not match the schema.
    """
    directory = Path(directory)
    meta_path = directory / "meta.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"No meta.json in {directory}")
    meta = json.loads(meta_path.read_text())
    books = _read_table(directory, "books")
    trades = _read_table(directory, "trades")
    if books is None:
        raise FileNotFoundError(f"A books table is required in {directory}")
    if trades is None:  # a recording can legitimately have no trades yet
        trades = pd.DataFrame(columns=TRADE_COLUMNS)
    books = clean_books(books)
    trades = clean_trades(trades)
    if "trade_id" in trades.columns:
        has_id = trades["trade_id"].notna() & (trades["trade_id"] != "")
        trades = pd.concat([trades[has_id].drop_duplicates("trade_id"), trades[~has_id]]).sort_values("timestamp")
        trades = trades.reset_index(drop=True)
    data = MarketData(
        books=books,
        trades=trades,
        markets=list(meta["markets"]),
        mutually_exclusive=bool(meta.get("mutually_exclusive", False)),
        exhaustive=bool(meta.get("exhaustive", False)),
        settlement=meta.get("settlement"),
        truth=_read_table(directory, "truth"),
        description=meta.get("description", ""),
    )
    validate_market_data(data)
    return data
