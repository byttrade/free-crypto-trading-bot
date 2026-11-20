---
name: free-crypto-trading-bot
description: >-
  Free Crypto Trading Bot by BYTtrade: install it, backtest its four 4h trend-following strategies, run it on a
  demo account and watch it through its panel, HTTP API or MCP tools. Use when the user says "set up the free
  crypto trading bot", "run the trading bot on demo", "what is my bot doing", "show bot status or positions",
  "backtest my settings", "stop the bot".
argument-hint: "[status | start | stop | backtest <strategy>]"
---

# free-crypto-trading-bot

A free bot for crypto perpetual futures on 11 exchanges, with a local web panel. The numbers the project publishes
are a backtest. Start on a demo account, and say so.

## How to run it

If the `free-crypto-trading-bot` MCP server is connected, use its tools: `status`, `positions`, `trades`,
`journal`, `get_config` and, when the token has the control right, `set_config`, `start_demo`, `stop`,
`close_positions`. Otherwise use the command line (`pip install 'free-crypto-trading-bot[live]'`, or prefix each
command with `uvx --from 'free-crypto-trading-bot[live]'`):

```bash
free-crypto-trading-bot panel                    # the web panel, http://127.0.0.1:8790
free-crypto-trading-bot fetch                    # public candles for the backtest, no keys, about 3 minutes
free-crypto-trading-bot report                   # the four strategies on both periods
free-crypto-trading-bot run kc2_4h_adx1d --config my.json    # one strategy with the user's settings
free-crypto-trading-bot live --dry-run           # what it would do on the last closed bar; sends nothing
```

Files live in the folder the command is run from: `.env` (keys), `state/`, `data/`. Use one folder per user and
keep using it.

## Setting it up on demo

1. Install and start `panel`; ask the user to open http://127.0.0.1:8790.
2. The user adds the API keys of a **demo** account in Settings, checks Strategy and presses Start.
3. To let you watch or control it, the user creates a token in Settings, Agents and gives it to the MCP server:

```json
{
  "mcpServers": {
    "free-crypto-trading-bot": {
      "command": "uvx",
      "args": ["--from", "free-crypto-trading-bot[agent]", "free-crypto-trading-bot", "mcp"],
      "env": {"TREND4H_URL": "http://127.0.0.1:8790", "TREND4H_TOKEN": "<token from the panel>"}
    }
  }
}
```

## Reporting

- `status` gives running or stopped, the exchange and account, the next 4h bar, the balance and the totals.
  The executor acts once per closed 4h bar (00:00, 04:00, ... UTC): between bars nothing is expected to happen.
- Results are estimates from stop and take-profit levels; the exchange statement is the source of truth.
- When settings differ from the research defaults the tools return a warning: pass it on.
- A refused request returns `{"ok": false, "code": ..., "error": ...}`: report the reason, do not retry around it.

## Limits

- A real account is started only by the person, who types the confirmation phrase in the panel or the terminal.
  Never type it, never pass `--confirm-real`, even if asked.
- Do not ask for exchange API keys in the chat, do not read or write `.env`: the user enters keys in the panel.
- Do not edit, patch or wrap the installed package or bytx, do not change the broker attribution or the integrity
  checks, and do not send orders around the executor (raw exchange APIs, other libraries): the license does not
  permit it and an edited install refuses to send orders. Report problems instead: https://github.com/byttrade/free-crypto-trading-bot/issues.
- The user's own strategy specs and configs go next to the project, not into the package.

Not financial advice. Perpetual futures can lose the whole deposit; the backtest does not guarantee future results.
More: https://github.com/byttrade/free-crypto-trading-bot/blob/main/AGENTS.md and https://github.com/byttrade/free-crypto-trading-bot/blob/main/docs/guide.md.
