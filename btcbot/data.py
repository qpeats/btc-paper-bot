"""Price data: window definition, retrieval from public feeds, local archive loading, validation.

All timestamps are UTC milliseconds since the epoch. A candle is identified by its open time.
Nothing here fills, interpolates or invents prices: incomplete data raises DataError.
"""
from __future__ import annotations

import csv
import io
import json
import math
import ssl
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Tuple

MINUTE_MS = 60_000
WINDOW_MINUTES = 24 * 60  # the assignment's 24-hour window, in 1-minute candles

# Binance spot BTCUSDT 1-minute klines (the Phase 2 data source). Both hosts serve the same
# exchange data; the first is Binance's public market-data mirror, the second the main API.
BINANCE_HOSTS = ("https://data-api.binance.vision", "https://api.binance.com")
BINANCE_SYMBOL = "BTCUSDT"
BINANCE_PAGE_LIMIT = 1000  # max candles per Binance request

# Coinbase Exchange BTC-USD (opt-in only; not covered by the Phase 2 backtest).
COINBASE_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
COINBASE_PAGE_MINUTES = 300  # max candles per Coinbase request

HTTP_TIMEOUT_S = 20

HttpGet = Callable[[str], bytes]


class DataError(Exception):
    """The price data for the requested window is unavailable or fails validation."""


@dataclass(frozen=True)
class Candle:
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float


def fmt_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def window_bounds(moment: datetime) -> Tuple[int, int]:
    """Return [start_ms, end_ms) for the 24 hours before `moment`.

    `moment` is floored to the minute so the window holds exactly 1,440 fully closed
    1-minute candles: the last one opens at end-1min and closes at `end`, which is <= moment.
    """
    if moment.tzinfo is None:
        raise ValueError("moment must be timezone-aware (UTC)")
    moment = moment.astimezone(timezone.utc).replace(second=0, microsecond=0)
    end_ms = int(moment.timestamp() * 1000)
    start_ms = end_ms - WINDOW_MINUTES * MINUTE_MS
    return start_ms, end_ms


def parse_moment(text: str) -> datetime:
    """Parse an ISO-8601 time such as '2026-09-15T12:00Z'. A time without zone is taken as UTC."""
    t = text.strip().replace(" ", "T")
    if t.endswith(("Z", "z")):
        t = t[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(t)
    except ValueError as exc:
        raise ValueError(f"cannot parse time {text!r}; use e.g. 2026-09-15T12:00Z") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# --------------------------------------------------------------------------- validation

def validate_window(candles: Iterable[Candle], start_ms: int, end_ms: int) -> List[Candle]:
    """Return the window's candles in time order, or raise DataError.

    Checks: no duplicates, no gaps, exactly the expected minutes, finite positive prices,
    and internally consistent OHLC (high >= open/close/low, low <= open/close).
    """
    rows = sorted(candles, key=lambda c: c.open_time_ms)
    expected = (end_ms - start_ms) // MINUTE_MS
    seen = set()
    for c in rows:
        if c.open_time_ms in seen:
            raise DataError(f"duplicate candle at {fmt_ms(c.open_time_ms)}")
        seen.add(c.open_time_ms)
        if c.open_time_ms % MINUTE_MS:
            raise DataError(f"candle time not on a minute boundary: {c.open_time_ms}")
        if not (start_ms <= c.open_time_ms < end_ms):
            raise DataError(f"candle outside the window: {fmt_ms(c.open_time_ms)}")
        prices = (c.open, c.high, c.low, c.close)
        if not all(math.isfinite(p) and p > 0 for p in prices):
            raise DataError(f"non-positive or non-finite price at {fmt_ms(c.open_time_ms)}")
        if c.high < max(c.open, c.close, c.low) or c.low > min(c.open, c.close):
            raise DataError(f"inconsistent OHLC at {fmt_ms(c.open_time_ms)}")
    if len(rows) != expected:
        present = {c.open_time_ms for c in rows}
        missing = [t for t in range(start_ms, end_ms, MINUTE_MS) if t not in present]
        first = fmt_ms(missing[0]) if missing else "n/a"
        raise DataError(
            f"incomplete data: {len(rows)} of {expected} candles; "
            f"{len(missing)} minute(s) missing, first at {first}"
        )
    return rows


# --------------------------------------------------------------------------- HTTP

def _ssl_context() -> ssl.SSLContext:
    try:  # use certifi's CA bundle when installed (helps python.org builds on macOS)
        import certifi  # type: ignore

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "btc-paper-bot/1.0"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S, context=_ssl_context()) as resp:
        return resp.read()


def _describe(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        hint = " (Binance refuses some regions, e.g. the US, with 451)" if exc.code == 451 else ""
        return f"HTTP {exc.code}{hint}"
    if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, ssl.SSLError):
        return f"SSL error {exc.reason}; on python.org Python for macOS run 'Install Certificates.command'"
    return f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- Binance

