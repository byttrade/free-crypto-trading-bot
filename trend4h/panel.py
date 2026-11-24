"""Local control panel: one page to start / stop the executor and watch it. Standard library only.

Security
- listens on 127.0.0.1 by default; any other address is refused unless a login and password are set
  (`python -m trend4h panel-user`); only a salted scrypt hash of the password is stored (file mode 600);
- login form + signed session cookie (HttpOnly, SameSite=Strict), constant-time comparisons,
  delay and temporary lock-out after failed logins, CSRF token on every state-changing request, POST only;
- exchange keys can be written to .env from the Settings page, only over a local or TLS connection (POST, session,
  CSRF); their values are never sent back, logged or put into error texts, the page only learns "set / not set";
- the owner's own agents use /api/v1/* with a bearer token made in Settings (only its sha256 is stored, mode 600):
  never exchange keys, the panel password, tokens or bytx updates, and never a real account;
- no TLS by default: put it behind an HTTPS reverse proxy or use an SSH tunnel (or pass --certfile/--keyfile).
"""
from __future__ import annotations

import asyncio
import getpass
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sys
import threading
import time
from importlib import metadata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from . import config as cfgmod
from . import live

LOCAL = ("127.0.0.1", "localhost", "::1")
LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
PROXY_HEADERS = ("X-Forwarded-For", "X-Forwarded-Proto", "X-Forwarded-Host", "Forwarded", "X-Real-IP", "Via")
ENV_NAMES = {"api_key": "API_KEY", "secret": "SECRET", "password": "PASSWORD",
             "wallet_address": "WALLET_ADDRESS", "private_key": "PRIVATE_KEY"}  # the names live.credentials reads
PASSPHRASE = ("okx", "kucoin", "bitget", "blofin")
SITE = "https://byttrade.com/"
SITE_UTM = "utm_source=github&utm_medium=free_tool&utm_campaign=free-crypto-trading-bot"   # no user data is ever added to these links
RATE_PER_MIN, MAX_TOKENS = 120, 20   # requests per token per minute; tokens per panel
SESSION_SEC = 12 * 3600
MAX_FAILS, LOCK_SEC, FAIL_DELAY = 5, 300, 1.0
_SCRYPT = dict(n=2 ** 14, r=8, p=1, dklen=32)


# ---------------------------------------------------------------- users

def _hash(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT).hex()


def save_user(state_dir, login: str, password: str) -> Path:
    path = Path(state_dir) / "panel-user.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    salt = secrets.token_bytes(16)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"login": login, "salt": salt.hex(), "hash": _hash(password, salt)}, fh)
    os.chmod(path, 0o600)
    return path


def load_user(state_dir):
    path = Path(state_dir) / "panel-user.json"
    return json.loads(path.read_text()) if path.exists() else None


def set_user(state_dir):
    login = input("Panel login: ").strip()
    p1, p2 = getpass.getpass("Password: "), getpass.getpass("Repeat password: ")
    if not login or len(p1) < 8 or p1 != p2:
        sys.exit("not saved: the login must not be empty, the password needs 8+ characters and both entries must match")
    print(f"saved: {save_user(state_dir, login, p1)} (login and password hash; mode 600)")


def check_bind(host: str, user) -> None:
    if host not in LOCAL and not user:
        raise ValueError(f"refusing to listen on {host} without a login and password: run "
                         f"`free-crypto-trading-bot panel-user` first (Docker: `docker compose run --rm bot panel-user`). "
                         f"Use HTTPS or an SSH tunnel: the panel has no TLS of its own unless --certfile/--keyfile are given")


# ---------------------------------------------------------------- exchange keys in .env

def key_fields(exchange: str) -> list[str]:
    if exchange == "hyperliquid":
        return ["wallet_address", "private_key"]
    return ["api_key", "secret"] + (["password"] if exchange in PASSPHRASE else [])


def write_env(path, prefix: str, values: dict) -> None:
    """Replace the <PREFIX>* key lines of .env with `values` (none: delete). Other lines stay as they are.

    Atomic (temporary file + rename), mode 600; the process environment is updated to match.
    """
    path = Path(path)
    names = {prefix + n for n in ENV_NAMES.values()}
    lines = path.read_text().splitlines() if path.exists() else []
    keep = [ln for ln in lines if ln.lstrip().startswith("#") or ln.split("=", 1)[0].strip() not in names]
    keep += [f"{prefix}{ENV_NAMES[f]}={v}" for f, v in values.items()]
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(keep) + "\n" if keep else "")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    for n in names:
        os.environ.pop(n, None)
    for f, v in values.items():
        os.environ[prefix + ENV_NAMES[f]] = v


def scrub(text: str) -> str:
    """Text with every exchange secret of this process masked (journal lines and error texts shown on the page)."""
    for k, v in os.environ.items():
        if len(v) >= 6 and k.endswith(("_API_KEY", "_SECRET", "_PASSWORD", "_PRIVATE_KEY")):
            text = text.replace(v, "***")
    return text


def site_link(place: str) -> str:
    """Link to the BYTtrade site from one place of the panel, e.g. panel_sidebar."""
    return f"{SITE}?{SITE_UTM}&utm_content={place}"


def _private(path: Path, text: str) -> None:
    """Write a file only its owner can read (temporary file + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def close_positions(cfg: dict, state_dir) -> dict:
    """What `python -m trend4h close` does: close the positions this executor opened (others are not touched)."""
    from . import integrity

    async def go():
        live.load_env()
        integrity.check_files("bytx")  # before bytx gets the keys
        from bytx import errors
        ex = live.connect(cfg)
        lock = None
        try:
            lock = live.Lock(Path(state_dir) / f"{cfg['exchange']}-{cfg['account']}.lock")  # no start meanwhile
            x = live.Executor(cfg, ex, errors, state_dir, guard=integrity.Guard(cfg["exchange"], ex.ccxt),
                              out=lambda line: None)
            await x.startup()
            before = len(x.st["pos"])
            await x.close_all()
            return {"closed": before - len(x.st["pos"]), "still_open": len(x.st["pos"])}
        finally:
            await ex.close()
            if lock:
                lock.release()

    return asyncio.run(go())


# ---------------------------------------------------------------- executor runner

class Runner:
    """Runs the same executor as `python -m trend4h live` in a background thread (one instance, same lock)."""

    def __init__(self, state_dir="state", serve=live.serve):
        self.state_dir, self.serve = state_dir, serve
        self.thread = self.stop_event = self.loop = self.executor = None
        self.cfg, self.error, self.restart = None, None, False
        self.lines = []
        self.eq, self.eq_asked = None, 0.0

    @property
    def running(self):
        return bool(self.thread and self.thread.is_alive())

    def start(self, cfg: dict):
        if self.running:
            raise ValueError("already running")
        self.cfg, self.error, self.executor = cfg, None, None
        self.eq, self.eq_asked = None, 0.0

        self.loop = asyncio.new_event_loop()   # made here so that stop() right after start() is not lost
        self.stop_event = asyncio.Event()

        def work():
            try:
                result = self.loop.run_until_complete(self.serve(
                    cfg, self.stop_event, state_dir=self.state_dir, out=self._line,
                    on_executor=lambda x: setattr(self, "executor", x)))
                self.restart = result == "restart"
            except Exception as e:  # shown on the page; keys never appear in bytx error texts
                self.error = f"{type(e).__name__}: {e}"[:500]
            finally:
                loop.close()

        loop = self.loop
        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()

    def _line(self, text):
        self.lines = (self.lines + [text])[-200:]

    def stop(self):
        if self.running:
            try:
                self.loop.call_soon_threadsafe(self.stop_event.set)
            except RuntimeError:  # the loop has just finished
                pass

    def update_now(self):
        up = getattr(self.executor, "updater", None)
        if not up:
            raise ValueError("the executor is not running")
        up.force = True

    def equity(self):
        """Last account equity read while running, {value, at}; asked in the executor's own loop once a minute."""
        x = self.executor
        if self.running and getattr(x, "ex", None) is not None and time.time() - self.eq_asked > 60:
            self.eq_asked = time.time()

            def done(f):
                if not f.cancelled() and not f.exception():
                    self.eq = {"value": round(f.result(), 2), "at": time.time(), "live": True}
            try:
                asyncio.run_coroutine_threadsafe(x.equity(), self.loop).add_done_callback(done)
            except RuntimeError:  # the loop has just finished
                pass
        return self.eq if self.running else None

    def status(self) -> dict:
        x = self.executor
        up = getattr(x, "updater", None)
        return {"running": self.running, "error": self.error and scrub(self.error), "config": self.cfg,
                "summary": x.summary() if x else None, "journal": [scrub(s) for s in self.lines[-40:]],
                "bytx": up.status() if up else None}


# ---------------------------------------------------------------- http

