"""PnL backtest framework — replay a stream of CrossVenueQuote samples and
simulate an arb position that opens when signal fires + closes when:
  - spread converges below `close_spread_bps_per_hour`,
  - max-drawdown gate trips, or
  - holding-period cap is hit.

Outputs a `BacktestResult` with per-trade PnL, win-rate, Sharpe-ish metric.
This is a SIMULATOR, not a live executor — no order routing or slippage
model beyond fixed taker fees + half-spread crossing.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from statistics import StatisticsError, mean, stdev

from .signals import ArbSignal, SignalThresholds, evaluate
from .state import CrossVenueQuote, Position, Venue


@dataclass(frozen=True)
class Trade:
    """One round-trip arb trade."""

    symbol: str
    long_venue: Venue
    short_venue: Venue
    opened_at: int
    closed_at: int
    entry_size_usd: float
    funding_pnl_usd: float
    fees_usd: float
    basis_pnl_usd: float
    reason_close: str

    @property
    def hours_held(self) -> float:
        return max(0.0, (self.closed_at - self.opened_at) / 3600)

    @property
    def total_pnl_usd(self) -> float:
        return self.funding_pnl_usd + self.basis_pnl_usd - self.fees_usd

    @property
    def pnl_pct(self) -> float:
        if self.entry_size_usd == 0:
            return 0.0
        return self.total_pnl_usd / self.entry_size_usd * 100


@dataclass(frozen=True)
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)

    @property
    def n_trades(self) -> int:
        return len(self.trades)

    @property
    def total_pnl_usd(self) -> float:
        return sum(t.total_pnl_usd for t in self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        wins = sum(1 for t in self.trades if t.total_pnl_usd > 0)
        return wins / len(self.trades)

    @property
    def avg_pnl_pct(self) -> float:
        if not self.trades:
            return 0.0
        return mean(t.pnl_pct for t in self.trades)

    @property
    def pnl_pct_std(self) -> float:
        if len(self.trades) < 2:
            return 0.0
        try:
            return stdev(t.pnl_pct for t in self.trades)
        except StatisticsError:
            return 0.0

    @property
    def sharpe_proxy(self) -> float:
        """Mean PnL pct / stdev — NOT annualized, NOT risk-free-adjusted."""
        if self.pnl_pct_std == 0:
            return 0.0
        return self.avg_pnl_pct / self.pnl_pct_std

    def to_dict(self) -> dict[str, float]:
        return {
            "n_trades": float(self.n_trades),
            "total_pnl_usd": self.total_pnl_usd,
            "win_rate": self.win_rate,
            "avg_pnl_pct": self.avg_pnl_pct,
            "pnl_pct_std": self.pnl_pct_std,
            "sharpe_proxy": self.sharpe_proxy,
        }


@dataclass
class BacktestConfig:
    thresholds: SignalThresholds = field(default_factory=SignalThresholds)
    close_spread_bps_per_hour: float = 0.5      # close when spread shrinks below this
    max_holding_hours: float = 168.0            # 7 days cap
    max_drawdown_pct: float = 1.0               # gate
    one_position_per_symbol: bool = True


def run_backtest(
    quote_stream: Iterable[CrossVenueQuote],
    config: BacktestConfig | None = None,
) -> BacktestResult:
    """Replay a chronological stream of CrossVenueQuotes (one symbol).

    The simulator opens a position the first time `evaluate(quote)` returns
    a signal with confidence >= threshold; thereafter accrues funding PnL
    each iteration; closes when spread shrinks below `close_spread_bps_per_hour`,
    drawdown trips, or holding cap reached.
    """
    cfg = config or BacktestConfig()
    trades: list[Trade] = []
    open_position: Position | None = None
    open_signal: ArbSignal | None = None
    cumulative_funding_pnl = 0.0
    # last_accrual_ts: timestamp at which funding was last accrued.
    # Set to opened_at on open, then advanced to the current quote's timestamp
    # after each accrual. This is the FIX for the previous over-accrual bug
    # where delta_hours was measured from open_at on every iteration, causing
    # PnL to compound as the sum of an arithmetic series instead of summing
    # disjoint Δt intervals.
    last_accrual_ts: int = 0

    last_quote: CrossVenueQuote | None = None
    for quote in sorted(quote_stream, key=lambda q: q.timestamp):
        last_quote = quote
        if open_position is None:
            signal = evaluate(quote, cfg.thresholds)
            if signal is not None:
                # Open position at recommended size
                size_tokens_high = signal.recommended_size_usd / quote.high_venue.mark_price
                size_tokens_low = signal.recommended_size_usd / quote.low_venue.mark_price
                open_signal = signal
                open_position = Position(
                    symbol=quote.symbol,
                    opened_at=quote.timestamp,
                    long_venue=quote.low_venue.venue,
                    long_size_tokens=size_tokens_low,
                    long_entry_price=quote.low_venue.mark_price,
                    short_venue=quote.high_venue.venue,
                    short_size_tokens=size_tokens_high,
                    short_entry_price=quote.high_venue.mark_price,
                    entry_funding_diff_hourly=quote.high_venue.funding_rate.hourly_rate
                    - quote.low_venue.funding_rate.hourly_rate,
                )
                cumulative_funding_pnl = 0.0
                last_accrual_ts = quote.timestamp
            continue

        # Position is open — accrue funding PnL since last sample
        assert open_signal is not None
        # Funding accrual model: spread × notional × Δhours-since-last-quote.
        # Use trapezoidal-style approximation with the CURRENT spread; this
        # under-counts slightly when spread is decaying and over-counts when
        # it's accelerating, but is consistent across the loop.
        hours_held = (quote.timestamp - open_position.opened_at) / 3600
        current_spread_hourly = (
            quote.high_venue.funding_rate.hourly_rate
            - quote.low_venue.funding_rate.hourly_rate
        )
        delta_hours = max(0.0, (quote.timestamp - last_accrual_ts) / 3600)
        cumulative_funding_pnl += current_spread_hourly * open_position.notional_usd * delta_hours
        last_accrual_ts = quote.timestamp

        # Check close conditions
        close_reason = ""
        if current_spread_hourly * 10_000 < cfg.close_spread_bps_per_hour:
            close_reason = "spread_converged"
        elif hours_held >= cfg.max_holding_hours:
            close_reason = "max_holding_hit"
        elif (
            open_position.notional_usd > 0
            and cumulative_funding_pnl / open_position.notional_usd * 100
            < -cfg.max_drawdown_pct
        ):
            close_reason = "max_drawdown"

        if close_reason:
            # Estimate basis PnL — both legs were opened delta-neutral; if the
            # MARK prices have drifted, there's residual price PnL.
            long_pnl = (
                quote.low_venue.mark_price - open_position.long_entry_price
            ) * open_position.long_size_tokens
            short_pnl = (
                open_position.short_entry_price - quote.high_venue.mark_price
            ) * open_position.short_size_tokens
            basis_pnl = long_pnl + short_pnl
            fees = (
                open_position.notional_usd
                * cfg.thresholds.taker_fee_bps
                / 10_000
                * 4
            )
            trades.append(
                Trade(
                    symbol=quote.symbol,
                    long_venue=open_position.long_venue,
                    short_venue=open_position.short_venue,
                    opened_at=open_position.opened_at,
                    closed_at=quote.timestamp,
                    entry_size_usd=open_position.notional_usd,
                    funding_pnl_usd=cumulative_funding_pnl,
                    fees_usd=fees,
                    basis_pnl_usd=basis_pnl,
                    reason_close=close_reason,
                )
            )
            open_position = None
            open_signal = None
            cumulative_funding_pnl = 0.0

    # If position still open at end-of-data, force-close
    if open_position is not None and last_quote is not None:
        long_pnl = (
            last_quote.low_venue.mark_price - open_position.long_entry_price
        ) * open_position.long_size_tokens
        short_pnl = (
            open_position.short_entry_price - last_quote.high_venue.mark_price
        ) * open_position.short_size_tokens
        basis_pnl = long_pnl + short_pnl
        fees = (
            open_position.notional_usd * cfg.thresholds.taker_fee_bps / 10_000 * 4
        )
        trades.append(
            Trade(
                symbol=open_position.symbol,
                long_venue=open_position.long_venue,
                short_venue=open_position.short_venue,
                opened_at=open_position.opened_at,
                closed_at=last_quote.timestamp,
                entry_size_usd=open_position.notional_usd,
                funding_pnl_usd=cumulative_funding_pnl,
                fees_usd=fees,
                basis_pnl_usd=basis_pnl,
                reason_close="end_of_data",
            )
        )

    return BacktestResult(trades=trades)
