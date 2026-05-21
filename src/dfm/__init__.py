"""drift-funding-monitor — cross-venue funding-rate arbitrage monitor.

Companion to `fundarb`: adds Drift Protocol (Solana perp DEX) to the cross-
venue arb universe and ships a backtest framework + 6 risk detectors for
already-open positions.

Quick start:
    >>> from dfm.synthetic import make_cross_venue_quote
    >>> from dfm.signals import evaluate
    >>> q = make_cross_venue_quote(high_hourly_rate=0.0005, low_hourly_rate=0.0001)
    >>> sig = evaluate(q)
    >>> sig.reason if sig else 'no signal'
"""

from .backtest import BacktestConfig, BacktestResult, Trade, run_backtest
from .report import (
    backtest_as_html,
    backtest_as_json,
    backtest_as_markdown,
    risks_as_json,
    signals_as_html,
    signals_as_json,
    signals_as_markdown,
)
from .risk import (
    BasisBlowoutRisk,
    ConcentrationRisk,
    DataStaleness,
    FundingFlipRisk,
    LiquidityImbalance,
    MaxDrawdownGate,
    RiskDetectorResult,
)
from .runner import ScanResult, evaluate_position_risks, scan_symbol
from .signals import ArbSignal, SignalThresholds, build_quote, evaluate
from .state import (
    CrossVenueQuote,
    FundingRate,
    PerpMarketState,
    Position,
    Side,
    Venue,
    VenueHistory,
)
from .venues import (
    BackpackClient,
    DriftClient,
    FakeVenueClient,
    HyperliquidClient,
    OrderlyClient,
    VenueClient,
    scan_all_venues,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # state
    "Venue", "Side", "FundingRate", "PerpMarketState", "CrossVenueQuote",
    "Position", "VenueHistory",
    # venues
    "VenueClient", "DriftClient", "HyperliquidClient", "OrderlyClient",
    "BackpackClient", "FakeVenueClient", "scan_all_venues",
    # signals
    "ArbSignal", "SignalThresholds", "build_quote", "evaluate",
    # risk
    "RiskDetectorResult", "FundingFlipRisk", "LiquidityImbalance",
    "DataStaleness", "ConcentrationRisk", "BasisBlowoutRisk", "MaxDrawdownGate",
    # backtest
    "BacktestConfig", "BacktestResult", "Trade", "run_backtest",
    # runner
    "ScanResult", "scan_symbol", "evaluate_position_risks",
    # reports
    "signals_as_json", "signals_as_markdown", "signals_as_html",
    "risks_as_json",
    "backtest_as_json", "backtest_as_markdown", "backtest_as_html",
]
