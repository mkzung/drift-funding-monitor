"""Risk detectors for open arb positions.

Six pure-function detectors complementing the signal layer:

  1. FundingFlipRisk     — flagged when the current high-vs-low funding ordering
                           is at risk of flipping (basis convergence shortens
                           the trade horizon).
  2. LiquidityImbalance  — flagged when one venue's depth has shrunk below
                           safe-exit threshold; closing legs may incur slippage.
  3. DataStaleness       — flagged when either venue's last_update_lag_s
                           exceeds a freshness threshold.
  4. ConcentrationRisk   — flagged when open interest on one side dominates
                           (>= 80%); funding regime change probability rises.
  5. BasisBlowoutRisk    — flagged when mark/index basis on either venue
                           exceeds a stress threshold (positive feedback
                           loop into funding).
  6. MaxDrawdownGate     — circuit-breaker that recommends close if
                           realized PnL is below `-X` pct of notional.

These are reporter detectors (returning structured results), NOT auto-
liquidators. The caller integrates with execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .state import CrossVenueQuote, PerpMarketState, Position


@dataclass(frozen=True)
class RiskDetectorResult:
    name: str
    triggered: bool
    severity: float      # [0, 1]
    headline: str
    evidence: dict[str, Any] = field(default_factory=dict)


# ──────────────────────────────────────────────────────────────────────
# 1. Funding flip risk
# ──────────────────────────────────────────────────────────────────────


class FundingFlipRisk:
    """How close is the high-low funding spread to flipping sign?

    A flip means the trade's carry direction reverses — what was alpha becomes
    drag. The detector measures hours of buffer remaining if the spread
    decays linearly at the rate observed since position-open.
    """

    def __init__(self, min_buffer_hours: float = 6.0):
        if min_buffer_hours <= 0:
            raise ValueError("min_buffer_hours must be > 0")
        self.min_buffer_hours = min_buffer_hours

    def run(
        self,
        quote_now: CrossVenueQuote,
        position: Position,
        hours_held: float,
    ) -> RiskDetectorResult:
        if hours_held <= 0:
            return RiskDetectorResult(
                name="FundingFlipRisk",
                triggered=False,
                severity=0.0,
                headline="Position just opened; insufficient time-series for flip estimate.",
                evidence={"hours_held": hours_held},
            )

        # Measure current spread in the SAME DIRECTION as the position's carry,
        # NOT just `quote_now.spread_bps_per_hour` (which always uses the
        # currently-highest venue as `high` and so will flip sign if rates
        # rotate between venues). Position carry direction = funding rate on
        # the SHORT venue minus funding on the LONG venue; positive means
        # the trade is still earning.
        high_state = quote_now.high_venue
        low_state = quote_now.low_venue
        if position.short_venue == high_state.venue and position.long_venue == low_state.venue:
            current_spread = (
                high_state.funding_rate.hourly_rate - low_state.funding_rate.hourly_rate
            )
        elif position.short_venue == low_state.venue and position.long_venue == high_state.venue:
            # Venues rotated since open — what was high is now low. The
            # position is now paying funding (negative carry).
            current_spread = (
                low_state.funding_rate.hourly_rate - high_state.funding_rate.hourly_rate
            )
        else:
            # Quote doesn't cover the position's venues at all — defensive
            # bail-out, treat as no signal.
            return RiskDetectorResult(
                name="FundingFlipRisk",
                triggered=False,
                severity=0.0,
                headline="Quote venues don't match position venues; cannot evaluate flip risk.",
                evidence={
                    "position_short": position.short_venue.value,
                    "position_long": position.long_venue.value,
                    "quote_high": high_state.venue.value,
                    "quote_low": low_state.venue.value,
                },
            )
        entry_spread = position.entry_funding_diff_hourly
        decay_per_hour = (entry_spread - current_spread) / hours_held

        if decay_per_hour <= 0:
            # Spread is widening or flat — no flip risk
            return RiskDetectorResult(
                name="FundingFlipRisk",
                triggered=False,
                severity=0.0,
                headline="Spread stable or widening; no near-term flip risk.",
                evidence={
                    "entry_spread_hourly": entry_spread,
                    "current_spread_hourly": current_spread,
                    "decay_per_hour": decay_per_hour,
                },
            )

        hours_to_zero = current_spread / decay_per_hour
        # If current_spread is already <= 0 the position has ALREADY flipped
        # — there's no future "flip in N hours" to report. Emit a distinct
        # "already flipped" headline (and hard-trigger) so the user reads
        # the truth instead of nonsense like "flip in -2.5h".
        if current_spread <= 0:
            hours_since_flip = abs(hours_to_zero)
            return RiskDetectorResult(
                name="FundingFlipRisk",
                triggered=True,
                severity=1.0,
                headline=(
                    f"Spread already flipped; carry has been negative for "
                    f"{hours_since_flip:.1f}h."
                ),
                evidence={
                    "entry_spread_hourly": entry_spread,
                    "current_spread_hourly": current_spread,
                    "decay_per_hour": decay_per_hour,
                    "hours_since_flip": hours_since_flip,
                    "hours_held": hours_held,
                    "already_flipped": True,
                },
            )
        triggered = hours_to_zero < self.min_buffer_hours
        severity = max(0.0, min(1.0, 1.0 - hours_to_zero / max(self.min_buffer_hours, 1e-9)))
        return RiskDetectorResult(
            name="FundingFlipRisk",
            triggered=triggered,
            severity=severity,
            headline=(
                f"Spread decaying at {decay_per_hour * 10_000:.2f} bps/h; "
                f"flip in {hours_to_zero:.1f}h (threshold {self.min_buffer_hours:.0f}h)."
            ),
            evidence={
                "entry_spread_hourly": entry_spread,
                "current_spread_hourly": current_spread,
                "decay_per_hour": decay_per_hour,
                "hours_to_zero": hours_to_zero,
                "hours_held": hours_held,
            },
        )


# ──────────────────────────────────────────────────────────────────────
# 2. Liquidity imbalance
# ──────────────────────────────────────────────────────────────────────


class LiquidityImbalance:
    """Flag when current depth on either exit side is below entry size."""

    def __init__(self, safety_factor: float = 1.5):
        if safety_factor <= 1.0:
            raise ValueError("safety_factor must be > 1.0")
        self.safety_factor = safety_factor

    def run(
        self, quote_now: CrossVenueQuote, position: Position
    ) -> RiskDetectorResult:
        # Resolve venues in POSITION direction, not by quote high/low — if
        # venues rotated between open and now, `quote.high_venue` is the
        # position's LONG leg and reading its ask_depth would check the
        # wrong side. Same hazard the Round-2 FundingFlipRisk fix addressed;
        # Round-4 audit caught it here too.
        high, low = quote_now.high_venue, quote_now.low_venue
        if position.short_venue == high.venue and position.long_venue == low.venue:
            short_state, long_state = high, low
        elif position.short_venue == low.venue and position.long_venue == high.venue:
            short_state, long_state = low, high
        else:
            return RiskDetectorResult(
                name="LiquidityImbalance",
                triggered=False,
                severity=0.0,
                headline="Quote venues don't cover this position; depth check skipped.",
                evidence={
                    "quote_high_venue": high.venue.value,
                    "quote_low_venue": low.venue.value,
                    "position_short_venue": position.short_venue.value,
                    "position_long_venue": position.long_venue.value,
                },
            )
        # Closing requires hitting the OPPOSITE sides of what we used to open:
        # short leg → buy back into ASKS of short venue
        # long leg → sell out into BIDS of long venue
        binding_depth = min(short_state.ask_depth_usd, long_state.bid_depth_usd)
        needed = position.notional_usd * self.safety_factor
        triggered = binding_depth < needed
        severity = max(0.0, min(1.0, 1.0 - binding_depth / max(needed, 1.0)))
        return RiskDetectorResult(
            name="LiquidityImbalance",
            triggered=triggered,
            severity=severity,
            headline=(
                f"Exit-side depth ${binding_depth:,.0f} vs required "
                f"${needed:,.0f} ({self.safety_factor:.1f}× notional)."
            ),
            evidence={
                "binding_exit_depth_usd": binding_depth,
                "required_depth_usd": needed,
                "short_venue_ask_depth_usd": short_state.ask_depth_usd,
                "long_venue_bid_depth_usd": long_state.bid_depth_usd,
                "short_venue": short_state.venue.value,
                "long_venue": long_state.venue.value,
            },
        )


# ──────────────────────────────────────────────────────────────────────
# 3. Data staleness
# ──────────────────────────────────────────────────────────────────────


class DataStaleness:
    """Either venue's last_update_lag_s above threshold = stale data risk."""

    def __init__(self, max_lag_seconds: int = 30):
        if max_lag_seconds < 0:
            raise ValueError("max_lag_seconds must be >= 0")
        self.max_lag_seconds = max_lag_seconds

    def run(self, quote_now: CrossVenueQuote) -> RiskDetectorResult:
        worst_lag = max(
            quote_now.high_venue.last_update_lag_s,
            quote_now.low_venue.last_update_lag_s,
        )
        triggered = worst_lag > self.max_lag_seconds
        severity = max(0.0, min(1.0, worst_lag / (self.max_lag_seconds * 4 or 1)))
        return RiskDetectorResult(
            name="DataStaleness",
            triggered=triggered,
            severity=severity,
            headline=(
                f"Worst venue lag {worst_lag}s vs threshold "
                f"{self.max_lag_seconds}s; data may be stale."
            ),
            evidence={
                "high_lag_s": quote_now.high_venue.last_update_lag_s,
                "low_lag_s": quote_now.low_venue.last_update_lag_s,
                "worst_lag_s": worst_lag,
            },
        )


