"""Panel security and control, without sockets or network."""
import json
import os
import re
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
    assert "'x'" not in status_text and "password" in page  # the fake key value is "x"; fields are type=password
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
    assert status == 200 and cookie and "<title>Free Crypto Trading Bot by BYTtrade</title>" in page and "cdn" not in page.lower()
    assert p.handle("POST", "/api/stop", cookie, "127.0.0.1", {}, None)[0] == 403
    assert p.handle("POST", "/api/login", None, "127.0.0.1", {"login": "", "password": ""}, None)[0] == 401


# ---------------------------------------------------------------- settings, journal and state for the page

def test_settings_are_saved_separately_from_start_and_survive_a_restart(rig):
    cookie, csrf = signed_in(rig)
    status, body, _ = rig.p.handle("POST", "/api/config", cookie, "1.1.1.1", {"config": {"exchange": "okx", "leverage": 5}}, csrf)
    assert status == 200 and body["config"]["leverage"] == 5 and not rig.p.runner.running
    assert rig.p.handle("POST", "/api/config", cookie, "1.1.1.1", {"config": {"leverage": None}}, csrf)[0] == 400
    assert rig.p.handle("POST", "/api/config", cookie, "1.1.1.1", {"config": {"exchange": "../x"}}, csrf)[0] == 400
    again = panel.Panel(panel.Runner(rig.tmp, serve=fake_serve), panel.load_user(rig.tmp))
    assert again.config["exchange"] == "okx" and again.config["leverage"] == 5
    assert rig.p.handle("POST", "/api/start", cookie, "1.1.1.1", {}, csrf)[0] == 200   # starts with the saved settings
    assert wait(lambda: rig.p.runner.running) and rig.p.runner.cfg["exchange"] == "okx"
    rig.p.runner.stop()
    assert wait(lambda: not rig.p.runner.running)


def test_status_reads_positions_balance_and_next_bar_while_stopped(rig):
    folder = rig.tmp / "bybit-demo"
    folder.mkdir()
    (folder / "state.json").write_text(json.dumps({"last_bar": 1, "pos": {"BTC": {"coin": "BTC", "side": 1}}, "closed": [
        {"coin": "ETH", "pnl": 12.5, "R": 1.5}, {"coin": "SOL", "pnl": -4.0, "R": -1.0}]}))
    (folder / "journal.jsonl").write_text(json.dumps({"t": 5000, "ev": "start", "equity": 987.6}) + "\nnot json\n"
                                          + json.dumps({"t": 6000, "ev": "entry_rejected", "error": "nope"}) + "\n")
    cookie, _ = signed_in(rig)
    st = rig.p.handle("GET", "/api/status", cookie, "1.1.1.1", {}, None)[1]
    assert st["summary"]["trades"] == 2 and st["summary"]["pnl_usdt"] == 8.5 and st["summary"]["sum_R"] == 0.5
    assert [p["coin"] for p in st["summary"]["open"]] == ["BTC"]
    assert st["balance"] == {"value": 987.6, "at": 5.0, "live": False}
    assert st["next_bar"] > st["now"] and st["next_bar"] % live.STEP == 0 and st["next_bar"] - st["now"] <= live.STEP
    events = rig.p.handle("GET", "/api/journal", cookie, "1.1.1.1", {}, None)[1]["events"]
    assert [e["ev"] for e in events] == ["start", "entry_rejected"]
    assert rig.p.handle("GET", "/api/journal", None, "1.1.1.1", {}, None)[0] == 401


# ---------------------------------------------------------------- exchange keys

SECRETS = {"api_key": "TESTONLYkey0001", "secret": "TESTONLYsecret0002", "password": "TESTONLYphrase0003"}
LOCAL = {"tls": False, "forwarded": False, "proto": "", "host": "127.0.0.1:8790"}


