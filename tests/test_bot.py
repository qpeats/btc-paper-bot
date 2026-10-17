"""Deterministic software-correctness tests (stdlib unittest; synthetic prices only).

These verify the implementation, not the strategy's profitability.
Run: python3 -m unittest -v
"""
from __future__ import annotations

import contextlib
import io
import os
import random
import tempfile
import unittest
import urllib.error
import zipfile
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from btcbot import strategy as S
from btcbot.account import FEE_RATE, Account, InsufficientBTC, InsufficientFunds
from btcbot.cli import main as cli_main
from btcbot.data import (MINUTE_MS, Candle, DataError, fetch_binance, fetch_coinbase,
                         get_window, load_archive, parse_moment, validate_window, window_bounds)
from btcbot.strategy import C3Strategy, simulate

T0 = 1_760_000_040_000  # a minute boundary (2025-10-09 08:54 UTC)
P = 50_000.0
END = T0 + 1440 * MINUTE_MS


def bar(i: int, o: float, h: float = None, l: float = None, c: float = None) -> Candle:
    c = o if c is None else c
    h = max(o, c) if h is None else h
    l = min(o, c) if l is None else l
    return Candle(T0 + i * MINUTE_MS, o, h, l, c)


def flat(n: int, p: float = P, start: int = 0):
    return [bar(start + i, p) for i in range(n)]


def random_walk(seed: int, n: int = 1440, p0: float = P, vol: float = 0.0008):
    rnd = random.Random(seed)
    out, p = [], p0
    for i in range(n):
        o = p
        c = o * (1 + rnd.gauss(0, vol))
        h = max(o, c) * (1 + abs(rnd.gauss(0, vol / 2)))
        l = min(o, c) * (1 - abs(rnd.gauss(0, vol / 2)))
        out.append(bar(i, o, h, l, c))
        p = c
    return out


def run(candles):
    acct = Account(S.INITIAL_CASH)
    st = C3Strategy(acct)
    for i, c in enumerate(candles):
        st.on_bar(i, c)
    st.finish(len(candles) - 1, candles[-1])
    return acct, st


def all_legs(trades):
    legs = []
    for t in trades:
        legs += [(b.bar, b.how, b.fill) for b in t.buys] + [(t.sell.bar, t.sell.how, t.sell.fill)]
    return legs


# ------------------------------------------------------------------------- 1. fees, 2. balances

class TestAccount(unittest.TestCase):
    def test_fee_rate(self):
        self.assertEqual(FEE_RATE, 0.001)

    def test_buy_and_sell_fees(self):
        a = Account(10_000)
        f = a.buy(1_000, 40_000)
        self.assertAlmostEqual(f.fee, 1.0, places=12)
        self.assertAlmostEqual(a.cash, 8_999.0, places=9)
        self.assertAlmostEqual(a.btc, 0.025, places=15)
        g = a.sell_all(44_000)
        self.assertAlmostEqual(g.notional, 1_100.0, places=9)
        self.assertAlmostEqual(g.fee, 1.1, places=12)
        self.assertAlmostEqual(a.cash, 8_999.0 + 1_100 - 1.1, places=9)
        self.assertEqual(a.btc, 0.0)
        self.assertAlmostEqual(a.fees_paid, 2.1, places=12)

    def test_phase2_fee_table(self):
        # Phase 2, Section 3: $10,000 spent incl. fee, sold after a gross move; net P&L.
        expected = {0.0: -19.98, 0.001: -10.00, 0.002: -0.02, 0.003: 9.96, 0.005: 29.92, 0.01: 79.82}
        for move, pnl in expected.items():
            a = Account(10_000)
            a.buy(a.max_buy_notional(), 60_000)
            a.sell_all(60_000 * (1 + move))
            self.assertEqual(round(a.cash - 10_000, 2), pnl, msg=f"move {move}")

    def test_break_even_move(self):
        a = Account(10_000)
        a.buy(a.max_buy_notional(), 60_000)
        a.sell_all(60_000 * (1 + FEE_RATE) / (1 - FEE_RATE))  # +0.2002002%
        self.assertAlmostEqual(a.cash, 10_000, places=8)

    def test_cannot_overspend_or_oversell(self):
        a = Account(100)
        with self.assertRaises(InsufficientFunds):
            a.buy(100, 1_000)  # 100 + 0.10 fee > 100
        a.buy(a.max_buy_notional(), 1_000)  # all-in is allowed
        self.assertGreaterEqual(a.cash, 0.0)
        self.assertLess(a.cash, 1e-9)
        with self.assertRaises(InsufficientBTC):
            a.sell(a.btc * 2, 1_000)