class Panel:
    def __init__(self, runner: Runner, user=None, clock=time.time, sleep=time.sleep, env_path=".env", https_proxy=False,
                 closer=close_positions, local_publish=False):
        self.runner, self.user, self.clock, self.sleep, self.closer = runner, user, clock, sleep, closer
        self.env_path, self.https_proxy, self.local_publish = env_path, https_proxy, local_publish
        self.dir = Path(runner.state_dir)
        self.secret = secrets.token_bytes(32)
        self.fails = {}  # ip -> (count, locked_until)
        self.env_lock = threading.Lock()
        saved = self.dir / "panel-config.json"
        self.config = cfgmod.validate(json.loads(saved.read_text())) if saved.exists() else dict(cfgmod.DEFAULTS)
        self.tokens_file = self.dir / "panel-tokens.json"
        self.tokens = json.loads(self.tokens_file.read_text()) if self.tokens_file.exists() else []
        self.rate, self.tokens_saved = {}, 0.0  # token id -> (minute start, requests)

    def sign(self, text: str) -> str:
        return hmac.new(self.secret, text.encode(), hashlib.sha256).hexdigest()

    def new_session(self, login: str) -> str:
        body = f"{login}|{int(self.clock()) + SESSION_SEC}|{secrets.token_hex(8)}"
        return f"{body}|{self.sign(body)}"

    def session_ok(self, cookie: str | None) -> bool:
        if not cookie or cookie.count("|") != 3:
            return False
        body, sig = cookie.rsplit("|", 1)
        login, expires, _ = body.split("|")
        if not hmac.compare_digest(sig, self.sign(body)) or not expires.isdigit() or int(expires) < self.clock():
            return False
        return login == "local" if not self.user else hmac.compare_digest(login, self.user["login"])

    def csrf(self, cookie: str) -> str:
        return self.sign("csrf:" + cookie)

    def login(self, ip: str, login: str, password: str):
        """(status, session cookie or None). Failures are delayed and lock the address out for a while."""
        count, until = self.fails.get(ip, (0, 0))
        if until > self.clock():
            return 429, None
        u = self.user
        ok = bool(u) and hmac.compare_digest(login.encode(), u["login"].encode()) and hmac.compare_digest(
            _hash(password, bytes.fromhex(u["salt"])), u["hash"])
        if ok:
            self.fails.pop(ip, None)
            return 200, self.new_session(u["login"])
        count += 1
        self.fails[ip] = (0, self.clock() + LOCK_SEC) if count >= MAX_FAILS else (count, 0)
        self.sleep(FAIL_DELAY)
        return 401, None

    def keys_found(self, exchange, account) -> bool:
        live.load_env(self.env_path)
        return bool(live.credentials(exchange, account))

    def secrets_allowed(self, ip, conn) -> bool:
        """Secrets (exchange keys, a new panel password) are accepted only over a local or TLS connection.

        `conn`: {"tls", "forwarded", "proto", "host"} of the request. Behind a reverse proxy the socket address is
        the proxy's, so a forwarded request counts as safe only with --behind-https-proxy and X-Forwarded-Proto: https.
        """
        c = conn or {}
        if c.get("tls"):
            return True
        if c.get("forwarded"):
            return bool(self.https_proxy) and c.get("proto") == "https"
        host = c.get("host")  # a foreign name pointed at 127.0.0.1 (DNS rebinding) is not local use
        if self.local_publish:
            # --trust-local-publish (a container whose port is published to 127.0.0.1 of its host): the socket address
            # is the container gateway for everyone, so the address the browser used decides. Opened as
            # http://127.0.0.1:port the traffic stays on the host; opened by the host's name or IP it is refused.
            return bool(host) and host.rsplit(":", 1)[0].strip("[]") in LOCAL
        return ip in LOOPBACK and (host is None or host.rsplit(":", 1)[0].strip("[]") in LOCAL)

    def _config(self, raw) -> dict:
        cfg = cfgmod.validate(raw if isinstance(raw, dict) else {})
        if not cfg["exchange"].isalnum():  # it becomes a folder name in the state directory
            raise ValueError("exchange: a name such as bybit")
        return cfg

    def _folder(self, cfg) -> Path:
        return self.dir / f"{cfg['exchange']}-{cfg['account']}"

    def journal(self, cfg, limit=300) -> list[dict]:
        """The last events of the executor's journal for this account, oldest first."""
        f = self._folder(cfg) / "journal.jsonl"
        if not f.exists():
            return []
        with open(f, "rb") as fh:
            fh.seek(max(0, f.stat().st_size - 262144))
            lines = fh.read().decode("utf-8", "replace").splitlines()[-limit:]
        out = []
        for line in lines:
            try:
                rec = json.loads(scrub(line))
            except ValueError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
        return out

    def stored_summary(self, cfg) -> dict:
        """What Executor.summary() shows, read from the state file while the executor is not running."""
        f = self._folder(cfg) / "state.json"
        st = json.loads(f.read_text()) if f.exists() else {}
        closed = st.get("closed", [])
        return {"open": list(st.get("pos", {}).values()), "closed": closed[-20:], "trades": len(closed),
                "sum_R": round(sum(c.get("R") or 0 for c in closed), 2),
                "pnl_usdt": round(sum(c.get("pnl") or 0 for c in closed), 2),
                "last_bar": st.get("last_bar", 0), "blocked": None}

    def balance(self, cfg):
        """{value, at, live}: read from the exchange while running, otherwise the equity logged at the last start."""
        eq = self.runner.equity()
        if eq:
            return eq
        for rec in reversed(self.journal(cfg)):
            if rec.get("ev") == "start" and isinstance(rec.get("equity"), (int, float)):
                return {"value": rec["equity"], "at": rec.get("t", 0) / 1000, "live": False}
        return None

    def bytx(self, cfg) -> dict:
        up = getattr(self.runner.executor, "updater", None) if self.runner.running else None
        if up:
            return up.status()
        try:
            current = metadata.version("bytx")
        except metadata.PackageNotFoundError:
            current = None
        f = self.dir / "update.json"
        st = json.loads(f.read_text()) if f.exists() else {}
        return {"mode": cfg["auto_update"], "current": current, "available": st.get("available"),
                "incompatible": st.get("incompatible"), "checked_at": st.get("checked_at"),
                "own": __version__, "own_available": st.get("own_available")}

    def status(self, cookie, ip, conn) -> dict:
        r = self.runner
        st = r.status()
        active = r.cfg if r.running else None
        shown = active or self.config
        try:
            exchanges = live.supported_exchanges()
        except ImportError:
            exchanges = []
        live.load_env(self.env_path)
        x = r.executor if r.running else None
        now = int(self.clock() * 1000)
        return {**st, "config": self.config, "active": active, "csrf": self.csrf(cookie),
                "summary": x.summary() if x else self.stored_summary(shown), "ready": x is not None,
                "now": now, "next_bar": (now // live.STEP + 1) * live.STEP, "balance": self.balance(shown),
                "bytx": self.bytx(shown), "defaults": cfgmod.DEFAULTS, "research_keys": cfgmod.RESEARCH_KEYS,
                "exchanges": exchanges, "strategies": cfgmod.STRATEGIES, "disclaimer": live.DISCLAIMER,
                "phrase": live.ACCOUNT_PHRASE, "warnings": cfgmod.warnings(shown),
                "keys_found": bool(live.credentials(shown["exchange"], shown["account"])),
                "keys": {e: {a: bool(live.credentials(e, a)) for a in ("demo", "real")} for e in exchanges},
                "key_fields": {e: key_fields(e) for e in exchanges},
                "keys_writable": self.secrets_allowed(ip, conn), "login": self.user["login"] if self.user else None,
                "tokens": [{k: v for k, v in t.items() if k != "hash"} for t in self.tokens]}

    def set_keys(self, body, delete=False):
        """Write or delete the keys of one account in .env. Values never appear in the answer or in error texts."""
        exchange, account = str(body.get("exchange", "")), str(body.get("account", ""))
        if exchange not in live.supported_exchanges() or account not in ("demo", "real"):
            raise ValueError("unknown exchange or account")
        r = self.runner
        if r.running and r.cfg and (r.cfg["exchange"], r.cfg["account"]) == (exchange, account):
            return 409, {"error": "the executor is running on this account: stop it before changing its keys",
                         "code": "keys_in_use"}, None
        values = {}
        if not delete:
            given = body.get("fields") if isinstance(body.get("fields"), dict) else {}
            for f in key_fields(exchange):
                v = given.get(f)
                v = v.strip() if isinstance(v, str) else ""
                if not v or len(v) > 512 or not v.isascii() or not v.isprintable() or any(ch in v for ch in " '\""):
                    raise ValueError(f"{f}: required, printable characters without spaces or quotes")
                values[f] = v
        with self.env_lock:
            write_env(self.env_path, f"{exchange}_{account}_".upper(), values)
        return 200, {"ok": True, "set": bool(values)}, None

    def set_password(self, ip, body):
        if self.user and self.login(ip, self.user["login"], str(body.get("current", "")))[0] != 200:
            return 403, {"error": "the current password is wrong", "code": "bad_password"}, None
        login, password = str(body.get("login", "")).strip(), str(body.get("password", ""))
        if not login or "|" in login or len(password) < 8:
            raise ValueError("the login must not be empty and the password needs 8+ characters")
        save_user(self.dir, login, password)
        self.user = load_user(self.dir)
        return 200, {"ok": True}, self.new_session(login)

    def save_config(self, cfg) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "panel-config.json").write_text(json.dumps(cfg, indent=1))
        self.config = cfg

    # ------------------------------------------------------------ agents

    def save_tokens(self) -> None:
        _private(self.tokens_file, json.dumps(self.tokens, indent=1))
        self.tokens_saved = self.clock()

    def set_token(self, body, delete=False):
        """Create (the value is returned once, only its sha256 is kept) or revoke an agent token."""
        if delete:
            self.tokens = [t for t in self.tokens if t["id"] != body.get("id")]
            self.save_tokens()
            return 200, {"ok": True}, None
        name = str(body.get("name", "")).strip()
        if not 1 <= len(name) <= 40 or not name.isprintable() or len(self.tokens) >= MAX_TOKENS:
            raise ValueError(f"the name needs 1 to 40 characters; at most {MAX_TOKENS} tokens")
        value = "t4h_" + secrets.token_urlsafe(32)
        self.tokens.append({"id": secrets.token_hex(4), "name": name,
                            "scopes": ["read", "control"] if body.get("control") else ["read"],
                            "hash": hashlib.sha256(value.encode()).hexdigest(), "created": int(self.clock()), "last_used": None})
        self.save_tokens()
        return 200, {"ok": True, "token": value}, None

    def audit(self, cfg, event, **kw) -> None:
        """An agent's action in the journal of the account, next to the executor's own events."""
        folder = self._folder(cfg)
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / "journal.jsonl", "a") as fh:
            fh.write(json.dumps({"t": int(self.clock() * 1000), "ev": event, **kw}, ensure_ascii=False) + "\n")

    def agent(self, method, path, ip, body, auth):
        """/api/v1/*: a bearer token instead of the cookie and CSRF.

        Fixed limits for every token: only the routes below exist here (no keys, password, tokens or bytx update),
        and everything that changes something works on the demo account only.
        """
        count, until = self.fails.get("token:" + ip, (0, 0))
        if until > self.clock():
            return 429, {"error": "too many wrong tokens from this address: try later", "code": "locked"}, None
        given = hashlib.sha256(auth[7:].strip().encode()).hexdigest() if isinstance(auth, str) and auth.startswith("Bearer ") else ""
        tok = None
        for t in self.tokens:  # every stored hash is compared, in constant time
            if hmac.compare_digest(t["hash"], given):
                tok = t
        if not tok:
            count += 1
            self.fails["token:" + ip] = (0, self.clock() + LOCK_SEC) if count >= MAX_FAILS else (count, 0)
            self.sleep(FAIL_DELAY)
            return 401, {"error": "missing, wrong or revoked token", "code": "unauthorized"}, None
        self.fails.pop("token:" + ip, None)
        now = self.clock()
        start, n = self.rate.get(tok["id"], (0, 0))
        if now - start >= 60:
            start, n = now, 0
        self.rate[tok["id"]] = (start, n + 1)
        if n >= RATE_PER_MIN:
            return 429, {"error": f"more than {RATE_PER_MIN} requests a minute", "code": "rate_limited"}, None
        tok["last_used"] = int(now)
        if now - self.tokens_saved > 60:
            self.save_tokens()

        r, what = self.runner, path[len("/api/v1/"):]
        shown = r.cfg if r.running else self.config
        if method == "GET":
            full = self.status("", ip, None)
            m = full["summary"]
            if what == "token":
                return 200, {"name": tok["name"], "scopes": tok["scopes"]}, None
            if what == "status":
                return 200, {"running": full["running"], "ready": full["ready"], "error": full["error"],
                             "exchange": shown["exchange"], "account": shown["account"], "blocked": m.get("blocked"),
                             "now": full["now"], "next_bar": full["next_bar"], "balance": full["balance"],
                             "open_positions": len(m["open"]), "trades": m["trades"], "sum_R": m["sum_R"],
                             "pnl_usdt": m["pnl_usdt"], "keys_found": full["keys_found"], "warnings": full["warnings"]}, None
            if what == "positions":
                return 200, {"positions": m["open"]}, None
            if what == "trades":
                return 200, {"trades": m["closed"], "total": m["trades"], "sum_R": m["sum_R"], "pnl_usdt": m["pnl_usdt"]}, None
            if what == "journal":
                return 200, {"events": self.journal(shown)}, None
            if what == "config":
                return 200, {"config": self.config, "running_config": r.cfg if r.running else None,
                             "defaults": cfgmod.DEFAULTS, "warnings": cfgmod.warnings(self.config)}, None
            return 404, {"error": "not found", "code": "not_found"}, None
        if method != "POST" or what not in ("config", "start", "stop", "close"):
            return 404, {"error": "not found", "code": "not_found"}, None
        if "control" not in tok["scopes"]:
            return 403, {"error": "this token can only read: create one with the control right", "code": "scope"}, None

        def refuse(reason, code="real_forbidden"):
            self.audit(shown, "agent_refused", token=tok["name"], action=what, reason=reason)
            return 403, {"error": reason, "code": code}, None

        real = "the real account is only for a person in the panel: tokens work on demo"
        try:
            if what == "config":
                given_cfg = body.get("config") if isinstance(body.get("config"), dict) else {}
                cfg = self._config({**self.config, **given_cfg})
                if cfg["account"] != "demo":
                    return refuse(real)
                if any(cfg[k] != self.config[k] for k in ("auto_update", "update_margin_min")):
                    return refuse("bytx update settings are changed only by a person in the panel", "forbidden")
                changed = sorted(k for k in cfg if cfg[k] != self.config[k])
                self.save_config(cfg)
                self.audit(cfg, "agent_config", token=tok["name"], changed=changed)
                return 200, {"ok": True, "config": cfg, "warnings": cfgmod.warnings(cfg)}, None
            if what == "start":
                cfg = self.config
                if cfg["account"] != "demo":
                    return refuse(real)
                if not self.keys_found(cfg["exchange"], cfg["account"]):
                    return 400, {"error": f"no API keys for {cfg['exchange']} demo: a person adds them in the panel",
                                 "code": "no_keys"}, None
                r.start(cfg)
                self.audit(cfg, "agent_start", token=tok["name"])
                return 200, {"ok": True, "warnings": cfgmod.warnings(cfg)}, None
            if shown["account"] != "demo":
                return refuse(real)
            if what == "stop":
                r.stop()
                self.audit(shown, "agent_stop", token=tok["name"])
                return 200, {"ok": True}, None
            if r.running:
                return 409, {"error": "stop the executor first, then close its positions", "code": "running"}, None
            self.audit(shown, "agent_close", token=tok["name"])
            return 200, {"ok": True, **self.closer(shown, self.dir)}, None
        except (ValueError, TypeError) as e:
            return 400, {"error": scrub(str(e)), "code": "bad_request"}, None
        except Exception as e:  # exchange or lock trouble while closing; nothing is re-sent
            return 502, {"error": scrub(f"{type(e).__name__}: {e}")[:300], "code": "failed"}, None

    def handle(self, method, path, cookie, ip, body: dict, csrf_header, conn=None):
        """Pure request logic: returns (status, json-able payload or html string, new cookie or None)."""
        if path.startswith("/api/v1/"):
            return self.agent(method, path, ip, body, (conn or {}).get("auth"))
        if method == "GET" and path == "/":
            if not self.user and not self.session_ok(cookie):
                cookie = self.new_session("local")  # local use without a password still gets a session for CSRF
                return 200, PAGE, cookie
            return 200, PAGE, None
        if method == "POST" and path == "/api/login":
            status, new = self.login(ip, str(body.get("login", "")), str(body.get("password", "")))
            return status, {"ok": status == 200}, new
        if not self.session_ok(cookie):
            return 401, {"error": "login required"}, None
        if method == "GET" and path == "/api/status":
            return 200, self.status(cookie, ip, conn), None
        if method == "GET" and path == "/api/journal":
            r = self.runner
            return 200, {"events": self.journal(r.cfg if r.running else self.config)}, None
        if method != "POST":
            return 405, {"error": "POST only"}, None
        if not csrf_header or not hmac.compare_digest(csrf_header, self.csrf(cookie)):
            return 403, {"error": "CSRF token missing or wrong"}, None
        try:
            if path == "/api/config":
                cfg = self._config(body.get("config"))
                self.save_config(cfg)
                return 200, {"ok": True, "config": cfg, "warnings": cfgmod.warnings(cfg)}, None
            if path == "/api/start":
                cfg = self._config(body["config"]) if body.get("config") else self.config
                if cfg["account"] == "real" and body.get("confirm") != live.ACCOUNT_PHRASE:
                    return 400, {"error": f'real account: type "{live.ACCOUNT_PHRASE}" to confirm', "code": "phrase"}, None
                if not self.keys_found(cfg["exchange"], cfg["account"]):
                    return 400, {"error": f"no API keys for {cfg['exchange']} {cfg['account']} in .env on the server",
                                 "code": "no_keys"}, None
                self.runner.start(cfg)
                return 200, {"ok": True, "warnings": cfgmod.warnings(cfg)}, None
            if path == "/api/stop":
                self.runner.stop()
                return 200, {"ok": True}, None
            if path == "/api/update":
                self.runner.update_now()
                return 200, {"ok": True}, None
            if path == "/api/logout":
                return 200, {"ok": True}, "deleted"
            if path in ("/api/keys", "/api/keys/delete", "/api/password", "/api/tokens", "/api/tokens/delete"):
                if not self.secrets_allowed(ip, conn):
                    return 403, {"error": "secrets are accepted only over a local or HTTPS connection: use an SSH "
                                          "tunnel, an HTTPS proxy with --behind-https-proxy, --certfile, or edit .env "
                                          "on the server", "code": "insecure"}, None
                if path == "/api/password":
                    return self.set_password(ip, body)
                if path.startswith("/api/tokens"):
                    return self.set_token(body, delete=path.endswith("/delete"))
                return self.set_keys(body, delete=path.endswith("/delete"))
        except (ValueError, TypeError) as e:
            return 400, {"error": str(e)}, None
        except OSError as e:
            return 500, {"error": f"cannot write the file: {e.strerror}"}, None
        return 404, {"error": "not found"}, None


