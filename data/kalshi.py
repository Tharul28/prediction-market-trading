"""Fetch and record real order book data from Kalshi's public API.

Kalshi's market data endpoints (events, markets, order books, trades) are
public: no account or API key is needed to read them. This module:

* parses Kalshi's order book format into this project's bid/ask format,
* parses public trades into the project's trade format,
* runs a long-lived, resumable **collector** that polls the order book and
  recent trades of a list of markets every N seconds and appends them to
  CSV files in the project's standard format,
* fills in settlement results once markets resolve.

Example (run from the project root, on your own machine)::

    python -m data.kalshi list-events --limit 20
    python -m data.kalshi collect --event KXEXAMPLE-26DEC31 --interval 5 --out data/raw/example
    python -m data.kalshi collect --markets TICKER1 TICKER2 --interval 5 --out data/raw/mine
    python -m data.kalshi settle --out data/raw/example     # after the markets resolve
    python scripts/run_all.py --data data/raw/example --compare

The collector stops by itself once Kalshi reports every market as closed,
then saves the settlement when the results are published. Stop it early with
Ctrl+C. Run the same ``collect`` command again (the market list can be
omitted on restart) and it carries on where it stopped. ``--start-at`` makes
it wait until a given time first, e.g. 30 minutes before kick-off. Rate
limiting (HTTP 429) and network errors are retried with backoff.

Kalshi order book quirk
-----------------------
Kalshi returns **bids only**: bids for YES and bids for NO. A bid for NO at
price ``p`` is the same thing as an offer to sell YES at ``100 - p``,
because holding one YES and one NO always pays exactly $1. So the YES asks
are derived as ``100 - (NO bid price)``.

Note: this code was written against Kalshi's documentation (checked
November 2026) but could not be run against the live API from the
environment it was written in. Parsing is unit tested on documented
example payloads; run ``list-events`` first to check your connection.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from data.schema import BOOK_COLUMNS, TRADE_COLUMNS, books_to_rows
from orderbook.book import PAYOUT_CENTS, OrderBookSnapshot, PriceLevel

DEFAULT_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
ALTERNATIVE_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"


# --------------------------------------------------------------------------- parsing
def dollars_to_cents(value: str | float) -> int:
    """Convert a Kalshi dollar amount such as "0.4200" to integer cents (42)."""
    return int(round(float(value) * 100))


def parse_orderbook(payload: dict[str, Any]) -> tuple[list[PriceLevel], list[PriceLevel]]:
    """Turn a Kalshi ``GET /markets/{ticker}/orderbook`` response into YES bids and asks.

    Handles the current fixed-point format (``orderbook_fp`` with
    ``yes_dollars`` / ``no_dollars`` arrays of ``[price_dollars, count]``
    strings) and the older integer-cents format (``orderbook`` with ``yes``
    / ``no`` arrays of ``[price_cents, count]``).

    Args:
        payload: Parsed JSON response.

    Returns:
        (yes_bids, yes_asks), each a list of (price_cents, size), best first.
    """
    if "orderbook_fp" in payload:
        book = payload["orderbook_fp"] or {}
        yes_raw = book.get("yes_dollars") or []
        no_raw = book.get("no_dollars") or []
        yes = [(dollars_to_cents(price), float(size)) for price, size in yes_raw]
        no = [(dollars_to_cents(price), float(size)) for price, size in no_raw]
    elif "orderbook" in payload:
        book = payload["orderbook"] or {}
        yes = [(int(price), float(size)) for price, size in (book.get("yes") or [])]
        no = [(int(price), float(size)) for price, size in (book.get("no") or [])]
    else:
        raise ValueError("Unrecognised Kalshi order book payload: expected 'orderbook_fp' or 'orderbook'")

    yes_bids = sorted(((p, s) for p, s in yes if s > 0), key=lambda level: -level[0])
    # A NO bid at p is a YES ask at 100 - p.
    yes_asks = sorted(((PAYOUT_CENTS - p, s) for p, s in no if s > 0), key=lambda level: level[0])
    return yes_bids, yes_asks


def _first_present(record: dict[str, Any], keys: list[str]) -> Any:
    """Return the value of the first key that exists and is not None."""
    for key in keys:
        if record.get(key) is not None:
            return record[key]
    return None


def parse_trades(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn a Kalshi ``GET /markets/trades`` response into rows of the trade table.

    The taker's side tells us who was the aggressor. If the taker bought YES
    they lifted a YES ask (aggressor "buy"). If the taker bought NO they
    effectively sold YES into a YES bid (aggressor "sell"). The trade price
    is always expressed as the YES price.

    Args:
        payload: Parsed JSON response with a ``trades`` list.

    Returns:
        List of dicts with keys trade_id, timestamp, market_id, price, size,
        aggressor.
    """
    rows = []
    for trade in payload.get("trades") or []:
        yes_price_dollars = trade.get("yes_price_dollars")
        if yes_price_dollars is not None:
            price = dollars_to_cents(yes_price_dollars)
        elif trade.get("yes_price") is not None:
            price = int(trade["yes_price"])
        else:
            continue
        size = _first_present(trade, ["count_fp", "count"])
        taker = _first_present(trade, ["taker_outcome_side", "taker_side"])
        if size is None or taker not in ("yes", "no"):
            continue
        rows.append({
            "trade_id": str(trade.get("trade_id", "")),
            "timestamp": pd.Timestamp(trade["created_time"]).timestamp(),
            "market_id": trade["ticker"],
            "price": price,
            "size": float(size),
            "aggressor": "buy" if taker == "yes" else "sell",
        })
    return rows


