# results

| File | Source |
|---|---|
| `metrics_by_period.csv`, `by_half_year.csv`, `by_side.csv` | **recomputed by this code**: `python -m trend4h report` rewrites them |
| `recorded/third_period.csv` | recorded research result for 2026-07-01..2026-11-17, **not recomputed here** |
| `recorded/controls.csv` | recorded research result (entries without / with inverted daily filters), **not recomputed here** |
| `recorded/wide_universe.csv` | recorded research result (639 other perpetuals), **not recomputed here** |
| `recorded/pre_2023.csv` | recorded result of the November 2026 re-test on 2020-07-01..2022-12-31 (two strategies and their controls, independent re-implementation), **not recomputed here** |

R = result in units of risk per trade. Drawdown is in % of capital at 1 % risk per trade.

Файлы в корне папки пересчитываются командой `python -m trend4h report`. Файлы в `recorded/` — записанные
результаты исследования, этим кодом здесь не пересчитаны.
