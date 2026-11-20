# trend-4h: four 4h trend-following strategies on 20 large Binance perpetuals

[Русская версия](README.ru.md)

> **Risk warning and disclaimer.** This is high-risk trading: perpetual futures can lose your entire deposit.
> BYTtrade is not responsible for the results you get or for any losses. Backtest and paper-trading results do not
> guarantee future returns. Nothing here is investment advice. You act at your own risk.
> The software comes without any warranty; see sections 8 and 9 of the [LICENSE](LICENSE).

**Status: research.** The numbers below are a backtest. No real-money track record exists.

What is in the repository:

| Path | Content |
|---|---|
| `trend4h/` | indicators, backtest engine, data loader, executor, control panel (Python 3.10+, numpy and pandas) |
| `specs/` | four declarative strategy specifications (JSON) |
| `results/` | metrics recomputed by this code, plus recorded research tables in `results/recorded/` |
| `tests/` | pytest suite, runs without network |
| `docs/agent-api.md` | HTTP API and MCP server for your own agent |
| `config.example.json`, `.env.example` | settings and key names for the executor |

## Quick start: reproduce the backtest

```bash
git clone https://github.com/byttrade/trend-4h && cd trend-4h
pip install -e .
python -m trend4h fetch          # public Binance USDT-M data, no keys, about 3 minutes, 11 MB in data/
python -m trend4h report         # all four strategies, IS and OOS, rewrites results/*.csv
```

More commands:

```bash
python -m trend4h run specs/kc2_4h_adx1d.json --period oos --trades trades.csv   # one strategy, trade list
python -m trend4h run specs/st4h_ema50_1d.json --from 2024-01-01 --to 2024-12-31 # any date range
python -m trend4h run specs/bbw1d_lr4h.json --config config.example.json          # your coins / costs / sizing
python -m trend4h fetch --coins BTC,ETH --end 2026-06-30                          # part of the data
pip install -e '.[test]' && pytest                                                # tests
```

`fetch` downloads up to 2026-06-30 by default (the end of the verification period), so that `report` gives the same
numbers for everyone. Pass a later `--end` and use `--from/--to` to look at other dates.

## Test conditions

| Parameter | Value |
|---|---|
| Market | Binance USDT-M perpetual futures |
| Coins (20) | BTC ETH SOL XRP BNB DOGE ADA LINK AVAX SUI LTC DOT NEAR AAVE TRX BCH UNI ARB OP INJ |
| Candles | 4h and 1d as published by the exchange (not resampled), from 2023-01-01 |
| Sides | long and short, one position per coin per strategy |
| Selection (IS) | 2023-01-01 … 2025-06-30: rules were searched here |
| Verification (OOS) | 2025-07-01 … 2026-06-30: rules frozen |
| Third period | 2026-07-01 … 2026-11-17: one run at the very end of the research |

SUI trades since 2023-05-03 and ARB since 2023-03-23, so their history is shorter.

### Costs in the backtest

| Item | Value |
|---|---|
| Fee | 0.05 % of notional per side (taker) |
| Slippage | 0.02 % of price per side |
| Funding | actual Binance rates: a long pays a positive rate, a short receives it |
| Share of costs | fees 6–10 R and funding 1–3 R per verification year per strategy, against 108–134 R of result |

### Execution rules

- A signal is known only after the bar closes. A daily condition is taken from the last daily bar that has closed
  by the close of the current 4h bar.
- Entry at the open of the next 4h bar. If long and short fire on the same bar, long wins.
- Stop, take-profit and trailing stop are checked inside the bar by high/low starting with the entry bar. If stop
  and take-profit are both touched in one bar, the stop counts. A gap through a level fills at the open.
- Distances are multiples of Wilder ATR(14) on 4h, fixed at the close of the signal bar. The trail follows the best
  price since entry and is moved after the stop check of the bar.
- Position size: notional = risk / stop distance. In the tables risk is 1 % of a fixed capital without compounding,
  so 1 R = 1 % of capital. This is a unit of measure, not a recommended size.
- Positions open at the end of a period are closed at the last close of the period.

## The four strategies

One idea: a regime filter on the daily chart, a breakout or flip entry on 4h, a wide trailing stop. There is no
signal exit, only stop, take-profit and trail.

