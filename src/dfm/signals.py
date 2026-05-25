"""Cross-venue arb signal detection.

Given a `CrossVenueQuote` (paired perp state on two venues), decide:
  1. Is the funding-rate spread wide enough to be worth opening?
  2. Is the price dispersion safe (no stale-mark cross-venue arb leg)?
  3. Is there enough depth on both venues for the desired size?
  4. What's the breakeven holding period given taker fees + slippage?

The output is an `ArbSignal` with a fractional confidence in [0,1] and an
evidence dict the caller can use for sizing decisions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .state import CrossVenueQuote, PerpMarketState, Venue


@dataclass(frozen=True)
class ArbSignal:
    """One arb signal — a recommendation, not an order."""

    symbol: str
    high_venue: Venue
    low_venue: Venue
    spread_bps_per_hour: float
    annualized_spread_pct: float
    recommended_size_usd: float
    breakeven_holding_hours: float
    confidence: float
    evidence: dict[str, Any] = field(default_factory=dict)
    reason: str = ""


def build_quote(
    states: dict[Venue, PerpMarketState], symbol: str, timestamp: int
) -> CrossVenueQuote | None:
    """Pick the two venues with the widest funding spread, build a CrossVenueQuote.

    Returns None if fewer than 2 venues reported, or if the spread is exactly
    zero (no arb opportunity).
    """
    if len(states) < 2:
        return None
    sorted_states = sorted(
        states.values(),
        key=lambda s: s.funding_rate.hourly_rate,
        reverse=True,
    )
    high, low = sorted_states[0], sorted_states[-1]
    if high.funding_rate.hourly_rate == low.funding_rate.hourly_rate:
        return None
    return CrossVenueQuote(
        symbol=symbol,
        timestamp=timestamp,
        high_venue=high,
        low_venue=low,
    )


@dataclass(frozen=True)
class SignalThresholds:
    """Configurable thresholds for ArbSignal generation.

    Defaults reflect typical retail-friendly settings; adjust for institutional
    size (lower min_spread, higher min_depth).
    """

    min_spread_bps_per_hour: float = 1.0        # ≥ ~0.876% APR — below this is noise
    max_price_dispersion_pct: float = 0.30      # > 0.3% mark dispersion = stale-leg risk
    min_depth_usd: float = 50_000.0             # min combined-side liquidity to enter
    taker_fee_bps: float = 5.0                  # round-trip taker fee, both legs
    max_size_pct_of_depth: float = 0.20         # never exceed 20% of min depth on either leg
    min_confidence: float = 0.40                # signals below this aren't emitted


def evaluate(
    quote: CrossVenueQuote, thresholds: SignalThresholds | None = None
) -> ArbSignal | None:
    """Evaluate one CrossVenueQuote against thresholds. Returns ArbSignal or None.

    Confidence is a [0,1] blend of:
      - spread magnitude vs threshold (40% weight)
      - depth sufficiency on the binding side (30%)
      - price dispersion penalty — high dispersion = stale data (20%)
      - data freshness — old last_update_lag_s penalizes confidence (10%)
    """
    th = thresholds or SignalThresholds()

    spread_bph = quote.spread_bps_per_hour
    if spread_bph < th.min_spread_bps_per_hour:
        return None

    dispersion = quote.price_dispersion_pct
    if dispersion > th.max_price_dispersion_pct:
        return None

    # Entry binding side = min(high.bid, low.ask) since we OPEN by:
    #   - SHORTING on the high-funding venue (sell-to-open into bids)
    #   - LONGING on the low-funding venue (buy-to-open into asks)
    # Closing binding side = min(high.ask, low.bid) since we EXIT by:
    #   - buying the short leg back (hit asks on high venue)
    #   - selling the long leg out (hit bids on low venue)
    # A signal that clears entry depth but not closing depth is a trap —
    # you can open the position but cannot exit cleanly. Both must clear.
    entry_depth = min(quote.high_venue.bid_depth_usd, quote.low_venue.ask_depth_usd)
    closing_depth = min(quote.high_venue.ask_depth_usd, quote.low_venue.bid_depth_usd)
    binding_depth = min(entry_depth, closing_depth)
    if binding_depth < th.min_depth_usd:
        return None

    recommended_size = binding_depth * th.max_size_pct_of_depth

    # Breakeven: total round-trip fee = taker_fee_bps × 2 (open both legs)
    # × 2 (close both legs eventually) = 4 × taker_fee_bps total, in bps.
    # Funding accrues at spread_bph per hour. Hours to breakeven:
    total_fee_bps = th.taker_fee_bps * 4
    breakeven_hours = total_fee_bps / spread_bph if spread_bph > 0 else float("inf")

    # Confidence components
    spread_score = min(1.0, spread_bph / max(th.min_spread_bps_per_hour, 1e-9) / 4.0)
    depth_score = min(1.0, binding_depth / th.min_depth_usd / 5.0)
    dispersion_score = 1.0 - dispersion / max(th.max_price_dispersion_pct, 1e-9)
    avg_lag = (
        quote.high_venue.last_update_lag_s + quote.low_venue.last_update_lag_s
    ) / 2
    freshness_score = max(0.0, 1.0 - avg_lag / 60.0)  # full score < 0s lag, zero at 60s

    confidence = (
        0.4 * spread_score
        + 0.3 * depth_score
        + 0.2 * dispersion_score
        + 0.1 * freshness_score
    )
    confidence = max(0.0, min(1.0, confidence))

    if confidence < th.min_confidence:
        return None

    return ArbSignal(
        symbol=quote.symbol,
        high_venue=quote.high_venue.venue,
        low_venue=quote.low_venue.venue,
        spread_bps_per_hour=spread_bph,
        annualized_spread_pct=quote.annualized_spread_pct,
        recommended_size_usd=recommended_size,
        breakeven_holding_hours=breakeven_hours,
        confidence=confidence,
        evidence={
            "binding_depth_usd": binding_depth,
            "entry_depth_usd": entry_depth,
            "closing_depth_usd": closing_depth,
            "price_dispersion_pct": dispersion,
            "total_fee_bps": total_fee_bps,
            "high_funding_hourly": quote.high_venue.funding_rate.hourly_rate,
            "low_funding_hourly": quote.low_venue.funding_rate.hourly_rate,
            "high_last_update_lag_s": quote.high_venue.last_update_lag_s,
            "low_last_update_lag_s": quote.low_venue.last_update_lag_s,
            "spread_score": spread_score,
            "depth_score": depth_score,
            "dispersion_score": dispersion_score,
            "freshness_score": freshness_score,
        },
        reason=(
            f"Short {quote.high_venue.venue.value} / long {quote.low_venue.venue.value} "
            f"on {quote.symbol}: spread {spread_bph:.2f} bps/h "
            f"({quote.annualized_spread_pct:.1f}% APR), "
            f"breakeven in {breakeven_hours:.1f}h, size ≤ ${recommended_size:,.0f}, "
            f"confidence {confidence:.2f}."
        ),
    )
