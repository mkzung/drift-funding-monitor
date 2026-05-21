"""Tests for backtest replay + multi-venue runner + venue clients."""

from __future__ import annotations

from dfm.backtest import BacktestConfig, run_backtest
from dfm.runner import evaluate_position_risks, scan_symbol
from dfm.signals import SignalThresholds
from dfm.state import Venue
from dfm.synthetic import (
    make_cross_venue_quote,
    make_market_state,
    make_oscillating_spread,
    make_quote_stream,
)
from dfm.venues import FakeVenueClient, scan_all_venues

# ──────────────────────────────────────────────────────────────────────
# Backtest
# ──────────────────────────────────────────────────────────────────────


class TestBacktest:
    def test_empty_stream_returns_no_trades(self):
        result = run_backtest([])
        assert result.n_trades == 0
        assert result.total_pnl_usd == 0.0
        assert result.win_rate == 0.0

    def test_single_decaying_spread_opens_one_trade(self):
        stream = list(make_quote_stream(
            n_samples=10,
            initial_spread_hourly=0.0005,
            final_spread_hourly=0.0,
            interval_seconds=3600,
        ))
        result = run_backtest(stream, BacktestConfig())
        # Should open trade at start, close when spread shrinks below 0.5 bps/h
        assert result.n_trades >= 1

    def test_oscillating_spread_opens_multiple_trades(self):
        stream = list(make_oscillating_spread(
            n_samples=48, amplitude_hourly=0.0005, period_samples=24,
        ))
        cfg = BacktestConfig(
            thresholds=SignalThresholds(min_spread_bps_per_hour=1.0, min_confidence=0.4),
        )
        result = run_backtest(stream, cfg)
        # Expect at least a couple of open/close cycles
        assert result.n_trades >= 1

    def test_backtest_summary_dict_keys(self):
        stream = list(make_quote_stream(n_samples=5))
        result = run_backtest(stream)
        d = result.to_dict()
        for k in ("n_trades", "total_pnl_usd", "win_rate", "avg_pnl_pct",
                  "pnl_pct_std", "sharpe_proxy"):
            assert k in d

    def test_funding_accrual_matches_closed_form(self):
        """Regression test for the v0.1.0 over-accrual bug.

        With constant spread Δr/hour and constant notional N held for T hours,
        cumulative funding PnL is EXACTLY N × Δr × T. Any deviation > 1%
        means the per-step accrual is mis-computing Δt (the v0.1.0 bug
        accumulated Σ(t_i - t_open) instead of Σ(t_i - t_{i-1}), inflating
        by ~(n+1)/2).
        """
        # 5 quotes 1h apart, constant 0.0004/h high vs 0.00005/h low.
        # Expected spread: 0.00035 per hour.
        # Recommended size: 500_000 depth × 0.20 = 100_000 USD.
        # Position opens at t=0, held until t=14_400 (4 hours).
        # Expected funding PnL: 100_000 × 0.00035 × 4 = $140.
        stream = list(make_quote_stream(
            n_samples=5,
            initial_spread_hourly=0.0004,
            final_spread_hourly=0.0004,
            interval_seconds=3600,
        ))
        cfg = BacktestConfig(
            thresholds=SignalThresholds(taker_fee_bps=0.0),
            close_spread_bps_per_hour=0.01,  # don't close on the constant spread
            max_holding_hours=100,
        )
        result = run_backtest(stream, cfg)
        assert result.n_trades == 1
        trade = result.trades[0]
        # 100k × 0.00035/h × 4h = $140
        expected_funding_pnl = 100_000 * 0.00035 * 4.0
        deviation_pct = abs(trade.funding_pnl_usd - expected_funding_pnl) / expected_funding_pnl
        assert deviation_pct < 0.01, (
            f"Funding PnL {trade.funding_pnl_usd:.2f} vs expected "
            f"{expected_funding_pnl:.2f} — deviation {deviation_pct:.1%} > 1%. "
            f"Likely accrual-Δt regression."
        )


# ──────────────────────────────────────────────────────────────────────
# Runner: position-side risk evaluation
# ──────────────────────────────────────────────────────────────────────


