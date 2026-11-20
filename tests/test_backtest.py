import numpy as np
import pytest

from trend4h import backtest

from conftest import H4

N = 40


def run(monkeypatch, signal_bar, side, edit=None, funding=(), **spec):
    """Flat market at 100 with a 2-point bar range (ATR = 2), one entry signal, optional bar edits."""
    b = {"ts": np.arange(N, dtype=np.int64) * H4, "open": np.full(N, 100.0), "high": np.full(N, 101.0),
         "low": np.full(N, 99.0), "close": np.full(N, 100.0)}
    for (key, i), v in (edit or {}).items():
        b[key][i] = v
    mask = np.zeros(N, bool)
    mask[signal_bar] = True
    monkeypatch.setattr(backtest, "entry_signal", lambda block, bars, tf: mask if block == side else ~mask & False)
    spec = {"base_tf": "4h", "long": "long", "short": "short", "fee": 0, "slippage": 0,
            "exit": {"sl": {"atr": 2}, "tp": {"atr": 5}, "atr_period": 14}, **spec}
    return backtest.simulate(spec, {"4h": b}, np.array(funding, dtype=float).reshape(-1, 2), 0, N * H4, "X")


def test_stop_loss_costs_one_r(monkeypatch):
    trades, daily = run(monkeypatch, 20, "long", {("low", 23): 95.0})
    (t,) = trades
    assert (t["entry_ts"], t["exit_ts"], t["reason"]) == (21 * H4, 23 * H4, "stop")
    assert t["entry"] == 100 and t["exit"] == 96 and t["R"] == pytest.approx(-1)
    assert sum(daily.values()) == pytest.approx(t["pnl"])


def test_gap_through_stop_fills_at_open_and_stop_beats_tp(monkeypatch):
    (t,) = run(monkeypatch, 20, "long", {("open", 23): 94.0, ("low", 23): 93.0, ("high", 23): 111.0})[0]
    assert t["exit"] == 94 and t["reason"] == "stop" and t["R"] == pytest.approx(-1.5)


def test_short_take_profit(monkeypatch):
    (t,) = run(monkeypatch, 20, "short", {("low", 25): 89.0})[0]
    assert t["side"] == "short" and t["exit"] == 90 and t["reason"] == "tp" and t["R"] == pytest.approx(2.5)


def test_fee_and_slippage(monkeypatch):
    (t,) = run(monkeypatch, 20, "long", {("low", 23): 95.0}, fee=0.001, slippage=0.01)[0]
    entry, stop = 101.0, 97.0  # entry 100 * 1.01, stop 4 below it
    qty = 100 / 4  # risk 100 / stop distance
    exit_ = stop * 0.99
    fees = 0.001 * qty * (entry + exit_)
    assert t["entry"] == pytest.approx(entry) and t["exit"] == pytest.approx(exit_)
    assert t["fees"] == pytest.approx(fees) and t["pnl"] == pytest.approx(qty * (exit_ - entry) - fees)


def test_funding_paid_by_long_received_by_short(monkeypatch):
    # settlements: at the entry bar open (not charged), at the open of bar 22 (charged), inside bar 22 (charged),
    # at the open of the stop bar 23 (charged: position was still open at that moment)
    f = [(21 * H4, 0.01), (22 * H4 + 7, 0.001), (22 * H4 + H4 // 2, 0.002), (23 * H4, 0.004)]
    (t,) = run(monkeypatch, 20, "long", {("low", 23): 95.0}, funding=f)[0]
    assert t["funding"] == pytest.approx(0.007 * 25 * 100) and t["R"] == pytest.approx(-1 - 0.175)
    (s,) = run(monkeypatch, 20, "short", {("high", 23): 105.0}, funding=f)[0]
    assert s["funding"] == pytest.approx(-0.007 * 25 * 100) and s["R"] == pytest.approx(-1 + 0.175)


def test_trailing_stop_follows_and_period_end_closes(monkeypatch):
    edit = {("high", 22): 110.0, ("close", 22): 109.0, ("open", 23): 109.0, ("high", 23): 109.5, ("low", 23): 105.0}
    (t,) = run(monkeypatch, 20, "long", edit, exit={"sl": {"atr": 2}, "trail": {"atr": 2}, "atr_period": 14})[0]
    assert t["exit"] == 106 and t["R"] == pytest.approx(1.5)  # stop trailed to 110 - 4
    (t,) = run(monkeypatch, 20, "long")[0]
    assert t["reason"] == "period_end" and t["exit_ts"] == N * H4 - 1 and t["R"] == 0


def test_no_entry_before_atr_is_ready(monkeypatch):
    assert run(monkeypatch, 5, "long")[0] == []
