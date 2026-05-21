"""CLI: `dfm demo | scan | backtest`.

Commands:
  demo                          — generate synthetic signal + risk report.
  demo --html OUT.html          — write standalone HTML.
  scan SYMBOL                   — live scan (requires venue HTTP access).
  backtest [--samples N]        — run synthetic backtest demo.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .backtest import BacktestConfig, run_backtest
from .report import (
    backtest_as_html,
    backtest_as_json,
    backtest_as_markdown,
    signals_as_html,
    signals_as_json,
    signals_as_markdown,
)
from .runner import scan_symbol
from .signals import evaluate
from .synthetic import (
    make_cross_venue_quote,
    make_oscillating_spread,
)
from .venues import (
    BackpackClient,
    DriftClient,
    HyperliquidClient,
    OrderlyClient,
)


def _cmd_demo(args: argparse.Namespace) -> int:
    q = make_cross_venue_quote(
        high_hourly_rate=args.high_rate,
        low_hourly_rate=args.low_rate,
    )
    signal = evaluate(q)
    signals = [signal] if signal else []
    if args.html:
        Path(args.html).write_text(signals_as_html(signals, title="dfm Demo"))
        print(f"Wrote {args.html}", file=sys.stderr)
        return 0
    if args.json:
        print(signals_as_json(signals))
        return 0
    print(signals_as_markdown(signals, title="dfm Demo"))
    return 0


def _cmd_backtest(args: argparse.Namespace) -> int:
    stream = make_oscillating_spread(
        n_samples=args.samples,
        amplitude_hourly=args.amplitude,
        period_samples=args.period,
    )
    cfg = BacktestConfig()
    result = run_backtest(stream, cfg)
    if args.html:
        Path(args.html).write_text(backtest_as_html(result, title="dfm Backtest"))
        print(f"Wrote {args.html}", file=sys.stderr)
        return 0
    if args.json:
        print(backtest_as_json(result))
        return 0
    print(backtest_as_markdown(result, title="dfm Backtest"))
    return 0


def _cmd_scan(args: argparse.Namespace) -> int:
    clients = [
        DriftClient(),
        HyperliquidClient(),
        OrderlyClient(),
        BackpackClient(),
    ]
    result = asyncio.run(scan_symbol(clients, args.symbol))
    if result.signal is None:
        print(f"No actionable signal on {args.symbol} (quote={result.quote is not None}).")
        return 0
    print(signals_as_markdown([result.signal], title=f"Live scan — {args.symbol}"))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dfm", description="Drift Funding Monitor CLI")
    sub = p.add_subparsers(dest="command", required=True)

    p_demo = sub.add_parser("demo", help="Synthetic signal demo.")
    p_demo.add_argument("--high-rate", type=float, default=0.0005)
    p_demo.add_argument("--low-rate", type=float, default=0.0001)
    p_demo.add_argument("--json", action="store_true")
    p_demo.add_argument("--html", type=str, default=None)
    p_demo.set_defaults(func=_cmd_demo)

    p_bt = sub.add_parser("backtest", help="Synthetic backtest demo.")
    p_bt.add_argument("--samples", type=int, default=48)
    p_bt.add_argument("--amplitude", type=float, default=0.0003,
                      help="Hourly spread amplitude (e.g. 0.0003 = 30 bps/h peak)")
    p_bt.add_argument("--period", type=int, default=24, help="Sine period in samples")
    p_bt.add_argument("--json", action="store_true")
    p_bt.add_argument("--html", type=str, default=None)
    p_bt.set_defaults(func=_cmd_backtest)

    p_scan = sub.add_parser("scan", help="Live venue scan (HTTP).")
    p_scan.add_argument("symbol", type=str)
    p_scan.set_defaults(func=_cmd_scan)

    args = p.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
