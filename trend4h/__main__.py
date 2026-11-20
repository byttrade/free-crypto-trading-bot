"""Command line: python -m trend4h fetch | run | report | start | live | close | panel | panel-user."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import backtest, data
from . import config as cfgmod
from .config import SPECS_DIR, STRATEGIES as SPECS
SUMMARY = ["trades", "profit_factor", "sum_R", "sum_R_without_top5", "win_pct", "max_dd_pct", "sharpe_daily",
           "fees_R", "funding_R", "median_stop_pct"]


def _iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")


def _dates(start, end):
    return f"{_iso(start)[:10]}..{_iso(end - 1)[:10]}"


def _write(path, header, rows):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


def cmd_fetch(a):
    coins = a.coins.split(",") if a.coins else data.COINS
    data.fetch(coins, a.start, a.end, a.data_dir)


def cmd_run(a):
    spec = json.loads(Path(a.spec).read_text())
    if a.config:
        cfg = cfgmod.load(a.config)
        spec = cfgmod.apply_to_spec(spec, cfg)
        for w in cfgmod.warnings(cfg):
            print(f"WARNING: {w}")
    if a.date_from or a.date_to:
        if not (a.date_from and a.date_to):
            sys.exit("--from and --to go together")
        ranges = {"custom": (data.day_ms(a.date_from), data.day_ms(a.date_to) + data.DAY)}
    else:
        ranges = backtest.PERIODS if a.period == "all" else {a.period: backtest.PERIODS[a.period]}
    all_trades = []
    print(spec.get("name", a.spec))
    for label, (start, end) in ranges.items():
        m, trades = backtest.run(spec, start, end, a.data_dir)
        all_trades += [{"period": label, **t, "entry_time": _iso(t["entry_ts"]), "exit_time": _iso(t["exit_ts"])}
                       for t in trades]
        print(f"[{label} {_dates(start, end)}] " + "  ".join(f"{k}={m.get(k)}" for k in SUMMARY))
        for group in ("by_half_year", "by_side"):
            print(f"   {group}: " + "  ".join(f"{k}: {v['sum_R']}R/{v['trades']}" for k, v in m.get(group, {}).items()))
    if a.trades and all_trades:
        with open(a.trades, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(all_trades[0]), lineterminator="\n")
            w.writeheader()
            w.writerows(all_trades)


def report(data_dir=data.DATA_DIR, specs_dir=SPECS_DIR) -> dict:
    """{strategy: {period: metrics}} for the four specs on IS and OOS."""
    out = {}
    for name in SPECS:
        spec = json.loads((Path(specs_dir) / f"{name}.json").read_text())
        out[name] = {p: backtest.run(spec, s, e, data_dir)[0] for p, (s, e) in backtest.PERIODS.items()}
    return out


def cmd_report(a):
    res = report(a.data_dir, a.specs)
    out = Path(a.out)
    out.mkdir(exist_ok=True)
    period_rows, half_rows, side_rows = [], [], []
    print(f"{'strategy':<16}{'period':<5}" + "".join(f"{k:>20}" for k in SUMMARY[:7]))
    for name, periods in res.items():
        for p, m in periods.items():
            label = p.upper()
            period_rows.append([name, label, _dates(*backtest.PERIODS[p])] + [m[k] for k in SUMMARY])
            half_rows += [[name, k, label, v["trades"], v["profit_factor"], v["sum_R"]] for k, v in m["by_half_year"].items()]
            side_rows += [[name, label, k, v["trades"], v["profit_factor"], v["sum_R"]] for k, v in m["by_side"].items()]
            print(f"{name:<16}{label:<5}" + "".join(f"{str(m[k]):>20}" for k in SUMMARY[:7]))
    _write(out / "metrics_by_period.csv", ["strategy", "period", "dates"] + SUMMARY, period_rows)
    _write(out / "by_half_year.csv", ["strategy", "half_year", "period", "trades", "profit_factor", "sum_R"], half_rows)
    _write(out / "by_side.csv", ["strategy", "period", "side", "trades", "profit_factor", "sum_R"], side_rows)
    print(f"written: {out}/metrics_by_period.csv, by_half_year.csv, by_side.csv")


def _cfg(a) -> dict:
    """Config file plus command-line overrides."""
    raw = json.loads(Path(a.config).read_text()) if a.config else {}
    for key in ("exchange", "account", "leverage", "sizing", "margin_mode"):
        if getattr(a, key, None) is not None:
            raw[key] = getattr(a, key)
    if getattr(a, "risk", None) is not None:
        raw.update(sizing="risk", risk_pct=a.risk)
    if getattr(a, "notional", None) is not None:
        raw.update(sizing="notional", notional_usdt=a.notional)
    for key in ("strategies", "coins"):
        v = getattr(a, key, None)
        if v and v != "all":
            raw[key] = v.split(",")
    return cfgmod.validate(raw)


def _confirm_real(cfg, phrase):
    from .live import ACCOUNT_PHRASE
    if cfg["account"] == "real" and phrase != ACCOUNT_PHRASE:
        sys.exit(f'real account: start refused without the confirmation phrase. Add --confirm-real "{ACCOUNT_PHRASE}" '
                 f"(or use `python -m trend4h start`). Results in this repository are a backtest, not a promise.")


def cmd_live(a):
    import asyncio
    import os
    import signal

    from . import live
    cfg = _cfg(a)
    print(live.DISCLAIMER)
    for w in cfgmod.warnings(cfg):
        print(f"WARNING: {w}")
    if not a.dry_run:
        _confirm_real(cfg, a.confirm_real)

    async def go():
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                asyncio.get_running_loop().add_signal_handler(sig, stop.set)
            except NotImplementedError:  # Windows
                pass
        return await live.serve(cfg, stop, dry_run=a.dry_run, state_dir=a.state_dir)

    if asyncio.run(go()) == "restart":  # bytx was updated: a fresh process loads the new files
        os.execv(sys.executable, [sys.executable, "-m", "trend4h", *sys.argv[1:]])


def cmd_close(a):
    import asyncio

    from . import integrity, live
    cfg = _cfg(a)
    live.load_env()

    async def go():
        from bytx import errors
        ex = live.connect(cfg)
        try:
            x = live.Executor(cfg, ex, errors, a.state_dir, guard=integrity.Guard(cfg["exchange"], ex.ccxt))
            await x.startup()
            await x.close_all()
        finally:
            await ex.close()

    asyncio.run(go())


def cmd_start(a):
    """Interactive wizard: asks the settings, shows a summary, waits for 'start'."""
    import asyncio

    from . import live

    def ask(prompt, default, choices=None):
        while True:
            v = input(f"{prompt} [{default}]: ").strip() or str(default)
            if not choices or v in choices:
                return v
            print(f"  choose one of: {', '.join(choices)}")

    print(live.DISCLAIMER + "\n")
    live.load_env()
    try:
        exchanges = live.supported_exchanges()
    except ImportError:
        sys.exit("the executor needs the bytx SDK: pip install -e '.[live]'")
    raw = {"exchange": ask(f"Exchange ({', '.join(exchanges)})", "bybit", exchanges),
           "account": ask("Account type (demo / real)", "demo", ["demo", "real"])}
    s = ask(f"Strategies (all or comma-separated: {', '.join(cfgmod.STRATEGIES)})", "all")
    if s != "all":
        raw["strategies"] = s.split(",")
    raw["sizing"] = ask("Position size: risk (% of equity per trade) or notional (fixed USDT per trade)", "risk", ["risk", "notional"])
    if raw["sizing"] == "risk":
        raw["risk_pct"] = float(ask("Risk per trade, % of equity (research: 0.056 per strategy)", cfgmod.DEFAULTS["risk_pct"]))
    else:
        raw["notional_usdt"] = float(ask("Notional per trade, USDT", cfgmod.DEFAULTS["notional_usdt"]))
    raw["leverage"] = float(ask("Leverage", cfgmod.DEFAULTS["leverage"]))
    raw["margin_mode"] = ask("Margin mode (cross / isolated)", "cross", ["cross", "isolated"])
    try:
        cfg = cfgmod.validate(raw)
    except ValueError as e:
        sys.exit(f"error: {e}")
    if not live.credentials(cfg["exchange"], cfg["account"]):
        pre = f"{cfg['exchange']}_{cfg['account']}".upper()
        sys.exit(f"no API keys found: put {pre}_API_KEY and {pre}_SECRET into .env (see .env.example) and run again")

    async def equity():
        from bytx import errors
        ex = live.connect(cfg)
        try:
            return await live.Executor(cfg, ex, errors, a.state_dir, dry_run=True, out=lambda s: None).equity()
        finally:
            await ex.close()

    eq = asyncio.run(equity())
    print(f"\nExchange: {cfg['exchange']}   account: {cfg['account']}   equity: {eq:,.2f} USDT")
    print(f"Strategies: {', '.join(cfg['strategies'])}   coins: {len(cfg['coins'])}   leverage: {cfg['leverage']:g}   margin: {cfg['margin_mode']}")
    if cfg["sizing"] == "risk":
        print(f"Risk per trade: {cfg['risk_pct']} % of equity = {eq * cfg['risk_pct'] / 100:,.2f} USDT at the current balance")
    else:
        print(f"Fixed notional per trade: {cfg['notional_usdt']:,.2f} USDT")
    for w in cfgmod.warnings(cfg):
        print(f"WARNING: {w}")
    print("\n" + live.DISCLAIMER)
    phrase = None
    if cfg["account"] == "real":
        print("\nREAL ACCOUNT: real money is at risk. The results in this repository are a backtest.")
        phrase = input(f'Type "{live.ACCOUNT_PHRASE}" to continue: ').strip()
        _confirm_real(cfg, phrase)
    if input('Type "start" to begin: ').strip() != "start":
        sys.exit("not started")
    path = Path(a.state_dir)
    path.mkdir(exist_ok=True)
    (path / "config.json").write_text(json.dumps(cfg, indent=1))
    argv = ["live", "--config", str(path / "config.json"), "--state-dir", a.state_dir]
    sys.argv = [sys.argv[0], *argv, *(["--confirm-real", phrase] if phrase else [])]
    main(sys.argv[1:])


def cmd_panel(a):
    from . import panel
    panel.main(a)


def cmd_panel_user(a):
    from . import panel
    panel.set_user(a.state_dir)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="trend4h", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="download public Binance USDT-M candles (4h, 1d) and funding")
    f.add_argument("--start", default=data.HISTORY_START)
    f.add_argument("--end", default="2026-06-30", help="last day to download, YYYY-MM-DD (default: end of the OOS period)")
    f.add_argument("--coins", help="comma-separated subset, e.g. BTC,ETH (default: all 20)")
    f.set_defaults(fn=cmd_fetch)

    r = sub.add_parser("run", help="backtest one spec")
    r.add_argument("spec", help="path to a spec, e.g. specs/st4h_ema50_1d.json")
    r.add_argument("--period", choices=["is", "oos", "all"], default="all")
    r.add_argument("--from", dest="date_from", help="custom range start, YYYY-MM-DD (instead of --period)")
    r.add_argument("--to", dest="date_to", help="custom range end, YYYY-MM-DD inclusive")
    r.add_argument("--trades", help="write the trade list to this CSV")
    r.add_argument("--config", help="apply your settings (coins, fee, slippage, sizing) from a config file")
    r.set_defaults(fn=cmd_run)

    p = sub.add_parser("report", help="backtest all four specs on IS and OOS, write results/*.csv")
    p.add_argument("--specs", default=str(SPECS_DIR))
    p.add_argument("--out", default="results")
    p.set_defaults(fn=cmd_report)

    w = sub.add_parser("start", help="interactive wizard: choose exchange, account and risk, then trade")
    w.set_defaults(fn=cmd_start)
    lv = sub.add_parser("live", help="run the executor without questions")
    lv.add_argument("--dry-run", action="store_true", help="compute signals and print the orders, send nothing")
    lv.add_argument("--confirm-real", help="confirmation phrase required for a real account")
    lv.set_defaults(fn=cmd_live)
    cl = sub.add_parser("close", help="close the positions opened by the executor (others are not touched)")
    cl.set_defaults(fn=cmd_close)
    for s in (lv, cl):
        s.add_argument("--config", help="settings file (see config.example.json)")
        s.add_argument("--exchange")
        s.add_argument("--account", choices=["demo", "real"])
        s.add_argument("--strategies", help="all or comma-separated names")
        s.add_argument("--coins", help="comma-separated tickers (default: the 20 coins of the research)")
        s.add_argument("--risk", type=float, help="risk per trade, %% of equity (default 0.056)")
        s.add_argument("--notional", type=float, help="fixed notional per trade in USDT instead of risk sizing")
        s.add_argument("--leverage", type=float)
        s.add_argument("--margin-mode", dest="margin_mode", choices=["cross", "isolated"])
    pn = sub.add_parser("panel", help="local control panel in the browser")
    pn.add_argument("--host", default="127.0.0.1")
    pn.add_argument("--port", type=int, default=8790)
    pn.add_argument("--certfile")
    pn.add_argument("--keyfile")
    pn.add_argument("--behind-https-proxy", action="store_true",
                    help="the panel is behind an HTTPS reverse proxy on this host: trust its X-Forwarded-Proto header")
    pn.set_defaults(fn=cmd_panel)
    pu = sub.add_parser("panel-user", help="set the login and password of the panel")
    pu.set_defaults(fn=cmd_panel_user)
    for s in (w, lv, cl, pn, pu):
        s.add_argument("--state-dir", default="state", help="where state, journal and locks live (default: state)")

    for s in (f, r, p):
        s.add_argument("--data-dir", default=str(data.DATA_DIR), help="candle cache directory (default: data)")
    a = ap.parse_args(argv)
    try:
        a.fn(a)
    except (FileNotFoundError, ValueError, RuntimeError, PermissionError) as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
