"""Cross-outcome arbitrage in a multi-outcome event.

In an event with mutually exclusive outcomes (e.g. "who wins?"), at most one
YES contract pays 100 cents. That gives two baskets:

* **Buy every outcome** at its best ask. If the outcomes are also
  *exhaustive* (exactly one must win), the basket pays exactly 100 cents, so
  if ``sum(asks) + fees < 100`` the profit is locked in.
* **Sell every outcome** at its best bid (on Kalshi: buy every NO). At most
  one YES pays out, so you owe at most 100 cents. If
  ``sum(bids) - fees > 100`` the profit is locked in. This only needs mutual
  exclusivity, not exhaustiveness.

The strategy checks both baskets on every book update and, if the edge after
taker fees is at least ``min_edge_cents`` per basket, sends one
immediate-or-cancel order per outcome at the prices it saw.

What makes this harder than it looks (the backtest models all of these):

* **Latency.** The orders reach the market after ``latency_s``. By then
  prices may have moved and some legs may not fill.
* **Legging risk.** If only some legs fill, you hold an unhedged position.
  This simple version keeps those leftovers to settlement; it does not try
  to repair them. Leftovers are reported separately, and the strategy stops
  opening new baskets once unhedged exposure reaches
  ``max_unhedged_contracts``.
* **Fees** on every leg, charged on each leg's price.
* **Depth.** Basket size is limited by the smallest best-level size.
"""

from __future__ import annotations

from dataclasses import dataclass

from backtest.fees import FeeModel
from backtest.orders import Liquidity, Side
from orderbook.book import PAYOUT_CENTS
from strategies.base import Action, PlaceTaker, Strategy, StrategyContext


@dataclass(frozen=True)
class ArbConfig:
    """Parameters of :class:`CrossOutcomeArbitrage`.

    Attributes:
        market_ids: Every outcome of the event.
        outcomes_mutually_exclusive: True if at most one outcome can
            resolve YES. Without this neither basket is risk free, so the
            strategy does nothing.
        outcomes_exhaustive: True if exactly one outcome must resolve YES.
            Required for the buy-every-outcome basket.
        min_edge_cents: Minimum profit per basket after fees to act.
        basket_size: Target contracts per leg.
        max_position: Strategy-level position limit per market.
        max_unhedged_contracts: Stop opening baskets once the leftover
            exposure from partly filled baskets reaches this many contracts.
    """

    market_ids: tuple[str, ...]
    outcomes_mutually_exclusive: bool = True
    outcomes_exhaustive: bool = True
    min_edge_cents: float = 0.5
    basket_size: float = 10.0
    max_position: float = 100.0
    max_unhedged_contracts: float = 30.0


def unhedged_contracts(positions: list[float]) -> float:
    """Contracts held that are not part of a complete set of outcomes.

    A complete set (one YES of every outcome, long or short) has a known
    payoff. Anything beyond that is a directional bet on which outcome wins.
    Example: positions [10, 10, 0] contain no complete set, so all 20
    contracts are unhedged; [10, 10, 10] is fully hedged.
    """
    if all(p > 0 for p in positions):
        complete_sets = min(positions)
    elif all(p < 0 for p in positions):
        complete_sets = max(positions)
    else:
        complete_sets = 0.0
    return float(sum(abs(p - complete_sets) for p in positions))


@dataclass
class Opportunity:
    """A detected basket mispricing, logged for analysis."""

    timestamp: float
    kind: str  # "buy_all" or "sell_all"
    raw_edge_cents: float  # before fees
    net_edge_cents: float  # after estimated fees
    size: float
    acted: bool  # False if the edge was too small after fees or the exposure cap was hit


class CrossOutcomeArbitrage(Strategy):
    """Buys or sells a full basket of outcomes when it is mispriced after fees."""

    def __init__(self, config: ArbConfig, fees: FeeModel) -> None:
        """Args:
            config: Strategy parameters.
            fees: The fee model the engine will charge, so the strategy can
                check whether an edge survives fees.
        """
        self.config = config
        self.fees = fees
        self.name = "cross_outcome_arb"
        self.markets = list(config.market_ids)
        self.opportunities: list[Opportunity] = []
        self.baskets_sent = 0
        self._last_logged: tuple[str, float] | None = None

    def _basket_fee_cents(self, prices: list[int], size: float) -> float:
        """Total taker fee per basket (in cents per contract of basket)."""
        return sum(self.fees.fee_per_contract_cents(price, size, Liquidity.TAKER) for price in prices)

    def _log(self, timestamp: float, kind: str, raw: float, net: float, size: float, acted: bool) -> None:
        # Only log each distinct (kind, edge) once in a row, so a mispricing
        # that persists for many snapshots is not counted many times.
        key = (kind, raw)
        if acted or key != self._last_logged:
            self.opportunities.append(Opportunity(timestamp, kind, raw, net, size, acted))
        self._last_logged = key

    def on_book_update(self, market_id: str, context: StrategyContext) -> list[Action]:
        """Check both baskets and send orders if one is profitable after fees."""
        cfg = self.config
        if not cfg.outcomes_mutually_exclusive or len(cfg.market_ids) < 2:
            return []
        # Wait until the previous basket's orders have been resolved.
        if any(order.is_taker for order in context.open_orders):
            return []
        if any(m not in context.books or not context.books[m].is_two_sided for m in cfg.market_ids):
            return []
        books = [context.books[m] for m in cfg.market_ids]
        positions = [context.positions.get(m, 0.0) for m in cfg.market_ids]
        # Too much leftover risk from earlier partial fills: keep logging, stop trading.
        allowed = unhedged_contracts(positions) < cfg.max_unhedged_contracts

        if cfg.outcomes_exhaustive:
            asks = [book.best_ask for book in books]
            raw_edge = PAYOUT_CENTS - sum(asks)
            if raw_edge > 0:
                room = min(cfg.max_position - p for p in positions)
                size = min([cfg.basket_size, room] + [book.best_ask_size for book in books])
                if size >= 1:
                    net_edge = raw_edge - self._basket_fee_cents(asks, size)
                    acted = allowed and net_edge >= cfg.min_edge_cents
                    self._log(context.timestamp, "buy_all", raw_edge, net_edge, size, acted)
                    if acted:
                        return self._send(Side.BUY, asks, size)

        bids = [book.best_bid for book in books]
        raw_edge = sum(bids) - PAYOUT_CENTS
        if raw_edge > 0:
            room = min(cfg.max_position + p for p in positions)
            size = min([cfg.basket_size, room] + [book.best_bid_size for book in books])
            if size >= 1:
                net_edge = raw_edge - self._basket_fee_cents(bids, size)
                acted = allowed and net_edge >= cfg.min_edge_cents
                self._log(context.timestamp, "sell_all", raw_edge, net_edge, size, acted)
                if acted:
                    return self._send(Side.SELL, bids, size)
        return []

    def _send(self, side: Side, prices: list[int], size: float) -> list[Action]:
        """One immediate-or-cancel order per outcome, all tagged with the same basket id."""
        self.baskets_sent += 1
        tag = f"basket-{self.baskets_sent}"
        return [PlaceTaker(market, side, price, size, tag)
                for market, price in zip(self.config.market_ids, prices)]
