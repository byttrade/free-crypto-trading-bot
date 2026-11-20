"""Executor on a fake exchange: entry, stop, exit, timeout without re-sending, restart."""
import asyncio

import pytest

from trend4h import config, live

from fakes import H4, FakeExchange, errors, flat_candles

SYM = "BTC/USDT:USDT"
BAR = 1000 * H4
SIGNAL = {"strategy": "st4h_ema50_1d", "side": 1, "atr": 2.0, "sl": 2, "tp": 10, "trail": 5}


class Rig:
    def __init__(self, tmp_path, monkeypatch, ex=None, signal=SIGNAL, **cfg):
        self.ex = ex or FakeExchange()
        self.now = BAR + 30_000
        self.tmp, self.lines = tmp_path, []
        self.signal = dict(signal) if signal else None
        monkeypatch.setattr(live, "decide", lambda specs, bars: dict(self.signal) if self.signal else None)
        self.cfg = config.validate({"coins": ["BTC"], "risk_pct": 1.0, **cfg})
        self.candles = flat_candles()

    def executor(self, **kw):
        n = iter(range(10**6))
        return live.Executor(self.cfg, self.ex, errors, self.tmp, candles=lambda *a: self.candles(*a),
                             clock=lambda: self.now, specs={}, out=self.lines.append,
                             new_cid=lambda: f"{live.TAG}{next(n):04d}", **kw)

    def events(self, name):
        import json
        return [e for e in map(json.loads, self.lines) if e["ev"] == name]


def run(coro):
    return asyncio.run(coro)


async def started(rig, **kw):
    x = rig.executor(**kw)
    await x.startup()
    return x


def test_entry_is_sized_by_risk_and_protected_on_the_exchange(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)

    async def go():
        x = await started(rig)
        await x.on_bar(BAR)
        return x
    x = run(go())
    pos = x.st["pos"]["BTC"]
    assert pos["status"] == "open" and pos["stop"] == 96.0 and pos["tp"] == 120.0
    assert pos["size"] == pytest.approx(100 / 4)  # 1 % of 10 000 over a 4-point stop
    assert rig.ex.pos[(SYM, "long")] == pytest.approx(25)
    legs = run(rig.ex.tp_sl_orders(SYM, "long"))
    assert {(t.stop_loss, t.take_profit) for t in legs} == {(96.0, None), (None, 120.0)}
    assert pos["cid"].startswith(live.TAG)
    assert (tmp_path / "bybit-demo" / "state.json").exists() and rig.events("entry")


def test_stop_fires_then_exit_is_recorded_and_leftover_tp_cancelled(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)

    async def go():
        x = await started(rig)
        await x.on_bar(BAR)
        rig.ex.trigger(SYM, "long", "stop_loss")
        rig.signal = None
        await x.reconcile()
        return x
    x = run(go())
    (closed,) = x.st["closed"]
    assert x.st["pos"] == {} and closed["reason"] == "stop" and closed["R"] == pytest.approx(-1)
    assert run(rig.ex.tp_sl_orders(SYM, "long")) == []  # the take-profit did not stay behind


def test_take_profit_exit(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)

    async def go():
        x = await started(rig)
        await x.on_bar(BAR)
        rig.ex.trigger(SYM, "long", "take_profit")
        await x.reconcile()
        return x
    (closed,) = run(go()).st["closed"]
    assert closed["reason"] == "tp" and closed["R"] == pytest.approx(5)


def test_trailing_stop_moves_only_in_favour(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)

    async def go():
        x = await started(rig)
        await x.on_bar(BAR)
        rig.candles = flat_candles(price=100.0, rng=20.0)  # the bar after entry made a high of 120
        rig.now += H4
        await x.on_bar(BAR + H4)
        first = x.st["pos"]["BTC"]["stop"]
        rig.candles = flat_candles(price=100.0, rng=1.0)
        rig.now += H4
        await x.on_bar(BAR + 2 * H4)
        return x, first
    x, first = run(go())
    assert first == 110.0 and x.st["pos"]["BTC"]["stop"] == 110.0  # 120 - 5 ATR, never moved back
    stops = [t.stop_loss for t in run(rig.ex.tp_sl_orders(SYM, "long")) if t.stop_loss]
    assert stops == [110.0]


