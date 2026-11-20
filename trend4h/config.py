"""User settings shared by the executor, the panel and the backtest (`run --config`).

Defaults are the parameters of the research. Backtest results in this repository apply to the defaults only.
"""
from __future__ import annotations

import json
from pathlib import Path

from .data import COINS

SPECS_DIR = Path(__file__).resolve().parent.parent / "specs"  # the repository checkout
STRATEGIES = ["st4h_ema50_1d", "kc2_4h_adx1d", "zmid1d_cloud4h", "bbw1d_lr4h"]
DEFAULTS = {
    "exchange": "bybit",
    "account": "demo",                 # demo | real
    "strategies": STRATEGIES,
    "coins": COINS,                    # the 20 coins of the research
    "sizing": "risk",                  # risk: risk_pct of equity per trade | notional: fixed notional_usdt per trade
    "risk_pct": 0.056,                 # % of account equity lost if the stop is hit (research: 0.056 per strategy)
    "notional_usdt": 100.0,            # used when sizing = notional
    "leverage": 3,
    "margin_mode": "cross",            # cross | isolated
    "max_positions": 20,               # simultaneously open positions of the executor
    "max_total_notional_usdt": None,   # cap on the sum of open notionals; null = no cap
    "fee": 0.0005,                     # backtest only: taker fee per side
    "slippage": 0.0002,                # backtest only: slippage per side
    "auto_update": "on",               # bytx auto-update: on | notify | off
    "update_margin_min": 60,           # do not update closer than this to the next 4h bar close (minimum 60)
}
RESEARCH_KEYS = ("strategies", "coins", "sizing", "risk_pct", "fee", "slippage")  # deviations void the published results


def validate(cfg: dict) -> dict:
    """Fill defaults and check values; ValueError with a readable message otherwise."""
    unknown = set(cfg) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"unknown settings: {sorted(unknown)}; known: {sorted(DEFAULTS)}")
    c = {**DEFAULTS, **cfg}
    c["exchange"] = str(c["exchange"]).lower()
    c["coins"] = [str(x).upper() for x in c["coins"]]

    def need(ok, msg):
        if not ok:
            raise ValueError(msg)

    need(c["account"] in ("demo", "real"), "account: demo or real")
    need(c["strategies"] and set(c["strategies"]) <= set(STRATEGIES), f"strategies: a non-empty subset of {STRATEGIES}")
    need(c["coins"] and all(x.isalnum() for x in c["coins"]), "coins: a non-empty list of tickers such as BTC")
    need(c["sizing"] in ("risk", "notional"), "sizing: risk or notional")
    need(0 < float(c["risk_pct"]) <= 5, "risk_pct: above 0 and at most 5 (% of equity per trade)")
    need(float(c["notional_usdt"]) > 0, "notional_usdt: above 0")
    need(1 <= float(c["leverage"]) <= 125, "leverage: from 1 to 125 (the exchange limit is checked at start)")
    need(c["margin_mode"] in ("cross", "isolated"), "margin_mode: cross or isolated")
    need(int(c["max_positions"]) >= 1, "max_positions: at least 1")
    need(c["max_total_notional_usdt"] is None or float(c["max_total_notional_usdt"]) > 0, "max_total_notional_usdt: above 0 or null")
    need(0 <= float(c["fee"]) < 0.01 and 0 <= float(c["slippage"]) < 0.01, "fee / slippage: fractions per side, e.g. 0.0005")
    need(c["auto_update"] in ("on", "notify", "off"), "auto_update: on, notify or off")
    need(int(c["update_margin_min"]) >= 60, "update_margin_min: at least 60")
    return c


def load(path=None) -> dict:
    return validate(json.loads(Path(path).read_text()) if path else {})


def warnings(cfg: dict) -> list[str]:
    """Honest notes about settings that differ from the research."""
    out = []
    changed = [k for k in RESEARCH_KEYS if cfg[k] != DEFAULTS[k]]
    if changed:
        out.append(f"settings differ from the research ({', '.join(changed)}): the published backtest results "
                   f"apply to the default settings only")
    extra = sorted(set(cfg["coins"]) - set(COINS))
    if extra:
        out.append(f"coins outside the 20 of the research ({', '.join(extra)}): the strategies are NOT confirmed there "
                   f"(on 639 other perpetuals the result was close to zero)")
    return out


def apply_to_spec(spec: dict, cfg: dict) -> dict:
    """Spec with the user's backtest-relevant settings."""
    out = {**spec, "coins": cfg["coins"], "fee": cfg["fee"], "slippage": cfg["slippage"], "risk_pct": cfg["risk_pct"]}
    if cfg["sizing"] == "notional":
        out["notional_usdt"] = cfg["notional_usdt"]
    return out
