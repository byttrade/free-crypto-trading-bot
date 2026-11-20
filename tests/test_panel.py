"""Panel security and control, without sockets or network."""
import asyncio
import time
from types import SimpleNamespace as NS

import pytest

from trend4h import live, panel


async def fake_serve(cfg, stop, state_dir=None, out=print, on_executor=None):
    on_executor(NS(summary=lambda: {"open": [], "closed": [], "trades": 0, "sum_R": 0, "pnl_usdt": 0,
                                    "last_bar": 0, "next_bar": 0, "blocked": None}))
    out("started")
    await stop.wait()


@pytest.fixture
def rig(tmp_path, monkeypatch):
    panel.save_user(tmp_path, "admin", "correct horse")
    monkeypatch.setattr(live, "load_env", lambda path=".env": None)
    monkeypatch.setattr(live, "credentials", lambda ex, acc: {"api_key": "x"} if acc == "demo" or ex == "okx" else {})
    monkeypatch.setattr(live, "supported_exchanges", lambda: ["bybit", "okx"])
    slept = []
    now = [1_000_000.0]
    p = panel.Panel(panel.Runner(tmp_path, serve=fake_serve), panel.load_user(tmp_path), clock=lambda: now[0], sleep=slept.append)
    return NS(p=p, slept=slept, now=now, tmp=tmp_path)


def signed_in(rig):
    status, _, cookie = rig.p.handle("POST", "/api/login", None, "10.0.0.1", {"login": "admin", "password": "correct horse"}, None)
    assert status == 200 and cookie
    csrf = rig.p.handle("GET", "/api/status", cookie, "10.0.0.1", {}, None)[1]["csrf"]
    return cookie, csrf


def wait(cond):
    for _ in range(200):
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_non_local_bind_needs_a_password(tmp_path):
    with pytest.raises(ValueError, match="without a login and password"):
        panel.check_bind("0.0.0.0", None)
    panel.check_bind("127.0.0.1", None)
    panel.check_bind("0.0.0.0", {"login": "a"})


def test_password_is_stored_only_as_a_salted_hash_with_mode_600(rig):
    f = rig.tmp / "panel-user.json"
    text = f.read_text()
    assert "correct horse" not in text and (f.stat().st_mode & 0o777) == 0o600
    other = panel.save_user(rig.tmp / "b", "admin", "correct horse").read_text()
    assert panel.json.loads(other)["hash"] != panel.json.loads(text)["hash"]  # fresh salt


def test_wrong_password_is_401_delayed_then_locked_out(rig):
    for n in range(panel.MAX_FAILS):
        status, _, cookie = rig.p.handle("POST", "/api/login", None, "10.0.0.9", {"login": "admin", "password": "nope"}, None)
        assert status == 401 and cookie is None
    assert rig.slept == [panel.FAIL_DELAY] * panel.MAX_FAILS
    good = {"login": "admin", "password": "correct horse"}
    assert rig.p.handle("POST", "/api/login", None, "10.0.0.9", good, None)[0] == 429   # locked, even with the right one
    assert rig.p.handle("POST", "/api/login", None, "10.0.0.2", good, None)[0] == 200   # other addresses are not
    rig.now[0] += panel.LOCK_SEC + 1
    assert rig.p.handle("POST", "/api/login", None, "10.0.0.9", good, None)[0] == 200


def test_api_needs_a_valid_unexpired_session(rig):
    assert rig.p.handle("GET", "/api/status", None, "1.1.1.1", {}, None)[0] == 401
    cookie, _ = signed_in(rig)
    forged = cookie.rsplit("|", 1)[0] + "|" + "0" * 64
    assert rig.p.handle("GET", "/api/status", forged, "1.1.1.1", {}, None)[0] == 401
    assert rig.p.handle("GET", "/api/status", "local|9999999999|ab|" + "0" * 64, "1.1.1.1", {}, None)[0] == 401
    rig.now[0] += panel.SESSION_SEC + 1
    assert rig.p.handle("GET", "/api/status", cookie, "1.1.1.1", {}, None)[0] == 401