@pytest.fixture
def keys(tmp_path, monkeypatch):
    """A panel with the real key reading and a .env in a temporary folder; the process environment is a copy."""
    monkeypatch.setattr(os, "environ", {k: v for k, v in os.environ.items() if "_DEMO_" not in k and "_REAL_" not in k})
    monkeypatch.setattr(live, "supported_exchanges", lambda: ["bybit", "okx", "hyperliquid"])
    env = tmp_path / ".env"
    env.write_text("# my notes\nOTHER=1\nOKX_DEMO_API_KEY=old\nBYBIT_REAL_SECRET=keepme\n")
    p = panel.Panel(panel.Runner(tmp_path, serve=fake_serve), None, env_path=env)
    cookie = p.handle("GET", "/", None, "127.0.0.1", {}, None)[2]
    csrf = p.handle("GET", "/api/status", cookie, "127.0.0.1", {}, None, LOCAL)[1]["csrf"]
    return NS(p=p, env=env, cookie=cookie, csrf=csrf, tmp=tmp_path)


def post(k, path, body, ip="127.0.0.1", conn=LOCAL, csrf=True):
    return k.p.handle("POST", path, k.cookie, ip, body, k.csrf if csrf is True else csrf, conn)


def test_keys_are_written_to_env_with_mode_600_and_other_lines_stay(keys):
    status, body, _ = post(keys, "/api/keys", {"exchange": "okx", "account": "demo", "fields": SECRETS})
    assert status == 200 and body == {"ok": True, "set": True}
    lines = keys.env.read_text().splitlines()
    assert lines[:3] == ["# my notes", "OTHER=1", "BYBIT_REAL_SECRET=keepme"]          # untouched, old okx line replaced
    assert sorted(lines[3:]) == ["OKX_DEMO_API_KEY=TESTONLYkey0001", "OKX_DEMO_PASSWORD=TESTONLYphrase0003",
                                 "OKX_DEMO_SECRET=TESTONLYsecret0002"]
    assert (keys.env.stat().st_mode & 0o777) == 0o600 and not list(keys.tmp.glob(".env.*"))
    assert live.credentials("okx", "demo") == SECRETS                                  # the executor reads the same names
    st = keys.p.handle("GET", "/api/status", keys.cookie, "127.0.0.1", {}, None, LOCAL)[1]
    assert st["keys"]["okx"] == {"demo": True, "real": False} and st["keys_writable"]
    assert st["key_fields"]["hyperliquid"] == ["wallet_address", "private_key"]

    assert post(keys, "/api/keys/delete", {"exchange": "okx", "account": "demo"})[0] == 200
    assert keys.env.read_text().splitlines() == ["# my notes", "OTHER=1", "BYBIT_REAL_SECRET=keepme"]
    assert live.credentials("okx", "demo") == {}


def test_key_values_are_never_returned_by_any_endpoint(keys):
    answers = [post(keys, "/api/keys", {"exchange": "okx", "account": "demo", "fields": SECRETS})]
    (keys.tmp / "okx-demo").mkdir()
    (keys.tmp / "okx-demo" / "journal.jsonl").write_text(json.dumps({"t": 1, "ev": "cycle_error", "error": "bad TESTONLYsecret0002"}) + "\n")
    keys.p.runner.error = "AuthError: key TESTONLYkey0001 rejected"
    keys.p.runner.lines = ["oops TESTONLYphrase0003"]
    answers.append(post(keys, "/api/config", {"config": {"exchange": "okx"}}))
    answers += [keys.p.handle("GET", path, keys.cookie, "127.0.0.1", {}, None, LOCAL) for path in ("/", "/api/status", "/api/journal")]
    answers.append(post(keys, "/api/keys", {"exchange": "okx", "account": "demo", "fields": {**SECRETS, "secret": "TESTONLY bad value"}}))
    answers.append(post(keys, "/api/keys", {"exchange": "nope", "account": "demo", "fields": SECRETS}))
    answers.append(post(keys, "/api/start", {"config": {"exchange": "okx", "account": "real"}, "confirm": live.ACCOUNT_PHRASE}))
    text = json.dumps([a[1] for a in answers])
    assert "TESTONLY" not in text and "keepme" not in text and "***" in text
    assert answers[-3][0] == 400 and answers[-2][0] == 400


