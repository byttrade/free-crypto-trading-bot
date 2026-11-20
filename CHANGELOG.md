# Changelog

## 1.0.0

First public release.

### What is in it

- Four 4h trend-following strategies for crypto perpetual futures (`st4h_ema50_1d`, `kc2_4h_adx1d`,
  `zmid1d_cloud4h`, `bbw1d_lr4h`) with an open backtest: `fetch` downloads public Binance data, `report` and `run`
  recompute the published tables.
- An executor for 11 exchanges through the bytx SDK: one position per coin, a stop-loss on the exchange for every
  position, no re-sending of orders, state and journal in local files, reconciliation after a restart.
- A web panel (English and Russian, light and dark, phone-friendly): overview, strategy settings, exchange keys,
  agent tokens, journal. The panel opens in English; Russian is chosen with the button.
- Access for your own AI agents: tokens with `read` or `control` rights, an HTTP API under `/api/v1`, and an MCP
  server (`mcp` command). Tokens never reach exchange keys and never touch a real account.
- Installation from PyPI (`pip install free-crypto-trading-bot`, `uvx`), as a container image
  (`ghcr.io/byttrade/free-crypto-trading-bot`) or from source. Console commands `free-crypto-trading-bot` and
  `trend4h`.
- The strategy specifications ship inside the package; `run` takes a built-in strategy by name or the path to your
  own file.
- Integrity checks: the installed bytx and, for an install from a wheel, this package itself are compared with
  the checksums of the released files; broker attribution must be intact. A failed check stops new orders and
  leaves exchange stops in place.
- bytx is updated automatically inside its allowed range; a newer release of this package is only reported.

### Notes

- Status: research. The numbers are a backtest; no real-money track record exists.
- The settings, state, keys and downloaded data live in the folder the command is run from, never inside the
  installed package.