def make_handler(panel: Panel, secure: bool):
    class Handler(BaseHTTPRequestHandler):
        def _name(self):  # cookies ignore the port: a second panel on this host must not replace our session
            return f"t4h{self.server.server_port}"

        def _cookie(self):
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == self._name():
                    return v
            return None

        def _reply(self, method):
            body = {}
            if method == "POST":
                n = min(int(self.headers.get("Content-Length") or 0), 65536)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    body = None
                if not isinstance(body, dict):
                    body = {}
            conn = {"tls": secure, "forwarded": any(self.headers.get(h) for h in PROXY_HEADERS),
                    "proto": (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower(),
                    "host": self.headers.get("Host") or "", "auth": self.headers.get("Authorization")}
            status, payload, cookie = panel.handle(method, self.path.split("?")[0], self._cookie(),
                                                   self.client_address[0], body, self.headers.get("X-CSRF-Token"), conn)
            data = payload.encode() if isinstance(payload, str) else json.dumps(payload, default=float).encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8" if isinstance(payload, str) else "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; "
                             "style-src 'unsafe-inline'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
            if cookie:
                value, age = ("", 0) if cookie == "deleted" else (cookie, SESSION_SEC)
                self.send_header("Set-Cookie", f"{self._name()}={value}; Path=/; Max-Age={age}; HttpOnly; SameSite=Strict"
                                 + ("; Secure" if secure else ""))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._reply("GET")

        def do_POST(self):
            self._reply("POST")

        def log_message(self, fmt, *args):  # request lines only on errors; bodies are never logged
            if args and str(args[1])[:1] in "45":
                sys.stderr.write(f"panel: {self.client_address[0]} {args[0]} -> {args[1]}\n")
    return Handler


def main(a):
    user = load_user(a.state_dir)
    check_bind(a.host, user)
    runner = Runner(a.state_dir)
    auto = Path(a.state_dir) / "panel-autostart.json"
    if auto.exists():  # the panel restarted itself after a bytx update: continue with the same settings
        runner.start(cfgmod.validate(json.loads(auto.read_text())))
        auto.unlink()
    server = ThreadingHTTPServer((a.host, a.port), make_handler(
        Panel(runner, user, https_proxy=getattr(a, "behind_https_proxy", False),
              local_publish=getattr(a, "trust_local_publish", False)), bool(a.certfile)))
    if a.certfile:
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(a.certfile, a.keyfile)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
    print(live.DISCLAIMER)
    print(f"panel: http{'s' if a.certfile else ''}://{a.host}:{a.port}  "
          f"({'login required' if user else 'no password: local use only'})")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        while not runner.restart:
            time.sleep(1)
        auto.write_text(json.dumps(runner.cfg))  # bytx was updated: a fresh process loads the new files
        os.execv(sys.executable, [sys.executable, "-m", "trend4h", *sys.argv[1:]])
    except KeyboardInterrupt:
        runner.stop()
        if runner.thread:
            runner.thread.join(30)


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Free Crypto Trading Bot by BYTtrade</title>
<script>(function(){var p=null;try{p=localStorage.getItem("t4h.theme")}catch(e){}
document.documentElement.dataset.theme=p==="light"?"light":p==="system"&&!matchMedia("(prefers-color-scheme: dark)").matches?"light":"dark"})()</script>
<style>
:root{--bg:#F3F5F1;--panel:#FFFFFF;--sunk:#E8ECE3;--ink:#0B0F14;--mute:#5B6573;--line:#DCE1D6;--accent:#4D7C0F;--accent-ink:#FFFFFF;--brand:#4D7C0F;
--soft:#EEF5DC;--up:#0E8A43;--down:#C8104C;--warn:#8A5A00;--warn-bg:#FBF0D9;--danger:#C8104C;--danger-bg:#FCE4EC;--shade:rgba(11,15,20,.45);
--sans:Manrope,ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
--mono:"IBM Plex Mono",ui-monospace,"SF Mono",SFMono-Regular,"Cascadia Mono","Roboto Mono",Menlo,Consolas,monospace;color-scheme:light}
:root[data-theme=dark]{--bg:#07090C;--panel:#10141A;--sunk:#0B0F14;--ink:#E8EEF4;--mute:#8B96A5;--line:#242C36;--accent:#C8F542;--accent-ink:#111111;--brand:#C8F542;
--soft:rgba(200,245,66,.1);--up:#17C964;--down:#F31260;--warn:#F5A524;--warn-bg:#241C0C;--danger:#F31260;--danger-bg:#2A0E17;--shade:rgba(0,0,0,.65);color-scheme:dark}
*{box-sizing:border-box}[hidden]{display:none!important}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 var(--sans);overflow-wrap:break-word}
.jd,.note,.err,.hint,.disc{overflow-wrap:anywhere}
h1,h2,h3,p{margin:0}h1{font-size:24px;line-height:1.2;font-weight:650;letter-spacing:-.01em}h2{font-size:17px;font-weight:650}h3{font-size:15px;font-weight:650}
button,input,select{font:inherit;color:inherit}
svg.i{width:18px;height:18px;flex:none;fill:none;stroke:currentColor;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.mute{color:var(--mute)}.small{font-size:13px}.up{color:var(--up)}.down{color:var(--down)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
a,.link{color:var(--accent);background:none;border:0;padding:0;cursor:pointer;text-decoration:underline;text-underline-offset:3px}

.app{display:grid;grid-template-columns:236px minmax(0,1fr);min-height:100vh}
.side{position:sticky;top:0;height:100vh;overflow-y:auto;display:flex;flex-direction:column;gap:22px;padding:22px 14px 16px;background:var(--panel);border-right:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:10px;padding:0 10px;font-weight:700;font-size:17px;letter-spacing:-.01em}
.brand svg{width:34px;height:22px;color:var(--ink);stroke:none}
.bname{display:flex;flex-direction:column;line-height:1.2;font-size:15px;font-weight:800}.bname i{font-style:normal;color:var(--brand)}.side .bname i{display:block}
.bname small{font-weight:500;font-size:12px;color:var(--mute);letter-spacing:0;margin-top:2px}
.nav{display:flex;flex-direction:column;gap:2px}
.nav button{display:flex;align-items:center;gap:11px;padding:9px 10px;border:0;border-radius:8px;background:none;cursor:pointer;color:var(--mute);font-weight:550;text-align:left}
.nav button:hover{color:var(--ink);background:var(--sunk)}
.nav button[aria-current=page]{color:var(--accent);background:var(--soft)}
.foot{margin-top:auto;display:flex;flex-direction:column;gap:12px}
.risk{display:flex;gap:9px;align-items:flex-start;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--warn-bg);color:var(--ink);font-size:12.5px;line-height:1.4;text-align:left;cursor:pointer;width:100%}
.risk svg{color:var(--warn);margin-top:1px}.risk u{color:var(--warn);text-underline-offset:2px;white-space:nowrap}
.quick{display:flex;gap:8px}
.byt{display:flex;flex-direction:column;gap:3px;padding:10px;border:1px solid var(--line);border-radius:8px;font-size:12.5px;line-height:1.4;color:var(--mute)}
.byt b{color:var(--ink);font-size:13px}.byt a{align-self:flex-start}.only-m{display:none}
.promo{position:relative;display:flex;gap:2px 12px;align-items:baseline;padding:10px 44px 10px 14px;border:1px dashed var(--line);border-radius:10px;font-size:14px;color:var(--mute)}
.promo b{color:var(--accent);font-weight:650}.promo>span{flex:1;min-width:0}
.promo .x{border:0;background:none;color:var(--mute);cursor:pointer;padding:6px;border-radius:6px;position:absolute;top:5px;right:6px;display:flex}.promo .x:hover{color:var(--ink)}.promo .x svg{width:15px;height:15px}
.trow{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(0,1fr) minmax(0,1.6fr) auto;gap:4px 16px;align-items:center;padding:9px 0;border-top:1px solid var(--line);font-size:14px}
.trow:first-child{border-top:0}.trow b{font-weight:600}
.top{display:none}
main{padding:30px 36px 72px;max-width:1120px;width:100%}
.page{display:flex;flex-direction:column;gap:18px}
.lead{color:var(--mute);max-width:68ch;margin-top:6px}

.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px}
.panel>header{display:flex;align-items:baseline;justify-content:space-between;gap:12px;padding:16px 20px 0;flex-wrap:wrap}
.panel>.body{padding:16px 20px 20px}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;padding:7px 13px;border:1px solid var(--line);border-radius:8px;background:var(--panel);cursor:pointer;font-weight:550;white-space:nowrap;line-height:1.3}
.btn:hover{border-color:var(--mute)}.btn:disabled{opacity:.5;cursor:not-allowed}
.btn.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink);font-weight:700}
.btn.primary:hover{filter:brightness(1.08)}
.btn.danger{background:var(--danger);border-color:var(--danger);color:var(--panel)}
.btn.stop{border-color:var(--danger);color:var(--danger)}
.btn.big{padding:12px 26px;font-size:16px;border-radius:10px;min-width:150px}
.btn.sm{padding:4px 10px;font-size:13px}
.icon-btn{display:inline-flex;align-items:center;justify-content:center;width:36px;height:34px;border:1px solid var(--line);border-radius:8px;background:var(--panel);cursor:pointer;color:var(--mute);font-weight:650;font-size:12.5px}
.icon-btn:hover{color:var(--ink);border-color:var(--mute)}
.seg{display:inline-flex;padding:3px;border-radius:9px;background:var(--sunk);gap:2px;max-width:100%;flex-wrap:wrap}
.seg button{border:0;background:none;padding:5px 13px;border-radius:7px;cursor:pointer;color:var(--mute);font-weight:550}
.seg button[aria-pressed=true]{background:var(--panel);color:var(--ink);box-shadow:0 0 0 1px var(--line)}
.seg button.real[aria-pressed=true]{color:var(--danger)}
input[type=text],input[type=password],select{width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--panel);min-width:0}
input.num{max-width:170px}
input:focus-visible,select:focus-visible{outline-offset:0}
.field{display:flex;flex-direction:column;gap:5px;min-width:0}
.field>label,.label{font-weight:550;font-size:14px}
.hint{color:var(--mute);font-size:13px;line-height:1.45;max-width:62ch}
.grid2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px 28px}
.note{display:flex;gap:10px;align-items:flex-start;padding:11px 14px;border-radius:10px;background:var(--warn-bg);border:1px solid var(--line);font-size:14px}
.note svg{color:var(--warn);margin-top:2px}.note.bad{background:var(--danger-bg)}.note.bad svg{color:var(--danger)}
.note.info{background:var(--soft)}.note.info svg{color:var(--accent)}
.note>div{min-width:0;flex:1}
.badge{display:inline-flex;align-items:center;gap:6px;font-size:13px;font-weight:550;white-space:nowrap}
.badge::before{content:"";width:8px;height:8px;border-radius:50%;background:var(--mute);opacity:.5}
.badge.ok::before{background:var(--up);opacity:1}

/* overview */
.hero-main{display:flex;justify-content:space-between;align-items:center;gap:20px;padding:22px 24px;flex-wrap:wrap}
.state{display:flex;align-items:center;gap:12px}
.dot{width:12px;height:12px;border-radius:50%;background:var(--mute);flex:none}
.dot.run{background:var(--up);box-shadow:0 0 0 4px color-mix(in srgb,var(--up) 22%,transparent)}
.dot.bad{background:var(--danger);box-shadow:0 0 0 4px color-mix(in srgb,var(--danger) 22%,transparent)}
.dot.wait{background:var(--warn)}
.hero-sub{color:var(--mute);margin-top:4px}
.tag{display:inline-block;padding:1px 8px;border-radius:999px;border:1px solid var(--line);font-size:12.5px;font-weight:600;vertical-align:1px}
.tag.real{border-color:var(--danger);color:var(--danger)}
.clock{padding:18px 24px 20px;border-top:1px solid var(--line)}
.clock-head{display:flex;align-items:baseline;gap:6px 14px;flex-wrap:wrap;margin-bottom:12px}
.clock-head b{font-size:26px;font-weight:600;line-height:1}
.day{display:grid;grid-template-columns:repeat(6,1fr);gap:4px}
.day i{display:block;font-style:normal}
.day i>b{display:block;height:8px;border-radius:3px;background:var(--sunk);overflow:hidden}
.day i>b>s{display:block;height:100%;background:var(--accent);border-radius:3px}
.day i.past>b{background:color-mix(in srgb,var(--accent) 32%,var(--sunk))}
.day i>span{display:block;margin-top:5px;font-size:12px;color:var(--mute)}
.day i.now>span{color:var(--ink);font-weight:600}
.figs{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));border-top:1px solid var(--line)}
.figs>div{padding:16px 24px 18px;border-left:1px solid var(--line);min-width:0}
.figs>div:first-child{border-left:0}
.figs span{display:block;color:var(--mute);font-size:13px}
.figs b{display:block;font-size:22px;font-weight:600;margin-top:2px;white-space:nowrap}
.figs small{display:block;color:var(--mute);font-size:12px;margin-top:2px}
table{border-collapse:collapse;width:100%}
th{font-weight:550;font-size:13px;color:var(--mute);text-align:left;padding:8px 12px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:10px 12px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:0}
th:first-child,td:first-child{padding-left:20px}th:last-child,td:last-child{padding-right:20px}
td.r,th.r{text-align:right}td.num{white-space:nowrap}td .mute{font-family:var(--sans);white-space:normal}
.tablewrap{padding:8px 0 6px}
.side-long{color:var(--up);font-weight:600}.side-short{color:var(--down);font-weight:600}
.empty{display:flex;flex-direction:column;align-items:center;text-align:center;gap:6px;padding:30px 20px 34px;color:var(--mute)}
.empty svg{width:30px;height:30px;stroke-width:1.4;opacity:.7}.empty b{color:var(--ink);font-weight:600}.empty p{max-width:46ch;font-size:14px}

/* strategy */
.strats{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.strat{display:flex;gap:12px;align-items:flex-start;padding:14px;border:1px solid var(--line);border-radius:10px;cursor:pointer;background:var(--panel);text-align:left}
.strat input{position:absolute;opacity:0;pointer-events:none}
.strat .box{flex:none;width:20px;height:20px;border-radius:6px;border:1.5px solid var(--mute);display:grid;place-items:center;margin-top:1px;color:transparent}
.strat .box svg{width:14px;height:14px;stroke-width:2.4}
.strat.on{border-color:var(--accent);background:var(--soft)}
.strat.on .box{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
.strat:focus-within{outline:2px solid var(--accent);outline-offset:2px}
.strat p{color:var(--mute);font-size:13.5px;margin-top:3px}.strat .exit{margin-top:8px;font-size:12.5px;color:var(--ink)}
.strat code{font-family:var(--mono);font-size:12px;color:var(--mute)}
.chips{display:flex;flex-wrap:wrap;gap:7px}
.chip{padding:5px 12px;border-radius:999px;border:1px solid var(--line);background:var(--panel);cursor:pointer;font-family:var(--mono);font-size:13px;color:var(--mute)}
.chip[aria-pressed=true]{background:var(--soft);border-color:var(--accent);color:var(--ink);font-weight:600}
.chip.extra[aria-pressed=true]{background:var(--warn-bg);border-color:var(--warn)}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.savebar{position:sticky;bottom:16px;display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;padding:12px 16px;background:var(--panel);border:1px solid var(--accent);border-radius:12px;box-shadow:0 8px 24px -12px var(--shade)}

/* settings */
.krow{display:grid;grid-template-columns:130px repeat(2,minmax(0,1fr));gap:8px 16px;align-items:center;padding:11px 20px;border-top:1px solid var(--line)}
.kname{font-weight:600}
.kcell{display:flex;align-items:center;gap:8px;flex-wrap:wrap;min-width:0}
.kacc{font-size:13px;color:var(--mute);width:38px}
.kcell .badge{min-width:86px}
.dl{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:8px 24px;align-items:baseline}
.dl dt{color:var(--mute)}.dl dd{margin:0}
ul.how{margin:8px 0 0;padding-left:18px}ul.how li{margin:3px 0}
code{font-family:var(--mono);font-size:.92em}

/* journal */
.jrow{display:grid;grid-template-columns:150px 200px minmax(0,1fr);gap:4px 16px;padding:9px 20px;border-top:1px solid var(--line);font-size:14px}
.jrow:first-child{border-top:0}
.jrow time{color:var(--mute);font-size:13px}
.jev{font-weight:600;display:flex;gap:8px;align-items:baseline}
.jev::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--line);flex:none;transform:translateY(-1px)}
.jrow.bad .jev::before{background:var(--danger)}.jrow.warn .jev::before{background:var(--warn)}
.jrow.bad .jev{color:var(--danger)}
.jd{color:var(--ink);font-size:13px}.jd i{font-style:normal;color:var(--mute)}.jd span{margin-right:12px;display:inline-block;max-width:100%}

/* dialogs */
dialog{border:1px solid var(--line);border-radius:14px;background:var(--panel);color:var(--ink);padding:0;width:min(520px,calc(100vw - 24px));max-height:calc(100vh - 24px)}
dialog::backdrop{background:var(--shade)}
dialog form{display:flex;flex-direction:column;gap:14px;padding:22px}
dialog .actions{display:flex;justify-content:flex-end;gap:10px;flex-wrap:wrap;margin-top:4px}
dialog.real{border:2px solid var(--danger)}
dialog.real h2{color:var(--danger)}
.disc{padding:12px 14px;border-radius:10px;background:var(--warn-bg);font-size:14px}
.err{color:var(--danger);font-size:14px;font-weight:550}
#toast{position:fixed;left:50%;bottom:28px;transform:translateX(-50%);background:var(--ink);color:var(--bg);padding:10px 16px;border-radius:10px;font-size:14px;max-width:min(520px,calc(100vw - 32px));z-index:20;box-shadow:0 10px 30px -10px var(--shade)}
.login{min-height:100vh;display:grid;place-items:center;padding:20px}
.login form{width:min(380px,100%);display:flex;flex-direction:column;gap:14px;padding:26px}

