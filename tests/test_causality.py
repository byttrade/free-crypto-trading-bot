"""A signal at bar t must not change when later bars are removed."""
import numpy as np
import pytest

from trend4h.indicators import entry_signal

from conftest import cut, synthetic

NAMES = ["st4h_ema50_1d", "kc2_4h_adx1d", "zmid1d_cloud4h", "bbw1d_lr4h"]


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("side", ["long", "short"])
def test_signal_does_not_look_ahead(specs, name, side):
    bars = synthetic()
    n = len(bars["4h"]["ts"])
    full = entry_signal(specs[name][side], bars, "4h")
    assert full.any(), "synthetic series should produce signals"
    for keep in (n - 1, n - 4, n - 7, n // 2 + 3, n // 3):  # keep 4h bars [0, keep); cuts fall inside a day too
        part = {"4h": cut(bars["4h"], keep), "1d": cut(bars["1d"], -(-keep // 6))}  # includes the unfinished day
        assert np.array_equal(entry_signal(specs[name][side], part, "4h"), full[:keep])
