# Agent API

The panel (`python -m trend4h panel`) lets your own AI agent or script work with the executor through access
tokens. An agent uses the public interfaces only: this HTTP API, or the MCP server that wraps it.

## Tokens

Create a token in the panel: Settings, Agents. Choose its rights:

| Right | What it allows |
|---|---|
| `read` | status, positions, trades, journal, settings |
| `control` | `read`, plus: save strategy settings, start and stop on the **demo** account, close the executor's demo positions |

The value is shown once. The panel stores only its SHA-256 (`state/panel-tokens.json`, mode 600). Tokens are
created and revoked only by a person in the panel, over a local or HTTPS connection.

Limits that apply to every token and cannot be changed:

- no access to exchange API keys (read, write or delete), the panel password, tokens, or bytx updates;
- a real account is never started, stopped, closed or selected by a token: only a person in the panel, with the
  confirmation phrase. An attempt is answered with `403 real_forbidden` and written to the journal;
- settings sent by an agent pass the same checks as settings from the page (the stop-loss cannot be turned off,
  leverage limits and so on); `auto_update` and `update_margin_min` cannot be changed;
- the bytx integrity and broker attribution checks work as usual.

Over plain HTTP from another machine the token travels in clear text. Use HTTPS (`--certfile`, or a reverse proxy)
or an SSH tunnel: `ssh -L 8790:127.0.0.1:8790 server`.

## Requests

Every request carries `Authorization: Bearer <token>`. No cookie and no CSRF token. Bodies and answers are JSON.

```bash
export TREND4H_TOKEN=...          # the value from the panel
API=http://127.0.0.1:8790/api/v1
curl -s -H "Authorization: Bearer $TREND4H_TOKEN" $API/status
curl -s -H "Authorization: Bearer $TREND4H_TOKEN" $API/positions
curl -s -H "Authorization: Bearer $TREND4H_TOKEN" -H 'Content-Type: application/json' \
     -d '{"config": {"leverage": 2, "coins": ["BTC", "ETH"]}}' $API/config
curl -s -X POST -H "Authorization: Bearer $TREND4H_TOKEN" $API/start
curl -s -X POST -H "Authorization: Bearer $TREND4H_TOKEN" $API/stop
```

| Method and path | Right | Answer |
|---|---|---|
| `GET /api/v1/token` | read | `name`, `scopes` of this token |
| `GET /api/v1/status` | read | `running`, `ready`, `error`, `exchange`, `account`, `blocked`, `now`, `next_bar` (ms), `balance`, `open_positions`, `trades`, `sum_R`, `pnl_usdt`, `keys_found`, `warnings` |
| `GET /api/v1/positions` | read | `positions`: strategy, coin, side (1 long, -1 short), entry, size, stop, tp, status |
| `GET /api/v1/trades` | read | `trades` (the last 20), `total`, `sum_R`, `pnl_usdt` |
| `GET /api/v1/journal` | read | `events`: the last 300 journal records, oldest first |
| `GET /api/v1/config` | read | `config` (saved), `running_config`, `defaults`, `warnings` |
| `POST /api/v1/config` | control | body `{"config": {...}}`: only the given keys change; the settings apply at the next start |
| `POST /api/v1/start` | control | starts the executor with the saved settings; demo only |
| `POST /api/v1/stop` | control | stops it; positions and their stops stay on the exchange |
| `POST /api/v1/close` | control | closes the positions the executor opened on the demo account; the executor must be stopped; answers `closed`, `still_open` |

Results in `trades` and `status` are estimated from stop and take-profit levels, as in the panel. The setting
names and their limits are in the README, section Settings.

Every `control` request is written to the journal of the account with the name of the token
(`agent_config`, `agent_start`, `agent_stop`, `agent_close`, `agent_refused`).

## Errors

Errors are `{"error": "<text>", "code": "<code>"}`.

| Status | Code | Meaning |
|---|---|---|
| 400 | `bad_request` | settings did not pass the checks, or the executor is already running |
| 400 | `no_keys` | there are no API keys for the demo account: a person adds them in the panel |
| 401 | `unauthorized` | the token is missing, wrong or revoked |
| 403 | `scope` | the token has only the `read` right |
| 403 | `real_forbidden` | the request touches a real account |
| 403 | `forbidden` | the request changes bytx update settings |
| 404 | `not_found` | no such path in the agent API |
| 409 | `running` | stop the executor before closing positions |
| 429 | `rate_limited` | more than 120 requests a minute with this token |
| 429 | `locked` | five wrong tokens from this address: wait five minutes |
| 502 | `failed` | the exchange or the bytx checks refused while closing positions; nothing is re-sent |

## MCP

```bash
pip install -e '.[agent]'
TREND4H_TOKEN=... python -m trend4h mcp --url http://127.0.0.1:8790
```

A stdio MCP server with the tools `status`, `positions`, `trades`, `journal`, `get_config` and, for a token with
the `control` right, `set_config`, `start_demo`, `stop`, `close_positions`. The token is read from the environment
variable named by `--token-env` (default `TREND4H_TOKEN`), never from the command line. When the panel refuses a
request, the tool returns `{"ok": false, "status": ..., "code": ..., "error": ...}` with the codes from the table above.
Works with `mcp` 1.x and 2.x. Client configuration:

```json
{
  "mcpServers": {
    "trend-4h": {
      "command": "python",
      "args": ["-m", "trend4h", "mcp", "--url", "http://127.0.0.1:8790"],
      "env": {"TREND4H_TOKEN": "<token from the panel>"}
    }
  }
}
```