def test_keys_need_a_session_csrf_and_post(keys):
    body = {"exchange": "bybit", "account": "demo", "fields": SECRETS}
    assert post(keys, "/api/keys", body, csrf=None)[0] == 403
    assert post(keys, "/api/keys", body, csrf="wrong")[0] == 403
    assert keys.p.handle("POST", "/api/keys", None, "127.0.0.1", body, keys.csrf, LOCAL)[0] == 401
    assert keys.p.handle("GET", "/api/keys", keys.cookie, "127.0.0.1", body, keys.csrf, LOCAL)[0] == 405
    assert post(keys, "/api/keys", {**body, "fields": {"api_key": "TESTONLYkey0001"}})[0] == 400   # the secret is missing
    assert "BYBIT_DEMO" not in keys.env.read_text()


def test_keys_are_refused_over_plain_http_from_another_address(keys):
    body = {"exchange": "bybit", "account": "demo", "fields": SECRETS}
    remote = {**LOCAL, "host": "203.0.113.5:8790"}
    status, answer, _ = post(keys, "/api/keys", body, ip="203.0.113.9", conn=remote)
    assert status == 403 and answer["code"] == "insecure" and "SSH" in answer["error"]
    assert post(keys, "/api/keys/delete", {"exchange": "okx", "account": "demo"}, ip="203.0.113.9", conn=remote)[0] == 403
    assert post(keys, "/api/password", {"login": "a", "password": "long enough"}, ip="203.0.113.9", conn=remote)[0] == 403
    assert post(keys, "/api/keys", body, conn={**LOCAL, "host": "evil.example:8790"})[0] == 403   # DNS rebinding
    assert not keys.p.handle("GET", "/api/status", keys.cookie, "203.0.113.9", {}, None, remote)[1]["keys_writable"]
    assert "BYBIT_DEMO" not in keys.env.read_text() and "OKX_DEMO_API_KEY=old" in keys.env.read_text()
    assert post(keys, "/api/keys", body, ip="203.0.113.9", conn={**remote, "tls": True})[0] == 200   # --certfile


def test_a_reverse_proxy_is_trusted_only_with_the_flag(keys):
    body = {"exchange": "bybit", "account": "demo", "fields": SECRETS}
    proxied = {**LOCAL, "forwarded": True, "proto": "https", "host": "panel.example"}
    assert post(keys, "/api/keys", body, conn=proxied)[0] == 403        # looks local, but the browser is elsewhere
    keys.p.https_proxy = True
    assert post(keys, "/api/keys", body, conn={**proxied, "proto": "http"})[0] == 403
    assert post(keys, "/api/keys", body, conn=proxied)[0] == 200


def test_keys_of_the_running_account_cannot_be_changed_or_deleted(keys):
    body = {"exchange": "bybit", "account": "demo", "fields": SECRETS}
    assert post(keys, "/api/keys", body)[0] == 200
    assert post(keys, "/api/start", {"config": {"exchange": "bybit"}})[0] == 200
    assert wait(lambda: keys.p.runner.running)
    before = keys.env.read_text()
    status, answer, _ = post(keys, "/api/keys", {**body, "fields": {"api_key": "TESTONLYnew", "secret": "TESTONLYnew2"}})
    assert status == 409 and answer["code"] == "keys_in_use"
    assert post(keys, "/api/keys/delete", {"exchange": "bybit", "account": "demo"})[0] == 409
    assert keys.env.read_text() == before
    assert post(keys, "/api/keys", {**body, "account": "real"})[0] == 200   # another account is free to edit
    keys.p.runner.stop()
    assert wait(lambda: not keys.p.runner.running)
    assert post(keys, "/api/keys/delete", {"exchange": "bybit", "account": "demo"})[0] == 200