# ------------------------------------------------------------- 3. counting, 4. entries/exits

class TestStrategyRules(unittest.TestCase):
    def test_count_trades_then_first_tranche(self):
        acct, st = run(flat(30))
        counts = [t for t in st.trades if t.kind == "count"]
        self.assertEqual(len(counts), 4)
        for k, t in enumerate(counts):
            self.assertEqual((t.buys[0].bar, t.sell.bar), (k, k + 1))
            self.assertEqual(t.buys[0].fill.notional, 10.0)
            self.assertAlmostEqual(t.buys[0].fill.price, P * 1.0001, places=9)
            self.assertAlmostEqual(t.sell.fill.price, P * 0.9999, places=9)
            exp = 10 / (P * 1.0001) * P * 0.9999 * 0.999 - 10.01
            self.assertAlmostEqual(t.pnl, exp, places=12)
        main = st.trades[4]
        self.assertEqual(main.kind, "main")
        self.assertEqual(main.buys[0].bar, 4)  # entered at the open where count trade 4 exits
        cash_at_cycle_start = 10_000 + 4 * counts[0].pnl
        self.assertAlmostEqual(main.buys[0].fill.notional, cash_at_cycle_start / 6, places=9)

    def test_flat_market_loses_and_is_reported(self):
        acct, st = run(flat(1440))
        self.assertEqual(len(st.trades), 5)
        self.assertEqual(st.trades[-1].exit_reason, "end of window")
        self.assertLess(acct.cash, 10_000)  # honest loss, not hidden
        self.assertEqual(acct.btc, 0.0)

    def _tp_series(self, tp_high_factor):
        c = flat(1440)
        entry = P * 1.0001
        target = entry * 1.003
        c[10] = bar(10, P, target * 1.0001 * tp_high_factor, P, P)
        return c, target

    def test_take_profit_needs_trade_through_and_fills_at_target(self):
        c, target = self._tp_series(1 - 1e-9)  # just short of the 1 bp trade-through
        _, st = run(c)
        self.assertEqual(st.trades[4].exit_reason, "end of window")

        c, target = self._tp_series(1 + 1e-6)  # just beyond it
        acct, st = run(c)
        t = st.trades[4]
        self.assertEqual((t.exit_reason, t.sell.bar), ("take-profit", 10))
        self.assertAlmostEqual(t.sell.fill.price, target, places=6)  # at target, never better
        self.assertEqual(len(st.trades), 5)
        self.assertTrue(st.locked)

    def test_reference_single_tranche_cycle(self):
        # One tranche + take-profit after four flat count trades: Phase 2 reports the
        # typical winning window at about +$1.58 (median +$1.58 for C3).
        c, _ = self._tp_series(1.001)
        acct, st = run(c)
        self.assertTrue(1.55 < acct.cash - 10_000 < 1.60, acct.cash - 10_000)

    def test_no_take_profit_on_entry_bar(self):
        c = flat(1440)
        c[4] = bar(4, P, P * 1.02, P, P)  # entry bar spikes; position opened at this open
        _, st = run(c)
        self.assertEqual(st.trades[4].exit_reason, "end of window")
        c[5] = bar(5, P, P * 1.02, P, P)  # next bar spikes: resting order is live now
        _, st = run(c)
        self.assertEqual((st.trades[4].exit_reason, st.trades[4].sell.bar), ("take-profit", 5))

    def test_averaging_down_fills_at_limit_and_adverse_first(self):
        c = flat(1440)
        entry = P * 1.0001
        # bar 6 gaps below the limit and also spikes above any take-profit
        c[6] = bar(6, P * 0.99, P * 1.05, P * 0.985, P * 0.99)
        # afterwards: above the next add level (entry*0.995^2) and below any take-profit
        c[7:] = flat(1440 - 7, P * 0.993, start=7)
        _, st = run(c)
        t = st.trades[4]
        self.assertEqual(len(t.buys), 2)
        self.assertEqual((t.buys[1].bar, t.buys[1].how), (6, "limit"))
        self.assertAlmostEqual(t.buys[1].fill.price, entry * 0.995, places=6)  # not the 0.99 open
        self.assertNotEqual(t.sell.bar, 6)  # take-profit not assumed in the same bar

    def test_one_add_per_bar_and_six_tranche_cap(self):
        c = flat(5)
        p = P
        for i in range(5, 40):  # falls 1% every bar
            c.append(bar(i, p, p, p * 0.99, p * 0.99))
            p *= 0.99
        c += flat(1440 - 40, p, start=40)
        acct, st = run(c)
        t = st.trades[4]
        self.assertEqual(len(t.buys), 6)
        self.assertEqual([b.bar for b in t.buys], [4, 5, 6, 7, 8, 9])  # one fill per bar
        for prev, nxt in zip(t.buys, t.buys[1:]):
            self.assertAlmostEqual(nxt.fill.price, prev.fill.price * 0.995, places=6)
        self.assertLess(t.buys[5].fill.notional, t.buys[0].fill.notional)  # capped by cash
        self.assertGreaterEqual(acct.cash, 0.0)
        self.assertEqual(acct.btc, 0.0)

    def test_restart_when_not_ahead_then_lock(self):
        # Count trades lose ~$0.50 each on 5% gaps, so the first cycle's win is not enough.
        opens = [P * 0.95 ** k for k in range(5)]
        c = [bar(i, o) for i, o in enumerate(opens)]
        p4 = opens[4]
        c += flat(1440 - 5, p4, start=5)
        spike = p4 * 1.02
        c[6] = bar(6, p4, spike, p4, p4)   # cycle 1 take-profit
        c[9] = bar(9, p4, spike, p4, p4)   # cycle 2 take-profit
        c[12] = bar(12, p4, spike, p4, p4)  # would be cycle 3, must not trade (locked)
        acct, st = run(c)
        self.assertEqual([t.kind for t in st.trades], ["count"] * 4 + ["main", "main"])
        cash_after_5 = 10_000 + sum(t.pnl for t in st.trades[:5])
        self.assertLessEqual(cash_after_5, 10_000.50)
        self.assertEqual(st.trades[5].buys[0].bar, 7)  # restarted at the next open
        self.assertTrue(st.locked)
        self.assertGreater(acct.cash, 10_000.50)

    def test_lock_stops_all_trading(self):
        c, _ = self._tp_series(1.001)
        for i in range(20, 1440, 50):
            c[i] = bar(i, P, P * 1.05, P * 0.9, P)
        _, st = run(c)
        self.assertEqual(len(st.trades), 5)


