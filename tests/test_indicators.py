import numpy as np
import pytest

from trend4h import indicators as ind

from conftest import D1, H4


def test_sma_stdev_bollinger():
    x = np.array([1.0, 2, 3, 4, 5])
    assert np.allclose(ind.sma(x, 3)[2:], [2, 3, 4]) and np.isnan(ind.sma(x, 3)[:2]).all()
    assert np.allclose(ind.stdev(x, 3)[2:], np.sqrt(2 / 3))  # population
    upper, mid, lower = ind.bollinger(x, 3, 2.0)
    assert np.allclose(upper[2:] - lower[2:], 4 * np.sqrt(2 / 3)) and np.allclose(mid[2:], [2, 3, 4])


def test_ema_seed_and_recursion():
    e = ind.ema(np.array([1.0, 2, 3, 4, 5]), 3)  # alpha = 0.5
    assert np.isnan(e[:2]).all() and np.allclose(e[2:], [2.0, 3.0, 4.0])


def test_atr_wilder():
    h, l, c = np.array([10.0, 12, 11, 15]), np.array([8.0, 9, 10, 11]), np.array([9.0, 11, 10.5, 14])
    # true ranges: 2, 3, 1, 4.5
    a = ind.atr(h, l, c, 2)
    assert np.isnan(a[0]) and np.allclose(a[1:], [2.5, 1.75, 3.125])
    assert np.allclose(ind.atr(h, l, c, 3, early="mean"), [2, 2.5, 2, (2 * 2 + 4.5) / 3])


def test_zscore():
    z = ind.zscore(np.array([1.0, 2, 3, 3, 3, 3]), 3)
    assert z[2] == pytest.approx(1 / np.sqrt(2 / 3)) and z[5] == 0  # flat window -> 0


def test_linreg_channel_on_line_plus_zigzag():
    x = np.arange(30.0)
    upper, mid, lower = ind.linreg_channel(2 * x + 5, 10, 2.0)
    assert np.allclose(mid[9:], 2 * x[9:] + 5) and np.allclose(upper[9:], lower[9:])  # perfect line: zero width
    zig = np.where(x % 2 == 0, 1.0, -1.0)
    upper, mid, lower = ind.linreg_channel(zig, 10, 2.0)
    fit = np.polyfit(np.arange(10), zig[20:30], 1)
    resid = zig[20:30] - np.polyval(fit, np.arange(10))
    assert mid[29] == pytest.approx(np.polyval(fit, 9)) and upper[29] - mid[29] == pytest.approx(2 * resid.std())


def test_ichimoku_cloud_shift():
    n = 120
    high, low = np.arange(n) + 1.0, np.arange(n) - 1.0
    top, bottom = ind.ichimoku_cloud(high, low, 9, 26, 52, 26)
    assert np.isnan(top[:77]).all() and not np.isnan(top[77:]).any()
    i = 100  # spans are computed at bar i-26 = 74
    span_a = ((74 + 1 + 74 - 8 - 1) / 2 + (74 + 1 + 74 - 25 - 1) / 2) / 2
    span_b = (74 + 1 + 74 - 51 - 1) / 2
    assert top[i] == pytest.approx(span_a) and bottom[i] == pytest.approx(span_b)


def test_adx_strong_and_absent_trend():
    n = 100
    up = np.arange(n, dtype=float)
    assert ind.adx(up + 1, up - 1, up, 14)[-1] == pytest.approx(100)  # only +DM ever
    assert np.isnan(ind.adx(up + 1, up - 1, up, 14)[:26]).all()
    flat = np.full(n, 50.0)
    assert ind.adx(flat + 1, flat - 1, flat, 14)[-1] == 0


def test_supertrend_flips_once_each_way():
    close = np.concatenate([np.full(30, 100.0), np.full(30, 130.0), np.full(30, 100.0)])
    line, trend = ind.supertrend(close + 1, close - 1, close, 10, 3.0)
    assert trend[59] == 1 and line[59] < 130 and trend[89] == -1 and line[89] > 100
    bars = {"high": close + 1, "low": close - 1, "close": close}
    cond = {"indicator": "supertrend", "settings": {"atr_period": 10, "mult": 3.0}}
    down = ind.condition({**cond, "condition": "price_crosses_below_supertrend"}, bars)
    assert np.flatnonzero(down).tolist() == [60]
    assert not ind.condition({**cond, "condition": "price_crosses_above_supertrend"}, bars)[12:].any()


def test_keltner_cross_is_an_event():
    close = np.concatenate([np.full(40, 100.0), np.full(10, 110.0)])
    bars = {"high": close + 0.5, "low": close - 0.5, "close": close}
    m = ind.condition({"indicator": "keltner", "condition": "price_crosses_above_upper",
                       "settings": {"ema_period": 20, "atr_period": 20, "mult": 2.0}}, bars)
    assert np.flatnonzero(m).tolist() == [40]


def test_align_uses_only_closed_daily_bars():
    base_ts = np.arange(18, dtype=np.int64) * H4  # three days of 4h bars
    day_mask = np.array([True, False, True])
    out = ind.align(np.arange(3, dtype=np.int64) * D1, day_mask, "1d", base_ts, "4h")
    # day 0 becomes visible on the 4h bar that closes at 24:00 (index 5), day 1 on index 11, day 2 on index 17
    assert out.tolist() == [False] * 5 + [True] * 6 + [False] * 6 + [True]


def test_expression():
    bars = {"4h": {"ts": np.arange(60, dtype=np.int64) * H4, "high": np.full(60, 2.0), "low": np.full(60, 1.0),
                   "close": np.linspace(1, 2, 60)}}
    a = {"id": "a", "indicator": "ema", "tf": "4h", "settings": {"period": 5}, "condition": "price_above_ma"}
    b = {**a, "id": "b", "condition": "price_below_ma"}
    assert ind.entry_signal({"conditions": [a, b], "expression": "a and not b"}, bars, "4h")[10:].all()
    assert not ind.entry_signal({"conditions": [a, b]}, bars, "4h").any()
    with pytest.raises(ValueError):
        ind.entry_signal({"conditions": [a], "expression": "a.__class__"}, bars, "4h")