def test_password_can_be_set_from_the_panel_only_with_the_current_one(keys):
    status, _, cookie = post(keys, "/api/password", {"login": "boss", "password": "long enough"})
    assert status == 200 and keys.p.user["login"] == "boss" and "long enough" not in (keys.tmp / "panel-user.json").read_text()
    assert not keys.p.session_ok(keys.cookie) and keys.p.session_ok(cookie)   # the password-less session is over
    csrf = keys.p.csrf(cookie)
    wrong = keys.p.handle("POST", "/api/password", cookie, "127.0.0.1", {"current": "nope", "login": "boss", "password": "another one"}, csrf, LOCAL)
    assert wrong[0] == 403 and wrong[1]["code"] == "bad_password"
    short = keys.p.handle("POST", "/api/password", cookie, "127.0.0.1", {"current": "long enough", "login": "boss", "password": "short"}, csrf, LOCAL)
    assert short[0] == 400


# ---------------------------------------------------------------- agents: tokens, /api/v1 and the MCP wrapper

@pytest.fixture
def bots(keys):
    """`keys` plus a read token, a control token, demo keys for bybit and a recording stand-in for `close`."""
    keys.now = [time.time()]
    keys.p.clock, keys.p.sleep, keys.closed = (lambda: keys.now[0]), (lambda s: None), []
    keys.p.closer = lambda cfg, state_dir: keys.closed.append(cfg["account"]) or {"closed": 2, "still_open": 0}
    assert post(keys, "/api/keys", {"exchange": "bybit", "account": "demo", "fields": SECRETS})[0] == 200
    assert post(keys, "/api/keys", {"exchange": "bybit", "account": "real", "fields": SECRETS})[0] == 200
    keys.reader = post(keys, "/api/tokens", {"name": "reader"})[1]["token"]
    keys.bot = post(keys, "/api/tokens", {"name": "night bot", "control": True})[1]["token"]
    return keys


def ask(k, method, what, token, body=None, ip="198.51.100.7"):
    return k.p.handle(method, "/api/v1/" + what, None, ip, body or {}, None, {"auth": f"Bearer {token}"})


def test_token_value_is_shown_once_and_only_its_hash_is_stored(bots):
    f = bots.tmp / "panel-tokens.json"
    assert (f.stat().st_mode & 0o777) == 0o600 and len(bots.bot) > 40
    st = bots.p.handle("GET", "/api/status", bots.cookie, "127.0.0.1", {}, None, LOCAL)[1]
    assert [(t["name"], t["scopes"]) for t in st["tokens"]] == [("reader", ["read"]), ("night bot", ["read", "control"])]
    everything = f.read_text() + json.dumps([st, ask(bots, "GET", "token", bots.bot)[1], ask(bots, "GET", "config", bots.bot)[1]])
    assert bots.bot not in everything and bots.reader not in everything and "hash" not in json.dumps(st["tokens"])
    assert post(bots, "/api/tokens", {"name": "x"}, csrf=None)[0] == 403                    # a person's session with CSRF
    assert post(bots, "/api/tokens", {"name": "x"}, ip="203.0.113.9", conn={**LOCAL, "host": "203.0.113.5"})[0] == 403
    assert post(bots, "/api/tokens/delete", {"id": st["tokens"][0]["id"]}, ip="203.0.113.9", conn={**LOCAL, "host": "x"})[0] == 403
    assert post(bots, "/api/tokens", {"name": ""})[0] == 400


def test_a_read_token_reads_and_cannot_control(bots):
    for what in ("token", "status", "positions", "trades", "journal", "config"):
        assert ask(bots, "GET", what, bots.reader)[0] == 200, what
    st = ask(bots, "GET", "status", bots.reader)[1]
    assert st["account"] == "demo" and st["next_bar"] > st["now"] and st["running"] is False
    for what in ("config", "start", "stop", "close"):
        status, body, _ = ask(bots, "POST", what, bots.reader, {"config": {"leverage": 5}})
        assert status == 403 and body["code"] == "scope", what
    assert bots.p.config["leverage"] == 3 and not bots.p.runner.running and not bots.closed