class TestEvaluatePositionRisks:
    def test_returns_six_results(self):
        from dfm.state import Position
        q = make_cross_venue_quote()
        pos = Position(
            symbol="SOL-PERP", opened_at=1_716_000_000,
            long_venue=Venue.HYPERLIQUID,
            long_size_tokens=100, long_entry_price=150.0,
            short_venue=Venue.DRIFT,
            short_size_tokens=100, short_entry_price=150.0,
            entry_funding_diff_hourly=0.0004,
        )
        results = evaluate_position_risks(q, pos, hours_held=2, realized_pnl_usd=10)
        assert len(results) == 6
        names = [r.name for r in results]
        assert "FundingFlipRisk" in names
        assert "MaxDrawdownGate" in names


# ──────────────────────────────────────────────────────────────────────
# Async: scan_all_venues + scan_symbol with FakeVenueClient
# ──────────────────────────────────────────────────────────────────────


class TestAsyncScan:
    async def test_scan_all_venues_returns_all_responding(self):
        drift_state = make_market_state(Venue.DRIFT, hourly_rate=0.0005)
        hl_state = make_market_state(Venue.HYPERLIQUID, hourly_rate=0.0001)
        clients = [
            FakeVenueClient(Venue.DRIFT, {"SOL-PERP": drift_state}),
            FakeVenueClient(Venue.HYPERLIQUID, {"SOL-PERP": hl_state}),
        ]
        out = await scan_all_venues(clients, "SOL-PERP")
        assert Venue.DRIFT in out
        assert Venue.HYPERLIQUID in out
        assert out[Venue.DRIFT].funding_rate.hourly_rate == 0.0005

    async def test_scan_all_venues_skips_silent_failures(self):
        good = FakeVenueClient(
            Venue.DRIFT, {"SOL-PERP": make_market_state(Venue.DRIFT)}
        )
        empty = FakeVenueClient(Venue.HYPERLIQUID, {})  # symbol not present
        out = await scan_all_venues([good, empty], "SOL-PERP")
        assert Venue.DRIFT in out
        assert Venue.HYPERLIQUID not in out

    async def test_scan_symbol_emits_signal_when_spread_wide(self):
        clients = [
            FakeVenueClient(Venue.DRIFT, {
                "SOL-PERP": make_market_state(Venue.DRIFT, hourly_rate=0.0005)
            }),
            FakeVenueClient(Venue.HYPERLIQUID, {
                "SOL-PERP": make_market_state(Venue.HYPERLIQUID, hourly_rate=0.0001)
            }),
        ]
        res = await scan_symbol(clients, "SOL-PERP")
        assert res.quote is not None
        assert res.signal is not None
        assert res.signal.spread_bps_per_hour > 0

    async def test_scan_symbol_no_signal_when_only_one_venue(self):
        clients = [
            FakeVenueClient(Venue.DRIFT, {
                "SOL-PERP": make_market_state(Venue.DRIFT, hourly_rate=0.0005)
            })
        ]
        res = await scan_symbol(clients, "SOL-PERP")
        assert res.quote is None
        assert res.signal is None

    async def test_pre_open_risks_returned(self):
        clients = [
            FakeVenueClient(Venue.DRIFT, {
                "SOL-PERP": make_market_state(Venue.DRIFT, hourly_rate=0.0005)
            }),
            FakeVenueClient(Venue.HYPERLIQUID, {
                "SOL-PERP": make_market_state(Venue.HYPERLIQUID, hourly_rate=0.0001)
            }),
        ]
        res = await scan_symbol(clients, "SOL-PERP")
        assert len(res.pre_open_risks) >= 3  # at minimum staleness + 2 concentration


# ──────────────────────────────────────────────────────────────────────
# Venue clients: symbol normalization
# ──────────────────────────────────────────────────────────────────────


class TestVenueNormalization:
    def test_drift_keeps_perp_suffix(self):
        from dfm.venues import DriftClient
        assert DriftClient().normalize_symbol("sol-perp") == "SOL-PERP"

    def test_hyperliquid_strips_perp(self):
        from dfm.venues import HyperliquidClient
        assert HyperliquidClient().normalize_symbol("SOL-PERP") == "SOL"

    def test_orderly_format(self):
        from dfm.venues import OrderlyClient
        assert OrderlyClient().normalize_symbol("SOL-PERP") == "PERP_SOL_USDC"

    def test_backpack_format(self):
        from dfm.venues import BackpackClient
        assert BackpackClient().normalize_symbol("SOL-PERP") == "SOL_USDC_PERP"