# --------------------------------------------------------------------------- 5. look-ahead

class TestNoLookAhead(unittest.TestCase):
    def test_future_bars_do_not_change_past_decisions(self):
        for seed in range(6):
            base = random_walk(seed)
            for k in (5, 200, 700, 1300):
                other = random_walk(seed + 100, 1440, base[k].open * 1.03)
                alt = base[:k] + other[k:]  # same past, a different future
                _, a = run(base)
                _, b = run(alt)
                legs_a = [l for l in all_legs(a.trades) if l[0] < k]
                legs_b = [l for l in all_legs(b.trades) if l[0] < k]
                self.assertEqual(legs_a, legs_b, f"seed {seed} k {k}")

    def test_bars_must_arrive_in_order(self):
        st = C3Strategy(Account(10_000))
        st.on_bar(0, bar(0, P))
        with self.assertRaises(ValueError):
            st.on_bar(2, bar(2, P))
        with self.assertRaises(ValueError):
            st.finish(5, bar(5, P))


# --------------------------------------------------------------------- 6. end-of-session

class TestLiquidation(unittest.TestCase):
    def test_open_position_sold_at_final_close(self):
        c = flat(1439) + [bar(1439, P, P, P * 0.98, P * 0.98)]
        acct, st = run(c)
        t = st.trades[-1]
        self.assertEqual((t.exit_reason, t.sell.bar), ("end of window", 1439))
        self.assertAlmostEqual(t.sell.fill.price, P * 0.98 * 0.9999, places=6)
        self.assertEqual(t.sell.time_ms, END)
        self.assertEqual(acct.btc, 0.0)

    def test_always_flat_on_random_paths(self):
        for seed in range(25):
            acct, st = run(random_walk(seed, vol=0.0015))
            self.assertEqual(acct.btc, 0.0)
            self.assertIsNone(st.pos)