def test_no_token_reaches_keys_password_tokens_updates_or_a_real_account(bots):
    answers = []
    for what in ("keys", "keys/delete", "password", "tokens", "tokens/delete", "update", "logout", "login"):
        for method in ("GET", "POST"):
            answers.append(ask(bots, method, what, bots.bot, {"exchange": "okx", "account": "demo", "fields": SECRETS}))
            assert answers[-1][0] == 404, what
    bearer = {**LOCAL, "auth": f"Bearer {bots.bot}"}                    # a token is not a session of the panel itself
    assert bots.p.handle("POST", "/api/keys/delete", None, "127.0.0.1", {"exchange": "bybit", "account": "demo"}, None, bearer)[0] == 401
    assert bots.p.handle("GET", "/api/status", None, "127.0.0.1", {}, None, bearer)[0] == 401

    answers.append(ask(bots, "POST", "config", bots.bot, {"config": {"account": "real"}}))
    assert answers[-1][0] == 403 and answers[-1][1]["code"] == "real_forbidden"
    answers.append(ask(bots, "POST", "config", bots.bot, {"config": {"auto_update": "off"}}))
    assert answers[-1][0] == 403
    answers.append(ask(bots, "POST", "config", bots.bot, {"config": {"leverage": 1000}}))   # the same checks as the page
    assert answers[-1][0] == 400 and "leverage" in answers[-1][1]["error"]
    assert bots.p.config == panel.cfgmod.DEFAULTS

    assert post(bots, "/api/config", {"config": {"account": "real"}})[0] == 200          # the person chose real
    for what in ("start", "stop", "close", "config"):
        answers.append(ask(bots, "POST", what, bots.bot, {"config": {"leverage": 2}, "confirm": live.ACCOUNT_PHRASE}))
        assert answers[-1][0] == 403 and answers[-1][1]["code"] == "real_forbidden", what
    assert not bots.p.runner.running and not bots.closed
    answers += [ask(bots, "GET", what, bots.bot) for what in ("status", "config", "journal")]
    assert "TESTONLY" not in json.dumps([a[1] for a in answers])
    refused = [e for e in bots.p.journal(bots.p.config) if e["ev"] == "agent_refused"]
    assert len(refused) == 4 and refused[0]["token"] == "night bot"


def test_a_control_token_works_on_demo_and_its_actions_are_in_the_journal(bots):
    status, body, _ = ask(bots, "POST", "config", bots.bot, {"config": {"leverage": 5, "coins": ["btc", "eth"]}})
    assert status == 200 and body["config"]["leverage"] == 5 and bots.p.config["coins"] == ["BTC", "ETH"] and body["warnings"]
    assert bots.p.config["strategies"] == panel.cfgmod.STRATEGIES                          # only the given keys changed
    assert ask(bots, "POST", "start", bots.bot)[0] == 200 and wait(lambda: bots.p.runner.running)
    assert ask(bots, "GET", "status", bots.reader)[1]["running"] is True
    assert ask(bots, "POST", "close", bots.bot)[1]["code"] == "running"
    assert ask(bots, "POST", "stop", bots.bot)[0] == 200 and wait(lambda: not bots.p.runner.running)
    assert ask(bots, "POST", "close", bots.bot)[1] == {"ok": True, "closed": 2, "still_open": 0} and bots.closed == ["demo"]
    events = [(e["ev"], e["token"]) for e in ask(bots, "GET", "journal", bots.reader)[1]["events"]]
    assert events == [("agent_config", "night bot"), ("agent_start", "night bot"), ("agent_stop", "night bot"), ("agent_close", "night bot")]


def test_revoked_and_wrong_tokens_are_401_then_the_address_is_locked(bots):
    st = bots.p.handle("GET", "/api/status", bots.cookie, "127.0.0.1", {}, None, LOCAL)[1]
    assert post(bots, "/api/tokens/delete", {"id": st["tokens"][0]["id"]})[0] == 200
    assert ask(bots, "GET", "status", bots.reader)[0] == 401                                # revoked
    assert bots.p.handle("GET", "/api/v1/status", bots.cookie, "127.0.0.1", {}, None, LOCAL)[0] == 401   # a cookie is not a token
    for n in range(panel.MAX_FAILS):
        assert ask(bots, "GET", "status", "t4h_guess%d" % n, ip="203.0.113.77")[0] == 401
    assert ask(bots, "GET", "status", bots.bot, ip="203.0.113.77")[0] == 429               # locked, even with a good one
    assert ask(bots, "GET", "status", bots.bot, ip="203.0.113.78")[0] == 200
    bots.now[0] += panel.LOCK_SEC + 1
    assert ask(bots, "GET", "status", bots.bot, ip="203.0.113.77")[0] == 200


