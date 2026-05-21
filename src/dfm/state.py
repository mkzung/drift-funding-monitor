"""Domain models for cross-venue perp funding-rate state.

A perp DEX exposes per-symbol "funding rate" — periodic payments between long
and short holders, set by the spread of mark vs index price. Positive funding
= longs pay shorts; negative = shorts pay longs. Funding accrues per-hour
(Hyperliquid: hourly; Drift: hourly with sub-hour interpolation; Orderly: 8h;
Backpack: 8h).

Cross-venue arb: when funding rate differs significantly between two venues
on the same symbol, you can short the high-funding side and long the low-
funding side delta-neutral, harvesting the spread until it converges.

This module models:
  1. FundingRate — a single (venue, symbol, ts, rate) sample.
  2. PerpMarketState — full market snapshot (price, depth, OI, funding).
  3. CrossVenueQuote — paired state for one symbol across two venues, used
     by the signal layer to decide whether to open an arb.
  4. Position — an open arb position with sides + size + entry funding diff.

All amounts are floats in human units (SOL, USD); on-chain encoding is
isolated in venues.py. Funding rates are floats representing hourly rate
(e.g., 0.0001 = 1 bps per hour ≈ 8.76% per year).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class Venue(str, Enum):
    """Supported perp venues. Order is the canonical sort for stable reports."""

    DRIFT = "drift"
    HYPERLIQUID = "hyperliquid"
    ORDERLY = "orderly"
    BACKPACK = "backpack"


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"


class FundingRate(BaseModel):
    """One funding-rate sample at one timestamp on one venue."""

    model_config = ConfigDict(frozen=True)

    venue: Venue
    symbol: str = Field(..., description='Asset symbol, e.g. "SOL-PERP" or "BTC-USD"')
    timestamp: int = Field(..., description="Unix seconds")
    hourly_rate: float = Field(
        ...,
        description="Funding rate per hour, signed. Positive = longs pay shorts.",
    )
    next_funding_in_seconds: int = Field(
        default=0, ge=0, description="Seconds until next funding payment"
    )

    @property
    def annualized_rate(self) -> float:
        """Hourly rate × 24 × 365.25 ≈ APR if rate stays constant."""
        return self.hourly_rate * 24 * 365.25


class PerpMarketState(BaseModel):
    """Full perp market snapshot on one venue at one timestamp.

    `mark_price` is the venue's traded mark used for PnL; `index_price` is the
    oracle (Pyth on Solana, weighted CEX index on EVM venues) used to compute
    funding. Their spread drives the next funding rate.
    """

    model_config = ConfigDict(frozen=True)

    venue: Venue
    symbol: str
    timestamp: int
    mark_price: float = Field(..., gt=0)
    index_price: float = Field(..., gt=0)
    open_interest_long: float = Field(default=0.0, ge=0)
    open_interest_short: float = Field(default=0.0, ge=0)
    bid_depth_usd: float = Field(default=0.0, ge=0, description="USD depth within 0.5% of mark")
    ask_depth_usd: float = Field(default=0.0, ge=0)
    funding_rate: FundingRate
    last_update_lag_s: int = Field(
        default=0, ge=0, description="Seconds since data was last refreshed"
    )

    @property
    def basis_pct(self) -> float:
        """Mark/index basis as a fraction. Positive = mark above index."""
        return (self.mark_price - self.index_price) / self.index_price

    @property
    def open_interest_imbalance(self) -> float:
        """(long - short) / (long + short). Range [-1, 1]; 0 = balanced."""
        total = self.open_interest_long + self.open_interest_short
        if total == 0:
            return 0.0
        return (self.open_interest_long - self.open_interest_short) / total

    @property
    def min_depth_usd(self) -> float:
        """Smaller side of the book — the binding constraint on entry size."""
        return min(self.bid_depth_usd, self.ask_depth_usd)


class CrossVenueQuote(BaseModel):
    """Paired perp state for one symbol across two venues at one timestamp.

    By convention, `high_venue` is the venue with the higher (more positive)
    funding rate, i.e. the side to SHORT in an arb; `low_venue` is the side
    to LONG. Spread is computed in basis points per hour for human-readable
    signal magnitude.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    timestamp: int
    high_venue: PerpMarketState
    low_venue: PerpMarketState

    @property
    def spread_bps_per_hour(self) -> float:
        """Funding-rate spread, in bps per hour. Always >= 0 by construction."""
        return (
            self.high_venue.funding_rate.hourly_rate - self.low_venue.funding_rate.hourly_rate
        ) * 10_000

    @property
    def annualized_spread_pct(self) -> float:
        """Annualized funding-rate spread as a percent (e.g., 12.0 == 12% APR)."""
        return self.spread_bps_per_hour / 10_000 * 24 * 365.25 * 100

    @property
    def price_dispersion_pct(self) -> float:
        """|mark_high − mark_low| / mid, as a percent. Wide dispersion =
        execution slippage risk (one venue's mark may be stale)."""
        mid = (self.high_venue.mark_price + self.low_venue.mark_price) / 2
        if mid == 0:
            return 0.0
        return abs(self.high_venue.mark_price - self.low_venue.mark_price) / mid * 100


class Position(BaseModel):
    """An open arb position: long on one venue, short on the other.

    Sizing is symmetric in notional — long_size_tokens × long_entry_price ≈
    short_size_tokens × short_entry_price. The PnL drivers are:
      1. Funding accrual (the alpha)
      2. Basis convergence/divergence (risk)
      3. Liquidation buffer if either leg moves into margin distress
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    opened_at: int = Field(..., description="Unix seconds")
    long_venue: Venue
    long_size_tokens: float = Field(..., gt=0)
    long_entry_price: float = Field(..., gt=0)
    short_venue: Venue
    short_size_tokens: float = Field(..., gt=0)
    short_entry_price: float = Field(..., gt=0)
    entry_funding_diff_hourly: float = Field(
        ..., description="(short.funding - long.funding) at entry; positive = expected carry"
    )

    @property
    def notional_usd(self) -> float:
        """Average notional across legs."""
        return (
            self.long_size_tokens * self.long_entry_price
            + self.short_size_tokens * self.short_entry_price
        ) / 2

    @property
    def is_delta_neutral(self, tolerance_pct: float = 0.5) -> bool:
        """True if the two legs are within `tolerance_pct` notional of each other."""
        ln = self.long_size_tokens * self.long_entry_price
        sn = self.short_size_tokens * self.short_entry_price
        if max(ln, sn) == 0:
            return True
        return abs(ln - sn) / max(ln, sn) * 100 <= tolerance_pct


@dataclass
class VenueHistory:
    """Ordered series of PerpMarketState samples for one (venue, symbol)."""

    venue: Venue
    symbol: str
    samples: list[PerpMarketState] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.samples)

    def latest(self) -> PerpMarketState:
        if not self.samples:
            raise ValueError(f"VenueHistory {self.venue.value}/{self.symbol} is empty")
        return max(self.samples, key=lambda s: s.timestamp)

    def at(self, timestamp: int) -> PerpMarketState | None:
        """Latest sample with timestamp <= given."""
        eligible = [s for s in self.samples if s.timestamp <= timestamp]
        return max(eligible, key=lambda s: s.timestamp) if eligible else None

    def iter_chronological(self) -> Iterable[PerpMarketState]:
        return iter(sorted(self.samples, key=lambda s: s.timestamp))