@media(max-width:860px){
.app{display:block}
.side{position:fixed;top:auto;bottom:0;left:0;right:0;height:auto;flex-direction:row;padding:5px 6px calc(5px + env(safe-area-inset-bottom));border-right:0;border-top:1px solid var(--line);z-index:10}
.side .brand,.side .foot{display:none}.only-m{display:flex}.promo{flex-direction:column}#p-overview .promo{display:none}
.trow{grid-template-columns:minmax(0,1fr) auto}.trow>*{grid-column:1}.trow>button{grid-column:2;grid-row:1/3}
.nav{flex-direction:row;flex:1}
.nav button{flex:1;flex-direction:column;gap:2px;padding:6px 2px;font-size:11.5px;text-align:center;min-width:0}
.top{display:flex;flex-direction:column;position:sticky;top:0;z-index:9;background:var(--panel);border-bottom:1px solid var(--line)}
.top>div{display:flex;align-items:center;justify-content:space-between;padding:9px 16px}
.top .risk{border:0;border-top:1px solid var(--line);border-radius:0;padding:6px 16px;align-items:center}
main{padding:18px 16px 110px}
h1{font-size:21px}
.hero-main{padding:18px 16px}.hero-main .btn.big{width:100%}
.clock{padding:16px}.figs{grid-template-columns:repeat(2,minmax(0,1fr))}
.figs>div{padding:14px 16px}.figs>div:nth-child(3){border-left:0}.figs>div:nth-child(n+3){border-top:1px solid var(--line)}
.figs b{font-size:19px}
.panel>header{padding:14px 16px 0}.panel>.body{padding:14px 16px 16px}
.grid2,.strats{grid-template-columns:minmax(0,1fr)}
thead{display:none}table,tbody{display:block}
tr{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px 16px;padding:14px 16px;border-bottom:1px solid var(--line)}
tr:last-child{border-bottom:0}
td,td:first-child,td:last-child{display:block;padding:0;border:0}td.r{text-align:left}
td::before{content:attr(data-l);display:block;font-size:12px;color:var(--mute);font-family:var(--sans)}
.tablewrap{padding:0}
.krow{grid-template-columns:minmax(0,1fr);padding:12px 16px}
.jrow{grid-template-columns:minmax(0,1fr);padding:10px 16px}
.savebar{bottom:74px}
#toast{bottom:84px}
.dl{grid-template-columns:minmax(0,1fr);gap:2px}.dl dd{margin-bottom:8px}
}
@media(prefers-reduced-motion:no-preference){.day i>b>s{transition:width .6s linear}}
</style></head><body>
<svg width="0" height="0" style="position:absolute" aria-hidden="true">
<symbol id="i-mark" viewBox="0 0 49 30"><g fill="currentColor"><rect y="18" width="33.4" height="11.4" rx="3"/><rect x="8.2" y="9" width="33.4" height="11.4" rx="3"/><rect x="15.6" width="33.4" height="11.4" rx="3"/></g><g fill="var(--bg)"><rect x="8.2" y="16.3" width="25.9" height="4.1" rx="1"/><rect x="15.6" y="7.3" width="25.9" height="4.1" rx="1"/></g></symbol>
<symbol id="i-overview" viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 18 0 9 9 0 0 0-18 0M12 7v5l3 2"/></symbol>
<symbol id="i-strategy" viewBox="0 0 24 24"><path d="M4 6h10M18 6h2M4 12h3M11 12h9M4 18h9M17 18h3"/><circle cx="16" cy="6" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="15" cy="18" r="2"/></symbol>
<symbol id="i-settings" viewBox="0 0 24 24"><circle cx="8" cy="15" r="4"/><path d="M11 12l9-9M17 6l2.5 2.5M14 9l2 2"/></symbol>
<symbol id="i-journal" viewBox="0 0 24 24"><path d="M5 4h14v16H5zM9 9h6M9 13h6M9 17h3"/></symbol>
<symbol id="i-warn" viewBox="0 0 24 24"><path d="M12 4l9 16H3zM12 10v4.5M12 17.5v.1"/></symbol>
<symbol id="i-info" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 11v5.5M12 7.6v.1"/></symbol>
<symbol id="i-check" viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5"/></symbol>
<symbol id="i-sun" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4"/></symbol>
<symbol id="i-moon" viewBox="0 0 24 24"><path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/></symbol>
<symbol id="i-inbox" viewBox="0 0 24 24"><path d="M3 13l3-8h12l3 8v6H3zM3 13h5l1 2.5h6l1-2.5h5"/></symbol>
<symbol id="i-x" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></symbol>
<symbol id="i-lock" viewBox="0 0 24 24"><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></symbol>
</svg>

<div id="login" class="login" hidden><form class="panel" id="login-form">
<div class="brand" style="padding:0"><svg class="i"><use href="#i-mark"/></svg><span class="bname"><b>Free Crypto <i>Trading Bot</i></b><small>by BYTtrade</small></span></div>
<div class="field"><label for="l-login" data-t="login">Login</label><input id="l-login" name="login" type="text" autocomplete="username" autocapitalize="none"></div>
<div class="field"><label for="l-pass" data-t="password">Password</label><input id="l-pass" name="password" type="password" autocomplete="current-password"></div>
<p class="err" id="l-err" role="alert" hidden></p>
<button class="btn primary" data-t="sign_in">Sign in</button>
<div class="row" style="justify-content:space-between"><span class="hint" data-t="risk_short"></span><button type="button" class="icon-btn" data-act="lang"></button></div>
</form></div>

<div id="app" class="app" hidden>
<aside class="side">
<div class="brand"><svg class="i"><use href="#i-mark"/></svg><span class="bname"><b>Free Crypto <i>Trading Bot</i></b><small>by BYTtrade</small></span></div>
<nav class="nav" id="nav"></nav>
<div class="foot">
<div class="byt"><b data-t="promo_title"></b><span data-t="promo_side"></span><a href="{{site:panel_sidebar}}" target="_blank" rel="noopener noreferrer" data-t="promo_link"></a></div>
<button class="risk" data-act="risk"><svg class="i"><use href="#i-warn"/></svg><span><span data-t="risk_short"></span> <u data-t="read_full"></u></span></button>
<div class="quick"><button class="icon-btn" data-act="lang"></button><button class="icon-btn" data-act="theme" data-ta="theme"></button></div>
</div></aside>
<div style="min-width:0">
<div class="top"><div><div class="brand" style="padding:0"><svg class="i"><use href="#i-mark"/></svg><span class="bname"><b>Free Crypto <i>Trading Bot</i></b><small>by BYTtrade</small></span></div>
<div class="quick"><button class="icon-btn" data-act="lang"></button><button class="icon-btn" data-act="theme" data-ta="theme"></button></div></div>
<button class="risk" data-act="risk"><svg class="i"><use href="#i-warn"/></svg><span><span data-t="risk_tiny"></span> <u data-t="read_full"></u></span></button></div>
<main>
<div class="note bad" id="offline" hidden style="margin-bottom:16px"><svg class="i"><use href="#i-warn"/></svg><div data-t="offline"></div></div>

<section class="page" id="p-overview">
<div class="note bad" id="run-error" hidden><svg class="i"><use href="#i-warn"/></svg><div><b data-t="start_failed"></b><div id="run-error-text"></div></div></div>
<div class="note" id="no-keys" hidden><svg class="i"><use href="#i-warn"/></svg><div><span id="no-keys-text"></span> <button class="link" data-go="settings" data-t="open_settings"></button></div></div>
<div class="panel">
<div class="hero-main"><div style="min-width:0"><div class="state"><span class="dot" id="st-dot"></span><h1 id="st-title"></h1></div>
<p class="hero-sub" id="st-sub"></p><p class="err" id="st-why" style="margin-top:6px" hidden></p></div>
<button class="btn big" id="go"></button></div>
<div class="clock"><div class="clock-head"><span class="mute" data-t="next_bar"></span><b class="num" id="cd">--:--:--</b><span class="mute small" id="cd-at"></span></div>
<div class="day" id="day"></div></div>
<div class="figs">
<div><span data-t="balance"></span><b class="num" id="f-bal"></b><small id="f-bal-n"></small></div>
<div><span data-t="result"></span><b class="num" id="f-pnl"></b><small data-t="estimated"></small></div>
<div><span data-t="result_r"></span><b class="num" id="f-r"></b><small data-t="r_note"></small></div>
<div><span data-t="trades"></span><b class="num" id="f-n"></b><small data-t="trades_note"></small></div>
</div></div>
<div class="panel"><header><h2 data-t="open_pos"></h2><span class="mute small" id="open-n"></span></header><div id="open"></div></div>
<div class="panel"><header><h2 data-t="last_trades"></h2><span class="mute small" data-t="last_trades_note"></span></header><div id="closed"></div></div>
<div class="promo" data-promo="results"><b>BYTtrade</b><span><span data-t="promo_results"></span> <a href="{{site:panel_trades}}" target="_blank" rel="noopener noreferrer" data-t="promo_open"></a></span><button class="x" data-act="hide" data-ta="hide"><svg class="i"><use href="#i-x"/></svg></button></div>
<div class="byt only-m"><b data-t="promo_title"></b><span data-t="promo_side"></span><a href="{{site:panel_overview}}" target="_blank" rel="noopener noreferrer" data-t="promo_link"></a></div>
</section>

<section class="page" id="p-strategy" hidden>
<div><h1 data-t="nav_strategy"></h1><p class="lead" data-t="strategy_lead"></p></div>
<div class="note info" id="running-note" hidden><svg class="i"><use href="#i-info"/></svg><div data-t="running_note"></div></div>
<div class="note" id="deviation" hidden><svg class="i"><use href="#i-warn"/></svg><div><b data-t="dev_title"></b><div id="dev-text"></div>
<button class="link" id="reset" data-t="reset_defaults" style="margin-top:4px"></button></div></div>
<div class="panel"><header><h2 data-t="account"></h2></header><div class="body grid2">
<div class="field"><label for="f-exchange" data-t="exchange"></label><select id="f-exchange"></select><span class="hint" data-t="exchange_hint"></span></div>
<div class="field"><span class="label" data-t="account_type"></span><div><div class="seg" id="f-account"></div></div><span class="hint" id="acc-keys"></span></div>
</div></div>
<div class="panel"><header><h2 data-t="strategies"></h2><span class="mute small" data-t="strategies_note"></span></header><div class="body"><div class="strats" id="f-strats"></div></div></div>
<div class="panel"><header><h2 data-t="coins"></h2><div class="row"><span class="mute small" id="coins-n"></span><button class="btn sm" id="coins-all" data-t="coins_all"></button></div></header>
<div class="body" style="display:flex;flex-direction:column;gap:14px"><div class="chips" id="f-coins"></div>
<div class="field"><label for="coin-add" data-t="coin_add"></label><div class="row"><input id="coin-add" type="text" class="num" maxlength="12" autocapitalize="characters" autocomplete="off" placeholder="PEPE"><button class="btn" id="coin-add-btn" data-t="add"></button></div>
<span class="hint" data-t="coin_add_hint"></span></div>
<div class="note" id="extra-note" hidden><svg class="i"><use href="#i-warn"/></svg><div id="extra-text"></div></div></div></div>
<div class="panel"><header><h2 data-t="size"></h2></header><div class="body grid2">
<div class="field" style="grid-column:1/-1"><span class="label" data-t="sizing"></span><div><div class="seg" id="f-sizing"></div></div></div>
<div class="field" id="w-risk"><label for="f-risk_pct" data-t="risk_pct"></label><input id="f-risk_pct" class="num" type="text" inputmode="decimal" autocomplete="off"><span class="hint" data-t="risk_pct_hint"></span></div>
<div class="field" id="w-notional"><label for="f-notional_usdt" data-t="notional"></label><input id="f-notional_usdt" class="num" type="text" inputmode="decimal" autocomplete="off"><span class="hint" data-t="notional_hint"></span></div>
<div class="field"><label for="f-leverage" data-t="leverage"></label><input id="f-leverage" class="num" type="text" inputmode="decimal" autocomplete="off"><span class="hint" data-t="leverage_hint"></span></div>
<div class="field"><span class="label" data-t="margin"></span><div><div class="seg" id="f-margin_mode"></div></div><span class="hint" data-t="margin_hint"></span></div>
</div></div>
<div class="panel"><header><h2 data-t="limits"></h2></header><div class="body grid2">
<div class="field"><label for="f-max_positions" data-t="max_pos"></label><input id="f-max_positions" class="num" type="text" inputmode="numeric" autocomplete="off"><span class="hint" data-t="max_pos_hint"></span></div>
<div class="field"><label for="f-max_total_notional_usdt" data-t="max_total"></label><input id="f-max_total_notional_usdt" class="num" type="text" inputmode="decimal" autocomplete="off" data-tp="no_cap"><span class="hint" data-t="max_total_hint"></span></div>
<p class="hint" style="grid-column:1/-1" data-t="stop_hint"></p>
</div></div>
<div class="promo" data-promo="strategy"><b>BYTtrade</b><span><span data-t="promo_strategy"></span> <a href="{{site:panel_strategy}}" target="_blank" rel="noopener noreferrer" data-t="promo_open"></a></span><button class="x" data-act="hide" data-ta="hide"><svg class="i"><use href="#i-x"/></svg></button></div>
<div class="savebar" id="savebar" hidden><div style="min-width:0"><b data-t="unsaved"></b><div class="err" id="save-err" hidden></div></div>
<div class="row"><button class="btn" id="discard" data-t="discard"></button><button class="btn primary" id="save" data-t="save_settings"></button></div></div>
</section>