| Spec | Daily filter | 4h entry | Stop / take / trail, ATR(14, 4h) |
|---|---|---|---|
| `st4h_ema50_1d` | daily close above EMA(50): long; below: short | Supertrend(10; 3.0) flips in the direction of the regime | 2 / 10 / 5 |
| `kc2_4h_adx1d` | daily ADX(14) below 20 (no daily trend); direction is not filtered | close crosses a Keltner(20; 20; 2.0) band: upper is long, lower is short | 2 / 10 / 5 |
| `zmid1d_cloud4h` | z-score of the daily close against SMA(75) within [−1; 1] (price near its mean) | close breaks the Ichimoku(9/26/52/26) cloud by more than 0.2 %: up is long, down is short | 3 / 10 / 5 |
| `bbw1d_lr4h` | relative width of daily Bollinger(20; 2.0) below 0.20 (squeeze) | close outside the linear regression channel(50; 2σ): above is long, below is short | 3 / 14 / 4 |

The first three entries are events (a cross); the last one is a state, so `bbw1d_lr4h` may re-enter on the next
bar after an exit while the price is still outside the channel. The exit template was chosen on the selection
period from a small grid; the verification period was not used for the choice.

Indicator details that matter for exact reproduction (all in `trend4h/indicators.py`): EMA is seeded with the SMA
of the first n values; ATR is Wilder's; ADX uses Wilder smoothing seeded with an exponentially weighted mean of the
first n values; Bollinger, z-score and regression channel use the population standard deviation; every indicator
keeps its warm-up bars silent.

## Results

PF is gross profit over gross loss. Σ R is the total result in units of risk per trade. Drawdown is taken on the
daily curve with open positions marked to market, in % of capital at 1 % risk per trade.

| Spec | Period | Trades | PF | Σ R | Σ R without 5 best trades | Drawdown, % | Sharpe |
|---|---|---|---|---|---|---|---|
| `st4h_ema50_1d` | selection | 839 | 1.33 | +180.6 | +154.1 | 41 | 0.96 |
| | verification | 352 | 1.59 | +133.8 | +108.7 | 50 | 1.46 |
| | third (recorded) | 140 | 1.32 | +29.1 | n/a | 36 | n/a |
| `kc2_4h_adx1d` | selection | 660 | 1.53 | +212.0 | +185.5 | 39 | 1.26 |
| | verification | 266 | 1.74 | +119.2 | +94.1 | 51 | 1.43 |
| | third (recorded) | 99 | 1.49 | +33.8 | n/a | 28 | n/a |
| `zmid1d_cloud4h` | selection | 793 | 1.41 | +168.4 | +150.6 | 42 | 1.36 |
| | verification | 318 | 1.70 | +108.4 | +91.6 | 35 | 1.78 |
| | third (recorded) | 133 | 1.29 | +20.4 | n/a | 21 | n/a |
| `bbw1d_lr4h` | selection | 1 048 | 1.41 | +177.8 | +154.2 | 25 | 1.27 |
| | verification | 466 | 1.53 | +111.4 | +88.0 | 69 | 1.43 |
| | third (recorded) | 202 | 1.48 | +45.0 | n/a | 33 | n/a |

Rows marked "recorded" are taken from the research report and are not recomputed by this code here.

All seven half-years 2023H1–2026H1 are positive for every strategy, but unevenly: from +1.2 R to +83.8 R per
half-year (`results/by_half_year.csv`). 15–18 of 20 coins are positive in each period.

An equal mix of the four: Sharpe 1.42 on selection and 1.78 on verification. At 0.25 % risk per trade per strategy
the mix has 52–66 % annual volatility and drawdowns of 28 % and 46 % of capital. That is a lot.

### Parameter stability (recorded)

Every numeric parameter was shifted by ±20 % with the others fixed. PF stayed at 1.15 or higher for all four
strategies on both periods. The weakest point is the Supertrend multiplier 2.4 instead of 3.0: PF 1.21 / 1.23.

### Control without filters (recorded, `results/recorded/controls.csv`)

