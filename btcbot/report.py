"""Terminal report. Every number is derived from the recorded fills, so it can be re-checked."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List

from . import strategy as s
from .data import fmt_ms
from .strategy import Leg, Result


def _t(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%m-%d %H:%M")


def _leg_time(leg: Leg) -> str:
    # limit / take-profit fills happen somewhere inside that minute
    return _t(leg.time_ms) + ("*" if leg.how in ("limit", "take-profit") else " ")


def _money(x: float) -> str:
    return f"{'-' if x < 0 else '+'}${abs(x):,.2f}"


def render(r: Result, source: str, run_at: str) -> str:
    out: List[str] = []
    w = out.append
    w("BTC paper-trading bot - strategy C3 (averaging-down take-profit)")
    w(f"Run at:       {run_at}")
    w(f"Data:         {source}")
    w(f"Window:       {fmt_ms(r.start_ms)}  ->  {fmt_ms(r.end_ms)}  (24 h, 1,440 candles)")
    w(f"Parameters:   {s.COUNT_TRADES} x ${s.COUNT_TRADE_USD:.0f} count trades; up to "
      f"{s.MAX_TRANCHES} tranches of 1/6, step {s.STEP:.1%}, take-profit {s.TAKE_PROFIT:.1%}; "
      f"stop at >= {s.LOCK_MIN_TRADES} trades and > ${s.LOCK_MIN_PROFIT:.2f} ahead")
    w(f"Costs:        fee 0.1% per side; market orders {s.SLIPPAGE * 1e4:.0f} bp slippage")
    w("")
    w("TRADES  (times UTC; * = limit fill inside that minute; size in BTC)")
    hdr = (f"{'#':>2}  {'type':<5} {'buy time':<12} {'buy price':>11}  {'sell time':<12} "
           f"{'sell price':>11}  {'size BTC':>11} {'size USD':>10} {'fees':>7} {'P&L':>9}  exit")
    w(hdr)
    w("-" * len(hdr))
    for t in r.trades:
        w(f"{t.number:>2}  {t.kind:<5} {_leg_time(t.buys[0]):<12} {t.avg_buy_price:>11,.2f}  "
          f"{_leg_time(t.sell):<12} {t.sell.fill.price:>11,.2f}  {t.qty:>11.8f} "
          f"{sum(b.fill.notional for b in t.buys):>10,.2f} {t.fees:>7.2f} {_money(t.pnl):>9}  "
          f"{t.exit_reason}")
        if len(t.buys) > 1:
            for k, b in enumerate(t.buys, 1):
                w(f"{'':>9}tranche {k}: {_leg_time(b)} @ {b.fill.price:>11,.2f}  "
                  f"{b.fill.qty:.8f} BTC  ${b.fill.notional:,.2f}  fee ${b.fill.fee:.2f}")
    if not r.trades:
        w("(no trades)")
    w("")
    w("TOTALS")
    w(f"  Completed trades:   {len(r.trades)}"
      + (f"   (stopped trading at {_t(r.trades[-1].sell.time_ms)} UTC: target reached)"
         if r.locked_at_bar is not None else ""))
    w(f"  Initial balance:    ${r.initial_cash:,.2f}")
    w(f"  Final balance:      ${r.final_cash:,.2f}")
    w(f"  Profit:             {_money(r.pnl)}  ({r.pnl_pct:+.4f}%)")
    w(f"  Fees paid:          ${r.fees:,.2f}")
    w(f"  BTC held at end:    {r.final_btc:.8f}")
    w(f"  Buy-and-hold:       {_money(r.hold_pnl)}  ({100 * r.hold_pnl / r.initial_cash:+.4f}%)"
      f"  after ${r.hold_fees:.2f} fees; BTC moved {r.market_change_pct:+.2f}% "
      f"({r.first_open:,.2f} -> {r.last_close:,.2f})")
    w("")
    w("ASSIGNMENT CHECKS")
    w(f"  At least 5 completed trades:   {'PASS' if r.check_trades else 'FAIL'}  ({len(r.trades)})")
    w(f"  Final balance > $10,000:       {'PASS' if r.check_profit else 'FAIL'}  "
      f"(${r.final_cash:,.4f})")
    w(f"  0 BTC held at end:             {'PASS' if r.check_flat else 'FAIL'}")
    w(f"  Overall:                       {'PASS' if r.passed else 'FAIL'}")
    return "\n".join(out)