def test_requests_per_token_are_limited(bots):
    for _ in range(panel.RATE_PER_MIN):
        assert ask(bots, "GET", "token", bots.reader)[0] == 200
    status, body, _ = ask(bots, "GET", "token", bots.reader)
    assert status == 429 and body["code"] == "rate_limited"
    assert ask(bots, "GET", "token", bots.bot)[0] == 200                                    # counted per token
    bots.now[0] += 61
    assert ask(bots, "GET", "token", bots.reader)[0] == 200


def test_mcp_tools_call_the_token_api(monkeypatch):
    from trend4h import mcp_server
    calls = []

    def call(method, what, body=None):
        calls.append((method, what, body))
        return {"scopes": scopes} if what == "token" else {"ok": True}

    scopes = ["read"]
    assert sorted(mcp_server.tools(call, scopes)) == ["get_config", "journal", "positions", "status", "trades"]
    scopes = ["read", "control"]
    t = mcp_server.tools(call, scopes)
    for name in ("status", "positions", "trades", "journal", "get_config", "start_demo", "stop", "close_positions"):
        assert t[name]() == {"ok": True}
    t["set_config"]({"leverage": 2})
    assert calls == [("GET", "status", None), ("GET", "positions", None), ("GET", "trades", None), ("GET", "journal", None),
                     ("GET", "config", None), ("POST", "start", {}), ("POST", "stop", {}), ("POST", "close", {}),
                     ("POST", "config", {"config": {"leverage": 2}})]

    class Server:
        def __init__(self, name):
            self.name, self.tools = name, {}

        def add_tool(self, fn, name):
            self.tools[name] = fn

    assert len(mcp_server.build(call, Server).tools) == 9
    with pytest.raises(ValueError, match="revoked"):
        mcp_server.build(lambda method, what, body=None: {"ok": False, "error": "missing, wrong or revoked token"}, Server)
    assert mcp_server.http_call("http://127.0.0.1:1", "x")("GET", "status")["code"] == "unreachable"   # nothing listens there
    with pytest.raises(ValueError):
        mcp_server.http_call("file:///etc/passwd", "x")
    monkeypatch.delenv("TREND4H_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="TREND4H_TOKEN"):
        mcp_server.main(NS(url="http://127.0.0.1:1", token_env="TREND4H_TOKEN"))


def test_mcp_tools_register_with_the_real_package():
    pytest.importorskip("mcp")
    from trend4h import mcp_server
    server = mcp_server.build(lambda method, what, body=None: {"scopes": ["read", "control"]}, mcp_server.server_class())
    assert server.name == "free-crypto-trading-bot"


def test_every_site_link_in_the_page_carries_the_four_utm_marks():
    links = re.findall(r'href="([^"]*byttrade\.com[^"]*)"', panel.PAGE)
    assert len(links) == 5 and "{{site" not in panel.PAGE and panel.PAGE.count("byttrade.com/") == len(links)
    places = set()
    for link in links:
        query = dict(part.split("=") for part in link.replace("&amp;", "&").split("?")[1].split("&"))
        assert query.pop("utm_content").startswith("panel_")
        assert query == {"utm_source": "github", "utm_medium": "free_tool", "utm_campaign": "free-crypto-trading-bot"}
        places.add(link.rsplit("=", 1)[1])
    assert places == {"panel_sidebar", "panel_trades", "panel_overview", "panel_strategy", "panel_settings"}
    assert all('target="_blank" rel="noopener noreferrer"' in tag for tag in re.findall(r'<a href="https://byttrade[^>]*>', panel.PAGE))
