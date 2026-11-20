"""Indicators and entry conditions used by the four strategy specs (numpy only).

Every function takes full-history arrays and returns an array of the same length;
value at index i depends only on bars 0..i (no look-ahead). Warm-up bars are NaN / False.
"""
from __future__ import annotations

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

TF_MS = {"4h": 14_400_000, "1d": 86_400_000}


# ---------------------------------------------------------------- building blocks

def _rolling(x, n, fn):
    """Apply fn(windows, axis=1) over trailing windows of n bars; first n-1 values are NaN."""
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        out[n - 1:] = fn(sliding_window_view(np.asarray(x, dtype=float), n), axis=1)
    return out


def sma(x, n):
    return _rolling(x, n, np.mean)


def stdev(x, n):
    """Population standard deviation (ddof=0) over a trailing window."""
    return _rolling(x, n, np.std)


def ema(x, n):
    """EMA with alpha = 2/(n+1), seeded with the SMA of the first n values."""
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    k = 2.0 / (n + 1)
    out[n - 1] = np.mean(x[:n])
    for i in range(n, len(x)):
        out[i] = k * x[i] + (1 - k) * out[i - 1]
    return out


def true_range(high, low, close):
    prev = np.concatenate([[np.nan], close[:-1]])
    tr = np.fmax(high - low, np.fmax(np.abs(high - prev), np.abs(low - prev)))
    return tr


def atr(high, low, close, n, early="nan"):
    """Wilder ATR: mean of the first n true ranges, then atr += (tr - atr) / n.

    early="nan"  - bars before the seed are NaN;
    early="mean" - bars before the seed hold the running mean of the true ranges seen so far.
    """
    tr = true_range(high, low, close)
    out = np.full(len(tr), np.nan)
    if early == "mean":
        m = min(n, len(tr))
        out[:m] = np.cumsum(tr[:m]) / np.arange(1, m + 1)
    if len(tr) >= n:
        out[n - 1] = tr[:n].mean()
        for i in range(n, len(tr)):
            out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return out


# ---------------------------------------------------------------- indicators

def supertrend(high, low, close, n=10, mult=3.0):
    """Supertrend line and direction (+1 up / -1 down). Bands are (H+L)/2 +- mult * ATR(n)."""
    a = atr(high, low, close, n, early="mean")
    mid = (high + low) / 2
    up, dn = mid + mult * a, mid - mult * a  # raw upper / lower bands
    line, trend = np.empty(len(close)), np.empty(len(close))
    trend[0], line[0] = 1, dn[0]
    for i in range(1, len(close)):
        prev_up, prev_dn = up[i - 1], dn[i - 1]  # already ratcheted
        if not (up[i] < prev_up or close[i - 1] > prev_up):
            up[i] = prev_up  # upper band may only fall while price stays below it
        if not (dn[i] > prev_dn or close[i - 1] < prev_dn):
            dn[i] = prev_dn  # lower band may only rise while price stays above it
        trend[i] = 1 if close[i] > prev_up else -1 if close[i] < prev_dn else trend[i - 1]
        line[i] = dn[i] if trend[i] == 1 else up[i]
    return line, trend


def keltner(high, low, close, ema_period=20, atr_period=20, mult=2.0):
    """Keltner channel: EMA(close) +- mult * ATR. Returns (upper, mid, lower)."""
    mid = ema(close, ema_period)
    w = mult * atr(high, low, close, atr_period)
    return mid + w, mid, mid - w


