import json
from pathlib import Path

import numpy as np
import pytest

from trend4h import config

ROOT = Path(__file__).resolve().parent.parent
H4, D1 = 14_400_000, 86_400_000


def synthetic(n_days=400, seed=1):
    """Random-walk 4h candles and the matching 1d candles."""
    rng = np.random.default_rng(seed)
    n = n_days * 6
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.012, n)))
    open_ = np.concatenate([[100.0], close[:-1]])
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.006, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.006, n))
    h4 = {"ts": np.arange(n, dtype=np.int64) * H4, "open": open_, "high": high, "low": low, "close": close}
    d1 = {"ts": np.arange(n_days, dtype=np.int64) * D1, "open": open_[::6], "close": close[5::6],
          "high": high.reshape(-1, 6).max(axis=1), "low": low.reshape(-1, 6).min(axis=1)}
    return {"4h": h4, "1d": d1}


def cut(bars, n):
    return {k: v[:n] for k, v in bars.items()}


@pytest.fixture(scope="session")
def specs():
    return {p.stem: json.loads(p.read_text()) for p in sorted(config.SPECS_DIR.glob("*.json"))}