<section class="page" id="p-settings" hidden>
<div><h1 data-t="nav_settings"></h1></div>
<div class="panel"><header><h2 data-t="keys_title"></h2></header><div class="body" style="padding-bottom:12px"><p class="hint" data-t="keys_lead"></p>
<div class="note" id="keys-off" hidden style="margin-top:12px"><svg class="i"><use href="#i-lock"/></svg><div><b data-t="keys_off_title"></b><div data-t="keys_off_text"></div>
<ul class="how"><li data-t="how_tunnel"></li><li data-t="how_proxy"></li><li data-t="how_cert"></li><li data-t="how_env"></li></ul></div></div>
<p class="hint" id="no-bytx" hidden data-t="no_bytx" style="margin-top:10px"></p></div><div id="keys"></div></div>
<div class="panel"><header><h2 data-t="bytx_title"></h2></header><div class="body" style="display:flex;flex-direction:column;gap:16px">
<dl class="dl"><dt data-t="own_version"></dt><dd id="b-own"></dd><dt data-t="bytx_current"></dt><dd class="num" id="b-cur"></dd><dt data-t="bytx_avail"></dt><dd id="b-av"></dd><dt data-t="bytx_checked"></dt><dd id="b-chk"></dd></dl>
<div class="field"><span class="label" data-t="bytx_mode"></span><div><div class="seg" id="b-mode"></div></div><span class="hint" id="b-mode-hint"></span></div>
<div class="row"><button class="btn" id="b-update" data-t="update_now"></button><span class="hint" id="b-update-hint"></span></div></div></div>
<div class="panel"><header><h2 data-t="ag_title"></h2></header><div class="body" style="display:flex;flex-direction:column;gap:14px">
<p class="hint" data-t="ag_lead"></p><div id="tokens"></div>
<form id="tok-form" class="row" autocomplete="off"><input id="tok-name" type="text" maxlength="40" data-tp="ag_name" style="max-width:260px" required><div class="seg" id="tok-scope"></div><button class="btn" data-t="ag_create"></button></form>
<p class="err" id="tok-err" hidden></p><p class="hint" id="tok-off" hidden data-t="ag_off"></p></div></div>
<div class="panel"><header><h2 data-t="access_title"></h2></header><div class="body" style="display:flex;flex-direction:column;gap:16px">
<p id="acc-state"></p>
<form id="pw-form" class="grid2" autocomplete="off">
<div class="field" id="pw-cur-w"><label for="pw-cur" data-t="pw_current"></label><input id="pw-cur" type="password" autocomplete="current-password"></div>
<div class="field"><label for="pw-login" data-t="login"></label><input id="pw-login" type="text" autocomplete="username" autocapitalize="none"></div>
<div class="field"><label for="pw-new" data-t="pw_new"></label><input id="pw-new" type="password" autocomplete="new-password"><span class="hint" data-t="pw_hint"></span></div>
<div class="row" style="grid-column:1/-1"><button class="btn" data-t="pw_save"></button><button type="button" class="btn" id="logout" data-t="sign_out"></button><span class="err" id="pw-err" hidden></span></div>
</form><p class="hint" id="pw-off" hidden data-t="pw_off"></p></div></div>
<div class="panel"><header><h2 data-t="look_title"></h2></header><div class="body grid2">
<div class="field"><span class="label" data-t="language"></span><div><div class="seg" id="s-lang"></div></div></div>
<div class="field"><span class="label" data-t="theme"></span><div><div class="seg" id="s-theme"></div></div></div></div></div>
<div class="promo" data-promo="community"><b>BYTtrade</b><span><span data-t="promo_community"></span> <a href="{{site:panel_settings}}" target="_blank" rel="noopener noreferrer" data-t="promo_open"></a></span><button class="x" data-act="hide" data-ta="hide"><svg class="i"><use href="#i-x"/></svg></button></div>
</section>

<section class="page" id="p-journal" hidden>
<div class="row" style="justify-content:space-between;align-items:flex-end"><div><h1 data-t="nav_journal"></h1><p class="lead" id="j-lead"></p></div><div class="seg" id="j-filter"></div></div>
<div class="panel" id="journal"></div>
</section>
</main></div></div>

<dialog id="d-start"><form method="dialog" id="start-form">
<h2 id="ds-title"></h2><div class="disc" id="ds-disc"></div>
<dl class="dl small" id="ds-sum"></dl>
<div class="note" id="ds-warn" hidden><svg class="i"><use href="#i-warn"/></svg><div id="ds-warn-text"></div></div>
<div class="field" id="ds-phrase-w" hidden><label for="ds-phrase" id="ds-phrase-l"></label><input id="ds-phrase" type="text" autocomplete="off" autocapitalize="characters" spellcheck="false"></div>
<p class="err" id="ds-err" role="alert" hidden></p>
<div class="actions"><button type="button" class="btn" data-close data-t="cancel"></button><button class="btn primary" id="ds-ok"></button></div>
</form></dialog>
<dialog id="d-risk"><form method="dialog"><h2 data-t="risk_title"></h2><div class="disc" id="dr-text"></div><p class="hint" data-t="risk_more"></p>
<div class="actions"><button class="btn primary" data-t="understood"></button></div></form></dialog>
<dialog id="d-keys"><form method="dialog" id="keys-form" autocomplete="off"><h2 id="dk-title"></h2><p class="hint" data-t="keys_dialog_hint"></p><div id="dk-fields" style="display:flex;flex-direction:column;gap:12px"></div>
<p class="err" id="dk-err" role="alert" hidden></p>
<div class="actions"><button type="button" class="btn" data-close data-t="cancel"></button><button class="btn primary" data-t="keys_save"></button></div></form></dialog>
<dialog id="d-ask"><form method="dialog"><h2 id="da-title"></h2><p id="da-text"></p>
<div class="actions"><button type="button" class="btn" data-close data-t="cancel"></button><button class="btn danger" id="da-ok" value="ok"></button></div></form></dialog>
<dialog id="d-token"><form method="dialog"><h2 id="dt-title"></h2><p class="hint" data-t="ag_new_text"></p>
<input id="dt-value" class="num" type="text" readonly spellcheck="false" style="max-width:none">
<div class="actions"><button type="button" class="btn" id="dt-copy" data-t="copy"></button><button class="btn primary" data-t="done"></button></div></form></dialog>
<div id="toast" role="status" hidden></div>

