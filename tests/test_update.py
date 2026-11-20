"""bytx auto-update with a fake index and installer: no network, nothing is installed."""
import pathlib
from types import SimpleNamespace as NS

from trend4h import update
from trend4h.live import STEP

BAR_SEC = STEP // 1000
MID_BAR = 1000 * BAR_SEC + 3600.0   # one hour into a 4h bar: three hours to its close
SOURCES = {"BYBIT_BROKER_ID": "builtin", "BINANCE_BROKER_ID_FUTURES": "none"}


class World:
    def __init__(self, tmp_path, index=("0.2.2", "0.2.5", "0.3.0", "0.2.6rc1"), now=MID_BAR, **kw):
        self.installed, self.now, self.calls, self.events = "0.2.2", now, [], []
        self.broken_install, self.broken_verify, self.sources = set(), set(), dict(SOURCES)
        self.up = update.Updater(state_dir=tmp_path, versions=lambda: list(index), install=self.install,
                                 verify=self.verify, current=lambda: self.installed, clock=lambda: self.now,
                                 log=lambda ev, **k: self.events.append(ev), **kw)

    def install(self, version):
        self.calls.append(version)
        if version in self.broken_install:
            raise RuntimeError("no matching distribution")
        self.installed = version

    def verify(self, version):
        if version in self.broken_verify:
            raise RuntimeError("the installed bytx differs from the released package")
        return dict(self.sources)


def idle(now):
    return NS(busy=0, last_bar_done=now - 3600)


def test_version_choice_stays_inside_the_allowed_range():
    assert update.pick(["0.2.2", "0.2.10", "0.2.9", "0.3.0", "1.0.0", "0.2.11rc1", "0.1.9"]) == ("0.2.10", "1.0.0")
    toml = (pathlib.Path(__file__).parent.parent / "pyproject.toml").read_text()
    assert '"bytx>=0.2.2,<0.3"' in toml and (update.BYTX_MIN, update.BYTX_BELOW) == ((0, 2, 2), (0, 3))


def test_successful_update_asks_for_a_restart(tmp_path):
    w = World(tmp_path)
    assert w.up.run(idle(w.now)) == "restart"
    assert w.installed == "0.2.5" and w.calls == ["0.2.5"]
    assert w.events == ["update_incompatible", "update_available", "update_start", "update_done"]
    assert w.up.run(idle(w.now)) is None  # nothing newer left


def test_failed_install_rolls_back_and_waits_a_day(tmp_path):
    w = World(tmp_path)
    w.broken_install.add("0.2.5")
    assert w.up.run(idle(w.now)) is None
    assert w.installed == "0.2.2" and w.calls == ["0.2.5", "0.2.2"] and "update_rolled_back" in w.events
    w.now += 3600
    assert w.up.run(idle(w.now)) is None and w.calls == ["0.2.5", "0.2.2"]  # same version: not before a day
    w.broken_install.clear()
    w.now += 24 * 3600
    assert w.up.run(idle(w.now)) == "restart"


def test_failed_integrity_of_the_new_version_rolls_back(tmp_path):
    w = World(tmp_path)
    w.broken_verify.add("0.2.5")
    assert w.up.run(idle(w.now)) is None
    assert w.installed == "0.2.2" and w.events[-2:] == ["update_failed", "update_rolled_back"]


def test_update_that_loses_attribution_is_rolled_back(tmp_path):
    w = World(tmp_path)
    real_verify = w.verify
    w.up.verify = lambda v: {**real_verify(v), **({"BYBIT_BROKER_ID": "none"} if v == "0.2.5" else {})}
    assert w.up.run(idle(w.now)) is None and w.installed == "0.2.2"


def test_update_waits_for_a_safe_window(tmp_path):
    w = World(tmp_path)
    assert w.up.run(NS(busy=1, last_bar_done=0)) is None and w.calls == []          # request in flight
    assert w.up.run(NS(busy=0, last_bar_done=w.now - 60)) is None and w.calls == []  # bar processed a minute ago
    w.now = 1000 * BAR_SEC + BAR_SEC - 59 * 60                                      # 59 minutes to the bar close
    assert "59 min" in w.up.safe_window(idle(w.now)) and w.up.run(idle(w.now)) is None and w.calls == []
    w.now = 1000 * BAR_SEC + BAR_SEC - 61 * 60                                      # more than an hour: allowed
    assert w.up.run(idle(w.now)) == "restart"


def test_margin_cannot_be_set_below_an_hour_and_modes(tmp_path):
    assert update.Updater(margin_min=15, state_dir=tmp_path).margin == 3600
    w = World(tmp_path / "n", mode="notify")
    assert w.up.run(idle(w.now)) is None and w.calls == [] and "update_available" in w.events
    assert w.up.status()["available"] == "0.2.5" and w.up.status()["incompatible"] == "0.3.0"
    w = World(tmp_path / "o", mode="off")
    assert w.up.run(idle(w.now)) is None and w.events == []


def test_two_processes_do_not_update_at_once(tmp_path):
    w = World(tmp_path)
    lock = update.Lock(tmp_path / "update.lock")
    assert w.up.run(idle(w.now)) is None and w.calls == []
    lock.release()
    assert w.up.run(idle(w.now)) == "restart"
