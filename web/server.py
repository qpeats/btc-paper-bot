#!/usr/bin/env python3
"""Hosted one-button version of the bot.

    python3 web/server.py            # then open http://localhost:8000

GET  /            the page with the Run button
POST /api/run     fetch the 24 h before the current minute, replay strategy C3, return JSON
GET  /healthz     liveness check for the hosting platform

Standard library only, like the bot. The strategy, data and report code in btcbot/ is used
unchanged; this file only adds HTTP and turns the Result into JSON.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from btcbot import data, strategy as s  # noqa: E402
from btcbot.data import DataError, SOURCES, get_window, utc_now, window_bounds  # noqa: E402
from btcbot.report import render  # noqa: E402
from btcbot.strategy import Leg, Result, simulate  # noqa: E402

INDEX_HTML = Path(__file__).resolve().parent / "index.html"
SOURCE = os.environ.get("BOT_SOURCE", "binance")  # binance (default, as researched) or coinbase

# Indirection so tests can replace the network call.
_http_get = data.http_get

# The window is floored to the minute, so every click within one minute replays the same
# 1,440 candles. Cache that one result and run at most one replay at a time.
_lock = threading.Lock()
_cache: Dict[str, Any] = {"end_ms": None, "payload": None}


def _leg(leg: Leg) -> Dict[str, Any]:
    f = leg.fill
    return {"bar": leg.bar, "time_ms": leg.time_ms, "how": leg.how, "price": f.price,
            "qty": f.qty, "notional": f.notional, "fee": f.fee}


def result_to_json(r: Result, closes, report_text: str, run_at: str) -> Dict[str, Any]:
    return {
        "run_at": run_at,
        "source": SOURCES[SOURCE],
        "window": {"start_ms": r.start_ms, "end_ms": r.end_ms, "candles": len(closes)},
        "parameters": {
            "count_trades": s.COUNT_TRADES, "count_trade_usd": s.COUNT_TRADE_USD,
            "max_tranches": s.MAX_TRANCHES, "step": s.STEP, "take_profit": s.TAKE_PROFIT,
            "lock_min_trades": s.LOCK_MIN_TRADES, "lock_min_profit": s.LOCK_MIN_PROFIT,
            "fee_rate": 0.001, "slippage": s.SLIPPAGE,
        },
        "totals": {
            "initial_cash": r.initial_cash, "final_cash": r.final_cash, "pnl": r.pnl,
            "pnl_pct": r.pnl_pct, "fees": r.fees, "final_btc": r.final_btc,
            "trades": len(r.trades),
            "stopped_at_ms": (r.trades[-1].sell.time_ms if r.locked_at_bar is not None else None),
            "hold_pnl": r.hold_pnl, "hold_pnl_pct": 100 * r.hold_pnl / r.initial_cash,
            "hold_fees": r.hold_fees, "market_change_pct": r.market_change_pct,
            "first_open": r.first_open, "last_close": r.last_close,
        },
        "checks": {"trades": r.check_trades, "profit": r.check_profit, "flat": r.check_flat,
                   "passed": r.passed},
        "trades": [{
            "number": t.number, "kind": t.kind, "exit_reason": t.exit_reason,
            "qty": t.qty, "avg_buy_price": t.avg_buy_price, "fees": t.fees, "pnl": t.pnl,
            "notional": sum(b.fill.notional for b in t.buys),
            "buys": [_leg(b) for b in t.buys], "sell": _leg(t.sell),
        } for t in r.trades],
        "closes": closes,
        "report": report_text,
    }


def run_once() -> Tuple[int, Dict[str, Any]]:
    """Replay the 24 h before the current minute. Returns (http_status, json_body)."""
    with _lock:
        now = utc_now()
        start_ms, end_ms = window_bounds(now)
        if _cache["end_ms"] == end_ms and _cache["payload"] is not None:
            return 200, dict(_cache["payload"], cached=True)
        t0 = time.monotonic()
        try:
            candles = get_window(start_ms, end_ms, SOURCE, get=lambda url: _http_get(url))
        except DataError as exc:
            return 502, {"error": "Price data unavailable, so no simulation was run.",
                         "detail": str(exc)}
        r = simulate(candles, start_ms, end_ms)
        run_at = now.strftime("%Y-%m-%d %H:%M:%S UTC")
        payload = result_to_json(r, [round(c.close, 2) for c in candles],
                                 render(r, SOURCES[SOURCE], run_at), run_at)
        payload["seconds"] = round(time.monotonic() - t0, 2)
        _cache.update(end_ms=end_ms, payload=payload)
        return 200, dict(payload, cached=False)


class Handler(BaseHTTPRequestHandler):
    server_version = "btc-paper-bot/1.0"

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj: Dict[str, Any]) -> None:
        self._send(status, json.dumps(obj).encode(), "application/json")

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, INDEX_HTML.read_bytes(), "text/html; charset=utf-8")
        elif path == "/healthz":
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not found"})

    def do_HEAD(self) -> None:  # noqa: N802
        self.send_response(200 if self.path.split("?", 1)[0] in ("/", "/healthz") else 404)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/api/run":
            self._json(404, {"error": "not found"})
            return
        try:
            status, body = run_once()
        except Exception as exc:  # never leave the button hanging
            status, body = 500, {"error": "The bot crashed during the replay.",
                                 "detail": f"{type(exc).__name__}: {exc}"}
        self._json(status, body)

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))


def make_server(host: str = "0.0.0.0", port: Optional[int] = None) -> ThreadingHTTPServer:
    port = int(os.environ.get("PORT", "8000")) if port is None else port
    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    srv = make_server()
    print(f"BTC paper bot listening on http://localhost:{srv.server_address[1]}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