| Control | PF selection | PF verification | Third period: PF / Σ R / trades |
|---|---|---|---|
| C1: `kc2_4h_adx1d` without the ADX filter | 1.19 | 1.30 | 1.35 / +66 / 281 |
| C2: `st4h_ema50_1d` without the EMA50 regime | 1.21 | 1.24 | 1.29 / +42 / 218 |
| C3: `kc2_4h_adx1d` with the filter inverted (ADX ≥ 20) | 1.09 | 1.15 | 1.27 / +36 / 204 |
| C4: `st4h_ema50_1d` against the regime | 1.24 | 0.88 | 1.29 / +34 / 167 |

The mechanics "breakout or flip on 4h + wide trail" are profitable without filters on all three periods. The
filters lift PF from 1.2–1.3 to 1.3–1.7 on selection and verification, but the third period did not confirm their
contribution: the controls passed there too, even with an inverted filter.

## Reproducibility

| What | Status |
|---|---|
| IS and OOS metrics of the four strategies: `metrics_by_period.csv`, `by_half_year.csv`, `by_side.csv` | **recomputed by this code** |
| Match with the research engine | trade for trade: 4 strategies × 2 periods, 4 742 trades, every entry and exit time and side identical, R of each trade equal to 1e-13, Σ R and PF identical |
| Downloaded data | `fetch` output is byte-identical to the research data set for all 20 coins (4h, 1d, funding) |
| Third period, parameter stability, controls, other perpetuals, limit entries | **taken from the research report**, not recomputed here (`results/recorded/`) |

`tests/expected.json` holds the IS/OOS totals; `pytest` compares `report` against them when `data/` exists.
Exchanges occasionally revise history, so tiny differences are possible if you download much later.

## Run the strategy

> Start with a **demo** account. A backtest does not guarantee the result; outside the 20 coins the strategies
> are not confirmed (see "Honest limitations"). BYTtrade is not responsible for losses.

```bash
pip install -e '.[live]'          # adds the bytx SDK
cp .env.example .env              # put the API keys of your account there
python -m trend4h start           # wizard: exchange, account, strategies, risk; type "start" to begin
```

Without questions: `python -m trend4h live --exchange bybit --account demo --strategies all --risk 0.056`, with
`--dry-run` to compute signals and print the orders without sending anything (works without keys). A config file
(`--config`, see `config.example.json`) holds the same settings. `python -m trend4h close` closes the positions
the executor opened. A real account needs a typed confirmation phrase (`--confirm-real`).

Exchanges: every exchange bytx trades USDT perpetuals on: Binance, Bybit, OKX, BingX, KuCoin, Hyperliquid,
Bitget, Gate, MEXC, HTX, BloFin. Demo accounts exist only where the exchange has a demo or testnet.

What the executor does:

- once per closed 4h bar computes the signals with the same code as the backtest, on public Binance candles
  (whatever exchange you trade on);
- enters with a market order sized by risk and puts the stop-loss on the exchange together with the entry; if the
  stop cannot be placed, the position is closed. A take-profit is a second exchange order. The trailing stop is
  moved once per bar;
- never re-sends an order after a timeout: the outcome is looked up by client_id;
- keeps state and a journal in `state/`, takes a lock (one instance), and after a restart reconciles with the
  exchange; positions keep their exchange stops while it is down;
- skips orders below the exchange minimum (journal record) and checks leverage and position mode before start.

What it does not do:

- it holds **one position per coin**: if two strategies want the same coin, the first one takes it. The backtest
  counts each strategy separately, so live results will differ from the sum of the four backtests;
- it does not touch positions and orders it did not open, and skips a coin that already has a foreign position;
- it does not read exit prices from the exchange: results in the journal and panel are estimated from the stop
  and take-profit levels. Your exchange statement is the source of truth;
- it does not model funding, liquidation or exchange outages.

### Settings

