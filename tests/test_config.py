import json

import pytest

from trend4h import backtest, config
from trend4h.__main__ import main

from conftest import ROOT


def test_defaults_are_the_research_settings():
    c = config.load()
    assert c["risk_pct"] == 0.056 and len(c["coins"]) == 20 and len(c["strategies"]) == 4
    assert config.warnings(c) == [] and c["auto_update"] == "on" and c["update_margin_min"] == 60
    assert config.validate(json.loads((ROOT / "config.example.json").read_text())) == c


@pytest.mark.parametrize("bad", [{"leverage": 0}, {"risk_pct": 0}, {"risk_pct": 50}, {"sizing": "martingale"},
                                 {"stop": False}, {"margin_mode": "portfolio"}, {"strategies": []},
                                 {"update_margin_min": 15}, {"account": "paper"}, {"notional_usdt": -1}])
def test_invalid_settings_are_rejected(bad):
    with pytest.raises(ValueError):
        config.validate(bad)


def test_deviations_from_research_are_called_out():
    w = config.warnings(config.validate({"coins": ["BTC", "PEPE"], "risk_pct": 0.5}))
    assert "default settings only" in w[0] and "PEPE" in w[1] and "NOT confirmed" in w[1]


def test_config_applies_to_backtest_spec(tmp_path, capsys):
    spec = {"base_tf": "4h", "coins": "all", "exit": {"sl": {"atr": 2}}}
    out = config.apply_to_spec(spec, config.validate({"coins": ["ETH"], "fee": 0.001, "sizing": "notional", "notional_usdt": 250}))
    assert out["coins"] == ["ETH"] and out["fee"] == 0.001 and out["notional_usdt"] == 250 and spec["coins"] == "all"
    if (ROOT / "data" / "BTC_4h.csv").exists():
        cfg = tmp_path / "c.json"
        cfg.write_text(json.dumps({"coins": ["BTC"], "sizing": "notional", "notional_usdt": 1000}))
        main(["run", "kc2_4h_adx1d", "--period", "oos", "--config", str(cfg), "--data-dir", str(ROOT / "data")])
        assert "WARNING" in capsys.readouterr().out
        m, trades = backtest.run(config.apply_to_spec(json.loads((config.SPECS_DIR / "kc2_4h_adx1d.json").read_text()), config.load(cfg)),
                                 *backtest.PERIODS["oos"], ROOT / "data")
        assert all(abs(t["pnl"]) < 1000 for t in trades) and m["pnl_usdt"] == pytest.approx(sum(t["pnl"] for t in trades), abs=0.01)


def test_run_takes_a_built_in_strategy_by_name_or_your_own_file(tmp_path, monkeypatch):
    from trend4h.__main__ import spec_path
    monkeypatch.chdir(tmp_path)                                            # an empty folder, as after pip install
    assert spec_path("kc2_4h_adx1d") == config.SPECS_DIR / "kc2_4h_adx1d.json"
    assert spec_path("specs/kc2_4h_adx1d.json") == config.SPECS_DIR / "kc2_4h_adx1d.json"   # the path of older docs
    own = tmp_path / "mine.json"
    own.write_text("{}")
    assert spec_path(str(own)) == own
    with pytest.raises(FileNotFoundError, match="built-in strategy"):
        spec_path("nope")
    assert sorted(p.stem for p in config.SPECS_DIR.glob("*.json")) == sorted(config.STRATEGIES)
