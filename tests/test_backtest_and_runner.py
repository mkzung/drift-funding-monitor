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

    def test_backtest_accrual_respects_venue_rotation(self):
        """Regression test for the v0.1.1 venue-rotation bug.

        If the same two venues swap "high" and "low" between samples while
        the position stays open, the carry direction inverts and accrual
        should sign-flip. Prior to the fix, `current_spread_hourly` was
        always `quote.high - quote.low` (≥0) which kept "earning" forever
        even though the trade was now paying funding.
        """
        from dfm.state import CrossVenueQuote, FundingRate, PerpMarketState, Venue
        def st(venue, hr, ts):
            return PerpMarketState(
                venue=venue, symbol="SOL-PERP", timestamp=ts,
                mark_price=150.0, index_price=150.0,
                bid_depth_usd=500_000, ask_depth_usd=500_000,
                funding_rate=FundingRate(venue=venue, symbol="SOL-PERP", timestamp=ts, hourly_rate=hr),
            )
        # 4 quotes; venues rotate at t=2h
        q1 = CrossVenueQuote(symbol="SOL-PERP", timestamp=0,
            high_venue=st(Venue.DRIFT, 0.0005, 0), low_venue=st(Venue.HYPERLIQUID, 0.0001, 0))
        q2 = CrossVenueQuote(symbol="SOL-PERP", timestamp=3600,
            high_venue=st(Venue.DRIFT, 0.0005, 3600), low_venue=st(Venue.HYPERLIQUID, 0.0001, 3600))
        q3 = CrossVenueQuote(symbol="SOL-PERP", timestamp=7200,
            high_venue=st(Venue.HYPERLIQUID, 0.0005, 7200), low_venue=st(Venue.DRIFT, 0.0001, 7200))
        q4 = CrossVenueQuote(symbol="SOL-PERP", timestamp=10800,
            high_venue=st(Venue.HYPERLIQUID, 0.0005, 10800), low_venue=st(Venue.DRIFT, 0.0001, 10800))
        result = run_backtest([q1, q2, q3, q4], BacktestConfig(
            thresholds=SignalThresholds(taker_fee_bps=0.0),
            close_spread_bps_per_hour=-10.0,  # don't close on spread-converge
            max_holding_hours=100,
        ))
        # Position opened with +0.0004/h carry. Venues rotate at t=2h,
        # flipping carry to -0.0004/h for the remaining 1h. End-of-interval
        # accrual convention attributes 1h of +carry and 2h of -carry → -$40.
        # Pre-fix (rotation-blind), this would be +$120 (all 3 accruals
        # treated as positive carry).
        assert result.n_trades == 1
        t = result.trades[0]
        # Closed-form expected value (signal-side accrual, end-of-interval):
        #   q2 (Δt=1h, pre-rotation):  +0.0004 × notional × 1 = +$40
        #   q3 (Δt=1h, post-rotation): -0.0004 × notional × 1 = -$40
        #   q4 (Δt=1h, post-rotation, close): -0.0004 × notional × 1 = -$40
        # Total accrued at close = +40 − 40 − 40 = -$40 (notional ≈ $100k).
        # Pre-fix would have produced +$120 (all three steps positive).
        assert -45.0 < t.funding_pnl_usd < -35.0, (
            f"Venue-rotation accrual = ${t.funding_pnl_usd:.2f}; "
            f"closed-form expected ≈ -$40 (+$40 pre-rotation, -$80 post). "
            f"Pre-fix bug returned ~+$120."
        )

    def test_backtest_basis_pnl_respects_venue_rotation(self):
        """Round-4 regression test: ensure basis PnL also reads marks in
        the position's carry direction. Pre-fix, `quote.high_venue.mark_price`
        was used unconditionally for the short leg — when venues rotated,
        the wrong mark was read and basis PnL sign-flipped silently.
        """
        from dfm.state import CrossVenueQuote, FundingRate, PerpMarketState, Venue
        def st(venue, hr, mark, ts):
            return PerpMarketState(
                venue=venue, symbol="SOL-PERP", timestamp=ts,
                mark_price=mark, index_price=mark,
                bid_depth_usd=500_000, ask_depth_usd=500_000,
                funding_rate=FundingRate(venue=venue, symbol="SOL-PERP",
                                          timestamp=ts, hourly_rate=hr),
            )
        # Open at t=0: DRIFT high (150), HL low (150). After rotation at
        # t=3600s, marks also diverge — HL up to 152, DRIFT down to 148.
        # Position is short DRIFT @ 150, long HL @ 150. With short_state.mark
        # = 148 and long_state.mark = 152:
        #   long PnL  = (152 - 150) × size_long  = +$2 × size
        #   short PnL = (150 - 148) × size_short = +$2 × size
        # Both legs profitable → basis PnL strongly POSITIVE.
        # Pre-fix would have read short_state.mark = quote.high_venue.mark = 152
        # (now HL, the long leg!) and long_state.mark = quote.low_venue.mark = 148
        # → both PnL negative → sign-flipped result.
        q1 = CrossVenueQuote(symbol="SOL-PERP", timestamp=0,
            high_venue=st(Venue.DRIFT, 0.0005, 150.0, 0),
            low_venue=st(Venue.HYPERLIQUID, 0.0001, 150.0, 0))
        # Rotation + price divergence
        q2 = CrossVenueQuote(symbol="SOL-PERP", timestamp=3600,
            high_venue=st(Venue.HYPERLIQUID, -0.0005, 152.0, 3600),
            low_venue=st(Venue.DRIFT, -0.0001, 148.0, 3600))
        # Force close via spread-flip
        result = run_backtest([q1, q2], BacktestConfig(
            thresholds=SignalThresholds(taker_fee_bps=0.0),
            close_spread_bps_per_hour=10.0,  # any small positive spread closes
            max_holding_hours=100,
        ))
        assert result.n_trades == 1
        t = result.trades[0]
        # Strong positive basis PnL proves marks were read in position direction.
        # Pre-fix would have produced strong NEGATIVE basis PnL.
        assert t.basis_pnl_usd > 100.0, (
            f"Basis PnL = ${t.basis_pnl_usd:.2f}; expected strongly POSITIVE "
            f"(both legs profit after rotation+divergence). "
            f"Pre-fix bug would have returned negative."
        )

    def test_drawdown_gate_fires_on_basis_loss_not_just_funding(self):
        """v0.2.0 fix: max-drawdown gate must include basis PnL, not just
        funding PnL. Pre-fix, a position bleeding basis but flat-positive
        on funding would never trip the circuit-breaker.

        Construct: open at t=0 with positive carry; at t=1h, marks diverge
        so that long-leg loses heavily while funding accrual is small.
        Combined PnL < -max_dd_pct → close with reason=max_drawdown.
        """
        from dfm.state import CrossVenueQuote, FundingRate, PerpMarketState, Venue

        def st(venue, hr, mark, ts):
            return PerpMarketState(
                venue=venue, symbol="SOL-PERP", timestamp=ts,
                mark_price=mark, index_price=mark,
                bid_depth_usd=500_000, ask_depth_usd=500_000,
                funding_rate=FundingRate(
                    venue=venue, symbol="SOL-PERP", timestamp=ts, hourly_rate=hr,
                ),
            )

        # t=0: signal opens (positive spread). Marks at 150.
        q1 = CrossVenueQuote(
            symbol="SOL-PERP", timestamp=0,
            high_venue=st(Venue.DRIFT, 0.0005, 150.0, 0),
            low_venue=st(Venue.HYPERLIQUID, 0.0001, 150.0, 0),
        )
        # t=1h: marks DIVERGE adversely — long leg crashes, short leg holds.
        # Long is on HL (low venue at open). HL price drops 5% → long PnL
        # = (142.5 - 150) × size_long ≈ -5% of notional. Funding accrual
        # is +0.0004/h × 1h ≈ +0.04% of notional. Combined ≈ -5% → trip.
        q2 = CrossVenueQuote(
            symbol="SOL-PERP", timestamp=3600,
            high_venue=st(Venue.DRIFT, 0.0005, 150.0, 3600),
            low_venue=st(Venue.HYPERLIQUID, 0.0001, 142.5, 3600),
        )
        cfg = BacktestConfig(
            thresholds=SignalThresholds(taker_fee_bps=0.0),
            close_spread_bps_per_hour=-10.0,  # don't close on spread
            max_holding_hours=100,
            max_drawdown_pct=1.0,
        )
        result = run_backtest([q1, q2], cfg)
        assert result.n_trades == 1
        t = result.trades[0]
        assert t.reason_close == "max_drawdown", (
            f"Expected max_drawdown close from basis loss; got reason={t.reason_close} "
            f"(funding=${t.funding_pnl_usd:.2f}, basis=${t.basis_pnl_usd:.2f}). "
            f"Pre-fix the gate ignored basis and would NOT have fired."
        )
        assert t.basis_pnl_usd < -100, "expected significant basis loss"

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
    def test_returns_eight_results_per_venue_detectors_run_on_both_legs(self):
        # 8 = 4 position-level (FundingFlipRisk, LiquidityImbalance,
        # DataStaleness, MaxDrawdownGate) + 2 per-venue × 2 legs
        # (ConcentrationRisk + BasisBlowoutRisk on both high and low).
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
        assert len(results) == 8
        names = [r.name for r in results]
        assert "FundingFlipRisk" in names
        assert "MaxDrawdownGate" in names
        # Per-venue detectors should appear exactly twice
        assert names.count("ConcentrationRisk") == 2
        assert names.count("BasisBlowoutRisk") == 2


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