| Setting | Default | Meaning |
|---|---|---|
| `exchange`, `account` | `bybit`, `demo` | where to trade; `demo` or `real` |
| `strategies` | all four | which specs are on |
| `coins` | the 20 research coins | adding others prints a warning: not confirmed there |
| `sizing` | `risk` | `risk`: % of equity lost at the stop; `notional`: fixed USDT per trade |
| `risk_pct` | 0.056 | research value per strategy per trade |
| `notional_usdt` | 100 | used with `sizing: notional` |
| `leverage` | 3 | checked against the exchange limit at start |
| `margin_mode` | `cross` | `cross` or `isolated` |
| `max_positions` | 20 | simultaneously open positions |
| `max_total_notional_usdt` | none | cap on the sum of open notionals |
| `fee`, `slippage` | 0.0005, 0.0002 | backtest only (`run --config`) |
| `auto_update` | `on` | bytx auto-update: `on`, `notify`, `off` |
| `update_margin_min` | 60 | no update closer than this to a 4h bar close (cannot be below 60) |

The stop-loss cannot be disabled. The published results apply to the default settings only; the tools say so when
you change them. `python -m trend4h run <spec> --config your.json` shows your settings on history.

### Control panel

```bash
python -m trend4h panel            # http://127.0.0.1:8790
python -m trend4h panel-user       # set a login and password (needed for any non-local address)
```

Four sections, English and Russian, light and dark theme. Overview: state, Start / Stop, time to the next 4h
bar, balance, result, open positions and last trades. Strategy: exchange and account, strategies, coins, size,
leverage, limits; settings are saved separately from starting. Settings: exchange API keys, bytx updates, the
panel login and password, language and theme. Journal: executor events with a filter for problems. The panel
drives the same executor as `live` (one instance, same lock).

API keys are added in Settings: the panel writes them to the same `.env` (mode 600), never shows them again
(only "set / not set"), and does not let you change the keys of the account the executor is running on. The key
form is available only when the panel is opened locally (127.0.0.1, an SSH tunnel included) or over HTTPS
(`--certfile`, or an HTTPS reverse proxy plus the `--behind-https-proxy` flag); over plain HTTP from another
address put the keys into `.env` on the server instead.

Security: the panel listens on 127.0.0.1. It refuses any other address until a login and password are set. Only
a salted scrypt hash is stored (`state/panel-user.json`, mode 600). Sessions use a signed HttpOnly SameSite=Strict
cookie, failed logins are delayed and locked out, every change is a POST with a CSRF token. **The panel has no
TLS by default**: on a server put it behind an HTTPS reverse proxy or use an SSH tunnel
(`ssh -L 8790:127.0.0.1:8790 server`). Binding to a public address over plain HTTP sends the password in clear
text. `--certfile/--keyfile` turn on TLS from the Python standard library.

### Connect your own agent

Your own AI agent can read the state and, if you allow it, control the executor on the demo account.

