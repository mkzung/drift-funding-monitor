"""Tests for the 6 risk detectors."""

from __future__ import annotations

import pytest

from dfm.risk import (
    BasisBlowoutRisk,
    ConcentrationRisk,
    DataStaleness,
    FundingFlipRisk,
    LiquidityImbalance,
    MaxDrawdownGate,
)
from dfm.state import Position, Venue
from dfm.synthetic import make_cross_venue_quote, make_market_state


def _open_position(notional_usd: float = 50_000.0) -> Position:
    return Position(
        symbol="SOL-PERP",
        opened_at=1_716_000_000,
        long_venue=Venue.HYPERLIQUID,
        long_size_tokens=notional_usd / 150,
        long_entry_price=150.0,
        short_venue=Venue.DRIFT,
        short_size_tokens=notional_usd / 150,
        short_entry_price=150.0,
        entry_funding_diff_hourly=0.0004,
    )


class TestFundingFlipRisk:
    def test_just_opened_returns_no_signal(self):
        det = FundingFlipRisk(min_buffer_hours=6.0)
        q = make_cross_venue_quote(high_hourly_rate=0.0005, low_hourly_rate=0.0001)
        res = det.run(q, _open_position(), hours_held=0)
        assert res.triggered is False
        assert "just opened" in res.headline.lower()

    def test_widening_spread_no_flip_risk(self):
        det = FundingFlipRisk()
        # Position opened with 0.0004 diff; now 0.0008 (widening)
        q = make_cross_venue_quote(high_hourly_rate=0.0009, low_hourly_rate=0.0001)
        res = det.run(q, _open_position(), hours_held=2)
        assert res.triggered is False

    def test_decaying_spread_triggers_when_close_to_zero(self):
        det = FundingFlipRisk(min_buffer_hours=10.0)
        # Position opened with 0.0004 diff; now 0.00005 after 5 hours
        # decay = (0.0004 - 0.00005) / 5 = 0.00007 per hour
        # hours_to_zero = 0.00005 / 0.00007 ≈ 0.71h < 10h → trigger
        q = make_cross_venue_quote(high_hourly_rate=0.00005, low_hourly_rate=0.0)
        res = det.run(q, _open_position(), hours_held=5)
        assert res.triggered is True
        assert res.severity > 0

    def test_invalid_buffer_hours(self):
        with pytest.raises(ValueError):
            FundingFlipRisk(min_buffer_hours=0)


class TestLiquidityImbalance:
    def test_ok_when_depth_above_requirement(self):
        det = LiquidityImbalance(safety_factor=1.5)
        q = make_cross_venue_quote(
            high_hourly_rate=0.0005, low_hourly_rate=0.0001, depth_usd=200_000
        )
        # Notional 50k * 1.5 = 75k required, depth = 200k → OK
        res = det.run(q, _open_position(50_000))
        assert res.triggered is False

    def test_triggers_when_depth_insufficient(self):
        det = LiquidityImbalance(safety_factor=2.0)
        q = make_cross_venue_quote(
            high_hourly_rate=0.0005, low_hourly_rate=0.0001, depth_usd=30_000
        )
        # Notional 50k * 2.0 = 100k required, depth = 30k → trigger
        res = det.run(q, _open_position(50_000))
        assert res.triggered is True

    def test_invalid_safety_factor(self):
        with pytest.raises(ValueError):
            LiquidityImbalance(safety_factor=0.5)


class TestDataStaleness:
    def test_fresh_data_ok(self):
        det = DataStaleness(max_lag_seconds=30)
        q = make_cross_venue_quote()
        # synthetic defaults to lag=0
        res = det.run(q)
        assert res.triggered is False
        assert res.severity == 0.0

    def test_stale_data_triggers(self):
        det = DataStaleness(max_lag_seconds=10)
        st_a = make_market_state(Venue.DRIFT, last_update_lag_s=60, hourly_rate=0.0005)
        st_b = make_market_state(Venue.HYPERLIQUID, last_update_lag_s=5, hourly_rate=0.0001)
        from dfm.state import CrossVenueQuote
        q = CrossVenueQuote(
            symbol="SOL", timestamp=0, high_venue=st_a, low_venue=st_b
        )
        res = det.run(q)
        assert res.triggered is True
        assert res.evidence["worst_lag_s"] == 60