<script>
"use strict";
const T={en:{
nav_overview:"Overview",nav_strategy:"Strategy",nav_settings:"Settings",nav_journal:"Journal",
login:"Login",password:"Password",sign_in:"Sign in",sign_out:"Sign out",bad_login:"Wrong login or password.",locked:"Too many attempts. Try again in a few minutes.",
risk_short:"High risk. BYTtrade is not responsible for the results or losses.",risk_tiny:"High risk, no liability.",read_full:"Read in full",
risk_title:"Risk notice",risk_more:"The published results are a backtest on 20 coins with the default settings. They do not predict what your account will do.",understood:"Understood",
offline:"No connection to the panel. The executor may still be running; this page will reconnect on its own.",
s_running:"Running",s_stopped:"Stopped",s_starting:"Starting",s_blocked:"Running, new orders blocked",s_failed:"Stopped after an error",
acc_demo:"demo account",acc_real:"real account",start:"Start",stop:"Stop",start_failed:"The executor did not start",
no_keys:"No API keys for {0}, {1}. The executor cannot start without them.",open_settings:"Add keys in Settings",
next_bar:"Next 4h bar in",at_utc:"at {0} UTC, {1} your time",
balance:"Balance, USDT",bal_live:"read from the exchange",bal_start:"at the last start, {0}",bal_none:"shown after the first start",
result:"Result, USDT",estimated:"estimated from stop and take-profit levels",result_r:"Result, R",r_note:"1 R is the risk of one trade",trades:"Closed trades",trades_note:"by this executor on this account",
open_pos:"Open positions",last_trades:"Last trades",last_trades_note:"newest first, up to 20",
no_pos:"No open positions",no_pos_text:"Positions are opened when a 4h bar closes and one of the strategies gives a signal. Each one gets a stop on the exchange.",
no_trades:"No closed trades yet",no_trades_text:"A trade appears here after its stop or take-profit is hit.",
c_strategy:"Strategy",c_coin:"Coin",c_side:"Side",c_entry:"Entry",c_size:"Size",c_stop:"Stop",c_tp:"Take-profit",c_exit:"Exit",c_reason:"Closed by",c_pnl:"PnL, USDT",c_r:"R",c_time:"Closed",
long:"long",short:"short",pending:"entry is being confirmed",
r_stop:"stop",r_tp:"take-profit",r_closed:"closed on the exchange",r_manual:"by hand",r_no_stop:"no stop could be placed",
strategy_lead:"What the executor trades and how much it risks. Changes are saved separately and apply at the next start.",
running_note:"The executor is running with the settings it was started with. What you save here applies after you stop and start it again.",
dev_title:"These settings differ from the research",dev_text:"The published backtest results apply to the default settings only. Changed: {0}.",reset_defaults:"Return to the research defaults",
account:"Account",exchange:"Exchange",exchange_hint:"Signals come from public Binance candles; orders go to the exchange you choose.",account_type:"Account type",
keys_yes:"API keys for this account are set.",keys_no:"No API keys for this account yet.",
strategies:"Strategies",strategies_note:"one position per coin; the first strategy with a signal takes it",
st_st4h_ema50_1d:"Supertrend with the daily trend",sd_st4h_ema50_1d:"Supertrend(10, 3) flips on 4h in the direction of the daily trend: the 1d close is above or below EMA50.",
st_kc2_4h_adx1d:"Keltner breakout from a quiet market",sd_kc2_4h_adx1d:"The price breaks out of the Keltner(20, 20, 2) channel on 4h while daily ADX(14) is below 20.",
st_zmid1d_cloud4h:"Ichimoku cloud breakout",sd_zmid1d_cloud4h:"The price leaves the Ichimoku(9/26/52/26) cloud on 4h while the daily close is within 1 sigma of SMA(75).",
st_bbw1d_lr4h:"Regression channel after a squeeze",sd_bbw1d_lr4h:"A 4h close outside the linear regression channel(50, 2 sigma) while daily Bollinger(20, 2) width is below 0.20.",
exits:"Stop {0} ATR, take-profit {1} ATR, trailing {2} ATR",
coins:"Coins",coins_n:"{0} selected",coins_all:"Select the 20 research coins",coin_add:"Add another coin",add:"Add",coin_add_hint:"Ticker without USDT, for example PEPE. Coins the exchange does not list are skipped at start.",
extra_text:"Outside the 20 research coins ({0}) the strategies are not confirmed: on 639 other perpetuals the result was close to zero.",
size:"Position size",sizing:"How a position is sized",sz_risk:"Risk, % of balance",sz_notional:"Fixed amount, USDT",
risk_pct:"Risk per trade, %",risk_pct_hint:"Share of the balance lost if the stop is hit. Research value: 0.056.",
notional:"Position amount, USDT",notional_hint:"Notional of each position. The loss at the stop then depends on the stop distance.",
leverage:"Leverage",leverage_hint:"From 1 to 125; checked against the exchange limit at start. It changes the margin, not the position size.",
margin:"Margin mode",margin_hint:"Cross shares the whole balance between positions; isolated limits each one to its own margin.",
limits:"Limits",max_pos:"Open positions at once",max_pos_hint:"New entries are skipped when this many are open.",
max_total:"Total open amount, USDT",no_cap:"no cap",max_total_hint:"Cap on the sum of open notionals. Leave empty for no cap.",
stop_hint:"The stop-loss cannot be turned off: every position gets a stop on the exchange, or it is closed.",
unsaved:"Unsaved changes",discard:"Discard",save_settings:"Save settings",saved:"Settings saved",fill:"Check the value: {0}",pick_one:"Choose at least one strategy and one coin.",
keys_title:"Exchange API keys",keys_lead:"Keys are written to the .env file next to the executor (owner-only access). The panel never shows them again, only whether they are set. Create keys with trading permission and without withdrawal permission.",
keys_off_title:"Keys cannot be entered over this connection",keys_off_text:"You opened the panel over plain HTTP from another machine, so anything typed here could be read on the way. Use one of these:",
how_tunnel:"SSH tunnel: ssh -L 8790:127.0.0.1:8790 server, then open http://127.0.0.1:8790",how_proxy:"an HTTPS reverse proxy on the server, with the panel started with --behind-https-proxy",how_cert:"TLS in the panel itself: --certfile and --keyfile",how_env:"or write the keys into .env on the server by hand (names are in .env.example)",
no_bytx:"The bytx SDK is not installed, so the list of exchanges is empty. Install it: pip install -e '.[live]'",
k_set:"Set",k_unset:"Not set",k_locked:"in use by the running executor",replace:"Replace",remove:"Remove",
keys_for:"{0} keys, {1} account",keys_dialog_hint:"The values are sent once to this panel's server and stored in .env. They are not shown after saving.",keys_save:"Save keys",keys_saved:"Keys saved",keys_removed:"Keys removed",
f_api_key:"API key",f_secret:"Secret",f_password:"Passphrase",f_wallet_address:"Wallet address",f_private_key:"Private key",
rm_title:"Remove the {0} {1} keys?",rm_text:"Their lines are deleted from .env. The keys stay valid on the exchange until you delete them there.",
bytx_title:"Updates",own_version:"This bot",own_newer:"version {0} is out: pip install -U free-crypto-trading-bot, or pull the new Docker image",bytx_current:"bytx SDK, installed",bytx_avail:"Available",bytx_checked:"Last check",none_newer:"nothing newer",never:"not yet",not_installed:"not installed",
incompatible:"version {0} needs a newer release of this bot",bytx_mode:"When a new version appears",m_on:"Install",m_notify:"Only tell me",m_off:"Do not check",
mh_on:"Installed in a quiet window between bars, verified, and rolled back if the check fails.",mh_notify:"The journal reports the new version; you install it yourself.",mh_off:"The package index is not asked at all.",
update_now:"Update now",update_idle:"Works while the executor is running.",update_asked:"Update requested. It waits for a safe window between bars.",
access_title:"Access to the panel",acc_none:"No password is set: the panel answers only on this computer (127.0.0.1).",acc_user:"Signed in as {0}.",
pw_current:"Current password",pw_new:"New password",pw_hint:"At least 8 characters. Only its salted hash is stored.",pw_save:"Save login and password",pw_saved:"Login and password saved",pw_off:"The login and password can be changed over a local or HTTPS connection, or with python -m trend4h panel-user on the server.",
look_title:"Language and theme",language:"Language",theme:"Theme",th_system:"System",th_light:"Light",th_dark:"Dark",
j_lead:"Events of the executor on {0}, {1}. Newest first.",j_all:"All",j_bad:"Problems",j_empty:"The journal is empty",j_empty_text:"Events appear here once the executor has been started on this account.",j_none_bad:"No problems in the last events",
start_title:"Start the executor",real_title:"Real account: real money is at risk",sum_exchange:"Exchange",sum_strats:"Strategies",sum_coins:"Coins",sum_size:"Size",sum_lev:"Leverage",
n_of:"{0} of {1}",size_risk:"{0}% of balance per trade",size_notional:"{0} USDT per position",
dirty_warn:"Unsaved changes on the Strategy page are not included. The executor starts with the saved settings.",
phrase_label:"Type {0} to confirm",start_demo:"Start on demo",start_real:"Start on the real account",stopping:"Stopping. Positions and their stops stay on the exchange.",started:"Executor started",
promo_title:"Free from BYTtrade",promo_side:"This bot is free. The BYTtrade site has more strategies, grid bots and indicators, also free.",promo_link:"More on byttrade.com",promo_open:"Open byttrade.com",hide:"Hide",
promo_strategy:"More ready-made strategies and indicators, grid bots and setup without writing code are on BYTtrade, free.",
promo_results:"Backtests on a longer history and more analysis tools are on BYTtrade, free.",
promo_community:"BYTtrade has a large community where strategies and settings are discussed. The way in is on the site.",
ag_title:"Agents",ag_lead:"Give your own AI agent access with a token: over the HTTP API (docs/agent-api.md) or the MCP server (python -m trend4h mcp). No token reaches exchange keys, the panel password, other tokens or bytx updates, and none can start a real account.",
ag_name:"Token name, for example night bot",ag_read:"Read only",ag_control:"Read and control demo",ag_create:"Create token",ag_none:"No tokens yet.",
ag_r_read:"read",ag_r_control:"read, control demo",ag_dates:"created {0}, last used {1}",ag_never:"never",ag_revoke:"Revoke",ag_revoked:"Token revoked",
ag_rm_title:"Revoke the token {0}?",ag_rm_text:"The agent that uses it loses access at once.",
ag_new_title:"Token {0}",ag_new_text:"Copy it now. It is shown once; the panel keeps only its hash. Over plain HTTP from another machine a token travels in clear text: use HTTPS or an SSH tunnel.",
copy:"Copy",copied:"Copied",done:"Done",ag_off:"Tokens are created and revoked over a local or HTTPS connection.",
cancel:"Cancel",e_no_keys:"There are no API keys for this account. Add them in Settings.",e_keys_in_use:"The executor is running on this account. Stop it before changing its keys.",e_insecure:"This connection is not safe for secrets. Use a local or HTTPS connection.",e_bad_password:"The current password is wrong.",e_phrase:"Type the phrase exactly as shown.",e_net:"The panel did not answer.",
},ru:{
nav_overview:"Обзор",nav_strategy:"Стратегия",nav_settings:"Настройки",nav_journal:"Журнал",
login:"Логин",password:"Пароль",sign_in:"Войти",sign_out:"Выйти",bad_login:"Неверный логин или пароль.",locked:"Слишком много попыток. Повторите через несколько минут.",
risk_short:"Высокий риск. BYTtrade не отвечает за результат и убытки.",disclaimer:"ВЫСОКИЙ РИСК. Торговля бессрочными фьючерсами может привести к потере всего депозита. BYTtrade не несёт ответственности за результаты и убытки. Результаты бэктеста и бумажной торговли не гарантируют будущую доходность. Это не инвестиционная рекомендация. Вы действуете на свой риск.",risk_tiny:"Высокий риск, без гарантий.",read_full:"Читать полностью",
risk_title:"Предупреждение о риске",risk_more:"Опубликованные результаты — бэктест на 20 монетах с настройками по умолчанию. Они не предсказывают, что будет на вашем счёте.",understood:"Понятно",
offline:"Нет связи с панелью. Исполнитель может продолжать работу; страница подключится сама.",
s_running:"Работает",s_stopped:"Остановлен",s_starting:"Запускается",s_blocked:"Работает, новые ордера заблокированы",s_failed:"Остановлен из-за ошибки",
acc_demo:"демо-счёт",acc_real:"реальный счёт",start:"Старт",stop:"Стоп",start_failed:"Исполнитель не запустился",
no_keys:"Нет ключей API для {0}, {1}. Без них исполнитель не запустится.",open_settings:"Добавить ключи в настройках",
next_bar:"До следующего бара 4h",at_utc:"в {0} UTC, {1} по вашему времени",
balance:"Баланс, USDT",bal_live:"получен с биржи",bal_start:"на последнем запуске, {0}",bal_none:"появится после первого запуска",
result:"Итог, USDT",estimated:"оценка по уровням стопа и тейка",result_r:"Итог, R",r_note:"1 R — риск одной сделки",trades:"Закрытых сделок",trades_note:"этого исполнителя на этом счёте",
open_pos:"Открытые позиции",last_trades:"Последние сделки",last_trades_note:"сначала новые, до 20",
no_pos:"Открытых позиций нет",no_pos_text:"Позиции открываются на закрытии бара 4h, когда одна из стратегий даёт сигнал. Каждая получает стоп на бирже.",
no_trades:"Закрытых сделок пока нет",no_trades_text:"Сделка появится здесь, когда сработает её стоп или тейк.",
c_strategy:"Стратегия",c_coin:"Монета",c_side:"Сторона",c_entry:"Вход",c_size:"Размер",c_stop:"Стоп",c_tp:"Тейк",c_exit:"Выход",c_reason:"Закрыта",c_pnl:"PnL, USDT",c_r:"R",c_time:"Время",
long:"лонг",short:"шорт",pending:"вход подтверждается",
r_stop:"стоп",r_tp:"тейк",r_closed:"закрыта на бирже",r_manual:"вручную",r_no_stop:"не удалось поставить стоп",
strategy_lead:"Что торгует исполнитель и сколько рискует. Изменения сохраняются отдельно и действуют со следующего запуска.",
running_note:"Исполнитель работает с настройками, с которыми был запущен. Сохранённое здесь подействует после остановки и нового запуска.",
dev_title:"Настройки отличаются от исследовательских",dev_text:"Опубликованные результаты бэктеста относятся только к настройкам по умолчанию. Изменено: {0}.",reset_defaults:"Вернуть настройки исследования",
account:"Счёт",exchange:"Биржа",exchange_hint:"Сигналы считаются по публичным свечам Binance; ордера уходят на выбранную биржу.",account_type:"Тип счёта",
keys_yes:"Ключи API для этого счёта заданы.",keys_no:"Ключей API для этого счёта пока нет.",
strategies:"Стратегии",strategies_note:"одна позиция на монету; её берёт первая стратегия с сигналом",
st_st4h_ema50_1d:"Supertrend по дневному тренду",sd_st4h_ema50_1d:"Supertrend(10, 3) разворачивается на 4h в сторону дневного тренда: дневное закрытие выше или ниже EMA50.",
st_kc2_4h_adx1d:"Пробой Кельтнера из тихого рынка",sd_kc2_4h_adx1d:"Цена выходит из канала Кельтнера(20, 20, 2) на 4h, пока дневной ADX(14) ниже 20.",
st_zmid1d_cloud4h:"Пробой облака Ишимоку",sd_zmid1d_cloud4h:"Цена выходит из облака Ишимоку(9/26/52/26) на 4h, пока дневное закрытие в пределах 1 сигмы от SMA(75).",
st_bbw1d_lr4h:"Канал регрессии после сжатия",sd_bbw1d_lr4h:"Закрытие 4h вне канала линейной регрессии(50, 2 сигмы), пока ширина дневных полос Боллинджера(20, 2) ниже 0,20.",
exits:"Стоп {0} ATR, тейк {1} ATR, трейлинг {2} ATR",
coins:"Монеты",coins_n:"выбрано {0}",coins_all:"Выбрать 20 монет исследования",coin_add:"Добавить другую монету",add:"Добавить",coin_add_hint:"Тикер без USDT, например PEPE. Монеты, которых нет на бирже, пропускаются при запуске.",
extra_text:"Вне 20 монет исследования ({0}) стратегии не подтверждены: на 639 других бессрочных контрактах результат был около нуля.",
size:"Размер позиции",sizing:"Как считается размер",sz_risk:"Риск, % баланса",sz_notional:"Фиксированная сумма, USDT",
risk_pct:"Риск на сделку, %",risk_pct_hint:"Доля баланса, которая теряется при срабатывании стопа. В исследовании: 0,056.",
notional:"Сумма позиции, USDT",notional_hint:"Номинал каждой позиции. Убыток на стопе тогда зависит от расстояния до стопа.",
leverage:"Плечо",leverage_hint:"От 1 до 125; при запуске сверяется с лимитом биржи. Меняет маржу, а не размер позиции.",
margin:"Режим маржи",margin_hint:"Кросс делит весь баланс между позициями; изолированная ограничивает каждую её собственной маржой.",
limits:"Лимиты",max_pos:"Позиций одновременно",max_pos_hint:"Когда открыто столько, новые входы пропускаются.",
max_total:"Сумма открытых позиций, USDT",no_cap:"без предела",max_total_hint:"Предел суммы открытых номиналов. Пусто — без предела.",
stop_hint:"Стоп-лосс отключить нельзя: каждая позиция получает стоп на бирже, иначе она закрывается.",
unsaved:"Есть несохранённые изменения",discard:"Отменить",save_settings:"Сохранить настройки",saved:"Настройки сохранены",fill:"Проверьте значение: {0}",pick_one:"Выберите хотя бы одну стратегию и одну монету.",
keys_title:"Ключи API бирж",keys_lead:"Ключи записываются в файл .env рядом с исполнителем (доступ только владельцу). Панель больше их не показывает — только заданы они или нет. Создавайте ключи с правом торговли и без права вывода.",
keys_off_title:"По этому соединению вводить ключи нельзя",keys_off_text:"Панель открыта по обычному HTTP с другого компьютера: всё, что вы здесь наберёте, можно прочитать по пути. Сделайте одно из этого:",
how_tunnel:"SSH-туннель: ssh -L 8790:127.0.0.1:8790 server, затем откройте http://127.0.0.1:8790",how_proxy:"обратный прокси с HTTPS на сервере, панель запущена с --behind-https-proxy",how_cert:"TLS в самой панели: --certfile и --keyfile",how_env:"или впишите ключи в .env на сервере вручную (имена — в .env.example)",
no_bytx:"SDK bytx не установлен, поэтому список бирж пуст. Установите: pip install -e '.[live]'",
k_set:"Заданы",k_unset:"Не заданы",k_locked:"используются работающим исполнителем",replace:"Заменить",remove:"Удалить",
keys_for:"Ключи {0}, счёт {1}",keys_dialog_hint:"Значения один раз отправляются на сервер панели и сохраняются в .env. После сохранения они не показываются.",keys_save:"Сохранить ключи",keys_saved:"Ключи сохранены",keys_removed:"Ключи удалены",
f_api_key:"API key",f_secret:"Secret",f_password:"Passphrase",f_wallet_address:"Адрес кошелька",f_private_key:"Приватный ключ",
rm_title:"Удалить ключи {0} {1}?",rm_text:"Их строки удаляются из .env. На бирже ключи действуют, пока вы не удалите их там.",
bytx_title:"Обновления",own_version:"Этот бот",own_newer:"вышла версия {0}: pip install -U free-crypto-trading-bot или новый образ Docker",bytx_current:"SDK bytx, установлен",bytx_avail:"Доступна",bytx_checked:"Последняя проверка",none_newer:"новее нет",never:"ещё не было",not_installed:"не установлен",
incompatible:"версии {0} нужен более новый выпуск бота",bytx_mode:"Когда выходит новая версия",m_on:"Устанавливать",m_notify:"Только сообщать",m_off:"Не проверять",
mh_on:"Ставится в спокойное окно между барами, проверяется и откатывается, если проверка не прошла.",mh_notify:"Журнал сообщит о новой версии; ставите её вы сами.",mh_off:"Индекс пакетов не опрашивается.",
update_now:"Обновить сейчас",update_idle:"Доступно, пока исполнитель работает.",update_asked:"Обновление запрошено. Оно дождётся безопасного окна между барами.",
access_title:"Доступ к панели",acc_none:"Пароль не задан: панель отвечает только на этом компьютере (127.0.0.1).",acc_user:"Вы вошли как {0}.",
pw_current:"Текущий пароль",pw_new:"Новый пароль",pw_hint:"Не короче 8 символов. Хранится только хэш с солью.",pw_save:"Сохранить логин и пароль",pw_saved:"Логин и пароль сохранены",pw_off:"Логин и пароль меняются по локальному или HTTPS-соединению либо командой python -m trend4h panel-user на сервере.",
look_title:"Язык и тема",language:"Язык",theme:"Тема",th_system:"Системная",th_light:"Светлая",th_dark:"Тёмная",
j_lead:"События исполнителя на {0}, {1}. Сначала новые.",j_all:"Все",j_bad:"Проблемы",j_empty:"Журнал пуст",j_empty_text:"События появятся, когда исполнитель будет запущен на этом счёте.",j_none_bad:"В последних событиях проблем нет",
start_title:"Запуск исполнителя",real_title:"Реальный счёт: под риском настоящие деньги",sum_exchange:"Биржа",sum_strats:"Стратегии",sum_coins:"Монеты",sum_size:"Размер",sum_lev:"Плечо",
n_of:"{0} из {1}",size_risk:"{0}% баланса на сделку",size_notional:"{0} USDT на позицию",
dirty_warn:"Несохранённые изменения на странице «Стратегия» не учитываются. Исполнитель стартует с сохранёнными настройками.",
phrase_label:"Введите {0} для подтверждения",start_demo:"Запустить на демо",start_real:"Запустить на реальном счёте",stopping:"Останавливается. Позиции и их стопы остаются на бирже.",started:"Исполнитель запущен",
promo_title:"Бесплатно от BYTtrade",promo_side:"Этот бот бесплатен. На сайте BYTtrade больше стратегий, сеточные боты и индикаторы — тоже бесплатно.",promo_link:"Больше на byttrade.com",promo_open:"Открыть byttrade.com",hide:"Скрыть",
promo_strategy:"Больше готовых стратегий и индикаторов, сеточные боты и настройка без кода — на BYTtrade, бесплатно.",
promo_results:"Бэктест на более длинной истории и больше инструментов анализа — на BYTtrade, бесплатно.",
promo_community:"У BYTtrade большое сообщество, где обсуждают стратегии и настройки. Вход — на сайте.",
ag_title:"Агенты",ag_lead:"Дайте своему ИИ-агенту доступ по токену: через HTTP API (docs/agent-api.md) или MCP-сервер (python -m trend4h mcp). Ни один токен не получает ключи бирж, пароль панели, другие токены и обновление bytx и не может запустить реальный счёт.",
ag_name:"Имя токена, например ночной бот",ag_read:"Только чтение",ag_control:"Чтение и управление demo",ag_create:"Создать токен",ag_none:"Токенов пока нет.",
ag_r_read:"чтение",ag_r_control:"чтение, управление demo",ag_dates:"создан {0}, использован {1}",ag_never:"ни разу",ag_revoke:"Отозвать",ag_revoked:"Токен отозван",
ag_rm_title:"Отозвать токен {0}?",ag_rm_text:"Агент, который им пользуется, сразу потеряет доступ.",
ag_new_title:"Токен {0}",ag_new_text:"Скопируйте его сейчас. Он показывается один раз; панель хранит только хэш. По обычному HTTP с другого компьютера токен идёт открытым текстом: нужен HTTPS или SSH-туннель.",
copy:"Скопировать",copied:"Скопировано",done:"Готово",ag_off:"Токены создаются и отзываются по локальному или HTTPS-соединению.",
cancel:"Отмена",e_no_keys:"Для этого счёта нет ключей API. Добавьте их в настройках.",e_keys_in_use:"Исполнитель работает на этом счёте. Остановите его, прежде чем менять ключи.",e_insecure:"Это соединение небезопасно для секретов. Нужно локальное или HTTPS-соединение.",e_bad_password:"Текущий пароль неверен.",e_phrase:"Введите фразу в точности как показано.",e_net:"Панель не ответила.",
}};
const EV={en:{start:"Started",stopped:"Stopped",bar:"Bar processed",bar_incomplete:"Bar data incomplete",entry:"Position opened",exit:"Position closed",entry_skipped:"Entry skipped",entry_rejected:"Entry rejected",entry_unknown:"Entry outcome unknown",entry_found:"Entry confirmed",entry_lost:"Entry not found",entry_not_filled:"Entry not filled",trail:"Stop moved",trail_failed:"Stop move failed",stop_placed:"Stop placed",stop_not_placed:"Stop not placed",close_failed:"Close failed",cancel_failed:"Cancel failed",orders_blocked:"New orders blocked",orders_unblocked:"Orders unblocked",checks:"Integrity checks",coin_skipped:"Coin skipped",cycle_error:"Cycle error",dry_run_order:"Dry-run order",update_available:"bytx update available",update_start:"bytx update started",update_done:"bytx updated",update_failed:"bytx update failed",update_rolled_back:"bytx rolled back",update_rollback_failed:"bytx rollback failed",update_postponed:"bytx update postponed",update_check_failed:"bytx check failed",update_incompatible:"Incompatible bytx version",update_own_available:"A newer release of this bot is out",agent_config:"Agent saved settings",agent_start:"Agent started the executor",agent_stop:"Agent stopped the executor",agent_close:"Agent closed positions",agent_refused:"Agent request refused"},
ru:{start:"Запуск",stopped:"Остановка",bar:"Бар обработан",bar_incomplete:"Данные бара неполные",entry:"Позиция открыта",exit:"Позиция закрыта",entry_skipped:"Вход пропущен",entry_rejected:"Вход отклонён",entry_unknown:"Исход входа неизвестен",entry_found:"Вход подтверждён",entry_lost:"Вход не найден",entry_not_filled:"Вход не исполнен",trail:"Стоп передвинут",trail_failed:"Стоп не передвинут",stop_placed:"Стоп поставлен",stop_not_placed:"Стоп не поставлен",close_failed:"Закрытие не удалось",cancel_failed:"Отмена не удалась",orders_blocked:"Новые ордера заблокированы",orders_unblocked:"Ордера разблокированы",checks:"Проверки целостности",coin_skipped:"Монета пропущена",cycle_error:"Ошибка цикла",dry_run_order:"Пробный ордер",update_available:"Доступно обновление bytx",update_start:"Обновление bytx начато",update_done:"bytx обновлён",update_failed:"Обновление bytx не удалось",update_rolled_back:"bytx возвращён к прежней версии",update_rollback_failed:"Откат bytx не удался",update_postponed:"Обновление bytx отложено",update_check_failed:"Проверка bytx не удалась",update_incompatible:"Несовместимая версия bytx",update_own_available:"Вышла новая версия бота",agent_config:"Агент сохранил настройки",agent_start:"Агент запустил исполнитель",agent_stop:"Агент остановил исполнитель",agent_close:"Агент закрыл позиции",agent_refused:"Запрос агента отклонён"}};
const EXITS={st4h_ema50_1d:[2,10,5],kc2_4h_adx1d:[2,10,5],zmid1d_cloud4h:[3,10,5],bbw1d_lr4h:[3,14,4]};
const NAMES={binance:"Binance",bybit:"Bybit",okx:"OKX",bingx:"BingX",kucoin:"KuCoin",hyperliquid:"Hyperliquid",bitget:"Bitget",gate:"Gate",mexc:"MEXC",htx:"HTX",blofin:"BloFin"};
const STEP=14400000,BAD=/fail|reject|orders_blocked|lost|unknown|not_placed|cycle_error|incompatible|refused/,WARN=/skipped|postponed|incomplete|not_filled|rolled_back/;
const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
const store={get(k){try{return localStorage.getItem("t4h."+k)}catch(e){return null}},set(k,v){try{localStorage.setItem("t4h."+k,v)}catch(e){}}};
let S=null,csrf="",form=null,page="overview",skew=0,jfilter="all",jevents=[],keysFor=null,askResolve=null,tokControl=false;
let lang=store.get("lang")==="ru"?"ru":"en";  /* English unless the person chose Russian with the button */
let themePref=store.get("theme")||"dark";

