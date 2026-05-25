"""Tests for state models — FundingRate, PerpMarketState, CrossVenueQuote, Position."""

from __future__ import annotations

import math

import pytest

from dfm.state import (
    PerpMarketState,
    Position,
    Venue,
    VenueHistory,
)
from dfm.synthetic import make_funding_rate, make_market_state


class TestFundingRate:
    def test_annualized_rate(self):
        fr = make_funding_rate(Venue.DRIFT, hourly_rate=0.0001)
        # 0.0001 * 24 * 365.25 = ~0.8766
        assert math.isclose(fr.annualized_rate, 0.0001 * 24 * 365.25)

    def test_negative_funding_rate(self):
        fr = make_funding_rate(Venue.DRIFT, hourly_rate=-0.0005)
        assert fr.hourly_rate < 0
        assert fr.annualized_rate < 0

    def test_frozen(self):
        from pydantic import ValidationError
        fr = make_funding_rate(Venue.DRIFT)
        with pytest.raises(ValidationError):
            fr.hourly_rate = 0.99  # type: ignore[misc]


class TestPerpMarketState:
    def test_basis_pct_positive(self):
        st = make_market_state(Venue.DRIFT, mark_price=151.0, index_price=150.0)
        assert math.isclose(st.basis_pct, 1.0 / 150.0)

    def test_basis_pct_negative(self):
        st = make_market_state(Venue.DRIFT, mark_price=149.0, index_price=150.0)
        assert st.basis_pct < 0

    def test_oi_imbalance_balanced(self):
        st = make_market_state(
            Venue.DRIFT, open_interest_long=1_000_000, open_interest_short=1_000_000
        )
        assert st.open_interest_imbalance == 0.0

    def test_oi_imbalance_long_heavy(self):
        st = make_market_state(
            Venue.DRIFT, open_interest_long=3_000_000, open_interest_short=1_000_000
        )
        assert math.isclose(st.open_interest_imbalance, 0.5)

    def test_oi_imbalance_zero_total(self):
        st = make_market_state(
            Venue.DRIFT, open_interest_long=0, open_interest_short=0
        )
        assert st.open_interest_imbalance == 0.0

    def test_min_depth(self):
        st = make_market_state(Venue.DRIFT, bid_depth_usd=400_000, ask_depth_usd=500_000)
        assert st.min_depth_usd == 400_000

    def test_validation_rejects_zero_mark_price(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            PerpMarketState(
                venue=Venue.DRIFT, symbol="SOL", timestamp=0,
                mark_price=0, index_price=1,
                funding_rate=make_funding_rate(Venue.DRIFT),
            )


class TestCrossVenueQuote:
    def test_spread_bps_per_hour(self):
        from dfm.synthetic import make_cross_venue_quote
        q = make_cross_venue_quote(high_hourly_rate=0.0005, low_hourly_rate=0.0001)
        # (0.0005 - 0.0001) * 10000 = 4.0 bps/h
        assert math.isclose(q.spread_bps_per_hour, 4.0)

    def test_annualized_spread_pct(self):
        from dfm.synthetic import make_cross_venue_quote
        q = make_cross_venue_quote(high_hourly_rate=0.0005, low_hourly_rate=0.0001)
        # 4 bps/h * 24 * 365.25 = ~35064 bps/yr = 350.64% APR
        # Actually: 4 / 10000 * 24 * 365.25 * 100 = 3.504 ... wait let me check
        # 4 bps/h hourly = 0.0004 hourly = 0.0004*24*365.25 annualized = 3.5064
        # in pct = 350.64%
        assert math.isclose(q.annualized_spread_pct, 0.0004 * 24 * 365.25 * 100, rel_tol=1e-9)

    def test_price_dispersion_pct(self):
        from dfm.synthetic import make_cross_venue_quote
        q = make_cross_venue_quote(mark_high=150.5, mark_low=149.5)
        # diff = 1.0, mid = 150.0 → 0.667%
        assert math.isclose(q.price_dispersion_pct, 1.0 / 150.0 * 100, rel_tol=1e-9)


class TestExtraForbid:
    """v0.2.0: every Pydantic model now rejects unknown fields with
    extra="forbid". Catches typos like `mark_pric=150` at construction
    time instead of silently letting the field be dropped.
    """

    def test_funding_rate_rejects_unknown_field(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            from dfm.state import FundingRate
            FundingRate(
                venue=Venue.DRIFT, symbol="SOL-PERP", timestamp=0,
                hourly_rate=0.0001,
                typo_field="oops",  # type: ignore[call-arg]
            )

    def test_perp_market_state_rejects_unknown_field(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            PerpMarketState(
                venue=Venue.DRIFT, symbol="SOL", timestamp=0,
                mark_price=150, index_price=150,
                funding_rate=make_funding_rate(Venue.DRIFT),
                mark_pric=999,  # type: ignore[call-arg]
            )

    def test_cross_venue_quote_rejects_unknown_field(self):
        from pydantic import ValidationError

        from dfm.state import CrossVenueQuote
        with pytest.raises(ValidationError):
            CrossVenueQuote(
                symbol="SOL", timestamp=0,
                high_venue=make_market_state(Venue.DRIFT),
                low_venue=make_market_state(Venue.HYPERLIQUID),
                extra_field=42,  # type: ignore[call-arg]
            )

    def test_position_rejects_unknown_field(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            Position(
                symbol="SOL-PERP", opened_at=0,
                long_venue=Venue.HYPERLIQUID,
                long_size_tokens=100, long_entry_price=150.0,
                short_venue=Venue.DRIFT,
                short_size_tokens=100, short_entry_price=150.0,
                entry_funding_diff_hourly=0.0004,
                leverage=5,  # type: ignore[call-arg]
            )


class TestPosition:
    def test_notional_usd(self):
        p = Position(
            symbol="SOL-PERP", opened_at=1_716_000_000,
            long_venue=Venue.HYPERLIQUID,
            long_size_tokens=100, long_entry_price=150.0,
            short_venue=Venue.DRIFT,
            short_size_tokens=100, short_entry_price=150.0,
            entry_funding_diff_hourly=0.0004,
        )
        assert math.isclose(p.notional_usd, 15_000)


class TestVenueHistory:
    def test_latest_raises_when_empty(self):
        h = VenueHistory(venue=Venue.DRIFT, symbol="SOL-PERP")
        with pytest.raises(ValueError):
            h.latest()

    def test_latest_returns_max_timestamp(self):
        h = VenueHistory(
            venue=Venue.DRIFT, symbol="SOL-PERP",
            samples=[
                make_market_state(Venue.DRIFT, timestamp=100),
                make_market_state(Venue.DRIFT, timestamp=300),
                make_market_state(Venue.DRIFT, timestamp=200),
            ],
        )
        assert h.latest().timestamp == 300

    def test_at_returns_latest_le(self):
        h = VenueHistory(
            venue=Venue.DRIFT, symbol="SOL-PERP",
            samples=[
                make_market_state(Venue.DRIFT, timestamp=100),
                make_market_state(Venue.DRIFT, timestamp=200),
                make_market_state(Venue.DRIFT, timestamp=300),
            ],
        )
        assert h.at(250).timestamp == 200
        assert h.at(50) is None
        assert h.at(300).timestamp == 300