def test_post_without_csrf_token_is_refused_and_get_cannot_change_state(rig):
    cookie, csrf = signed_in(rig)
    assert rig.p.handle("POST", "/api/start", cookie, "1.1.1.1", {"config": {}}, None)[0] == 403
    assert rig.p.handle("POST", "/api/start", cookie, "1.1.1.1", {"config": {}}, "wrong")[0] == 403
    assert rig.p.handle("GET", "/api/start", cookie, "1.1.1.1", {}, csrf)[0] == 405
    assert not rig.p.runner.running


def test_start_and_stop_drive_the_executor(rig):
    cookie, csrf = signed_in(rig)
    status, body, _ = rig.p.handle("POST", "/api/start", cookie, "1.1.1.1", {"config": {"exchange": "bybit"}}, csrf)
    assert status == 200 and wait(lambda: rig.p.runner.running and rig.p.runner.executor)
    st = rig.p.handle("GET", "/api/status", cookie, "1.1.1.1", {}, None)[1]
    assert st["running"] and st["journal"] == ["started"] and st["keys_found"] and "HIGH RISK" in st["disclaimer"]
    assert rig.p.handle("POST", "/api/start", cookie, "1.1.1.1", {"config": {}}, csrf)[0] == 400  # one instance
    assert rig.p.handle("POST", "/api/stop", cookie, "1.1.1.1", {}, csrf)[0] == 200
    assert wait(lambda: not rig.p.runner.running)


def test_real_account_needs_the_phrase_and_keys_are_never_sent_to_the_page(rig):
    cookie, csrf = signed_in(rig)
    real = {"config": {"exchange": "okx", "account": "real"}}
    status, body, _ = rig.p.handle("POST", "/api/start", cookie, "1.1.1.1", real, csrf)
    assert status == 400 and live.ACCOUNT_PHRASE in body["error"] and not rig.p.runner.running
    assert rig.p.handle("POST", "/api/start", cookie, "1.1.1.1", {"config": {"exchange": "bybit", "account": "real"},
                                                                 "confirm": live.ACCOUNT_PHRASE}, csrf)[0] == 400  # no keys
    assert rig.p.handle("POST", "/api/start", cookie, "1.1.1.1", {**real, "confirm": live.ACCOUNT_PHRASE}, csrf)[0] == 200
    assert wait(lambda: rig.p.runner.running)
    page = rig.p.handle("GET", "/", cookie, "1.1.1.1", {}, None)[1]
    status_text = str(rig.p.handle("GET", "/api/status", cookie, "1.1.1.1", {}, None)[1])
    assert "api_key" not in status_text and "api_key" not in page and "password" in page  # only the login form field
    rig.p.runner.stop()
    assert wait(lambda: not rig.p.runner.running)


def test_bad_settings_from_the_page_are_rejected(rig):
    cookie, csrf = signed_in(rig)
    status, body, _ = rig.p.handle("POST", "/api/start", cookie, "1.1.1.1", {"config": {"leverage": 1000}}, csrf)
    assert status == 400 and "leverage" in body["error"]


def test_local_use_without_password_still_gets_session_and_csrf(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "load_env", lambda path=".env": None)
    monkeypatch.setattr(live, "supported_exchanges", lambda: ["bybit"])
    p = panel.Panel(panel.Runner(tmp_path, serve=fake_serve), None)
    status, page, cookie = p.handle("GET", "/", None, "127.0.0.1", {}, None)
    assert status == 200 and cookie and "<title>trend-4h panel</title>" in page and "cdn" not in page.lower()
    assert p.handle("POST", "/api/stop", cookie, "127.0.0.1", {}, None)[0] == 403
    assert p.handle("POST", "/api/login", None, "127.0.0.1", {"login": "", "password": ""}, None)[0] == 401
