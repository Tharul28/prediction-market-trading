"""Exchange fee model.

Defaults follow Kalshi's published fee schedule (checked November 2026):

* taker fee = round up(0.07 x C x P x (1 - P)) to the next cent,
* maker fee = round up(0.0175 x C x P x (1 - P)) on series that charge maker
  fees (many Kalshi markets charge makers nothing),

where C is the number of contracts and P the price in dollars. The fee is
largest at P = 0.50 (1.75 cents per contract for takers) and small near 0 or 1.

This project charges the maker fee by default, which is the conservative
choice. Set ``maker_rate=0.0`` to model a market with free maker orders.
Polymarket's 2026 fee formula has the same ``rate x C x P x (1 - P)`` shape,
so changing the two rates is enough to approximate it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from backtest.orders import Liquidity
from orderbook.book import PAYOUT_CENTS


@dataclass(frozen=True)
class FeeModel:
    """Fees per fill, switchable on and off.

    Attributes:
        enabled: If False, every fee is zero.
        taker_rate: Multiplier in the taker formula.
        maker_rate: Multiplier in the maker formula.
    """

    enabled: bool = True
    taker_rate: float = 0.07
    maker_rate: float = 0.0175

    def fee(self, price_cents: int, size: float, liquidity: Liquidity) -> float:
        """Fee in dollars for one fill, rounded up to the next cent.

        Kalshi rounds per order; this model rounds per fill, which can only
        charge slightly more, never less.

        Args:
            price_cents: Fill price in cents.
            size: Contracts filled.
            liquidity: MAKER or TAKER.

        Returns:
            Fee in dollars (0.0 if fees are disabled).
        """
        if not self.enabled or size <= 0:
            return 0.0
        rate = self.taker_rate if liquidity is Liquidity.TAKER else self.maker_rate
        price = price_cents / PAYOUT_CENTS
        raw_fee_dollars = rate * size * price * (1.0 - price)
        # Subtract a tiny epsilon so that exact cent amounts are not pushed up by float error.
        return math.ceil(raw_fee_dollars * 100.0 - 1e-9) / 100.0

    def fee_per_contract_cents(self, price_cents: int, size: float, liquidity: Liquidity) -> float:
        """Rounded fee for ``size`` contracts expressed in cents per contract.

        Strategies use this to decide whether an opportunity survives fees.
        """
        if size <= 0:
            return 0.0
        return 100.0 * self.fee(price_cents, size, liquidity) / size
