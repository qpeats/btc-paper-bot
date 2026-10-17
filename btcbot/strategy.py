"""Strategy C3 (Phase 2, Section 10, frozen) and the minute-by-minute replay.

C3: first, four $10 round trips (buy at a bar's open, sell at the next). Then buy 1/6 of
capital. Add another 1/6 each time price falls 0.5% below the last purchase, up to six
tranches. Sell everything when the high reaches 0.3% above the average cost, then start
over. Stop once at least five trades are complete and the account is more than $0.50 ahead.
Sell anything left at the final close.

Fill rules (Phase 2, Section 9), within each 1-minute bar j:
  1. Time-based exits fill at bar j's open, less 1 bp slippage.
  2. Entries fill at bar j's open, plus 1 bp slippage.
  3. Resting orders of a position opened before bar j are checked against bar j's high/low.
     A take-profit fills only if the high exceeds the target by an extra 1 bp, and fills at
     the target. An averaging-down buy fills at its limit price, never at a better open.
  4. Anything still held is sold at the final bar's close, less 1 bp slippage.

The replay hands the strategy one candle at a time, so no decision can see a later candle.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from .account import Account, Fill
from .data import MINUTE_MS, Candle

# ---- C3 parameters (frozen in Phase 2; do not tune) --------------------------------------
INITIAL_CASH = 10_000.0     # README: starts with 10,000 USD
COUNT_TRADES = 4            # four small round trips made first, to meet the trade count
COUNT_TRADE_USD = 10.0      # notional of each count trade
MAX_TRANCHES = 6            # averaging-down position: up to six tranches
TRANCHE_FRACTION = 1 / 6    # each tranche is 1/6 of capital
STEP = 0.005                # add a tranche when price falls 0.5% below the last purchase
TAKE_PROFIT = 0.003         # sell all when the high reaches 0.3% above average cost
LOCK_MIN_TRADES = 5         # stop after at least five completed trades ...
LOCK_MIN_PROFIT = 0.50      # ... once the account is more than $0.50 ahead
# ---- execution model (Phase 2, Section 9) ------------------------------------------------
SLIPPAGE = 0.0001           # 1 bp against us on every market order
TP_TRADE_THROUGH = 0.0001   # take-profit needs the high 1 bp beyond the target


@dataclass(frozen=True)
class Leg:
    bar: int        # index of the 1-minute bar within the window
    time_ms: int    # when the fill happened (bar open, or bar close for the final sale)
    how: str        # "open", "limit", "take-profit", "close"
    fill: Fill


@dataclass
class Trade:
    number: int
    kind: str                    # "count" or "main"
    buys: List[Leg]
    sell: Leg
    exit_reason: str

    @property
    def qty(self) -> float:
        return sum(b.fill.qty for b in self.buys)

    @property
    def avg_buy_price(self) -> float:
        return sum(b.fill.notional for b in self.buys) / self.qty

    @property
    def fees(self) -> float:
        return sum(b.fill.fee for b in self.buys) + self.sell.fill.fee

    @property
    def cost(self) -> float:
        return sum(b.fill.notional + b.fill.fee for b in self.buys)

    @property
    def proceeds(self) -> float:
        return self.sell.fill.notional - self.sell.fill.fee

    @property
    def pnl(self) -> float:
        return self.proceeds - self.cost


@dataclass
class _Position:
    kind: str
    entry_bar: int
    buys: List[Leg] = field(default_factory=list)
    tranche_notional: float = 0.0
    orders_active_from: int = 0  # first bar on which resting orders are checked

    @property
    def qty(self) -> float:
        return sum(b.fill.qty for b in self.buys)

    @property
    def avg_price(self) -> float:
        return sum(b.fill.notional for b in self.buys) / self.qty

    @property
    def last_buy_price(self) -> float:
        return self.buys[-1].fill.price


class C3Strategy:
    """Event-driven C3. Call on_bar() for each candle in order, then finish() on the last."""

    def __init__(self, account: Account) -> None:
        self.acct = account
        self.initial_cash = account.cash
        self.trades: List[Trade] = []
        self.locked = False
        self.locked_at_bar: Optional[int] = None
        self.count_trades_done = 0
        self.pos: Optional[_Position] = None
        self._last_bar = -1

    # -- helpers ---------------------------------------------------------------------------
    def _buy(self, bar: int, time_ms: int, how: str, notional: float, price: float) -> Leg:
        notional = min(notional, self.acct.max_buy_notional())  # never commit cash twice
        return Leg(bar, time_ms, how, self.acct.buy(notional, price))

    def _close(self, bar: int, time_ms: int, how: str, price: float, reason: str) -> None:
        pos = self.pos
        assert pos is not None
        sell = Leg(bar, time_ms, how, self.acct.sell_all(price))
        self.trades.append(Trade(len(self.trades) + 1, pos.kind, pos.buys, sell, reason))
        if pos.kind == "count":
            self.count_trades_done += 1
        self.pos = None
        if (len(self.trades) >= LOCK_MIN_TRADES
                and self.acct.cash > self.initial_cash + LOCK_MIN_PROFIT):
            self.locked = True
            self.locked_at_bar = bar

    # -- per-bar logic -----------------------------------------------------------------------
    def on_bar(self, bar: int, c: Candle) -> None:
        if bar != self._last_bar + 1:
            raise ValueError("bars must be fed in order, one at a time")
        self._last_bar = bar

        # 1. time-based exit: a count trade bought at an earlier open sells at this open
        if self.pos is not None and self.pos.kind == "count" and self.pos.entry_bar < bar:
            self._close(bar, c.open_time_ms, "open", c.open * (1 - SLIPPAGE), "next open")

        # 2. entries at this open (no price condition: decisions need no data beyond bar-1)
        if self.pos is None and not self.locked:
            if self.count_trades_done < COUNT_TRADES:
                pos = _Position("count", bar)
                pos.buys.append(self._buy(bar, c.open_time_ms, "open",
                                          COUNT_TRADE_USD, c.open * (1 + SLIPPAGE)))
                self.pos = pos
            else:
                pos = _Position("main", bar, orders_active_from=bar + 1)
                pos.tranche_notional = self.acct.cash * TRANCHE_FRACTION  # capital at cycle start
                pos.buys.append(self._buy(bar, c.open_time_ms, "open",
                                          pos.tranche_notional, c.open * (1 + SLIPPAGE)))
                self.pos = pos

        # 3. resting orders of a main position opened before this bar
        pos = self.pos
        if pos is not None and pos.kind == "main" and bar >= pos.orders_active_from:
            add_limit = pos.last_buy_price * (1 - STEP)
            if len(pos.buys) < MAX_TRANCHES and c.low <= add_limit:
                # Averaging-down buy at its limit. If the same bar also reaches the take-profit,
                # the intrabar order is unknown; assume the adverse fill (the buy) came first.
                # The take-profit is re-based on the new average from the next bar.
                pos.buys.append(self._buy(bar, c.open_time_ms, "limit",
                                          pos.tranche_notional, add_limit))
                pos.orders_active_from = bar + 1
            else:
                target = pos.avg_price * (1 + TAKE_PROFIT)
                if c.high > target * (1 + TP_TRADE_THROUGH):
                    self._close(bar, c.open_time_ms, "take-profit", target, "take-profit")

    def finish(self, bar: int, c: Candle) -> None:
        """4. End of window: sell anything still held at the final close."""
        if bar != self._last_bar:
            raise ValueError("finish() must be called with the last bar fed to on_bar()")
        if self.pos is not None:
            self._close(bar, c.open_time_ms + MINUTE_MS, "close",
                        c.close * (1 - SLIPPAGE), "end of window")


# ---------------------------------------------------------------------------- results

@dataclass
class Result:
    start_ms: int
    end_ms: int
    initial_cash: float
    final_cash: float
    final_btc: float
    fees: float
    trades: List[Trade]
    locked_at_bar: Optional[int]
    first_open: float
    last_close: float
    hold_final_cash: float
    hold_fees: float

    @property
    def pnl(self) -> float:
        return self.final_cash - self.initial_cash

    @property
    def pnl_pct(self) -> float:
        return 100 * self.pnl / self.initial_cash

    @property
    def hold_pnl(self) -> float:
        return self.hold_final_cash - self.initial_cash

    @property
    def market_change_pct(self) -> float:
        return 100 * (self.last_close / self.first_open - 1)

    @property
    def check_trades(self) -> bool:
        return len(self.trades) >= LOCK_MIN_TRADES

    @property
    def check_profit(self) -> bool:
        return self.final_cash > self.initial_cash

    @property
    def check_flat(self) -> bool:
        return self.final_btc == 0.0

    @property
    def passed(self) -> bool:
        return self.check_trades and self.check_profit and self.check_flat


def buy_and_hold(candles: Sequence[Candle], cash: float = INITIAL_CASH) -> Account:
    """Comparison only (README): all-in at the first open, out at the last close, same fees
    and slippage as the bot. Computed after the replay; the strategy never sees it."""
    acct = Account(cash)
    acct.buy(acct.max_buy_notional(), candles[0].open * (1 + SLIPPAGE))
    acct.sell_all(candles[-1].close * (1 - SLIPPAGE))
    return acct


def simulate(candles: Sequence[Candle], start_ms: int, end_ms: int,
             cash: float = INITIAL_CASH) -> Result:
    if not candles:
        raise ValueError("no candles to replay")
    acct = Account(cash)
    strat = C3Strategy(acct)
    for i, c in enumerate(candles):  # one candle at a time, in time order
        strat.on_bar(i, c)
    strat.finish(len(candles) - 1, candles[-1])
    hold = buy_and_hold(candles, cash)
    return Result(start_ms, end_ms, cash, acct.cash, acct.btc, acct.fees_paid, strat.trades,
                  strat.locked_at_bar, candles[0].open, candles[-1].close,
                  hold.cash, hold.fees_paid)
