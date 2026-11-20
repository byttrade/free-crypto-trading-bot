"""MCP server (stdio) for your own agent: a thin wrapper over the panel's token API (/api/v1, see docs/agent-api.md).

    TREND4H_TOKEN=... python -m trend4h mcp --url http://127.0.0.1:8790

The token is read from an environment variable only. What the agent may do is decided by the panel: the tools
below just call it. Needs the `mcp` package: pip install -e '.[agent]'.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def http_call(url: str, token: str):
    if not url.startswith(("http://", "https://")):
        raise ValueError("--url: an http:// or https:// address of the panel")

    def call(method: str, what: str, body: dict | None = None):
        req = urllib.request.Request(f"{url.rstrip('/')}/api/v1/{what}", method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:  # returned, not raised: the agent must see why the panel refused
            try:
                info = json.load(e)
            except ValueError:
                info = {}
            return {"ok": False, "status": e.code, "code": info.get("code"), "error": info.get("error") or str(e.reason)}
        except OSError as e:
            return {"ok": False, "status": 0, "code": "unreachable", "error": f"the panel at {url} does not answer: {e}"}
    return call


def tools(call, scopes) -> dict:
    """{tool name: function}; the control tools exist only for a token with the control right."""

    def status() -> dict:
        """State of the executor: running or not, exchange and account, time of the next 4h bar, balance, totals."""
        return call("GET", "status")

    def positions() -> dict:
        """Open positions of the executor: strategy, coin, side, entry, size, stop, take-profit."""
        return call("GET", "positions")

    def trades() -> dict:
        """The last closed trades and the totals in USDT and R (estimated from stop / take-profit levels)."""
        return call("GET", "trades")

    def journal() -> dict:
        """The last events of the executor's journal, oldest first."""
        return call("GET", "journal")

    def get_config() -> dict:
        """Saved settings, the research defaults and notes about settings that differ from the research."""
        return call("GET", "config")

    def set_config(config: dict) -> dict:
        """Save strategy settings (only the given keys change). Demo account only; applies at the next start."""
        return call("POST", "config", {"config": config})

    def start_demo() -> dict:
        """Start the executor with the saved settings on the demo account. A real account cannot be started here."""
        return call("POST", "start", {})

    def stop() -> dict:
        """Stop the executor. Positions and their stops stay on the exchange."""
        return call("POST", "stop", {})

    def close_positions() -> dict:
        """Close the positions the executor opened on the demo account (stop the executor first)."""
        return call("POST", "close", {})

    out = {f.__name__: f for f in (status, positions, trades, journal, get_config)}
    if "control" in scopes:
        out.update({f.__name__: f for f in (set_config, start_demo, stop, close_positions)})
    return out


def build(call, server_class):
    info = call("GET", "token")
    if "scopes" not in info:
        raise ValueError(f"the panel refused the token: {info.get('error')}")
    server = server_class("free-crypto-trading-bot")
    for name, fn in tools(call, info["scopes"]).items():
        server.add_tool(fn, name=name)
    return server


def server_class():
    """The server class of the official `mcp` package: MCPServer in mcp 2, FastMCP in mcp 1."""
    try:
        from mcp.server.mcpserver import MCPServer
        return MCPServer
    except ImportError:
        from mcp.server.fastmcp import FastMCP
        return FastMCP


def main(a):
    token = os.environ.get(a.token_env)
    if not token:
        sys.exit(f"set the token in the {a.token_env} environment variable (create it in the panel: Settings, Agents)")
    try:
        cls = server_class()
    except ImportError:
        sys.exit("the mcp package is not installed: pip install -e '.[agent]'  (or: pip install mcp)")
    try:
        server = build(http_call(a.url, token), cls)
    except ValueError as e:
        sys.exit(str(e))
    server.run()  # stdio
