"""Paper account: USD cash, BTC holdings, fees. No trading logic lives here.

Fee convention (Phase 2, Section 3): buying notional N costs N * (1 + f) in USD;
selling q BTC at price p returns q * p * (1 - f) in USD. f = 0.1%.
"""
from __future__ import annotations

from dataclasses import dataclass

FEE_RATE = 0.001  # 0.1% on every buy and every sell (README)
_CASH_TOLERANCE = 1e-9  # float rounding allowance when spending all available cash


class InsufficientFunds(Exception):
    pass


class InsufficientBTC(Exception):
    pass


@dataclass(frozen=True)
class Fill:
    side: str        # "buy" or "sell"
    price: float     # USD per BTC actually paid / received before fee
    qty: float       # BTC
    notional: float  # qty * price, USD
    fee: float       # USD


class Account:
    def __init__(self, cash: float) -> None:
        if cash <= 0:
            raise ValueError("starting cash must be positive")
        self.cash = float(cash)
        self.btc = 0.0
        self.fees_paid = 0.0

    def max_buy_notional(self) -> float:
        """Largest notional affordable including the buy fee: cash / (1 + f)."""
        return self.cash / (1 + FEE_RATE)

    def buy(self, notional: float, price: float) -> Fill:
        if notional <= 0 or price <= 0:
            raise ValueError("notional and price must be positive")
        fee = notional * FEE_RATE
        cost = notional + fee
        if cost > self.cash:
            if cost - self.cash > _CASH_TOLERANCE * max(1.0, self.cash):
                raise InsufficientFunds(f"cost {cost:.6f} > cash {self.cash:.6f}")
            cost = self.cash  # spending all cash; absorb a sub-nano-dollar rounding difference
        qty = notional / price
        self.cash -= cost
        self.btc += qty
        self.fees_paid += fee
        return Fill("buy", price, qty, notional, fee)

    def sell(self, qty: float, price: float) -> Fill:
        if qty <= 0 or price <= 0:
            raise ValueError("qty and price must be positive")
        if qty > self.btc * (1 + 1e-12):
            raise InsufficientBTC(f"selling {qty} BTC but holding {self.btc}")
        notional = qty * price
        fee = notional * FEE_RATE
        self.cash += notional - fee
        self.btc = max(0.0, self.btc - qty)
        self.fees_paid += fee
        return Fill("sell", price, qty, notional, fee)

    def sell_all(self, price: float) -> Fill:
        fill = self.sell(self.btc, price)
        self.btc = 0.0  # exact zero: the whole holding was sold
        return fill