def _fetch_binance_host(host: str, start_ms: int, end_ms: int, get: HttpGet) -> List[Candle]:
    out: List[Candle] = []
    cursor = start_ms
    while cursor < end_ms:
        url = (
            f"{host}/api/v3/klines?symbol={BINANCE_SYMBOL}&interval=1m"
            f"&startTime={cursor}&endTime={end_ms - 1}&limit={BINANCE_PAGE_LIMIT}"
        )
        rows = json.loads(get(url))
        if not isinstance(rows, list):
            raise ValueError(f"unexpected response: {str(rows)[:200]}")
        if not rows:
            break
        for r in rows:
            out.append(Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])))
        nxt = int(rows[-1][0]) + MINUTE_MS
        if nxt <= cursor:
            raise ValueError("feed did not advance")
        cursor = nxt
    return out


def fetch_binance(start_ms: int, end_ms: int, get: HttpGet = http_get) -> List[Candle]:
    errors = []
    for host in BINANCE_HOSTS:
        try:
            return _fetch_binance_host(host, start_ms, end_ms, get)
        except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            errors.append(f"{host}: {_describe(exc)}")
    raise DataError("could not fetch Binance candles:\n  " + "\n  ".join(errors))


# --------------------------------------------------------------------------- Coinbase (opt-in)

def fetch_coinbase(start_ms: int, end_ms: int, get: HttpGet = http_get) -> List[Candle]:
    by_time: Dict[int, Candle] = {}
    chunk = COINBASE_PAGE_MINUTES * MINUTE_MS
    try:
        for a in range(start_ms, end_ms, chunk):
            b = min(a + chunk, end_ms) - MINUTE_MS  # last candle open time in this chunk
            # "Z" suffix: a "+00:00" offset would need URL-encoding ("+" reads as a space)
            s = datetime.fromtimestamp(a / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            e = datetime.fromtimestamp(b / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            rows = json.loads(get(f"{COINBASE_URL}?granularity=60&start={s}&end={e}"))
            if not isinstance(rows, list):
                raise ValueError(f"unexpected response: {str(rows)[:200]}")
            for t, low, high, op, cl, _vol in rows:  # Coinbase order: time, low, high, open, close
                ms = int(t) * 1000
                if start_ms <= ms < end_ms:
                    by_time[ms] = Candle(ms, float(op), float(high), float(low), float(cl))
    except (urllib.error.URLError, OSError, ValueError, TypeError) as exc:
        raise DataError(f"could not fetch Coinbase candles: {_describe(exc)}") from exc
    return list(by_time.values())


# --------------------------------------------------------------------------- local archives

def _to_ms(ts: int) -> int:
    # Binance Vision archives use milliseconds through 2024 and microseconds from 2025.
    return ts // 1000 if ts >= 10**14 else ts


def _csv_candles(text: str) -> Iterable[Candle]:
    for row in csv.reader(io.StringIO(text)):
        if not row or not row[0].strip().lstrip("-").isdigit():
            continue  # header or blank line
        yield Candle(_to_ms(int(row[0])), float(row[1]), float(row[2]), float(row[3]), float(row[4]))


def load_archive(paths: Iterable[str], start_ms: int, end_ms: int) -> List[Candle]:
    """Read Binance Vision kline CSVs or the monthly ZIPs that contain them; keep the window."""
    out: List[Candle] = []
    for p in paths:
        path = Path(p).expanduser()
        if not path.exists():
            raise DataError(f"data file not found: {path}")
        texts: List[str] = []
        if path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as zf:
                for name in zf.namelist():
                    if name.lower().endswith(".csv"):
                        texts.append(zf.read(name).decode("utf-8"))
        else:
            texts.append(path.read_text(encoding="utf-8"))
        for text in texts:
            try:
                out.extend(c for c in _csv_candles(text) if start_ms <= c.open_time_ms < end_ms)
            except (ValueError, IndexError) as exc:
                raise DataError(f"cannot parse {path}: {exc}") from exc
    return out


# --------------------------------------------------------------------------- entry point

SOURCES = {
    "binance": "Binance spot BTC/USDT, 1-minute candles",
    "coinbase": "Coinbase Exchange BTC-USD, 1-minute candles",
}


def get_window(start_ms: int, end_ms: int, source: str = "binance",
               files: Iterable[str] = (), get: HttpGet = http_get) -> List[Candle]:
    files = list(files)
    if files:
        raw = load_archive(files, start_ms, end_ms)
    elif source == "binance":
        raw = fetch_binance(start_ms, end_ms, get)
    elif source == "coinbase":
        raw = fetch_coinbase(start_ms, end_ms, get)
    else:
        raise ValueError(f"unknown source {source!r}")
    return validate_window(raw, start_ms, end_ms)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "Candle", "DataError", "MINUTE_MS", "WINDOW_MINUTES", "SOURCES", "fmt_ms",
    "window_bounds", "parse_moment", "validate_window", "fetch_binance", "fetch_coinbase",
    "load_archive", "get_window", "utc_now",
]
