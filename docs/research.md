# Research: test conditions, results and limitations

[Русская версия](research.ru.md) · [Back to the README](../README.md)

> The numbers on this page are a backtest. Backtest and paper results do not
> guarantee future returns.

## Test conditions

| Parameter | Value |
|---|---|
| Market | Binance USDT-M perpetual futures |
| Coins (20) | BTC ETH SOL XRP BNB DOGE ADA LINK AVAX SUI LTC DOT NEAR AAVE TRX BCH UNI ARB OP INJ |
| Candles | 4h and 1d as published by the exchange (not resampled), from 2023-01-01 |
| Sides | long and short, one position per coin per strategy |
| Selection (IS) | 2023-01-01 … 2025-06-30: rules were searched here |
| Verification (OOS) | 2025-07-01 … 2026-06-30: rules frozen. Not a blind test: results on this period also took part in choosing the four strategies |
| Earlier period | 2020-07-01 … 2022-12-31: not seen by the choice; run once in November 2026 by an independent re-implementation, two strategies of four |
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
half-year (`../results/by_half_year.csv`). 15–18 of 20 coins are positive in each period. This does not hold
before 2023: see the earlier period below.

An equal mix of the four: Sharpe 1.42 on selection and 1.78 on verification. At 0.25 % risk per trade per strategy
the mix has 52–66 % annual volatility and drawdowns of 28 % and 46 % of capital. That is a lot.

### Earlier period 2020-07 … 2022-12 (recorded, `../results/recorded/pre_2023.csv`)

The four strategies were chosen on 2023-01 … 2026-06. In November 2026 an independent re-implementation (its own
indicator and exit code, 4h and 1d bars built from 1h candles, the same fee, slippage and funding) ran
`st4h_ema50_1d`, `kc2_4h_adx1d` and their controls on 2020-07-01 … 2022-12-31. `zmid1d_cloud4h` and `bbw1d_lr4h`
were not re-tested. Coins listed later than 2020 take part from their listing date.

| What | Trades | PF | Mean R | Σ R | Long PF | Short PF |
|---|---|---|---|---|---|---|
| `st4h_ema50_1d` | 665 | 1.41 | +0.261 | +173.4 | 1.20 | 1.60 |
| C2: the same without the EMA50 regime | 942 | 0.91 | −0.062 | −58.8 | 0.86 | 0.95 |
| C4: the same against the regime | 689 | 0.87 | −0.088 | −60.9 | 0.93 | 0.82 |
| `kc2_4h_adx1d` | 504 | 0.99 | −0.009 | −4.5 | 1.30 | 0.58 |
| C1: the same without the ADX filter | 1 168 | 1.21 | +0.136 | +159.3 | 1.35 | 1.03 |
| C3: the same with the filter inverted (ADX ≥ 20) | 919 | 1.28 | +0.182 | +167.1 | 1.39 | 1.15 |

Σ R by calendar year, the same re-implementation (2026 is the first half only):

| Spec | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 H1 |
|---|---|---|---|---|---|---|---|
| `st4h_ema50_1d` | +16.8 | +146.0 | +19.8 | +20.0 | +71.7 | +155.4 | +59.7 |
| `kc2_4h_adx1d` | +7.5 | +12.2 | −21.9 | +97.5 | +100.9 | +92.7 | +45.3 |

What this shows:

- `kc2_4h_adx1d` did not hold up: PF 0.99, a losing 2022, shorts at PF 0.58. Its ADX filter, which helped on
  2023–2026, hurt here: the entry without the filter and with the filter inverted both did better.
- `st4h_ema50_1d` held up (PF 1.41), with 2021 giving most of the result. Here its daily filter mattered: without
  it and against it the entry lost.
- At the research position size (0.056 % of capital at risk per trade per strategy) the yearly results above are
  from −1.2 % to +8.7 % of capital per strategy.
- Random entries with the same exits, 30 runs: on 2020-07 … 2022-12 random shorts PF 0.96 and random longs 1.08;
  on the verification year random shorts PF 1.36 (mean +0.234 R) and random longs 0.80. In the verification year a
  short with these exits made money without any signal.

On 2023–2026 the re-implementation is close to this code but not identical: PF 1.32 / 1.56 for `st4h_ema50_1d` and
1.54 / 1.79 for `kc2_4h_adx1d` on selection / verification, against 1.33 / 1.59 and 1.53 / 1.74 here.

### Parameter stability (recorded)

Every numeric parameter was shifted by ±20 % with the others fixed. PF stayed at 1.15 or higher for all four
strategies on both periods. The weakest point is the Supertrend multiplier 2.4 instead of 3.0: PF 1.21 / 1.23.

### Control without filters (recorded, `../results/recorded/controls.csv`)