def parse_settlement(markets: list[dict[str, Any]]) -> dict[str, int] | None:
    """Return {ticker: 100 or 0} if every market has a yes/no result, else None."""
    settlement = {}
    for market in markets:
        result = market.get("result")
        if result == "yes":
            settlement[market["ticker"]] = 100
        elif result == "no":
            settlement[market["ticker"]] = 0
        else:
            return None
    return settlement


# --------------------------------------------------------------------------- client
RETRY_STATUS_CODES = {429, 500, 502, 503, 504}


def retry_delay_s(attempt: int, retry_after: str | None, base_s: float = 1.0, cap_s: float = 30.0) -> float:
    """Seconds to wait before retry number ``attempt`` (0-based).

    Uses the server's ``Retry-After`` header when it gives a number of
    seconds, otherwise exponential backoff: 1, 2, 4, 8, ... seconds, capped.
    """
    if retry_after:
        try:
            return min(max(float(retry_after), 0.0), cap_s)
        except ValueError:
            pass  # an HTTP date instead of seconds: fall back to backoff
    return min(base_s * 2 ** attempt, cap_s)


@dataclass
class KalshiClient:
    """Minimal read-only client for Kalshi's public market data endpoints.

    Attributes:
        base_url: API root. If the default stops working, try
            ``ALTERNATIVE_BASE_URL``.
        timeout_s: HTTP timeout per request.
        pause_s: Pause after every request, to stay well inside rate limits.
        max_retries: Retries for rate limiting (HTTP 429), server errors and
            network errors, with backoff between them, before giving up.
        http_get: Function with the signature of ``requests.get``. Only
            replaced in tests.
        sleep: Function with the signature of ``time.sleep``. Only replaced
            in tests.
    """

    base_url: str = DEFAULT_BASE_URL
    timeout_s: float = 10.0
    pause_s: float = 0.1
    max_retries: int = 5
    http_get: Callable[..., Any] | None = None
    sleep: Callable[[float], None] = time.sleep
    retries_used: int = 0  # running count, reported by the collector

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET a path and return the parsed JSON.

        Rate limiting (429), server errors (5xx) and network errors are
        retried with backoff; other HTTP errors are raised at once.
        """
        import requests  # imported here so the rest of the project runs without it

        http_get = self.http_get or requests.get
        attempt = 0
        while True:
            try:
                response = http_get(f"{self.base_url}{path}", params=params, timeout=self.timeout_s)
            except (requests.ConnectionError, requests.Timeout):
                if attempt >= self.max_retries:
                    raise
                self.sleep(retry_delay_s(attempt, None))
            else:
                if response.status_code not in RETRY_STATUS_CODES or attempt >= self.max_retries:
                    response.raise_for_status()
                    self.sleep(self.pause_s)
                    return response.json()
                self.sleep(retry_delay_s(attempt, response.headers.get("Retry-After")))
            attempt += 1
            self.retries_used += 1

    def list_events(self, status: str = "open", limit: int = 20) -> list[dict[str, Any]]:
        """Return up to ``limit`` events with the given status."""
        return self._get("/events", {"status": status, "limit": limit}).get("events", [])

    def get_event(self, event_ticker: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Return (event, markets) for an event ticker."""
        payload = self._get(f"/events/{event_ticker}", {"with_nested_markets": "true"})
        event = payload["event"]
        markets = event.get("markets") or payload.get("markets") or []
        return event, markets

    def get_market(self, ticker: str) -> dict[str, Any]:
        """Return one market's details (including ``status`` and ``result``)."""
        return self._get(f"/markets/{ticker}")["market"]

    def get_orderbook(self, ticker: str, depth: int = 10) -> tuple[list[PriceLevel], list[PriceLevel]]:
        """Return (yes_bids, yes_asks) for a market ticker."""
        return parse_orderbook(self._get(f"/markets/{ticker}/orderbook", {"depth": depth}))

    def get_trades(self, ticker: str, min_ts: int, max_ts: int, page_size: int = 1000) -> list[dict[str, Any]]:
        """Return all public trades for a ticker between two Unix timestamps."""
        rows: list[dict[str, Any]] = []
        cursor = ""
        while True:
            params: dict[str, Any] = {"ticker": ticker, "min_ts": min_ts, "max_ts": max_ts, "limit": page_size}
            if cursor:
                params["cursor"] = cursor
            payload = self._get("/markets/trades", params)
            rows.extend(parse_trades(payload))
            cursor = payload.get("cursor") or ""
            if not cursor:
                return rows