1. In the panel open Settings, Agents and create a token: `read` (status, positions, trades, journal, settings)
   or `control` (also: save strategy settings, start and stop on demo, close the executor's demo positions). The
   value is shown once; the panel keeps only its hash.
2. Give the token to the agent: either the HTTP API (`Authorization: Bearer <token>`, see
   [docs/agent-api.md](docs/agent-api.md)), or the MCP server:

```bash
pip install -e '.[agent]'          # adds the mcp package; the panel and the executor work without it
```

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

What no token can do: read, write or delete exchange keys, touch the panel password, tokens or bytx updates, or
start a real account (that takes a person in the panel and the confirmation phrase). Settings from an agent pass
the same checks as settings from the page, and every action of an agent is written to the journal with the token
name. Tokens also work on a non-local address, but over plain HTTP a token travels in clear text: use HTTPS or
an SSH tunnel. An agent works through these public interfaces and does not change the code, which is what the
license allows ("you may add, you may not modify").

### bytx integrity and auto-update

Orders go out only through the released bytx with its broker attribution. Before start, before every batch of
orders and every 10 minutes the executor checks:

| Exchange | bytx files (RECORD sha256, import path) | Attribution source | Tag read back from the order |
|---|---|---|---|
| Bybit | yes | must be `builtin` | no (configuration check only) |
| OKX | yes | must be `builtin` | yes, field `tag` |
| KuCoin | yes | must be `builtin` (partner, key, name, available) | no |
| BloFin | yes | must be `builtin` | yes, field `brokerId` |
| Binance, BingX, Bitget, Gate, MEXC, Hyperliquid, HTX | yes | no built-in code in bytx 0.2.2: source must be `none`, mode not off | no |

The list of exchanges with a built-in code is read from the installed bytx at start, so a newer bytx that adds a
code is enforced without changes here. If a check fails, no new orders are sent and the reason is shown;
protective stops stay on the exchange. Fix: `pip install --force-reinstall bytx` and remove broker settings from
the environment. Changing bytx or its attribution is not permitted by the bytx license (sections 2(e), 3(a), 3(g)).

Auto-update: once a day the executor asks PyPI for a newer bytx inside the allowed range (`>=0.2.2,<0.3`). It
installs only in a safe window (no request in flight, more than an hour to the next 4h bar close, the last bar
processed at least five minutes ago), verifies the new version in a separate process, and restarts. On any
failure it puts the previous version back and continues; exchange stops are never touched. trend-4h itself is not
auto-updated. Turn it off with `"auto_update": "notify"` or `"off"`. Be aware: an auto-update installs code from
PyPI into a process that holds your exchange keys.

## Honest limitations

1. **This is one idea, not four independent sources.** Daily results of the three breakout strategies correlate at
   0.65–0.80 with each other, `st4h_ema50_1d` at 0.47–0.49 with them. Drawdowns come together.
2. **It does not work outside the twenty coins.** The same specs on 639 other Binance perpetuals (filtered by
   turnover and age) gave PF 1.04–1.07 for three strategies on the verification year and 1.34 for `bbw1d_lr4h`,
   which failed the half-year criterion on selection. The control without a filter: PF 1.00. All the profit outside
   the twenty coins is in shorts (PF 1.32–2.03); longs lose (PF 0.60–0.86; `bbw1d_lr4h` 1.07–1.09). At a turnover
   of $20–100M a day the verification result is zero (PF 0.99–1.02). Table: `results/recorded/wide_universe.csv`.
   The result on 20 coins is largely a property of this set of large coins, not general market mechanics.
3. **Shorts are stronger than longs on the twenty coins too.** On the verification year short PF is 1.74–2.65
   against 1.17–1.34 for longs; `st4h_ema50_1d` longs are about zero on selection (PF 0.99). The 2025–2026 market
   was falling. Table: `results/by_side.csv`.
4. **A limit entry makes it worse.** 64 variants of a limit entry instead of a market one were tested: none passed,
   all are below the market entry on the verification year. The reason is adverse selection: trades where the limit
   would fill average 0.32 R, the missed ones 0.72 R. The ceiling of the saving is about 0.011 R per trade. Treating
   the take-profit as a limit order adds 0.5–0.6 pp per year, which is minor.
5. **The third period is used up.** 31 candidates were checked on it and 16 passed the threshold (PF ≥ 1.2). A
   4.5-month period with one market regime suited breakout systems and does not tell filters from no filters. It
   cannot be used as evidence again; from here only a forward test counts.
6. **Multiple testing.** The four strategies were chosen from about 3 000 tested variants. Part of the result is
   hindsight selection; the control without filters (PF 1.2–1.3) is closer to an honest estimate than PF 1.5–1.7.
7. **Position size.** Drawdowns in the tables are at 1 % risk per trade with up to 20 correlated positions per
   strategy. That is far too much for trading; the research portfolio used about 0.06 % of capital per trade per
   strategy. About 1,350 trades a year across the four strategies cannot be run by hand.
8. **The execution model is simplified.** Fixed slippage, unknown high/low order inside a bar (stop wins), no
   intraday drawdown.

## License

[BYTtrade Source-Available License 1.3 (trend-4h edition)](LICENSE), the same terms as bytx. In plain words:

- **You may:** run it on your own computers and servers, trade with it, add your own strategy specs, configs and
  your own code next to it.
- **You may not:** modify the code of this project or of bytx; disable or replace the broker attribution or the
  integrity checks, including through settings and environment variables (section 3(g)); distribute modified
  versions; offer it to third parties as a service.

The LICENSE text is what counts; this summary does not replace it. Dependencies and their licenses: [NOTICE](NOTICE).

## BYTtrade

trend-4h is free and made by BYTtrade. On [byttrade.com](https://byttrade.com/?utm_source=github&utm_medium=free_tool&utm_campaign=trend-4h&utm_content=readme_en) there are more strategies, grid bots,
more indicators and a deeper history for backtests, with nothing to code by hand, and a large community. That is
free as well.
