"""Executor: trades the strategy signals on an exchange account through the bytx SDK.

Signals are the same code as the backtest (trend4h.indicators), computed on public Binance USDT-M candles.
Once per closed 4h bar the executor
  1. reconciles its positions with the exchange (state is kept in local files, so a restart continues),
  2. moves trailing stops,
  3. opens new positions: market entry sized by risk, stop-loss placed on the exchange with the entry
     (if the stop cannot be placed the position is closed), take-profit as a second exchange order.
Rules
- an order is never re-sent after a timeout or a dropped connection: the outcome is looked up by client_id;
- one position per coin; a coin with a position the executor did not open is left alone;
- every entry has a stop on the exchange; the stop cannot be disabled;
- no new orders while the bytx integrity / broker attribution checks fail (existing stops stay on the exchange).
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import numpy as np

from . import config as cfgmod
from . import data
from .indicators import TF_MS, atr, entry_signal

STEP = TF_MS["4h"]
TAG = "7e4d"                 # client_id tag of this executor (hex digits: valid on every exchange)
BAR_DELAY_MS = 20_000        # wait after the bar close before reading candles
LATE_MS = 20 * 60_000        # entries are skipped later than this after the bar open (the price has moved on)
RECONCILE_SEC = 300
ACCOUNT_PHRASE = "I ACCEPT THE RISK"
DISCLAIMER = (
    "HIGH RISK. Trading perpetual futures can lose your entire deposit. BYTtrade is not responsible for the results "
    "or losses. Backtest and paper results do not guarantee future returns. This is not investment advice. "
    "You act at your own risk.")


def load_env(path=".env"):
    """KEY=VALUE lines of a local .env into the process environment (existing variables win)."""
    p = Path(path)
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def credentials(exchange: str, account: str) -> dict:
    """create_exchange keyword arguments from <EXCHANGE>_<ACCOUNT>_* variables; {} if no key is set."""
    pre = f"{exchange}_{account}_".upper()
    names = {"api_key": "API_KEY", "secret": "SECRET", "password": "PASSWORD",
             "wallet_address": "WALLET_ADDRESS", "private_key": "PRIVATE_KEY"}
    return {arg: os.environ[pre + env] for arg, env in names.items() if os.environ.get(pre + env)}


def supported_exchanges() -> list[str]:
    from bytx.testing import EXCHANGES  # every exchange bytx trades USDT perpetuals on
    return list(EXCHANGES)


def connect(cfg: dict):
    """bytx exchange client for the configured account. Keys come from the environment only."""
    from bytx import create_exchange
    keys = credentials(cfg["exchange"], cfg["account"])
    if not keys:
        raise ValueError(f"no API keys for {cfg['exchange']} {cfg['account']}: set "
                         f"{cfg['exchange'].upper()}_{cfg['account'].upper()}_API_KEY and ..._SECRET in .env")
    return create_exchange(cfg["exchange"], demo=cfg["account"] == "demo", margin_mode=cfg["margin_mode"], **keys)


def load_specs(names, specs_dir=cfgmod.SPECS_DIR) -> dict:
    return {n: json.loads((Path(specs_dir) / f"{n}.json").read_text()) for n in names}


def decide(specs: dict, bars: dict) -> dict | None:
    """Entry decision for one coin on its last closed 4h bar: the first strategy with a signal wins, long first."""
    b = bars["4h"]
    for name, spec in specs.items():
        for side, key in ((1, "long"), (-1, "short")):
            if spec.get(key) and entry_signal(spec[key], bars, "4h")[-1]:
                ex = spec["exit"]
                a = atr(b["high"], b["low"], b["close"], int(ex.get("atr_period", 14)))[-1]
                if a > 0:
                    return {"strategy": name, "side": side, "atr": float(a), "sl": ex["sl"]["atr"],
                            "tp": ex.get("tp", {}).get("atr"), "trail": ex.get("trail", {}).get("atr")}
    return None


def levels(d: dict, price: float) -> dict:
    """Stop, take-profit and trail distance for an entry at `price` (same arithmetic as the backtest)."""
    side, a = d["side"], d["atr"]
    stop = price - side * d["sl"] * a
    trail = d["trail"] * a if d["trail"] else None
    if trail:
        stop = side * max(side * stop, side * (price - side * trail))
    return {"stop": stop, "tp": price + side * d["tp"] * a if d["tp"] else None, "trail": trail}


class Lock:
    """One executor per state directory."""

    def __init__(self, path):
        self.fh = open(path, "a+")
        try:
            import fcntl
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:  # Windows
            import msvcrt
            msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self.fh.close()
            raise RuntimeError(f"another executor already runs on {path}") from None

    def release(self):
        self.fh.close()


class Executor:
    def __init__(self, cfg, ex, errors, state_dir="state", guard=None, dry_run=False, candles=data.recent,
                 clock=lambda: int(time.time() * 1000), specs=None, out=print, new_cid=None):
        self.cfg, self.ex, self.err, self.guard, self.dry = cfg, ex, errors, guard, dry_run
        self.candles, self.clock, self.out = candles, clock, out
        self.specs = specs or load_specs(cfg["strategies"])
        self.dir = Path(state_dir) / f"{cfg['exchange']}-{cfg['account']}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_file, self.journal = self.dir / "state.json", self.dir / "journal.jsonl"
        self.st = json.loads(self.state_file.read_text()) if self.state_file.exists() else {"last_bar": 0, "pos": {}, "closed": []}
        self.new_cid = new_cid or (lambda: __import__("bytx").generate_client_id(ex, prefix=TAG))
        self.coins, self.symbols = list(cfg["coins"]), {}
        self.busy = 0                # > 0 while exchange requests are in flight (the updater waits for it)
        self.last_bar_done = 0.0     # wall time when the last bar was processed
        self.blocked = None

    # ------------------------------------------------------------ state and journal

    def log(self, event, **kw):
        rec = {"t": self.clock(), "ev": event, **kw}
        self.out(json.dumps(rec, ensure_ascii=False, default=float))
        if not self.dry:
            with open(self.journal, "a") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False, default=float) + "\n")

    def save(self):
        if not self.dry:
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.st, indent=1, default=float))
            tmp.replace(self.state_file)

    def symbol(self, coin):
        return self.symbols.get(coin, f"{coin}/USDT")  # the exchange's own symbol once the market is known

    def gate(self, force=False):
        """Integrity and attribution gate; returns the reason new orders are blocked, or None."""
        problem = self.guard.problem(force) if self.guard else None
        if problem != self.blocked:
            self.log("orders_blocked" if problem else "orders_unblocked", reason=problem)
        self.blocked = problem
        return problem

    # ------------------------------------------------------------ start-up

    async def startup(self):
        """Checks before the first order; ValueError with a readable message if the account is not usable."""
        cfg = self.cfg
        if self.ex is None:  # dry run without keys: public data only
            return self.log("start", dry_run=True, exchange=cfg["exchange"], warnings=cfgmod.warnings(cfg))
        if self.guard:
            self.log("checks", **self.guard.describe())
            problem = self.gate(force=True)
            if problem:
                raise ValueError(problem)
        try:
            equity = await self.equity()
        except self.err.BytxError as e:
            raise ValueError(f"cannot read the account balance (check the API keys and their permissions): {e}") from None
        if equity <= 0:
            raise ValueError("the account has no USDT balance")
        try:
            hedge = await self.ex.is_hedge_mode()
        except self.err.BytxError as e:
            raise ValueError(f"position mode is not usable: {e}. Switch the account to one-way mode.") from None
        coins = []
        for coin in cfg["coins"]:
            try:
                m = await self.ex.market(self.symbol(coin))
            except Exception:
                self.log("coin_skipped", coin=coin, reason="not listed on this exchange")
                continue
            if m.max_leverage and cfg["leverage"] > m.max_leverage:
                raise ValueError(f"leverage {cfg['leverage']} is above the exchange limit {m.max_leverage:g} for {coin}: lower it")
            coins.append(coin)
            self.symbols[coin] = m.symbol
        if not coins:
            raise ValueError("none of the configured coins is listed on this exchange")
        self.coins = coins
        self.log("start", exchange=cfg["exchange"], account=cfg["account"], equity=round(equity, 2),
                 position_mode="hedge" if hedge else "one-way", coins=len(coins), strategies=cfg["strategies"],
                 sizing=cfg["sizing"], leverage=cfg["leverage"], warnings=cfgmod.warnings(cfg))
        await self.reconcile()

    async def equity(self) -> float:
        bal = await self.ex.balance()
        # VST is the virtual USDT of the BingX demo account
        return float(bal.total("USDT") or bal.total("USDC") or bal.total("VST") or 0.0)

    async def _fill_price(self, sym, order):
        """Average fill price of a market order; exchanges that answer without it are asked once more by order id."""
        if order.average:
            return order.average
        try:
            return (await self.ex.order(sym, order.id)).average
        except self.err.BytxError:
            return None

    # ------------------------------------------------------------ reconciliation

    async def _exchange_positions(self) -> dict:
        """{(coin, side): position} of everything open on the account."""
        return {(p.symbol.split("/")[0], p.side): p for p in await self.ex.positions() if p.size}

    async def _stop_order(self, pos):
        ps = "long" if pos["side"] == 1 else "short"
        legs = await self.ex.tp_sl_orders(self.symbol(pos["coin"]), ps)
        return next((t for t in legs if t.stop_loss is not None), None)

    async def _close(self, pos, reason):
        """Close our position with a reduce-only market order and remove its TP/SL."""
        sym, ps = self.symbol(pos["coin"]), "long" if pos["side"] == 1 else "short"
        price = None
        try:
            o = await self.ex.reduce_position(sym, ps, size=pos["size"])
            price = await self._fill_price(sym, o)
        except self.err.PositionAlreadyClosed:
            pass
        except self.err.BytxError as e:  # not re-sent: the next reconciliation looks at the exchange again
            self.log("close_failed", coin=pos["coin"], reason=reason, error=str(e)[:300])
            return False
        await self._finish(pos, reason, price)
        return True

    async def _finish(self, pos, reason, price):
        sym, ps = self.symbol(pos["coin"]), "long" if pos["side"] == 1 else "short"
        try:
            await self.ex.cancel_tp_sl(sym, side=ps)  # the coin is ours alone, so are its TP/SL
        except self.err.BytxError as e:
            self.log("cancel_failed", coin=pos["coin"], error=str(e)[:300])
        pnl = pos["side"] * pos["size"] * (price - pos["entry"]) if price and pos.get("entry") else None
        rec = {"strategy": pos["strategy"], "coin": pos["coin"], "side": "long" if pos["side"] == 1 else "short",
               "entry_ts": pos["bar"], "exit_ts": self.clock(), "entry": pos.get("entry"), "exit": price,
               "size": pos.get("size"), "reason": reason, "pnl": pnl,
               "R": pnl / pos["risk_usdt"] if pnl is not None and pos.get("risk_usdt") else None}
        self.st["pos"].pop(pos["coin"], None)
        self.st["closed"].append(rec)
        self.log("exit", **rec)
        self.save()

    async def _ensure_stop(self, pos):
        """Every open position has a stop on the exchange; if one cannot be placed the position is closed."""
        stop = await self._stop_order(pos)
        if stop is not None:
            pos["stop_id"] = stop.id
            return True
        ps = "long" if pos["side"] == 1 else "short"
        try:
            t = await self.ex.place_tp_sl(self.symbol(pos["coin"]), "stop_loss", pos["stop"], position_side=ps, size=pos["size"])
            pos["stop_id"] = t.id
            self.log("stop_placed", coin=pos["coin"], stop=pos["stop"], id=t.id)
            self.save()
            return True
        except self.err.BytxError as e:
            self.log("stop_not_placed", coin=pos["coin"], error=str(e)[:300])
        if await self._stop_order(pos) is None:  # looked up once more: the request may have gone through
            await self._close(pos, "no_stop")
            return False
        return True

    async def reconcile(self):
        """Bring the local state in line with the exchange. Safe to run at any time, also right after a restart."""
        if self.dry or not self.st["pos"]:
            return
        self.busy += 1
        try:
            live = await self._exchange_positions()
            for coin, pos in list(self.st["pos"].items()):
                ps = "long" if pos["side"] == 1 else "short"
                on_exchange = live.get((coin, ps))
                if pos["status"] == "unknown":  # entry outcome was not known: look the order up, never re-send
                    order = await self.ex.find_order(self.symbol(coin), pos["cid"], timeout=8)
                    if order is not None and order.filled and on_exchange:
                        pos.update(status="open", entry=order.average, size=on_exchange.size)
                        self.log("entry_found", coin=coin, cid=pos["cid"], fill=order.average, size=on_exchange.size)
                        self.save()
                    elif self.clock() - pos["bar"] > LATE_MS:
                        self.log("entry_lost", coin=coin, cid=pos["cid"])
                        del self.st["pos"][coin]
                        self.save()
                        continue
                    else:
                        continue
                if on_exchange is None:  # closed by its stop or take-profit (or by hand)
                    reason, price = "closed", None
                    try:
                        if pos.get("stop_id") and (await self.ex.tp_sl_order(self.symbol(coin), pos["stop_id"])).state == "triggered":
                            reason, price = "stop", pos["stop"]
                    except self.err.BytxError:
                        pass
                    if reason == "closed" and pos.get("tp"):
                        reason, price = "tp", pos["tp"]  # the stop did not fire: the take-profit level is the estimate
                    await self._finish(pos, reason, price)
                else:
                    pos["size"] = on_exchange.size
                    pos["entry"] = pos.get("entry") or on_exchange.entry_price  # the entry answer had no fill price
                    await self._ensure_stop(pos)
        finally:
            self.busy -= 1

    # ------------------------------------------------------------ bar cycle

    def _bars(self, coin, bar):
        """Closed candles up to the bar that has just closed, or None if the exchange has not published it yet."""
        now = self.clock()
        b = {"4h": self.candles(coin, "4h", 1500, now), "1d": self.candles(coin, "1d", 500, now)}
        return b if len(b["4h"]["ts"]) and b["4h"]["ts"][-1] == bar - STEP else None

    async def _trail(self, pos, bars):
        if not pos.get("trail") or pos["status"] != "open":
            return
        side, b = pos["side"], bars["4h"]
        seen = b["ts"] >= pos["bar"]
        if not seen.any():
            return
        best = max(pos["best"], b["high"][seen].max()) if side == 1 else min(pos["best"], b["low"][seen].min())
        stop = best - side * pos["trail"]
        pos["best"] = float(best)
        if (stop - pos["stop"]) * side <= 0 or not pos.get("stop_id"):
            return
        try:
            r = await self.ex.modify_tp_sl(self.symbol(pos["coin"]), pos["stop_id"], stop_loss=float(stop))
        except self.err.BytxError as e:
            self.log("trail_failed", coin=pos["coin"], error=str(e)[:300])
            r = None
        if r is not None and r.new == "created":
            pos.update(stop=float(stop), stop_id=r.order.id if r.order else pos["stop_id"])
            self.log("trail", coin=pos["coin"], stop=float(stop))
        self.save()
        await self._ensure_stop(pos)  # whatever happened to the move, a stop must be on the exchange

    async def _enter(self, coin, d, bar, equity, foreign):
        cfg, sym = self.cfg, self.symbol(coin)
        side_name = "long" if d["side"] == 1 else "short"
        price = float((await self.ex.ticker(sym)).last) if self.ex else d["ref_price"]
        lv = levels(d, price)
        dist = abs(price - lv["stop"])
        risk = equity * cfg["risk_pct"] / 100 if cfg["sizing"] == "risk" else cfg["notional_usdt"] * dist / price
        notional = risk * price / dist
        order = {"strategy": d["strategy"], "coin": coin, "side": side_name, "ref_price": price, "stop": lv["stop"],
                 "take_profit": lv["tp"], "risk_usdt": round(risk, 4), "notional_usdt": round(notional, 2)}
        open_notional = sum(p.get("notional", 0) for p in self.st["pos"].values())
        if coin in foreign:
            return self.log("entry_skipped", reason="the account already has a position on this coin that is not ours", **order)
        if len(self.st["pos"]) >= cfg["max_positions"]:
            return self.log("entry_skipped", reason="max_positions reached", **order)
        if cfg["max_total_notional_usdt"] and open_notional + notional > cfg["max_total_notional_usdt"]:
            return self.log("entry_skipped", reason="max_total_notional_usdt reached", **order)
        if self.dry:
            return self.log("dry_run_order", **order)
        cid = self.new_cid()
        pos = self.st["pos"][coin] = {"strategy": d["strategy"], "coin": coin, "side": d["side"], "bar": bar, "cid": cid,
                                      "stop": lv["stop"], "tp": lv["tp"], "trail": lv["trail"], "best": price,
                                      "risk_usdt": risk, "notional": notional, "status": "sending"}
        self.save()  # the intent is on disk before anything is sent
        e = self.err
        try:
            r = await self.ex.open_position(sym, side_name, risk_usdt=risk, stop=lv["stop"], take_profit=lv["tp"],
                                            leverage=cfg["leverage"], client_id=cid)
        except (e.MinSizeExceedsRisk, e.OrderTooSmall) as x:
            del self.st["pos"][coin]
            self.save()
            return self.log("entry_skipped", reason=f"below the exchange minimum order: {str(x)[:200]}", **order)
        except e.ProtectionNotPlaced as x:  # bytx already closed the position; reconciliation confirms it
            pos["status"] = "unknown"
            self.save()
            self.log("stop_not_placed", coin=coin, cid=cid, error=str(x)[:300])
            return await self.reconcile()  # if the position is still there it gets a stop or is closed now
        except (e.OrderStatusUnknown, e.NetworkError) as x:  # may have been placed: never re-send, look up by client_id
            pos["status"] = "unknown"
            self.save()
            return self.log("entry_unknown", coin=coin, cid=cid, error=str(x)[:300])
        except e.BytxError as x:
            del self.st["pos"][coin]
            self.save()
            return self.log("entry_rejected", coin=coin, cid=cid, error=str(x)[:300])
        if r.status != "opened":
            del self.st["pos"][coin]
            self.save()
            return self.log("entry_not_filled", coin=coin, cid=cid)
        fill = await self._fill_price(sym, r.entry) if r.entry else r.average
        pos.update(status="open", entry=fill, size=r.filled_size, best=fill or price,
                   stop_id=r.stop.id if r.stop else None)
        self.save()
        self.log("entry", cid=cid, fill=r.average, size=r.filled_size, warnings=list(r.warnings), **order)
        if self.guard:
            self.guard.check_order(r.entry)
        await self._ensure_stop(pos)

    async def on_bar(self, bar):
        """Everything that happens once per closed 4h bar. `bar` is the open time of the bar that has just started."""
        self.busy += 1
        try:
            await self.reconcile()
            blocked = self.gate()
            late = self.clock() - bar > LATE_MS
            foreign, equity = set(), 10_000.0
            if self.ex:
                foreign = {coin for coin, _ in await self._exchange_positions()} - set(self.st["pos"])
                equity = await self.equity()
            missing = []
            for coin in self.coins:
                bars = await asyncio.to_thread(self._bars, coin, bar)
                if bars is None:
                    missing.append(coin)
                    continue
                pos = self.st["pos"].get(coin)
                if pos:
                    if not blocked and not self.dry:
                        await self._trail(pos, bars)
                    continue
                d = decide(self.specs, bars)
                if not d:
                    continue
                d["ref_price"] = float(bars["4h"]["close"][-1])
                if blocked:
                    self.log("entry_skipped", coin=coin, strategy=d["strategy"], reason=f"orders blocked: {blocked[:120]}")
                elif late and not self.dry:
                    self.log("entry_skipped", coin=coin, strategy=d["strategy"], reason="too late after the bar open")
                else:
                    await self._enter(coin, d, bar, equity, foreign)
            if missing and not late:
                return self.log("bar_incomplete", bar=bar, missing=missing)  # retried on the next loop pass
            self.st["last_bar"] = bar
            self.save()
            self.last_bar_done = time.time()
            self.log("bar", bar=bar, open_positions=len(self.st["pos"]), no_data=missing)
        finally:
            self.busy -= 1

    async def run(self, stop: asyncio.Event, poll_sec=30, on_idle=None):
        """Main loop until `stop` is set. Positions and their stops stay on the exchange when it ends."""
        await self.startup()
        last_reconcile = time.time()
        while not stop.is_set():
            now = self.clock()
            bar = now // STEP * STEP
            try:
                if bar > self.st["last_bar"] and now - bar >= BAR_DELAY_MS:
                    await self.on_bar(bar)
                    last_reconcile = time.time()
                elif time.time() - last_reconcile > RECONCILE_SEC:
                    self.gate()
                    await self.reconcile()
                    last_reconcile = time.time()
            except self.err.BytxError as e:  # network trouble etc.: nothing is re-sent, the next pass reconciles
                self.log("cycle_error", error=str(e)[:300])
            if on_idle and await on_idle(self) == "restart":
                return "restart"
            try:
                await asyncio.wait_for(stop.wait(), poll_sec)
            except asyncio.TimeoutError:
                pass
        self.log("stopped", open_positions=len(self.st["pos"]))

    async def close_all(self):
        """Close every position this executor opened (others are not touched)."""
        for pos in list(self.st["pos"].values()):
            if pos["status"] == "open":
                await self._close(pos, "manual")

    def summary(self) -> dict:
        closed = self.st["closed"]
        return {"open": list(self.st["pos"].values()), "closed": closed[-20:], "trades": len(closed),
                "sum_R": round(sum(c["R"] or 0 for c in closed), 2), "pnl_usdt": round(sum(c["pnl"] or 0 for c in closed), 2),
                "last_bar": self.st["last_bar"], "next_bar": (self.clock() // STEP + 1) * STEP, "blocked": self.blocked}


async def serve(cfg, stop: asyncio.Event, dry_run=False, state_dir="state", out=print, on_executor=None):
    """Connect, check, and run the executor until `stop`. Returns 'restart' when bytx was updated."""
    from . import integrity, update
    load_env()
    keys = credentials(cfg["exchange"], cfg["account"])
    if dry_run and not keys:
        x = Executor(cfg, None, None, state_dir, dry_run=True, out=out)
        await x.startup()
        return await x.on_bar(x.clock() // STEP * STEP)
    integrity.check_files("bytx")  # before bytx gets the keys
    from bytx import errors
    ex = connect(cfg)
    lock = None if dry_run else Lock(Path(state_dir) / f"{cfg['exchange']}-{cfg['account']}.lock")
    try:
        x = Executor(cfg, ex, errors, state_dir, guard=integrity.Guard(cfg["exchange"], ex.ccxt), dry_run=dry_run, out=out)
        if on_executor:
            on_executor(x)
        if dry_run:
            await x.startup()
            return await x.on_bar(x.clock() // STEP * STEP)
        updater = update.Updater(cfg["auto_update"], cfg["update_margin_min"], state_dir, log=x.log)
        x.updater = updater

        async def idle(executor):
            return await asyncio.to_thread(updater.run, executor)

        return await x.run(stop, on_idle=idle)
    finally:
        await ex.close()
        if lock:
            lock.release()
