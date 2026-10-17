"""Command line: python3 bot.py [--source binance|coinbase] [--end TIME] [--data-file PATH ...]"""
from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from .data import SOURCES, DataError, get_window, parse_moment, utc_now, window_bounds
from .report import render
from .strategy import simulate


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Replay the 24 hours before now (or before --end) with strategy C3.")
    p.add_argument("--source", choices=sorted(SOURCES), default="binance",
                   help="price feed (default: binance = Binance spot BTCUSDT, the source the "
                        "strategy was researched on)")
    p.add_argument("--end", metavar="TIME",
                   help="replay the 24 h before this UTC time instead of now, "
                        "e.g. 2026-09-15T12:00Z")
    p.add_argument("--data-file", metavar="PATH", nargs="+", default=[],
                   help="offline mode: Binance Vision kline CSV/ZIP file(s) instead of the "
                        "network (requires --end)")
    a = p.parse_args(argv)

    if a.data_file and not a.end:
        p.error("--data-file needs --end (the archive has no 'now')")

    run_at = utc_now()
    try:
        moment = parse_moment(a.end) if a.end else run_at
    except ValueError as exc:
        p.error(str(exc))
    if moment > run_at:
        p.error("--end is in the future")
    start_ms, end_ms = window_bounds(moment)

    source = SOURCES["binance"] if a.data_file else SOURCES[a.source]
    if a.data_file:
        source += f" (local archive: {', '.join(a.data_file)})"
    try:
        candles = get_window(start_ms, end_ms, a.source, a.data_file)
    except DataError as exc:
        print(f"ERROR: price data unavailable, no simulation was run.\n{exc}", file=sys.stderr)
        return 2

    result = simulate(candles, start_ms, end_ms)
    print(render(result, source, run_at.strftime("%Y-%m-%d %H:%M:%S UTC")))
    return 0