function t(k,...a){let s=(T[lang]&&T[lang][k])||T.en[k]||k;a.forEach((v,i)=>{s=s.split("{"+i+"}").join(v)});return s}
function h(tag,attrs,...kids){const e=document.createElement(tag);for(const k in attrs||{}){const v=attrs[k];if(v==null||v===false)continue;
  if(k==="class")e.className=v;else if(k.startsWith("on"))e.addEventListener(k.slice(2),v);else e.setAttribute(k,v===true?"":v)}
  kids.flat().forEach(c=>{if(c!=null&&c!==false)e.append(c)});return e}
function icon(n){const s=document.createElementNS("http://www.w3.org/2000/svg","svg");s.setAttribute("class","i");s.setAttribute("aria-hidden","true");
  const u=document.createElementNS("http://www.w3.org/2000/svg","use");u.setAttribute("href","#i-"+n);s.append(u);return s}
const exName=e=>NAMES[e]||e;
const disc=()=>T[lang].disclaimer||(S&&S.disclaimer)||t("risk_short");
const stName=s=>T[lang]["st_"+s]||s;
function num(v,max=6){if(v==null||v===""||isNaN(v))return "—";const a=Math.abs(v);
  return Number(v).toLocaleString(lang==="ru"?"ru-RU":"en-US",a>=1000?{maximumFractionDigits:2}:{maximumSignificantDigits:max})}
