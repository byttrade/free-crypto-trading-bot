"""Bar-by-bar backtest of a strategy spec on USDT-M perpetuals.

Execution model
- A signal is known at the close of a bar; the entry is at the open of the next bar.
  One position per coin. If long and short fire on the same bar, long wins.
- Stop / take-profit are checked inside each bar by high / low, starting with the entry bar.
  If both are touched in one bar the stop wins; a gap through a level fills at the open.
- Distances are multiples of Wilder ATR(base timeframe) taken at the close of the signal bar.
  The trailing stop follows the best price since entry and is moved after the stop check of the bar.
- Size: notional = risk / stop distance; risk = risk_pct of a fixed capital (no compounding). R = PnL / risk.
  With "notional_usdt" in the spec the notional is fixed instead (R is still PnL / risk_pct of capital).
- Costs: fee and slippage per side, actual funding (long pays a positive rate, short receives it).
- Positions still open at the end of the tested range are closed at the last close.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from . import data
from .indicators import TF_MS, atr, entry_signal

CAPITAL = 10_000.0
DEFAULTS = {"risk_pct": 1.0, "fee": 0.0005, "slippage": 0.0002}
DAY = data.DAY
PERIODS = {  # [start, end) in ms UTC
    "is": (data.day_ms("2023-01-01"), data.day_ms("2025-07-01")),
    "oos": (data.day_ms("2025-07-01"), data.day_ms("2026-07-01")),
}


def funding_per_bar(ts, step, funding):
    """Sum of funding rates settled exactly at each bar open, and strictly inside each bar."""
    at_open, inside = np.zeros(len(ts)), np.zeros(len(ts))
    if len(funding):
        ft = (np.round(funding[:, 0] / 60_000) * 60_000).astype(np.int64)  # exchange stamps drift by a few ms
        i = np.searchsorted(ts, ft, side="right") - 1
        ok = (i >= 0) & (ft < ts[np.maximum(i, 0)] + step)
        exact = ok & (ft == ts[np.maximum(i, 0)])
        np.add.at(at_open, i[exact], funding[exact, 1])
        np.add.at(inside, i[ok & ~exact], funding[ok & ~exact, 1])
    return at_open, inside


def simulate(spec: dict, bars_by_tf: dict, funding: np.ndarray, start: int, end: int, coin: str = ""):
    """Trade one coin over bars with start <= open time < end.

    Returns (trades, daily): list of trade dicts and {day_ms: PnL booked that day, marked to market}.
    """
    tf = spec["base_tf"]
    step = TF_MS[tf]
    ex = spec["exit"]
    unknown = set(ex) - {"sl", "tp", "trail", "atr_period"}
    if unknown or "sl" not in ex:
        raise ValueError(f"exit needs sl (and optional tp, trail, atr_period) in ATR units; unknown: {sorted(unknown)}")
    k_sl, k_tp, k_trail = (ex[k]["atr"] if k in ex else None for k in ("sl", "tp", "trail"))
    fee, slip = (spec.get(k, DEFAULTS[k]) for k in ("fee", "slippage"))
    risk = CAPITAL * spec.get("risk_pct", DEFAULTS["risk_pct"]) / 100

    b = bars_by_tf[tf]
    ts, o, h, l, c = b["ts"], b["open"], b["high"], b["low"], b["close"]
    vol = atr(h, l, c, int(ex.get("atr_period", 14)))
    signal = {side: entry_signal(spec[name], bars_by_tf, tf) if spec.get(name) else np.zeros(len(ts), bool)
              for side, name in ((1, "long"), (-1, "short"))}
    fund_open, fund_in = funding_per_bar(ts, step, funding)
    first, last = np.searchsorted(ts, start), np.searchsorted(ts, end)

    trades, daily = [], {}
    pos, enter = None, 0  # enter: side to open at the next bar open

    def book(t, amount):
        day = t // DAY * DAY
        daily[day] = daily.get(day, 0.0) + amount

    def close(raw_price, t, reason):
        nonlocal pos
        side = pos["side"]
        price = raw_price * (1 - slip * side)
        exit_fee = fee * pos["qty"] * price
        pnl = side * pos["qty"] * (price - pos["entry"]) - pos["fees"] - exit_fee - pos["funding"]
        book(t, pnl - pos["booked"])
        trades.append({"coin": coin, "side": "long" if side == 1 else "short", "entry_ts": pos["ts"], "exit_ts": t,
                       "entry": pos["entry"], "exit": price, "reason": reason, "stop_pct": pos["stop_pct"],
                       "fees": pos["fees"] + exit_fee, "funding": pos["funding"], "pnl": pnl, "R": pnl / risk})
        pos = None

    for i in range(first, last):
        t = int(ts[i])
        if pos and fund_open[i] and pos["ts"] < t:  # held through the settlement at this bar's open
            pos["funding"] += pos["side"] * fund_open[i] * pos["qty"] * o[i]
        if enter:
            side, enter = enter, 0
            a = vol[i - 1] if i else np.nan
            if a > 0:  # skip while ATR is not warmed up
                price = o[i] * (1 + slip * side)
                notional = spec.get("notional_usdt") or risk * price / (k_sl * a)
                pos = {"side": side, "ts": t, "entry": price, "qty": notional / price, "fees": fee * notional,
                       "funding": 0.0, "booked": -fee * notional, "stop": price - side * k_sl * a,
                       "tp": price + side * k_tp * a if k_tp else None,
                       "trail": k_trail * a if k_trail else None, "best": price, "stop_pct": k_sl * a / price * 100}
                if pos["trail"]:
                    pos["stop"] = side * max(side * pos["stop"], side * (price - side * pos["trail"]))
                book(t, -pos["fees"])
        if pos:
            side, stop, tp = pos["side"], pos["stop"], pos["tp"]
            if l[i] <= stop if side == 1 else h[i] >= stop:
                close(min(o[i], stop) if side == 1 else max(o[i], stop), t, "stop")
            elif tp is not None and (h[i] >= tp if side == 1 else l[i] <= tp):
                close(max(o[i], tp) if side == 1 else min(o[i], tp), t, "tp")
        if pos:
            side = pos["side"]
            pos["funding"] += side * fund_in[i] * pos["qty"] * c[i]
            if pos["trail"]:
                pos["best"] = max(pos["best"], h[i]) if side == 1 else min(pos["best"], l[i])
                pos["stop"] = side * max(side * pos["stop"], side * (pos["best"] - side * pos["trail"]))
            mark = side * pos["qty"] * (c[i] - pos["entry"]) - pos["fees"] - pos["funding"]
            book(t + step - 1, mark - pos["booked"])
            pos["booked"] = mark
        elif i < last - 1:
            enter = 1 if signal[1][i] else -1 if signal[-1][i] else 0
    if pos:
        close(c[last - 1], int(ts[last - 1]) + step - 1, "period_end")
    return trades, daily


def _pf(r):
    loss = -r[r < 0].sum()
    return round(float(r[r > 0].sum() / loss), 2) if loss > 0 else None


def _group(trades, key):
    out = {}
    for t in trades:
        out.setdefault(key(t), []).append(t["R"])
    return {k: {"trades": len(v), "sum_R": round(float(np.sum(v)), 2), "profit_factor": _pf(np.array(v)),
                "win_pct": round(float((np.array(v) > 0).mean() * 100), 1)} for k, v in sorted(out.items())}


def half_year(ms):
    d = datetime.fromtimestamp(ms / 1000, timezone.utc)
    return f"{d.year}H{1 if d.month <= 6 else 2}"


def metrics(trades, daily, start, end, risk_pct=DEFAULTS["risk_pct"]) -> dict:
    r = np.array([t["R"] for t in trades])
    if not len(r):
        return {"trades": 0}
    risk = CAPITAL * risk_pct / 100
    days = np.arange(start // DAY * DAY, end, DAY)
    day_pct = np.array([daily.get(int(d), 0.0) for d in days]) / CAPITAL * 100
    equity = np.concatenate([[0.0], np.cumsum(day_pct)])
    return {
        "trades": len(r),
        "win_pct": round(float((r > 0).mean() * 100), 1),
        "profit_factor": _pf(r),
        "sum_R": round(float(r.sum()), 2),
        "pnl_usdt": round(float(sum(t["pnl"] for t in trades)), 2),  # on a fixed capital of 10 000
        "sum_R_without_top5": round(float(r.sum() - np.sort(r)[-5:].sum()), 2),
        "max_dd_pct": round(float((np.maximum.accumulate(equity) - equity).max()), 2),  # % of capital, daily curve
        "sharpe_daily": round(float(day_pct.mean() / day_pct.std() * np.sqrt(365)), 2) if day_pct.std() > 0 else None,
        "fees_R": round(sum(t["fees"] for t in trades) / risk, 2),
        "funding_R": round(sum(t["funding"] for t in trades) / risk, 2),  # > 0 = paid
        "median_stop_pct": round(float(np.median([t["stop_pct"] for t in trades])), 2),
        "by_half_year": _group(trades, lambda t: half_year(t["exit_ts"])),
        "by_side": _group(trades, lambda t: t["side"]),
        "by_coin": _group(trades, lambda t: t["coin"]),
    }


def run(spec: dict, start: int, end: int, data_dir=data.DATA_DIR):
    """Backtest all coins of the spec on [start, end). Returns (metrics, trades)."""
    coins = data.COINS if spec.get("coins", "all") == "all" else spec["coins"]
    tfs = {spec["base_tf"]} | {c["tf"] for side in ("long", "short") for c in (spec.get(side) or {}).get("conditions", [])}
    trades, daily = [], {}
    for coin in coins:
        bars = {tf: data.load_bars(coin, tf, data_dir) for tf in tfs}
        tr, dl = simulate(spec, bars, data.load_funding(coin, data_dir), start, end, coin)
        trades += tr
        for day, x in dl.items():
            daily[day] = daily.get(day, 0.0) + x
    return metrics(trades, daily, start, end, spec.get("risk_pct", DEFAULTS["risk_pct"])), trades