# --------------------------------------------------------------------------- collector
COLLECTED_TRADE_COLUMNS = TRADE_COLUMNS + ["trade_id"]


def repair_partial_last_line(path: Path) -> bool:
    """If a crash left a half-written last line, cut the file back to the last newline.

    Returns:
        True if the file was changed.
    """
    if not path.exists() or path.stat().st_size == 0:
        return False
    with open(path, "rb+") as handle:
        handle.seek(-1, os.SEEK_END)
        if handle.read(1) == b"\n":
            return False
        handle.seek(0)
        content = handle.read()
        cut = content.rfind(b"\n") + 1  # 0 if there is no complete line at all
        handle.seek(cut)
        handle.truncate()
    return True


def _write_json_atomically(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON to a temporary file and rename it, so a crash never leaves half a file."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2))
    os.replace(temporary, path)


@dataclass
class CollectorConfig:
    """Settings for :class:`KalshiCollector`.

    Attributes:
        out_dir: Directory for ``books.csv``, ``trades.csv``, ``meta.json``
            and ``collector_state.json``. Reusing a directory resumes it.
        tickers: Markets to collect. May be empty on restart, in which case
            the list saved in ``meta.json`` is used.
        interval_s: Seconds between polls (each poll covers every market).
        depth: Price levels requested per side.
        trade_overlap_s: Each trade request starts this many seconds before
            the newest trade already saved, so nothing is missed at the
            boundary. Duplicates are removed by trade id.
        mutually_exclusive: Whether at most one of the markets can resolve YES.
        exhaustive: Whether exactly one must resolve YES (check the rules).
        description: Free text saved in ``meta.json``.
        max_polls: Stop after this many polls (None = run until Ctrl+C).
        status_check_s: How often to ask Kalshi whether the markets are still
            open. Once every market has closed the collector stops by itself.
            None turns the check off.
        settle_wait_s: After stopping because the markets closed, keep
            trying to save the settlement for up to this many seconds.
    """

    out_dir: Path
    tickers: list[str] = field(default_factory=list)
    interval_s: float = 5.0
    depth: int = 10
    trade_overlap_s: float = 60.0
    mutually_exclusive: bool = False
    exhaustive: bool = False
    description: str = ""
    max_polls: int | None = None
    status_check_s: float | None = 60.0
    settle_wait_s: float = 1800.0
    market_info: dict[str, dict[str, Any]] = field(default_factory=dict)


# Kalshi market statuses after which no more trading happens. "inactive" is
# left out on purpose: it can be a temporary pause.
CLOSED_STATUSES = {"closed", "determined", "disputed", "amended", "finalized", "settled"}


class KalshiCollector:
    """Polls order books and trades for a fixed list of markets and appends them to disk.

    Files written in ``out_dir`` (the standard format read by ``data.storage``):

    * ``books.csv``: one row per price level per snapshot, appended each poll.
    * ``trades.csv``: new public trades, appended each poll, with ``trade_id``.
    * ``meta.json``: market list and flags (merged with any existing file).
    * ``collector_state.json``: newest trade time per market, for resuming.

    Crash safety: rows are appended and flushed after every poll, so at most
    the poll in progress is lost. On start-up a half-written last line is cut
    off, trade ids already on disk are loaded so no trade is written twice,
    and the trade cursor is restored from the state file.
    """

    def __init__(self, client: KalshiClient, config: CollectorConfig,
                 clock: Callable[[], float] = time.time) -> None:
        """Args:
            client: A :class:`KalshiClient` (or anything with the same
                ``get_orderbook`` and ``get_trades`` methods, e.g. a test replay).
            config: Collector settings.
            clock: Returns the current Unix time. Only replaced in tests.
        """
        self.client = client
        self.config = config
        self.clock = clock
        self.out_dir = Path(config.out_dir)
        self.books_path = self.out_dir / "books.csv"
        self.trades_path = self.out_dir / "trades.csv"
        self.meta_path = self.out_dir / "meta.json"
        self.state_path = self.out_dir / "collector_state.json"
        self.seen_trade_ids: set[str] = set()
        self.last_trade_ts: dict[str, float] = {}
        self.tickers: list[str] = []
        self._prepare()

    # ------------------------------------------------------------------ start-up
    def _prepare(self) -> None:
        """Create or resume the output directory."""
        self.out_dir.mkdir(parents=True, exist_ok=True)
        for path in (self.books_path, self.trades_path):
            if repair_partial_last_line(path):
                print(f"Repaired a half-written last line in {path.name}")

        meta: dict[str, Any] = json.loads(self.meta_path.read_text()) if self.meta_path.exists() else {}
        saved = list(meta.get("markets", []))
        self.tickers = list(self.config.tickers) or saved
        if not self.tickers:
            raise ValueError("No markets given and none saved in meta.json")
        meta["markets"] = saved + [t for t in self.tickers if t not in saved]
        if self.config.tickers:  # markets given on the command line: use the flags given with them
            meta["mutually_exclusive"] = self.config.mutually_exclusive
            meta["exhaustive"] = self.config.exhaustive
        else:  # plain restart: keep what was saved
            meta.setdefault("mutually_exclusive", self.config.mutually_exclusive)
            meta.setdefault("exhaustive", self.config.exhaustive)
        meta.setdefault("settlement", None)
        meta["description"] = self.config.description or meta.get("description", "Kalshi collector")
        # Schedule as known when collection started. Never overwritten on restart:
        # Kalshi later moves close_time to the actual close, which would leak the future.
        info = meta.setdefault("market_info", {})
        for ticker, details in self.config.market_info.items():
            info.setdefault(ticker, details)
        _write_json_atomically(self.meta_path, meta)

        if self.trades_path.exists() and self.trades_path.stat().st_size > 0:
            existing = pd.read_csv(self.trades_path, usecols=["trade_id", "timestamp", "market_id"],
                                   dtype={"trade_id": str}, on_bad_lines="skip")
            self.seen_trade_ids = set(existing["trade_id"].dropna())
            self.last_trade_ts = existing.groupby("market_id")["timestamp"].max().to_dict()
        if self.state_path.exists():
            state = json.loads(self.state_path.read_text())
            for ticker, ts in state.get("last_trade_ts", {}).items():
                self.last_trade_ts[ticker] = max(ts, self.last_trade_ts.get(ticker, ts))

    # ------------------------------------------------------------------ writing
    @staticmethod
    def _append_rows(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
        """Append rows to a CSV, writing the header only if the file is new, then flush to disk."""
        if not rows:
            return
        is_new = not path.exists() or path.stat().st_size == 0
        with open(path, "a", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            if is_new:
                writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())

    # ------------------------------------------------------------------ polling
    def poll_once(self) -> tuple[list[OrderBookSnapshot], list[dict[str, Any]]]:
        """Take one order book snapshot and fetch new trades for every market.

        A failed request for one market is logged and skipped; it does not stop
        the others.

        Returns:
            (snapshots taken, new trades) for this poll. Both are also
            appended to disk before this returns.
        """
        snapshots: list[OrderBookSnapshot] = []
        book_rows: list[dict[str, Any]] = []
        new_trades: list[dict[str, Any]] = []
        for ticker in self.tickers:
            try:
                bids, asks = self.client.get_orderbook(ticker, self.config.depth)
                snapshot = OrderBookSnapshot.from_levels(self.clock(), ticker, bids, asks)
                snapshots.append(snapshot)
                book_rows.extend(books_to_rows(snapshot))
            except Exception as error:  # network errors must not kill a long recording
                print(f"  {ticker}: order book request failed ({error})")
            try:
                now = self.clock()
                since = self.last_trade_ts.get(ticker, now - self.config.trade_overlap_s)
                trades = self.client.get_trades(ticker, int(since - self.config.trade_overlap_s), int(now) + 1)
                for trade in sorted(trades, key=lambda t: t["timestamp"]):
                    if trade["trade_id"] and trade["trade_id"] in self.seen_trade_ids:
                        continue
                    self.seen_trade_ids.add(trade["trade_id"])
                    new_trades.append(trade)
                    self.last_trade_ts[ticker] = max(self.last_trade_ts.get(ticker, 0.0), trade["timestamp"])
            except Exception as error:
                print(f"  {ticker}: trades request failed ({error})")

        self._append_rows(self.books_path, BOOK_COLUMNS, book_rows)
        self._append_rows(self.trades_path, COLLECTED_TRADE_COLUMNS, new_trades)
        _write_json_atomically(self.state_path, {"last_trade_ts": self.last_trade_ts, "updated": self.clock()})
        return snapshots, new_trades

    def markets_closed(self) -> bool:
        """True if Kalshi reports every collected market as closed or settled.

        A failed status request counts as "still open", so a network problem
        never stops a recording early.
        """
        try:
            statuses = [self.client.get_market(ticker).get("status") for ticker in self.tickers]
        except Exception as error:
            print(f"  market status check failed ({error})")
            return False
        return all(status in CLOSED_STATUSES for status in statuses)

    def run(self) -> str:
        """Poll every ``interval_s`` seconds until the markets close, Ctrl+C or ``max_polls``.

        Returns:
            Why it stopped: "closed", "interrupted" or "max_polls".
        """
        print(f"Collecting {len(self.tickers)} markets every {self.config.interval_s}s into {self.out_dir}")
        polls = 0
        last_status_check = self.clock()
        retries_reported = 0
        try:
            while self.config.max_polls is None or polls < self.config.max_polls:
                started = time.time()
                snapshots, trades = self.poll_once()
                polls += 1
                if polls % 12 == 1:
                    retries = getattr(self.client, "retries_used", 0)
                    note = f", {retries - retries_reported} rate-limit/network retries" if retries > retries_reported else ""
                    retries_reported = retries
                    print(f"  poll {polls}: {len(snapshots)} snapshots, {len(trades)} new trades "
                          f"({len(self.seen_trade_ids)} trades saved in total{note})")
                check_every = self.config.status_check_s
                if check_every is not None and self.clock() - last_status_check >= check_every:
                    last_status_check = self.clock()
                    if self.markets_closed():
                        print(f"All markets have closed after {polls} polls; stopping.")
                        self._settle_when_ready()
                        return "closed"
                time.sleep(max(0.0, self.config.interval_s - (time.time() - started)))
        except KeyboardInterrupt:
            print("Stopped. Run the same command again to resume.")
            return "interrupted"
        return "max_polls"

    def _settle_when_ready(self) -> None:
        """Try to save the settlement, retrying for up to ``settle_wait_s`` seconds."""
        deadline = time.time() + self.config.settle_wait_s
        while True:
            try:
                if settle(self.client, self.out_dir) is not None:
                    return
            except Exception as error:
                print(f"  settlement lookup failed ({error})")
            if time.time() >= deadline:
                print(f"Gave up waiting for results. Later, run: python -m data.kalshi settle --out {self.out_dir}")
                return
            time.sleep(min(60.0, max(0.0, deadline - time.time())))


def settle(client: KalshiClient, out_dir: str | Path) -> dict[str, int] | None:
    """Look up results for the markets in ``out_dir/meta.json`` and save them if all are known."""
    meta_path = Path(out_dir) / "meta.json"
    meta = json.loads(meta_path.read_text())
    markets = [client.get_market(ticker) for ticker in meta["markets"]]
    settlement = parse_settlement(markets)
    if settlement is None:
        pending = [m["ticker"] for m in markets if m.get("result") not in ("yes", "no")]
        print(f"Not all markets have resolved yet: {pending}")
        return None
    meta["settlement"] = settlement
    _write_json_atomically(meta_path, meta)
    print(f"Saved settlement: {settlement}")
    return settlement


def main() -> None:
    """Command line entry point: ``python -m data.kalshi --help``."""
    parser = argparse.ArgumentParser(description="Read-only Kalshi market data tools")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser("list-events", help="List open events")
    list_parser.add_argument("--limit", type=int, default=20)

    collect_parser = sub.add_parser("collect", help="Collect order books and trades; resumes if restarted")
    group = collect_parser.add_mutually_exclusive_group()
    group.add_argument("--markets", nargs="+", default=[], help="Market tickers to collect")
    group.add_argument("--event", help="Collect every active market of this event")
    collect_parser.add_argument("--out", required=True, help="Output directory (reuse it to resume)")
    collect_parser.add_argument("--interval", type=float, default=5.0, help="Seconds between polls")
    collect_parser.add_argument("--depth", type=int, default=10)
    collect_parser.add_argument("--mutually-exclusive", action="store_true",
                                help="Markets are outcomes of which at most one can resolve YES")
    collect_parser.add_argument("--exhaustive", action="store_true",
                                help="Exactly one of the markets must resolve YES (check the event rules)")
    collect_parser.add_argument("--start-at", default=None,
                                help="Wait until this time before collecting, e.g. 2026-11-21T14:30:00Z "
                                     "(no timezone means UTC)")
    collect_parser.add_argument("--no-auto-stop", action="store_true",
                                help="Keep collecting after the markets close (default: stop and settle)")

    settle_parser = sub.add_parser("settle", help="Save market results once they have resolved")
    settle_parser.add_argument("--out", required=True)

    args = parser.parse_args()
    client = KalshiClient(base_url=args.base_url)
    if args.command == "list-events":
        for event in client.list_events(limit=args.limit):
            print(f"{event['event_ticker']:<40} mutually_exclusive={event.get('mutually_exclusive')}  "
                  f"{event.get('title', '')}")
    elif args.command == "settle":
        settle(client, args.out)
    else:
        if args.start_at:
            wait_until(parse_utc(args.start_at))
        tickers = list(args.markets)
        mutually_exclusive = args.mutually_exclusive
        description = ""
        if args.event:
            event, markets = until_online(lambda: client.get_event(args.event))
            tickers = [m["ticker"] for m in markets if m.get("status") in (None, "active", "open")]
            mutually_exclusive = mutually_exclusive or bool(event.get("mutually_exclusive", False))
            description = f"Kalshi event {args.event}: {event.get('title', '')}"
        config = CollectorConfig(out_dir=Path(args.out), tickers=tickers, interval_s=args.interval,
                                 depth=args.depth, mutually_exclusive=mutually_exclusive,
                                 exhaustive=args.exhaustive, description=description,
                                 status_check_s=None if args.no_auto_stop else 60.0,
                                 market_info=fetch_market_info(client, tickers))
        KalshiCollector(client, config).run()


MARKET_INFO_FIELDS = ("title", "open_time", "close_time", "expected_expiration_time", "latest_expiration_time")


def fetch_market_info(client: KalshiClient, tickers: list[str]) -> dict[str, dict[str, Any]]:
    """Each market's title and schedule as published right now (missing markets are skipped)."""
    info = {}
    for ticker in tickers:
        try:
            market = client.get_market(ticker)
        except Exception as error:
            print(f"  could not fetch details for {ticker} ({error})")
            continue
        info[ticker] = {key: market.get(key) for key in MARKET_INFO_FIELDS}
        info[ticker]["recorded_at"] = pd.Timestamp.now(tz="UTC").isoformat()
    return info


def parse_utc(text: str) -> float:
    """Parse an ISO time to a Unix timestamp; a time without a timezone is taken as UTC."""
    stamp = pd.Timestamp(text)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    return stamp.timestamp()


def until_online(request: Callable[[], Any], wait_s: float = 60.0, give_up_s: float = 6 * 3600.0,
                 sleep: Callable[[float], None] = time.sleep) -> Any:
    """Run ``request`` until it gets past network errors, retrying every ``wait_s`` seconds.

    A collector that wakes at its start time while the laptop is offline (lid
    closed, a maintenance wake, no Wi-Fi yet) must wait for the network rather
    than crash. HTTP errors such as 404 are not network errors and are raised.
    """
    import requests

    waited = 0.0
    while True:
        try:
            return request()
        except (requests.ConnectionError, requests.Timeout) as error:
            if waited >= give_up_s:
                raise
            print(f"  network unavailable ({type(error).__name__}); retrying in {wait_s:.0f} s", flush=True)
            sleep(wait_s)
            waited += wait_s


def wait_until(start: float) -> None:
    """Sleep until a Unix time, printing when collection will begin."""
    remaining = start - time.time()
    if remaining <= 0:
        return
    when = pd.Timestamp(start, unit="s", tz="UTC").strftime("%Y-%m-%d %H:%M UTC")
    print(f"Waiting {remaining / 3600:.1f} h until {when} before collecting", flush=True)
    while (remaining := start - time.time()) > 0:
        time.sleep(min(remaining, 300.0))  # short naps so a laptop waking from sleep catches up


if __name__ == "__main__":
    main()