| Control | PF selection | PF verification | Third period: PF / Σ R / trades |
|---|---|---|---|
| C1: `kc2_4h_adx1d` without the ADX filter | 1.19 | 1.30 | 1.35 / +66 / 281 |
| C2: `st4h_ema50_1d` without the EMA50 regime | 1.21 | 1.24 | 1.29 / +42 / 218 |
| C3: `kc2_4h_adx1d` with the filter inverted (ADX ≥ 20) | 1.09 | 1.15 | 1.27 / +36 / 204 |
| C4: `st4h_ema50_1d` against the regime | 1.24 | 0.88 | 1.29 / +34 / 167 |

The mechanics "breakout or flip on 4h + wide trail" are profitable without filters on all three periods. The
filters lift PF from 1.2–1.3 to 1.3–1.7 on selection and verification, but the third period did not confirm their
contribution: the controls passed there too, even with an inverted filter. On 2020–2022 the picture is mixed: the
Keltner entry without a filter was profitable (PF 1.21) and the Supertrend entry without a filter lost (PF 0.91).

## Reproducibility

| What | Status |
|---|---|
| IS and OOS metrics of the four strategies: `metrics_by_period.csv`, `by_half_year.csv`, `by_side.csv` | **recomputed by this code** |
| Match with the research engine | trade for trade: 4 strategies × 2 periods, 4 742 trades, every entry and exit time and side identical, R of each trade equal to 1e-13, Σ R and PF identical |
| Downloaded data | `fetch` output is byte-identical to the research data set for all 20 coins (4h, 1d, funding) |
| Third period, parameter stability, controls, other perpetuals, limit entries | **taken from the research report**, not recomputed here (`../results/recorded/`) |
| Earlier period 2020-07 … 2022-12, random entries | **taken from the November 2026 re-test**, not recomputed here: `fetch` downloads history from 2023-01-01 (`../results/recorded/pre_2023.csv`) |

`tests/expected.json` holds the IS/OOS totals; `pytest` compares `report` against them when `data/` exists.
Exchanges occasionally revise history, so tiny differences are possible if you download much later.

## Honest limitations

1. **This is one idea, not four independent sources.** Daily results of the three breakout strategies correlate at
   0.65–0.80 with each other, `st4h_ema50_1d` at 0.47–0.49 with them. Drawdowns come together.
2. **It does not work outside the twenty coins.** The same specs on 639 other Binance perpetuals (filtered by
   turnover and age) gave PF 1.04–1.07 for three strategies on the verification year and 1.34 for `bbw1d_lr4h`,
   which failed the half-year criterion on selection. The control without a filter: PF 1.00. All the profit outside
   the twenty coins is in shorts (PF 1.32–2.03); longs lose (PF 0.60–0.86; `bbw1d_lr4h` 1.07–1.09). At a turnover
   of $20–100M a day the verification result is zero (PF 0.99–1.02). Table: `../results/recorded/wide_universe.csv`.
   The result on 20 coins is largely a property of this set of large coins, not general market mechanics.
3. **Shorts are stronger than longs on the twenty coins too.** On the verification year short PF is 1.74–2.65
   against 1.17–1.34 for longs; `st4h_ema50_1d` longs are about zero on selection (PF 0.99). The 2025–2026 market
   was falling, and random shorts with the same exits made PF 1.36 in that year. On 2020–2022 the sides differ by
   strategy: `st4h_ema50_1d` shorts PF 1.60 and longs 1.20, `kc2_4h_adx1d` shorts 0.58 and longs 1.30.
   Table: `../results/by_side.csv`.
4. **A limit entry makes it worse.** 64 variants of a limit entry instead of a market one were tested: none passed,
   all are below the market entry on the verification year. The reason is adverse selection: trades where the limit
   would fill average 0.32 R, the missed ones 0.72 R. The ceiling of the saving is about 0.011 R per trade. Treating
   the take-profit as a limit order adds 0.5–0.6 pp per year, which is minor.
5. **The third period is used up.** 31 candidates were checked on it and 16 passed the threshold (PF ≥ 1.2). A
   4.5-month period with one market regime suited breakout systems and does not tell filters from no filters. It
   cannot be used as evidence again; from here only a forward test counts.
6. **Multiple testing, and no blind year.** The four strategies were chosen from about 3 000 tested variants, and
   results on the verification year took part in the choice, so PF 1.5–1.7 on it is not an out-of-sample number.
   Part of the result is hindsight selection. The control without filters gave PF 1.2–1.3 on 2023–2026 and
   0.91–1.21 on 2020–2022.
7. **Position size.** Drawdowns in the tables are at 1 % risk per trade with up to 20 correlated positions per
   strategy. That is far too much for trading; the research portfolio used about 0.06 % of capital per trade per
   strategy. About 1,350 trades a year across the four strategies cannot be run by hand.
8. **The execution model is simplified.** Fixed slippage, unknown high/low order inside a bar (stop wins), no
   intraday drawdown.
9. **Earlier history is weaker.** On 2020-07 … 2022-12, which the choice did not see, `st4h_ema50_1d` held
   (PF 1.41) and `kc2_4h_adx1d` did not (PF 0.99, a losing 2022). `zmid1d_cloud4h` and `bbw1d_lr4h` were not
   re-tested on it. The re-test covers one earlier market cycle and was run once.
