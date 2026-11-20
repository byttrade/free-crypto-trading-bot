"""Local control panel: one page to start / stop the executor and watch it. Standard library only.

Security
- listens on 127.0.0.1 by default; any other address is refused unless a login and password are set
  (`python -m trend4h panel-user`); only a salted scrypt hash of the password is stored (file mode 600);
- login form + signed session cookie (HttpOnly, SameSite=Strict), constant-time comparisons,
  delay and temporary lock-out after failed logins, CSRF token on every state-changing request, POST only;
- exchange keys are never entered or shown here: they are read from .env on the server;
- no TLS by default: put it behind an HTTPS reverse proxy or use an SSH tunnel (or pass --certfile/--keyfile).
"""
from __future__ import annotations

import asyncio
import getpass
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config as cfgmod
from . import live

LOCAL = ("127.0.0.1", "localhost", "::1")
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
                         f"`python -m trend4h panel-user` first (and use HTTPS or an SSH tunnel: the panel has no TLS "
                         f"of its own unless --certfile/--keyfile are given)")


# ---------------------------------------------------------------- executor runner

class Runner:
    """Runs the same executor as `python -m trend4h live` in a background thread (one instance, same lock)."""

    def __init__(self, state_dir="state", serve=live.serve):
        self.state_dir, self.serve = state_dir, serve
        self.thread = self.stop_event = self.loop = self.executor = None
        self.cfg, self.error, self.restart = None, None, False
        self.lines = []

    @property
    def running(self):
        return bool(self.thread and self.thread.is_alive())

    def start(self, cfg: dict):
        if self.running:
            raise ValueError("already running")
        self.cfg, self.error, self.executor = cfg, None, None

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

    def status(self) -> dict:
        x = self.executor
        up = getattr(x, "updater", None)
        return {"running": self.running, "error": self.error, "config": self.cfg,
                "summary": x.summary() if x else None, "journal": self.lines[-40:],
                "bytx": up.status() if up else None}


# ---------------------------------------------------------------- http

class Panel:
    def __init__(self, runner: Runner, user=None, clock=time.time, sleep=time.sleep):
        self.runner, self.user, self.clock, self.sleep = runner, user, clock, sleep
        self.secret = secrets.token_bytes(32)
        self.fails = {}  # ip -> (count, locked_until)

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
        live.load_env()
        return bool(live.credentials(exchange, account))

    def handle(self, method, path, cookie, ip, body: dict, csrf_header):
        """Pure request logic: returns (status, json-able payload or html string, new cookie or None)."""
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
            st = self.runner.status()
            cfg = st["config"] or cfgmod.DEFAULTS
            try:
                exchanges = live.supported_exchanges()
            except ImportError:
                exchanges = []
            return 200, {**st, "csrf": self.csrf(cookie), "defaults": cfgmod.DEFAULTS, "exchanges": exchanges,
                         "strategies": cfgmod.STRATEGIES, "disclaimer": live.DISCLAIMER, "phrase": live.ACCOUNT_PHRASE,
                         "keys_found": self.keys_found(cfg["exchange"], cfg["account"])}, None
        if method != "POST":
            return 405, {"error": "POST only"}, None
        if not csrf_header or not hmac.compare_digest(csrf_header, self.csrf(cookie)):
            return 403, {"error": "CSRF token missing or wrong"}, None
        try:
            if path == "/api/start":
                cfg = cfgmod.validate(body.get("config") or {})
                if cfg["account"] == "real" and body.get("confirm") != live.ACCOUNT_PHRASE:
                    return 400, {"error": f'real account: type "{live.ACCOUNT_PHRASE}" to confirm'}, None
                if not self.keys_found(cfg["exchange"], cfg["account"]):
                    return 400, {"error": f"no API keys for {cfg['exchange']} {cfg['account']} in .env on the server"}, None
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
        except ValueError as e:
            return 400, {"error": str(e)}, None
        return 404, {"error": "not found"}, None


