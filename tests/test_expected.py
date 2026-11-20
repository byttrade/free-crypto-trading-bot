"""Reference totals of the published research; checked only when the candle cache exists."""
import json

import pytest

from trend4h import data
from trend4h.__main__ import report

from conftest import ROOT

EXPECTED = json.loads((ROOT / "tests" / "expected.json").read_text())


@pytest.mark.skipif(not all((ROOT / "data" / f"{c}_{s}.csv").exists() for c in data.COINS for s in ("4h", "1d", "funding")),
                    reason="no data/ - run `python -m trend4h fetch` first")
def test_report_matches_published_results():
    res = report(ROOT / "data", ROOT / "specs")
    for name, periods in EXPECTED.items():
        for period, want in periods.items():
            got = res[name][period]
            assert {k: got[k] for k in want} == want, (name, period)
