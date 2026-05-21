"""Orchestration: scan venues → build quote → emit signal → evaluate risks.

Two top-level entry points:

  - `async scan_symbol(clients, symbol, ...)` — one-shot multi-venue fetch +
    signal evaluation. Returns (signal_or_none, list_of_risk_results).
  - `evaluate_position_risks(quote, position, ...)` — given an open position
    and current quote, run all 6 risk detectors.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .risk import (
    BasisBlowoutRisk,
    ConcentrationRisk,
    DataStaleness,
    FundingFlipRisk,
    LiquidityImbalance,
    MaxDrawdownGate,
    RiskDetectorResult,
)
from .signals import ArbSignal, SignalThresholds, build_quote, evaluate
from .state import CrossVenueQuote, Position
from .venues import VenueClient, scan_all_venues


@dataclass
class ScanResult:
    symbol: str
    timestamp: int
    quote: CrossVenueQuote | None
    signal: ArbSignal | None
    pre_open_risks: list[RiskDetectorResult] = field(default_factory=list)


async def scan_symbol(
    clients: list[VenueClient],
    symbol: str,
    thresholds: SignalThresholds | None = None,
) -> ScanResult:
    """One-shot symbol scan: fan out to all venue clients, build the quote,
    evaluate signal, run pre-open risk detectors (staleness, basis, concentration).

    Returns a ScanResult with `signal=None` if no actionable arb is found.
    """
    states = await scan_all_venues(clients, symbol)
    timestamp = int(time.time())
    quote = build_quote(states, symbol, timestamp)
    if quote is None:
        return ScanResult(symbol=symbol, timestamp=timestamp, quote=None, signal=None)

    signal = evaluate(quote, thresholds)

    # Pre-open risk detectors — don't depend on an existing Position.
    pre_open: list[RiskDetectorResult] = [
        DataStaleness().run(quote),
        ConcentrationRisk().run(quote.high_venue),
        ConcentrationRisk().run(quote.low_venue),
        BasisBlowoutRisk().run(quote.high_venue),
        BasisBlowoutRisk().run(quote.low_venue),
    ]

    return ScanResult(
        symbol=symbol,
        timestamp=timestamp,
        quote=quote,
        signal=signal,
        pre_open_risks=pre_open,
    )


def evaluate_position_risks(
    quote: CrossVenueQuote,
    position: Position,
    hours_held: float,
    realized_pnl_usd: float = 0.0,
) -> list[RiskDetectorResult]:
    """All 6 risk detectors fired against an open position."""
    return [
        FundingFlipRisk().run(quote, position, hours_held),
        LiquidityImbalance().run(quote, position),
        DataStaleness().run(quote),
        ConcentrationRisk().run(quote.high_venue),
        BasisBlowoutRisk().run(quote.high_venue),
        MaxDrawdownGate().run(position, realized_pnl_usd),
    ]