function money(v,sign){if(v==null||isNaN(v))return "—";const s=Number(v).toLocaleString(lang==="ru"?"ru-RU":"en-US",{minimumFractionDigits:2,maximumFractionDigits:2});return sign&&v>0?"+"+s:s}
function when(ms,sec){return new Date(ms).toLocaleString(lang==="ru"?"ru-RU":"en-GB",{day:"numeric",month:"short",hour:"2-digit",minute:"2-digit",...(sec?{second:"2-digit"}:{})})}
function toast(text){const e=$("#toast");e.textContent=text;e.hidden=false;clearTimeout(toast.t);toast.t=setTimeout(()=>{e.hidden=true},4200)}
function show(el,text){el.textContent=text||"";el.hidden=!text}
function errText(d){return (d&&d.code&&T[lang]["e_"+d.code])||(d&&d.error)||t("e_net")}
async function api(path,body){try{const r=await fetch(path,body===undefined?{}:{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":csrf},body:JSON.stringify(body)});
  return [r.status,await r.json().catch(()=>({}))]}catch(e){return [0,{}]}}
function seg(el,opts,value,onpick){el.textContent="";opts.forEach(([v,label,cls])=>el.append(h("button",{type:"button",class:cls,"aria-pressed":String(v===value),onclick:()=>onpick(v)},label)))}
function empty(title,text){return h("div",{class:"empty"},icon("inbox"),h("b",null,title),text&&h("p",null,text))}

/* ---------- language, theme, navigation */
function applyTheme(){document.documentElement.dataset.theme=themePref==="system"?(matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light"):themePref;
  const dark=document.documentElement.dataset.theme==="dark";$$("[data-act=theme]").forEach(b=>{b.textContent="";b.append(icon(dark?"sun":"moon"))})}
function setTheme(v){themePref=v;store.set("theme",v);applyTheme();drawLook()}
function setLang(v){lang=v;store.set("lang",v);applyLang()}
function applyLang(){document.documentElement.lang=lang;
  $$("[data-t]").forEach(e=>{e.textContent=t(e.dataset.t)});$$("[data-tp]").forEach(e=>{e.placeholder=t(e.dataset.tp)});$$("[data-ta]").forEach(e=>{e.setAttribute("aria-label",t(e.dataset.ta))});
  $$("[data-act=lang]").forEach(b=>{b.textContent=lang==="ru"?"EN":"RU";b.setAttribute("aria-label",lang==="ru"?"Switch to English":"Переключить на русский")});
  drawNav();drawLook();if(S){drawForm();render()}}
function drawNav(){const n=$("#nav");n.textContent="";["overview","strategy","settings","journal"].forEach(p=>n.append(
  h("button",{"aria-current":p===page?"page":null,onclick:()=>go(p)},icon(p),h("span",null,t("nav_"+p)))))}
function go(p){page=p;try{history.replaceState(null,"","#"+p)}catch(e){}$$("section.page").forEach(s=>{s.hidden=s.id!=="p-"+p});drawNav();scrollTo(0,0);if(p==="journal")loadJournal()}
function drawLook(){seg($("#s-lang"),[["en","English"],["ru","Русский"]],lang,setLang);
  seg($("#s-theme"),[["system",t("th_system")],["light",t("th_light")],["dark",t("th_dark")]],themePref,setTheme)}
document.addEventListener("click",e=>{const b=e.target.closest("[data-act],[data-go],[data-close]");if(!b)return;
  if(b.dataset.go)go(b.dataset.go);else if("close" in b.dataset)b.closest("dialog").close();
  else if(b.dataset.act==="lang")setLang(lang==="ru"?"en":"ru");
  else if(b.dataset.act==="theme")setTheme(document.documentElement.dataset.theme==="dark"?"light":"dark");
  else if(b.dataset.act==="hide"){const box=b.closest("[data-promo]");store.set("promo."+box.dataset.promo,"1");box.hidden=true}
  else if(b.dataset.act==="risk"){$("#dr-text").textContent=disc();$("#d-risk").showModal()}});
matchMedia("(prefers-color-scheme: dark)").addEventListener("change",applyTheme);

/* ---------- status */
async function refresh(){const [s,d]=await api("/api/status");
  if(s===401){if(S)return location.reload();  /* the panel was restarted: without a password a reload gets a new session */
    $("#login").hidden=false;$("#app").hidden=true;return}
  $("#offline").hidden=s===200;if(s!==200)return;
  $("#login").hidden=true;$("#app").hidden=false;S=d;csrf=d.csrf;skew=d.now-Date.now();
  if(!form)loadForm(d.config);render();if(page==="journal")loadJournal()}
function shown(){return S.active||S.config}
function render(){renderOverview();formState();renderKeys();renderBytx();renderTokens();renderAccess();tick()}

function renderOverview(){const c=shown(),m=S.summary||{open:[],closed:[],trades:0,sum_R:0,pnl_usdt:0},x=S.running;
  const st=x?(S.summary&&S.summary.blocked?"blocked":S.ready?"running":"starting"):S.error?"failed":"stopped";
  $("#st-title").textContent=t("s_"+st);$("#st-dot").className="dot "+({running:"run",blocked:"bad",starting:"wait",failed:"bad"}[st]||"");
  const sub=$("#st-sub");sub.textContent="";sub.append(exName(c.exchange)+" ",h("span",{class:"tag "+c.account},t("acc_"+c.account)));
  show($("#st-why"),st==="blocked"?S.summary.blocked:"");
  $("#run-error").hidden=!S.error||x;$("#run-error-text").textContent=S.error||"";
  $("#no-keys").hidden=S.keys_found||x;$("#no-keys-text").textContent=t("no_keys",exName(c.exchange),t("acc_"+c.account));
  const go=$("#go");go.textContent=t(x?"stop":"start");go.className="btn big "+(x?"stop":"primary");
  const b=S.balance;$("#f-bal").textContent=b?money(b.value):"—";$("#f-bal-n").textContent=b?(b.live?t("bal_live"):t("bal_start",when(b.at*1000))):t("bal_none");
  const p=$("#f-pnl");p.textContent=money(m.pnl_usdt,true);p.className="num "+(m.pnl_usdt>0?"up":m.pnl_usdt<0?"down":"");
  const r=$("#f-r");r.textContent=(m.sum_R>0?"+":"")+num(m.sum_R)+" R";r.className="num "+(m.sum_R>0?"up":m.sum_R<0?"down":"");
  $("#f-n").textContent=num(m.trades);$("#open-n").textContent=m.open.length?String(m.open.length):"";
  const side=v=>{const l=v===1||v==="long";return h("span",{class:l?"side-long":"side-short"},t(l?"long":"short"))};
  table($("#open"),m.open,[["c_coin",r=>h("b",null,r.coin)],["c_side",r=>side(r.side)],["c_strategy",r=>stName(r.strategy)],
    ["c_entry",r=>r.status==="open"?num(r.entry,7):h("span",{class:"mute"},t("pending")),1],["c_size",r=>num(r.size),1],["c_stop",r=>num(r.stop,7),1],["c_tp",r=>num(r.tp,7),1]],
    empty(t("no_pos"),t("no_pos_text")));
  table($("#closed"),m.closed.slice().reverse(),[["c_time",r=>r.exit_ts?when(r.exit_ts):"—"],["c_coin",r=>h("b",null,r.coin)],["c_side",r=>side(r.side)],["c_strategy",r=>stName(r.strategy)],
    ["c_entry",r=>num(r.entry,7),1],["c_exit",r=>num(r.exit,7),1],["c_reason",r=>T[lang]["r_"+r.reason]||r.reason||"—"],
    ["c_pnl",r=>h("span",{class:r.pnl>0?"up":r.pnl<0?"down":""},money(r.pnl,true)),1],["c_r",r=>h("span",{class:r.R>0?"up":r.R<0?"down":""},r.R==null?"—":(r.R>0?"+":"")+num(Math.round(r.R*100)/100)),1]],
    empty(t("no_trades"),t("no_trades_text")))}
function table(box,rows,cols,blank){box.textContent="";if(!rows.length){box.append(blank);return}
  const tb=h("tbody");rows.forEach(r=>tb.append(h("tr",null,cols.map(([k,f,n])=>h("td",{"data-l":t(k),class:n?"num r":null},f(r))))));
  box.append(h("div",{class:"tablewrap"},h("table",null,h("thead",null,h("tr",null,cols.map(([k,,n])=>h("th",{class:n?"r":null},t(k))))),tb)))}
function tick(){if(!S)return;const now=Date.now()+skew,next=(Math.floor(now/STEP)+1)*STEP,left=Math.max(0,Math.floor((next-now)/1000)),p2=n=>String(n).padStart(2,"0");
  $("#cd").textContent=p2(Math.floor(left/3600))+":"+p2(Math.floor(left%3600/60))+":"+p2(left%60);
  const d=new Date(next);$("#cd-at").textContent=t("at_utc",p2(d.getUTCHours())+":00",p2(d.getHours())+":"+p2(d.getMinutes()));
  const cur=new Date(now).getUTCHours()/4|0,day=$("#day");
  if(day.children.length!==6){day.textContent="";for(let i=0;i<6;i++)day.append(h("i",null,h("b",null,h("s")),h("span",{class:"num"},p2(i*4)+":00")))}
  [...day.children].forEach((e,i)=>{e.className=i<cur?"past":i===cur?"now":"";e.querySelector("s").style.width=i===cur?(100-left/144)+"%":"0"})}

/* ---------- start / stop */
$("#go").onclick=async()=>{if(S.running){await api("/api/stop",{});toast(t("stopping"));return refresh()}
  const c=S.config,real=c.account==="real",d=$("#d-start");d.className=real?"real":"";
  $("#ds-title").textContent=real?t("real_title"):t("start_title");
  $("#ds-disc").textContent=disc();
  const sum=$("#ds-sum");sum.textContent="";[[t("sum_exchange"),exName(c.exchange)+", "+t("acc_"+c.account)],[t("sum_strats"),t("n_of",c.strategies.length,S.strategies.length)],
    [t("sum_coins"),String(c.coins.length)],[t("sum_size"),c.sizing==="risk"?t("size_risk",num(c.risk_pct)):t("size_notional",num(c.notional_usdt))],[t("sum_lev"),num(c.leverage)+"x, "+c.margin_mode]]
    .forEach(([a,b])=>sum.append(h("dt",null,a),h("dd",null,b)));
  const w=[];if(isDirty())w.push(t("dirty_warn"));if(deviation(c).length)w.push(t("dev_title")+". "+t("dev_text",deviation(c).join(", ")));const ex=extras(c);if(ex.length)w.push(t("extra_text",ex.join(", ")));
  $("#ds-warn").hidden=!w.length;const wt=$("#ds-warn-text");wt.textContent="";w.forEach(s=>wt.append(h("div",null,s)));
  $("#ds-phrase-w").hidden=!real;$("#ds-phrase").value="";$("#ds-phrase-l").textContent=t("phrase_label",S.phrase);
  const ok=$("#ds-ok");ok.textContent=t(real?"start_real":"start_demo");ok.className="btn "+(real?"danger":"primary");ok.disabled=real;show($("#ds-err"),"");d.showModal()};
$("#ds-phrase").oninput=e=>{$("#ds-ok").disabled=e.target.value.trim()!==S.phrase};
$("#start-form").onsubmit=async e=>{e.preventDefault();const [s,d]=await api("/api/start",{confirm:$("#ds-phrase").value.trim()||null});
  if(s!==200)return show($("#ds-err"),errText(d));$("#d-start").close();toast(t("started"));refresh()};

/* ---------- strategy form */
function loadForm(c){form=JSON.parse(JSON.stringify(c));drawForm()}
function isDirty(){return !!(S&&form)&&JSON.stringify(form)!==JSON.stringify(S.config)}
function deviation(c){const names={strategies:t("strategies"),coins:t("coins"),sizing:t("sizing"),risk_pct:t("risk_pct"),fee:"fee",slippage:"slippage"};
  return S.research_keys.filter(k=>JSON.stringify(c[k])!==JSON.stringify(S.defaults[k])).map(k=>names[k]||k)}
function extras(c){return c.coins.filter(x=>!S.defaults.coins.includes(x))}
function setCoins(list){const base=S.defaults.coins.filter(x=>list.includes(x));form.coins=base.concat(list.filter(x=>!S.defaults.coins.includes(x)))}
function drawForm(){if(!S||!form)return;const f=form;
  const sel=$("#f-exchange");sel.textContent="";(S.exchanges.includes(f.exchange)?S.exchanges:[f.exchange].concat(S.exchanges)).forEach(e=>sel.add(new Option(exName(e),e)));sel.value=f.exchange;
  seg($("#f-account"),[["demo","Demo"],["real","Real","real"]],f.account,v=>{f.account=v;drawForm()});
  const sb=$("#f-strats");sb.textContent="";S.strategies.forEach(s=>{const on=f.strategies.includes(s),x=EXITS[s];
    sb.append(h("label",{class:"strat"+(on?" on":"")},h("input",{type:"checkbox",checked:on,onchange:e=>{f.strategies=S.strategies.filter(k=>k===s?e.target.checked:f.strategies.includes(k));drawForm()}}),
      h("span",{class:"box"},icon("check")),h("span",{style:"min-width:0"},h("h3",null,stName(s)),h("p",null,t("sd_"+s)),x&&h("p",{class:"exit"},t("exits",...x)),h("code",null,s))))});
  const cb=$("#f-coins");cb.textContent="";S.defaults.coins.concat(extras(f)).forEach(c=>{const on=f.coins.includes(c),extra=!S.defaults.coins.includes(c);
    cb.append(h("button",{type:"button",class:"chip"+(extra?" extra":""),"aria-pressed":String(on),onclick:()=>{setCoins(on?f.coins.filter(x=>x!==c):f.coins.concat(c));drawForm()}},c))});
  seg($("#f-sizing"),[["risk",t("sz_risk")],["notional",t("sz_notional")]],f.sizing,v=>{f.sizing=v;drawForm()});
  $("#w-risk").hidden=f.sizing!=="risk";$("#w-notional").hidden=f.sizing!=="notional";
  seg($("#f-margin_mode"),[["cross","Cross"],["isolated","Isolated"]],f.margin_mode,v=>{f.margin_mode=v;drawForm()});
  ["risk_pct","notional_usdt","leverage","max_positions","max_total_notional_usdt"].forEach(k=>{const e=$("#f-"+k);if(document.activeElement!==e)e.value=f[k]==null||Number.isNaN(f[k])?"":String(f[k])});
  formState()}
function formState(){if(!S||!form)return;const dev=deviation(form),ex=extras(form);
  $("#deviation").hidden=!dev.length;$("#dev-text").textContent=t("dev_text",dev.join(", "));
  $("#extra-note").hidden=!ex.length;$("#extra-text").textContent=t("extra_text",ex.join(", "));
  $("#coins-n").textContent=t("coins_n",form.coins.length);$("#running-note").hidden=!S.running;
  const k=S.keys[form.exchange],a=$("#acc-keys");a.textContent="";a.append(t(k&&k[form.account]?"keys_yes":"keys_no")+" ");
  if(!(k&&k[form.account]))a.append(h("button",{type:"button",class:"link","data-go":"settings"},t("open_settings")));
  $("#savebar").hidden=!isDirty();if(!isDirty())show($("#save-err"),"")}
$("#f-exchange").onchange=e=>{form.exchange=e.target.value;formState()};
["risk_pct","notional_usdt","leverage","max_positions","max_total_notional_usdt"].forEach(k=>{$("#f-"+k).oninput=e=>{const v=e.target.value.trim().replace(",",".");
  form[k]=v===""?(k==="max_total_notional_usdt"?null:NaN):k==="max_positions"?parseInt(v,10):Number(v);formState()}});
function addCoin(){const e=$("#coin-add"),v=e.value.trim().toUpperCase().replace(/[^A-Z0-9]/g,"");if(v&&!form.coins.includes(v)){setCoins(form.coins.concat(v));drawForm()}e.value=""}
$("#coin-add-btn").onclick=addCoin;$("#coin-add").onkeydown=e=>{if(e.key==="Enter"){e.preventDefault();addCoin()}};
$("#coins-all").onclick=()=>{setCoins(S.defaults.coins.concat(extras(form)));drawForm()};
$("#reset").onclick=()=>{loadForm({...S.defaults,exchange:form.exchange,account:form.account,auto_update:form.auto_update,update_margin_min:form.update_margin_min})};
$("#discard").onclick=()=>loadForm(S.config);
async function saveConfig(cfg){const [s,d]=await api("/api/config",{config:cfg});if(s===200){S.config=d.config;return null}return errText(d)}
$("#save").onclick=async()=>{if(!form.strategies.length||!form.coins.length)return show($("#save-err"),t("pick_one"));
  const bad=[["risk_pct","risk_pct"],["notional_usdt","notional"],["leverage","leverage"],["max_positions","max_pos"]].find(([k])=>!(form[k]>0));
  if(bad)return show($("#save-err"),t("fill",t(bad[1])));
  const err=await saveConfig(form);if(err)return show($("#save-err"),err);loadForm(S.config);toast(t("saved"));refresh()};

/* ---------- settings */
function renderKeys(){const box=$("#keys");box.textContent="";$("#keys-off").hidden=S.keys_writable;$("#no-bytx").hidden=S.exchanges.length>0;
  S.exchanges.forEach(ex=>{const row=h("div",{class:"krow"},h("div",{class:"kname"},exName(ex)));
    ["demo","real"].forEach(acc=>{const set=S.keys[ex][acc],locked=S.running&&S.active&&S.active.exchange===ex&&S.active.account===acc;
      const cell=h("div",{class:"kcell"},h("span",{class:"kacc"},acc),h("span",{class:"badge"+(set?" ok":"")},t(set?"k_set":"k_unset")));
      if(locked)cell.append(h("span",{class:"hint"},t("k_locked")));
      else if(S.keys_writable){cell.append(h("button",{class:"btn sm",onclick:()=>openKeys(ex,acc)},t(set?"replace":"add")));
        if(set)cell.append(h("button",{class:"btn sm",onclick:()=>removeKeys(ex,acc)},t("remove")))}
      row.append(cell)});box.append(row)})}
function openKeys(ex,acc){keysFor=[ex,acc];$("#dk-title").textContent=t("keys_for",exName(ex),acc);const box=$("#dk-fields");box.textContent="";
  S.key_fields[ex].forEach(f=>box.append(h("div",{class:"field"},h("label",{for:"k-"+f},t("f_"+f)),h("input",{id:"k-"+f,name:f,type:"password",autocomplete:"off",spellcheck:"false",required:true}))));
  show($("#dk-err"),"");$("#d-keys").showModal()}
$("#keys-form").onsubmit=async e=>{e.preventDefault();const fields={};$$("#dk-fields input").forEach(i=>{fields[i.name]=i.value.trim()});
  const [s,d]=await api("/api/keys",{exchange:keysFor[0],account:keysFor[1],fields});$$("#dk-fields input").forEach(i=>{i.value=""});
  if(s!==200)return show($("#dk-err"),errText(d));$("#d-keys").close();toast(t("keys_saved"));refresh()};
function ask(title,text,ok){$("#da-title").textContent=title;$("#da-text").textContent=text;$("#da-ok").textContent=ok;const d=$("#d-ask");d.returnValue="";d.showModal();
  return new Promise(r=>{askResolve=r})}
$("#d-ask").addEventListener("close",e=>{if(askResolve)askResolve(e.target.returnValue==="ok");askResolve=null});
async function removeKeys(ex,acc){if(!await ask(t("rm_title",exName(ex),acc),t("rm_text"),t("remove")))return;
  const [s,d]=await api("/api/keys/delete",{exchange:ex,account:acc});toast(s===200?t("keys_removed"):errText(d));refresh()}
function renderBytx(){const b=S.bytx||{},mode=S.config.auto_update;$("#b-own").textContent=(b.own||"")+(b.own_available?" · "+t("own_newer",b.own_available):"");$("#b-cur").textContent=b.current||t("not_installed");
  $("#b-av").textContent=(b.available||t("none_newer"))+(b.incompatible?"; "+t("incompatible",b.incompatible):"");
  $("#b-chk").textContent=b.checked_at?when(b.checked_at*1000):t("never");
  seg($("#b-mode"),[["on",t("m_on")],["notify",t("m_notify")],["off",t("m_off")]],mode,async v=>{const err=await saveConfig({...S.config,auto_update:v});
    if(err)return toast(err);if(form)form.auto_update=v;toast(t("saved"));render()});
  $("#b-mode-hint").textContent=t("mh_"+mode)+(S.running&&S.active.auto_update!==mode?" "+t("running_note"):"");
  $("#b-update").disabled=!S.running;$("#b-update-hint").textContent=S.running?"":t("update_idle")}
$("#b-update").onclick=async()=>{const [s,d]=await api("/api/update",{});toast(s===200?t("update_asked"):errText(d))};
function renderTokens(){const box=$("#tokens");box.textContent="";if(!S.tokens.length)box.append(h("p",{class:"mute small"},t("ag_none")));
  S.tokens.forEach(k=>box.append(h("div",{class:"trow"},h("b",null,k.name),h("span",null,t(k.scopes.includes("control")?"ag_r_control":"ag_r_read")),
    h("span",{class:"mute small"},t("ag_dates",when(k.created*1000),k.last_used?when(k.last_used*1000):t("ag_never"))),
    S.keys_writable&&h("button",{class:"btn sm",onclick:async()=>{if(!await ask(t("ag_rm_title",k.name),t("ag_rm_text"),t("ag_revoke")))return;
      const [s,d]=await api("/api/tokens/delete",{id:k.id});toast(s===200?t("ag_revoked"):errText(d));refresh()}},t("ag_revoke")))));
  $("#tok-form").hidden=!S.keys_writable;$("#tok-off").hidden=S.keys_writable;
  seg($("#tok-scope"),[[false,t("ag_read")],[true,t("ag_control")]],tokControl,v=>{tokControl=v;renderTokens()})}
$("#tok-form").onsubmit=async e=>{e.preventDefault();const name=$("#tok-name").value.trim(),[s,d]=await api("/api/tokens",{name,control:tokControl});
  if(s!==200)return show($("#tok-err"),errText(d));show($("#tok-err"),"");$("#tok-name").value="";$("#dt-title").textContent=t("ag_new_title",name);
  $("#dt-value").value=d.token;$("#dt-copy").textContent=t("copy");$("#d-token").showModal();$("#dt-value").select();refresh()};
$("#dt-copy").onclick=async()=>{const v=$("#dt-value");v.select();try{await navigator.clipboard.writeText(v.value);$("#dt-copy").textContent=t("copied")}catch(e){}};
$("#d-token").addEventListener("close",()=>{$("#dt-value").value=""});
function renderAccess(){$("#acc-state").textContent=S.login?t("acc_user",S.login):t("acc_none");$("#pw-cur-w").hidden=!S.login;$("#logout").hidden=!S.login;
  $("#pw-form").hidden=!S.keys_writable;$("#pw-off").hidden=S.keys_writable;const l=$("#pw-login");if(!l.value&&document.activeElement!==l)l.value=S.login||""}
$("#pw-form").onsubmit=async e=>{e.preventDefault();const [s,d]=await api("/api/password",{current:$("#pw-cur").value,login:$("#pw-login").value.trim(),password:$("#pw-new").value});
  $("#pw-cur").value="";$("#pw-new").value="";show($("#pw-err"),s===200?"":errText(d));if(s===200){toast(t("pw_saved"));refresh()}};
$("#logout").onclick=async()=>{await api("/api/logout",{});location.reload()};
$("#login-form").onsubmit=async e=>{e.preventDefault();const [s]=await api("/api/login",{login:$("#l-login").value,password:$("#l-pass").value});
  $("#l-pass").value="";show($("#l-err"),s===200?"":t(s===429?"locked":s===0?"e_net":"bad_login"));if(s===200){form=null;refresh()}};

/* ---------- journal */
async function loadJournal(){const [s,d]=await api("/api/journal");if(s===200){jevents=d.events||[];renderJournal()}}
function detail(k,v){if(v==null||v==="")return null;if(/^(bar|entry_ts|exit_ts)$/.test(k)&&typeof v==="number")v=when(v);else if(k==="strategy")v=stName(v);
  else if(Array.isArray(v)){if(!v.length)return null;v=v.join(", ")}else if(typeof v==="object")v=JSON.stringify(v);else if(typeof v==="number")v=num(v,7);
  return h("span",null,h("i",null,k.replace(/_/g," ")+" "),String(v))}
function renderJournal(){if(!S)return;const c=shown(),box=$("#journal");box.textContent="";$("#j-lead").textContent=t("j_lead",exName(c.exchange),t("acc_"+c.account));
  seg($("#j-filter"),[["all",t("j_all")],["bad",t("j_bad")]],jfilter,v=>{jfilter=v;renderJournal()});
  const lvl=r=>r.error||BAD.test(r.ev)?"bad":WARN.test(r.ev)?"warn":"";
  const rows=jevents.slice().reverse().filter(r=>jfilter==="all"||lvl(r));
  if(!rows.length)return box.append(jevents.length?empty(t("j_none_bad")):empty(t("j_empty"),t("j_empty_text")));
  rows.forEach(r=>box.append(h("div",{class:"jrow "+lvl(r)},h("time",{class:"num"},r.t?when(r.t,true):""),h("div",{class:"jev"},(EV[lang]&&EV[lang][r.ev])||r.ev),
    h("div",{class:"jd"},Object.keys(r).filter(k=>k!=="t"&&k!=="ev").map(k=>detail(k,r[k]))))))}

function fromHash(){const p=location.hash.slice(1);if(p!==page&&/^(overview|strategy|settings|journal)$/.test(p))go(p)}
addEventListener("hashchange",fromHash);fromHash();
$$("[data-promo]").forEach(e=>{e.hidden=store.get("promo."+e.dataset.promo)==="1"});
applyTheme();applyLang();refresh();setInterval(refresh,5000);setInterval(tick,1000);
</script></body></html>"""
PAGE = re.sub(r"\{\{site:(\w+)\}\}", lambda m: html.escape(site_link(m.group(1))), PAGE)