def adx(high, low, close, n=14):
    """Wilder ADX. Smoothing is recursive with alpha = 1/n; each smoothed series is seeded
    with the exponentially weighted mean (same alpha) of its first n values."""
    size = len(close)
    out = np.full(size, np.nan)
    if size < 2 * n:
        return out
    tr = true_range(high, low, close)
    up = np.diff(high, prepend=high[0])
    dn = -np.diff(low, prepend=low[0])
    plus = np.where((up > dn) & (up > 0), up, 0.0)
    minus = np.where((dn > up) & (dn > 0), dn, 0.0)
    w = (1 - 1 / n) ** np.arange(n - 1, -1, -1)

    def smooth(x, first):  # first = index of the first of the n seed values
        s = np.full(size, np.nan)
        s[first + n - 1] = (w * x[first:first + n]).sum() / w.sum()
        for i in range(first + n, size):
            s[i] = s[i - 1] + (x[i] - s[i - 1]) / n
        return s

    s_tr, s_plus, s_minus = smooth(tr, 0), smooth(plus, 0), smooth(minus, 0)
    with np.errstate(divide="ignore", invalid="ignore"):
        di_plus = np.where(s_tr != 0, 100 * s_plus / s_tr, 0.0)
        di_minus = np.where(s_tr != 0, 100 * s_minus / s_tr, 0.0)
        total = di_plus + di_minus
        dx = np.where(total != 0, 100 * np.abs(di_plus - di_minus) / total, 0.0)
    return smooth(dx, n - 1)


def ichimoku_cloud(high, low, conversion=9, base=26, span_b=52, displacement=26):
    """Top and bottom of the Ichimoku cloud as plotted at each bar (spans shifted forward)."""
    def mid(n):
        return (_rolling(high, n, np.max) + _rolling(low, n, np.min)) / 2

    def shift(x):
        out = np.full(len(x), np.nan)
        out[displacement:] = x[:-displacement]
        return out

    a, b = shift((mid(conversion) + mid(base)) / 2), shift(mid(span_b))
    return np.maximum(a, b), np.minimum(a, b)