# ------------------------------------------------- 7. timestamps, 8. missing data, data I/O

def _csv_rows(candles, micro=True):
    mult = 1000 if micro else 1
    return "".join(f"{c.open_time_ms * mult},{c.open},{c.high},{c.low},{c.close},1,0,0,0,0,0,0\n"
                   for c in candles)


class TestWindowAndData(unittest.TestCase):
    def test_window_bounds(self):
        s, e = window_bounds(datetime(2026, 10, 17, 15, 14, 37, tzinfo=timezone.utc))
        self.assertEqual(e, int(datetime(2026, 10, 17, 15, 14, tzinfo=timezone.utc).timestamp() * 1000))
        self.assertEqual(e - s, 1440 * MINUTE_MS)
        with self.assertRaises(ValueError):
            window_bounds(datetime(2026, 10, 17, 15, 14))  # naive time rejected

    def test_parse_moment(self):
        a = parse_moment("2026-09-15T12:00Z")
        self.assertEqual(a, parse_moment("2026-09-15 12:00"))
        self.assertEqual(a, parse_moment("2026-09-15T14:00+02:00"))
        with self.assertRaises(ValueError):
            parse_moment("yesterday")

    def test_archive_ms_us_zip_and_window_selection(self):
        three_days = [bar(i - 1440, P + i) for i in range(3 * 1440)]
        with tempfile.TemporaryDirectory() as d:
            f_us = os.path.join(d, "us.csv")
            f_ms = os.path.join(d, "ms.csv")
            f_zip = os.path.join(d, "m.zip")
            with open(f_us, "w") as f:
                f.write(_csv_rows(three_days, micro=True))
            with open(f_ms, "w") as f:
                f.write("open_time,open,high,low,close,volume\n" + _csv_rows(three_days, micro=False))
            with zipfile.ZipFile(f_zip, "w") as z:
                z.writestr("BTCUSDT-1m.csv", _csv_rows(three_days))
            for path in (f_us, f_ms, f_zip):
                got = get_window(T0, END, files=[path])
                self.assertEqual(len(got), 1440)
                self.assertEqual((got[0].open_time_ms, got[-1].open_time_ms),
                                 (T0, END - MINUTE_MS))
            # split across two files (e.g. a month boundary)
            half_a, half_b = os.path.join(d, "a.csv"), os.path.join(d, "b.csv")
            with open(half_a, "w") as f:
                f.write(_csv_rows(three_days[:2000]))
            with open(half_b, "w") as f:
                f.write(_csv_rows(three_days[2000:]))
            self.assertEqual(len(get_window(T0, END, files=[half_a, half_b])), 1440)
            with self.assertRaises(DataError):
                load_archive([os.path.join(d, "nope.csv")], T0, END)

    def test_validation_rejects_bad_data(self):
        good = flat(1440)
        self.assertEqual(len(validate_window(list(reversed(good)), T0, END)), 1440)
        cases = {
            "gap": good[:700] + good[701:],
            "duplicate": good[:700] + [good[700]] + good[700:1439],
            "short": good[:1439],
            "empty": [],
            "bad ohlc": good[:5] + [Candle(good[5].open_time_ms, P, P * 0.99, P, P)] + good[6:],
            "zero price": good[:5] + [Candle(good[5].open_time_ms, 0.0, P, 0.0, P)] + good[6:],
            "outside": good[1:] + [bar(1440, P)],
        }
        for name, rows in cases.items():
            with self.assertRaises(DataError, msg=name):
                validate_window(rows, T0, END)

    def test_binance_pagination_and_host_fallback(self):
        src = {c.open_time_ms: c for c in random_walk(1)}
        calls = []

        def fake_get(url):
            calls.append(url)
            if "data-api" in url:
                raise urllib.error.URLError("blocked")
            q = {k: int(v[0]) for k, v in parse_qs(urlparse(url).query).items()
                 if k in ("startTime", "endTime", "limit")}
            rows = [[t, str(c.open), str(c.high), str(c.low), str(c.close), "1", t + 59_999]
                    for t, c in sorted(src.items())
                    if q["startTime"] <= t <= q["endTime"]][: q["limit"]]
            return str(rows).replace("'", '"').encode()

        got = validate_window(fetch_binance(T0, END, fake_get), T0, END)
        self.assertEqual(len(got), 1440)
        self.assertEqual(got, [src[t] for t in sorted(src)])
        self.assertEqual(sum("api.binance.com" in u for u in calls), 2)  # 1000 + 440
        self.assertTrue(all(f"endTime={END - 1}" in u for u in calls))  # never asks past window

        def dead(url):
            raise urllib.error.HTTPError(url, 451, "unavailable", None, None)
        with self.assertRaises(DataError):
            fetch_binance(T0, END, dead)

    def test_coinbase_parsing(self):
        src = random_walk(2)

        def fake_get(url):
            q = parse_qs(urlparse(url).query)  # decodes like a server would ("+" -> space)
            s = int(parse_moment(q["start"][0]).timestamp() * 1000)
            e = int(parse_moment(q["end"][0]).timestamp() * 1000)
            rows = [[c.open_time_ms // 1000, c.low, c.high, c.open, c.close, 1.0]
                    for c in src if s <= c.open_time_ms <= e]
            return str(rows[::-1]).encode()  # Coinbase returns newest first

        got = validate_window(fetch_coinbase(T0, END, fake_get), T0, END)
        self.assertEqual(got, src)


# ------------------------------------------------- 9. determinism, 10. final P&L reconciliation

class TestDeterminismAndPnl(unittest.TestCase):
    def test_repeatable(self):
        c = random_walk(3)
        r1, r2 = simulate(c, T0, END), simulate(c, T0, END)
        self.assertEqual(all_legs(r1.trades), all_legs(r2.trades))
        self.assertEqual(r1.final_cash, r2.final_cash)

    def test_pnl_reconciles_with_trade_records(self):
        for seed in range(25):
            r = simulate(random_walk(seed, vol=0.0012), T0, END)
            self.assertAlmostEqual(r.pnl, sum(t.pnl for t in r.trades), places=8)
            self.assertAlmostEqual(r.fees, sum(t.fees for t in r.trades), places=10)
            for t in r.trades:
                self.assertAlmostEqual(t.sell.fill.qty, t.qty, places=15)
                self.assertTrue(all(b.bar <= t.sell.bar for b in t.buys))
                self.assertAlmostEqual(t.sell.fill.fee, t.sell.fill.notional * FEE_RATE, places=12)
                for b in t.buys:
                    self.assertAlmostEqual(b.fill.fee, b.fill.notional * FEE_RATE, places=12)
            # trades never overlap: each starts after the previous one closed
            for a, b in zip(r.trades, r.trades[1:]):
                self.assertLessEqual(a.sell.bar, b.buys[0].bar)
            self.assertEqual(r.passed, r.check_trades and r.check_profit and r.check_flat)
            self.assertEqual(r.check_profit, r.final_cash > 10_000)

    def test_buy_and_hold_comparison(self):
        c = flat(1439, P) + [bar(1439, P, P * 1.01, P, P * 1.01)]
        r = simulate(c, T0, END)
        a = Account(10_000)
        a.buy(a.max_buy_notional(), P * 1.0001)
        a.sell_all(P * 1.01 * 0.9999)
        self.assertAlmostEqual(r.hold_final_cash, a.cash, places=9)


# ----------------------------------------------------------------------------------- CLI

class TestCli(unittest.TestCase):
    def test_offline_run_and_errors(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "k.csv")
            with open(path, "w") as f:
                f.write(_csv_rows(random_walk(4)))
            end = datetime.fromtimestamp(END / 1000, tz=timezone.utc).isoformat()
            outs = []
            for _ in range(2):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    self.assertEqual(cli_main(["--end", end, "--data-file", path]), 0)
                outs.append([l for l in buf.getvalue().splitlines() if not l.startswith("Run at")])
            self.assertEqual(outs[0], outs[1])
            text = "\n".join(outs[0])
            for needle in ("TRADES", "Final balance", "Buy-and-hold", "Overall:"):
                self.assertIn(needle, text)

            err = io.StringIO()
            with contextlib.redirect_stderr(err):  # window not in file -> no simulation
                self.assertEqual(cli_main(["--end", "2026-01-01T00:00Z", "--data-file", path]), 2)
            self.assertIn("no simulation was run", err.getvalue())

            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli_main(["--data-file", path])  # offline mode needs --end
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli_main(["--end", "2999-01-01T00:00Z"])  # future window


if __name__ == "__main__":
    unittest.main()
