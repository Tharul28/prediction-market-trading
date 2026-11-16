"""Tests for the fee model and P&L accounting."""

import pytest

from backtest.fees import FeeModel
from backtest.orders import Fill, Liquidity, Side
from backtest.portfolio import Portfolio
from tests.helpers import book


def test_kalshi_taker_fee_examples():
    fees = FeeModel()
    # 100 contracts at 50c: 0.07 * 100 * 0.5 * 0.5 = $1.75 exactly.
    assert fees.fee(50, 100, Liquidity.TAKER) == pytest.approx(1.75)
    # 1 contract at 50c: $0.0175, rounded up to the next cent.
    assert fees.fee(50, 1, Liquidity.TAKER) == pytest.approx(0.02)
    # 100 contracts at 90c: 0.07 * 100 * 0.9 * 0.1 = $0.63.
    assert fees.fee(90, 100, Liquidity.TAKER) == pytest.approx(0.63)


def test_maker_fee_and_switching_fees_off():
    assert FeeModel().fee(50, 100, Liquidity.MAKER) == pytest.approx(0.44)  # 0.4375 rounded up
    assert FeeModel(maker_rate=0.0).fee(50, 100, Liquidity.MAKER) == 0.0
    assert FeeModel(enabled=False).fee(50, 100, Liquidity.TAKER) == 0.0


def test_fee_per_contract_includes_rounding():
    fees = FeeModel()
    assert fees.fee_per_contract_cents(50, 1, Liquidity.TAKER) == pytest.approx(2.0)


def _fill(side: Side, price: int, size: float, fee: float = 0.0, market: str = "M") -> Fill:
    return Fill(0.0, 1, market, side, price, size, fee, Liquidity.MAKER)


def test_round_trip_pnl_with_fees():
    portfolio = Portfolio(["M"])
    portfolio.apply_fill(_fill(Side.BUY, 40, 10, fee=0.05))
    portfolio.apply_fill(_fill(Side.SELL, 45, 10, fee=0.05))
    assert portfolio.positions["M"] == 0
    # Gross: 10 contracts * 5 cents = $0.50. Net of $0.10 fees = $0.40.
    assert portfolio.cash == pytest.approx(0.40)
    assert portfolio.fees_paid == pytest.approx(0.10)
    assert portfolio.mark_to_market({"M": 99.0}) == pytest.approx(0.40)  # flat, so the mark is irrelevant


def test_mark_to_market_liquidation_and_settlement_of_a_long():
    portfolio = Portfolio(["M"])
    portfolio.apply_fill(_fill(Side.BUY, 40, 10))
    assert portfolio.mark_to_market({"M": 44.0}) == pytest.approx(0.40)
    books = {"M": book(0, [(43, 50)], [(45, 50)])}
    assert portfolio.liquidation_value(books) == pytest.approx(0.30)  # sell at the 43 bid
    assert portfolio.settlement_value({"M": 100}) == pytest.approx(6.00)
    assert portfolio.settlement_value({"M": 0}) == pytest.approx(-4.00)


def test_short_position_pnl():
    portfolio = Portfolio(["M"])
    portfolio.apply_fill(_fill(Side.SELL, 70, 10))
    assert portfolio.positions["M"] == -10
    assert portfolio.mark_to_market({"M": 60.0}) == pytest.approx(1.00)
    assert portfolio.settlement_value({"M": 0}) == pytest.approx(7.00)
    assert portfolio.settlement_value({"M": 100}) == pytest.approx(-3.00)
