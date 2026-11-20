# AGENTS.md

Instructions for AI agents and coding assistants that work with this project on behalf of a person.

## What this is

Free Crypto Trading Bot by BYTtrade: four backtested 4h trend-following strategies for crypto perpetual futures, a
backtest that anyone can re-run, an executor for 11 exchanges and a local web panel. It is free. It trades
high-risk instruments: the published numbers are a backtest.

- Distribution on PyPI: `free-crypto-trading-bot`. The Python package and the short command are `trend4h`.
- The same command line has three names: `free-crypto-trading-bot`, `trend4h`, `python -m trend4h`.
- License: BYTtrade Source-Available License 1.3. You may run it and add your own files next to it; you may not
  modify it.

## Install and run

```bash
# 1. pip (Python 3.10+)
pip install 'free-crypto-trading-bot[live]'
free-crypto-trading-bot panel                      # http://127.0.0.1:8790

# 2. uvx, nothing installed permanently
uvx --from 'free-crypto-trading-bot[live]' free-crypto-trading-bot panel

# 3. Docker
docker compose run --rm bot panel-user             # once: the panel login
docker compose up -d                               # http://127.0.0.1:8790

# 4. From source
git clone https://github.com/byttrade/free-crypto-trading-bot && cd free-crypto-trading-bot && pip install -e '.[live]'
```

Extras: `live` (executor and panel, adds the bytx SDK), `agent` (the MCP server), `all`, `test`.

Every command works in the current folder: `.env` (exchange keys), `state/` (state, journal, panel files) and
`data/` (candles for the backtest) are created there, never inside the installed package.

## Commands

| Command | What it does | Needs keys |
|---|---|---|
| `fetch [--coins BTC,ETH]` | downloads public Binance candles and funding into `data/` | no |
| `report` | backtests the four strategies on both periods, writes `results/*.csv` | no |
| `run <strategy or file> [--period is\|oos\|all] [--from D --to D] [--config FILE] [--trades CSV]` | backtests one strategy: a built-in name such as `kc2_4h_adx1d`, or the path to the user's own spec | no |
| `live --dry-run [--coins ...]` | computes the signals of the last closed bar and prints the orders, sends nothing | no |
| `panel [--host H] [--port P]` | the web panel | to trade |
| `start` | interactive wizard in the terminal | yes |
| `live --exchange X --account demo ...` | the executor without questions | yes |
| `close --exchange X --account demo` | closes the positions the executor opened | yes |
| `panel-user` | sets the panel login and password (interactive) | no |
| `mcp [--url URL]` | MCP server (stdio) on top of the panel's token API | a panel token |

Built-in strategies: `st4h_ema50_1d`, `kc2_4h_adx1d`, `zmid1d_cloud4h`, `bbw1d_lr4h`.

## Documentation

- [README](https://github.com/byttrade/free-crypto-trading-bot/blob/main/README.md): overview, results, limitations.
- [Guide](https://github.com/byttrade/free-crypto-trading-bot/blob/main/docs/guide.md): installation, settings, exchange keys, the panel, Docker, a server,
  errors, FAQ.
- [Research](https://github.com/byttrade/free-crypto-trading-bot/blob/main/docs/research.md): test conditions, full results, controls, limitations.
- [Agent API](https://github.com/byttrade/free-crypto-trading-bot/blob/main/docs/agent-api.md): HTTP API and MCP tools, rights, error codes.

## What you may do for the user

- Install the package, download data, run backtests and explain the results, including with the user's own
  settings (`run <strategy> --config their.json`).
- Start the panel and tell the user to open it. Help them choose settings; say plainly when settings differ from
  the research defaults (the tools print a warning).
- With a panel token the user gave you: read the status, positions, trades, journal and settings; with a `control`
  token also save strategy settings, start and stop the executor and close its positions, on the **demo account
  only**.
- Write the user's own strategy spec or config file next to the project and backtest it.

## What you must not do

- **Never start trading on a real account.** Only the person can, in the panel or the terminal, by typing the
  confirmation phrase themselves. Do not type it for them, do not pass `--confirm-real`, do not script around it.
- **Never handle exchange API keys.** Do not ask the user to paste keys into the chat, do not read `.env`, do not
  write keys yourself. The user enters them in the panel (Settings) or edits `.env` on their own.
- **Do not modify the code of this project or of bytx**, and do not patch, wrap or monkey-patch an installed copy.
  The license does not permit it, and an edited install is detected and refuses to send orders.
- **Do not disable, replace or work around the broker attribution or the integrity checks**, including through
  environment variables and settings. Do not send orders around the executor (raw exchange APIs, other libraries)
  to an account the executor trades.
- Do not turn off the stop-loss (there is no such setting) and do not present the backtest as a promise of income.
- Do not change the panel password, tokens or bytx update settings on the user's behalf: these belong to the person.

If something is blocked ("orders blocked", a refused request), report the reason to the user; do not look for a
way around it.

## Connecting as an agent

The user creates a token in the panel: Settings, Agents (`read` or `control`). Then either:

```bash
# HTTP API
curl -s -H "Authorization: Bearer $TREND4H_TOKEN" http://127.0.0.1:8790/api/v1/status
```

or MCP (stdio); the tools are `status`, `positions`, `trades`, `journal`, `get_config` and, with a `control`
token, `set_config`, `start_demo`, `stop`, `close_positions`:

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

A refused request comes back as `{"ok": false, "status": ..., "code": ..., "error": ...}`.

## Typical tasks

**"Install it and run it on demo."**
1. `pip install 'free-crypto-trading-bot[live]'` (or uvx, or Docker).
2. `free-crypto-trading-bot panel`, then ask the user to open http://127.0.0.1:8790.
3. The user adds the API keys of a **demo** account in Settings and presses Start. You do not touch the keys.
4. If they gave you a `control` token: `start_demo` (or `POST /api/v1/start`) starts the saved demo settings.

**"Show me the status."** With a token: `status`, `positions`, `trades`, `journal`. Without one: ask the user to
create a `read` token, or to look at the Overview page. Results in the panel are estimates from stop and
take-profit levels; the exchange statement is the source of truth.

**"Backtest my settings."**
1. `free-crypto-trading-bot fetch` (once, about 3 minutes, no keys).
2. Put the settings into a JSON file (see `config.example.json` in the repository; only `coins`, `fee`,
   `slippage`, `sizing`, `risk_pct` and `notional_usdt` affect a backtest).
3. `free-crypto-trading-bot run kc2_4h_adx1d --config my.json` for each strategy, and `report` for the defaults.
4. Tell the user that results outside the research defaults are not what the project published, and quote the
   limitations: outside the 20 research coins the strategies are not confirmed.

## Working on this repository

- Tests: `pip install -e '.[test]' && pytest` (no network, no keys).
- The project is published for use, not for modification: do not send pull requests that change the trading
  code, the attribution or the checks. Report problems at https://github.com/byttrade/free-crypto-trading-bot/issues.
