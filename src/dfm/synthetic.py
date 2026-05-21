"""Deterministic synthetic data generators for tests and offline demos.

`make_market_state()` produces a realistic single-venue PerpMarketState.
`make_cross_venue_quote()` builds a paired quote with a configurable spread.
`make_quote_stream()` yields a chronological sequence used by the backtester.

All generators are deterministic given (seed, params), so test fixtures
stay stable across CI runs.
"""

from __future__ import annotations

import math
from collections.abc import Iterator

from .state import (
    CrossVenueQuote,
    FundingRate,
    PerpMarketState,
    Venue,
)


def make_funding_rate(
    venue: Venue,
    symbol: str = "SOL-PERP",
    hourly_rate: float = 0.0001,
    timestamp: int = 1_716_000_000,
) -> FundingRate:
    return FundingRate(
        venue=venue,
        symbol=symbol,
        timestamp=timestamp,
        hourly_rate=hourly_rate,
        next_funding_in_seconds=3600,
    )


def make_market_state(
    venue: Venue,
    symbol: str = "SOL-PERP",
    *,
    mark_price: float = 150.0,
    index_price: float | None = None,
    hourly_rate: float = 0.0001,
    open_interest_long: float = 10_000_000.0,
    open_interest_short: float = 10_000_000.0,
    bid_depth_usd: float = 500_000.0,
    ask_depth_usd: float = 500_000.0,
    last_update_lag_s: int = 0,
    timestamp: int = 1_716_000_000,
) -> PerpMarketState:
    if index_price is None:
        index_price = mark_price
    return PerpMarketState(
        venue=venue,
        symbol=symbol,
        timestamp=timestamp,
        mark_price=mark_price,
        index_price=index_price,
        open_interest_long=open_interest_long,
        open_interest_short=open_interest_short,
        bid_depth_usd=bid_depth_usd,
        ask_depth_usd=ask_depth_usd,
        funding_rate=make_funding_rate(venue, symbol, hourly_rate, timestamp),
        last_update_lag_s=last_update_lag_s,
    )


def make_cross_venue_quote(
    *,
    symbol: str = "SOL-PERP",
    timestamp: int = 1_716_000_000,
    high_venue: Venue = Venue.DRIFT,
    low_venue: Venue = Venue.HYPERLIQUID,
    high_hourly_rate: float = 0.0005,
    low_hourly_rate: float = 0.0001,
    mark_high: float = 150.10,
    mark_low: float = 150.00,
    depth_usd: float = 500_000.0,
) -> CrossVenueQuote:
    return CrossVenueQuote(
        symbol=symbol,
        timestamp=timestamp,
        high_venue=make_market_state(
            high_venue,
            symbol=symbol,
            mark_price=mark_high,
            hourly_rate=high_hourly_rate,
            bid_depth_usd=depth_usd,
            ask_depth_usd=depth_usd,
            timestamp=timestamp,
        ),
        low_venue=make_market_state(
            low_venue,
            symbol=symbol,
            mark_price=mark_low,
            hourly_rate=low_hourly_rate,
            bid_depth_usd=depth_usd,
            ask_depth_usd=depth_usd,
            timestamp=timestamp,
        ),
    )


def make_quote_stream(
    *,
    n_samples: int = 24,
    start_timestamp: int = 1_716_000_000,
    interval_seconds: int = 3600,
    initial_spread_hourly: float = 0.0005,
    final_spread_hourly: float = 0.0001,
    high_venue: Venue = Venue.DRIFT,
    low_venue: Venue = Venue.HYPERLIQUID,
    mark_high_start: float = 150.0,
    mark_low_start: float = 150.0,
    mark_drift_per_step: float = 0.0,
) -> Iterator[CrossVenueQuote]:
    """Generate a chronological CrossVenueQuote stream.

    Spread decays linearly from initial to final; mark prices drift by
    `mark_drift_per_step` per sample on the high side (low side stays put,
    creating basis PnL when the position is forced to close).
    """
    if n_samples < 1:
        return
    for i in range(n_samples):
        t = start_timestamp + i * interval_seconds
        # Linear decay of spread
        frac = i / (n_samples - 1) if n_samples > 1 else 0.0
        high_rate = initial_spread_hourly * (1 - frac) + final_spread_hourly * frac
        mark_high = mark_high_start + mark_drift_per_step * i
        mark_low = mark_low_start
        yield CrossVenueQuote(
            symbol="SOL-PERP",
            timestamp=t,
            high_venue=make_market_state(
                high_venue,
                symbol="SOL-PERP",
                mark_price=mark_high,
                hourly_rate=high_rate,
                bid_depth_usd=500_000.0,
                ask_depth_usd=500_000.0,
                timestamp=t,
            ),
            low_venue=make_market_state(
                low_venue,
                symbol="SOL-PERP",
                mark_price=mark_low,
                hourly_rate=0.00005,
                bid_depth_usd=500_000.0,
                ask_depth_usd=500_000.0,
                timestamp=t,
            ),
        )


def make_oscillating_spread(
    *,
    n_samples: int = 48,
    amplitude_hourly: float = 0.0003,
    period_samples: int = 24,
    start_timestamp: int = 1_716_000_000,
    interval_seconds: int = 3600,
) -> Iterator[CrossVenueQuote]:
    """Sine-wave spread between two venues — used to verify backtester opens
    and closes positions across multiple cycles.
    """
    for i in range(n_samples):
        t = start_timestamp + i * interval_seconds
        phase = 2 * math.pi * i / period_samples
        spread = amplitude_hourly * math.sin(phase)
        # When spread positive: drift high, hyperliquid low
        # When negative: reverse
        if spread >= 0:
            high_rate = spread
            low_rate = -spread / 4
            yield make_cross_venue_quote(
                timestamp=t,
                high_venue=Venue.DRIFT,
                low_venue=Venue.HYPERLIQUID,
                high_hourly_rate=high_rate,
                low_hourly_rate=low_rate,
            )
        else:
            yield make_cross_venue_quote(
                timestamp=t,
                high_venue=Venue.HYPERLIQUID,
                low_venue=Venue.DRIFT,
                high_hourly_rate=-spread,
                low_hourly_rate=spread / 4,
            )