# ──────────────────────────────────────────────────────────────────────
# 4. OI concentration
# ──────────────────────────────────────────────────────────────────────


class ConcentrationRisk:
    """Open-interest imbalance ≥ threshold raises funding-flip probability."""

    def __init__(self, max_imbalance: float = 0.6):
        if not 0 < max_imbalance < 1:
            raise ValueError("max_imbalance must be in (0, 1)")
        self.max_imbalance = max_imbalance

    def run(self, state: PerpMarketState) -> RiskDetectorResult:
        # If OI long/short is unknown (both 0), the venue feed doesn't
        # expose a long/short breakdown. Reporting "+0% balanced" is
        # MISLEADING — caller would read it as "book is in balance" when
        # truth is "we don't know". Emit explicit unavailable-data result.
        if state.open_interest_long == 0 and state.open_interest_short == 0:
            return RiskDetectorResult(
                name="ConcentrationRisk",
                triggered=False,
                severity=0.0,
                headline=(
                    f"OI data unavailable on {state.venue.value} "
                    f"{state.symbol}; concentration check skipped."
                ),
                evidence={
                    "venue": state.venue.value,
                    "symbol": state.symbol,
                    "data_available": False,
                },
            )
        imb = abs(state.open_interest_imbalance)
        triggered = imb > self.max_imbalance
        severity = max(0.0, min(1.0, imb / max(self.max_imbalance, 1e-9)))
        return RiskDetectorResult(
            name="ConcentrationRisk",
            triggered=triggered,
            severity=severity,
            headline=(
                f"OI imbalance on {state.venue.value} {state.symbol}: "
                f"{state.open_interest_imbalance:+.0%} "
                f"(threshold ±{self.max_imbalance:.0%})."
            ),
            evidence={
                "venue": state.venue.value,
                "symbol": state.symbol,
                "open_interest_long": state.open_interest_long,
                "open_interest_short": state.open_interest_short,
                "imbalance": state.open_interest_imbalance,
                "data_available": True,
            },
        )