def zscore(close, n=75):
    """(close - SMA(n)) / population stdev(n); 0 where the stdev is 0."""
    sd = stdev(close, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(sd > 0, (close - sma(close, n)) / sd, 0.0)


def linreg_channel(close, n=50, mult=2.0):
    """Least-squares line over the last n closes, evaluated at the last bar,
    +- mult * population stdev of the residuals. Returns (upper, mid, lower)."""
    size = len(close)
    upper, mid, lower = (np.full(size, np.nan) for _ in range(3))
    if size < n:
        return upper, mid, lower
    y = sliding_window_view(np.asarray(close, dtype=float), n)
    x = np.arange(n) - (n - 1) / 2  # centred x
    y_mean = y.mean(axis=1)
    slope = ((y - y_mean[:, None]) * x).sum(axis=1) / (x * x).sum()
    resid = y - (y_mean[:, None] + slope[:, None] * x)
    sd = resid.std(axis=1)
    mid[n - 1:] = y_mean + slope * x[-1]
    upper[n - 1:], lower[n - 1:] = mid[n - 1:] + mult * sd, mid[n - 1:] - mult * sd
    return upper, mid, lower


def bollinger(close, n=20, mult=2.0):
    """Bollinger bands: SMA +- mult * population stdev. Returns (upper, mid, lower)."""
    mid, sd = sma(close, n), stdev(close, n)
    return mid + mult * sd, mid, mid - mult * sd


# ---------------------------------------------------------------- conditions

def _prev(x):
    return np.concatenate([[np.nan], x[:-1]])


def _cross_up(close, level):
    """Close moved from at-or-below the level to above it."""
    return (_prev(close) <= _prev(level)) & (close > level)


def _cross_down(close, level):
    return (_prev(close) >= _prev(level)) & (close < level)


def condition(cond: dict, bars: dict) -> np.ndarray:
    """Boolean mask over `bars` (dict of ts/open/high/low/close arrays of cond['tf']).

    True at bar i means the condition holds on the close of bar i.
    The first `warm` bars are always False (indicator not settled yet).
    """
    h, l, c = bars["high"], bars["low"], bars["close"]
    s, p = cond.get("settings") or {}, cond.get("params") or {}
    key = (cond["indicator"], cond["condition"])
    with np.errstate(invalid="ignore", divide="ignore"):
        if cond["indicator"] == "supertrend":
            n = int(s.get("atr_period", 10))
            line, _ = supertrend(h, l, c, n, float(s.get("mult", 3.0)))
            warm = n + 2
            m = {"price_crosses_above_supertrend": _cross_up, "price_crosses_below_supertrend": _cross_down}[key[1]](c, line)
        elif cond["indicator"] == "ema":
            n = int(s.get("period", 20))
            ma = ema(c, n)
            warm = n - 1  # the seed bar already counts
            m = {"price_above_ma": c > ma, "price_below_ma": c < ma}[key[1]]
        elif cond["indicator"] == "keltner":
            n_ema, n_atr = int(s.get("ema_period", 20)), int(s.get("atr_period", 20))
            upper, _, lower = keltner(h, l, c, n_ema, n_atr, float(s.get("mult", 2.0)))
            warm = max(n_ema, n_atr)
            m = {"price_crosses_above_upper": lambda: _cross_up(c, upper),
                 "price_crosses_below_lower": lambda: _cross_down(c, lower)}[key[1]]()
        elif key == ("adx", "adx_below"):
            n = int(s.get("period", 14))
            warm = 2 * n
            m = adx(h, l, c, n) < float(p.get("level", 20.0))
        elif cond["indicator"] == "ichimoku":
            lens = [int(s.get(k, d)) for k, d in (("conversion", 9), ("base", 26), ("span_b", 52), ("displacement", 26))]
            top, bottom = ichimoku_cloud(h, l, *lens)
            warm = max(lens)
            tol = float(p.get("tolerance", 0.002))  # close must clear the cloud edge by this fraction
            m = {"price_breaks_cloud_up": lambda: _cross_up(c, top * (1 + tol)),
                 "price_breaks_cloud_down": lambda: _cross_down(c, bottom * (1 - tol))}[key[1]]()
        elif key == ("sma_zscore", "between"):
            warm = int(s.get("length", 75))
            z = zscore(c, warm)
            m = (z >= float(p.get("lower", -1.0))) & (z <= float(p.get("upper", 1.0)))
        elif cond["indicator"] == "linreg_channel":
            warm = int(s.get("length", 100))
            upper, _, lower = linreg_channel(c, warm, float(s.get("mult", 2.0)))
            m = {"price_above_upper": c > upper, "price_below_lower": c < lower}[key[1]]
        elif key == ("bb", "bandwidth_below"):
            warm = int(s.get("length", 20))
            upper, mid, lower = bollinger(c, warm, float(s.get("mult", 2.0)))
            m = (upper - lower) / mid < float(p.get("value", 0.05))
        else:
            raise KeyError(f"unsupported condition {key}")
    m = np.asarray(m, dtype=bool).copy()
    m[:warm] = False
    return m


def align(src_ts, src_mask, src_tf, base_ts, base_tf):
    """Project a mask from src_tf bars onto base_tf bars without look-ahead:
    base bar i gets the value of the last src bar that has closed by the close of base bar i."""
    src_ms = TF_MS[src_tf]
    close_time = base_ts + TF_MS[base_tf]
    want = close_time // src_ms * src_ms - src_ms  # open time of that src bar
    j = np.minimum(np.searchsorted(src_ts, want), len(src_ts) - 1)
    return (src_ts[j] == want) & src_mask[j]


def entry_signal(block: dict, bars_by_tf: dict, base_tf: str) -> np.ndarray:
    """Entry mask on base_tf bars for one side of a spec ({"conditions": [...], "expression": "a and b"}).

    True at bar i = enter at the open of bar i+1. `expression` may use and / or / not / parentheses
    over condition ids; by default all conditions are AND-ed.
    """
    base_ts = bars_by_tf[base_tf]["ts"]
    masks = {}
    for cond in block["conditions"]:
        src = bars_by_tf[cond["tf"]]
        masks[cond["id"]] = align(src["ts"], condition(cond, src), cond["tf"], base_ts, base_tf)
    expr = block.get("expression") or " and ".join(masks)
    py = expr.replace(" and ", " & ").replace(" or ", " | ").replace("not ", "~")
    if not set(py) <= set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_ &|~()"):
        raise ValueError(f"bad expression: {expr!r}")
    return eval(py, {"__builtins__": {}}, masks)  # names restricted to condition ids, operators to & | ~
