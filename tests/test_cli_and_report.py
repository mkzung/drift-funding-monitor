"""Tests for CLI + JSON / Markdown / HTML reporting."""

from __future__ import annotations

import json

from dfm.__main__ import main
from dfm.backtest import run_backtest
from dfm.report import (
    backtest_as_html,
    backtest_as_json,
    backtest_as_markdown,
    signals_as_html,
    signals_as_json,
    signals_as_markdown,
)
from dfm.signals import evaluate
from dfm.synthetic import make_cross_venue_quote, make_oscillating_spread


def _one_signal():
    q = make_cross_venue_quote(high_hourly_rate=0.0005, low_hourly_rate=0.0001)
    return [evaluate(q)]  # type: ignore[list-item]


def test_signals_json_round_trip():
    s = signals_as_json(_one_signal())
    parsed = json.loads(s)
    assert isinstance(parsed, list)
    assert parsed[0]["symbol"] == "SOL-PERP"


def test_signals_markdown_contains_venue_names():
    md = signals_as_markdown(_one_signal())
    assert "drift" in md.lower()
    assert "hyperliquid" in md.lower()


def test_signals_html_is_self_contained():
    h = signals_as_html(_one_signal())
    assert h.startswith("<!DOCTYPE html>")
    assert "</html>" in h
    assert "SOL-PERP" in h


def test_empty_signals_renders_gracefully():
    assert "No signals" in signals_as_markdown([])
    assert "No signals" in signals_as_html([])


def test_backtest_markdown_table_when_trades_present():
    stream = list(make_oscillating_spread(n_samples=48, amplitude_hourly=0.0005))
    result = run_backtest(stream)
    md = backtest_as_markdown(result)
    if result.n_trades > 0:
        assert "| open |" in md or "| total |" in md.lower()


def test_backtest_html_is_self_contained():
    stream = list(make_oscillating_spread(n_samples=24, amplitude_hourly=0.0005))
    result = run_backtest(stream)
    h = backtest_as_html(result)
    assert h.startswith("<!DOCTYPE html>")
    assert "</html>" in h


def test_backtest_json_includes_summary_and_trades():
    stream = list(make_oscillating_spread(n_samples=24, amplitude_hourly=0.0005))
    result = run_backtest(stream)
    parsed = json.loads(backtest_as_json(result))
    assert "summary" in parsed
    assert "trades" in parsed


# ──────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────


def test_cli_demo_markdown(capsys):
    rc = main(["demo"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "drift" in out.lower() or "no signals" in out.lower()


def test_cli_demo_json(capsys):
    rc = main(["demo", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    # Should parse as JSON list
    parsed = json.loads(out)
    assert isinstance(parsed, list)


def test_cli_demo_html(tmp_path):
    out_path = tmp_path / "demo.html"
    rc = main(["demo", "--html", str(out_path)])
    assert rc == 0
    content = out_path.read_text()
    assert "<!DOCTYPE html>" in content


def test_cli_backtest(capsys):
    rc = main(["backtest", "--samples", "24"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Trades:" in out or "n_trades" in out


def test_cli_backtest_json(capsys):
    rc = main(["backtest", "--samples", "24", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    parsed = json.loads(out)
    assert "summary" in parsed