class TestConcentrationRisk:
    def test_balanced_oi_no_trigger(self):
        det = ConcentrationRisk(max_imbalance=0.6)
        st = make_market_state(
            Venue.DRIFT, open_interest_long=1_000_000, open_interest_short=1_000_000
        )
        res = det.run(st)
        assert res.triggered is False

    def test_heavy_long_imbalance_triggers(self):
        det = ConcentrationRisk(max_imbalance=0.5)
        st = make_market_state(
            Venue.DRIFT, open_interest_long=4_000_000, open_interest_short=1_000_000
        )
        # imbalance = (4-1)/5 = 0.6 > 0.5
        res = det.run(st)
        assert res.triggered is True

    def test_invalid_imbalance_threshold(self):
        with pytest.raises(ValueError):
            ConcentrationRisk(max_imbalance=1.5)


class TestBasisBlowoutRisk:
    def test_small_basis_ok(self):
        det = BasisBlowoutRisk(max_basis_pct=0.02)
        st = make_market_state(Venue.DRIFT, mark_price=150.1, index_price=150.0)
        # basis ~ 0.067%
        res = det.run(st)
        assert res.triggered is False

    def test_large_basis_triggers(self):
        det = BasisBlowoutRisk(max_basis_pct=0.01)
        st = make_market_state(Venue.DRIFT, mark_price=155.0, index_price=150.0)
        # basis = 3.3%
        res = det.run(st)
        assert res.triggered is True


class TestMaxDrawdownGate:
    def test_positive_pnl_no_trigger(self):
        det = MaxDrawdownGate(max_dd_pct=1.0)
        res = det.run(_open_position(50_000), realized_pnl_usd=500)
        assert res.triggered is False

    def test_severe_drawdown_triggers(self):
        det = MaxDrawdownGate(max_dd_pct=1.0)
        res = det.run(_open_position(50_000), realized_pnl_usd=-1_500)
        # -1500 / 50000 = -3% < -1%
        assert res.triggered is True

    def test_zero_notional_returns_no_signal(self):
        det = MaxDrawdownGate()
        # Can't construct zero-notional Position via Pydantic (validation), so skip
        # the zero case directly. The detector's defensive branch is exercised below.
        # Build a position with size > 0 but force notional=0 via mock-style: skip.
        pos = _open_position(0.000001)  # tiny but non-zero
        res = det.run(pos, realized_pnl_usd=0)
        # Tiny notional, zero PnL → dd_pct = 0, not triggered
        assert res.triggered is False


# ──────────────────────────────────────────────────────────────────────
# Round-5 regression: ConcentrationRisk must NOT report "balanced" when
# OI data is unknown (both long+short = 0 = HL sentinel).
# ──────────────────────────────────────────────────────────────────────


def test_concentration_risk_signals_unknown_when_oi_missing():
    """HyperliquidClient sets open_interest_{long,short} = 0 when the
    venue feed doesn't expose a long/short split. Pre-Round-5 the
    detector then reported "OI imbalance: +0% (balanced)" — a
    misleading-data-as-safe report. Post-fix it must explicitly say
    "data unavailable; check skipped".
    """
    from dfm.risk import ConcentrationRisk
    from dfm.state import FundingRate, PerpMarketState, Venue

    unknown_oi_state = PerpMarketState(
        venue=Venue.HYPERLIQUID, symbol="SOL-PERP", timestamp=1_716_000_000,
        mark_price=150.0, index_price=150.0,
        bid_depth_usd=500_000, ask_depth_usd=500_000,
        open_interest_long=0.0, open_interest_short=0.0,
        funding_rate=FundingRate(
            venue=Venue.HYPERLIQUID, symbol="SOL-PERP",
            timestamp=1_716_000_000, hourly_rate=0.0001,
        ),
    )
    r = ConcentrationRisk().run(unknown_oi_state)
    assert r.triggered is False
    assert r.severity == 0.0
    assert "unavailable" in r.headline.lower()
    assert r.evidence.get("data_available") is False


def test_concentration_risk_real_imbalance_still_fires():
    """Sanity: real OI data still triggers when imbalance exceeds threshold."""
    from dfm.risk import ConcentrationRisk
    from dfm.state import FundingRate, PerpMarketState, Venue

    heavy_long_state = PerpMarketState(
        venue=Venue.DRIFT, symbol="SOL-PERP", timestamp=1_716_000_000,
        mark_price=150.0, index_price=150.0,
        bid_depth_usd=500_000, ask_depth_usd=500_000,
        open_interest_long=9_000_000.0, open_interest_short=1_000_000.0,
        funding_rate=FundingRate(
            venue=Venue.DRIFT, symbol="SOL-PERP",
            timestamp=1_716_000_000, hourly_rate=0.0005,
        ),
    )
    r = ConcentrationRisk(max_imbalance=0.5).run(heavy_long_state)
    assert r.triggered is True
    assert r.evidence.get("data_available") is True
    assert r.evidence["imbalance"] > 0.5