# ──────────────────────────────────────────────────────────────────────
# 5. Basis blowout
# ──────────────────────────────────────────────────────────────────────


class BasisBlowoutRisk:
    """Mark/index basis above stress threshold → funding spiral risk."""

    def __init__(self, max_basis_pct: float = 0.02):
        if max_basis_pct <= 0:
            raise ValueError("max_basis_pct must be > 0")
        self.max_basis_pct = max_basis_pct

    def run(self, state: PerpMarketState) -> RiskDetectorResult:
        b = abs(state.basis_pct)
        triggered = b > self.max_basis_pct
        severity = max(0.0, min(1.0, b / max(self.max_basis_pct, 1e-9)))
        return RiskDetectorResult(
            name="BasisBlowoutRisk",
            triggered=triggered,
            severity=severity,
            headline=(
                f"{state.venue.value} {state.symbol}: basis "
                f"{state.basis_pct:+.2%} vs threshold ±{self.max_basis_pct:.0%}."
            ),
            evidence={
                "venue": state.venue.value,
                "symbol": state.symbol,
                "mark_price": state.mark_price,
                "index_price": state.index_price,
                "basis_pct": state.basis_pct,
            },
        )


# ──────────────────────────────────────────────────────────────────────
# 6. Max-drawdown gate
# ──────────────────────────────────────────────────────────────────────


class MaxDrawdownGate:
    """Circuit-breaker — recommend close if realized PnL < -max_dd_pct of notional."""

    def __init__(self, max_dd_pct: float = 1.0):
        if max_dd_pct <= 0:
            raise ValueError("max_dd_pct must be > 0")
        self.max_dd_pct = max_dd_pct

    def run(self, position: Position, realized_pnl_usd: float) -> RiskDetectorResult:
        if position.notional_usd == 0:
            return RiskDetectorResult(
                name="MaxDrawdownGate",
                triggered=False,
                severity=0.0,
                headline="Position has zero notional; skipping.",
            )
        dd_pct = realized_pnl_usd / position.notional_usd * 100
        triggered = dd_pct < -self.max_dd_pct
        severity = max(0.0, min(1.0, -dd_pct / max(self.max_dd_pct, 1e-9))) if triggered else 0.0
        return RiskDetectorResult(
            name="MaxDrawdownGate",
            triggered=triggered,
            severity=severity,
            headline=(
                f"Realized PnL {dd_pct:+.2f}% of notional "
                f"vs DD threshold -{self.max_dd_pct:.1f}%."
            ),
            evidence={
                "realized_pnl_usd": realized_pnl_usd,
                "notional_usd": position.notional_usd,
                "drawdown_pct": dd_pct,
            },
        )


Detector = (
    FundingFlipRisk
    | LiquidityImbalance
    | DataStaleness
    | ConcentrationRisk
    | BasisBlowoutRisk
    | MaxDrawdownGate
)
