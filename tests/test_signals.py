"""Tests for the signal-evaluation layer (build_quote + evaluate)."""

from __future__ import annotations

import math

from dfm.signals import SignalThresholds, build_quote, evaluate
from dfm.state import Venue
from dfm.synthetic import make_cross_venue_quote, make_market_state


def test_build_quote_picks_highest_lowest():
    states = {
        Venue.DRIFT: make_market_state(Venue.DRIFT, hourly_rate=0.0002),
        Venue.HYPERLIQUID: make_market_state(Venue.HYPERLIQUID, hourly_rate=0.0005),
        Venue.ORDERLY: make_market_state(Venue.ORDERLY, hourly_rate=0.0001),
    }
    q = build_quote(states, "SOL-PERP", 1_716_000_000)
    assert q is not None
    assert q.high_venue.venue == Venue.HYPERLIQUID
    assert q.low_venue.venue == Venue.ORDERLY


def test_build_quote_requires_two_venues():
    states = {Venue.DRIFT: make_market_state(Venue.DRIFT)}
    assert build_quote(states, "SOL-PERP", 0) is None


def test_build_quote_skips_zero_spread():
    states = {
        Venue.DRIFT: make_market_state(Venue.DRIFT, hourly_rate=0.0001),
        Venue.HYPERLIQUID: make_market_state(Venue.HYPERLIQUID, hourly_rate=0.0001),
    }
    assert build_quote(states, "SOL-PERP", 0) is None


def test_evaluate_above_threshold_emits_signal():
    q = make_cross_venue_quote(high_hourly_rate=0.0005, low_hourly_rate=0.0001)
    sig = evaluate(q)
    assert sig is not None
    assert sig.high_venue == Venue.DRIFT
    assert sig.low_venue == Venue.HYPERLIQUID
    assert sig.spread_bps_per_hour > 0
    assert sig.confidence > 0.4


def test_evaluate_below_threshold_returns_none():
    q = make_cross_venue_quote(high_hourly_rate=0.00011, low_hourly_rate=0.0001)
    # spread = 0.1 bps/h, below default 1.0 threshold
    assert evaluate(q) is None


def test_evaluate_rejects_high_price_dispersion():
    q = make_cross_venue_quote(
        high_hourly_rate=0.0005, low_hourly_rate=0.0001,
        mark_high=150.0, mark_low=145.0,  # ~3.4% dispersion
    )
    assert evaluate(q) is None  # dispersion > 0.3%


def test_evaluate_rejects_insufficient_depth():
    q = make_cross_venue_quote(
        high_hourly_rate=0.0005, low_hourly_rate=0.0001,
        depth_usd=10_000.0,  # below 50k default min
    )
    assert evaluate(q) is None


def test_evaluate_breakeven_holding_hours_math():
    # spread = 4 bps/h, taker fee = 5 bps × 4 sides = 20 bps total
    # breakeven = 20 / 4 = 5 hours
    q = make_cross_venue_quote(high_hourly_rate=0.0005, low_hourly_rate=0.0001)
    sig = evaluate(q, SignalThresholds(taker_fee_bps=5.0))
    assert sig is not None
    assert math.isclose(sig.breakeven_holding_hours, 5.0, abs_tol=0.01)


def test_evaluate_recommended_size_capped_to_depth_fraction():
    q = make_cross_venue_quote(
        high_hourly_rate=0.0005, low_hourly_rate=0.0001, depth_usd=1_000_000
    )
    sig = evaluate(q, SignalThresholds(max_size_pct_of_depth=0.10))
    assert sig is not None
    assert math.isclose(sig.recommended_size_usd, 100_000)


def test_evaluate_confidence_climbs_with_spread():
    q_small = make_cross_venue_quote(high_hourly_rate=0.00015, low_hourly_rate=0.0001)
    q_big = make_cross_venue_quote(high_hourly_rate=0.0010, low_hourly_rate=0.0001)
    s_small = evaluate(q_small, SignalThresholds(min_spread_bps_per_hour=0.3))
    s_big = evaluate(q_big)
    assert s_small is not None and s_big is not None
    assert s_big.confidence > s_small.confidence


def test_signal_with_unrealistic_threshold_returns_none():
    # Very high min_spread rejects everything
    q = make_cross_venue_quote(high_hourly_rate=0.0005, low_hourly_rate=0.0001)
    sig = evaluate(q, SignalThresholds(min_spread_bps_per_hour=1000.0))
    assert sig is None