def test_timeout_is_never_resent_and_order_is_found_by_client_id(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.ex.fail["open_position_after"] = errors.OrderStatusUnknown("timeout")

    async def go():
        x = await started(rig)
        await x.on_bar(BAR)
        assert x.st["pos"]["BTC"]["status"] == "unknown"
        await x.reconcile()
        rig.now += H4
        await x.on_bar(BAR + H4)
        return x
    x = run(go())
    assert [c[0] for c in rig.ex.sent].count("open_position") == 1  # one request, whatever happened to the answer
    pos = x.st["pos"]["BTC"]
    assert pos["status"] == "open" and rig.events("entry_unknown") and rig.events("entry_found")
    assert [t.stop_loss for t in run(rig.ex.tp_sl_orders(SYM, "long")) if t.stop_loss] == [96.0]  # stop placed after the lookup


def test_network_error_without_order_is_dropped_not_retried(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.ex.fail["open_position"] = errors.NetworkError("connection reset")  # raised before the order exists

    async def go():
        x = await started(rig)
        await x.on_bar(BAR)
        rig.signal = None
        rig.now = BAR + live.LATE_MS + 60_000
        await x.reconcile()
        return x
    x = run(go())
    assert x.st["pos"] == {} and rig.events("entry_lost")
    assert [c[0] for c in rig.ex.sent].count("open_position") == 1


def test_restart_continues_from_files_and_does_not_enter_twice(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)

    async def go():
        x = await started(rig)
        await x.on_bar(BAR)
        y = await started(rig)  # a new process: state comes from disk, positions from the exchange
        assert y.st["pos"]["BTC"]["status"] == "open" and y.st["last_bar"] == BAR
        rig.now += H4
        await y.on_bar(BAR + H4)
        rig.ex.trigger(SYM, "long", "stop_loss")
        z = await started(rig)  # closed while the executor was down
        return z
    z = run(go())
    assert [c[0] for c in rig.ex.sent].count("open_position") == 1
    assert z.st["pos"] == {} and z.st["closed"][0]["reason"] == "stop"


def test_missing_stop_is_restored_or_the_position_is_closed(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)

    async def go():
        x = await started(rig)
        await x.on_bar(BAR)
        for t in rig.ex.legs.values():
            t.state = "canceled"  # somebody removed the stop
        await x.reconcile()
        assert [t.stop_loss for t in await rig.ex.tp_sl_orders(SYM, "long")] == [96.0]
        for t in rig.ex.legs.values():
            t.state = "canceled"
        rig.ex.fail["place_tp_sl"] = errors.BytxError("rejected")
        await x.reconcile()
        return x
    x = run(go())
    assert x.st["pos"] == {} and x.st["closed"][0]["reason"] == "no_stop" and not rig.ex.pos[(SYM, "long")]


def test_below_exchange_minimum_is_skipped_with_a_journal_record(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, ex=FakeExchange(min_size=1000))
    x = run(started(rig))
    run(x.on_bar(BAR))
    assert x.st["pos"] == {} and "minimum" in rig.events("entry_skipped")[0]["reason"]
    assert (tmp_path / "bybit-demo" / "journal.jsonl").read_text().count("entry_skipped") == 1


def test_foreign_position_is_left_alone(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.ex.pos[(SYM, "short")] = 3.0
    rig.ex._leg(SYM, "short", stop_loss=130.0)

    async def go():
        x = await started(rig)
        await x.on_bar(BAR)
        await x.close_all()
        return x
    x = run(go())
    assert x.st["pos"] == {} and rig.ex.sent == [] and rig.ex.pos[(SYM, "short")] == 3.0
    assert "not ours" in rig.events("entry_skipped")[0]["reason"]


def test_limits_fixed_notional_and_late_start(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, sizing="notional", notional_usdt=500, max_total_notional_usdt=400)
    x = run(started(rig))
    run(x.on_bar(BAR))
    assert "max_total_notional" in rig.events("entry_skipped")[0]["reason"]
    rig2 = Rig(tmp_path / "b", monkeypatch, sizing="notional", notional_usdt=500)
    x = run(started(rig2))
    run(x.on_bar(BAR))
    assert x.st["pos"]["BTC"]["size"] == pytest.approx(5)  # 500 USDT at a price of 100
    rig3 = Rig(tmp_path / "c", monkeypatch)
    rig3.now = BAR + live.LATE_MS + 1
    x = run(started(rig3))
    run(x.on_bar(BAR))
    assert x.st["pos"] == {} and "late" in rig3.events("entry_skipped")[0]["reason"]


def test_blocked_gate_stops_entries_but_keeps_stops(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    guard = type("G", (), {"reason": None, "problem": lambda self, force=False: self.reason,
                           "describe": lambda self: {}, "check_order": lambda self, o: None})()

    async def go():
        x = await started(rig, guard=guard)
        await x.on_bar(BAR)
        guard.reason = "the installed bytx differs from the released package"
        rig.candles = flat_candles(rng=20.0)
        rig.now += H4
        await x.on_bar(BAR + H4)
        return x
    x = run(go())
    assert rig.events("orders_blocked") and x.st["pos"]["BTC"]["stop"] == 96.0  # no trail move, no new order
    assert [t.stop_loss for t in run(rig.ex.tp_sl_orders(SYM, "long")) if t.stop_loss] == [96.0]
    assert [c[0] for c in rig.ex.sent] == ["open_position"]


def test_dry_run_sends_nothing(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    x = run(started(rig, dry_run=True))
    run(x.on_bar(BAR))
    (order,) = rig.events("dry_run_order")
    assert order["stop"] == 96.0 and order["risk_usdt"] == 100 and rig.ex.sent == []
    assert not (tmp_path / "bybit-demo" / "state.json").exists()


def test_startup_refuses_bad_leverage_and_single_instance_lock(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, leverage=100)
    with pytest.raises(ValueError, match="leverage"):
        run(started(rig))
    lock = live.Lock(tmp_path / "x.lock")
    with pytest.raises(RuntimeError, match="another executor"):
        live.Lock(tmp_path / "x.lock")
    lock.release()
    live.Lock(tmp_path / "x.lock").release()


def test_levels_match_backtest_arithmetic():
    assert live.levels({"side": -1, "atr": 2.0, "sl": 3, "tp": 14, "trail": 4}, 100.0) == {"stop": 106.0, "tp": 72.0, "trail": 8.0}
    assert live.levels({"side": 1, "atr": 2.0, "sl": 3, "tp": None, "trail": 2}, 100.0)["stop"] == 96.0  # tighter trail wins


def test_credentials_come_from_environment_only(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("# comment\nOKX_DEMO_API_KEY=k1\nOKX_DEMO_SECRET='s1'\nOKX_DEMO_PASSWORD=p1\n")
    env = {}
    monkeypatch.setattr(live.os, "environ", env)
    live.load_env(tmp_path / ".env")
    assert live.credentials("okx", "demo") == {"api_key": "k1", "secret": "s1", "password": "p1"}
    assert live.credentials("okx", "real") == {}


@pytest.mark.parametrize("name", ["bingx", "kucoin", "bitget"])
def test_against_the_bytx_offline_emulator(tmp_path, monkeypatch, name):
    """The same executor on the emulators shipped with bytx (skipped when bytx is not installed)."""
    testing = pytest.importorskip("bytx.testing")
    from bytx import errors as bytx_errors
    ex, venue = testing.fake_exchange(name, price=50_000)
    monkeypatch.setattr(live, "decide", lambda specs, bars: {**SIGNAL, "atr": 1000.0})
    cfg = config.validate({"exchange": name, "coins": ["BTC"]})
    now = [BAR + 30_000]
    x = live.Executor(cfg, ex, bytx_errors, tmp_path, candles=flat_candles(50_000.0, 500.0), clock=lambda: now[0],
                      specs={}, out=lambda s: None)

    async def go():
        await x.startup()
        await x.on_bar(BAR)
        opened = dict(x.st["pos"])
        await x.close_all()
        await ex.close()
        return opened
    opened = run(go())
    assert opened["BTC"]["status"] == "open" and opened["BTC"]["stop"] == 48_000 and opened["BTC"]["cid"]
    assert x.st["pos"] == {} and x.st["closed"][0]["reason"] == "manual" and len(venue.entries()) == 1
