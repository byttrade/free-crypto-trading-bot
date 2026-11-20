"""bytx must be the released package and its broker attribution must be what a clean process gets."""
import base64
import hashlib
from types import SimpleNamespace as NS

import pytest

from trend4h import integrity


class Registry:
    """Stand-in for the bytx attribution registry (names and sources only, no code values)."""
    MODE_ENV = "BYTX_BROKER_MODE"

    def __init__(self, sources, mode="full"):
        self.src, self.mode = dict(sources), mode
        self.KEYS = frozenset(self.src)

    def resolve_source(self, key, exchange=None):
        return ("tag" if self.src[key] == "builtin" else ""), self.src[key]

    def resolve(self, key, exchange=None):
        return self.resolve_source(key)[0]

    def broker_mode(self):
        return self.mode


CLEAN = {"BYBIT_BROKER_ID": "builtin", "OKX_BROKER_CODE": "builtin", "BINANCE_BROKER_ID_FUTURES": "none",
         "KUCOIN_BROKER_PARTNER": "builtin", "KUCOIN_BROKER_KEY": "builtin", "KUCOIN_BROKER_NAME": "builtin",
         "KUCOIN_BROKER_AVAILABLE": "builtin", "HL_BUILDER_ADDRESS": "none"}


def guard(exchange, actual=None, mode="full", expected=CLEAN):
    return integrity.Guard(exchange, object(), opts=Registry({**CLEAN, **(actual or {})}, mode), headers=None,
                           expected=expected, files=lambda: None)


def test_builtin_exchange_passes_only_with_builtin_source():
    assert guard("bybit").problem() is None
    for source in ("env", "instance", "process", "mode:off", "disabled:process", "none"):
        msg = guard("bybit", {"BYBIT_BROKER_ID": source}).problem()
        assert "broker attribution is disabled or overridden" in msg and source in msg and "tag" not in msg


def test_kucoin_needs_partner_key_name_and_available():
    assert guard("kucoin").problem() is None
    for key in ("KUCOIN_BROKER_PARTNER", "KUCOIN_BROKER_KEY", "KUCOIN_BROKER_NAME", "KUCOIN_BROKER_AVAILABLE"):
        assert key in guard("kucoin", {key: "env"}).problem()


def test_exchange_without_builtin_code_must_stay_untagged():
    assert guard("binance").problem() is None
    assert guard("hyperliquid").problem() is None
    assert "env" in guard("binance", {"BINANCE_BROKER_ID_FUTURES": "env"}).problem()  # nobody else's code either
    assert "mode: off" in guard("htx", mode="off").problem()  # no keys at all: the mode is still checked


def test_new_builtin_code_in_a_future_bytx_is_required_automatically():
    future = {**CLEAN, "BINANCE_BROKER_ID_FUTURES": "builtin"}  # the clean registry of a newer bytx
    assert "BINANCE_BROKER_ID_FUTURES: none" in guard("binance", expected=future).problem()
    assert guard("binance", {"BINANCE_BROKER_ID_FUTURES": "builtin"}, expected=future).problem() is None
    assert "must be builtin" in integrity.checks_for("binance", future, Registry(CLEAN))["attribution"]
    assert "no built-in code" in integrity.checks_for("binance", CLEAN, Registry(CLEAN))["attribution"]


def test_file_check_is_repeated_on_a_timer_and_failure_blocks():
    t, state = [0.0], {"error": None}

    def files():
        if state["error"]:
            raise integrity.IntegrityError(state["error"])
    g = integrity.Guard("bybit", object(), opts=Registry(CLEAN), expected=CLEAN, files=files, clock=lambda: t[0], recheck_sec=600)
    assert g.problem() is None
    state["error"] = "the installed bytx differs from the released package"
    t[0] = 599
    assert g.problem() is None       # not re-read yet
    t[0] = 600
    assert "differs" in g.problem()  # a file changed while the process was running
    state["error"] = None
    assert g.problem(force=True) is None


def test_order_tag_is_read_back_where_the_exchange_returns_it():
    g = guard("okx")
    g.check_order(NS(raw={"info": {"tag": "tag"}}))
    assert g.problem() is None
    g.check_order(NS(raw={"info": {"ordId": "1"}}))  # response without the field: nothing to compare
    assert g.problem() is None
    g.check_order(NS(raw={"info": {"tag": ""}}))
    assert "without the broker tag" in g.problem()
    b = guard("bybit")
    b.check_order(NS(raw={"info": {}}))  # not readable on this exchange: configuration check only
    assert b.problem() is None and "not readable" in b.describe()["order_tag"]


def _dist(tmp_path, content=b"x = 1\n", recorded=None, hashed=True):
    pkg = tmp_path / "site" / "demo"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_bytes(content)
    digest = base64.urlsafe_b64encode(hashlib.sha256(recorded or content).digest()).rstrip(b"=").decode()
    F = type("F", (NS,), {"__str__": lambda self: "demo/__init__.py"})
    f = F(hash=NS(mode="sha256", value=digest) if hashed else None, locate=lambda: pkg / "__init__.py")
    return NS(files=[f], locate_file=lambda name: tmp_path / "site" / name), pkg


def test_changed_file_editable_install_and_wrong_import_path_are_refused(tmp_path, monkeypatch):
    dist, pkg = _dist(tmp_path)
    monkeypatch.setattr(integrity.metadata, "distribution", lambda name: dist)
    integrity.check_files("demo", module=NS(__file__=str(pkg / "__init__.py")))
    with pytest.raises(integrity.IntegrityError, match="imported from"):
        integrity.check_files("demo", module=NS(__file__=str(tmp_path / "elsewhere" / "demo" / "__init__.py")))
    (pkg / "__init__.py").write_bytes(b"x = 2\n")
    with pytest.raises(integrity.IntegrityError, match="differs from the released package"):
        integrity.check_files("demo")
    dist2, _ = _dist(tmp_path / "e", hashed=False)
    monkeypatch.setattr(integrity.metadata, "distribution", lambda name: dist2)
    with pytest.raises(integrity.IntegrityError, match="editable"):
        integrity.check_files("demo")


def test_real_bytx_install_if_present():
    pytest.importorskip("bytx")
    from bytx.net._sig import headers, opts
    integrity.check_files("bytx")
    clean = integrity.clean_sources(opts)
    assert set(clean.values()) <= {"builtin", "none"}
    assert integrity.Guard("bybit", None, opts=opts, headers=headers, expected=clean).problem() is None