def make_handler(panel: Panel, secure: bool):
    class Handler(BaseHTTPRequestHandler):
        def _cookie(self):
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "t4h":
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
            status, payload, cookie = panel.handle(method, self.path.split("?")[0], self._cookie(),
                                                   self.client_address[0], body, self.headers.get("X-CSRF-Token"))
            data = payload.encode() if isinstance(payload, str) else json.dumps(payload, default=float).encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8" if isinstance(payload, str) else "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'")
            if cookie:
                value, age = ("", 0) if cookie == "deleted" else (cookie, SESSION_SEC)
                self.send_header("Set-Cookie", f"t4h={value}; Path=/; Max-Age={age}; HttpOnly; SameSite=Strict"
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
    server = ThreadingHTTPServer((a.host, a.port), make_handler(Panel(runner, user), bool(a.certfile)))
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


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>trend-4h panel</title><style>
body{font:14px system-ui,sans-serif;margin:0;background:#f5f5f3;color:#1c1c1a}
@media(prefers-color-scheme:dark){body{background:#161615;color:#e8e8e3}input,select,button{background:#262624;color:inherit;border-color:#444}}
main{max-width:980px;margin:0 auto;padding:16px}
.warn{background:#7a1f1f;color:#fff;padding:10px 16px;position:sticky;top:0}
fieldset{border:1px solid #8884;border-radius:8px;margin:12px 0;padding:12px}label{display:inline-block;margin:4px 12px 4px 0}
input,select,button{font:inherit;padding:5px 8px;border:1px solid #999;border-radius:6px}button{cursor:pointer}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #8883;padding:4px 6px;text-align:left;font-variant-numeric:tabular-nums}
pre{white-space:pre-wrap;word-break:break-all;max-height:260px;overflow:auto;background:#8881;padding:8px;border-radius:6px;font-size:12px}
.err{color:#c0392b}.ok{color:#2e8b57}.hide{display:none}</style></head><body>
<div class="warn" id="disc">HIGH RISK. BYTtrade is not responsible for the results or losses.</div><main>
<h2>trend-4h</h2>
<form id="login" class="hide"><fieldset><legend>Login</legend>
<label>Login <input name="login" autocomplete="username"></label>
<label>Password <input name="password" type="password" autocomplete="current-password"></label>
<button>Sign in</button> <span class="err" id="lerr"></span></fieldset></form>
<div id="app" class="hide">
<p>State: <b id="state"></b> &nbsp; next 4h bar: <span id="next"></span> &nbsp; <span class="err" id="blocked"></span></p>
<fieldset><legend>Settings</legend>
<label>Exchange <select id="exchange"></select></label>
<label>Account <select id="account"><option>demo</option><option>real</option></select></label>
<span id="keys"></span><br>
<label>Strategies <span id="strats"></span></label><br>
<label>Size <select id="sizing"><option value="risk">risk, % of equity per trade</option><option value="notional">fixed notional, USDT per trade</option></select></label>
<label>Risk % <input id="risk_pct" type="number" step="0.001" style="width:80px"></label>
<label>Notional USDT <input id="notional_usdt" type="number" style="width:90px"></label>
<label>Leverage <input id="leverage" type="number" style="width:60px"></label>
<label>Margin <select id="margin_mode"><option>cross</option><option>isolated</option></select></label><br>
<label>Max positions <input id="max_positions" type="number" style="width:60px"></label>
<label>Max total notional USDT <input id="max_total_notional_usdt" type="number" placeholder="no cap" style="width:100px"></label>
<label>Coins <input id="coins" style="width:420px"></label>
<label>bytx auto-update <select id="auto_update"><option>on</option><option>notify</option><option>off</option></select></label>
<p>Backtest results apply to the default settings only. Outside the 20 research coins the strategies are not confirmed. The stop-loss cannot be disabled.</p>
<button id="start">Start</button> <button id="stop">Stop</button> <button id="logout">Sign out</button>
<span class="err" id="err"></span> <span id="msg"></span></fieldset>
<fieldset><legend>Result</legend><span id="total"></span></fieldset>
<fieldset><legend>Open positions of the executor</legend><table id="open"></table></fieldset>
<fieldset><legend>Last trades</legend><table id="closed"></table></fieldset>
<fieldset><legend>bytx</legend><span id="bytx"></span> <button id="update">Update now</button></fieldset>
<fieldset><legend>Journal</legend><pre id="journal"></pre></fieldset></div></main>
<script>
const $=id=>document.getElementById(id);let csrf="",filled=false,phrase="";
async function api(path,body){const r=await fetch(path,body===undefined?{}:{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":csrf},body:JSON.stringify(body)});
  return [r.status,await r.json().catch(()=>({}))]}
function table(el,rows,cols){el.textContent="";const h=el.insertRow();cols.forEach(c=>{const th=document.createElement("th");th.textContent=c;h.appendChild(th)});
  rows.forEach(r=>{const tr=el.insertRow();cols.forEach(c=>{let v=r[c];if(typeof v==="number")v=+v.toPrecision(7);if(c==="side"&&typeof r[c]==="number")v=r[c]>0?"long":"short";tr.insertCell().textContent=v==null?"":v})})}
function fill(d){const c=d.config||d.defaults;$("exchange").textContent="";d.exchanges.forEach(e=>$("exchange").add(new Option(e)));
  $("strats").textContent="";d.strategies.forEach(s=>{const l=document.createElement("label"),i=document.createElement("input");i.type="checkbox";i.value=s;i.checked=c.strategies.includes(s);l.append(i," "+s);$("strats").appendChild(l)});
  ["exchange","account","sizing","risk_pct","notional_usdt","leverage","margin_mode","max_positions","auto_update"].forEach(k=>$(k).value=c[k]);
  $("max_total_notional_usdt").value=c.max_total_notional_usdt||"";$("coins").value=c.coins.join(",");filled=true}
function collect(){const n=k=>parseFloat($(k).value);return{exchange:$("exchange").value,account:$("account").value,
  strategies:[...document.querySelectorAll("#strats input:checked")].map(i=>i.value),sizing:$("sizing").value,risk_pct:n("risk_pct"),notional_usdt:n("notional_usdt"),
  leverage:n("leverage"),margin_mode:$("margin_mode").value,max_positions:parseInt($("max_positions").value),max_total_notional_usdt:n("max_total_notional_usdt")||null,
  coins:$("coins").value.split(",").map(s=>s.trim()).filter(Boolean),auto_update:$("auto_update").value}}
async function refresh(){const [s,d]=await api("/api/status");if(s===401){$("login").classList.remove("hide");$("app").classList.add("hide");return}
  $("login").classList.add("hide");$("app").classList.remove("hide");csrf=d.csrf;phrase=d.phrase;$("disc").textContent=d.disclaimer;if(!filled)fill(d);
  $("state").textContent=d.running?"running":"stopped";$("state").className=d.running?"ok":"";$("err").textContent=d.error||"";
  $("keys").textContent="API keys on the server: "+(d.keys_found?"found":"not found (put them into .env)");
  const m=d.summary;$("next").textContent=m?new Date(m.next_bar).toISOString().slice(0,16)+" UTC":"";$("blocked").textContent=m&&m.blocked?"orders blocked: "+m.blocked:"";
  $("total").textContent=m?`${m.trades} closed trades, ${m.sum_R} R, ${m.pnl_usdt} USDT (estimated from stop / take-profit levels)`:"";
  table($("open"),m?m.open:[],["strategy","coin","side","entry","size","stop","tp","status"]);
  table($("closed"),m?m.closed.slice().reverse():[],["strategy","coin","side","entry","exit","reason","pnl","R"]);
  const b=d.bytx;$("bytx").textContent=b?`installed ${b.current}; available ${b.available||"none newer"}${b.incompatible?"; incompatible newer version "+b.incompatible+" (update trend-4h)":""}; last check ${b.checked_at?new Date(b.checked_at*1000).toISOString().slice(0,16)+" UTC":"never"}; mode ${b.mode}`:"shown while the executor runs";
  $("journal").textContent=d.journal.slice().reverse().join("\n")}
$("login").onsubmit=async e=>{e.preventDefault();const f=new FormData(e.target);const [s]=await api("/api/login",{login:f.get("login"),password:f.get("password")});
  $("lerr").textContent=s===200?"":s===429?"too many attempts, try later":"wrong login or password";e.target.password.value="";refresh()};
$("start").onclick=async()=>{const config=collect();let confirm=null;
  if(config.account==="real"){confirm=prompt($("disc").textContent+"\n\nREAL ACCOUNT: real money is at risk. The published results are a backtest.\nType the phrase to confirm: "+phrase);if(confirm===null)return}
  const [s,d]=await api("/api/start",{config,confirm});$("err").textContent=s===200?"":d.error||"error";$("msg").textContent=(d.warnings||[]).join(" | ");refresh()};
$("stop").onclick=async()=>{await api("/api/stop",{});$("msg").textContent="stopping: positions and their stops stay on the exchange";refresh()};
$("update").onclick=async()=>{const [s,d]=await api("/api/update",{});$("msg").textContent=s===200?"update requested (waits for a safe window)":d.error;};
$("logout").onclick=async()=>{await api("/api/logout",{});location.reload()};
refresh();setInterval(refresh,5000);
</script></body></html>"""
