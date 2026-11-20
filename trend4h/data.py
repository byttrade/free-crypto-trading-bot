"""Market data: download public Binance USDT-M history (no API keys) and load it from the local cache.

Cache layout (plain CSV, one file per coin and series):
    data/BTC_4h.csv, data/BTC_1d.csv   ts,open,high,low,close,volume   (ts = bar open, ms UTC)
    data/BTC_funding.csv               ts,rate
"""
from __future__ import annotations

import json
import ssl
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .indicators import TF_MS

COINS = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "LINK", "AVAX", "SUI",
         "LTC", "DOT", "NEAR", "AAVE", "TRX", "BCH", "UNI", "ARB", "OP", "INJ"]
API = "https://fapi.binance.com"
DATA_DIR = Path("data")
HISTORY_START = "2023-01-01"
DAY = 86_400_000


def day_ms(date: str) -> int:
    """'YYYY-MM-DD' -> ms of 00:00 UTC."""
    return int(datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def urlopen(url, timeout=30):
    """urllib with the certifi CA bundle when it is installed: some Python builds ship without root certificates."""
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = None
    return urllib.request.urlopen(url, timeout=timeout, context=ctx)


def _get(path: str, **params):
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    for attempt in range(5):
        try:
            with urlopen(url) as r:
                return json.load(r)
        except Exception:
            if attempt == 4:
                raise
            time.sleep(2 * (attempt + 1))


def _pages(path, start, end, limit, ts_of, **params):
    """All rows with start <= ts < end, following the API pagination."""
    rows = []
    while start < end:
        page = _get(path, startTime=start, endTime=end - 1, limit=limit, **params)
        rows += page
        if len(page) < limit:
            break
        start = ts_of(page[-1]) + 1
        time.sleep(0.15)  # stay far below the public rate limit
    return rows


def fetch(coins=COINS, start=HISTORY_START, end="2026-06-30", data_dir=DATA_DIR, log=print):
    """Download 4h and 1d candles and funding rates for `coins`, from `start` to `end` inclusive."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    t0, t1 = day_ms(start), day_ms(end) + DAY
    now = int(time.time() * 1000)
    for coin in coins:
        sym = f"{coin}USDT"
        for tf in ("4h", "1d"):
            raw = _pages("/fapi/v1/klines", t0, t1, 1500, lambda k: k[0], symbol=sym, interval=tf)
            rows = [(int(k[0]), *(float(x) for x in k[1:6])) for k in raw if k[0] + TF_MS[tf] <= now]  # closed bars only
            pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"]).to_csv(
                data_dir / f"{coin}_{tf}.csv", index=False)
            log(f"{coin} {tf}: {len(rows)} bars")
        raw = _pages("/fapi/v1/fundingRate", t0, t1, 1000, lambda f: f["fundingTime"], symbol=sym)
        pd.DataFrame([(int(f["fundingTime"]), float(f["fundingRate"])) for f in raw], columns=["ts", "rate"]).to_csv(
            data_dir / f"{coin}_funding.csv", index=False)
        log(f"{coin} funding: {len(raw)} rows")


def recent(coin: str, tf: str, limit: int, now_ms=None) -> dict:
    """Last `limit` closed bars straight from the exchange API (no cache) - used by the executor."""
    now_ms = now_ms or int(time.time() * 1000)
    raw = [k for k in _get("/fapi/v1/klines", symbol=f"{coin}USDT", interval=tf, limit=limit) if k[0] + TF_MS[tf] <= now_ms]
    out = {name: np.array([float(k[i]) for k in raw]) for i, name in ((1, "open"), (2, "high"), (3, "low"), (4, "close"))}
    out["ts"] = np.array([int(k[0]) for k in raw], dtype=np.int64)
    return out


def load_bars(coin: str, tf: str, data_dir=DATA_DIR) -> dict:
    path = Path(data_dir) / f"{coin}_{tf}.csv"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found - run `python -m trend4h fetch` first")
    df = pd.read_csv(path)
    out = {k: df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close")}
    out["ts"] = df["ts"].to_numpy(dtype=np.int64)
    return out


def load_funding(coin: str, data_dir=DATA_DIR) -> np.ndarray:
    """Array of (ts_ms, rate) rows; empty if the file is missing."""
    path = Path(data_dir) / f"{coin}_funding.csv"
    if not path.exists():
        return np.empty((0, 2))
    return pd.read_csv(path).to_numpy(dtype=float).reshape(-1, 2)
