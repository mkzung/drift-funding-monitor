# drift-funding-monitor

[![CI](https://github.com/mkzung/drift-funding-monitor/actions/workflows/ci.yml/badge.svg)](https://github.com/mkzung/drift-funding-monitor/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

**Cross-venue funding-rate arbitrage monitor — Drift Protocol (Solana) + Hyperliquid + Orderly + Backpack.**

Companion to [`fundarb`](https://github.com/mkzung/fundarb): adds Drift Protocol (Solana's leading perp DEX) to the cross-venue arb universe and ships a backtest framework + 6 risk detectors for already-open positions.

> **Live-scan scope (v0.2.0):** only **Hyperliquid** is fully wired to a live data source today. Drift, Orderly, and Backpack are scaffolded — symbol normalization, funding-interval discovery, and HTTP plumbing are in place, but the mark/index/depth surface for Drift requires an `anchorpy` + Drift IDL integration not yet shipped, and the Orderly/Backpack funding endpoints don't expose mark/index/OI in a single call. `dfm scan SOL-PERP` will return only the Hyperliquid leg; the other three clients return `None` (or partial state with sentinel `mark=index=1.0` for Orderly/Backpack) and are documented as such. Honesty over scope.

---

## What it does

Given live (or backtest-replayed) perp-market state on each venue, the monitor:

1. **Signals.** For each symbol, picks the venue with the highest funding rate (short side) and lowest (long side). Returns an `ArbSignal` if the spread is wide enough to clear taker-fee breakeven, depth on both sides supports the recommended size, and price dispersion is below the staleness gate.

2. **Risks.** For an already-open position, evaluates 6 pure-function risk detectors:

| # | Detector | What it answers |
|---|---|---|
| 1 | **FundingFlipRisk** | How many hours until the spread inverts at current decay rate? |
| 2 | **LiquidityImbalance** | Is exit-side depth still sufficient to close cleanly? |
| 3 | **DataStaleness** | Is either venue's data feed lagging beyond threshold? |
| 4 | **ConcentrationRisk** | Is OI heavily one-sided (funding regime change risk)? |
| 5 | **BasisBlowoutRisk** | Has mark/index basis crossed a stress threshold? |
| 6 | **MaxDrawdownGate** | Has realized PnL crossed a circuit-breaker level? |

3. **Backtest.** Replay a chronological stream of `CrossVenueQuote` samples to simulate the strategy's PnL: opens when signal fires, accrues funding, closes when spread converges / drawdown trips / max-holding hit. Output: per-trade PnL, win rate, Sharpe-proxy.

---

## Architecture

```
src/dfm/
├── state.py          # FundingRate, PerpMarketState, CrossVenueQuote, Position
├── venues.py         # Venue ABC + Drift, Hyperliquid, Orderly, Backpack clients
│                       (+ FakeVenueClient for hermetic tests)
├── signals.py        # build_quote, evaluate → ArbSignal
├── risk.py           # 6 risk detectors
├── backtest.py       # run_backtest → BacktestResult
├── synthetic.py      # deterministic test data (no HTTP)
├── report.py         # JSON / Markdown / HTML renderers
├── runner.py         # scan_symbol, evaluate_position_risks
└── __main__.py       # CLI: dfm demo | scan | backtest
```

Tests in `tests/` are hermetic — `FakeVenueClient` satisfies the `VenueClient` ABC so no live HTTP is needed in CI.

---

## Install

```bash
pip install -e ".[dev]"
```

Python ≥ 3.10. No optional UI extras — the package is CLI-only and emits Markdown / JSON / standalone HTML for inspection.

---

## Quickstart

### Synthetic demo (no HTTP needed)

```bash
dfm demo                                  # markdown to stdout
dfm demo --json                           # JSON
dfm demo --html report.html               # standalone HTML
dfm demo --high-rate 0.0008 --low-rate 0.0001
```

### Backtest

```bash
dfm backtest --samples 168 --amplitude 0.0003   # 168 samples, ~24-hour sine
dfm backtest --html backtest.html
```

### Live scan (requires public HTTP access)

```bash
dfm scan SOL-PERP
```

Only **Hyperliquid** returns a live, fully-populated `PerpMarketState` today.
Drift returns `None` (stub — needs `anchorpy` + IDL wiring). Orderly and
Backpack return funding-rate-only state with sentinel `mark=index=1.0` and
`last_update_lag_s=999`, which the signal layer correctly down-weights via
the freshness term. See the v0.2.0 entry in `CHANGELOG.md` for the
remediation plan.

### Programmatic

```python
from dfm import evaluate, make_cross_venue_quote, run_backtest, BacktestConfig
from dfm.synthetic import make_oscillating_spread

# One-shot signal
q = make_cross_venue_quote(high_hourly_rate=0.0005, low_hourly_rate=0.0001)
print(evaluate(q).reason if evaluate(q) else "no signal")

# Backtest over a synthetic spread oscillation
result = run_backtest(make_oscillating_spread(n_samples=96, amplitude_hourly=0.0005))
print(f"Trades: {result.n_trades}  PnL: ${result.total_pnl_usd:,.0f}  "
      f"Win rate: {result.win_rate:.0%}  Sharpe-proxy: {result.sharpe_proxy:.2f}")
```

---

## What this is NOT

- ❌ **An auto-executor.** Signals and risk detectors are reports; the package never signs or submits orders.
- ❌ **A backtester with realistic slippage modeling.** Fees are flat taker bps; slippage beyond mark crossing is not modeled. Calibrate against your own exchange-specific cost model.
- ❌ **A backtest of real markets.** The `dfm backtest` quickstart uses a **synthetic sine-wave fixture** so the engine produces deterministic round-trips for CI; the resulting "100% win rate" on the default demo is an engine sanity check, not a strategy backtest. Replace `make_oscillating_spread` with real historical CrossVenueQuote samples for any meaningful PnL claim.
- ❌ **Production financial advice.** Backtest PnL on synthetic data is a sanity-check, not an expected-return estimate.
- ❌ **A live monitor for all four venues.** Only Hyperliquid is fully wired today; see the live-scan section above.

---

## Companion repos

- 🟦 [`fundarb`](https://github.com/mkzung/fundarb) — original cross-venue funding arb CLI (Hyperliquid + Orderly + Backpack).
- 🟧 [`morpho-vault-counterfactuals`](https://github.com/mkzung/morpho-vault-counterfactuals) — risk framework for Morpho MetaMorpho on Ethereum.
- 🟦 [`kamino-vault-counterfactuals`](https://github.com/mkzung/kamino-vault-counterfactuals) — same six-detector design for Kamino Lend on Solana.
- 🟨 [`ethbtc-suspicious-patterns`](https://github.com/mkzung/ethbtc-suspicious-patterns) — six-detector forensics on ETH/BTC microstructure.

Shared design language: **pure functions, fractional impairment metrics, hermetic CI, MIT.**

---

## License

[MIT](LICENSE).
