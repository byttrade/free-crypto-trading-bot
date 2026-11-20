"""A tiny in-memory exchange with the bytx calls the executor uses. No network, no bytx needed."""
from types import SimpleNamespace as NS

import numpy as np

H4, D1 = 14_400_000, 86_400_000


class BytxError(Exception):
    pass


def _err(name, base=BytxError):
    return type(name, (base,), {})


errors = NS(BytxError=BytxError, NetworkError=_err("NetworkError"), OrderStatusUnknown=_err("OrderStatusUnknown"),
            ProtectionNotPlaced=_err("ProtectionNotPlaced"), OrderTooSmall=_err("OrderTooSmall"),
            PositionAlreadyClosed=_err("PositionAlreadyClosed"))
errors.MinSizeExceedsRisk = _err("MinSizeExceedsRisk", errors.OrderTooSmall)


class FakeExchange:
    def __init__(self, price=100.0, equity=10_000.0, min_size=0.001):
        self.price, self.equity, self.min_size = price, equity, min_size
        self.pos, self.legs, self.orders = {}, {}, {}     # (symbol, side) -> size; id -> leg; client_id -> order
        self.sent, self.fail, self.n = [], {}, 0          # every state-changing call; method -> exception to raise once
        self.ccxt = object()

    def _call(self, name, *a):
        self.sent.append((name, *a))
        if name in self.fail:
            raise self.fail.pop(name)

    def _id(self):
        self.n += 1
        return str(self.n)

    async def balance(self):
        return NS(total=lambda cur: self.equity if cur == "USDT" else 0.0)

    async def is_hedge_mode(self):
        return False

    async def market(self, symbol):
        if symbol.startswith("NOPE"):
            raise KeyError(symbol)
        return NS(symbol=symbol + ":USDT", max_leverage=50.0)

    async def ticker(self, symbol):
        return NS(last=self.price)

    async def positions(self):
        return [NS(symbol=s, side=side, size=size) for (s, side), size in self.pos.items() if size]

    async def open_position(self, symbol, side, *, risk_usdt, stop, take_profit=None, leverage=None, client_id=None):
        size = risk_usdt / abs(self.price - stop)
        if size < self.min_size:
            raise errors.MinSizeExceedsRisk("minimum order risks more")
        timeout = self.fail.get("open_position_after")
        self._call("open_position", symbol, side, client_id)
        self.pos[(symbol, side)] = size
        self.orders[client_id] = NS(filled=size, average=self.price, raw={"info": {}})
        if timeout:  # the order went through but the answer was lost
            del self.fail["open_position_after"]
            raise timeout
        sl = self._leg(symbol, side, stop_loss=stop)
        tp = self._leg(symbol, side, take_profit=take_profit) if take_profit else None
        return NS(status="opened", average=self.price, filled_size=size, stop=sl, take_profit=tp, warnings=[],
                  entry=self.orders[client_id])

    def _leg(self, symbol, side, stop_loss=None, take_profit=None):
        leg = NS(id=self._id(), symbol=symbol, position_side=side, stop_loss=stop_loss, take_profit=take_profit, state="open")
        self.legs[leg.id] = leg
        return leg

    async def tp_sl_orders(self, symbol, side=None):
        return [t for t in self.legs.values() if t.symbol == symbol and t.position_side == side and t.state == "open"]

    async def tp_sl_order(self, symbol, order_id):
        return self.legs[order_id]

    async def place_tp_sl(self, symbol, kind, trigger, *, position_side, size=None):
        self._call("place_tp_sl", symbol, kind, trigger)
        return self._leg(symbol, position_side, **{kind: trigger})

    async def modify_tp_sl(self, symbol, order_id, *, stop_loss):
        self._call("modify_tp_sl", symbol, stop_loss)
        self.legs[order_id].state = "canceled"
        return NS(new="created", old="canceled", order=self._leg(symbol, self.legs[order_id].position_side, stop_loss=stop_loss))

    async def cancel_tp_sl(self, symbol, *, side=None):
        self._call("cancel_tp_sl", symbol, side)
        for t in await self.tp_sl_orders(symbol, side):
            t.state = "canceled"

    async def reduce_position(self, symbol, side, *, size):
        self._call("reduce_position", symbol, side)
        if not self.pos.get((symbol, side)):
            raise errors.PositionAlreadyClosed("no position")
        self.pos[(symbol, side)] = 0
        return NS(average=self.price)

    async def find_order(self, symbol, client_id, timeout=8):
        return self.orders.get(client_id)

    def trigger(self, symbol, side, kind):
        """The exchange fills a stop or take-profit: the position is gone, the leg is 'triggered'."""
        self.pos[(symbol, side)] = 0
        for t in self.legs.values():
            if t.symbol == symbol and t.state == "open" and getattr(t, kind) is not None:
                t.state = "triggered"


def flat_candles(price=100.0, rng=1.0):
    """candles(coin, tf, limit, now_ms): a flat market, last bar = the bar that closed before now."""
    def candles(coin, tf, limit, now_ms):
        step = H4 if tf == "4h" else D1
        last = now_ms // step * step - step
        ts = (last - step * np.arange(59, -1, -1)).astype(np.int64)
        c = np.full(60, price)
        return {"ts": ts, "open": c.copy(), "high": c + rng, "low": c - rng, "close": c.copy()}
    return candles
