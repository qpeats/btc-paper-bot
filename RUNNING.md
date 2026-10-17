# Running the bot (Phase 3 notes — not the submission README)

Prerequisite: Python 3.9 or newer. No packages to install (standard library only).
A clean Mac's built-in `python3` (3.9) works.

```bash
git clone <repo> && cd btc-bot
python3 bot.py
```

That replays the 24 hours before the current minute on Binance spot BTC/USDT 1-minute candles
and prints every trade, the totals, buy-and-hold, and PASS/FAIL for the three rerun rules.
Exit code 0 = simulation ran (whatever the result); 2 = price data unavailable, nothing simulated.

Options (none needed for the normal run):

| Option | Effect |
|---|---|
| `--end 2026-09-15T12:00Z` | replay the 24 h before that UTC time instead of now |
| `--data-file PATH [PATH ...]` | offline: read Binance Vision kline CSV/ZIP archives (needs `--end`) |
| `--source coinbase` | use Coinbase BTC-USD instead of Binance (not covered by the Phase 2 backtest) |

Offline example with the monthly archives:

```bash
python3 bot.py --end 2026-09-15T12:00Z --data-file ~/Downloads/BTCUSDT-1m-2026-09.zip
```

A window that straddles a month boundary needs both months' files.

Tests:

```bash
python3 -m unittest -v
```

If HTTPS fails with an SSL certificate error on a python.org install of Python for macOS, run
its `Install Certificates.command` once (or `pip install certifi`; the bot uses it if present).

## Layout

- `bot.py` — entry point
- `btcbot/data.py` — window definition, Binance/Coinbase fetch, archive loading, validation
- `btcbot/account.py` — cash, BTC, 0.1% fees
- `btcbot/strategy.py` — C3 parameters, per-bar rules, replay, results
- `btcbot/report.py` — terminal output
- `tests/test_bot.py` — correctness tests (synthetic data)
