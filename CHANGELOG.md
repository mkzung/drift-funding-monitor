# Changelog

All notable changes to `drift-funding-monitor` are documented here.
Format roughly follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versions follow [SemVer](https://semver.org/).

## [0.2.0] — 2026-05-25

Post-audit truthfulness release. Honest scope on the README, correct
funding-interval handling, and rotation-aware drawdown gate. No new
features — every change is either a correctness fix or a label fix.

### BREAKING

- **`SignalThresholds` and `BacktestConfig` are now `frozen=True` dataclasses.**
  Code that mutated fields post-construction (`th.min_spread_bps_per_hour = 2.0`)
  will raise `FrozenInstanceError`. Construct a new instance instead.
- **All four Pydantic models (`FundingRate`, `PerpMarketState`,
  `CrossVenueQuote`, `Position`) now use `extra="forbid"`.** Construction
  with unknown fields — including typos like `mark_pric=150` — raises
  `ValidationError` instead of silently dropping the field.
- **`[streamlit]` extra removed from `pyproject.toml`.** It was never
  implemented; `pip install dfm[streamlit]` previously surfaced a
  non-functional install path.
- **`live-data/` directory removed.** Dead infrastructure — there was
  no consumer or producer wired up. Resurrect with a real cron+writer
  if reintroduced.

### Fixed

- **README scope honesty (P0).** `dfm scan SOL-PERP` only fully populates
  the Hyperliquid leg today. Drift requires `anchorpy` + IDL wiring;
  Orderly/Backpack expose funding rates but not mark/index/OI in a
  single call. README now documents this explicitly, top-of-file +
  in the Live-scan section + in the What-this-is-NOT block. Honesty
  over scope; the four-venue architecture is preserved for the future
  build-out.
- **Funding-interval discovery for Orderly + Backpack (P0).** Prior
  versions hard-coded `rate / 8` for hourly normalization — wrong for
  the 1h and 4h markets each venue ships. Orderly now queries
  `/v1/public/funding_rate_history` and reads `funding_period_hours`
  (or infers from successive timestamps); Backpack queries
  `/api/v1/markets` and converts `fundingInterval` ms → hours. Both
  cache the result per `(client, symbol)`. Fallback to 8h with explicit
  WARNING log if the API returns an unexpected shape.
- **Backtest drawdown gate now includes basis PnL (P1).** Pre-fix the
  gate only checked `cumulative_funding_pnl / notional < -max_drawdown_pct`,
  meaning a position bleeding 5% on basis with flat-positive funding
  would never trip the circuit-breaker. Fixed to compute combined
  `(funding + basis) / notional` each iteration. Regression test
  `test_drawdown_gate_fires_on_basis_loss_not_just_funding`.
- **Signal layer now requires closing-side depth (P1).** Pre-fix
  `evaluate()` only checked entry-side depth (`min(high.bid, low.ask)`)
  — a market with deep entry bids but thin exit asks would emit
  signals that no operator could safely close. Fixed to require BOTH
  entry and closing depths clear `min_depth_usd`. Evidence dict now
  surfaces `entry_depth_usd` and `closing_depth_usd` separately.
- **`FundingFlipRisk` "flip in -2.5h" gibberish (P1).** When the
  position has already flipped (`current_spread <= 0`), the headline
  now reads `"Spread already flipped; carry has been negative for {N}h"`
  with `triggered=True, severity=1.0` and `evidence.already_flipped=True`,
  instead of computing a meaningless negative hours-to-zero.
- **Venue HTTP clients now log on failure + retry on 429/5xx (P1).**
  All three live clients (Hyperliquid, Orderly, Backpack) route through
  `_get_with_retry` / `_post_with_retry` which emit `logger.warning`
  on each retry and on terminal failure (status code + URL), and back
  off exponentially (base 0.5s) up to 3 retries on 429 / 5xx /
  TimeoutException / ConnectError. Non-retryable 4xx → single WARNING,
  immediate `None`. Silent-failure paths are gone.
- **`dfm scan` now prints pre-open risks alongside (or instead of)
  the signal (P1).** Pre-fix, `result.pre_open_risks` (DataStaleness,
  ConcentrationRisk on both legs, BasisBlowoutRisk on both legs) was
  computed in `runner.scan_symbol` but discarded by the CLI when
  `signal is None` — which is precisely the case where the operator
  needs the diagnostic. New output: `## Pre-open risks` section with
  per-detector `[TRIG]` / `[ok ]` flag + headline.
- **Backtest summary now carries an explicit synthetic disclaimer (P1).**
  Markdown + HTML outputs include "synthetic sine-wave fixture; not
  a backtest of real markets" so a reader scanning the demo output
  doesn't mistake the deterministic "100% win rate" for a real-strategy
  metric.
- **Parser regression tests for venue HTTP clients (P1).** New file
  `tests/test_venue_parsers.py` covers garbage-in / None-out for HL
  (scalar, short-list, zero markPx), Orderly (missing fields, list
  response, null data), Backpack (list of strings, empty list, dict
  instead of list). Also locks the per-symbol funding-interval cache
  behavior.

### Added

- **`CHANGELOG.md`** (this file).
- **`CITATION.cff`** — academic-citation metadata for the repo,
  attributing Max Gorbuk as author.

### Removed

- `[streamlit]` optional-dependency group.
- `live-data/` directory (was empty with a `.gitkeep`).


## [0.1.2] — 2026-05-25

Version-tag hygiene release. The previously-cut `v0.1.1` git tag
pointed at a commit whose code already had four downstream fixes
(accrual bug, venue rotation, Round-4 basis, Round-5 parser
hardening) but still self-reported as `version = "0.1.1"`. This
release bumps `pyproject.toml` to `0.1.2` so HEAD and tag versions
match before the v0.2.0 work begins.

### Fixed

- `pyproject.toml` version bumped from `0.1.1` to `0.1.2` to reflect
  the four post-tag correctness fixes shipped between `v0.1.1` and
  HEAD.


## [0.1.1] — 2026-05-23 (approximate)

First post-audit pass.

### Fixed

- **Funding-accrual over-accrual bug.** Per-step accrual was measuring
  `Δt` from `opened_at` on every iteration instead of from the last
  accrual, inflating PnL by roughly `(n+1)/2`. Now sums disjoint
  `Δt` intervals; regression test pins the closed-form expected value.
- **Venue rotation blindness in accrual** (round-3). When the same
  two venues swap high/low between samples while a position is open,
  the carry direction inverts. Pre-fix `current_spread_hourly` was
  always `quote.high - quote.low ≥ 0`, so a now-paying position kept
  "earning" forever.
- **Basis PnL also blind to venue rotation** (round-4). `quote.high_venue.mark_price`
  was used unconditionally for the short leg; rotation silently
  sign-flipped basis PnL.
- **`LiquidityImbalance` venue rotation** (round-4). Same hazard for
  the depth check.
- **`Position.is_delta_neutral` was a `@property` with an unreachable
  default parameter.** Now a plain method.
- **Backpack URL was kebab-case** (round-2) — corrected to camelCase
  `/api/v1/fundingRates` per docs.backpack.exchange.
- **Venue parser hardening** (round-5). Defensive type checks on every
  payload — non-dict/non-list/None responses yield `None` rather
  than crashing. Hyperliquid zero `markPx` is now rejected (was
  silently coerced to 1.0). `ConcentrationRisk` no longer reports
  "+0% balanced" when `open_interest_long == open_interest_short == 0`
  (HL sentinel for "unknown") — emits an explicit "data unavailable"
  result.


## [0.1.0] — 2026-05-22

Initial release. Cross-venue funding-rate arb monitor: state models,
signal evaluator, 6 risk detectors, replay backtester, JSON / Markdown /
HTML reporting, four venue-client stubs (Drift / Hyperliquid / Orderly
/ Backpack), `FakeVenueClient` for hermetic CI, `dfm` CLI.
